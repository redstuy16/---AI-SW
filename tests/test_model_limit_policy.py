"""소유자가 저장한 자동 구성의 상한과 과거 호출 지문을 보존한다."""
import asyncio
from copy import deepcopy

import httpx
import pytest

from probe.control_plane import Connection, ControlError, ModelProfile
from probe.control_runtime import RoutedGateway
from probe.product_policy import effective_snapshot
from probe.provider_checks import check_model
from probe.research_settings import apply_pending
from probe.science_policy import explicit_model_limits, prepare_science_profiles
from test_multi_provider import app, document, setup_app


def automatic_profile(app, monkeypatch, *, identity='AUTO-owner-limits'):
    _, profile, _, _ = setup_app(app, 'openai', monkeypatch)
    profile.profile_id, profile.model_id = identity, 'gpt-6-luna'
    profile.output_limit = 8192
    profile.task_output_limits = {'planning': 2048, 'report': 8192}
    app.store.put('model', identity, profile)
    return profile


def science_request(identity):
    return {'settings_version': 2, 'execution_mode': 'SCIENCE_AUTO', 'title': '과학 상한 검사',
            'question': '빛 조건에 따른 식물 생장의 원리를 설명합니다.', 'model_profile_id': identity,
            'egress': 'research', 'run_limit_usd': '1'}


def owner_save(app, profile, **changes):
    raw = profile.model_dump(mode='json')
    raw.update(changes)
    revision = next(p['revision'] for p in app.store.configs('model') if p['profile_id'] == profile.profile_id)
    return app.request('POST', '/api/control/models', {'value': raw, 'expected_revision': revision})


def test_owner_api_limits_survive_new_science_research_and_frozen_resume(app, monkeypatch):
    profile = automatic_profile(app, monkeypatch)
    original = app.create(science_request(profile.profile_id))['snapshot']
    assert original['models']['manager']['context_limit'] == 160768
    assert owner_save(app, profile, context_limit=4096, output_limit=256,
                      task_output_limits={'planning': 128, 'report': 256}).status == 200
    policy = app.store.config('model_limit_policy', profile.profile_id)
    assert policy['explicit_limits'] is True and 'output_limit' in policy['declared_fields']
    created = app.create(science_request(profile.profile_id))
    snapshot = created['snapshot']
    assert snapshot['explicit_model_limits'] == {profile.profile_id: True}
    for raw in snapshot['models'].values():
        assert raw['context_limit'] == 4096 and raw['output_limit'] == 256
        assert raw['task_output_limits'] == {'planning': 128, 'report': 256}
        assert raw['max_input_tokens'] is None
    resumed = effective_snapshot(app.store, created['research_id'])
    prepare_science_profiles(resumed)
    assert resumed['models'] == snapshot['models']
    assert original['models']['manager']['output_limit'] == 32768
    assert 'explicit_limits' not in ModelProfile.model_fields and not app.store.ledger()['requests']


@pytest.mark.parametrize('selection', ['model', 'role'])
def test_owner_limits_survive_settings_model_and_role_reselection(app, monkeypatch, selection):
    profile = automatic_profile(app, monkeypatch)
    created = app.create(science_request(profile.profile_id if selection == 'model' else 'm'))
    frozen = deepcopy(created['snapshot'])
    assert owner_save(app, profile, context_limit=8192, output_limit=512,
                      task_output_limits={'planning': 256, 'report': 512}).status == 200
    value = {'run_limit_usd': '1', 'max_elapsed_sec': 600, 'egress': 'research'}
    if selection == 'model':
        value['model_profile_id'] = profile.profile_id
    else:
        value.update(manual_role_override=True, routing={'analysis_planner_worker': profile.profile_id})
    response = app.request('POST', '/api/control/research/' + created['research_id'] + '/settings',
                           {'expected_version': 0, 'value': value})
    assert response.status == 200
    assert response.body['settings']['explicit_model_limits'][profile.profile_id] is True
    apply_pending(app.store, app.read._state, created['research_id'], created['snapshot'])
    prepare_science_profiles(created['snapshot'])
    role = 'manager' if selection == 'model' else 'analysis_planner_worker'
    current = created['snapshot']['models'][role]
    assert current['output_limit'] == 512 and current['context_limit'] == 8192
    assert current['task_output_limits'] == {'planning': 256, 'report': 512}
    assert effective_snapshot(app.store, created['research_id'])['explicit_model_limits'][profile.profile_id]
    assert app.store.run(created['research_id'])['snapshot'] == frozen
    assert not app.store.ledger()['requests']


@pytest.mark.parametrize('failure', ['stale', 'callback'])
def test_model_and_limit_marker_save_are_atomic(app, monkeypatch, failure):
    import probe.science_policy as policy_module
    profile = automatic_profile(app, monkeypatch)
    before = app.store.config('model', profile.profile_id)
    raw = {**before, 'output_limit': 512}
    if failure == 'callback':
        original = policy_module.record_model_limit_policy
        def fail_after_marker(*args):
            original(*args)
            raise ControlError('MODEL_LIMIT_POLICY_SAVE_FAILED')
        monkeypatch.setattr(policy_module, 'record_model_limit_policy', fail_after_marker)
    response = app.request('POST', '/api/control/models', {'value': raw,
        'expected_revision': 0 if failure == 'stale' else 1})
    assert response.status == 409
    assert app.store.config('model', profile.profile_id) == before
    with pytest.raises(ControlError, match='CONFIG_MISSING'):
        app.store.config('model_limit_policy', profile.profile_id)
    assert app.store.db.execute("SELECT COUNT(*) FROM control_audit WHERE kind='MODEL_LIMITS_DECLARED'").fetchone()[0] == 0


