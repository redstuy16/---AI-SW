"""관측 표본의 중앙값·범위를 비교하고 결과 범위를 함께 기록한다."""
import json
from pathlib import Path
import statistics

FOLDER = Path(__file__).resolve().parent


def read(name):
    return json.loads((FOLDER / name).read_text(encoding='utf-8', errors='strict'))


def samples(values):
    return {'median':statistics.median(values),'min':min(values),'max':max(values),'samples':values}


def main():
    before, after = read('baseline.json'), read('after.json')
    browser_before, browser_after = read('browser_baseline.json'), read('browser_after.json')
    metrics = {}
    def add(name, b, a, scope):
        metrics[name] = {'before':b,'after':a,'scope':scope}
    for name, field in [('startup_time_ms','cold_start_ms'),('backend_ready_ms','backend_ready_ms')]:
        add(name,samples([v[field] for v in before['cold']]),samples([v[field] for v in after['cold']]),'별도 Python 프로세스 3회 · OS 파일 캐시는 초기화하지 않음')
    add('browser_ready_ms',samples([v['browser_ready_ms'] for v in browser_before['samples']]),samples([v['browser_ready_ms'] for v in browser_after['samples']]),'실제 인증·Chrome headless 3회 · 기존 전체 목록과 새 첫 페이지의 화면 준비 시간')
    for name in ('idle_ram_mb','idle_cpu_percent','process_count'):
        add(name,before['cold'][0][name],after['cold'][0][name],'Python 본체 60초 유휴 · 브라우저·외부 모델 제외')
    for name, field in [('research_list_ms','research_list'),('activity_view_ms','activity_view_large'),('verification_view_ms','verification_view_open'),('report_open_ms','report_open'),('experiments_view_ms','experiments_view_open'),('evidence_view_ms','evidence_view_open')]:
        add(name,before[field+'_repeat_ms'],after[field+'_repeat_ms'],'동일 전체 응답 API · 첫 조회 별도, 이후 5회')
    for name in ('sqlite_size_mb','trace_size_mb','workspace_size_mb'):
        add(name,before[name],after[name],'동일 고정 합성 자료 · 1MiB=1024² bytes')
    for name in ('peak_ram_mb','peak_cpu_percent'):
        add(name,{k:samples([v[name] for v in rows]) for k,rows in before['demos'].items()},
            {k:samples([v[name] for v in rows]) for k,rows in after['demos'].items()},'각 Demo 3회 · Python+자식 RSS · CPU는 한 코어 100%, 100ms 표본')
    decisions = [
        ('LIST_BULK','research_list','전체 목록의 개별 Overview 반복을 일괄 SQL로 대체','KEEP'),
        ('VERIFICATION_BULK','verification_view_open','검증·무효화·메타데이터 조회를 묶고 기존 판정을 유지','KEEP'),
        ('EXPERIMENT_BULK','experiments_view_open','자료·의존·산출물 조회를 묶고 실제 파일 해시 검사를 유지','KEEP'),
        ('STARTUP_IMPORT','startup','기준선에서도 무거운 라이브러리를 읽지 않아 기존 경계를 유지','NO_MEANINGFUL_GAIN'),
        ('IDLE','idle','기준선·수정 후 모두 유휴 CPU 0% · 개선 주장 없음','NO_MEANINGFUL_GAIN')]
    records = []
    for identity, metric, change, decision in decisions:
        b = before.get(metric+'_repeat_ms',before['cold'])
        a = after.get(metric+'_repeat_ms',after['cold'])
        records.append({'optimization_id':identity,'baseline':b,'change':change,'after':a,'complexity_risk':'읽기 범위·판정 동등성 검사 필요 · 과학 캐시 없음','decision':decision})
    for identity, baseline, change, observed, risk in (
        ('UI_PAGING','전체 근거·실험·검증·활동·자료 상세를 읽음','조회 페이지와 선택 상세를 분리',{k:v for k,v in after.items() if k.startswith('ui_')},'요약과 실제 재검증을 구분 · 응답 범위 차이를 개선율로 사용하지 않음'),
        ('EVIDENCE_INDEX','EXPLAIN: SCAN evidence','기존 운영 초기화에서 연구/실험 복합 인덱스 1개 추가',after['evidence_query_plan'],'추가 디스크 공간 · migration 6개 유지'),
        ('SHARED_RESOURCE_QUEUE','연결별 예산 예약 제한 · 공유 자원 대기 정보 없음','기존 운영 설정·감사와 BEGIN IMMEDIATE로 원자적 자원 획득','실제 별도 프로세스/취소/timeout/불확실 비용/모드 동등성 테스트','동일 앱 DB 범위 · 외부 서버 실측 없음'),
        ('FIGURE_FINALLY','저장 실패 경로에 close() 없음 · 코드 관찰','생성한 그림을 finally에서 닫음','OSError/MemoryError × 일반/Skills: 4 PASS','기존 과학 출력과 다른 그림은 유지'),
        ('BOUNDED_FLOW','NOT_AVAILABLE','제한된 DOM과 안정된 배치·지연 상세·닫기 해제',read('flow_large.json'),'새 화면의 측정 · 전체 Chrome RSS나 Live Agent 성능 아님')):
        records.append({'optimization_id':identity,'baseline':baseline,'change':change,'after':observed,'complexity_risk':risk,'decision':'KEEP'})
    value = {'baseline_source_fingerprint':before['source_fingerprint'],'after_source_fingerprint':after['source_fingerprint'],
             'metrics':metrics,'decisions':records,'fixture_counts':after['fixture_counts'],
             'ui_page_metrics':{k:v for k,v in after.items() if k.startswith('ui_')},
             'browser_payload':{'before':browser_before['samples'],'after':browser_after['samples'],'comparison_limit':'조회 범위가 전체에서 첫 페이지로 바뀜. 기준선 bytes는 준비 시점의 미완료 비동기 카운터로 정확한 총 전송량 비교 불가'},
             'live_agent_performance':'NOT_VALIDATED','local_model_throughput':'NOT_VALIDATED',
             'regression_evidence':'최종 결과는 final_validation.json 참조 · 중간 실행을 통과로 합산하지 않음',
             'limits':['단일 Windows 호스트 관측','실제 저사양 하드웨어·GPU·외부 로컬 서버 미검증','최초 브라우저 기준선은 API 측정과 잠시 겹침','짧은 작업은 100ms 표본 사이의 최고 RSS/CPU를 놓칠 수 있음']}
    text = json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    text.encode('utf-8',errors='strict')
    (FOLDER / 'comparison.json').write_text(text,encoding='utf-8')
    print('관측 비교 저장 완료')


if __name__=='__main__':
    main()
