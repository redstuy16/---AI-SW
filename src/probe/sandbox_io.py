"""바이너리 콘솔·tmpfs 출력의 제한된 회수와 컨테이너 정리."""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import threading
import time


class SandboxFailure(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


SUPERVISOR = """import subprocess,selectors,sys,json,base64,os
mounts=open('/proc/mounts').read().splitlines()
valid=any(line.split()[1:3]==['/work/output','tmpfs'] for line in mounts) and os.statvfs('/work/output').f_blocks*os.statvfs('/work/output').f_frsize<=10010000
if not valid:
 print(json.dumps({'exit_code':0,'error':'SANDBOX_LIMIT_UNSUPPORTED','stdout':'','stderr':''}),flush=True)
 sys.stdin.buffer.readline();sys.exit(1)
p=subprocess.Popen(['python','-I','/work/script/analysis.py'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
s=selectors.DefaultSelector()
s.register(p.stdout,selectors.EVENT_READ,'stdout');s.register(p.stderr,selectors.EVENT_READ,'stderr')
out={'stdout':bytearray(),'stderr':bytearray()};error=None
while s.get_map():
 for key,_ in s.select(.1):
  chunk=key.fileobj.read1(65536)
  if not chunk:s.unregister(key.fileobj);continue
  if sum(map(len,out.values()))+len(chunk)>100000:
   error='OUTPUT_LIMIT';p.kill();s.close();break
  out[key.data].extend(chunk)
 if error:break
code=p.wait()
print(json.dumps({'exit_code':code,'error':error,**{k:base64.b64encode(v).decode('ascii') for k,v in out.items()}}),flush=True)
sys.stdin.buffer.readline()
"""


def _binary_command(command, limit, timeout):
    from .sandbox import docker_environment
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=docker_environment(), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    data, done, error = bytearray(), threading.Event(), []
    def read():
        try:
            while part := process.stdout.read1(64 * 1024):
                if len(data) + len(part) > limit:
                    error.append("OUTPUT_LIMIT")
                    break
                data.extend(part)
        finally:
            done.set()
    threading.Thread(target=read, daemon=True).start()
    try:
        if not done.wait(timeout):
            raise SandboxFailure("TIMEOUT")
        if error:
            raise SandboxFailure(error[0])
        if process.wait(timeout=2):
            raise SandboxFailure("DOCKER_UNAVAILABLE")
        return bytes(data)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def collect_tar(data, output):
    """링크·특수 파일을 거절한 뒤 합계 한도 안에서만 회수한다."""
    total, records, names, members = 0, [], set(), 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for item in archive:
            members += 1
            name = Path(item.name)
            if item.isdir() and item.name in {".", "./"}:
                continue
            if members > 1024 or item.issym() or item.islnk() or name.is_absolute() or ".." in name.parts or "\\" in item.name or ":" in item.name or name in names:
                raise SandboxFailure("SANDBOX_VIOLATION")
            names.add(name)
            path = output / name
            if not path.resolve().is_relative_to(output.resolve()):
                raise SandboxFailure("SANDBOX_VIOLATION")
            if item.isdir():
                continue
            if not item.isfile() or item.size < 0:
                raise SandboxFailure("SANDBOX_VIOLATION")
            total += item.size
            if total > 10_000_000:
                raise SandboxFailure("OUTPUT_LIMIT")
            records.append((item, path))
        for item, path in records:
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(item) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target, 64 * 1024)


def decode_console(data):
    try:
        value = json.loads(data.decode("utf-8", errors="strict"))
        stdout = base64.b64decode(value["stdout"], validate=True)
        stderr = base64.b64decode(value["stderr"], validate=True)
        if len(stdout) + len(stderr) > 100000:
            raise SandboxFailure("OUTPUT_LIMIT")
        value.update(stdout=stdout.decode("utf-8", errors="strict"), stderr=stderr.decode("utf-8", errors="strict"))
        return value
    except SandboxFailure:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise SandboxFailure("INVALID_UTF8_OUTPUT") from None


def execute_container(command, output, timeout, boundary=None):
    from .sandbox import docker_environment
    name = command[command.index("--name") + 1]
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=docker_environment(), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        records, done = [], threading.Event()
        def read():
            try:
                records.append(process.stdout.readline(180001))
            finally:
                done.set()
        threading.Thread(target=read, daemon=True).start()
        expires = time.monotonic() + timeout
        while not done.wait(.05):
            if boundary:
                boundary()
            if time.monotonic() >= expires:
                raise SandboxFailure("TIMEOUT")
        if not records or len(records[0]) > 180000:
            raise SandboxFailure("OUTPUT_LIMIT")
        value = decode_console(records[0])
        if value.get("error"):
            raise SandboxFailure(value["error"])
        if value["exit_code"]:
            raise SandboxFailure("OOM" if value["exit_code"] == 137 else "NON_ZERO_EXIT")
        if boundary:
            boundary()
        remaining = expires - time.monotonic()
        if remaining <= 0:
            raise SandboxFailure("TIMEOUT")
        data = _binary_command(["docker", "cp", name + ":/work/output/.", "-"], 12_000_000, min(5, remaining))
        collect_tar(data, output)
        process.stdin.write(b"DONE\n")
        process.stdin.flush()
        process.wait(timeout=2)
        return value
    finally:
        try:
            result = subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=5, env=docker_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode and process:
                raise SandboxFailure("ANALYSIS_TERMINATION_FAILED")
        except (OSError, subprocess.TimeoutExpired):
            if process:
                raise SandboxFailure("ANALYSIS_TERMINATION_FAILED") from None
        if process and process.poll() is None:
            from .analysis_process import terminate
            from .control_plane import ControlError
            try:
                terminate(process)
            except ControlError:
                raise SandboxFailure("ANALYSIS_TERMINATION_FAILED") from None
