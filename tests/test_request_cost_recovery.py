"""과거 미정산 노출과 새 요청·자동 재전송을 구분하는 실제 저장소 검사."""
import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from probe.control_plane import ControlError
from probe.control_runtime import RoutedGateway, execute
from probe.schemas import utc_now
from probe.science_policy import sync_ledger_budget
from test_workbench import app, configure, create, gateway, invoke, SmokeOutput


def good(req):
    return httpx.Response(200, json={"id": "new-response", "usage": {"prompt_tokens": 30, "completion_tokens": 10},
        "choices": [{"message": {"content": json.dumps({"status": "ok"})}}]})


def test_new_request_in_same_research_is_allowed_but_unknown_exact_replay_is_blocked(app):
    broker, calls = gateway(app, mode="timeout", paid=True)
    with pytest.raises(Exception):
        invoke(broker)
    before = app.store.ledger()
    with pytest.raises(ControlError, match="REQUEST_OUTCOME_UNKNOWN"):
        invoke(broker)
    assert len(calls) == 1 and app.store.ledger() == before
    def reply(req):
        calls.append(req)
        return good(req)
    broker.client_factory = lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(reply))
    result = asyncio.run(broker.run_structured(role="manager", instructions="Test", input_text="new data",
        output_type=SmokeOutput, model="manual-id"))
    assert result.output.status == "ok" and result.request_count == 1
    assert len(calls) == 2
    assert app.store.ledger()["unresolved"] == before["unresolved"]
    assert [r["status"] for r in app.store.ledger()["requests"]] == ["UNRESOLVED", "SETTLED"]
    with pytest.raises(ControlError, match="REQUEST_OUTCOME_UNKNOWN"):
        invoke(broker)
    assert len(calls) == 2


def test_explicit_owner_request_epoch_allows_unknown_request_continuation_once(app):
    broker, calls = gateway(app, mode="timeout", paid=True)
    with pytest.raises(Exception):
        invoke(broker)
    old = app.store.ledger()["requests"][0]
    app.store.put("request_epoch", broker.rid, {"value": "explicit-owner-request", "issued_at": utc_now().isoformat()})
    def reply(req):
        calls.append(req)
        return good(req)
    broker.client_factory = lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(reply))
    assert invoke(broker).output.status == "ok"
    assert invoke(broker).request_count == 0
    assert len(calls) == 2
    assert app.store.ledger()["requests"][0] == old
    assert app.store.db.execute("SELECT count(*) FROM control_configs WHERE kind='provider_request'").fetchone()[0] == 2


def test_request_claim_release_allows_retry_and_settlement_preserves_claim(app):
    kwargs = dict(rid="r", connection="c", model="m", role="manager", purpose="research", bound="0.01",
                  run_limit="1", monthly_limit="1", request_limit="1", attempts=5, revision="QA", request_key="same-step")
    first = app.store.reserve(**kwargs)
    with pytest.raises(ControlError, match="REQUEST_IN_FLIGHT"):
        app.store.reserve(**kwargs)
    app.store.transition(first, "RELEASED")
    second = app.store.reserve(**kwargs)
    app.store.transition(second, "DISPATCHED")
    app.store.transition(second, "SETTLED", settled="0.005")
    with pytest.raises(ControlError, match="REQUEST_ALREADY_COMPLETED"):
        app.store.reserve(**kwargs)
    assert app.store.ledger()["spent"] == "0.005"


