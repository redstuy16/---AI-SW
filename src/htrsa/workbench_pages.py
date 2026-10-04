"""작업대의 목록·요약 조회. 세부 검증은 선택한 항목에서 수행한다."""
from __future__ import annotations

import json

from .control_plane import ControlError


def research_list(api):
    db = api.store.db
    def grouped(query):
        return {r['research_id']: dict(r) for r in db.execute(query)}
    controls = grouped('SELECT * FROM control_runs')
    evidence = grouped("SELECT research_id,COUNT(*) AS n FROM evidence WHERE status='VERIFIED' GROUP BY research_id")
    experiments = grouped("SELECT research_id,COUNT(*) AS n FROM experiments WHERE status='VERIFIED' GROUP BY research_id")
    actions = grouped('SELECT research_id,COUNT(*) AS n FROM research_actions GROUP BY research_id')
    agents = grouped("SELECT research_id,COUNT(*) AS n,SUM(estimated_cost_usd) AS cost,SUM(provider NOT IN ('fake','scholarly.fake')) AS real,SUM(provider='scholarly.fake') AS cached FROM agent_runs WHERE provider IS NOT NULL GROUP BY research_id")
    costs = grouped("SELECT research_id,COUNT(*) AS n,COALESCE(SUM(CASE WHEN status='SETTLED' THEN settled ELSE 0 END),0) AS spent,COALESCE(SUM(CASE WHEN status='UNRESOLVED' THEN reserved ELSE 0 END),0) AS unresolved,COALESCE(SUM(CASE WHEN status NOT IN ('SETTLED','UNRESOLVED','RELEASED') THEN reserved ELSE 0 END),0) AS reserved FROM spend_ledger GROUP BY research_id")
    events = grouped('SELECT r.research_id,r.event_type,r.created_at FROM runtime_events r JOIN (SELECT research_id,MAX(seq) AS seq FROM runtime_events GROUP BY research_id) m ON r.seq=m.seq')
    cursors = grouped('SELECT research_id,cursor_json FROM research_runtime_state')
    last_actions = grouped('SELECT a.research_id,a.action_type FROM research_actions a JOIN (SELECT research_id,MAX(action_index) AS n FROM research_actions GROUP BY research_id) m ON a.research_id=m.research_id AND a.action_index=m.n')
    recovered = grouped("SELECT research_id,1 AS recovered FROM runtime_events WHERE event_type IN ('RUNTIME_RECONCILED','RECOVERY_COMPLETED','COMMITTED_ACTION_RECONCILED') GROUP BY research_id")
    contracts, failures = {}, {r[0]:r[1] for r in db.execute("SELECT contract_id,COUNT(*) FROM agent_runs WHERE status='FAILED' GROUP BY contract_id")}
    for row in db.execute("SELECT * FROM contracts ORDER BY CASE WHEN status IN ('ISSUED','RUNNING','WAITING_RETRY','WAITING_ESCALATION') THEN 0 ELSE 1 END,rowid DESC"):
        contracts.setdefault(row['research_id'], row)
    output = []
    from .research_lifecycle import lifecycle
    for raw in db.execute('SELECT * FROM research_runs ORDER BY created_at DESC'):
        rid = raw['research_id'];control = controls.get(rid);snapshot = json.loads(control['snapshot']) if control else {}
        event = events.get(rid, {});actor = agents.get(rid, {});contract = contracts.get(rid);detail = None
        stage = json.loads(cursors[rid]['cursor_json']).get('stage') if rid in cursors else None
        stage = stage or last_actions.get(rid, {}).get('action_type') or event.get('event_type') or 'START'
        if contract:
            value = json.loads(contract['contract_json']);constraints = value.get('constraints') or {}
            detail = {'contract_id':contract['contract_id'],'task_id':contract['task_id'],'status':contract['status'],
                      'agent_role':value.get('assigned_role'),'objective':value.get('objective'),'allowed_tools':value.get('allowed_tools',[]),
                      'remaining_retries':max(0,int(constraints.get('max_retries',0))-failures.get(contract['contract_id'],0)),
                      'acceptance_criteria':value.get('acceptance',[]),'constraints':constraints}
        mode = api.mode or ('DEMO' if actor.get('n') and not actor.get('real') else 'OFFLINE-CACHED' if actor.get('cached') else 'LIVE')
        overview = {'research_id':rid,'question':raw['research_question'] or raw['goal'],'status':raw['run_status'],'current_stage':stage,
                    'current_agent':detail['agent_role'] if detail else None,'current_contract_id':contract['contract_id'] if contract else None,
                    'current_contract':detail,'state_version':raw['state_version'],'stop_reason':raw['stop_reason'],
                    'action_count':actions.get(rid,{}).get('n',0),'verified_evidence_count':evidence.get(rid,{}).get('n',0),
                    'verified_experiment_count':experiments.get(rid,{}).get('n',0),'api_calls':actor.get('n',0),
                    'estimated_cost_usd':actor.get('cost'),'mode':mode,'resume_recovered':rid in recovered,
                    'resume_status':'RECOVERED' if rid in recovered else 'NOT_RECOVERED'}
        from .control_plane import money
        cost = costs.get(rid, {})
        overview['cost'] = {'basis': 'LEDGER' if cost or control else 'AGENT_ESTIMATE',
                            'spent': money(cost.get('spent', 0)) if cost or control else actor.get('cost'),
                            'reserved': money(cost.get('reserved', 0)), 'unresolved': money(cost.get('unresolved', 0))}
        life = lifecycle(api, rid)
        output.append({**overview,'title':life.get('title') or (control['title'] if control else overview['question']),'control_status':control['status'] if control else overview['status'],
                       'control_version':control['version'] if control else None,'research_depth':snapshot.get('research_depth'),
                       'last_action':event.get('event_type',stage),'updated_at':life.get('updated_at') or event.get('created_at',raw['created_at']),'controlled':bool(control),
                       'lifecycle':life})
    return output


