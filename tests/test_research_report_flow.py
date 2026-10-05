"""파일 없는 조사와 PDF의 비용·복구·출처 경계를 실제 실행기로 검사한다."""
import asyncio
import io
import json
from decimal import Decimal

import httpx
import pytest

from probe.control_plane import ControlError
from probe.control_runtime import RoutedGateway, execute
from probe.report_pdf import render_pdf
from probe.research_report import input_fingerprint, report_record, rewrite_report, validate_draft, ReportDraft, _save
from probe.release import validate_report_snapshot, export_release, ReleaseExportError
from probe.scholarly import CrossrefProvider, ScholarlyHTTPClient
from test_workbench import app, configure

QUESTION = "탄산음료의 온도에 따른 CO₂ 방출 속도"
ABSTRACT = "CO2 release increased with temperature in carbonated beverages. Container geometry limits comparisons."


def request(**updates):
    return {"title": QUESTION, "question": QUESTION, "settings_version": 2, "model_profile_id": "m",
            "run_limit_usd": ".10", "egress": "selected", "search_policy": "AUTO", "search_required": True,
            "public_search_query": QUESTION, "public_search_consent": True, "ai_report_enabled": True, **updates}


def rig(app, monkeypatch, *, search="ok", report="ok", source_count=1):
    import probe.search_policy as policy
    if not app.store.configs("model"):
        configure(app)
    search_calls, model_calls = [], []
    def scholarly(req):
        search_calls.append(str(req.url))
        if search == "rate":
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        items = [] if search == "empty" else [{"DOI": "10.5555/co2" + ("/" + str(i) if i else ""),
                "title": ["Temperature and CO2 release in carbonated beverages"],
                "URL": "https://doi.org/10.5555/co2" + ("/" + str(i) if i else ""), "abstract": ABSTRACT if search != "no_abstract" else None,
                "author": [{"given": "Test", "family": "Author"}], "published": {"date-parts": [[2024]]}} for i in range(source_count)]
        return httpx.Response(200, json={"message": {"items": items}})
    provider = CrossrefProvider(ScholarlyHTTPClient(retries=0, transport=httpx.MockTransport(scholarly)))
    real = policy.run_search
    async def offline(*args, **kwargs):
        return await real(*args, provider=provider, price=Decimal(0), price_source="offline-fixed", **kwargs)
    monkeypatch.setattr(policy, "run_search", offline)
    def reply(req, active_store=None, active_rid=None):
        payload = json.loads(req.content)
        writing = "ReportDraft" in json.dumps(payload)
        model_calls.append("report" if writing else "manager")
        if not writing:
            output = {"decision_type": "INITIAL_PLAN", "research_question": QUESTION, "rationale": "주제를 유지해 문헌과 측정 설계를 검토합니다.",
                      "coordinator_role": "experiment_coordinator", "objective": QUESTION,
                      "search_queries": ["temperature CO2 release carbonated beverage", "carbon dioxide degassing temperature"]}
        else:
            if report == "fail":
                return httpx.Response(403, json={"error": {"message": "mock refusal", "type": "permission_error"}})
            evidence = (active_store or app.store).db.execute("SELECT evidence_id,evidence_text FROM evidence WHERE research_id=? AND status='VERIFIED' LIMIT 1", (active_rid or rid_holder[0],)).fetchone()
            output = {"report_type": "design", "summary": "확보한 초록에서는 온도에 따라 기체 방출이 달라질 수 있다고 보고합니다. 직접 측정한 속도는 없습니다.",
                      "claims": [{"text": "온도가 올라갈 때 기체 방출이 증가했다는 초록 기록이 있습니다.", "evidence_id": evidence[0], "quote": evidence[1]}] if evidence else [],
                      "variables": [{"name": "음료 온도", "role": "독립변인"}, {"name": "시간별 질량 변화", "role": "종속변인"}, {"name": "음료·용기·개봉 방법", "role": "통제변인"}],
                      "materials": ["같은 음료와 용기", "온도계와 저울"], "procedure": ["온도 외 조건을 동일하게 유지합니다.", "개봉 후 시간별 질량을 반복 기록합니다."],
                      "measurement": "질량 변화에는 증발·유출도 포함될 수 있어 별도 대조 조건이 필요합니다.", "limitations": ["초록만 확인했습니다."], "figure_refs": []}
            if report == "inquiry":
                output = {"report_type": "literature" if evidence else "principle",
                    "summary": "온도와 기체 방출의 관계를 설명한다.",
                    "purpose": "온도 변화가 탄산 방출에 미치는 영향을 분석한다.",
                    "explanation": "용해 평형과 방출 속도는 서로 다른 개념이다.",
                    "method": "온도와 기체 방출의 원리를 비교하였다.",
                    "results": "직접 측정 자료는 없다. 온도 외에 용기와 흔들림도 방출에 영향을 줄 수 있다.",
                    "conclusion": "온도는 주요 조건이며 정확한 속도는 측정 자료가 필요하다.",
                    "claims": output["claims"]}
            if report == "number":
                output["summary"] = "방출 속도는 999 mL/s로 측정되었습니다."
        return httpx.Response(200, json={"id": "offline-" + str(len(model_calls)), "model": "manual-id",
            "choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "length" if writing and (report == "always_length" or report == "length_then_ok" and model_calls.count("report") == 1) else "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 100}})
    def gateway(store, rid, snapshot):
        rid_holder[0] = rid
        return RoutedGateway(store, app.credentials, rid, snapshot, client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(lambda req: reply(req, store, rid))))
    rid_holder = [None]
    return gateway, model_calls, search_calls