def test_unresolved_bounds_still_limit_run_month_and_internal_budget(app):
    configure(app)
    rid = create(app)
    app.read._state.configure_budget(rid, .25, .75, 1)
    kwargs = dict(rid=rid, connection="c", model="m", role="manager", purpose="research", bound="0.7",
                  run_limit="1", monthly_limit="1", request_limit="1", attempts=5, revision="QA", request_key="first")
    first = app.store.reserve(**kwargs)
    app.store.transition(first, "DISPATCHED")
    app.store.transition(first, "UNRESOLVED")
    app.store.db.execute("UPDATE spend_ledger SET month='2001-01'")
    sync_ledger_budget(app.store.db, rid)
    assert app.read._state.budget(rid)["remaining_usd"] == pytest.approx(.3)
    with pytest.raises(ControlError, match="BUDGET_BLOCKED"):
        app.store.reserve(**(kwargs | {"request_key": "new", "bound": ".4"}))
    with pytest.raises(ControlError, match="BUDGET_BLOCKED"):
        app.store.reserve(**(kwargs | {"rid": "different", "request_key": "new", "bound": ".4"}))
    assert app.store.ledger()["unresolved"] == ".7" or Decimal(app.store.ledger()["unresolved"]) == Decimal(".7")


def test_past_unresolved_does_not_replace_current_failure_with_billing_error(app):
    configure(app)
    rid = create(app)
    first = app.store.reserve(rid=rid, connection="local", model="manual-id", role="manager", purpose="research",
        bound=".01", run_limit="1", monthly_limit="20", request_limit="1", attempts=10, revision="QA")
    app.store.transition(first, "DISPATCHED")
    app.store.transition(first, "UNRESOLVED")
    app.store.db.execute("UPDATE control_runs SET status='STARTING' WHERE research_id=?", (rid,))
    class Blocked:
        name = "control_broker"
        async def run_structured(self, **kwargs):
            raise ControlError("PRICE_REQUIRED")
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=lambda *_: Blocked()))
    assert app.store.run(rid)["status"] == "BUDGET_BLOCKED"
    assert app.store.run(rid)["error"] == "PRICE_REQUIRED"
    assert app.store.ledger()["unresolved"] == "0.01"


@pytest.mark.parametrize("prior_state", ["WAITING_ESCALATION", "FAILED", "LEGACY_FATAL"])
def test_science_timeout_can_resume_without_losing_cost_or_failed_step_history(app, prior_state):
    configure(app)
    raw = app.store.config("model", "m")
    raw.update(input_byte_limit=200000, context_limit=262144)
    app.store.put("model", "m", raw, 1)
    created = app.create({"beginner_mode": True, "settings_version": 2, "execution_mode": "SCIENCE_AUTO",
        "question": "식물 발아의 조건을 조사", "model_profile_id": "m", "egress": "selected",
        "search_policy": "DISABLED", "research_profile_mode": "DISABLED", "adaptive_budget": False})
    rid = created["research_id"]
    calls = []
    def reply(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ReadTimeout("QA unknown acceptance", request=req)
        return httpx.Response(200, json={"id": "continued-response", "usage": {"prompt_tokens": 30, "completion_tokens": 10},
            "choices": [{"message": {"content": json.dumps({"action": "NEED_INPUT", "rationale": "원자료가 필요합니다."})}}]})
    def factory(store, active_rid, snapshot):
        return RoutedGateway(store, app.credentials, active_rid, snapshot,
            client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(reply)))
    app.command(rid, "start", {"expected_version": 0, "idempotency_key": "cost-fix-start"})
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=factory))
    before = app.store.ledger()["requests"][0]
    assert app.store.run(rid)["status"] == "NEEDS_RECONCILIATION"
    assert app.store.run(rid)["error"] == "TIMEOUT"
    assert app.read._state._db.execute("SELECT run_status FROM research_runs WHERE research_id=?", (rid,)).fetchone()[0] == "ACTIVE"
    if prior_state == "FAILED":
        app.read._state.fail_runtime_step(rid, "science:decision_output:0", {"error": "TIMEOUT"})
    elif prior_state == "LEGACY_FATAL":
        app.read._state.stop_research(rid, "FATAL_ERROR")
    # 오래된 최초 시작 기록을 덮어쓰지 않고 새 실행 시간으로 이어간다.
    app.store.db.execute("UPDATE runtime_steps SET output_json=json_set(output_json,'$.started_at',?) WHERE research_id=? AND step_key='science:started'", (utc_now().timestamp() - 86400, rid))
    original_start_step = app.read._state.runtime_step(rid, "science:started")
    body = {"expected_version": app.store.run(rid)["version"], "idempotency_key": "cost-fix-resume"}
    assert app.command(rid, "resume", body)["status"] == "RESUMING"
    epoch = app.store.config("request_epoch", rid)
    assert app.read._state.runtime_step(rid, "science:started") == original_start_step
    assert app.command(rid, "resume", body)["status"] == "RESUMING"
    assert app.store.config("request_epoch", rid) == epoch
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=factory))
    assert app.store.run(rid)["status"] == "INSUFFICIENT_DATA", app.store.run(rid)
    assert len(calls) == 2 and app.store.ledger()["requests"][0] == before
    assert app.store.db.execute("SELECT count(*) FROM agent_runs WHERE research_id=? AND status='FAILED'", (rid,)).fetchone()[0] == 1
    assert app.store.db.execute("SELECT count(*) FROM contracts WHERE research_id=? AND runtime_key LIKE '%owner-resume:%'", (rid,)).fetchone()[0] == 1


