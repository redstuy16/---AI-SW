"""저장된 연구 관계를 읽는 흐름도. 과학 상태와 실행 순서를 저장하지 않는다."""
from __future__ import annotations

from collections import deque
from hashlib import sha256
import json

from .control_plane import ControlError
from .database import to_json

LANES = {'manager': '연구 총괄', 'knowledge': '자료·근거', 'experiment': '실험·분석',
         'verification': '검증·복구', 'tools': '실행 도구'}
ROLES = {'manager': 'manager', 'knowledge_coordinator': 'knowledge', 'literature_worker': 'knowledge',
         'experiment_coordinator': 'experiment', 'analysis_planner_worker': 'experiment',
         'verification_coordinator': 'verification', 'critic': 'verification'}
TITLES = {'PROFILE': '자료 요약', 'DATA_PROFILE':'자료 요약', 'STATS': '분석 결과', 'FIGURE': '그림', 'FINAL_REPORT': '최종 보고서',
          'SKILL_PLAN': '분석 도구 계획', 'SKILL_RESULT': '분석 도구 결과', 'F3P_FAILURE_EVIDENCE': '검증 실패 증거',
          'F3P_REPAIR_DECISION': '복구 결정', 'F3P_REVALIDATION': '필수 재검증',
          'F3P_CHECKER_QUALIFICATION': '검증기 확인', 'F3P_REPAIR_REQUEST': '복구 요청'}
ACTIVE = {'RUNNING', 'VERIFYING', 'REPAIRING'}
ISSUES = {'FAILED', 'BLOCKED', 'NEEDS_REVIEW', 'INVALIDATED', 'VALIDATION_INCOMPLETE'}


def public_fields(value):
    excluded = {'input_text','system_instructions','developer_instructions','messages','headers','authorization','api_key',
                'password','secret','cot','chain_of_thought','reasoning_content','hidden_answer','oracle','golden_answer','private_prompt'}
    if isinstance(value,dict):
        return {k:public_fields(v) for k,v in value.items() if k.lower() not in excluded}
    if isinstance(value,list):
        return [public_fields(v) for v in value]
    return value


def display_status(value):
    return {'ISSUED': 'QUEUED', 'PENDING': 'QUEUED', 'WAITING_RETRY': 'QUEUED', 'STARTING': 'QUEUED',
            'WAITING_ESCALATION': 'NEEDS_REVIEW', 'NEEDS_SEMANTIC_REVIEW': 'NEEDS_REVIEW',
            'VERIFIED': 'COMPLETED', 'PASS': 'COMPLETED', 'COMMITTED': 'COMPLETED', 'FINISHED': 'COMPLETED', 'SUCCESS': 'COMPLETED',
            'FAIL': 'FAILED', 'ROLLED_BACK': 'FAILED', 'BUDGET_BLOCKED': 'BLOCKED',
            'NEEDS_RECONCILIATION': 'BLOCKED', 'PREFLIGHT_BLOCKED': 'BLOCKED',
            'PROPOSED': 'NEEDS_REVIEW', 'INCONCLUSIVE': 'NEEDS_REVIEW', 'UNRESOLVED_DISAGREEMENT': 'NEEDS_REVIEW',
            'CONFLICTED':'NEEDS_REVIEW','NOT_SUPPORTED':'NEEDS_REVIEW','NEEDS_REVALIDATION':'NEEDS_REVIEW',
            'UNRESOLVED': 'NEEDS_REVIEW', 'SUPPORTED': 'COMPLETED', 'IMPORTED': 'COMPLETED',
            'PROFILED': 'COMPLETED', 'ACTIVE': 'COMPLETED', 'EVIDENCE_EXTRACTED': 'COMPLETED',
            'RELEVANT': 'COMPLETED', 'INVALID': 'INVALIDATED', 'DISCOVERED': 'NEEDS_REVIEW',
            'REJECTED': 'FAILED', 'WEAKENED': 'NEEDS_REVIEW', 'SHORTLISTED': 'QUEUED'}.get(value, value or 'UNKNOWN')


def observed_control(api, rid, control):
    """실행 프로세스 상태를 조회에만 반영한다."""
    value = dict(control) if control else None
    if value and value['status'] in {'RUNNING','STARTING','RESUMING','PAUSE_REQUESTED','STOP_REQUESTED'} and value.get('pid'):
        from .control_plane import process_alive
        if not process_alive(value['pid']):
            uncertain = api.store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status IN ('DISPATCHED','UNRESOLVED')", (rid,)).fetchone()
            value['status'] = 'NEEDS_RECONCILIATION' if uncertain else 'PAUSED'
    return value