def item_page(api, rid, kind, *, limit=50, offset=0):
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ControlError('PAGE_INVALID')
    definitions = {
        'evidence': ("SELECT e.evidence_id,e.claim,e.status,e.polarity,e.source_ref,e.source_id,e.experiment_id,e.limitations_json,s.title AS source_title FROM evidence e LEFT JOIN sources s ON s.source_id=e.source_id WHERE e.research_id=? ORDER BY e.rowid", 'evidence'),
        'experiments': ("SELECT e.experiment_id,e.method,e.status,e.dataset_id,d.row_count AS sample_size FROM experiments e LEFT JOIN datasets d ON d.dataset_id=e.dataset_id WHERE e.research_id=? ORDER BY e.rowid", 'experiments'),
        'verification': ("SELECT mutation_id AS item_id,COALESCE(json_extract(payload_json,'$.scientific.experiment_id'),mutation_id) AS subject_id,status,json_extract(verification_json,'$.verdict') AS verdict,COALESCE(json_array_length(verification_json,'$.checks'),0) AS checks_total,(SELECT COUNT(*) FROM json_each(verification_json,'$.checks') WHERE json_extract(value,'$.passed')=1) AS checks_passed FROM staged_mutations WHERE research_id=? AND verification_json IS NOT NULL ORDER BY rowid", 'staged_mutations')}
    if kind not in definitions:
        raise ControlError('NOT_FOUND')
    query, table = definitions[kind]
    params = (rid,)
    if kind == 'verification':
        query = "SELECT * FROM (SELECT mutation_id AS item_id,COALESCE(json_extract(payload_json,'$.scientific.experiment_id'),mutation_id) AS subject_id,status,json_extract(verification_json,'$.verdict') AS verdict,COALESCE(json_array_length(verification_json,'$.checks'),0) AS checks_total,(SELECT COUNT(*) FROM json_each(verification_json,'$.checks') WHERE json_extract(value,'$.passed')=1) AS checks_passed,'verification' AS node_kind,0 AS section,rowid AS ordering FROM staged_mutations WHERE research_id=? AND verification_json IS NOT NULL UNION ALL SELECT evidence_id,evidence_id,status,CASE WHEN status='INVALIDATED' OR json_extract(verification_json,'$.passed')<>1 THEN 'FAIL' ELSE 'PASS' END,1,COALESCE(json_extract(verification_json,'$.passed'),0),'evidence',1,rowid FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND verification_json IS NOT NULL) ORDER BY section,ordering"
        params = (rid,rid)
    rows = [dict(r) for r in api.store.db.execute(query+' LIMIT ? OFFSET ?', (*params,limit+1,offset))]
    for row in rows:
        if kind == 'evidence':
            row['limitations'] = json.loads(row.pop('limitations_json') or '[]')
        elif kind == 'experiments':
            row.update(dataset={'dataset_id':row.pop('dataset_id')},measured_values=None)
        elif kind == 'verification':
            invalid = api.store.db.execute("SELECT 1 FROM experiments WHERE research_id=? AND experiment_id=? AND status='INVALIDATED'", (rid,row['subject_id'])).fetchone()
            if invalid or row['status']=='ROLLED_BACK':
                row['verdict'] = 'FAIL'
    return {'items':rows[:limit],'offset':offset,'limit':limit,'next_offset':offset+limit if len(rows)>limit else None}