def test_new_report_and_batch_admission_ignore_past_unresolved_cost(app, monkeypatch):
    from test_research_report_flow import rig, run
    from probe.research_report import rewrite_report
    from probe.report_maintenance import admission
    factory, calls, searches = rig(app, monkeypatch)
    rid = run(app, factory)
    identity = app.store.reserve(rid=rid, connection="local", model="manual-id", role="manager", purpose="research",
        bound=".01", run_limit="1", monthly_limit="20", request_limit="1", attempts=20, revision="QA")
    app.store.transition(identity, "DISPATCHED")
    app.store.transition(identity, "UNRESOLVED")
    app.store.db.execute("UPDATE control_runs SET status='NEEDS_RECONCILIATION',pid=NULL WHERE research_id=?", (rid,))
    before = app.store.ledger()["requests"][-1]
    assert admission(app, rid)["minimum_request_bound_usd"] == "0"
    body = {"idempotency_key": "new-report-with-old-cost", "expected_version": app.store.run(rid)["version"],
            "state_version": app.read._state.state_version(rid)}
    result = asyncio.run(rewrite_report(app, rid, body, provider_factory=factory))
    assert result["status"] == "READY" and result.get("error") != "NEEDS_RECONCILIATION"
    assert app.store.ledger()["requests"][-2] == before
    assert app.store.ledger()["unresolved"] == "0.01"


def test_explicit_search_continuation_keeps_old_uncertain_cost_and_search_slots(app):
    from types import SimpleNamespace
    from test_ai_web_search import research, native_config, response, HTML, URL
    from probe.source_documents import PublicDocumentClient
    from probe.search_policy import run_search, qualified_literature
    from probe.control_plane import PriceRecord
    from probe.ai_web_search import SharedSearchSlots
    rid, snapshot = research(app, required=True)
    connection, profile, _ = native_config()
    price = PriceRecord(input_per_million=Decimal('.1'), output_per_million=Decimal('.5'),
        web_search_per_call=Decimal('.01'), source='모의 가격', checked_at=utc_now(), revision='continuation', owner_verified=True)
    profile = profile.model_copy(update={'price': price, 'context_limit': 131072, 'max_input_tokens': 128000})
    snapshot['models'] = {role: profile.model_dump(mode='json') for role in snapshot['models']}
    snapshot['search_attempt_limit'] = 6
    app.store.put('connection', connection.connection_id, connection)
    credentials = SimpleNamespace(get=lambda *_: 'offline-placeholder', active_secrets=lambda *_: [])
    calls = []
    def reply(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ReadTimeout('QA unknown search acceptance', request=req)
        return httpx.Response(200, json=response())
    broker = RoutedGateway(app.store, credentials, rid, snapshot,
        client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(reply)))
    client = PublicDocumentClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=HTML, headers={'Content-Type': 'text/html'})))
    with pytest.raises(Exception):
        asyncio.run(run_search(app.state, app.store, credentials, rid, snapshot, gateway=broker, document_client=client))
    old_request = app.store.ledger()['requests'][0]
    old_slot = next(v for v in app.store.configs('hosted_search_slots') if v['kind'] == 'hosted')
    assert old_slot['status'] == 'UNRESOLVED'
    with pytest.raises(ControlError, match='NEEDS_RECONCILIATION'):
        asyncio.run(run_search(app.state, app.store, credentials, rid, snapshot, gateway=broker, document_client=client))
    assert len(calls) == 1
    app.store.put('request_epoch', rid, {'value': 'explicit-search-continuation', 'issued_at': utc_now().isoformat()})
    asyncio.run(run_search(app.state, app.store, credentials, rid, snapshot, gateway=broker, document_client=client))
    assert len(calls) == 2 and qualified_literature(app.state, rid)
    assert app.store.ledger()['requests'][0] == old_request
    slots = app.store.configs('hosted_search_slots')
    assert sum(v['status'] == 'UNRESOLVED' for v in slots if v['kind'] == 'hosted') == 1
    assert app.store.config('search_attempts', rid)['used'] == 6


