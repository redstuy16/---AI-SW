"""지정 문제만 실제 앱 실행기로 검사하며 재시도까지 문제별 상한에 합산한다."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'qa')]
from science_live_validation import source_settings, make_snapshot, ValidationSourceGuard, write_json
from science_validation_budget import LiveValidationBudget
from probe.control_plane import ControlStore, Defaults, Credentials, ReasoningPolicy
from probe.control_runtime import execute
from probe.database import initialize, to_json
from probe.service import StateService
from probe.storage import Workspace
from probe.schemas import utc_now
from probe.research_report import report_record

CASES = json.loads((ROOT / 'qa/climate_questions.json').read_text(encoding='utf-8'))
CAP = Decimal('1.40')

def history(root, case):
    import sqlite3
    total = Decimal(0)
    for folder in root.iterdir():
        if not folder.is_dir() or not (folder/'attempt.json').is_file():
            continue
        info=json.loads((folder/'attempt.json').read_text(encoding='utf-8'))
        if info['case'] != case:
            continue
        db=sqlite3.connect((folder/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
        try:
            protected=db.execute("SELECT COUNT(*) FROM spend_ledger WHERE status IN ('RESERVED','DISPATCHED')").fetchone()[0]
            if protected:
                raise ValueError('NEEDS_RECONCILIATION')
            total += Decimal(db.execute("SELECT COALESCE(SUM(settled),0) FROM spend_ledger WHERE status='SETTLED'").fetchone()[0])/Decimal(1000000)
            total += Decimal(db.execute("SELECT COALESCE(SUM(reserved),0) FROM spend_ledger WHERE status='UNRESOLVED'").fetchone()[0])/Decimal(1000000)
        finally:
            db.close()
    return total

async def run_case(case_id):
    case=CASES[case_id-1]
    root=ROOT/'build/climate-live';root.mkdir(parents=True,exist_ok=True)
    folder=root/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-q'+str(case_id)+'-'+uuid4().hex[:6]);folder.mkdir()
    connection,profile,source_meta=source_settings(ROOT/'build/workbench/state.sqlite')
    connection=connection.model_copy(update={'read_timeout':120})
    profile=profile.model_copy(update={'input_byte_limit':200000 if case_id==5 else 131072,'context_limit':262144,'max_input_tokens':128000,'max_retries':0,'timeout_sec':120,'reasoning_policy':ReasoningPolicy.MEDIUM if case_id==5 else ReasoningPolicy.LOW})
    credentials=Credentials(ROOT,folder/'workspace')
    if not credentials.get(connection.credential_env_name):
        raise ValueError('CREDENTIAL_UNCONFIGURED')
    with ExitStack() as cleanup:
        budget=cleanup.enter_context(LiveValidationBudget(root,folder,CAP,conservative_upper_bounds=True))
        prior=history(root,case_id);available=Decimal(case['cap'])-prior
        if available<=0:
            raise ValueError('CASE_BUDGET_EXHAUSTED')
        db=initialize(folder/'state.sqlite');cleanup.callback(db.close)
        store=ControlStore(db)
        store.put('defaults','global',Defaults(monthly_limit_usd=budget.available_usd,request_limit_usd=available,search_attempt_limit=20))
        store.put('connection',connection.connection_id,connection)
        snapshot=make_snapshot({'id':'literature' if case_id==5 else 'principle','question':case['question']},'app',32768 if case_id==5 else 8192,connection,profile,None,None,source_meta['explicit_model_limits'])
        if case_id==5:
            for model in snapshot['models'].values():model['task_output_limits']={'planning':8192,'report':32768}
        snapshot.update(run_limit_usd=str(available),request_limit_usd=str(available),monthly_limit_usd=str(budget.available_usd),
            max_elapsed_sec=3600 if case_id==5 else 600,science_max_decisions=20 if case_id==5 else 8,
            science_no_progress_limit=3,science_field='earth_science',search_policy='AUTO' if case_id==5 else 'DISABLED',
            science_context_budget=64000 if case_id==5 else 16000,
            science_max_actions=200 if case_id==5 else 30,
            search_backend='FREE_SCHOLARLY' if case_id==5 else 'AI',
            search_required=case_id==5,search_attempt_limit=20,source_fetch_attempt_limit=20)
        if case.get('csv'):
            data=case['csv'].encode('utf-8',errors='strict');path=folder/'workspace/inputs/climate.csv';path.parent.mkdir(parents=True);path.write_bytes(data)
            snapshot.update(source_relative='climate.csv',source={'sha256':hashlib.sha256(data).hexdigest()})
        state=StateService(db,Workspace(folder/'workspace'));rid=state.create_research(case['question']);state.workspace.prepare(rid)
        db.execute("INSERT INTO control_runs(research_id,title,status,snapshot,created_at) VALUES(?,?,'STARTING',?,?)",(rid,case['title'],to_json(snapshot),utc_now().isoformat()))
        write_json(folder/'attempt.json',{'case':case_id,'title':case['title'],'research_id':rid,'model':'gpt-6-luna','cap_usd':case['cap'],'prior_spent_usd':str(prior),'remaining_cap_usd':str(available),'execution':'LIVE_USER_SESSION','source_meta':source_meta})
        guard=ValidationSourceGuard();guard.check('CASE_START');guard.install(cleanup)
        print(to_json({'event':'START','case':case_id,'research_id':rid,'folder':str(folder),'remaining_cap_usd':str(available)}),flush=True)
        started=perf_counter();await execute(folder/'state.sqlite',folder/'workspace',rid)
        try:guard.check('CASE_FINISH')
        except ValueError:pass
        row=store.run(rid);ledger=store.ledger(rid);record=report_record(state,rid,validate=False)
        observations=[json.loads(r[0]) for r in db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'science:observation:%' ORDER BY rowid",(rid,))]
        events=[{'type':r[0],'details':json.loads(r[1])} for r in db.execute('SELECT event_type,details_json FROM runtime_events WHERE research_id=? ORDER BY seq',(rid,))]
        summary={'case':case_id,'research_id':rid,'model':'gpt-6-luna','status':row['status'],'error':row.get('error'),
            'elapsed_sec':round(perf_counter()-started,3),'spent_usd':ledger['spent'],'unresolved_usd':ledger['unresolved'],
            'reserved_usd':ledger['reserved'],'model_calls':db.execute("SELECT COUNT(*) FROM spend_ledger WHERE status='SETTLED' AND model='gpt-6-luna'").fetchone()[0],
            'free_search_requests':db.execute("SELECT COUNT(*) FROM spend_ledger WHERE status='SETTLED' AND role='search' AND model!='gpt-6-luna'").fetchone()[0],
            'report_status':record.get('status') if record else 'NOT_CREATED','source_guard':guard.metadata(),
            'report':record,'observations':observations,'events':events}
        write_json(folder/'result.json',summary)
        print(to_json({key:summary[key] for key in ('case','research_id','status','error','elapsed_sec','spent_usd','model_calls','report_status')}),flush=True)
        if record:
            try:
                from probe.report_pdf import render_pdf
                (folder/'report.pdf').write_bytes(render_pdf(state,rid,protected_values=credentials.active_secrets([connection.credential_env_name]))['data'])
            except Exception as exc:
                print(to_json({'event':'PDF_NOT_AVAILABLE','error_type':type(exc).__name__}),flush=True)
        return folder

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--case',type=int,choices=range(1,6),required=True)
    args=parser.parse_args();asyncio.run(run_case(args.case))
