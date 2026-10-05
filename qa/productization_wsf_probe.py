"""실제 Windows Script Host로 실행 파일 구문·경로 준비를 검사한다."""
import json
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from probe.preflight import _source_fingerprint
from probe.sandbox import clean_environment


ROOT = Path(__file__).resolve().parents[1]


def main():
    original = (ROOT / "Probe.wsf").read_text(encoding="utf-8", errors="strict")
    root_line = "root = files.GetParentFolderName(WScript.ScriptFullName)"
    dispatch_line = "shell.Run command, 0, False"
    assert original.count(root_line) == original.count(dispatch_line) == 1
    # 사용자 자료를 열지 않고 실제 구문과 가상환경 경로까지만 확인한다.
    code = original.replace(root_line, 'root = "' + str(ROOT).replace('"', '""') + '"').replace(dispatch_line, 'WScript.Echo "PROBE_WSF_PREPARED"')
    code = "\n".join('  WScript.Echo "PROBE_WSF_MISSING_RUNTIME"' if line.strip().startswith("MsgBox ") else line for line in code.splitlines()) + "\n"
    data = code.encode("utf-8", errors="strict")
    folder = ROOT / "build/productization-wsf" / uuid4().hex
    folder.mkdir(parents=True)
    target = folder / "launcher-probe.wsf"
    target.write_bytes(data)
    executable = shutil.which("cscript.exe")
    observed = None
    if executable:
        try:
            observed = subprocess.run([executable, "//Nologo", str(target)], capture_output=True,
                                      env=clean_environment(), timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    passed = bool(observed and observed.returncode == 0 and b"PROBE_WSF_PREPARED" in observed.stdout)
    result = {"passed": passed, "status": "VALIDATED" if passed else "NOT_VALIDATED", "source_fingerprint": _source_fingerprint(),
              "script_host_present": bool(executable), "actual_script_host": True if observed else False,
              "exit_code": observed.returncode if observed else None, "production_launch_dispatched": False,
              "error_code": "WSH_SETTINGS_ACCESS_DENIED" if observed and b"Access is denied" in observed.stderr + observed.stdout else None,
              "diagnostic": ((observed.stdout + observed.stderr).decode("utf-8", errors="replace")[:2000] if observed else None),
              "explorer_file_association": "NOT_VALIDATED", "paid_live_calls": 0,
              "note": "실제 cscript로 production WSF의 구문·경로 준비 검사를 시도했다. QA 복사본의 root를 실제 저장소로 고정하고 마지막 실행을 성공 표식으로, MsgBox를 console 표식으로 바꿨다. 실행이 막히면 구문 통과·사용자 작업대 실행·파일 연결을 주장하지 않는다."}
    record = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    (ROOT / "qa/results/productization_wsf_results.json").write_bytes(record)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