@pytest.mark.parametrize("new_request", [False, True])
def test_interrupted_report_recovery_only_flags_its_own_uncertain_request(app, new_request):
    from probe.research_report import _save, recover_report_rewrites
    configure(app)
    rid = create(app)
    kwargs = dict(rid=rid, connection="local", model="manual-id", role="manager", purpose="research",
        bound=".01", run_limit="1", monthly_limit="20", request_limit="1", attempts=10, revision="QA")
    old = app.store.reserve(**kwargs)
    app.store.transition(old, "DISPATCHED")
    app.store.transition(old, "UNRESOLVED")
    previous = app.store.ledger()['requests'][0]
    key = 'interrupted-report'
    _save(app.store, 'report_command', key, {'research_id': rid, 'status': 'RUNNING'})
    _save(app.store, 'report_rewrite', rid, {'status': 'RUNNING', 'key': key, 'owner_pid': None, 'prior_reservations': [old]})
    if new_request:
        app.store.reserve(**kwargs)
    recover_report_rewrites(app)
    result = app.store.config('report_command', key)
    assert result['status'] == ('NEEDS_RECONCILIATION' if new_request else 'FAILED')
    assert result['error'] == ('NEEDS_RECONCILIATION' if new_request else 'REPORT_REWRITE_INTERRUPTED')
    assert app.store.ledger()['requests'][0] == previous


def test_science_default_wire_limit_expands_only_for_automatic_profiles(app):
    from probe.science_policy import prepare_science_profiles
    configure(app)
    snapshot = app.prepare({'beginner_mode': True, 'execution_mode': 'SCIENCE_AUTO', 'question': '공개 자료 탐구',
        'model_profile_id': 'm', 'egress': 'selected', 'search_policy': 'DISABLED'})
    raw = snapshot['models']['manager']
    raw.update(profile_id='AUTO-test', input_byte_limit=32000, context_limit=160768, max_input_tokens=128000)
    snapshot['explicit_model_limits'] = {'AUTO-test': False}
    prepare_science_profiles(snapshot)
    assert raw['input_byte_limit'] == 128000
    raw['input_byte_limit'] = 32000
    snapshot['explicit_model_limits']['AUTO-test'] = True
    prepare_science_profiles(snapshot)
    assert raw['input_byte_limit'] == 32000


def test_owner_restart_after_context_failure_reopens_only_failed_execution(app):
    configure(app)
    rid = create(app)
    app.read._state.stop_research(rid, 'FATAL_ERROR')
    app.store.db.execute("UPDATE control_runs SET status='FAILED',error='CONTEXT_LIMIT_BLOCKED',pid=NULL WHERE research_id=?", (rid,))
    result = app.command(rid, 'start', {'expected_version': 0, 'idempotency_key': 'context-owner-restart'})
    assert result['status'] == 'STARTING'
    assert app.store.db.execute('SELECT run_status,stop_reason FROM research_runs WHERE research_id=?', (rid,)).fetchone()[0] == 'ACTIVE'
    assert not app.store.ledger()['requests']
