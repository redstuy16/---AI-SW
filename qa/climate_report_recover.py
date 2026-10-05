"""정산·응답·입력·본문이 일치하는 완료 초안만 재호출 없이 다시 검증한다."""
import json,sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'qa')]
from probe.workbench import WorkbenchAPI
from probe.research_report import ReportDraft,requested_report_sections,missing_report_sections,report_record,input_fingerprint,persist_report_draft
from probe.report_pdf import render_pdf
from probe.final_report import export_final_report
from probe.control_plane import ControlError
from science_live_validation import write_json

folder=(ROOT/'build/climate-live/20261005-234058-q5-12e638').resolve()
app=WorkbenchAPI(folder/'state.sqlite',folder/'workspace',launch=False)
try:
    rid=json.loads((folder/'attempt.json').read_text(encoding='utf-8'))['research_id'];state=app.read._state
    prior=report_record(state,rid,validate=False);run=app.store.run(rid)
    if prior['error']!='REPORT_REQUESTED_SECTION_MISSING' or prior['input_fingerprint']!=input_fingerprint(state,rid):raise ControlError('REPORT_STALE')
    contract=prior['attempts'][-1]['contract_id']
    rows=app.store.db.execute("SELECT c.key,c.output_json,c.reservation_id,l.response_id,a.payload FROM control_model_cache c JOIN spend_ledger l ON l.id=c.reservation_id JOIN control_audit a ON json_extract(a.payload,'$.reservation_id')=l.id AND a.kind='NORMALIZED_RESPONSE_SETTLED' WHERE c.research_id=? AND l.research_id=? AND a.research_id=? AND l.status='SETTLED' AND c.error IS NULL AND c.model='gpt-6-luna' AND json_extract(a.payload,'$.contract_id')=?",(rid,rid,rid,contract)).fetchall()
    if len(rows)!=1:raise ControlError('REPORT_CACHE_UNVERIFIED')
    row=rows[0];trace=json.loads(row['payload']);checkpoint=app.store.config('provider_response_checkpoint',row['reservation_id']);native=checkpoint['result'];output=json.loads(row['output_json'])
    if checkpoint['research_id']!=rid or checkpoint['request_key']!=row['key'] or native['status']!='COMPLETED' or native['response_id']!=row['response_id'] or native['response_id']!=trace['response_id'] or native['model_id']!='gpt-6-luna' or native['usage']!=trace['usage'] or native['raw_response_ref']!=trace['raw_response_ref'] or json.loads(native['output_text'])!=output:raise ControlError('REPORT_CACHE_CHANGED')
    draft=ReportDraft.model_validate(output);missing=missing_report_sections(draft,requested_report_sections(run['snapshot']['question']))
    if missing:raise ControlError('REPORT_REQUESTED_SECTION_MISSING')
    record=persist_report_draft(SimpleNamespace(state=state),app.store,rid,run['snapshot'],draft,request_key='validated-table-recovery-'+row['reservation_id'])
    if record['status']!='READY':raise ControlError('REPORT_NOT_READY')
    state.runtime_event(rid,'REPORT_COMPLETED_DRAFT_RECOVERED',{'reservation_id':row['reservation_id'],'response_id':native['response_id'],'paid_requests':0,'repair':'RESULT_TABLE_SATISFIES_GRAPH_OR_TABLE'})
    export_final_report(state,rid)
    (folder/'report-verified.pdf').write_bytes(render_pdf(state,rid,protected_values=app.credentials.active_secrets())['data'])
    write_json(folder/'report-recovery.json',{'status':record['status'],'missing_sections':missing,'paid_requests':0,'original_response_id':native['response_id'],'report':record})
    print(json.dumps({'status':record['status'],'missing':missing,'paid_requests':0}))
finally:app.store.db.close()