def run(app, gateway, **updates):
    created = app.create(request(**updates))
    rid = created["research_id"]
    app.command(rid, "start", {"expected_version": 0, "idempotency_key": "start-" + rid})
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=gateway))
    return rid


@pytest.mark.parametrize("changes,code,field", [
    ({"public_search_consent": False}, "SEARCH_EGRESS_DENIED", "public_search_consent"),
    ({"search_attempt_limit": 0}, "SEARCH_ATTEMPT_LIMIT", "search_attempt_limit"),
    ({"search_policy": "DISABLED"}, "SEARCH_REQUIRED_BUT_DISABLED", "search_policy")])
def test_search_preflight_blocks_before_any_charge(app, changes, code, field):
    configure(app)
    value = app.request("POST", "/api/control/research/preflight", request(**changes))
    assert value.status == 200 and not value.body["ready"]
    assert code in value.body["reasons"] and field in value.body["repair_fields"]
    created = app.create(request(**changes))
    response = app.request("POST", f"/api/control/research/{created['research_id']}/start",
                           {"expected_version": 0, "idempotency_key": "no-charge-start"})
    assert response.status == 409
    assert app.store.db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0
    assert app.store.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


def test_suggestion_is_read_only_and_never_includes_private_text(app):
    before = app.store.db.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0]
    response = app.request("POST", "/api/control/search-suggestion", {"title": QUESTION})
    assert response.body["query"] == QUESTION.replace("₂", "2") and response.body["paid_calls"] == 0
    assert app.store.db.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == before
    assert app.store.db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0
    assert app.request("POST", "/api/control/search-suggestion", {"title": "private secret@example.com"}).status == 409


