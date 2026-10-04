"""모의 검색·PDF 저장·보고서 응답 뒤 실제 프로세스 종료와 재개를 검사한다."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT / 'qa')]
from htrsa.control_runtime import execute
from htrsa.research_report import report_record
from htrsa.service import StateService
from htrsa.workbench import WorkbenchAPI
from test_free_search_recovery import free_rig
from test_research_report_flow import run
from research_report_browser_fixture import Patch


def save(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8', errors='strict'))


def worker(folder, boundary, mode):
    api = WorkbenchAPI(folder / 'state.sqlite', folder / 'workspace', launch=False)
    patch = Patch()
    gateway, model_calls, requests = free_rig(api, patch)

    def observed():
        row = api.store.db.execute('SELECT research_id FROM control_runs LIMIT 1').fetchone()
        rid = row[0] if row else None
        return {'model_calls': model_calls, 'requests': requests, 'paid_calls': 0,
                'status': api.store.run(rid)['status'] if rid else None,
                'report_status': (report_record(api.read._state, rid) or {}).get('status') if rid else None}

    def crash():
        save(folder / 'before.json', {'model_calls': model_calls, 'requests': requests, 'paid_calls': 0})
        os._exit(79)

    if mode == 'crash':
        if boundary == 'search-cache':
            original = StateService.search_cache_put
            def after_search(state, *args, **kwargs):
                value = original(state, *args, **kwargs)
                crash()
                return value
            patch.setattr(StateService, 'search_cache_put', after_search)
        elif boundary == 'download':
            patch.setattr('htrsa.source_documents.extract_pdf', lambda *args, **kwargs: crash())
        elif boundary == 'report-response':
            original = StateService.finish_runtime_step
            def after_report(state, rid, key, *args, **kwargs):
                value = original(state, rid, key, *args, **kwargs)
                if key == 'report-draft:1':
                    crash()
                return value
            patch.setattr(StateService, 'finish_runtime_step', after_report)
        run(api, gateway, fulltext_enabled=True, public_search_query='')
        raise AssertionError('종료 지점에 도달하지 못했습니다.')
    rid = api.store.db.execute('SELECT research_id FROM control_runs LIMIT 1').fetchone()[0]
    api.command(rid, 'resume', {'expected_version': api.store.run(rid)['version'],
                              'idempotency_key': 'fresh-free-source-resume'})
    asyncio.run(execute(api.database, api.workspace, rid, provider_factory=gateway))
    save(folder / 'after.json', observed())
    api.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
        return
    folder = ROOT / 'build/free-search/recovery' / uuid4().hex
    folder.mkdir(parents=True)
    cases = []
    for boundary in ['search-cache', 'download', 'report-response']:
        target = folder / boundary
        target.mkdir()
        command = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--worker', str(target), boundary]
        crashed = subprocess.run([*command, 'crash'], capture_output=True, timeout=90, cwd=ROOT)
        assert crashed.returncode == 79, crashed.stderr.decode('utf-8', errors='replace')
        before = json.loads((target / 'before.json').read_text(encoding='utf-8'))
        resumed = subprocess.run([*command, 'resume'], capture_output=True, timeout=90, cwd=ROOT)
        assert resumed.returncode == 0, resumed.stderr.decode('utf-8', errors='replace')
        after = json.loads((target / 'after.json').read_text(encoding='utf-8'))
        model_calls = before['model_calls'] + after['model_calls']
        requests = before['requests'] + after['requests']
        assert after['status'] == 'COMPLETED' and after['report_status'] == 'READY', after
        assert model_calls == ['manager', 'report'], model_calls
        assert all(requests.count(item) == 1 for item in before['requests']), requests
        assert sum(host == 'papers.example.org' for host, _ in requests) == 1, requests
        cases.append({'boundary': boundary, 'crash_exit_code': crashed.returncode,
                      'resume_exit_code': resumed.returncode, 'new_process': True, 'passed': True,
                      'model_calls_before': before['model_calls'], 'model_calls_after': after['model_calls'],
                      'requests_before': len(before['requests']), 'requests_after': len(after['requests']),
                      'pdf_downloads': 1, 'report_status': after['report_status'], 'paid_calls': 0})
    result = {'execution': 'FRESH_PROCESS_OFFLINE_FAKE_PROVIDERS', 'passed': True, 'cases': cases,
              'paid_calls': 0, 'live_efficacy': 'NOT_VALIDATED'}
    save(folder / 'result.json', result)
    save(ROOT / 'build/free-search/recovery_results.json', result)
    print(json.dumps({'passed': True, 'cases': len(cases), 'paid_calls': 0, 'output': str(folder / 'result.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
