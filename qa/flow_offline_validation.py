"""현재 소스의 기존 파괴적 검사·반복 데모·복구·출시 자료를 실제 검증한다."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import qa_day1 as qa
from probe.preflight import _source_fingerprint, record_qa_validation, environment_status


def save(path, value):
    def relative(v):
        if isinstance(v,str) and v.startswith((str(ROOT)+'\\',str(ROOT)+'/')):
            return Path(v).relative_to(ROOT).as_posix()
        if isinstance(v,dict):
            return {k:relative(x) for k,x in v.items()}
        if isinstance(v,list):
            return [relative(x) for x in v]
        return v
    text = json.dumps(relative(value),ensure_ascii=False,indent=2)+'\n'
    text.encode('utf-8', errors='strict')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def main():
    qa.QA = ROOT / 'build/flow-final-qa'
    qa.QA.mkdir(parents=True, exist_ok=True)
    stress = qa.stress_suite()
    print(json.dumps({'stress': stress['pytest'], 'scenarios': {k:v['status'] for k,v in stress['scenarios'].items()}}), flush=True)
    repeat, exemplars = qa.demo_repeatability()
    fresh = qa.BUILD / 'fresh-process'
    for phase in ('crash', 'resume'):
        process = qa.run([sys.executable, str(ROOT / 'qa/qa_day1.py'), '--probe', phase, '--root', str(fresh)])
        if process.returncode:
            raise RuntimeError('새 프로세스 복구 실패: ' + phase + ' ' + process.stderr[-1000:])
    resume = json.loads((fresh / 'resume.json').read_text(encoding='utf-8'))
    release = qa.validate_release(exemplars['A'])
    record_qa_validation('stress', stress['all_executable_passed'] and resume['passed'], {'scenarios':len(stress['scenarios']), 'fresh_process_resume':resume['passed']})
    record_qa_validation('artifact', release['passed'], {'file_count':release['file_count']})
    value = {'source_fingerprint':_source_fingerprint(), 'stress':stress, 'demo_repeatability':repeat,
             'fresh_process_resume':resume, 'release':release, 'environment':environment_status()}
    save(ROOT / 'qa/performance/low_spec/offline_validation.json', value)
    print(json.dumps({'stress':stress['all_executable_passed'], 'repeat':repeat['passed'], 'resume':resume['passed'], 'release':release['passed'], 'files':release['file_count']}))
    if not (stress['all_executable_passed'] and all(repeat['passed'].values()) and resume['passed'] and release['passed']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
