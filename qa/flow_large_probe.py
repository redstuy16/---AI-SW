"""합성 대형 이력과 실제 복구 분기를 복사하고 상태 변경을 정본 서비스로 수행한다."""
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from probe.workbench import WorkbenchAPI


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--update', action='store_true')
    parser.add_argument('--folder', type=Path, default=ROOT / 'build/flow-large-fixture')
    args = parser.parse_args()
    if args.prepare and not args.folder.exists():
        shutil.copytree(ROOT / 'build/flow-ui-fixture', args.folder)
    fixture = json.loads((args.folder / 'fixture.json').read_text(encoding='utf-8'))
    api = WorkbenchAPI(args.folder / 'state.sqlite', args.folder / 'workspace', launch=False)
    try:
        state, rid = api.read._state, fixture['repair']
        if args.prepare:
            from probe.database import connect
            source = connect(ROOT / 'build/flow-opt-fixture/state.sqlite')
            with api.store.transaction():
                for table in ('experiments', 'evidence', 'staged_mutations'):
                    for row in source.execute('SELECT * FROM ' + table + ' WHERE research_id=?', ('R-PERF-0',)):
                        value = dict(row, research_id=rid)
                        if table == 'staged_mutations':
                            value['contract_id'] = api.store.db.execute('SELECT contract_id FROM contracts WHERE research_id=? LIMIT 1', (rid,)).fetchone()[0]
                        columns = list(value)
                        api.store.db.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(value[k] for k in columns))
            source.close()
            fixture['hypothesis'] = state.create_hypothesis(rid, {'statement':'합성 화면 상태 변경 검사', 'rationale':'성능 측정용이며 과학 결론에 사용하지 않음',
                'score':dict.fromkeys(('plausibility','testability','data_availability','information_value','cost'), .5)}, created_by='manager')
            
        if args.update:
            state.set_hypothesis_status(rid, fixture['hypothesis'], 'SHORTLISTED', decided_by='manager', rationale='합성 화면의 단일 상태 갱신 측정')
        from probe.research_flow import project_flow
        result = project_flow(api, rid, view='all', limit=150)
        if args.prepare:
            from probe.preflight import _source_fingerprint
            seen, boundary = set(), 0
            for offset in range(0,result['visible_total'],150):
                page = project_flow(api,rid,view='all',limit=150,offset=offset)
                seen.update(e['id'] for e in page['edges'])
                boundary = max(boundary,page['boundary_edges'])
            fixture.update(source_fingerprint=_source_fingerprint(),edge_lower_bound=max(len(seen),boundary))
            text = json.dumps(fixture,ensure_ascii=False,indent=2)+'\n'
            text.encode('utf-8',errors='strict')
            (args.folder/'fixture.json').write_text(text,encoding='utf-8')
        print(json.dumps({'nodes':result['total_nodes'], 'visible':result['visible_total'], 'page_edges':len(result['edges'])}))
    finally:
        api.close()


if __name__ == '__main__':
    main()
