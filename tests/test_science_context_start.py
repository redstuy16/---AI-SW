"""앱의 연구 생성·시작 경로에서 긴 과학 요청의 실제 전송 크기를 검사한다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from probe.control_runtime import RoutedGateway, execute
from probe.research_report import execution_summary
from probe.science_policy import prepare_science_profiles
from test_multi_provider import app, setup_app


QUESTION = json.loads((Path(__file__).resolve().parents[1] / 'qa/climate_questions.json').read_text(encoding='utf-8'))[4]['question']


def setup_model(app, monkeypatch, *, explicit=False):
    connection, profile, _, _ = setup_app(app, 'openai', monkeypatch)
    profile = profile.model_copy(update={'profile_id': 'AUTO-context-start', 'model_id': 'gpt-6-luna',
        'input_byte_limit': 32000, 'context_limit': 160768, 'output_limit': 32768, 'max_input_tokens': 128000})
    app.store.put('model', profile.profile_id, profile)
    if explicit:
        from probe.science_policy import record_model_limit_policy
        record_model_limit_policy(app.store, profile.profile_id, {'input_byte_limit'})
    return profile


def body(profile, **changes):
    return {'question': QUESTION, 'execution_mode': 'SCIENCE_AUTO', 'model_profile_id': profile.profile_id,
        'search_policy': 'DISABLED', 'egress': 'research', 'run_limit_usd': '1', **changes}


def test_long_research_uses_app_defaults_and_preserves_explicit_settings(app, monkeypatch):
    profile = setup_model(app, monkeypatch)
    saved = deepcopy(app.store.config('model', profile.profile_id))
    snapshot = app.prepare(body(profile))
    assert snapshot['models']['manager']['input_byte_limit'] == 128000
    assert snapshot['science_max_decisions'] == 20
    assert snapshot['science_context_budget'] == 64000
    assert snapshot['science_max_actions'] == 200
    assert app.store.config('model', profile.profile_id) == saved
    fixed = app.prepare(body(profile, science_max_decisions=4, science_no_progress_limit=1, max_elapsed_sec=60))
    assert (fixed['science_max_decisions'], fixed['science_no_progress_limit'], fixed['max_elapsed_sec']) == (4, 1, 60)
    short = app.prepare(body(profile, question='지구의 평균 흡수 복사량을 계산해 주세요.'))
    assert short['science_max_decisions'] == 6
    assert 'science_context_budget' not in short
    legacy = app.prepare(body(profile, execution_mode='LEGACY', research_profile_mode='DISABLED'))
    assert legacy['models']['manager']['input_byte_limit'] == 32000


def test_explicit_automatic_model_limits_and_provider_maximum_stay_fixed(app, monkeypatch):
    profile = setup_model(app, monkeypatch, explicit=True)
    snapshot = app.prepare(body(profile))
    assert snapshot['explicit_model_limits'][profile.profile_id]
    assert snapshot['models']['manager']['input_byte_limit'] == 32000
    candidate = deepcopy(snapshot)
    candidate['explicit_model_limits'] = {}
    candidate['models']['manager']['max_input_tokens'] = 60000
    prepare_science_profiles(candidate)
    assert candidate['models']['manager']['input_byte_limit'] == 60000


@pytest.mark.parametrize('explicit', [False, True])
def test_app_create_and_start_reaches_native_serialization_without_live_requests(app, monkeypatch, explicit):
    profile = setup_model(app, monkeypatch, explicit=explicit)
    sent = []
    def handler(request):
        sent.append(request)
        answer = {'action': 'NEED_INPUT', 'rationale': '모의 실행에서는 공식 관측 자료를 제공하지 않았습니다.'}
        return httpx.Response(200, json={'id': 'resp-context-offline', 'model': 'gpt-6-luna', 'status': 'completed',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(answer, ensure_ascii=False)}]}],
            'usage': {'input_tokens': 1000, 'output_tokens': 100, 'input_tokens_details': {'cached_tokens': 0}}})
    created = app.request('POST', '/api/control/research', body(profile))
    assert created.status == 201, created.body
    rid = created.body['research_id']
    started = app.request('POST', f'/api/control/research/{rid}/start',
        {'expected_version': 0, 'idempotency_key': 'context-native-start'})
    assert started.status == 200, started.body
    def factory(store, research_id, snapshot):
        return RoutedGateway(store, app.credentials, research_id, snapshot,
            client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=factory))
    result = app.store.run(rid)
    if explicit:
        assert result['error'] == 'CONTEXT_LIMIT_BLOCKED'
        assert not sent and not app.store.ledger(rid)['requests']
        blocker = execution_summary(app, rid)['blocker']
        assert blocker['code'] == 'CONTEXT_LIMIT_BLOCKED' and '입력 한도' in blocker['message']
    else:
        assert result['status'] == 'INSUFFICIENT_DATA', result
        assert result['error'] != 'CONTEXT_LIMIT_BLOCKED'
        assert len(sent) == 1 and len(sent[0].content) > 32000
        assert app.read._state.runtime_step(rid, 'science:decision_output:0')['status'] == 'COMPLETED'