def list_page(rows, query):
    limit, offset = int(query.get('limit',['50'])[0]), int(query.get('offset',['0'])[0])
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ControlError('PAGE_INVALID')
    text, status = query.get('q',[''])[0].lower(), query.get('status',[''])[0]
    rows = [r for r in rows if text in (r['title']+' '+r['question']).lower() and (not status or r['control_status']==status or status=='RUNNING' and r['control_status']=='ACTIVE')]
    group = query.get('status_group',['all'])[0]
    if group == 'running':
        rows = [r for r in rows if r['control_status'] in {'DRAFT','STARTING','RESUMING','RUNNING','ACTIVE','PAUSED','PAUSE_REQUESTED'}]
    elif group == 'completed':
        rows = [r for r in rows if r['control_status']=='COMPLETED']
    rows.sort(key=lambda r:r['title'] if query.get('sort',['date'])[0]=='name' else r['updated_at'],reverse=query.get('sort',['date'])[0]!='name')
    fields = {'research_id','title','question','mode','control_status','last_action','research_depth','estimated_cost_usd','cost','updated_at'}
    return {'items':[{k:v for k,v in r.items() if k in fields} for r in rows[offset:offset+limit]],'total':len(rows),'offset':offset,'limit':limit,'next_offset':offset+limit if len(rows)>offset+limit else None}


def usage_page(api, path, rid=None):
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(path).query)
    if 'limit' not in query:
        return api.store.ledger(rid)
    return api.store.ledger(rid,limit=int(query['limit'][0]),offset=int(query.get('offset',['0'])[0]),scope=query.get('scope',['all'])[0],role=query.get('role',[None])[0])


def semantic_summary(api, rid):
    """의미 기록의 무결성을 확인하고 저장된 검사 요약만 반환한다."""
    cycle = api.read._state.cycle5
    if not cycle.enabled(rid):
        return {'enabled': False}
    from .cycle5 import CHECK_IDS
    sources, goals = cycle.records(rid, 'source'), cycle.records(rid, 'goal')
    checks = []
    for row in api.store.db.execute("SELECT mutation_id,status,verification_json FROM staged_mutations WHERE research_id=? AND verification_json IS NOT NULL ORDER BY rowid DESC LIMIT 10", (rid,)):
        saved = json.loads(row['verification_json'])
        selected = [c for c in saved.get('checks', []) if c.get('check_id') in CHECK_IDS]
        if row['status'] != 'COMMITTED':
            selected = [dict(c, passed=False, message='현재 승인 결과가 아님 · 저장된 검사 기록') for c in selected]
        if selected:
            checks.append({'mutation_id': row['mutation_id'], 'checks': selected})
    return {'enabled': True, 'summary': True, 'sources': [r.model_dump(mode='json') for r in sources[:50]],
            'goal': goals[0].model_dump(mode='json') if goals else None, 'current_checks': checks,
            'lineages': [], 'source_total': len(sources), 'live_efficacy': 'NOT_VALIDATED'}


def library_page(api, *, limit=50, offset=0):
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ControlError('PAGE_INVALID')
    query = "SELECT * FROM (SELECT research_id,artifact_id,artifact_type,status,sha256,size_bytes,created_at AS at,'artifact' AS node_kind FROM artifacts UNION ALL SELECT research_id,dataset_id,'DATASET',status,sha256,size_bytes,created_at,'dataset' FROM datasets UNION ALL SELECT research_id,source_id,'LITERATURE_SOURCE',status,metadata_hash,NULL,retrieved_at,'source' FROM sources) a JOIN (SELECT research_id,research_question,goal FROM research_runs) r USING(research_id) ORDER BY at DESC,artifact_id LIMIT ? OFFSET ?"
    rows = [dict(r) for r in api.store.db.execute(query,(limit+1,offset))]
    return {'items':rows[:limit],'offset':offset,'limit':limit,'next_offset':offset+limit if len(rows)>limit else None}
