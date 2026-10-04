"""연구 기본 화면의 상태·현재성·읽기 전용 경계를 확인한다."""
import json

import pytest

from htrsa.research_screen import screen_summary, status_badge
from htrsa.research_flow import project_flow
from test_workbench import app, configure, create
from test_research_report_flow import rig, run
from test_execution_flow import demos, demo_api


def test_screen_is_read_only_and_does_not_dispatch(app, monkeypatch):
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    before = app.store.db.total_changes
    observed = (len(calls), len(searches), app.store.ledger(rid))
    value = app.request('GET', f'/api/control/research/{rid}/screen')
    assert value.status == 200, value.body
    result = value.body
    assert result['official_status']['label'] == '완료'
    assert [s['id'] for s in result['stages']] == ['question','search','evidence','analysis','execution','verification','report']
    assert result['counts']['verified'] == 1
    assert result['claims'][0]['verified_count'] == 1
    assert next(s for s in result['stages'] if s['id']=='execution')['status'] == 'SKIPPED'
    assert result['conclusion_use'] == 'CURRENT'
    assert result['counts']['checks_passed'] == result['counts']['checks_total'] == 1
    assert '\n' not in result['work']['last']['objective']
    assert 'evidence_text' not in result['work']['last']['objective']
    assert 'current_contract' not in result['overview']
    assert 'recent' not in result and 'phases' not in result
    assert app.store.db.total_changes == before
    assert (len(calls), len(searches), app.store.ledger(rid)) == observed


@pytest.mark.parametrize('report_status', ['RUNNING', 'PARTIAL'])
def test_report_without_draft_keeps_verified_card_readable(app, monkeypatch, report_status):
    from htrsa.research_report import execution_summary
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    value = execution_summary(app, rid)
    assert value['card']['available'] and value['card']['current']
    # 검증 결과가 있어도 보고서 작성 중·실패 직후에는 본문이 없을 수 있다.
    value['report'].update(status=report_status, draft=None)
    value['report_ready'] = False
    app.store.db.execute("UPDATE control_runs SET status='RUNNING' WHERE research_id=?", (rid,))
    monkeypatch.setattr('htrsa.research_report.execution_summary', lambda *_: value)
    before = app.store.db.total_changes
    observed = (len(calls), len(searches), app.store.ledger(rid))
    result = screen_summary(app, rid)
    assert result['official_status']['label'] == '진행 중'
    assert result['conclusion_use'] == 'UNCONFIRMED'
    assert result['report_ready'] is False
    assert result['report']['draft'] is None
    assert app.store.db.total_changes == before
    assert (len(calls), len(searches), app.store.ledger(rid)) == observed


@pytest.mark.parametrize('search', ['empty','no_abstract','rate'])
def test_design_only_is_not_measured_success(app, monkeypatch, search):
    gateway, calls, searches = rig(app, monkeypatch, search=search)
    rid = run(app, gateway, search_required=False)
    result = screen_summary(app, rid)
    assert result['official_status'] == {'code':'COMPLETED','label':'설계안 완료','tone':'review'}
    assert result['conclusion_use'] == 'DESIGN_ONLY'
    assert result['counts']['verified_experiments'] == 0
    assert result['stages'][4]['status'] == 'SKIPPED'
    assert result['stages'][5]['status'] == 'SKIPPED'
    assert result['report_ready'] and result['claims'] == []
    assert app.research_list()[0]['official_status'] == result['official_status']


def test_draft_does_not_claim_finished_stages(app):
    configure(app)
    rid = create(app)
    result = screen_summary(app, rid)
    assert result['official_status']['label'] == '준비 중'
    assert all(s['status']=='WAITING' for s in result['stages'])
    assert result['report_ready'] is False


@pytest.mark.parametrize('status,label,tone', [
    ('RUNNING','진행 중','running'),('PAUSED','일시정지','waiting'),
    ('STOPPED','중단됨','failed'),('INSUFFICIENT_DATA','자료 부족으로 종료','review'),
    ('FAILED','확인 필요','failed'),('BUDGET_BLOCKED','예산 부족','review'),
    ('NEEDS_RECONCILIATION','비용 확인 필요','review'),('COMPLETED','완료','complete')])
def test_official_status_labels(status, label, tone):
    assert status_badge(status) == {'code':status,'label':label,'tone':tone}