def project_flow(api, rid, *, view='current', limit=100, offset=0, selected=None, lane=None):
    requested_lane = lane
    if view not in {'current', 'all', 'recovery', 'verification'} or not 1 <= limit <= 150 or not 0 <= offset <= 100000:
        raise ControlError('PAGE_INVALID')
    if requested_lane is not None and requested_lane not in LANES:
        raise ControlError('PAGE_INVALID')
    db = api.store.db
    api.read._research(rid)
    run = dict(db.execute('SELECT * FROM research_runs WHERE research_id=?', (rid,)).fetchone())
    control_row = db.execute('SELECT status,version,pid FROM control_runs WHERE research_id=?', (rid,)).fetchone()
    control = observed_control(api, rid, control_row)
    nodes, edges, task_contracts = {}, {}, {}
    def key(kind, identity):
        return f'{kind}:{identity}'
    def add(kind, identity, title, status, lane, *, role=None, at=None, elapsed=None, cost=None, revision=None, level=0):
        identity = key(kind, identity)
        nodes[identity] = {'id': identity, 'kind': kind, 'ref': identity.split(':', 1)[1], 'title': str(title or kind)[:180],
                           'status': display_status(status), 'stored_status': status, 'lane': lane, 'role': role,
                           'timestamp': at, 'elapsed_ms': elapsed, 'cost_usd': cost, 'cost_status': 'UNKNOWN' if cost is None else 'ESTIMATED',
                           'revision': revision, 'history': 'invalidated' if status in {'INVALIDATED', 'INVALID'} else 'superseded' if status == 'SUPERSEDED' else 'current', 'level': level}
        return identity
    def edge(source, target, relation, *, stored_status='ACTIVE'):
        if source and target and source != target:
            identity = f'{source}>{relation}>{target}'
            edges[identity] = {'id': identity, 'source': source, 'target': target, 'relation': relation, 'stored_status': stored_status}
    root = add('research', rid, run['research_question'] or run['goal'], run['run_status'], 'manager')
    if run['run_status'] == 'ACTIVE':
        nodes[root]['status'] = 'RUNNING'
    if control:
        nodes[root]['status'] = display_status(control['status'])
    # 화면용 작은 필드만 읽는다. 요청 본문·프롬프트·파일 원문은 조회하지 않는다.
    contracts = list(db.execute("SELECT contract_id,task_id,status,json_extract(contract_json,'$.objective') AS objective,json_extract(contract_json,'$.assigned_role') AS role,json_extract(contract_json,'$.inputs') AS inputs,json_extract(contract_json,'$.task_type') AS task_type FROM contracts WHERE research_id=? ORDER BY rowid", (rid,)))
    for row in contracts:
        cid = add('contract', row['contract_id'], row['objective'], row['status'], ROLES.get(row['role'], 'experiment'), role=row['role'], level=1)
        task_contracts[row['task_id']] = cid
    from .research_design import current_design
    design = current_design(api.read._state, rid)
    if design:
        node = add('design', str(design['revision']), '이번 연구의 조건', 'NEEDS_REVIEW' if design['issues'] else 'COMPLETED', 'manager', revision=design['revision'])
        edge(root, node, 'uses')
        for row in contracts:
            binding = api.read._state.runtime_step(rid, 'research_design_contract:' + row['contract_id'])
            if binding and binding['output']['hash'] == design['hash']:
                edge(node, key('contract', row['contract_id']), 'constrains')
    for row in db.execute('SELECT task_id,parent_task_id FROM tasks WHERE research_id=?', (rid,)):
        edge(task_contracts.get(row['parent_task_id'], root), task_contracts.get(row['task_id']), 'depends_on')
    for row in contracts:
        for ref in json.loads(row['inputs'] or '[]'):
            edge(key(ref['type'], ref['id']), key('contract', row['contract_id']), 'uses')
    for row in db.execute('SELECT ar.agent_run_id,ar.contract_id,ar.actor_role,ar.status,ar.started_at,ar.latency_ms,ar.estimated_cost_usd FROM agent_runs ar JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=? AND ar.provider IS NOT NULL ORDER BY ar.rowid', (rid,)):
        node = add('agent_run', row['agent_run_id'], LANES.get(ROLES.get(row['actor_role']), row['actor_role']), row['status'], ROLES.get(row['actor_role'], 'experiment'), role=row['actor_role'], at=row['started_at'], elapsed=row['latency_ms'], cost=row['estimated_cost_usd'], level=2)
        edge(key('contract', row['contract_id']), node, 'produces')
    for row in db.execute("SELECT tc.request_id,ar.contract_id,tc.tool_name,tc.status,tc.started_at,tc.latency_ms,tc.estimated_cost_usd,json_extract(tc.result_json,'$.artifacts') AS artifacts,json_extract(tc.request_json,'$.args.dataset_id') AS dataset FROM tool_calls tc JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=? ORDER BY tc.rowid", (rid,)):
        node = add('tool', row['request_id'], row['tool_name'], row['status'], 'tools', at=row['started_at'], elapsed=row['latency_ms'], cost=row['estimated_cost_usd'], level=3)
        edge(key('contract', row['contract_id']), node, 'produces')
        if row['dataset']:
            edge(key('dataset', row['dataset']), node, 'uses')
        for ref in json.loads(row['artifacts'] or '[]'):
            edge(node, key('artifact', ref['id']), 'produces')
    for table, kind, identity, title, lane, level in (
        ('hypotheses', 'hypothesis', 'hypothesis_id', 'statement', 'manager', 1),
        ('datasets', 'dataset', 'dataset_id', 'original_name', 'knowledge', 1),
        ('sources', 'source', 'source_id', 'title', 'knowledge', 1),
        ('experiments', 'experiment', 'experiment_id', 'method', 'experiment', 3),
        ('evidence', 'evidence', 'evidence_id', 'claim', 'knowledge', 5)):
        suffix = " AND status<>'IRRELEVANT'" if kind == 'source' else ''
        for row in db.execute(f'SELECT {identity},{title},status FROM {table} WHERE research_id=?' + suffix + ' ORDER BY rowid', (rid,)):
            add(kind, row[identity], row[title] or row[identity], row['status'], lane, level=level)
    for row in db.execute('SELECT artifact_id,artifact_type,kind,contract_id,status,created_at FROM artifacts WHERE research_id=? ORDER BY rowid', (rid,)):
        kind = row['artifact_type'] or row['kind']
        lane = 'verification' if kind.startswith('F3P_') else 'tools'
        node = add('artifact', row['artifact_id'], TITLES.get(kind, kind), row['status'], lane, at=row['created_at'], level=4)
        if kind == 'F3P_REVALIDATION':
            nodes[node]['status'] = 'VALIDATION_INCOMPLETE'
        elif kind == 'F3P_FAILURE_EVIDENCE':
            nodes[node]['status'] = 'FAILED'
        elif kind == 'F3P_CHECKER_QUALIFICATION':
            nodes[node]['status'] = 'NEEDS_REVIEW'
        elif kind == 'F3P_REPAIR_DECISION':
            decision = api.read._state.runtime_step(rid,'repair_decision:'+row['contract_id'])
            nodes[node]['status'] = 'COMPLETED' if decision and decision['status']=='COMPLETED' else 'NEEDS_REVIEW'
            failure = db.execute("SELECT artifact_id FROM artifacts WHERE research_id=? AND contract_id=? AND artifact_type='F3P_FAILURE_EVIDENCE'",(rid,row['contract_id'])).fetchone()
            if failure:
                edge(key('artifact',failure[0]),node,'repairs')
        edge(key('contract', row['contract_id']), node, 'produces')
    for row in db.execute("SELECT experiment_id,task_id,dataset_id,result_artifact_id FROM experiments WHERE research_id=?", (rid,)):
        eid = key('experiment', row['experiment_id'])
        edge(task_contracts.get(row['task_id']), eid, 'produces')
        edge(key('dataset', row['dataset_id']), eid, 'uses') if row['dataset_id'] else None
        edge(eid, key('artifact', row['result_artifact_id']), 'produces') if row['result_artifact_id'] else None
    for row in db.execute('SELECT evidence_id,source_id,experiment_id,target_hypothesis_id,polarity FROM evidence WHERE research_id=?', (rid,)):
        eid = key('evidence', row['evidence_id'])
        if row['source_id']:
            edge(key('source', row['source_id']), eid, 'produces')
        if row['experiment_id']:
            edge(key('experiment', row['experiment_id']), eid, 'produces')
        if row['target_hypothesis_id']:
            relation = {'SUPPORT':'supports','support':'supports','SUPPORTS':'supports',
                        'CONTRADICT':'contradicts','contradict':'contradicts','CONTRADICTS':'contradicts','contradicts':'contradicts',
                        'QUALIFIES':'qualifies','qualifies':'qualifies'}.get(row['polarity'],'uses')
            edge(eid, key('hypothesis', row['target_hypothesis_id']), relation)
    for row in db.execute('SELECT from_type,from_id,to_type,to_id,edge_type,status FROM entity_edges WHERE research_id=?', (rid,)):
        relation = {'tests': 'uses', 'uses_dataset': 'uses', 'generated_from': 'produces', 'produced_by': 'produces'}.get(row['edge_type'], row['edge_type'])
        source, target = key(row['from_type'], row['from_id']), key(row['to_type'], row['to_id'])
        if row['edge_type'] in {'uses_dataset', 'tests', 'generated_from', 'produced_by'} or row['from_type'] == 'hypothesis' and row['to_type'] == 'evidence':
            source, target = target, source
        edge(source, target, relation, stored_status=row['status'])
    verification_contracts = {}
    for row in db.execute("SELECT sm.mutation_id,sm.contract_id,sm.status,sm.base_state_version,sm.created_at,json_extract(sm.verification_json,'$.verdict') AS verdict,json_extract(sm.payload_json,'$.scientific.experiment_id') AS experiment,json_extract(sm.payload_json,'$.agent_result.provenance.analysis_revision') AS revision,e.status AS experiment_status FROM staged_mutations sm LEFT JOIN experiments e ON e.experiment_id=json_extract(sm.payload_json,'$.scientific.experiment_id') WHERE sm.research_id=? AND sm.verification_json IS NOT NULL ORDER BY sm.rowid", (rid,)):
        status = 'FAILED' if row['verdict'] != 'PASS' or row['experiment_status'] == 'INVALIDATED' or row['status'] == 'ROLLED_BACK' else 'COMPLETED' if row['status'] == 'COMMITTED' else 'VERIFYING'
        node = add('verification', row['mutation_id'], '원본 검증' if not row['revision'] else '수정본 검증', status, 'verification', at=row['created_at'], revision=row['revision'] or 0, level=5)
        verification_contracts.setdefault(row['contract_id'], []).append(node)
        edge(key('contract', row['contract_id']), node, 'verifies')
        if row['experiment']:
            edge(key('experiment', row['experiment']), node, 'verifies')
            for ev in db.execute('SELECT evidence_id FROM evidence WHERE research_id=? AND experiment_id=?', (rid, row['experiment'])):
                edge(node, key('evidence', ev[0]), 'produces')
    for row in db.execute("SELECT step_key,contract_id,json_extract(output_json,'$.failure_id') AS failure,json_extract(output_json,'$.contract_id') AS parent,json_extract(output_json,'$.revision') AS revision FROM runtime_steps WHERE research_id=? AND (step_key LIKE 'repair_failure:%' OR step_key LIKE 'repair_parent:%')", (rid,)):
        if row['failure']:
            for verification in verification_contracts.get(row['contract_id'], []):
                edge(verification, key('artifact', row['failure']), 'produces')
        if row['parent']:
            old, new = key('contract', row['parent']), key('contract', row['contract_id'])
            if old in nodes:
                nodes[old]['history'] = 'superseded'
            for artifact in db.execute('SELECT artifact_id FROM artifacts WHERE research_id=? AND contract_id=?',(rid,row['parent'])):
                if key('artifact',artifact[0]) in nodes:
                    nodes[key('artifact',artifact[0])]['history'] = 'historic'
            for verification in verification_contracts.get(row['parent'],[]):
                nodes[verification]['history'] = 'historic'
            if new in nodes:
                nodes[new]['revision'] = row['revision']
                nodes[new]['status'] = 'REPAIRING' if nodes[new]['status'] == 'RUNNING' else nodes[new]['status']
            edge(old, new, 'supersedes')
            for artifact in db.execute("SELECT artifact_id,artifact_type FROM artifacts WHERE research_id=? AND contract_id=? AND artifact_type IN ('F3P_FAILURE_EVIDENCE','F3P_REPAIR_DECISION')", (rid, row['parent'])):
                edge(key('artifact', artifact['artifact_id']), new, 'repairs')
            for artifact in db.execute("SELECT artifact_id FROM artifacts WHERE research_id=? AND contract_id=? AND artifact_type='F3P_REVALIDATION'", (rid, row['contract_id'])):
                art = key('artifact', artifact[0])
                required = api.read._state.runtime_step(rid, 'repair_parent:' + row['contract_id'])['output'].get('required_checks', [])
                checks = db.execute("SELECT sm.status,sm.verification_json FROM staged_mutations sm WHERE research_id=? AND contract_id=? ORDER BY rowid DESC LIMIT 1", (rid, row['contract_id'])).fetchone()
                if checks:
                    checked = json.loads(checks['verification_json'] or '{}')
                    passed = {c['check_id'] for c in checked.get('checks', []) if c.get('passed')}
                    nodes[art]['status'] = 'COMPLETED' if checks['status'] == 'COMMITTED' and checked.get('verdict') == 'PASS' and required and set(required) <= passed else 'VALIDATION_INCOMPLETE'
                for verification in verification_contracts.get(row['contract_id'], []):
                    edge(verification, art, 'verifies')
    terminal = {'REPAIR_NOT_STARTED_BUDGET':('복구 예산 부족','BLOCKED'),
                'REPAIR_UNRESOLVED':('복구 검토 필요','NEEDS_REVIEW'),
                'REPAIR_INCOMPLETE':('복구 미완료','VALIDATION_INCOMPLETE'),
                'CHECKER_QUALIFICATION_FAILED':('검증기 확인 실패','NEEDS_REVIEW'),
                'REPAIR_CONTRACT_MUTATION_BLOCKED':('승인 범위 변경 차단','BLOCKED')}
    error_events, current_error = [], None
    for row in db.execute("SELECT rowid AS identity,event_type,created_at,json_extract(details_json,'$.failure_id') AS failure FROM runtime_events WHERE research_id=? AND event_type IN ("+','.join('?' for _ in terminal)+") ORDER BY rowid",(rid,*terminal)):
        title,status = terminal[row['event_type']]
        node = add('runtime_event',str(row['identity']),title,status,'verification',at=row['created_at'],level=5)
        # 작업 참조가 없는 예산 기록은 연구 범위에서만 연결한다.
        edge(key('artifact',row['failure']) if row['failure'] else root,node,'repairs' if row['failure'] else 'produces')
        error_events.append(row)
    if error_events:
        last = error_events[-1]
        committed = db.execute('SELECT MAX(created_at) FROM state_events WHERE research_id=?',(rid,)).fetchone()[0]
        if not committed or last['created_at'] > committed:
            current_error = key('runtime_event', str(last['identity']))
            status = terminal[last['event_type']][1]
            for node in nodes.values():
                if node['status'] in ACTIVE and (not node['timestamp'] or node['timestamp']<=last['created_at']):
                    node['status'] = status
    for row in db.execute('SELECT decision_id,status,created_at,state_version FROM decisions WHERE research_id=? ORDER BY rowid', (rid,)):
        add('decision', row['decision_id'], '연구 결정', row['status'], 'manager', at=row['created_at'], revision=row['state_version'], level=6)
    for row in db.execute('SELECT checkpoint_id,reason,state_version,created_at FROM checkpoints WHERE research_id=? ORDER BY rowid', (rid,)):
        add('checkpoint', row['checkpoint_id'], row['reason'], 'COMPLETED', 'manager', at=row['created_at'], revision=row['state_version'], level=6)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='claim_revisions'").fetchone():
        for row in db.execute("SELECT claim_id,revision,current,json_extract(payload_json,'$.text') AS text,json_extract(payload_json,'$.support_state') AS state FROM claim_revisions WHERE research_id=? ORDER BY claim_id,revision", (rid,)):
            node = add('claim', f"{row['claim_id']}@{row['revision']}", row['text'], row['state'], 'manager', revision=row['revision'], level=7)
            if not row['current']:
                nodes[node]['history'] = 'superseded'
            old = key('claim', f"{row['claim_id']}@{row['revision']-1}")
            if old in nodes:
                edge(old, node, 'supersedes')
        for row in db.execute("SELECT claim_id,claim_revision,json_extract(payload_json,'$.evidence_kind') AS kind,json_extract(payload_json,'$.target_id') AS target,json_extract(payload_json,'$.relation') AS relation,json_extract(payload_json,'$.status') AS status FROM claim_bindings WHERE research_id=?", (rid,)):
            edge(key(row['kind'], row['target']), key('claim', f"{row['claim_id']}@{row['claim_revision']}"), row['relation'].lower(), stored_status=row['status'])
    conclusion = json.loads(run['conclusion_json'] or '{}')
    if conclusion:
        claim = add('conclusion', rid, conclusion.get('statement', '현재 결론'), run['run_status'], 'manager', level=7)
        for ref in conclusion.get('evidence_refs', []):
            edge(key(ref['type'], ref['id']), claim, 'supports')
        for ref in conclusion.get('limitation_refs', []):
            edge(key(ref['type'], ref['id']), claim, 'qualifies')
        for row in db.execute("SELECT artifact_id FROM artifacts WHERE research_id=? AND artifact_type='FINAL_REPORT'", (rid,)):
            edge(claim, key('artifact', row[0]), 'produces')
    try:
        report_path = api.read.workspace.path(rid,'research_output/final_report.md')
        if report_path.is_file():
            report = add('report',rid,'최종 보고서','AVAILABLE','manager',level=8)
            if conclusion:
                edge(key('conclusion',rid),report,'produces')
    except Exception:
        pass
    from .resource_queue import ResourcePool
    for job in ResourcePool(api.store).summary(rid):
        if job['status'] not in {'QUEUED', 'RUNNING'}:
            continue
        title = '자원 대기' if job['status']=='QUEUED' else '분석 실행' if job['resource']=='heavy-analysis' else '요청 처리'
        node = add('resource', job['id'], title, job['status'], 'tools' if job['resource']=='heavy-analysis' else ROLES.get(job['role'], 'tools'), role=job['role'], at=job['started_at'] or job['created_at'], level=2)
        nodes[node]['ahead_count'] = job['ahead_count']
    edges = {k: e for k, e in edges.items() if e['source'] in nodes and e['target'] in nodes}
    if control and control['status'] in {'PAUSED','BUDGET_BLOCKED','NEEDS_RECONCILIATION','NEEDS_SEMANTIC_REVIEW','FAILED','STOPPED'}:
        for node in nodes.values():
            if node['status'] in ACTIVE:
                node['status'] = display_status(control['status'])
    # 생산·검증 방향만 배치에 사용한다. 반증 관계는 진행 순서를 바꾸지 않는다.
    forward = [e for e in edges.values() if e['relation'] in {'depends_on', 'produces', 'verifies', 'repairs', 'supersedes'}]
    for _ in range(min(12, len(nodes))):
        changed = False
        for e in forward:
            a, b = nodes[e['source']], nodes[e['target']]
            value = min(24, a['level'] + 1)
            if b['level'] < value:
                b['level'] = value
                changed = True
        if not changed:
            break
    ancestors, neighbors = {}, {}
    for e in edges.values():
        ancestors.setdefault(e['target'], set()).add(e['source'])
        neighbors.setdefault(e['source'], set()).add(e['target'])
        neighbors.setdefault(e['target'], set()).add(e['source'])
    def closure(seeds):
        reached, queue = set(seeds), deque(seeds)
        while queue:
            for parent in ancestors.get(queue.popleft(), ()):
                if parent not in reached:
                    reached.add(parent)
                    queue.append(parent)
        return reached
    active = {k for k, n in nodes.items() if n['status'] in ACTIVE and n['kind'] in {'contract','agent_run','tool','resource'}}
    issues = {k for k, n in nodes.items() if n['status'] in ISSUES}
    accepted = {k for k,n in nodes.items() if n['kind']=='evidence' and n['status']=='COMPLETED' and n['history']=='current'}
    seeds = active or ({current_error} if current_error else {key('conclusion', rid)} if conclusion else accepted or issues)
    path = closure(seeds)
    if selected and selected not in nodes:
        raise ControlError('NOT_FOUND')
    selected_neighbors = ({selected} | neighbors.get(selected, set())) if selected else set()
    visible = set(nodes) if view == 'all' else closure(issues | {k for k,n in nodes.items() if n['title'] in TITLES.values() and n['lane'] == 'verification'}) if view == 'recovery' else closure({k for k,n in nodes.items() if n['lane'] == 'verification'}) if view == 'verification' else path or {root}
    visible |= selected_neighbors
    if requested_lane:
        visible = {k for k in visible if nodes[k]['lane'] == requested_lane}
    ordered = sorted(visible, key=lambda k: (nodes[k]['level'], list(LANES).index(nodes[k]['lane']), k))
    page = set(ordered[offset:offset + limit])
    # 완료된 복구 자료의 무결성은 표시할 때도 실제 파일로 확인한다.
    from .storage import sha256_file, UnsafeWorkspacePathError
    integrity = {}
    def intact(artifact):
        if artifact['artifact_id'] not in integrity:
            try:
                file = api.read.workspace.path(rid,artifact['relative_path'])
                integrity[artifact['artifact_id']] = sha256_file(file)==artifact['sha256']
            except (OSError,ValueError,UnsafeWorkspacePathError):
                integrity[artifact['artifact_id']] = False
        return integrity[artifact['artifact_id']]
    for identity in page:
        node = nodes[identity]
        if node['kind'] == 'artifact' and node['lane'] == 'verification':
            artifact = db.execute('SELECT artifact_id,artifact_type,contract_id,relative_path,sha256 FROM artifacts WHERE research_id=? AND artifact_id=?', (rid, node['ref'])).fetchone()
            try:
                artifact_path = api.read.workspace.path(rid, artifact['relative_path'])
                if not intact(artifact):
                    raise ValueError('ARTIFACT_INTEGRITY_FAILED')
                if artifact['artifact_type']=='F3P_CHECKER_QUALIFICATION':
                    if artifact_path.stat().st_size>2_000_000:
                        raise ValueError('ARTIFACT_SIZE_LIMIT')
                    qualification = json.loads(artifact_path.read_text(encoding='utf-8',errors='strict')).get('status')
                    node['qualification_status'] = qualification
                    node['status'] = 'COMPLETED' if qualification=='ANALYSIS_CHECK_FAILED' else 'NEEDS_REVIEW'
                if artifact['artifact_type']=='F3P_REVALIDATION' and node['status']=='COMPLETED':
                    parent = api.read._state.runtime_step(rid,'repair_parent:'+artifact['contract_id'])
                    related = {artifact['contract_id'],parent['output']['contract_id'] if parent else artifact['contract_id']}
                    placeholders = ','.join('?' for _ in related)
                    for related_artifact in db.execute("SELECT artifact_id,relative_path,sha256 FROM artifacts WHERE research_id=? AND contract_id IN ("+placeholders+") AND artifact_type LIKE 'F3P_%'",(rid,*sorted(related))):
                        if not intact(related_artifact):
                            raise ValueError('REPAIR_DEPENDENCY_INTEGRITY_FAILED')
            except (OSError, ValueError, UnsafeWorkspacePathError):
                node['status'] = 'NEEDS_REVIEW'
                node['integrity'] = 'INVALID'
    for n in nodes.values():
        n['on_current_path'], n['selected_neighbor'] = n['id'] in path, n['id'] in selected_neighbors
    structure = sha256(to_json({'nodes': [(n['id'], n['lane'], n['level']) for n in nodes.values() if n['id'] in page], 'edges': [e['id'] for e in edges.values() if e['source'] in page and e['target'] in page]}).encode('utf-8', errors='strict')).hexdigest()
    return {'research_id': rid, 'state_version': run['state_version'], 'status': run['run_status'], 'stop_reason': run['stop_reason'],
            'control_status':control['status'] if control else run['run_status'], 'control_version':control['version'] if control else None,
            'lanes': LANES, 'lane_order':[requested_lane] if requested_lane else list(LANES), 'view': view, 'lane': requested_lane, 'layout_revision': structure, 'nodes': [nodes[k] for k in ordered[offset:offset + limit]],
            'edges': [e for e in edges.values() if e['source'] in page and e['target'] in page], 'total_nodes': len(nodes),
            'visible_total': len(ordered), 'boundary_edges': sum((e['source'] in page) != (e['target'] in page) for e in edges.values()),
            'offset': offset, 'limit': limit, 'next_offset': offset + limit if len(ordered) > offset + limit else None,
            'current_ids': sorted(active), 'counts': {'running': len(active), 'queued': sum(n['status'] == 'QUEUED' and n['kind'] in {'contract','resource'} and n['history']=='current' for n in nodes.values()), 'review': sum(n['status'] in ISSUES and n['history']=='current' for n in nodes.values())},
            'irrelevant_sources': db.execute("SELECT COUNT(*) FROM sources WHERE research_id=? AND status='IRRELEVANT'", (rid,)).fetchone()[0],
            'plan_version': None, 'plan_progress': None, 'position_semantics': 'LOGICAL_DEPENDENCY', 'read_only': True}


