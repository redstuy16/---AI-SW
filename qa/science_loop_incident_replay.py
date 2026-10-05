"""운영 기록은 읽기만 하며 거절됐던 보고서를 격리 복사본에서 다시 검증한다."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from probe.control_plane import ControlStore
from probe.database import initialize
from probe.final_report import export_final_report
from probe.research_report import ReportDraft, bind_calculation_mentions, validate_draft, persist_report_draft
from probe.report_pdf import render_pdf
from probe.service import StateService
from probe.storage import Workspace


def main():
    parser=argparse.ArgumentParser(description='실제 실패 초안의 오프라인 복구 검사')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    if not output.is_relative_to(ROOT/'build') or output.exists():
        raise ValueError('새 build 하위 폴더를 지정하세요.')
    source=ROOT/'build/workbench/state.sqlite'
    before=sha256(source.read_bytes()).hexdigest()
    readonly=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    readonly.row_factory=sqlite3.Row
    run=readonly.execute('SELECT research_id,snapshot,status,error FROM control_runs ORDER BY rowid DESC LIMIT 1').fetchone()
    rid=run['research_id']
    output.mkdir(parents=True)
    db=initialize(output/'state.sqlite')
    readonly.backup(db)
    readonly.close()
    source_root=Workspace(ROOT/'build/workbench/workspace').research_root(rid)
    workspace=Workspace(output/'workspace')
    shutil.copytree(source_root,workspace.research_root(rid))
    state=StateService(db,workspace)
    checks=[]
    candidates=[]
    for row in db.execute("SELECT step_key,output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'science:observation:%' ORDER BY rowid",(rid,)):
        saved=json.loads(row['output_json'])
        if saved['action']!='COMPLETE' or not saved['result'].get('draft'):
            continue
        draft=bind_calculation_mentions(state,rid,ReportDraft.model_validate(saved['result']['draft']))
        try:
            validate_draft(state,rid,draft)
            checks.append({'step':row['step_key'],'original_error':saved['result'].get('reason'),'status':'READY'})
            candidates.append((row['step_key'],draft))
        except Exception as exc:
            checks.append({'step':row['step_key'],'original_error':saved['result'].get('reason'),
                'status':'BLOCKED','code':getattr(exc,'code',type(exc).__name__),'repair':getattr(exc,'repair',None)})
    if candidates:
        key,draft=candidates[0]
        record=persist_report_draft(SimpleNamespace(state=state),ControlStore(db),rid,json.loads(run['snapshot']),
            draft,request_key='incident-replay:'+key)
        export_final_report(state,rid)
        pdf=render_pdf(state,rid)['data']
        (output/'recovered_report.pdf').write_bytes(pdf)
    result={'research_id':rid,'original_status':run['status'],'original_error':run['error'],
        'checks':checks,'recovered_report_status':record['status'] if candidates else 'NOT_CREATED',
        'source_database_unchanged':before==sha256(source.read_bytes()).hexdigest(),
        'execution':'OFFLINE_SAVED_RESPONSE_REPLAY','paid_calls':0}
    text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    data=text.encode('utf-8',errors='strict')
    (output/'result.json').write_bytes(data)
    print(json.dumps(result,ensure_ascii=False))
    db.close()
    if not checks or any(item['status']!='READY' for item in checks) or not result['source_database_unchanged']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
