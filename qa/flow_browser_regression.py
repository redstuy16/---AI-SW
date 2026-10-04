"""기존 Chrome 검사를 수정하지 않고 순차 실행해 현재 결과를 기록한다."""
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    commands = [[sys.executable, '-X', 'utf8', 'qa/' + name] for name in
                ('gui_visual_fixture.py','cycle5_visual_fixture.py','product_visual_fixture.py')]
    commands += [['node', 'qa/' + name] for name in (
        'gui_visual_qa.cjs', 'product_visual_qa.cjs', 'multi_provider_visual_qa.cjs',
        'cycle5_visual_qa.cjs', 'productization_visual_qa.cjs', 'connection_ux_visual_qa.cjs',
        'live_api_recording_visual_qa.cjs')]
    commands += [['node','qa/live_api_recording_visual_qa.cjs','--settings-layout'],
                 ['node','qa/local_browser_auth_qa.cjs'], ['node','qa/hardening_visual_qa.cjs']]
    records = []
    for command in commands:
        started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=180)
        record = {'command': ['.venv/Scripts/python.exe' if k == sys.executable else k for k in command],
                  'exit_code':result.returncode,'seconds':round(time.monotonic()-started,3)}
        if command[0]=='node' and not result.returncode:
            record['result'] = json.loads(result.stdout.strip().splitlines()[-1])
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if result.returncode:
            print(result.stderr[-1000:], file=sys.stderr)
            break
    text = json.dumps({'commands':records,'all_passed':len(records)==len(commands) and all(r['exit_code']==0 for r in records)},ensure_ascii=False,indent=2)+'\n'
    text.encode('utf-8',errors='strict')
    (ROOT / 'qa/performance/low_spec/browser_regression.json').write_text(text,encoding='utf-8')
    if len(records)!=len(commands) or any(r['exit_code'] for r in records):
        raise SystemExit(1)


if __name__=='__main__':
    main()
