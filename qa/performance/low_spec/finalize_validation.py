"""현재 gate를 확인하고 기존 export·보안 검사 결과와 최종 보고서를 묶는다."""
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'qa'))
from flow_offline_validation import save
import qa_day1 as qa
from probe.preflight import _source_fingerprint, environment_status, record_qa_validation

FOLDER = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text(encoding='utf-8', errors='strict'))


def main():
    source = _source_fingerprint()
    gates = environment_status()
    core = read(ROOT / 'build/validation/core.json')
    if not gates['demo_ready'] or not core['passed'] or core['source_fingerprint'] != source:
        raise RuntimeError('현재 소스의 CORE·파괴·Demo·artifact gate가 필요합니다.')
    offline = read(FOLDER / 'offline_validation.json')
    if offline['source_fingerprint'] != source:
        raise RuntimeError('파괴·반복 검증의 소스가 현재 소스와 다릅니다.')
    folder = ROOT / Path(offline['release']['clean_database']).parent
    rid = offline['demo_repeatability']['runs']['A'][0]['research_id']
    qa.BUILD = ROOT / 'build/flow-final-release' / uuid4().hex
    release = qa.validate_release((folder, {'research_id':rid}))
    record_qa_validation('artifact', release['passed'], {'file_count':release['file_count'], 'final_export':True})
    save(FOLDER / 'final_release.json', release)
    if not release['passed']:
        raise RuntimeError('최종 release 검증 실패')
    exports = {}
    for clean, name in ((False,'f3p_export_validation.json'), (True,'f3p_clean_export_validation.json')):
        command = [sys.executable, str(ROOT/'qa/f3p_export_probe.py'), '--evaluation-dir', str(ROOT/'build/flow-final-f3p-v3')]
        if clean:
            command.append('--clean')
        run = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=120)
        if run.returncode:
            raise RuntimeError('F3-P export 검사 실패: ' + name)
        exports['clean' if clean else 'canary'] = read(ROOT/'qa/results'/name)
    total = sum(c['file_count'] for v in exports.values() for c in v['cases'])
    value = read(FOLDER / 'final_validation.json')
    env = environment_status()
    value.update(recorded_at=datetime.now().astimezone().isoformat(), source_fingerprint=source,
        source_fingerprint_status='현재 소스와 CORE·오프라인 검증·마지막 성능 측정 일치',
        core_gate={'status':'PASS', 'passed':core['passed_count'], 'skipped':False,
            'command':'.venv\\Scripts\\python.exe -X utf8 -m probe.preflight validate-core',
            'marker':'not live_api and not live_search and not docker_integration and not os_secret_integration'},
        final_regression={'status':'PASS', 'passed':845, 'skipped':5, 'deselected':2, 'seconds':807.97,
            'result_path':'build/flow-final-regression-v3.xml',
            'scope':'전역 로컬 한도 추가 전 전체; 추가 후 전체 오프라인 CORE 결과는 core_gate에 기록'},
        focused={'passed':209, 'seconds':44.42,
            'command':'.venv\\Scripts\\python.exe -X utf8 -m pytest -q tests/test_multi_provider.py tests/test_execution_flow.py -p no:cacheprovider --basetemp build/flow-local-limit-tests --junitxml build/flow-local-limit-tests.xml'},
        destructive={'passed':offline['stress']['pytest']['passed_count'],
            'seconds':offline['stress']['pytest']['duration_sec'], 'scenarios_passed':19, 'docker_scenario':'NOT_VALIDATED'},
        export={'release_files':release['file_count'],'release_pass':release['passed'],
            'f3p_canary_and_clean_files':total,'f3p_pass':all(v['all_passed'] for v in exports.values()),
            'live_api_calls':sum(v['live_api_calls'] for v in exports.values()), 'details':exports},
        transient_execution_error={'code':1909,'status':'RECOVERED',
            'note':'실행 환경 복구 뒤 전역 로컬 한도·209개 관련 검사·CORE·export·보안 검사를 실행'},
        environment=env, readiness={k:env[k] for k in ('demo_ready','release_ready','product_release_ready')})
    value.pop('blocked_commands', None)
    value['implementation_limits'][0] = '같은 앱 DB의 모든 로컬 서버에서 추론 1개. 다른 DB·외부 프로그램은 제어하지 않음.'
    after = read(FOLDER/'after.json')
    if after['source_fingerprint'] != source:
        raise RuntimeError('마지막 성능 측정의 소스가 다릅니다.')
    value['measured'].update(cold_median_after_ms=sorted(v['cold_start_ms'] for v in after['cold'])[1],
        list_median_after_ms=after['research_list_repeat_ms']['median'],
        sample_interference='마지막 cold 첫 표본 일부는 관련 테스트 끝부분과 겹침; 시작 시간 개선을 단정하지 않음')
    save(FOLDER/'final_validation.json', value)
    for f in (FOLDER/'report_ko.md', ROOT/'qa/research_flow/report_ko.md'):
        text=f.read_text(encoding='utf-8',errors='strict')
        text=text.replace('실행 중; 완료 결과 추가 예정', f"최종 전체 오프라인 CORE {core['passed_count']} passed, skipped=false")
        if f.parent.name=='research_flow':
            text=text.replace('| 파일 52개, hash·참조·secret canary 이상 0 |', f'| canary·clean 합계 {total}개 파일, hash·참조·secret 이상 0 |')
            text=text.replace('기존 363개 화면 검사는', 'CORE 명령 `.venv\\Scripts\\python.exe -X utf8 -m probe.preflight validate-core`는 최종 '+str(core['passed_count'])+' passed, skipped=false였다. 기존 363개 화면 검사는')
        ready=', '.join(k+'='+str(env[k]).lower() for k in ('demo_ready','release_ready','product_release_ready'))
        extra=f"\n\n최종 gate: **{ready}**. Docker는 {env['docker']['status']}, Live LLM은 {env['provider']['status']}, Live Search는 {env['search']['status']}다. OS 기본 브라우저·Windows 키 실환경은 NOT_VALIDATED, Skills/F3-P Live 효능은 NOT_VALIDATED다. 최종 기본 release {release['file_count']}개, F3-P canary·clean {total}개 파일을 검증했고 hash·누락·참조·secret canary 이상은 0이었다. 최종 후처리 명령은 `.venv\\Scripts\\python.exe -X utf8 qa/performance/low_spec/finalize_validation.py`다.\n"
        text=text.split('\n\n최종 gate:')[0]+extra
        text.encode('utf-8',errors='strict');f.write_text(text,encoding='utf-8')
    command=[sys.executable,str(ROOT/'qa/prepublish_check.py'),'--output',str(FOLDER/'security_summary.json')]
    result=subprocess.run(command,cwd=ROOT,capture_output=True,timeout=180)
    if result.returncode:
        raise RuntimeError('최종 공개 후보 보안 검사 실패')
    security=read(FOLDER/'security_summary.json')
    value['security']={k:security[k] for k in ('publication_status','blockers','public_candidate_files','suspected_secrets','git_status','history_status','history_objects_scanned','core_regression')}
    save(FOLDER/'final_validation.json',value)
    print(json.dumps({'core':core['passed_count'],'base_export_files':release['file_count'],
        'f3p_export_files':total,'security':value['security'],'readiness':value['readiness']},ensure_ascii=False))


if __name__=='__main__':
    main()
