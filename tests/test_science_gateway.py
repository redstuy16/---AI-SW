"""과학 모드의 실제 중개·비용·동시 실행 경계를 모의 HTTP로 확인한다."""
import asyncio
from decimal import Decimal
import json

import httpx
import pytest

from probe.ai_web_search import SharedSearchSlots
from probe.control_plane import ControlError, ModelProfile
from probe.control_runtime import RoutedGateway
from probe.providers.normalized import HostedWebSearchTool
from probe.science_policy import prepare_science_profiles, request_cost_bound
from test_multi_provider import app, setup_app
from test_ai_web_search import response


def hosted(app, monkeypatch):
    _, profile, request, snapshot = setup_app(app, "openai", monkeypatch)
    profile.model_id = "gpt-6-luna"
    profile.context_limit = 131072
    profile.max_input_tokens = 128000
    profile.price.web_search_per_call = Decimal("0.01")
    snapshot.update(execution_mode="SCIENCE_AUTO", request_limit_usd="1", run_limit_usd="1", search_attempt_limit=3)
    snapshot["models"]["manager"] = profile.model_dump(mode="json")
    app.store.put("model", "m", profile, 1)
    request.hosted_tools = [HostedWebSearchTool()]
    request.max_tool_calls = 3
    request.max_output_tokens = 2048
    request.hosted_input_token_bound = 128000
    identity = SharedSearchSlots(app.store, "r", snapshot).reserve(3)
    request.metadata.update(hosted_search_slot_id=identity, hosted_search_slots_reserved=3)
    return profile, request, snapshot, identity