def flow_node(api, rid, identity):
    """선택한 객체의 허용 필드만 조회한다. Agent 입력과 내부 사고 기록은 제외한다."""
    api.read._research(rid)
    if ':' not in identity:
        raise ControlError('NOT_FOUND')
    kind, ref = identity.split(':', 1)
    db, state = api.store.db, api.read._state
    definitions = {'research':('research_runs','research_id'), 'contract':('contracts','contract_id'),
                   'hypothesis':('hypotheses','hypothesis_id'), 'dataset':('datasets','dataset_id'),
                   'source':('sources','source_id'), 'experiment':('experiments','experiment_id'),
                   'evidence':('evidence','evidence_id'), 'artifact':('artifacts','artifact_id'),
                   'verification':('staged_mutations','mutation_id'), 'decision':('decisions','decision_id'),
                   'checkpoint':('checkpoints','checkpoint_id')}
    if kind in definitions:
        table, column = definitions[kind]
        row = db.execute(f'SELECT * FROM {table} WHERE research_id=? AND {column}=?', (rid, ref)).fetchone()
    elif kind == 'agent_run':
        row = db.execute('SELECT ar.* FROM agent_runs ar JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=? AND ar.agent_run_id=?', (rid,ref)).fetchone()
    elif kind == 'tool':
        row = db.execute('SELECT tc.*,ar.contract_id FROM tool_calls tc JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=? AND tc.request_id=?', (rid,ref)).fetchone()
    elif kind == 'conclusion' and ref == rid:
        row = db.execute('SELECT * FROM research_runs WHERE research_id=?', (rid,)).fetchone()
    elif kind == 'report' and ref == rid:
        from .dashboard import project_report
        record = project_report(state,rid)
        return {'title':'최종 보고서','summary':{'상태':'저장된 보고서' if record.available else '보고서 검증 필요'},
                'detail':{'상대 경로':record.relative_path,'보고서':record.content if record.available else '기존 보고서 검증을 통과하지 못했습니다.'},'artifacts':[]}
    elif kind == 'resource':
        from .resource_queue import ResourcePool
        row = next((j for j in ResourcePool(api.store).summary(rid) if j['id'] == ref),None)
    elif kind == 'runtime_event':
        row = db.execute('SELECT * FROM runtime_events WHERE research_id=? AND rowid=?',(rid,ref)).fetchone()
    elif kind == 'claim' and db.execute("SELECT 1 FROM sqlite_master WHERE name='claim_revisions'").fetchone() and '@' in ref:
        claim, revision = ref.rsplit('@',1)
        row = db.execute('SELECT * FROM claim_revisions WHERE research_id=? AND claim_id=? AND revision=?', (rid,claim,revision)).fetchone()
    else:
        row = None
    if row is None:
        raise ControlError('NOT_FOUND')
    value = dict(row)
    result = {'title':ref,'summary':{'항목':ref,'저장된 상태':value.get('status',value.get('run_status','정보 없음'))},'detail':{},'artifacts':[]}
    detail = result['detail']
    if kind in {'research','conclusion'}:
        result['title'] = '연구 질문' if kind == 'research' else '현재 결론'
        detail.update({'연구 질문':value['research_question'] or value['goal'],'상태 버전':value['state_version'],'종료 사유':value['stop_reason']})
        if kind == 'conclusion':
            conclusion = json.loads(value['conclusion_json'] or '{}')
            detail.update({'결론':conclusion.get('statement'),'근거':conclusion.get('evidence_refs',[]),'제한':conclusion.get('limitation_refs',[]),'미해결 질문':conclusion.get('unresolved_questions',[])})
    elif kind == 'contract':
        contract = json.loads(value['contract_json'])
        result['title'] = contract['objective']
        detail.update({'담당 역할':LANES.get(ROLES.get(contract['assigned_role']),contract['assigned_role']),
                       '입력 참조':contract['inputs'],'허용 도구':contract['allowed_tools'],
                       '필수 검사':contract['acceptance'],'제약':contract['constraints']})
    elif kind == 'evidence':
        from .dashboard import project_evidence
        record = project_evidence(state,rid,evidence_id=ref)[0].model_dump(mode='json')
        result['title'] = record['claim'];detail.update(record)
    elif kind == 'experiment':
        from .dashboard import project_experiments, _stats_payload
        record = project_experiments(state,rid,experiment_id=ref)[0].model_dump(mode='json')
        result['title'] = record['method'] or ref
        detail.update(record)
        detail['측정값'] = _stats_payload(state,rid,value['result_artifact_id'])
    elif kind == 'verification':
        from .dashboard import project_verification
        detail['검사 결과'] = [v.model_dump(mode='json') for v in project_verification(state,rid,subject_id=ref)]
        detail['상태 버전'] = value['base_state_version']
    elif kind == 'artifact':
        result['title'] = TITLES.get(value['artifact_type'], value['artifact_type'])
        detail.update({'유형':value['artifact_type'],'크기':value['size_bytes'],'상대 경로':value['relative_path'],'해시':value['sha256']})
        try:
            from .storage import sha256_file
            path = api.read.workspace.path(rid,value['relative_path'])
            detail['무결성'] = 'VALID' if sha256_file(path) == value['sha256'] else 'INVALID'
        except Exception:
            detail['무결성'] = 'INVALID'
        if detail['무결성']=='VALID' and value['artifact_type'] in {'SKILL_PLAN','SKILL_RESULT'} and path.stat().st_size<=2_000_000:
            try:
                detail['분석 도구 기록'] = public_fields(json.loads(path.read_text(encoding='utf-8',errors='strict')))
            except (ValueError,UnicodeError,OSError):
                detail['분석 도구 기록'] = '기록 형식 확인 필요'
        if detail['무결성']=='VALID' and value['status'] not in {'INVALIDATED','SUPERSEDED'} and (value['artifact_type'] or '').upper() in {'FIGURE','FIGURE_PNG','PNG'} and path.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}:
            result['preview_url'] = f'/api/research/{rid}/artifacts/{ref}/preview'
    elif kind == 'dataset':
        result['title'] = value['original_name']
        detail.update({'행 수':value['row_count'],'열 수':value['column_count'],'크기':value['size_bytes'],
                       '스키마':json.loads(value['schema_json'] or '{}'),'해시':value['sha256']})
        try:
            import csv
            from itertools import islice
            from .storage import sha256_file
            path = api.read.workspace.path(rid,value['stored_path'])
            if sha256_file(path) != value['sha256']:
                raise ValueError('DATASET_INTEGRITY_FAILED')
            with path.open(encoding='utf-8',newline='') as stream:
                detail['미리보기'] = list(islice(csv.DictReader(stream),5))
        except Exception:
            detail['미리보기'] = '자료 무결성 확인 필요'
    elif kind == 'source':
        result['title'] = value['title']
        detail.update({k:value[k] for k in ('title','doi','openalex_id','url','publication_year','source_name','retrieved_at','metadata_hash')})
        detail['authors'] = json.loads(value['authors_json'])
        detail['abstract'] = value.get('abstract') or '초록 없음 · 서지정보만 확보'
    elif kind == 'tool':
        result['title'] = value['tool_name']
        record = json.loads(value['result_json'])
        detail.update({'도구':value['tool_name'],'시작':value['started_at'],'완료':value['finished_at'],
                       '시간 (ms)':value['latency_ms'],'산출물 참조':record.get('artifacts',[]),'오류 코드':record.get('error')})
    elif kind == 'agent_run':
        detail.update({k:value[k] for k in ('actor_role','provider','model','started_at','finished_at','latency_ms','input_tokens','output_tokens','estimated_cost_usd','base_state_version')})
    elif kind == 'checkpoint':
        detail.update({'저장 사유':value['reason'],'상태 버전':value['state_version'],'저장 시각':value['created_at']})
    elif kind == 'decision':
        detail.update({'결정':json.loads(value['payload_json']),'상태 버전':value['state_version'],'시각':value['created_at']})
    elif kind == 'claim':
        record = json.loads(value['payload_json'])
        result['title'] = record['text'];detail.update({k:record.get(k) for k in ('claim_id','revision','claim_type','support_state','scope','evidence_binding_ids','invalidated_by')})
    elif kind == 'resource':
        detail.update({'담당 역할':value['role'],'자원':value['resource'],'앞선 작업 수':value['ahead_count'],'대기 시작':value['created_at'],'실행 시작':value['started_at'],'사유':value['reason']})
    elif kind == 'runtime_event':
        from .resource_policy import activity_detail
        detail.update({'사유':value['event_type'],'시각':value['created_at'],'저장된 기록':activity_detail(state,rid,'runtime:'+ref)})
    contract = value.get('contract_id')
    if contract:
        result['artifacts'] = [{'id':r[0],'type':r[1],'status':r[2]} for r in db.execute('SELECT artifact_id,artifact_type,status FROM artifacts WHERE research_id=? AND contract_id=? ORDER BY rowid LIMIT 50', (rid,contract))]
        if db.execute("SELECT 1 FROM artifacts WHERE research_id=? AND contract_id=? AND artifact_type LIKE 'F3P_%'", (rid,contract)).fetchone():
            projection = api.repair_projection(rid)
            chain = next((c for c in projection['chains'] if contract in {c['failure']['contract_id'],c['new_revision']}),None)
            if chain:
                failure = chain['failure']['data']
                plan = state.runtime_step(rid,'worker_plan:'+chain['failure']['contract_id'])
                frozen = plan['output'] if plan else {}
                result['repair_contract'] = {'계약':chain['failure']['contract_id'],'원본 수정본':failure.get('analysis_revision'),
                    '방법':frozen.get('method'),'자료':frozen.get('dataset_id'),'변수':frozen.get('selected_variables'),
                    '수정 허용 범위':failure.get('allowed_repair_scope'),'바꿀 수 없는 항목':failure.get('forbidden_semantic_changes')}
                result['analysis_result'] = {'관측값':failure.get('observed_value'),'실패 증거':failure.get('failure_id'),
                    '새 수정본':chain['new_revision'],'복구 시도':chain['revision_detail'].get('repair_attempt'),
                    '복구 예산':failure.get('budget_required_for_recovery'),'사용 가능 예산':failure.get('budget_available')}
                result['verifier_result'] = {'검사 도구':failure.get('checker_identity'),'검사 도구 확인':failure.get('checker_qualification_status'),
                    '실패 검사':failure.get('check_ids'),'필수 검사':chain['required_checks'],'완료 검사':chain['completed_checks'],
                    '남은 검사':chain['remaining_checks'],'최종 상태':chain['final_disposition']}
                result['summary']['검증 상태'] = chain['final_disposition']
                result['summary']['파일 기록 상태'] = result['summary'].pop('저장된 상태')
    return public_fields(result)