def test_no_csv_bilingual_search_design_ai_pdf_and_export(app, monkeypatch, tmp_path):
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    assert app.store.run(rid)["status"] == "COMPLETED", app.store.run(rid)
    assert calls == ["manager", "report"] and len(searches) == 5
    from urllib.parse import unquote_plus
    assert QUESTION.replace("₂", "2") in unquote_plus(searches[0])
    assert "temperature" in searches[1] and "CO2" in searches[1]
    assert app.store.db.execute("SELECT COUNT(*) FROM experiments WHERE research_id=?", (rid,)).fetchone()[0] == 0
    saved = report_record(app.read._state, rid)
    assert saved["status"] == "READY" and saved["current"]
    assert saved["numeric_analysis"] is False
    view = app.request("GET", f"/api/control/research/{rid}/execution-summary")
    assert view.status == 200, view.body
    assert view.body["search"]["status"] == "COMPLETED"
    assert view.body["phases"][-1]["status"] == "READY"
    pdf = render_pdf(app.read._state, rid)
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf["data"]))
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "실험 설계안" in text and "직접 측정" in text and "참고문헌" in text
    assert reader.outline and all(entry.title not in {"검증", "재현 방법", "시각화"} for entry in reader.outline)
    links = [a.get_object().get("/A", {}).get("/URI") for p in reader.pages for a in p.get("/Annots", [])]
    assert "https://doi.org/10.5555/co2" in links
    assert render_pdf(app.read._state, rid)["sha256"] == pdf["sha256"]
    release = export_release(app.read._state, rid, tmp_path / "release")
    assert any(f["path"] == "research_output/ai_report.json" for f in release["files"])
    before = len(calls), len(searches)
    for suffix in ["report-view", "report.pdf", "execution-summary", "report-preview"]:
        assert app.request("GET", f"/api/control/research/{rid}/{suffix}").status == 200
    assert (len(calls), len(searches)) == before


@pytest.mark.parametrize("search,report", [("empty", "ok"), ("no_abstract", "ok"), ("rate", "ok"), ("ok", "fail"), ("ok", "number")])
def test_failure_keeps_partial_report_and_actions(app, monkeypatch, search, report):
    gateway, calls, searches = rig(app, monkeypatch, search=search, report=report)
    rid = run(app, gateway)
    assert app.store.run(rid)["status"] == "INSUFFICIENT_DATA", app.store.run(rid)
    assert len(calls) == 2 and len(searches) == (5 if search == "ok" else 4)
    assert validate_report_snapshot(app.read._state, rid)
    assert render_pdf(app.read._state, rid)["data"].startswith(b"%PDF-")
    saved = report_record(app.read._state, rid)
    if report != "ok":
        assert saved["status"] == "PARTIAL" and saved["draft"]["claims"]
        assert not saved["draft"]["procedure"] and not saved["draft"]["variables"]
        assert "확보한 자료의 범위" in saved["draft"]["summary"]
        assert saved["author"] == "LOCAL_FALLBACK"
        assert not app.store.db.execute("SELECT 1 FROM runtime_steps WHERE research_id=? AND step_key LIKE 'report-draft:%' AND status='RUNNING'", (rid,)).fetchone()
    if search != "ok":
        summary = app.request("GET", f"/api/control/research/{rid}/execution-summary").body
        assert summary["blocker"]["message"]


@pytest.mark.parametrize("search", ["empty", "no_abstract", "rate"])
def test_weak_evidence_continues_as_design_without_claiming_research_success(app, monkeypatch, search):
    gateway, calls, searches = rig(app, monkeypatch, search=search)
    rid = run(app, gateway, search_required=False)
    control = app.store.run(rid)
    assert control['status'] == 'COMPLETED' and control['error'] == 'LITERATURE_DESIGN_COMPLETED'
    assert calls == ['manager', 'report'] and len(searches) == 4
    saved = report_record(app.read._state, rid)
    assert saved['status'] == 'READY' and saved['draft']['claims'] == []
    assert len(saved['draft']['procedure']) >= 2 and '정량 결론' in saved['draft']['summary']
    assert app.store.db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone()[0] == 0
    view = app.request('GET', f'/api/control/research/{rid}/report-view').body
    assert view['complete'] is False and view['display_numbers'] == []
    summary = app.request('GET', f'/api/control/research/{rid}/execution-summary').body
    assert summary['completion_kind'] == 'DESIGN_ONLY'
    assert summary['blocker']['code'] == 'LITERATURE_DESIGN_COMPLETED' and '미확인' in summary['blocker']['message']
    assert render_pdf(app.read._state, rid)['data'].startswith(b'%PDF-')


