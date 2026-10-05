"""과거 무료 검색 probe를 명시 LEGACY 실행으로 격리하며 현행 AI 검색 검증에서 제외한다."""
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from probe.workbench import WorkbenchAPI
from probe.search_policy import run_search, qualified_literature
from probe.schemas import utc_now
from probe.database import to_json
from test_workbench import configure
from test_research_report_flow import request


async def main():
    if os.environ.get('PROBE_LEGACY_LIVE_SEARCH') != '1':
        raise SystemExit('LEGACY_LIVE_SEARCH_DISABLED: 과거 probe에는 PROBE_LEGACY_LIVE_SEARCH=1이 필요합니다. 현행 검사는 science_live_validation.py를 사용하세요.')
    folder = ROOT / 'build/free-search/live' / uuid4().hex
    api = WorkbenchAPI(folder / 'state.sqlite', folder / 'workspace', launch=False)
    configure(api)
    created = api.create(request(search_attempt_limit=10, max_elapsed_sec=180))
    rid = created['research_id']
    result = {'execution': 'LIVE_ANONYMOUS_OPENALEX_CROSSREF', 'started_at': utc_now().isoformat(),
              'research_id': rid, 'model_calls': 0, 'cash_cost_usd': '0', 'quantitative_values': {'requested': 5, 'verified': 0},
              'live_agent_efficacy': 'NOT_VALIDATED', 'live_pdf_collection': 'NOT_VALIDATED'}
    try:
        await run_search(api.read._state, api.store, api.credentials, rid, created['snapshot'],
                         queries=['CO2 degassing temperature carbonated water', 'temperature CO2 release carbonated beverages'])
        result['status'] = 'COMPLETED'
    except Exception as exc:
        result.update(status='LIMITED', error=getattr(exc, 'code', type(exc).__name__))
    result.update(completed_at=utc_now().isoformat(), qualified_evidence=qualified_literature(api.read._state, rid),
                  sources=[dict(r) for r in api.store.db.execute('SELECT source_id,title,url,status,CASE WHEN abstract IS NULL THEN 0 ELSE length(abstract) END abstract_length FROM sources WHERE research_id=?', (rid,))],
                  evidence=[dict(r) for r in api.store.db.execute("SELECT evidence_id,status,evidence_text,text_field FROM evidence WHERE research_id=?", (rid,))],
                  requests=[json.loads(r[0]) for r in api.store.db.execute("SELECT payload FROM control_audit WHERE research_id=? AND kind IN ('SEARCH_COMPLETED','SEARCH_FETCH_COMPLETED')", (rid,))], ledger=api.store.ledger(rid))
    data = (to_json(result) + '\n').encode('utf-8', errors='strict')
    (folder / 'result.json').write_bytes(data)
    print(to_json({'status': result['status'], 'error': result.get('error'), 'sources': len(result['sources']),
                   'verified_evidence': len(result['evidence']), 'actual_requests': sum(v.get('actual_dispatch_attempts', 0) for v in result['requests']),
                   'cash_cost_usd': result['ledger']['spent'], 'output': str(folder / 'result.json')}))
    api.close()


if __name__ == '__main__':
    asyncio.run(main())
