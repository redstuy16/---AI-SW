"""현재 과학 실행에 이전 전용 절차·역할·예산 제한이 섞이지 않는지 검사한다."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from probe.control_plane import ControlError, ModelProfile, ROLES, admitted_cost
from probe.control_runtime import RoutedGateway, execute
from probe.providers.fake import FakeProvider
from probe.providers.normalized import GenerationRequest
from probe.research_design import current_design, amend_design
from probe.science_policy import context_budget
from test_workbench import app, configure
from test_product_ux import paid

COMPLEX = ("1990년 이후 대기 중 CO₂ 증가와 지구 평균기온 상승 사이의 관계를 정량적으로 분석하고, "
           "관측된 온난화가 단순한 두 변수의 상관관계만으로 설명될 수 있는지 평가하라. "
           "전 지구 기온 자료를 사용하고 상관관계를 인과관계로 해석해서는 안 된다. "
           "교란요인을 고려하고 예상과 다른 결과의 원인을 조사하라.")
FIXED = "1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요."


def body(**changes):
    return {"beginner_mode": True, "execution_mode": "SCIENCE_AUTO", "research_profile_mode": "AUTO",
            "model_profile_id": "m", "question": COMPLEX, "egress": "selected", "ai_report_enabled": True,
            "search_policy": "DISABLED", "run_limit_usd": "0.1", **changes}


@pytest.mark.parametrize("question", [COMPLEX, "전 지구 기온 편차를 비교해 주세요.", FIXED])
@pytest.mark.parametrize("profile_mode", ["AUTO", "DISABLED"])
def test_science_preflight_never_uses_legacy_topic_scope(app, question, profile_mode):
    configure(app)
    result = app.request("POST", "/api/control/research/preflight", body(question=question, research_profile_mode=profile_mode))
    assert result.status == 200 and result.body["ready"], result.body
    assert result.body["research_profile"]["status"] == "GENERAL"
    assert result.body["research_design"]["profile_enabled"] is False
    assert result.body["paid_calls"] == 0 and not app.store.ledger()["requests"]


@pytest.mark.parametrize("question,code", [(COMPLEX, "PROFILE_UNSUPPORTED"),
                                           ("전 지구 기온 편차를 비교해 주세요.", "PROFILE_CLARIFICATION_REQUIRED")])
def test_legacy_qualified_procedure_retains_its_scope(app, question, code):
    configure(app)
    result = app.request("POST", "/api/control/research/preflight", body(question=question, execution_mode="LEGACY"))
    assert result.status == 200 and not result.body["ready"]
    assert code in result.body["reasons"]


def test_new_science_defaults_disable_unused_profile_but_legacy_defaults_stay_auto(app):
    configure(app)
    request = body()
    request.pop("research_profile_mode")
    assert app.prepare(request)["research_profile_mode"] == "DISABLED"
    assert app.prepare(request | {"execution_mode": "LEGACY"})["research_profile_mode"] == "AUTO"


@pytest.mark.parametrize("updates,code", [({"public_search_consent": False}, "SEARCH_EGRESS_DENIED"),
    ({"search_policy": "DISABLED"}, "SEARCH_REQUIRED_BUT_DISABLED"),
    ({"search_attempt_limit": 0}, "SEARCH_ATTEMPT_LIMIT")])
def test_science_climate_question_cannot_skip_required_search_guards(app, updates, code):
    configure(app)
    request = body(question=FIXED, search_policy="AUTO", search_required=True,
                   public_search_consent=True, public_search_query="global temperature data") | updates
    result = app.request("POST", "/api/control/research/preflight", request)
    assert result.status == 200 and not result.body["ready"]
    assert code in result.body["reasons"] and result.body["search_status"] != "SEARCH_NOT_NEEDED"
    assert not app.store.ledger()["requests"]


def test_science_csv_is_not_rejected_as_legacy_fixed_climate_input(app):
    configure(app)
    result = app.request("POST", "/api/control/research/preflight", body(question=FIXED, source_relative="data.csv"))
    assert result.status == 200 and result.body["ready"], result.body
    assert "PROFILE_CLARIFICATION_REQUIRED" not in result.body["reasons"]


def test_unused_unsupported_role_does_not_block_manager_but_is_checked_on_dispatch(app):
    configure(app)
    worker = deepcopy(app.store.config("model", "m"))
    worker.update(profile_id="unused-worker", model_id="unused-model", capability_status="unsupported")
    app.store.put("model", "unused-worker", ModelProfile.model_validate(worker))
    request = body(manual_role_override=True, routing={"verification_coordinator": "unused-worker"})
    modern = app.request("POST", "/api/control/research/preflight", request)
    assert modern.status == 200 and modern.body["ready"], modern.body
    legacy = app.request("POST", "/api/control/research/preflight", request | {"execution_mode": "LEGACY", "question": "자료 검토"})
    assert not legacy.body["ready"] and "CAPABILITY_NOT_VALIDATED" in legacy.body["reasons"]
    snapshot = app.prepare(request)
    gateway = RoutedGateway(app.store, app.credentials, "role-check", snapshot)
    invocation = GenerationRequest(request_id="optional-role", research_id="role-check", role="verification_coordinator",
                                   model_profile_id="unused-worker", max_output_tokens=32, input_text="독립 검토")
    with pytest.raises(ControlError, match="CAPABILITY_NOT_VALIDATED"):
        asyncio.run(gateway.generate(invocation))
    assert gateway.dispatch_count == 0 and not app.store.ledger()["requests"]


def test_science_requires_a_valid_manager(app):
    configure(app)
    snapshot = app.prepare(body())
    snapshot["models"].pop("manager")
    result = app.preflight(snapshot)
    assert not result["ready"] and "ROLE_MODELS_REQUIRED" in result["reasons"]
    snapshot = app.prepare(body())
    snapshot["models"]["manager"]["capability_status"] = "unsupported"
    result = app.preflight(snapshot)
    assert not result["ready"] and "CAPABILITY_NOT_VALIDATED" in result["reasons"]


def test_science_startup_cost_uses_one_actual_role_and_preserves_legacy_budget(app):
    paid(app)
    raw = app.store.config("model", "m")
    raw["price"]["output_per_million"] = "30"
    app.store.put("model", "m", ModelProfile.model_validate(raw), 2)
    request = body(question="공개 자료에 근거한 원리 검토", run_limit_usd="0.09", research_profile_mode="DISABLED")
    snapshot = app.prepare(request)
    expected = admitted_cost(ModelProfile.model_validate(snapshot["models"]["manager"]), 4096)
    modern = app.request("POST", "/api/control/research/preflight", request)
    assert modern.status == 200 and modern.body["ready"], modern.body
    assert Decimal(modern.body["minimum_role_call_bound_usd"]) == expected
    legacy = app.request("POST", "/api/control/research/preflight", request | {"execution_mode": "LEGACY"})
    assert not legacy.body["ready"] and "COMPLETION_RESERVE_BLOCKED" in legacy.body["reasons"]
    assert Decimal(legacy.body["minimum_role_call_bound_usd"]) == len(ROLES) * expected
    low = app.request("POST", "/api/control/research/preflight", request | {"run_limit_usd": "0.000001"})
    assert not low.body["ready"] and "COMPLETION_RESERVE_BLOCKED" in low.body["reasons"]
    assert not app.store.ledger()["requests"]


def test_science_context_is_selected_per_role_and_legacy_minimum_is_preserved(app):
    configure(app)
    snapshot = app.prepare(body())
    snapshot["models"]["manager"].update(input_byte_limit=64000, context_limit=32768, output_limit=1024)
    snapshot["models"]["verification_coordinator"].update(input_byte_limit=2048, context_limit=4096, output_limit=1024)
    assert context_budget(snapshot) == context_budget(snapshot, role="manager") == 16000
    assert context_budget(snapshot, role="verification_coordinator") == 2048
    assert context_budget(snapshot | {"execution_mode": "LEGACY"}) == 512


def test_science_design_review_creation_and_amendment_do_not_inherit_fixed_method(app):
    configure(app)
    design = {"fields": {"method": {"value": "pearson_correlation"}}}
    reviewed = app.request("POST", "/api/control/research/design-review",
                           {"question": FIXED, "detailed_design": design, "execution_mode": "SCIENCE_AUTO"})
    assert reviewed.status == 200 and reviewed.body["profile"]["status"] == "GENERAL"
    assert reviewed.body["issues"] == []
    legacy = app.request("POST", "/api/control/research/design-review", {"question": FIXED, "detailed_design": design})
    assert any(issue["status"] == "METHOD_UNSUPPORTED" for issue in legacy.body["issues"])
    created = app.create(body(question=FIXED, detailed_design=design))
    rid = created["research_id"]
    assert current_design(app.read._state, rid)["issues"] == []
    amended = amend_design(app.read._state, rid, {"fields": {"method": {"value": "spearman_correlation"}}},
                           expected_version=app.read._state.state_version(rid))
    assert amended["changed"] and current_design(app.read._state, rid)["issues"] == []


def test_science_execution_reaches_search_and_decision_without_qualified_procedure(app, monkeypatch):
    configure(app)
    searches = []
    async def search(state, store, credentials, rid, snapshot, **kwargs):
        searches.append(snapshot["question"])
        return False
    async def forbidden(*args, **kwargs):
        raise AssertionError("현재 과학 모드에서 이전 전용 절차 실행")
    monkeypatch.setattr("probe.search_policy.run_search", search)
    monkeypatch.setattr("probe.qualified_workflow.execute_profile", forbidden)
    created = app.create(body(search_policy="AUTO", public_search_consent=True))
    rid = created["research_id"]
    app.command(rid, "start", {"expected_version": 0, "idempotency_key": "science-policy-start"})
    provider = FakeProvider([{"action": "NEED_INPUT", "rationale": "정량 분석에 필요한 CO₂와 기온 원자료가 없습니다."}])
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=lambda *args: provider))
    result = app.store.run(rid)
    assert result["status"] == "INSUFFICIENT_DATA" and result["error"] == "INSUFFICIENT_DATA"
    assert searches == [COMPLEX] and [call["role"] for call in provider.calls] == ["manager"]
    assert app.read._state.runtime_step(rid, "science:initial_search")["status"] == "COMPLETED"
    assert not app.store.ledger()["requests"]


def test_science_csv_uses_current_settings_without_changing_legacy_or_explicit_versions(app):
    configure(app)
    request = body(source_relative="data.csv")
    assert app.prepare(request)["settings_version"] == 2
    assert app.prepare(request | {"execution_mode": "LEGACY"})["settings_version"] == 1
    assert app.prepare(request | {"settings_version": 1})["settings_version"] == 1
