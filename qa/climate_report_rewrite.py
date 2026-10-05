"""완료한 장기 연구의 자료를 재사용해 요청 산출물을 보완한다."""
import argparse,asyncio,json,sys
from pathlib import Path
from contextlib import ExitStack
from decimal import Decimal
from time import perf_counter
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'qa')]
from climate_live import history,CAP
from science_validation_budget import LiveValidationBudget
from science_live_validation import ValidationSourceGuard,write_json
from probe.workbench import WorkbenchAPI
from probe.research_report import rewrite_report,report_record,requested_report_sections,missing_report_sections
from probe.report_pdf import render_pdf

async def main(folder):
    root=ROOT/'build/climate-live';folder=folder.resolve()
    if not folder.is_relative_to(root.resolve()):raise ValueError('WORKSPACE_ESCAPE')
    info=json.loads((folder/'attempt.json').read_text(encoding='utf-8'))
    if info['case']!=5:raise ValueError('CASE_MISMATCH')
    with ExitStack() as cleanup:
        budget=cleanup.enter_context(LiveValidationBudget(root,folder,CAP,conservative_upper_bounds=True))
        available=Decimal(info['cap_usd'])-history(root,5)
        if available<=0:raise ValueError('CASE_BUDGET_EXHAUSTED')
        app=WorkbenchAPI(folder/'state.sqlite',folder/'workspace',launch=False);cleanup.callback(app.store.db.close)
        rid=info['research_id'];run=app.store.run(rid)
        guard=ValidationSourceGuard();guard.check('REWRITE_START');guard.install(cleanup)
        print(json.dumps({'event':'REWRITE_START','research_id':rid,'remaining_case_cap':str(available)}),flush=True)
        started=perf_counter();key='climate-report-'+uuid4().hex
        result=await rewrite_report(app,rid,{'expected_version':run['version'],'state_version':app.read._state.state_version(rid),'idempotency_key':key})
        guard.check('REWRITE_FINISH');record=report_record(app.read._state,rid,validate=True)
        from probe.research_report import ReportDraft
        missing=missing_report_sections(ReportDraft.model_validate(record['draft']),requested_report_sections(run['snapshot']['question']))
        summary={'result':result,'elapsed_sec':round(perf_counter()-started,3),'missing_sections':missing,'report':record,'source_guard':guard.metadata()}
        write_json(folder/('rewrite-'+key+'.json'),summary)
        print(json.dumps({'event':'REWRITE_FINISH','status':result['status'],'elapsed_sec':summary['elapsed_sec'],'missing':missing}),flush=True)
        if record['status']=='READY':
            (folder/'report-verified.pdf').write_bytes(render_pdf(app.read._state,rid,protected_values=app.credentials.active_secrets())['data'])

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);args=parser.parse_args()
    asyncio.run(main(args.folder))
