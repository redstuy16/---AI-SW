"""실제 API 없이 과학 행동 선택·검증·지속 복구를 확인한다."""
from __future__ import annotations

import asyncio
import json

import pytest

from probe.autonomous_loop import AutonomousResearchLoop
from probe.database import initialize
from probe.providers.fake import FakeProvider
from probe.recovery import FaultInjector, InjectedCrash
from probe.service import ContractViolationError, StateService
from probe.storage import Workspace

from test_autonomous_loop import CSV, MODELS


def complete(**extra):
    return {"action": "COMPLETE", "rationale": "안정적인 원리와 직접 수행할 설계를 충분히 설명했습니다.",
            "report_draft": {"report_type": "principle", "summary": "빛의 세기가 광합성에 영향을 주는 원리를 설명합니다.",
                             "explanation": "빛에너지는 광합성 과정에 쓰입니다. 다른 조건은 일정하게 유지하고 직접 관찰합니다.",
                             "variables": [{"name": "빛의 세기", "role": "독립변인"}, {"name": "기포 변화", "role": "종속변인"}],
                             "procedure": ["빛의 조건을 달리합니다.", "기포 변화를 관찰합니다."], "measurement": "같은 시간 동안 기포 변화를 기록합니다."}, **extra}


def runtime(tmp_path, replies, fault=None):
    db = initialize(tmp_path / "science.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    provider = FakeProvider(replies)
    faults = FaultInjector(lambda point: (_ for _ in ()).throw(InjectedCrash(point)) if point == fault else None)
    loop = AutonomousResearchLoop(state, provider, MODELS, faults=faults)
    loop.science_settings = {"enabled": True}
    return db, state, provider, loop


def analyze(method):
    def reply(call):
        bundle = json.loads(call["input_text"])
        objective = bundle["active_state"]["contract"]["objective"]
        context = json.loads(objective.split("\n", 1)[1])
        return {"action": "ANALYZE", "rationale": "실제 두 열의 연관성을 검증합니다.",
                "analysis_plan": {"dataset_id": context["dataset"]["dataset_id"],
                    "selected_variables": ["temperature", "growth"], "method": method,
                    "justification": "실제 자료의 수치 열을 사용합니다.",
                    "requested_tools": ["stats.run", "visualization.render", "evidence.record"]}}
    return reply


def test_known_principle_searches_before_one_model_call(tmp_path):
    db, state, provider, loop = runtime(tmp_path, [complete()])
    searches, saved = [], []
    async def search(queries=None):
        assert provider.calls == []
        searches.append(queries)
        return True
    async def writer(draft, decision, *, request_key):
        saved.append((draft, decision, request_key))
        return {"status": "READY", "draft": draft.model_dump(mode="json")}
    loop.evidence_acquisition, loop.science_report_writer = search, writer
    result = asyncio.run(loop.run("빛의 세기와 광합성 탐구를 설계해 주세요.", None))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert len(provider.calls) == 1 and searches == [None] and len(saved) == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    assert saved[0][0].report_type == "principle"
    assert state.load_runtime_cursor(result["research_id"]).stage == "STOPPED"
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE status='RUNNING'").fetchone()[0] == 0
    db.close()


def test_stop_false_continues_to_the_next_decision(tmp_path):
    db, _, provider, loop = runtime(tmp_path, [complete(stop=False), complete()])
    result = asyncio.run(loop.run("광합성 탐구", None))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert len(provider.calls) == 2 and result["observations"][0]["result"]["status"] == "CONTINUE"
    assert "CONTINUE" in provider.calls[1]["input_text"]
    db.close()


def test_selected_audience_and_science_field_reach_the_decision_context(tmp_path):
    db, _, provider, loop = runtime(tmp_path, [complete()])
    loop.science_settings.update(audience="teacher", science_field="biology")
    asyncio.run(loop.run("광합성 탐구", None))
    bundle = json.loads(provider.calls[0]["input_text"])
    context = json.loads(bundle["active_state"]["contract"]["objective"].split("\n", 1)[1])
    assert context["audience"] == "teacher" and context["science_field"] == "biology"
    db.close()


def test_empty_search_twice_stops_with_a_preserved_limitation(tmp_path):
    search = {"action": "SEARCH", "rationale": "직접 근거를 확인합니다.", "search_queries": ["photosynthesis light evidence"]}
    db, _, provider, loop = runtime(tmp_path, [search, search, complete()])
    calls = []
    async def acquire(queries=None):
        calls.append(queries)
        return False
    loop.evidence_acquisition = acquire
    result = asyncio.run(loop.run("새 연구 근거 확인", None))
    assert result["stop_reason"] == "UNRESOLVED_VERIFICATION"
    assert len(calls) == 3 and len(provider.calls) == 2
    assert result["observations"][-1]["result"]["status"] == "NO_EVIDENCE"
    db.close()


def test_required_literature_cannot_complete_with_an_uncited_design(tmp_path):
    db, _, provider, loop = runtime(tmp_path, [complete(), complete()])
    saved = []
    async def writer(draft, decision, *, request_key):
        saved.append(draft)
        return {"status": "READY"}
    loop.science_report_writer = writer
    loop.science_settings["search_required"] = True
    result = asyncio.run(loop.run("정확한 문헌 인용을 포함한 광합성 탐구", None))
    assert result["stop_reason"] == "UNRESOLVED_VERIFICATION"
    assert len(provider.calls) == 2
    assert result["observations"][-1]["result"]["reason"] == "REQUIRED_LITERATURE_CITATION_MISSING"
    assert result["observations"][-1]["result"]["draft"]["summary"]
    assert saved == []
    db.close()


def test_csv_analysis_uses_verified_tools_without_a_worker_model_call(tmp_path):
    db, _, provider, loop = runtime(tmp_path, [analyze("pearson_correlation"), complete()])
    result = asyncio.run(loop.run("온도와 생장 간 관계를 확인합니다.", CSV))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert [call["role"] for call in provider.calls] == ["manager", "manager"]
    assert db.execute("SELECT status FROM experiments").fetchone()[0] == "VERIFIED"
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='stats.run'").fetchone()[0] == 1
    db.close()


@pytest.mark.parametrize("fault", ["AFTER_SCIENCE_OBSERVATION", "AFTER_COMMIT", "AFTER_INTAKE_PROFILE"])
def test_science_restart_reuses_observations_models_and_tools(tmp_path, fault):
    replies = [analyze("pearson_correlation"), complete()]
    db, _, provider, loop = runtime(tmp_path, replies, fault)
    with pytest.raises(InjectedCrash):
        asyncio.run(loop.run("온도와 생장의 관계", CSV))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    consumed = len(provider.calls)
    db.close()
    db, _, resumed_provider, resumed = runtime(tmp_path, replies[consumed:])
    result = asyncio.run(resumed.resume(rid))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert consumed + len(resumed_provider.calls) == 2
    assert db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('data.import','data.profile','stats.run')").fetchone()[0] == 3
    db.close()


def test_method_request_is_present_in_the_legacy_followup_contract(tmp_path):
    from test_autonomous_loop import fake_replies
    def inconclusive_status(call):
        active = json.loads(call["input_text"])["active_state"]
        evidence = active["verified_evidence"]
        assert len(evidence) == 2
        assert {item["polarity"] for item in evidence} == {"neutral", "support"}
        return {"hypothesis_id": active["active_hypotheses"][0]["hypothesis_id"],
                "new_status": "INCONCLUSIVE", "rationale": "Pearson은 유의수준을 충족하지 않고 Spearman만 지지하므로 결론을 유보합니다.",
                "evidence_refs": [{"type": "evidence", "id": item["evidence_id"]} for item in evidence]}
    replies = fake_replies()
    # 후속 문맥 검증을 유지하며 중립 결과를 지지 근거로 승격하지 않는다.
    replies[-2] = inconclusive_status
    replies[-1] = {"stop": True, "reason": "INSUFFICIENT_DATA", "rationale": "지지와 중립 근거가 혼재해 결론을 유보합니다."}
    db, _, provider, loop = runtime(tmp_path, replies)
    loop.science_settings = {"enabled": False}
    source = tmp_path / "extreme-endpoint.csv"
    source.write_bytes(("temperature,growth\n" + "".join(f"{value},{value if value < 12 else 1000}\n" for value in range(1, 13))).encode("utf-8", errors="strict"))
    result = asyncio.run(loop.run("온도와 생장 관계", source))
    assert result["stop_reason"] == "INSUFFICIENT_DATA" and result["conclusion"] is None
    followup = provider.calls[5]
    worker = provider.calls[6]
    assert "spearman_correlation" in followup["input_text"]
    assert "NONLINEARITY" in followup["input_text"]
    assert "spearman_correlation" in worker["input_text"]
    db.close()


def test_two_independent_design_reviews_are_saved_before_completion(tmp_path):
    improve = {"action": "REFINE_DESIGN", "rationale": "통제 조건을 명시합니다.", "design_updates": {"control": "같은 수온을 유지합니다."}}
    delegate = {"action": "DELEGATE", "rationale": "실행 가능성과 안전을 각각 확인합니다.",
                "delegate_role": "verification_coordinator", "delegate_objectives": ["변인과 측정 절차를 검토합니다.", "안전과 실행 가능성을 검토합니다."]}
    review = {"verdict": "ACCEPT", "rationale": "직접 수행할 수 있고 위험한 조작이 없습니다.", "issues": []}
    db, _, provider, loop = runtime(tmp_path, [improve, delegate, review, review, complete()])
    result = asyncio.run(loop.run("광합성 실험 설계", None))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert len(result["observations"][1]["result"]["independent_reviews"]) == 2
    assert [call["role"] for call in provider.calls].count("verification_coordinator") == 2
    assert db.execute("SELECT COUNT(*) FROM runtime_steps WHERE step_key LIKE 'science:review_output:%' AND status='COMPLETED'").fetchone()[0] == 2
    db.close()


def test_invalid_draft_is_observed_and_can_be_corrected(tmp_path):
    invalid = complete()
    invalid["report_draft"]["summary"] = "관찰에서 결과값 999를 얻었습니다."
    db, _, provider, loop = runtime(tmp_path, [invalid, complete()])
    result = asyncio.run(loop.run("광합성 원리", None))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert result["observations"][0]["result"]["status"] == "PARTIAL"
    assert "REPORT_UNPROVEN_NUMBER" in provider.calls[1]["input_text"]
    db.close()


def test_question_only_restart_preserves_none_source_and_cached_decision(tmp_path):
    replies = [complete(stop=False), complete()]
    db, _, provider, loop = runtime(tmp_path, replies, "AFTER_SCIENCE_OBSERVATION")
    with pytest.raises(InjectedCrash):
        asyncio.run(loop.run("광합성 원리", None))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    assert len(provider.calls) == 1
    db.close()
    db, _, provider, loop = runtime(tmp_path, replies[1:])
    result = asyncio.run(loop.resume(rid))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert len(provider.calls) == 1 and db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
    db.close()


def test_cached_ready_resume_rechecks_new_required_literature(tmp_path):
    db, _, provider, loop = runtime(tmp_path, [complete()], "AFTER_SCIENCE_OBSERVATION")
    with pytest.raises(InjectedCrash):
        asyncio.run(loop.run("광합성 원리", None))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    assert len(provider.calls) == 1
    db.close()
    reply = {"action": "NEED_INPUT", "rationale": "새롭게 요구한 원문 근거가 필요합니다."}
    db, state, provider, loop = runtime(tmp_path, [reply])
    loop.science_settings["search_required"] = True
    result = asyncio.run(loop.resume(rid))
    assert result["stop_reason"] == "INSUFFICIENT_DATA" and len(provider.calls) == 1
    assert result["observations"][0]["result"]["status"] == "NEEDS_REVIEW"
    assert result["observations"][0]["result"]["reason"] == "REQUIRED_LITERATURE_CITATION_MISSING"
    assert state.runtime_step(rid, "science:observation:0")["output"]["result"]["status"] == "READY"
    assert db.execute("SELECT COUNT(*) FROM runtime_steps WHERE step_key LIKE 'science:revalidation:%'").fetchone()[0] == 1
    db.close()


def test_cached_ready_resume_rechecks_report_after_question_change(tmp_path):
    from probe.control_plane import ControlStore
    from probe.research_report import persist_report_draft
    db, state, _, loop = runtime(tmp_path, [complete()], "AFTER_SCIENCE_OBSERVATION")
    store = ControlStore(db)
    loop.science_report_writer = lambda draft, decision, request_key: persist_report_draft(
        loop, store, state._db.execute("SELECT research_id FROM research_runs").fetchone()[0], {}, draft, request_key=request_key)
    with pytest.raises(InjectedCrash):
        asyncio.run(loop.run("광합성 원리", None))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    changed_question = "광합성 탐구의 통제 조건과 안전 조건을 설명합니다."
    state.set_research_question(rid, changed_question)
    db.close()
    reply = {"action": "NEED_INPUT", "rationale": "현재 바뀐 질문의 안전 조건을 확인합니다."}
    db, state, provider, loop = runtime(tmp_path, [reply])
    result = asyncio.run(loop.resume(rid))
    assert result["stop_reason"] == "INSUFFICIENT_DATA" and len(provider.calls) == 1
    assert result["observations"][0]["result"]["reason"] == "REPORT_STALE"
    context = json.loads(json.loads(provider.calls[0]["input_text"])["active_state"]["contract"]["objective"].split("\n", 1)[1])
    assert context["current_question"] == changed_question
    assert state.runtime_step(rid, "science:observation:0")["output"]["result"]["status"] == "READY"
    db.close()


def test_science_critic_followup_replans_the_exact_requested_method(tmp_path):
    from test_autonomous_loop import first_critic, second_critic
    delegate = {"action": "DELEGATE", "rationale": "분석의 과학적 한계를 검토합니다.", "delegate_role": "verification_coordinator"}
    replies = [analyze("pearson_correlation"), delegate, first_critic,
               analyze("spearman_correlation"), delegate, second_critic, complete()]
    db, _, provider, loop = runtime(tmp_path, replies)
    result = asyncio.run(loop.run("온도와 생장 간 관계", CSV))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert [row[0] for row in db.execute("SELECT method FROM experiments ORDER BY rowid")] == ["pearson_correlation", "spearman_correlation"]
    assert "FOLLOW_UP_REQUIRED" in provider.calls[3]["input_text"]
    assert "spearman_correlation" in provider.calls[3]["input_text"]
    assert db.execute("SELECT COUNT(*) FROM critic_reviews").fetchone()[0] == 2
    db.close()


@pytest.mark.parametrize("requested,expected", [(None, 6), (8, 8), (20, 20), (30, 20)])
def test_decision_limit_preserves_default_and_honors_schema_bound(tmp_path, requested, expected):
    replies = [{"action": "REFINE_DESIGN", "rationale": f"서로 다른 통제 항목 {index}를 개선합니다.",
                "design_updates": {"control": f"항목 {index}를 일정하게 유지합니다."}} for index in range(expected+1)]
    db, _, provider, loop = runtime(tmp_path, replies)
    provider.name = "control_broker"
    if requested is not None:
        loop.science_settings.update(max_decisions=requested)
    result = asyncio.run(loop.run("실험 조건 개선", None))
    assert result["stop_reason"] == "ACTION_LIMIT_REACHED"
    assert result["science_decisions"] == len(provider.calls) == expected
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE status='RUNNING'").fetchone()[0] == 0
    db.close()


def test_explicit_runtime_limit_can_exceed_default_without_waiting(tmp_path, monkeypatch):
    db, state, provider, loop = runtime(tmp_path, [complete()])
    loop.science_settings.update(max_runtime_sec=3600)
    rid = state.create_research("광합성 원리")
    state.configure_budget(rid, 1, 2, 3)
    state.finish_runtime_step(rid, "science:started", {"started_at": 1000})
    monkeypatch.setattr("probe.science_loop.time.time", lambda: 1301)
    from probe.science_loop import run_science_loop
    result = asyncio.run(run_science_loop(loop, rid, None, "광합성 원리"))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED" and len(provider.calls) == 1
    db.close()


def test_explicit_no_progress_limit_can_reach_five(tmp_path):
    search = {"action": "SEARCH", "rationale": "필요한 근거를 확인합니다.", "search_queries": ["photosynthesis evidence"]}
    db, _, provider, loop = runtime(tmp_path, [search]*5)
    provider.name = "control_broker"
    loop.science_settings.update(no_progress_limit=5)
    async def acquire(queries=None):
        return False
    loop.evidence_acquisition = acquire
    result = asyncio.run(loop.run("과학 문헌 확인", None))
    assert result["stop_reason"] == "UNRESOLVED_VERIFICATION" and len(provider.calls) == 5
    db.close()


def test_pending_decision_limit_is_reloaded_at_the_next_boundary(tmp_path):
    first = {"action": "REFINE_DESIGN", "rationale": "통제 조건을 개선합니다.", "design_updates": {"control": "같은 조건을 유지합니다."}}
    db, _, provider, loop = runtime(tmp_path, [first, complete()])
    loop.science_settings.update(max_decisions=1)
    def boundary():
        if len(provider.calls) == 1:
            loop.science_settings = {"enabled": True, "max_decisions": 2}
    loop.control_boundary = boundary
    result = asyncio.run(loop.run("광합성 원리", None))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED" and len(provider.calls) == 2
    db.close()


def test_elapsed_limit_cancels_the_model_and_closes_running_tasks(tmp_path):
    db, _, provider, loop = runtime(tmp_path, [])
    original = provider.run_structured
    async def slow(**kwargs):
        await asyncio.sleep(5)
        return await original(**kwargs)
    provider.run_structured = slow
    loop.science_settings.update(max_runtime_sec=1)
    result = asyncio.run(loop.run("광합성 원리", None))
    assert result["stop_reason"] == "ACTION_LIMIT_REACHED"
    assert db.execute("SELECT error_code FROM agent_runs WHERE provider IS NOT NULL").fetchone()[0] == "PROCESS_INTERRUPTED"
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE status='RUNNING'").fetchone()[0] == 0
    db.close()


def test_invalid_analysis_plan_closes_internal_tasks(tmp_path):
    def invalid(call):
        value = analyze("pearson_correlation")(call)
        value["analysis_plan"]["dataset_id"] = "foreign-dataset"
        return value
    db, _, _, loop = runtime(tmp_path, [invalid])
    with pytest.raises(ContractViolationError):
        asyncio.run(loop.run("자료 분석", CSV))
    assert db.execute("SELECT run_status FROM research_runs").fetchone()[0] != "ACTIVE"
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE status='RUNNING'").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("outcome", ["empty", "error", "WEB_SEARCH_UNSUPPORTED", "CONTEXT_LIMIT_BLOCKED"])
def test_initial_search_failure_still_allows_report_with_recorded_scope(tmp_path, outcome):
    from probe.control_plane import ControlError
    db, state, provider, loop = runtime(tmp_path, [complete()])
    calls = []
    async def acquire(queries=None):
        calls.append(queries)
        if outcome == "error":
            raise ControlError("SEARCH_PROVIDER_UNAVAILABLE")
        if outcome != "empty":
            raise ControlError(outcome)
        return False
    loop.evidence_acquisition = acquire
    result = asyncio.run(loop.run("광합성 원리", None))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert calls == [None] and len(provider.calls) == 1
    context = json.loads(json.loads(provider.calls[0]["input_text"])["active_state"]["contract"]["objective"].split("\n", 1)[1])
    assert context["initial_search"]["status"] == "NO_EVIDENCE"
    assert state.runtime_step(result["research_id"], "science:initial_search")["output"] == context["initial_search"]
    db.close()


def test_explicit_disabled_search_does_not_dispatch(tmp_path):
    db, state, provider, loop = runtime(tmp_path, [complete()])
    loop.science_settings["search_policy"] = "DISABLED"
    async def acquire(queries=None):
        raise AssertionError("검색을 끈 경우 외부 요청을 보내면 안 됩니다.")
    loop.evidence_acquisition = acquire
    result = asyncio.run(loop.run("광합성 원리", None))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert state.runtime_step(result["research_id"], "science:initial_search")["output"]["reason"] == "SEARCH_DISABLED"
    assert len(provider.calls) == 1
    db.close()


def test_initial_search_is_not_repeated_after_saved_observation(tmp_path):
    replies = [complete(stop=False), complete()]
    db, _, provider, loop = runtime(tmp_path, replies, "AFTER_SCIENCE_OBSERVATION")
    calls = []
    async def acquire(queries=None):
        calls.append(queries)
        return False
    loop.evidence_acquisition = acquire
    with pytest.raises(InjectedCrash):
        asyncio.run(loop.run("광합성 원리", None))
    rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
    assert calls == [None]
    db.close()
    db, _, resumed_provider, resumed = runtime(tmp_path, [complete()])
    resumed.evidence_acquisition = acquire
    result = asyncio.run(resumed.resume(rid))
    assert result["stop_reason"] == "SCIENCE_INQUIRY_COMPLETED"
    assert calls == [None] and len(resumed_provider.calls) == 1
    db.close()


@pytest.mark.parametrize('code', ['PRICE_CATEGORY_UNKNOWN', 'NEEDS_RECONCILIATION', 'USAGE_MISSING', 'AUTHENTICATION_ERROR'])
def test_initial_search_accounting_failure_is_not_empty_evidence_or_retried(tmp_path, code):
    from probe.control_plane import ControlError
    db, state, provider, loop = runtime(tmp_path, [complete()])
    async def acquire(queries=None): raise ControlError(code)
    loop.evidence_acquisition = acquire
    with pytest.raises(ControlError, match=code):
        asyncio.run(loop.run('고무줄 온도 탐구', None))
    assert not provider.calls
    row = db.execute("SELECT details_json FROM runtime_events WHERE event_type='SCIENCE_INITIAL_SEARCH_FAILED'").fetchone()
    assert json.loads(row[0]) == {'status': 'ERROR', 'reason': code}
    db.close()
