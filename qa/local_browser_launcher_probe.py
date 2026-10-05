"""실제 OS 기본 브라우저의 호출·자동 교환을 비밀 없는 표식으로 확인한다."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from uuid import uuid4

from probe.preflight import VALIDATION_DIR, _source_fingerprint, native_environment_id
from probe.schemas import utc_now


ROOT = Path(__file__).resolve().parents[1]
CODE = '''import sys,webbrowser
from probe.workbench import main,OwnerSession,WorkbenchAPI
original_open,original_exchange,original_request=webbrowser.open,OwnerSession.exchange,WorkbenchAPI.request
def observed_open(*args,**kwargs):
 try: result=original_open(*args,**kwargs)
 except Exception: result=False
 print('QA_BROWSER_OPEN:'+str(bool(result)),flush=True)
 return result
def observed_exchange(self,*args,**kwargs):
 result=original_exchange(self,*args,**kwargs)
 print('QA_BOOTSTRAP_EXCHANGED',flush=True)
 return result
def observed_request(self,method,path,body=None):
 result=original_request(self,method,path,body)
 if method=='GET' and path=='/api/control/research' and result.status==200: print('QA_OWNER_LIST_READ',flush=True)
 return result
webbrowser.open=observed_open
OwnerSession.exchange=observed_exchange
WorkbenchAPI.request=observed_request
sys.argv=['probe.workbench',*sys.argv[1:]]
main()
'''


def main():
    folder = ROOT / "build/browser-auth-native" / uuid4().hex
    folder.mkdir(parents=True)
    command = [sys.executable, "-c", CODE, str(folder / "state.sqlite"), str(folder / "workspace"), "--mode", "DEMO"]
    messages = queue.Queue()
    child = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace")
    def read():
        for line in child.stdout:
            # 일반 시작 표식만 전달하고 대체 링크는 수집하지 않는다.
            if line.startswith("QA_"):
                messages.put(line.strip())
            elif line.startswith("Probe: http://127.0.0.1:"):
                messages.put(line.strip())
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    opened, exchanged, listed, origin = False, False, False, None
    started = time.monotonic()
    try:
        while time.monotonic() - started < 25:
            try:
                line = messages.get(timeout=0.2)
            except queue.Empty:
                if child.poll() is not None: break
                continue
            opened |= line == "QA_BROWSER_OPEN:True"
            exchanged |= line == "QA_BOOTSTRAP_EXCHANGED"
            listed |= line == "QA_OWNER_LIST_READ"
            if line.startswith("Probe: "): origin = line.removeprefix("Probe: ")
            if opened and exchanged and listed: break
    finally:
        child.terminate()
        child.wait(timeout=10)
        thread.join(timeout=2)
    result = {"passed": opened and exchanged and listed,
              "execution": "NATIVE_WINDOWS", "source_fingerprint": _source_fingerprint(),
              "environment_id": native_environment_id(), "checked_at": utc_now().isoformat(),
              "checks": {"open": opened, "exchange": exchanged, "owner_list": listed},
              "status": "VALIDATED" if opened and exchanged and listed else "NOT_VALIDATED",
              "os_browser_open_returned_true": opened, "actual_bootstrap_exchange": exchanged,
              "authenticated_research_list_request": listed, "origin": origin,
              "manual_code_input": False, "paid_live_calls": 0,
              "note": "실제 기본 브라우저 함수를 그대로 실행했다. 인증 값 대신 호출 성공·교환·인증된 목록 조회 표식만 기록했다. QA 서버는 종료했다."}
    data = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (ROOT / "qa/results/local_browser_native_results.json").write_bytes(data.encode("utf-8", errors="strict"))
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    (VALIDATION_DIR / "default_browser.json").write_bytes(data.encode("utf-8", errors="strict"))
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