def test_hosted_tokens_use_context_not_input_byte_limit_and_charge_once(app, monkeypatch):
    profile, request, snapshot, identity = hosted(app, monkeypatch)
    sent = []
    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json=response())
    gateway = RoutedGateway(app.store, app.credentials, "r", snapshot,
        client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    first, _, dispatched = asyncio.run(gateway.generate(request))
    assert dispatched == 1 and first.usage.web_search_calls == 1
    assert profile.input_byte_limit < request.hosted_input_token_bound
    assert first.provider_metadata["estimated_cost_usd"] == "0.0102"
    SharedSearchSlots(app.store, "r", snapshot).finish(identity, 3)
    replay, _, dispatched = asyncio.run(gateway.generate(request))
    assert dispatched == 0 and replay.usage.input_tokens == replay.usage.web_search_calls == 0
    assert len(sent) == app.store.db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 1
    assert app.store.db.execute("SELECT settled FROM spend_ledger").fetchone()[0] == 10200


def test_hosted_without_shared_reservation_never_dispatches(app, monkeypatch):
    _, request, snapshot, _ = hosted(app, monkeypatch)
    request.metadata["hosted_search_slot_id"] = "missing"
    gateway = RoutedGateway(app.store, app.credentials, "r", snapshot)
    with pytest.raises(ControlError, match="SEARCH_RESERVATION_INVALID"):
        asyncio.run(gateway.generate(request))
    assert gateway.dispatch_count == 0 and not app.store.ledger()["requests"]


def test_automatic_output_limits_preserve_explicit_user_profile(app, monkeypatch):
    _, profile, _, snapshot = setup_app(app, "openai", monkeypatch)
    snapshot["execution_mode"] = "SCIENCE_AUTO"
    snapshot["depth_limits"]["attempts"] = 5
    profile.output_limit = 1024
    snapshot["models"]["manager"] = profile.model_dump(mode="json")
    prepare_science_profiles(snapshot)
    assert snapshot["depth_limits"]["attempts"] == 18
    assert snapshot["models"]["manager"]["output_limit"] == 1024
    snapshot["models"]["manager"]["profile_id"] = "AUTO-science"
    prepare_science_profiles(snapshot)
    assert snapshot["models"]["manager"]["task_output_limits"] == {"planning": 16384, "report": 16384}


def test_science_parallel_reservations_keep_unresolved_and_cross_research_protection(app):
    kwargs = dict(connection="c", model="m", role="manager", purpose="research", bound="0.01",
        run_limit="1", monthly_limit="10", request_limit="1", attempts=6, revision="test", run_concurrency=2)
    first = app.store.reserve(rid="r", **kwargs)
    app.store.transition(first, "DISPATCHED")
    second = app.store.reserve(rid="r", **kwargs)
    app.store.transition(second, "DISPATCHED")
    with pytest.raises(ControlError, match="SERVER_BUSY"):
        app.store.reserve(rid="other", **kwargs)
    app.store.transition(first, "UNRESOLVED")
    third = app.store.reserve(rid="r", **kwargs)
    assert third not in {first, second}
    assert app.store.ledger()["unresolved"] == "0.01"
    with pytest.raises(ControlError, match="REQUEST_IN_FLIGHT"):
        app.store.reserve(rid="r", **kwargs)


def test_wire_size_and_model_context_have_independent_limits(app, monkeypatch):
    _, profile, _, _ = setup_app(app, "openai", monkeypatch)
    profile.context_limit = 131072
    assert request_cost_bound(profile, 1000, 128000, 2048) > 0
    with pytest.raises(ControlError, match="CONTEXT_LIMIT_BLOCKED"):
        request_cost_bound(profile, profile.input_byte_limit+1, 1000, 2048)
    with pytest.raises(ControlError, match="CONTEXT_LIMIT_BLOCKED"):
        request_cost_bound(profile, 1000, 128000, 8192)


def test_required_search_explains_explicit_context_limit_before_dispatch(app, monkeypatch):
    _, profile, _, _ = setup_app(app, "openai", monkeypatch)
    profile.model_id = "gpt-6-luna"
    app.store.put("model", "m", profile, 1)
    created = app.create({"settings_version": 2, "execution_mode": "SCIENCE_AUTO",
        "question": "광합성 문헌의 출처를 확인합니다.", "model_profile_id": "m", "egress": "research",
        "public_search_consent": True, "search_policy": "ALLOWED", "search_required": True, "run_limit_usd": "1"})
    view = app.preflight(created["snapshot"])
    assert "SEARCH_CONTEXT_LIMIT_BLOCKED" in view["reasons"]
    assert not view["ready"] and app.store.ledger()["requests"] == []


def test_relaxed_automatic_output_respects_discovered_provider_maximum(app, monkeypatch):
    _, profile, _, snapshot = setup_app(app, "openai", monkeypatch)
    raw = profile.model_dump(mode="json")
    raw.update(profile_id="AUTO-provider-small", max_output_tokens=4096)
    snapshot.update(execution_mode="SCIENCE_AUTO", models={"manager": raw})
    prepare_science_profiles(snapshot)
    raw = snapshot["models"]["manager"]
    assert raw["output_limit"] == 4096
    assert raw["task_output_limits"] == {"planning": 4096, "report": 4096}


def test_search_output_uses_relaxed_planning_limit_and_fits_reserved_context(app, monkeypatch):
    from probe.ai_web_search import search_output_limit
    _, profile, _, snapshot = setup_app(app, "openai", monkeypatch)
    raw = profile.model_dump(mode="json")
    raw.update(profile_id="AUTO-search-room", model_id="gpt-6-luna")
    snapshot.update(execution_mode="SCIENCE_AUTO", models={"manager": raw})
    prepare_science_profiles(snapshot)
    before = dict(snapshot["models"]["manager"])
    profile = ModelProfile.model_validate(before)
    assert search_output_limit(profile) == 16384
    assert 128000 + search_output_limit(profile) <= profile.context_limit
    prepare_science_profiles(snapshot)
    assert snapshot["models"]["manager"] == before


def test_search_output_fits_existing_manual_context_without_overwriting_it():
    from probe.ai_web_search import search_output_limit
    profile = ModelProfile(profile_id="manual", connection_id="c", model_id="manual", protocol="responses",
        output_limit=16384, context_limit=131072, task_output_limits={"planning": 8192, "report": 16384})
    assert search_output_limit(profile) == 3072
    assert profile.context_limit == 131072 and profile.output_limit == 16384


def test_existing_luna_profile_resolves_missing_cache_prices_and_tool_cost(app, monkeypatch):
    profile, request, snapshot, _ = hosted(app, monkeypatch)
    profile.price.input_per_million = Decimal('0.1')
    profile.price.output_per_million = Decimal('0.5')
    snapshot['models']['manager'] = profile.model_dump(mode='json')
    calls = []
    def handler(req):
        calls.append(req)
        raw = response(actions=('search',))
        raw['usage']['input_tokens_details'] = {'cached_tokens': 20}
        raw['usage']['cache_write_tokens'] = 10
        return httpx.Response(200, json=raw)
    gateway = RoutedGateway(app.store, app.credentials, 'r', snapshot,
        client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result, _, dispatched = asyncio.run(gateway.generate(request))
    assert dispatched == 1 and result.provider_metadata['estimated_cost_usd'] == '0.01003345'
    row = app.store.db.execute('SELECT * FROM spend_ledger').fetchone()
    assert row['status'] == 'SETTLED' and row['settled'] == 10034
    trace = json.loads(app.store.db.execute("SELECT payload FROM control_audit WHERE kind='NORMALIZED_RESPONSE_SETTLED'").fetchone()[0])
    assert trace['resolved_price_categories'] == {'cached_input_per_million': '0.01', 'cache_write_per_million': '0.125'}
    _, _, dispatched = asyncio.run(gateway.generate(request))
    assert dispatched == 0 and len(calls) == 1
