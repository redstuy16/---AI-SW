"""같은 저장된 활동 자료와 새 Demo A로 로컬 자원 사용을 관측한다."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from threading import Event, Thread
from time import perf_counter
from uuid import uuid4


def write(path, value):
    text=json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    text.encode('utf-8',errors='strict')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8')


def timed(call):
    start=perf_counter()
    value=call()
    return round((perf_counter()-start)*1000,3),value


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline',type=Path,default=Path('build/hardening-resource-baseline'))
    parser.add_argument('--output',type=Path,default=Path('build/hardening-resource-after'))
    args=parser.parse_args()
    folder=args.output/uuid4().hex
    folder.mkdir(parents=True)
    start=perf_counter()
    from htrsa.workbench import WorkbenchAPI
    api=WorkbenchAPI(folder/'cold.sqlite',folder/'cold-workspace',launch=False)
    cold=perf_counter()-start
    import psutil
    process=psutil.Process()
    result={'cold_start_sec':cold,'idle_process_count':1+len(process.children(recursive=True)),
            'idle_cpu_percent':process.cpu_percent(interval=.3),'idle_ram_mb':process.memory_info().rss/1024**2,
            'simple_view_heavy_modules':[k for k in ('numpy','scipy','matplotlib','sklearn','pandas') if k in sys.modules],
            'gpu':'NOT_APPLICABLE','api_load_ms':{}}
    result['api_load_ms']['list'],_=timed(lambda:api.request('GET','/api/control/research'))
    api.close()
    stop=Event()
    samples=[]
    def sample():
        process.cpu_percent()
        while not stop.wait(.1):
            samples.append((process.memory_info().rss/1024**2,process.cpu_percent()))
    thread=Thread(target=sample,daemon=True)
    thread.start()
    try:
        from htrsa.demo import run_demo_a
        start=perf_counter()
        demo=run_demo_a(folder/'fresh-demo.sqlite',folder/'fresh-demo-workspace')
        result['demo_sec']=perf_counter()-start
        result['demo_passed']=bool(demo['validation']['passed'])
    finally:
        stop.set();thread.join()
    result['peak_demo_ram_mb']=max((v[0] for v in samples),default=process.memory_info().rss/1024**2)
    result['peak_demo_cpu_percent']=max((v[1] for v in samples),default=0)
    # 기준 자료는 복사한 뒤 조회하여 이전 측정 기록을 보존한다.
    workspace=folder/'shared-workspace'
    shutil.copytree(args.baseline/'demo-workspace',workspace)
    for source,target,large in [('demo.pre-workbench.sqlite','detail.sqlite',False),('demo.sqlite','large.sqlite',True)]:
        shutil.copy2(args.baseline/source,folder/target)
        view=WorkbenchAPI(folder/target,workspace,launch=False)
        rid=view.store.db.execute('SELECT research_id FROM research_runs').fetchone()[0]
        try:
            if not large:
                result['api_load_ms']['detail'],_=timed(lambda:view.request('GET',f'/api/research/{rid}'))
                result['api_load_ms']['activity'],_=timed(lambda:view.request('GET',f'/api/control/research/{rid}/activity?limit=25'))
            else:
                result['api_load_ms']['large_activity'],page=timed(lambda:view.request('GET',f'/api/control/research/{rid}/activity?limit=25'))
                result['large_activity_rows']=len(page.body['items'])
                result['api_load_ms']['large_activity_legacy'],legacy=timed(lambda:view.request('GET',f'/api/research/{rid}/timeline'))
                result['large_activity_legacy_rows']=len(legacy.body)
                result['canonical_runtime_events']=view.store.db.execute('SELECT COUNT(*) FROM runtime_events WHERE research_id=?',(rid,)).fetchone()[0]
                result['sqlite_query_plan']=[tuple(r) for r in view.store.db.execute('EXPLAIN QUERY PLAN SELECT * FROM runtime_events WHERE research_id=? ORDER BY seq DESC LIMIT 25',(rid,))]
        finally:
            view.close()
    result['sqlite_bytes']=(folder/'large.sqlite').stat().st_size
    files=[p for p in workspace.rglob('*') if p.is_file()]
    result['trace_bytes']=sum(p.stat().st_size for p in files if p.suffix=='.jsonl')
    result['workspace_bytes']=sum(p.stat().st_size for p in files)
    result['limits']=['각 항목 1회 관측 · 통계적 성능 개선 주장 없음',
                      '활동 화면은 전체 3109행에서 페이지 25행으로 변경 · 원본 기록 유지',
                      'CPU는 프로세스 기준이며 한 코어 100% · GPU 측정 대상 없음',
                      '기준 측정 당시 전체 테스트도 실행 중이었으므로 시간·CPU 비교에 간섭 가능']
    write(args.output/'measurements.json',result)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
