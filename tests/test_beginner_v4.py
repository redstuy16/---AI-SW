from probe.report_publication import report_root
"""초보자 기본값·휴지통·한정 연구 절차의 실제 저장 경계를 검사한다."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import pytest

from probe.autonomous_loop import AutonomousResearchLoop
from probe.climate_profile import ClimateComparisonProfile, MEANING, parse_source, transform_rows, independent_comparison
from probe.control_plane import ControlError, ROLES
from probe.final_report import export_final_report
from probe.providers.fake import FakeProvider
from probe.qualified_profiles import ProfileRegistry, conclusion_card
from probe.qualified_workflow import execute_profile, update_source
from probe.recovery import FaultInjector, InjectedCrash
from probe.research_lifecycle import trash, restore, cleanup, purge
from probe.resource_policy import preferences
from probe.schemas import utc_now
from test_workbench import app, configure, create

SOURCE = Path(__file__).resolve().parents[1] / "qa/qualified_profiles/public/gistemp.txt"
QUESTION = "1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요."


def prepare(app, question=QUESTION):
    configure(app)
    created = app.create({"beginner_mode": True, "question": question, "egress": "selected"})
    provider = FakeProvider([{"decision": "PROCEED", "reason": "기록된 공식 연간 자료의 두 기간 비교"}])
    runtime = AutonomousResearchLoop(app.read._state, provider, models={r: "manual-id" for r in ROLES})
    return created["research_id"], created["snapshot"], runtime, provider


def run_profile(app):
    rid, snapshot, runtime, provider = prepare(app)
    result = asyncio.run(execute_profile(runtime, rid, snapshot, source_text=SOURCE.read_text(encoding="utf-8")))
    return rid, snapshot, runtime, provider, result


def test_beginner_question_only_resolves_real_defaults(app):
    configure(app)
    snap = app.prepare({"beginner_mode": True, "question": "공개 자료를 검토해 주세요.", "egress": "selected"})
    assert snap["model_profile_id"] == "m"
    assert snap["routing"] == {r: "m" for r in ROLES}
    assert snap["run_limit_usd"] == "0.10" and snap["attachments"] == []
    assert snap["report_format"] == "pdf" and snap["search_attempt_limit"] == 10
    assert snap["adaptive_budget"] and not snap["search_required"]
    assert app.preflight(snap)["ready"]
    assert app.store.ledger()["requests"] == []


def test_explicit_zero_false_and_advanced_precedence(app):
    configure(app)
    snap = app.prepare({"beginner_mode": True, "question": "자료 비교", "egress": "selected",
                        "adaptive_budget": False, "search_attempt_limit": 0, "search_required": False,
                        "performance_profile": "FAST", "advanced_performance_profile": "DEEP"})
    assert not snap["adaptive_budget"] and not snap["search_required"]
    assert snap["search_attempt_limit"] == 0 and snap["performance_profile"] == "DEEP"


def test_preferences_reset_preserves_credentials_files_and_research(app):
    configure(app)
    rid = create(app)
    old_files = sorted(str(p) for p in app.workspace.rglob("*"))
    app.request("POST", "/api/control/preferences", {"new_research_explanations": True, "explanation_prompt_dismissed": True})
    assert preferences(app.store)["new_research_explanations"]
    assert app.request("POST", "/api/control/preferences/reset").status == 200
    assert not preferences(app.store)["new_research_explanations"]
    assert app.store.db.execute("SELECT 1 FROM research_runs WHERE research_id=?", (rid,)).fetchone() and len(app.connections()) == 1
    assert sorted(str(p) for p in app.workspace.rglob("*")) == old_files


def test_trash_restore_rename_and_hidden_default_list(app):
    configure(app)
    rid = create(app)
    assert app.request("POST", f"/api/control/research/{rid}/rename", {"title": "변경한 제목"}).status == 200
    assert app.request("POST", f"/api/control/research/{rid}/trash").status == 200
    assert app.request("GET", "/api/control/research").body == []
    items = app.request("GET", "/api/control/research?trash=1").body
    assert len(items) == 1 and items[0]["title"] == "변경한 제목"
    assert app.request("POST", f"/api/control/research/{rid}/start", {"expected_version": 0, "idempotency_key": "trash-start-test"}).body["error"] == "RESEARCH_IN_TRASH"
    assert app.request("POST", f"/api/control/research/{rid}/restore").status == 200
    assert len(app.request("GET", "/api/control/research").body) == 1


def test_trash_retention_29_and_30_days(app):
    configure(app)
    rid = create(app)
    now = utc_now()
    trash(app, rid, now=now)
    assert cleanup(app, now=now + timedelta(days=29)) == []
    assert app.store.db.execute("SELECT 1 FROM research_runs WHERE research_id=?", (rid,)).fetchone()
    assert cleanup(app, now=now + timedelta(days=30))[0]["status"] == "PURGED"
    assert not (app.workspace / rid).exists()
    assert (app.workspace / "inputs/data.csv").is_file()
    assert app.store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_permanent_delete_requires_confirmation_and_preserves_other_run(app):
    configure(app)
    first, second = create(app), create(app)
    trash(app, first)
    assert app.request("POST", f"/api/control/research/{first}/purge").status == 409
    assert app.request("POST", f"/api/control/research/{first}/purge", {"confirm": True}).status == 200
    assert app.store.db.execute("SELECT 1 FROM research_runs WHERE research_id=?", (second,)).fetchone()
    assert (app.workspace / second).is_dir()


@pytest.mark.parametrize("status", ["RUNNING", "STARTING", "RESUMING", "PAUSE_REQUESTED", "STOP_REQUESTED"])
def test_active_research_cannot_be_deleted(app, status):
    configure(app)
    rid = create(app)
    app.store.db.execute("UPDATE control_runs SET status=? WHERE research_id=?", (status, rid))
    response = app.request("POST", f"/api/control/research/{rid}/trash")
    assert response.status == 409 and response.body["error"] == "RESEARCH_PAUSE_BEFORE_DELETE"
    assert app.request("GET", "/api/control/research").body[0]["research_id"] == rid


def test_live_worker_blocks_trash_even_when_control_status_is_stopped(app, monkeypatch):
    configure(app)
    rid = create(app)
    app.store.db.execute("UPDATE control_runs SET status='STOPPED',pid=12345 WHERE research_id=?", (rid,))
    monkeypatch.setattr("probe.research_lifecycle.process_alive", lambda pid: pid == 12345)
    response = app.request("POST", f"/api/control/research/{rid}/trash")
    assert response.status == 409 and response.body["error"] == "RESEARCH_PAUSE_BEFORE_DELETE"


@pytest.mark.parametrize("question,status", [
    (QUESTION, "SUPPORTED"),
    ("전 지구 기온은 왜 상승했나요?", "UNSUPPORTED"),
    ("전 지구 기온을 미래에 예측해 주세요.", "UNSUPPORTED"),
    ("서울 1981~2000년과 2001~2020년 기온 비교", "UNSUPPORTED"),
    ("전 지구 기온 편차를 비교해 주세요.", "CLARIFICATION_REQUIRED"),
])
def test_profile_scope_keeps_original_goal(question, status):
    assert ClimateComparisonProfile().candidate(question)["status"] == status


def test_profile_core_is_generic_and_future_registration(app):
    class Future:
        profile_id = "synthetic_future_v1"
        display_name = "격리 검사"
        qualified = True
        def candidate(self, question):
            return {"status": "SUPPORTED"} if question == "합성 질문" else None
    registry = ProfileRegistry()
    registry.register(Future())
    assert registry.choose("합성 질문")["profile_id"] == Future.profile_id
    assert registry.choose("다른 질문")["status"] == "GENERAL"
    rid = app.read._state.create_research("빛의 세기")
    assert app.store.db.execute("SELECT 1 FROM research_runs WHERE research_id=?", (rid,)).fetchone()
    assert not any("climate" in c[1] or "temperature" in c[1] for c in app.store.db.execute("PRAGMA table_info(research_runs)"))


def test_real_public_profile_commits_claim_and_card(app):
    rid, snap, runtime, provider, result = run_profile(app)
    card = conclusion_card(app.read._state, rid)
    assert card["current"] and card["question"] == QUESTION
    assert card["record"]["execution"] == "fake"
    assert len(provider.calls) == 1
    assert all(c["passed"] for c in card["checks"])
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0] == 1
    claim = app.read._state.research_slice.current(rid)[0]
    assert claim.claim_type == "comparison"
    assert any(s.name == "difference" for s in claim.numeric_slots)
    assert claim.scope["qualified_profile"]["semantics"] == MEANING


def test_profile_export_pdf_and_external_replay(app):
    from probe.qualified_replay import replay
    from probe.release import validate_report_snapshot
    from probe.report_pdf import render_pdf
    rid, snap, runtime, provider, result = run_profile(app)
    state = app.read._state
    state.stop_research(rid, "QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(state, rid)
    assert validate_report_snapshot(state, rid)
    manifest = (__import__("probe.report_publication", fromlist=["report_root"]).report_root(state, rid) / 'replay_manifest.json')
    assert replay(manifest)["paid_calls"] == 0
    assert render_pdf(state, rid)["data"].startswith(b"%PDF-")
    asyncio.run(execute_profile(runtime, rid, snap, replay=True))
    export_final_report(state, rid)
    assert validate_report_snapshot(state, rid)


def revise(text, year, delta):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(str(year) + " "):
            cells = line.split()
            cells[13] = str(int(cells[13]) + delta)
            lines[index] = " ".join(cells)
    return "\n".join(lines) + "\n"


def test_used_revision_stale_and_paid_free_revalidation(app):
    rid, snap, runtime, provider, result = run_profile(app)
    original = SOURCE.read_text(encoding="utf-8")
    changed = update_source(app.read._state, rid, revise(original, 2005, 10))
    assert changed["affected"]
    assert not conclusion_card(app.read._state, rid)["current"]
    new = asyncio.run(execute_profile(runtime, rid, snap, replay=True))
    assert new["analysis_revision"] == 2 and len(provider.calls) == 1
    assert conclusion_card(app.read._state, rid)["current"]
    assert len(conclusion_card(app.read._state, rid)["history"]) == 3


def test_unused_revision_preserves_conclusion(app):
    rid, snap, runtime, provider, result = run_profile(app)
    changed = update_source(app.read._state, rid, revise(SOURCE.read_text(encoding="utf-8"), 1900, 10))
    assert not changed["affected"] and conclusion_card(app.read._state, rid)["current"]
    assert len(provider.calls) == 1


def test_semantic_revision_requires_clarification(app):
    rid, snap, runtime, provider, result = run_profile(app)
    metadata = deepcopy(MEANING)
    metadata["storage_scale"] = "1"
    result = update_source(app.read._state, rid, SOURCE.read_text(encoding="utf-8"), semantics=metadata)
    assert result["affected"] and result["status"] == "CLARIFICATION_REQUIRED"
    assert not conclusion_card(app.read._state, rid)["current"]
    with pytest.raises(ControlError, match="CLARIFICATION"):
        asyncio.run(execute_profile(runtime, rid, snap, replay=True))


def test_meaning_preserving_fahrenheit_and_baseline_transform():
    rows = parse_source(SOURCE.read_text(encoding="utf-8"), MEANING)
    periods = [[1981, 2000], [2001, 2020]]
    _, expected = independent_comparison(rows, periods)
    transformed = transform_rows(rows, {"unit": "degF_difference", "baseline_period": [1961, 1990]})
    _, actual = independent_comparison(transformed, periods)
    assert float(actual) == pytest.approx(float(expected) * 1.8)
    with pytest.raises(ValueError):
        transform_rows(rows, {"absolute_offset": 32})


@pytest.mark.parametrize("boundary", ["profile_after_verify", "profile_after_commit"])
def test_profile_crash_resume_no_second_agent_or_commit(app, boundary):
    rid, snap, runtime, provider = prepare(app)
    runtime.faults = FaultInjector(lambda name: (_ for _ in ()).throw(InjectedCrash()) if name == boundary else None)
    with pytest.raises(InjectedCrash):
        asyncio.run(execute_profile(runtime, rid, snap, source_text=SOURCE.read_text(encoding="utf-8")))
    runtime.faults = FaultInjector()
    asyncio.run(execute_profile(runtime, rid, snap))
    assert len(provider.calls) == 1
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0] == 1
    assert conclusion_card(app.read._state, rid)["current"]


@pytest.mark.parametrize("fault", ["numeric", "scope", "binding"])
def test_profile_fault_not_committed(app, fault):
    rid, snap, runtime, provider = prepare(app)
    def corrupt(payload):
        value = payload.model_copy(deep=True)
        if fault == "numeric":
            value.tool_result.result["difference"] += 1
        elif fault == "scope":
            value.scientific.claim = "이 차이는 온난화의 원인입니다."
        else:
            value.agent_result.provenance.pop("qualified_profile")
        return value
    with pytest.raises(ControlError, match="VERIFICATION_FAILED"):
        asyncio.run(execute_profile(runtime, rid, snap, source_text=SOURCE.read_text(encoding="utf-8"), fault=corrupt))
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0] == 0


def test_replay_uses_saved_source_without_network(app, monkeypatch):
    rid, snap, runtime, provider, result = run_profile(app)
    async def forbidden(*args):
        raise AssertionError("네트워크를 다시 읽었습니다")
    monkeypatch.setattr("probe.qualified_workflow.fetch_source", forbidden)
    asyncio.run(execute_profile(runtime, rid, snap, replay=True))
    assert len(provider.calls) == 1 and conclusion_card(app.read._state, rid)["current"]


def test_reverted_source_is_latest_not_old_row_order(app):
    from probe.qualified_workflow import latest_source
    rid, snap, runtime, provider, result = run_profile(app)
    original = SOURCE.read_text(encoding="utf-8")
    a = update_source(app.read._state, rid, revise(original, 2005, 10))
    b = update_source(app.read._state, rid, original)
    assert latest_source(app.read._state, rid)["source_sha256"] == b["source"]["source_sha256"]
    assert a["source"]["source_sha256"] != b["source"]["source_sha256"]
    asyncio.run(execute_profile(runtime, rid, snap, replay=True))
    assert conclusion_card(app.read._state, rid)["current"]


def test_unused_revised_source_is_exported_and_tamper_blocked(app):
    from probe.release import validate_report_snapshot, ReleaseExportError
    rid, snap, runtime, provider, result = run_profile(app)
    update_source(app.read._state, rid, revise(SOURCE.read_text(encoding="utf-8"),1900,10))
    app.read._state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED")
    root = export_final_report(app.read._state, rid)
    document = json.loads((report_root(app.read._state, rid) / "qualified_profile.json").read_text(encoding="utf-8"))
    raw = (report_root(app.read._state, rid) / "qualified_sources" / Path(document["current_source"]["source_relative"]).name)
    assert raw.is_file() and validate_report_snapshot(app.read._state,rid)
    raw.write_bytes(raw.read_bytes()+b"tampered")
    with pytest.raises(ReleaseExportError,match="hash mismatch"):
        validate_report_snapshot(app.read._state,rid)


def test_external_replay_manifest_tamper_blocked(app):
    from probe.qualified_replay import replay
    rid, snap, runtime, provider, result = run_profile(app)
    app.read._state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(app.read._state,rid)
    path=(report_root(app.read._state, rid) / "replay_manifest.json")
    document=json.loads(path.read_text(encoding="utf-8"));document["binding"]["plan"]["periods"]=[[1981,1990],[2001,2010]]
    text=json.dumps(document,ensure_ascii=False);text.encode("utf-8",errors="strict");path.write_text(text,encoding="utf-8")
    with pytest.raises(ValueError,match="MANIFEST_HASH"):
        replay(path)


def test_pdf_contains_six_card_questions(app):
    import io
    from pypdf import PdfReader
    from probe.report_pdf import render_pdf
    rid, snap, runtime, provider, result = run_profile(app)
    app.read._state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(app.read._state,rid)
    pdf=render_pdf(app.read._state,rid)["data"]
    text="".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert all(q in text for q in ["무엇을 알아본 결과인가?","어떤 자료를 사용했나?","무엇을 계산했나?","어디까지 말할 수 있나?","무엇은 아직 확인하지 못했나?","이 결론은 현재도 유효한가?"])
    assert "0.01" in text and "1951" in text


@pytest.mark.parametrize("defect",["missing","duplicate","alignment","column","nonfinite"])
def test_source_defects_do_not_commit(app,defect):
    rid,snap,runtime,provider=prepare(app)
    lines=SOURCE.read_text(encoding="utf-8").splitlines();i=next(i for i,s in enumerate(lines) if s.startswith("2005 "))
    if defect=="duplicate":lines.insert(i,lines[i])
    elif defect=="column":lines=[s.replace("J-D","NOT-ANNUAL") for s in lines]
    else:
        cells=lines[i].split()
        if defect=="missing":cells[13]="***"
        elif defect=="alignment":cells[-1]="2004"
        else:cells[13]="NaN"
        lines[i]=" ".join(cells)
    with pytest.raises(ValueError):
        asyncio.run(execute_profile(runtime,rid,snap,source_text="\n".join(lines)))
    assert not app.store.db.execute("SELECT 1 FROM state_events WHERE research_id=?",(rid,)).fetchone()
    assert not provider.calls


@pytest.mark.parametrize("question",[
 "1981~1990년과 2001~2010년의 전 지구 기온 편차 평균 비교",
 "1981~2000년과 2001~2020년의 전 지구 기온 편차 평균을 화씨 편차로 비교",
 "기준 기간을 1961~1990년으로 바꾸고 1981~2000년과 2001~2020년의 전 지구 기온 편차 평균 비교"])
def test_approved_partial_and_transform_are_not_false_held(app,question):
    rid,snap,runtime,provider=prepare(app,question)
    asyncio.run(execute_profile(runtime,rid,snap,source_text=SOURCE.read_text(encoding="utf-8")))
    card=conclusion_card(app.read._state,rid)
    assert card["current"] and card["question"]==question


def test_qualifed_stop_requires_current_verified_claim(app):
    from probe.service import ContractViolationError
    rid,snap,runtime,provider,result=run_profile(app)
    update_source(app.read._state,rid,revise(SOURCE.read_text(encoding="utf-8"),2005,10))
    with pytest.raises(ContractViolationError):app.read._state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED")


def test_progress_is_saved_execution_not_card_only(app):
    configure(app);rid=create(app)
    value=app.request("GET",f"/api/control/research/{rid}/beginner-progress")
    assert value.status==200 and [s["done"] for s in value.body["steps"]]==[True,False,False,False,False,False]


def test_connection_delete_confirmation_revision_and_key_boundary(app,monkeypatch):
    configure(app);seen=[]
    app.store.db.execute("UPDATE control_configs SET payload=json_set(payload,'$.credential_env_name','QA_ONLY_KEY') WHERE kind='connection'")
    monkeypatch.setattr(app.credentials,"save",lambda name,value: seen.append((name,value)) or {"deleted":True})
    path="/api/control/connections/local/delete"
    assert app.request("POST",path).body["error"]=="CONNECTION_DELETE_CONFIRMATION_REQUIRED"
    assert app.request("POST",path,{"confirm":True,"expected_revision":0}).body["error"]=="CONFIG_STALE"
    assert app.request("POST",path,{"confirm":True,"expected_revision":1}).status==200
    assert not app.connections() and not app.store.configs("model") and seen==[("QA_ONLY_KEY",None)]
    assert len(app.store.ledger()["requests"])==0


def test_connection_delete_in_use_and_shared_key(app,monkeypatch):
    from probe.control_plane import Connection
    c,m=configure(app);rid=create(app)
    app.store.db.execute("UPDATE control_runs SET status='PAUSED' WHERE research_id=?",(rid,))
    path="/api/control/connections/local/delete"
    assert app.request("POST",path,{"confirm":True,"expected_revision":1}).body["error"]=="CONNECTION_IN_USE"
    app.store.db.execute("UPDATE control_runs SET status='STOPPED' WHERE research_id=?",(rid,))
    app.store.db.execute("UPDATE control_configs SET payload=json_set(payload,'$.credential_env_name','QA_SHARED_KEY') WHERE kind='connection'")
    app.store.put("connection","other",c.model_copy(update={"connection_id":"other","credential_env_name":"QA_SHARED_KEY"}))
    monkeypatch.setattr(app.credentials,"save",lambda *args: (_ for _ in ()).throw(AssertionError("공유 키 삭제")))
    response=app.request("POST",path,{"confirm":True,"expected_revision":1})
    assert response.status==200 and response.body["shared_key_preserved"]
    assert app.connections()[0]["connection_id"]=="other"


def test_purge_interrupted_after_db_recovers_on_cleanup(app,monkeypatch):
    import probe.research_lifecycle as life
    configure(app);rid=create(app);trash(app,rid)
    original=life.shutil.rmtree
    monkeypatch.setattr(life.shutil,"rmtree",lambda *args: (_ for _ in ()).throw(OSError("QA interruption")))
    with pytest.raises(OSError):purge(app,rid)
    assert life.lifecycle(app,rid)["status"]=="PURGING" and (app.workspace/".research-trash"/rid).is_dir()
    monkeypatch.setattr(life.shutil,"rmtree",original)
    assert cleanup(app)[0]["status"]=="PURGED"
    assert app.store.db.execute("PRAGMA foreign_key_check").fetchall()==[]


def test_budget_finish_is_explicit_idle_and_does_not_raise_cap(app):
    configure(app);rid=create(app);state=app.read._state;state.configure_budget(rid,1,1,2)
    path=f"/api/control/research/{rid}/finish-current-budget"
    assert app.request("POST",path).body["error"]=="BUDGET_FINISH_REQUIRES_PAUSE"
    app.store.db.execute("UPDATE control_runs SET status='PAUSED' WHERE research_id=?",(rid,))
    before=app.store.defaults().model_dump(mode="json")
    response=app.request("POST",path)
    assert response.status==200 and response.body["paid_calls"]==0
    assert app.store.defaults().model_dump(mode="json")==before and app.store.run(rid)["status"]=="STOPPED"


@pytest.mark.parametrize("ledger_status,run_status", [("RESERVED", "PAUSED"), ("DISPATCHED", "FAILED"), ("UNRESOLVED", "NEEDS_RECONCILIATION")])
def test_unsettled_spend_allows_trash_and_restore_without_changing_costs(app, ledger_status, run_status):
    configure(app)
    rid = create(app)
    identity = app.store.reserve(rid=rid, connection="local", model="manual-id", role="manager", purpose="QA",
                                 bound=0.01, run_limit=0.1, monthly_limit=20, request_limit=0.1, attempts=2, revision="QA")
    if ledger_status != "RESERVED":
        app.store.transition(identity, "DISPATCHED")
    if ledger_status == "UNRESOLVED":
        app.store.transition(identity, "UNRESOLVED")
    app.store.db.execute("UPDATE control_runs SET status=? WHERE research_id=?", (run_status, rid))
    checkpoint = {"research_id": rid, "reservation_id": identity, "response_id": "qa-response", "usage": {"input_tokens": 100}}
    app.store.put("provider_response_checkpoint", identity, checkpoint)
    before_ledger = app.store.ledger()
    before_run = app.store.run(rid)
    before_files = sorted(str(p) for p in (app.workspace / rid).rglob("*"))
    response = app.request("POST", f"/api/control/research/{rid}/trash")
    assert response.status == 200 and response.body["status"] == "TRASH"
    assert app.request("GET", "/api/control/research").body == []
    assert app.request("GET", "/api/control/research?trash=1").body[0]["research_id"] == rid
    assert app.store.ledger() == before_ledger
    assert app.store.run(rid) == before_run
    assert sorted(str(p) for p in (app.workspace / rid).rglob("*")) == before_files
    assert app.store.config("provider_response_checkpoint", identity) == checkpoint
    assert app.request("POST", f"/api/control/research/{rid}/trash").body == response.body
    # 아직 예약·전송 중인 요청은 영구 삭제를 차단한다.
    if ledger_status != "UNRESOLVED":
        permanent = app.request("POST", f"/api/control/research/{rid}/purge", {"confirm": True})
        assert permanent.status == 409 and permanent.body["error"] == "NEEDS_RECONCILIATION"
    assert app.request("POST", f"/api/control/research/{rid}/restore").status == 200
    assert app.request("GET", "/api/control/research").body[0]["research_id"] == rid
    app.store.db.execute("UPDATE control_runs SET status='PAUSED' WHERE research_id=?", (rid,))
    from probe.research_lifecycle import _idle
    if ledger_status == "UNRESOLVED":
        _idle(app, rid)
    else:
        with pytest.raises(ControlError, match="NEEDS_RECONCILIATION"):
            _idle(app, rid)
    resumed = app.request("POST", f"/api/control/research/{rid}/resume",
                          {"expected_version": before_run["version"], "idempotency_key": "unsettled-trash-resume"})
    if ledger_status == "UNRESOLVED":
        assert resumed.status == 200
    else:
        assert resumed.status == 409 and resumed.body["error"] == "REQUEST_IN_FLIGHT"
    assert app.store.ledger() == before_ledger
    assert app.store.config("provider_response_checkpoint", identity) == checkpoint


def test_reserved_spend_financial_history_survives_purge_after_released(app):
    configure(app)
    rid = create(app)
    identity = app.store.reserve(rid=rid, connection="local", model="manual-id", role="manager", purpose="QA",
                                 bound=0.01, run_limit=0.1, monthly_limit=20, request_limit=0.1, attempts=2, revision="QA")
    trash(app, rid)
    app.store.transition(identity, "RELEASED")
    assert purge(app, rid)["status"] == "PURGED"
    assert app.store.db.execute("SELECT status FROM spend_ledger WHERE id=?", (identity,)).fetchone()[0] == "RELEASED"
    assert app.store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_qualified_preflight_reserves_actual_paid_role_without_weakening_legacy(app):
    from probe.control_plane import PriceRecord, admitted_cost
    from decimal import Decimal
    c,m=configure(app)
    m=m.model_copy(update={"local_api_unmetered":False,"price":PriceRecord(input_per_million=1,output_per_million=30,checked_at=utc_now(),revision="QA",source="QA fixture",owner_verified=True)})
    app.store.put("model","m",m,1)
    body={"beginner_mode":True,"question":QUESTION,"model_profile_id":"m","egress":"selected","run_limit_usd":"0.10"}
    response=app.request("POST","/api/control/research/preflight",body)
    assert response.status==200 and response.body["ready"]
    assert Decimal(response.body["minimum_role_call_bound_usd"])==admitted_cost(m,min(4096,m.input_byte_limit))
    body["research_profile_mode"]="DISABLED"
    legacy=app.request("POST","/api/control/research/preflight",body)
    assert "COMPLETION_RESERVE_BLOCKED" in legacy.body["reasons"]
    assert not app.store.ledger()["requests"]


def test_unused_refresh_keeps_pdf_current_without_recalculation(app,monkeypatch):
    from probe.release import validate_report_snapshot
    rid,snap,runtime,provider,result=run_profile(app)
    app.read._state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(app.read._state,rid)
    async def source(snapshot):return revise(SOURCE.read_text(encoding="utf-8"),1900,10)
    monkeypatch.setattr("probe.qualified_workflow.fetch_source",source)
    response=app.request("POST",f"/api/control/research/{rid}/refresh-source")
    assert response.status==200 and not response.body["affected"]
    assert validate_report_snapshot(app.read._state,rid) and len(provider.calls)==1
    assert conclusion_card(app.read._state,rid)["current"]


@pytest.mark.parametrize("compatible",[True,False])
def test_profile_preserves_existing_dependency_and_reliability_gates(app,compatible):
    from probe.research_slice_schemas import ResearchSliceConfig
    rid,snap,runtime,provider=prepare(app)
    app.read._state.research_slice.configure(rid,ResearchSliceConfig(claim_evidence_provenance=compatible,verifier_dependency_catalog=True,reliability_lab=True))
    if not compatible:
        with pytest.raises(ControlError,match="PROFILE_POLICY_INCOMPATIBLE"):
            asyncio.run(execute_profile(runtime,rid,snap,source_text=SOURCE.read_text(encoding="utf-8")))
        config=app.read._state.research_slice.config(rid)
        assert not config.claim_evidence_provenance and config.verifier_dependency_catalog and config.reliability_lab
        assert not provider.calls
        return
    asyncio.run(execute_profile(runtime,rid,snap,source_text=SOURCE.read_text(encoding="utf-8")))
    config=app.read._state.research_slice.config(rid)
    assert config.verifier_dependency_catalog and config.reliability_lab
    assert config.claim_evidence_provenance and config.revision_invalidation
    assert conclusion_card(app.read._state,rid)["current"]


@pytest.mark.parametrize("automatic", [False, True])
def test_unresolved_trash_can_be_purged_with_billing_evidence_retained(app, automatic):
    configure(app)
    rid, other = create(app), create(app)
    identity = app.store.reserve(rid=rid, connection="local", model="manual-id", role="manager", purpose="QA",
                                 bound=0.01, run_limit=0.1, monthly_limit=20, request_limit=0.1, attempts=2, revision="QA")
    app.store.transition(identity, "DISPATCHED")
    app.store.transition(identity, "UNRESOLVED")
    app.store.db.execute("UPDATE control_runs SET status='NEEDS_RECONCILIATION',pid=NULL WHERE research_id=?", (rid,))
    usage = {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 10}
    app.store.put("provider_response_checkpoint", identity, {"research_id": rid, "request_key": "qa-request", "profile_id": "m",
        "result": {"response_id": "qa-response", "model_id": "manual-id", "usage": usage, "output_text": "삭제할 연구 응답 본문"}})
    now = utc_now()
    trash(app, rid, now=now)
    before = app.store.ledger()
    shared = (app.workspace / "inputs/data.csv").read_bytes()
    if automatic:
        assert cleanup(app, now=now + timedelta(days=30))[0]["status"] == "PURGED"
    else:
        unconfirmed = app.request("POST", f"/api/control/research/{rid}/purge")
        assert unconfirmed.body["error"] == "PERMANENT_DELETE_CONFIRMATION_REQUIRED"
        response = app.request("POST", f"/api/control/research/{rid}/purge", {"confirm": True})
        assert response.status == 200 and response.body["status"] == "PURGED"
    assert app.store.ledger() == before
    assert not (app.workspace / rid).exists()
    assert not (app.workspace / ".research-trash" / rid).exists()
    assert (app.workspace / "inputs/data.csv").read_bytes() == shared
    assert (app.workspace / other).is_dir()
    assert not app.store.db.execute("SELECT 1 FROM research_runs WHERE research_id=?", (rid,)).fetchone()
    assert not app.store.db.execute("SELECT 1 FROM control_runs WHERE research_id=?", (rid,)).fetchone()
    assert not app.store.db.execute("SELECT 1 FROM control_configs WHERE kind='provider_response_checkpoint' AND id=?", (identity,)).fetchone()
    row = app.store.db.execute("SELECT payload FROM control_audit WHERE research_id=? AND kind='PURGED_BILLING_EVIDENCE'", (rid,)).fetchone()
    details = json.loads(row[0])
    assert details == {"reservation_id": identity, "response_id": "qa-response", "model_id": "manual-id", "usage": usage,
                       "request_key": "qa-request", "profile_id": "m"}
    assert "삭제할 연구 응답 본문" not in row[0]
    assert app.request("GET", "/api/control/research?trash=1").body == []
    assert app.request("POST", f"/api/control/research/{rid}/restore").body["error"] == "RESEARCH_NOT_RESTORABLE"
    assert purge(app, rid)["status"] == "PURGED"
    assert app.store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    # 삭제된 연구는 실행하지 못하며 미정산 노출은 월 예산에 계속 반영한다.
    assert app.request("POST", f"/api/control/research/{rid}/start", {"expected_version": 0, "idempotency_key": "purged-start"}).body["error"] == "RESEARCH_IN_TRASH"
    with pytest.raises(ControlError, match="BUDGET_BLOCKED"):
        app.store.reserve(rid="new-research", connection="local", model="manual-id", role="manager", purpose="QA",
                          bound=0.01, run_limit=0.1, monthly_limit=0.015, request_limit=0.1, attempts=2, revision="QA")


def test_live_worker_still_blocks_unresolved_purge(app, monkeypatch):
    configure(app)
    rid = create(app)
    trash(app, rid)
    app.store.db.execute("UPDATE control_runs SET status='STOPPED',pid=12345 WHERE research_id=?", (rid,))
    monkeypatch.setattr("probe.research_lifecycle.process_alive", lambda pid: pid == 12345)
    response = app.request("POST", f"/api/control/research/{rid}/purge", {"confirm": True})
    assert response.body["error"] == "RESEARCH_PAUSE_BEFORE_DELETE"
    assert (app.workspace / rid).is_dir()


def test_unresolved_purge_rolls_back_billing_archive_and_file_move(app, monkeypatch):
    configure(app)
    rid = create(app)
    identity = app.store.reserve(rid=rid, connection="local", model="manual-id", role="manager", purpose="QA",
                                 bound=0.01, run_limit=0.1, monthly_limit=20, request_limit=0.1, attempts=2, revision="QA")
    app.store.transition(identity, "DISPATCHED")
    app.store.transition(identity, "UNRESOLVED")
    checkpoint = {"research_id": rid, "usage": {"input_tokens": 100}, "response_id": "qa-response"}
    app.store.put("provider_response_checkpoint", identity, checkpoint)
    trash(app, rid)
    before = app.store.ledger()
    original = app.store.audit
    def fail(rid, kind, payload):
        original(rid, kind, payload)
        if kind == "PURGED_BILLING_EVIDENCE":
            raise RuntimeError("QA 정산 근거 저장 실패")
    monkeypatch.setattr(app.store, "audit", fail)
    with pytest.raises(RuntimeError, match="QA 정산 근거 저장 실패"):
        purge(app, rid)
    assert app.store.ledger() == before
    assert app.store.config("provider_response_checkpoint", identity) == checkpoint
    assert app.store.config("research_lifecycle", rid)["status"] == "TRASH"
    assert (app.workspace / rid).is_dir()
    assert not (app.workspace / ".research-trash" / rid).exists()
    assert not app.store.db.execute("SELECT 1 FROM control_audit WHERE kind='PURGED_BILLING_EVIDENCE' AND research_id=?", (rid,)).fetchone()
    assert app.store.db.execute("PRAGMA foreign_key_check").fetchall() == []