def test_metadata_and_price_only_updates_keep_automatic_policy(app, monkeypatch):
    profile = automatic_profile(app, monkeypatch)
    profile.price.revision = 'price-only-update'
    app.store.put('model', profile.profile_id, profile, 1)
    profile.max_input_tokens, profile.max_output_tokens = 131072, 32768
    app.store.put('model', profile.profile_id, profile, 2)
    assert explicit_model_limits(app.store, {'manager': profile.model_dump(mode='json')}) == {}
    with pytest.raises(ControlError, match='CONFIG_MISSING'):
        app.store.config('model_limit_policy', profile.profile_id)
    snapshot = app.create(science_request(profile.profile_id))['snapshot']
    assert snapshot['models']['manager']['output_limit'] == 32768
    assert snapshot['models']['manager']['context_limit'] == 160768


def test_old_owner_limit_change_is_inferred_without_migrating_database(app, monkeypatch):
    profile = automatic_profile(app, monkeypatch)
    profile.output_limit, profile.context_limit = 256, 4096
    profile.task_output_limits = {}
    app.store.put('model', profile.profile_id, profile, 1)
    assert explicit_model_limits(app.store, {'manager': profile.model_dump(mode='json')}) == {profile.profile_id: True}
    snapshot = app.create(science_request(profile.profile_id))['snapshot']
    assert snapshot['models']['manager']['output_limit'] == 256
    assert snapshot['models']['manager']['context_limit'] == 4096
    with pytest.raises(ControlError, match='CONFIG_MISSING'):
        app.store.config('model_limit_policy', profile.profile_id)


def test_old_limits_do_not_cross_deleted_profile_creation(app, monkeypatch):
    profile = automatic_profile(app, monkeypatch)
    profile.output_limit = 512
    app.store.put('model', profile.profile_id, profile, 1)
    assert explicit_model_limits(app.store, {'manager': profile.model_dump(mode='json')})
    app.store.db.execute("DELETE FROM control_configs WHERE kind='model' AND id=?", (profile.profile_id,))
    profile.output_limit = 8192
    app.store.put('model', profile.profile_id, profile)
    assert explicit_model_limits(app.store, {'manager': profile.model_dump(mode='json')}) == {}


def test_limit_origin_unknown_at_creation_is_not_fabricated(app, monkeypatch):
    _, profile, _, _ = setup_app(app, 'openai', monkeypatch)
    profile.profile_id = 'AUTO-historical-unknown'
    profile.output_limit, profile.context_limit = 256, 4096
    app.store.put('model', profile.profile_id, profile)
    assert explicit_model_limits(app.store, {'manager': profile.model_dump(mode='json')}) == {}
    assert not app.store.ledger()['requests']


def test_price_api_and_automatic_discovery_never_mark_owner_limits(app, monkeypatch):
    profile = automatic_profile(app, monkeypatch)
    profile.price = None
    app.store.put('model', profile.profile_id, profile, 1)
    quote = app.request('GET', '/api/control/models/' + profile.profile_id + '/pricing')
    assert quote.status == 200 and quote.body['candidate']
    response = app.request('POST', '/api/control/models/' + profile.profile_id + '/pricing',
        {'approve_price': True, 'quote_id': quote.body['quote_id'], 'expected_revision': quote.body['expected_revision']})
    assert response.status == 200 and response.body['status'] == 'PRICE_APPLIED'
    conn = Connection.model_validate(app.store.config('connection', 'c'))
    conn.discovery_unmetered = True
    app.store.put('connection', 'c', conn, 1)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [{'id': 'gpt-6-luna', 'max_input_tokens': 131072,
                                                  'max_output_tokens': 32768}]})
    result = asyncio.run(check_model(app, profile.profile_id, {'mode': 'discovery', 'consent': True},
        client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert result['status'] == 'DISCOVERY_OK' and len(calls) == 1
    current = app.store.config('model', profile.profile_id)
    assert current['max_input_tokens'] == 131072
    assert explicit_model_limits(app.store, {'manager': current}) == {}
    snapshot = app.create(science_request(profile.profile_id))['snapshot']
    assert snapshot['models']['manager']['context_limit'] == 160768
    assert snapshot['models']['manager']['output_limit'] == 32768
    assert all(row['reserved'] == row['settled'] == 0 for row in app.store.ledger()['requests'])


def test_sidecar_snapshot_preserves_existing_model_cache_identity(app, monkeypatch):
    _, _, request, snapshot = setup_app(app, 'openai', monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=document('responses'))
    factory = lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = RoutedGateway(app.store, app.credentials, 'r', snapshot, client_factory=factory)
    _, _, count = asyncio.run(first.generate(request))
    assert count == 1
    frozen = deepcopy(snapshot)
    frozen['explicit_model_limits'] = {'m': True}
    replay = RoutedGateway(app.store, app.credentials, 'r', frozen, client_factory=factory)
    _, _, count = asyncio.run(replay.generate(request))
    assert count == 0 and len(calls) == 1


def test_connection_delete_removes_explicit_limit_sidecar(app, monkeypatch):
    profile = automatic_profile(app, monkeypatch)
    assert owner_save(app, profile, output_limit=512).status == 200
    monkeypatch.setattr(app.credentials, 'save', lambda *_: {'status': 'REMOVED'})
    result = app.request('POST', '/api/control/connections/c/delete', {'confirm': True, 'expected_revision': 1})
    assert result.status == 200 and result.body['deleted'] is True
    with pytest.raises(ControlError, match='CONFIG_MISSING'):
        app.store.config('model_limit_policy', profile.profile_id)