def test_screen_and_graph_agree_after_process_exit(app, monkeypatch):
    configure(app)
    rid = create(app)
    app.store.db.execute("UPDATE control_runs SET status='RUNNING',pid=12345678 WHERE research_id=?", (rid,))
    monkeypatch.setattr('htrsa.control_plane.process_alive', lambda pid: False)
    before = app.store.db.total_changes
    result = screen_summary(app, rid)
    flow = project_flow(app, rid, view='all')
    assert result['status'] == flow['control_status'] == 'PAUSED'
    assert result['official_status']['label'] == '일시정지'
    assert not any(s['status']=='RUNNING' for s in result['stages'])
    assert app.store.db.total_changes == before
    assert app.store.run(rid)['status'] == 'RUNNING'


@pytest.mark.parametrize('lane', ['manager','knowledge','experiment','verification','tools'])
def test_lane_filter_uses_existing_relationships(demo_api, lane):
    api, a, b = demo_api
    before = api.store.db.total_changes
    full = project_flow(api, a, view='all', limit=150)
    result = project_flow(api, a, view='all', limit=150, lane=lane)
    assert result['lane_order'] == [lane]
    assert all(node['lane']==lane for node in result['nodes'])
    expected = {node['id'] for node in full['nodes'] if node['lane']==lane}
    assert {node['id'] for node in result['nodes']} == expected
    assert all(edge['source'] in expected and edge['target'] in expected for edge in result['edges'])
    assert api.store.db.total_changes == before


def test_legacy_demo_uses_same_screen_without_control_snapshot(demo_api):
    api, a, b = demo_api
    before = api.store.db.total_changes
    result = api.request('GET', f'/api/control/research/{b}/screen')
    assert result.status == 200, result.body
    assert not result.body['controlled']
    assert result.body['official_status']['label'] == '완료'
    assert result.body['counts']['verified_experiments'] == 2
    assert result.body['counts']['revalidation'] == 0
    assert result.body['report_ready']
    assert api.store.db.total_changes == before


def test_invalid_lane_and_cross_research_detail_are_blocked(app):
    configure(app)
    rid = create(app)
    invalid = app.request('GET', f'/api/control/research/{rid}/flow?lane=unknown')
    assert invalid.status == 409 and invalid.body['error'] == 'PAGE_INVALID'
    unknown = app.request('GET', '/api/control/research/unknown/screen')
    assert unknown.status == 409 and unknown.body['error'] == 'OPERATION_UNAVAILABLE'


def test_screen_excludes_internal_agent_payload(app, monkeypatch):
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    app.store.db.execute("INSERT INTO runtime_events(research_id,event_type,created_at,details_json) VALUES(?,?,?,?)",
                         (rid,'UI_FIXTURE','2026-10-05T00:00:00+00:00',json.dumps({'chain_of_thought':'private-canary-ui','messages':['private-canary-ui']})))
    result = json.dumps(screen_summary(app, rid), ensure_ascii=False)
    assert 'private-canary-ui' not in result
    assert 'chain_of_thought' not in result and 'messages' not in result


def test_tampered_report_is_not_current_success(app, monkeypatch):
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    path = app.read._state.workspace.path(rid, 'research_output/ai_report.json')
    assert path.is_file()
    path.write_bytes(path.read_bytes()+b'tampered')
    result = screen_summary(app, rid)
    assert not result['report_ready']
    assert result['currentness'] == 'STALE'
    assert result['official_status']['label'] == '재검증 필요'
    assert result['claims'] == []


def test_stale_state_is_consistent_in_list_and_screen(app, monkeypatch):
    gateway, calls, searches = rig(app, monkeypatch)
    rid = run(app, gateway)
    app.store.db.execute("UPDATE research_runs SET state_version=state_version+1 WHERE research_id=?", (rid,))
    result = screen_summary(app, rid)
    row = next(row for row in app.research_list() if row['research_id'] == rid)
    assert result['currentness'] == 'STALE'
    assert row['official_status'] == result['official_status']
    assert result['official_status']['label'] == '재검증 필요'


def test_legacy_report_tamper_is_visible(demo_api):
    api, a, b = demo_api
    path = api.read._state.workspace.path(b, 'research_output/final_report.md')
    original = path.read_bytes()
    try:
        path.write_bytes(original + b'tampered')
        result = screen_summary(api, b)
        assert not result['report_ready']
        assert result['currentness'] == 'STALE'
        assert result['official_status']['label'] == '재검증 필요'
    finally:
        path.write_bytes(original)