def test_tolerant_evidence_still_requires_valid_ai_report(app, monkeypatch):
    gateway, calls, _ = rig(app, monkeypatch, search='empty', report='fail')
    rid = run(app, gateway, search_required=False)
    assert app.store.run(rid)['status'] == 'INSUFFICIENT_DATA'
    assert report_record(app.read._state, rid)['status'] == 'PARTIAL'
    assert calls == ['manager', 'report']


def test_completed_work_is_not_dispatched_on_reentry(app, monkeypatch):
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    app.store.db.execute("UPDATE control_runs SET status='RESUMING' WHERE research_id=?", (rid,))
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=gateway))
    assert calls == ["manager", "report"] and len(searches) == 5


@pytest.mark.parametrize("fault", ["tamper", "stale", "citation", "number", "secret"])
def test_report_security_and_currentness(app, monkeypatch, fault):
    gateway, _, _ = rig(app, monkeypatch)
    rid = run(app, gateway)
    state = app.read._state
    saved = report_record(state, rid)
    if fault == "tamper":
        (__import__("probe.report_publication", fromlist=["report_root"]).report_root(state, rid) / 'ai_report.json').write_bytes(b"{}")
        with pytest.raises(ReleaseExportError):
            render_pdf(state, rid)
    elif fault == "stale":
        state._db.execute("UPDATE sources SET abstract=abstract||' altered' WHERE research_id=?", (rid,))
        assert not report_record(state, rid, validate=False)["current"]
        with pytest.raises(ControlError, match="REPORT_STALE"):
            report_record(state, rid)
        with pytest.raises(ReleaseExportError):
            render_pdf(state, rid)
    else:
        draft = dict(saved["draft"])
        if fault == "citation":
            draft["claims"][0]["evidence_id"] = "missing"
        elif fault == "number":
            draft["summary"] = "실험 속도 999"
        else:
            monkeypatch.setenv("OPENAI_API_KEY", "offline-secret-canary-17396729")
            draft["summary"] = "offline-secret-canary-17396729"
        with pytest.raises(ControlError):
            validate_draft(state, rid, ReportDraft.model_validate(draft))


def test_rewrite_version_idempotency_and_no_search(app, monkeypatch):
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    body = {"expected_version": app.store.run(rid)["version"], "state_version": app.read._state.state_version(rid), "idempotency_key": "rewrite-once-01"}
    value = asyncio.run(rewrite_report(app, rid, body, provider_factory=gateway))
    assert value["status"] == "READY" and value["revision"] == 2
    assert len(calls) == 3 and len(searches) == 5
    assert asyncio.run(rewrite_report(app, rid, body, provider_factory=gateway)) == value
    assert len(calls) == 3
    with pytest.raises(ControlError, match="STATE_STALE"):
        asyncio.run(rewrite_report(app, rid, {**body, "idempotency_key": "rewrite-stale-02"}, provider_factory=gateway))

@pytest.mark.parametrize("empty", [False, True])
def test_fresh_process_crash_after_report_response_reuses_completed_requests(tmp_path, empty):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    folder = tmp_path / "fresh-report"
    extra = ["--empty"] if empty else []
    crashed = subprocess.run([sys.executable, "-B", "-X", "utf8", "qa/research_report_browser_fixture.py", str(folder), "--crash", *extra],
                             cwd=root, capture_output=True, timeout=60)
    assert crashed.returncode == 79, crashed.stderr.decode("utf-8", errors="replace")
    before = json.loads((folder / "observed.json").read_text(encoding="utf-8"))
    assert before["model_calls"] == ["manager", "report"] and before["searches"] == (4 if empty else 5)
    resumed = subprocess.run([sys.executable, "-B", "-X", "utf8", "qa/research_report_browser_fixture.py", str(folder), "--resume", *extra],
                             cwd=root, capture_output=True, timeout=60)
    assert resumed.returncode == 0, resumed.stderr.decode("utf-8", errors="replace")
    result = json.loads((folder / "resume.json").read_text(encoding="utf-8"))
    assert result["status"] == ("INSUFFICIENT_DATA" if empty else "COMPLETED")
    assert result["report"]["status"] == "READY"
    assert result["model_calls"] == [] and result["searches"] == 0


def test_crossref_jats_is_plain_text_and_korean_co2_relevance():
    from probe.scholarly import normalize_crossref_work
    from probe.literature import screen_source
    source = normalize_crossref_work({"DOI": "10.5555/test", "title": ["탄산음료 CO2 방출"], "abstract": "<jats:p>온도가 증가하면 CO<jats:sub>2</jats:sub> 방출이 증가할 수 있다.</jats:p>"})
    assert source.abstract == "온도가 증가하면 CO2 방출이 증가할 수 있다."
    assert screen_source("source", source, QUESTION).relevance == "DIRECT"


def test_report_budget_is_reserved_before_start(app):
    from probe.control_plane import ModelProfile, PriceRecord
    from probe.product_policy import completion_budget
    configure(app)
    profile = ModelProfile.model_validate(app.store.config("model", "m"))
    profile.local_api_unmetered = False
    profile.price = PriceRecord(input_per_million="1", output_per_million="1", source="offline-owner", revision="fixed-1", checked_at=__import__("probe.schemas", fromlist=["utc_now"]).utc_now().isoformat(), owner_verified=True)
    app.store.put("model", "m", profile, 1)
    snapshot = app.prepare(request(run_limit_usd=".000001"))
    view = completion_budget(app.store, "preflight", snapshot)
    assert view["mandatory_calls"] == ["manager", "manager"]
    assert not app.preflight(snapshot)["ready"]
    assert "COMPLETION_RESERVE_BLOCKED" in app.preflight(snapshot)["reasons"]


def test_supported_fixed_data_procedure_does_not_require_unused_literature_search(app):
    configure(app)
    value = app.request("POST", "/api/control/research/preflight", request(
        question="1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요.",
        research_profile_mode="AUTO", public_search_query="", public_search_consent=False))
    assert value.status == 200 and value.body["ready"], value.body
    assert value.body["search_status"] == "SEARCH_NOT_NEEDED"
    assert value.body["research_profile"]["status"] == "SUPPORTED"
    assert app.store.db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0


def test_report_budget_shortage_preserves_evidence_without_new_model_request(app, monkeypatch):
    from probe.control_plane import PriceRecord
    from probe.research_report import write_report
    from probe.autonomous_loop import AutonomousResearchLoop
    from probe.final_report import export_final_report
    from probe.schemas import utc_now
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    snapshot = app.store.run(rid)["snapshot"]
    snapshot["run_limit_usd"] = ".000001"
    for model in snapshot["models"].values():
        model["local_api_unmetered"] = False
        model["price"] = PriceRecord(input_per_million="1", output_per_million="1", source="offline-owner",
                                     revision="fixed-budget", checked_at=utc_now().isoformat(), owner_verified=True).model_dump(mode="json")
    runtime = AutonomousResearchLoop(app.read._state, gateway(app.store, rid, snapshot))
    result = asyncio.run(write_report(runtime, app.store, rid, snapshot, request_key="budget-short-report"))
    assert result["status"] == "PARTIAL" and result["error"] == "COMPLETION_RESERVE_BLOCKED"
    assert calls == ["manager", "report"] and len(searches) == 5
    export_final_report(app.read._state, rid)
    assert render_pdf(app.read._state, rid)["data"].startswith(b"%PDF-")
    assert app.store.db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone()[0] > 0


def test_local_pdfjs_bundle_is_pinned_and_integrity_checked():
    from pathlib import Path
    from hashlib import sha256
    vendor = Path(__file__).resolve().parents[1] / "src/probe/workbench_static/pdfjs"
    manifest = json.loads((vendor / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "6.4.299"
    assert {"build/pdf.mjs", "build/pdf.worker.mjs"} <= set(manifest["files"])
    for name, digest in manifest["files"].items():
        target = (vendor / name).resolve()
        assert target.is_relative_to(vendor.resolve())
        assert sha256(target.read_bytes()).hexdigest() == digest


def test_live_transport_path_preserves_dns_tls_and_model_endpoint_boundaries(monkeypatch):
    import socket
    from probe.control_plane import Connection, PinnedTransport
    from probe.scholarly import SearchRequest
    recorded = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    def reply(req):
        recorded.append(req)
        return httpx.Response(200, json={"message": {"items": [{"DOI": "10.5555/pinned",
            "title": ["CO2 temperature"], "abstract": ABSTRACT}]}})
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **k: httpx.MockTransport(reply))
    result = asyncio.run(CrossrefProvider(ScholarlyHTTPClient(retries=0)).search(
        SearchRequest(research_id="R-pinned", query="CO2 temperature", limit=1)))
    assert result.sources[0].abstract == ABSTRACT
    assert recorded[0].url.host == "93.184.216.34"
    assert recorded[0].headers["Host"] == "api.crossref.org"
    assert recorded[0].extensions["sni_hostname"] == "api.crossref.org"
    with pytest.raises(ControlError, match="ENDPOINT_INVALID"):
        Connection(connection_id="model", display_name="모델", adapter_id="openai_compatible", base_url="https://api.crossref.org")
    with pytest.raises(ControlError, match="SEARCH_DESTINATION_DENIED"):
        PinnedTransport.scholarly("unapproved.example")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ControlError, match="ENDPOINT_DENIED"):
        PinnedTransport.scholarly("api.crossref.org")


@pytest.mark.parametrize("kind", ["shared_limit", "price_missing"])
def test_search_limit_and_price_are_checked_before_the_first_model_call(app, monkeypatch, kind):
    from probe.control_plane import Defaults
    configure(app)
    if kind == "shared_limit":
        row = app.store.db.execute("SELECT revision FROM control_configs WHERE kind='defaults' AND id='global'").fetchone()
        app.store.put("defaults", "global", Defaults(search_attempt_limit=0), row[0] if row else 0)
        code = "SEARCH_ATTEMPT_LIMIT"
    else:
        import probe.search_policy as policy
        monkeypatch.setattr(policy, "AI_SEARCH_PRICE_CHECKED_AT", "2020-01-01T00:00:00+00:00")
        code = "PRICE_UNKNOWN"
    value = app.request("POST", "/api/control/research/preflight", request())
    assert not value.body["ready"] and code in value.body["reasons"]
    assert app.store.db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0


def test_distinct_sources_with_same_quote_are_not_a_research_loop(app, monkeypatch):
    gateway, calls, searches = rig(app, monkeypatch, source_count=5)
    rid = run(app, gateway)
    assert app.store.run(rid)["status"] == "COMPLETED", app.store.run(rid)
    assert report_record(app.read._state, rid)["status"] == "READY"
    assert app.store.db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone()[0] == 5
    assert not app.store.db.execute("SELECT 1 FROM runtime_events WHERE research_id=? AND event_type='LOOP_DETECTED'", (rid,)).fetchone()
    actions = app.store.db.execute("SELECT fingerprint,input_refs_json FROM research_actions WHERE research_id=? AND action_type='VERIFY_LITERATURE'", (rid,)).fetchall()
    assert len({r["fingerprint"] for r in actions}) == 5
    assert all(json.loads(r["input_refs_json"])[0]["type"] == "source" for r in actions)
    before = len(calls), len(searches)
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=gateway))
    assert (len(calls), len(searches)) == before


def test_report_writer_returns_inquiry_body_through_routed_gateway(app, monkeypatch):
    """실제 실행·요청 경로에서 새 형식의 본문 저장과 PDF를 확인한다."""
    gateway, calls, _ = rig(app, monkeypatch, report="inquiry")
    rid = run(app, gateway)
    assert calls == ["manager", "report"]
    record = report_record(app.read._state, rid)
    assert record["status"] == "READY"
    draft = record["draft"]
    assert all(draft[key] for key in ("purpose", "explanation", "method", "results", "conclusion"))
    assert not draft["procedure"] and not draft["variables"] and not draft["materials"]
    reader = __import__("pypdf").PdfReader(io.BytesIO(render_pdf(app.read._state, rid)["data"]))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "실험 설계안" not in text
    assert "결과 및 해석" in text and "참고문헌" in text
