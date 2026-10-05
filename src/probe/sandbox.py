"""신뢰하지 않는 Python은 Docker에서만 실행한다. 호스트 실행 대체 경로는 없다."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from pydantic import ValidationError

from .real_schemas import PythonExecuteArgs
from .schemas import ContextRef, RefType, ToolRequest, ToolResult, new_id
from .service import StateService, EntityNotFoundError
from .storage import sha256_file, UnsafeWorkspacePathError, DatasetIntegrityError, ArtifactIntegrityError
from .sandbox_io import SUPERVISOR, SandboxFailure, execute_container


class SandboxUnavailableError(Exception):
    """Docker가 없거나 데몬에 연결할 수 없다."""


def clean_environment() -> dict[str, str]:
    """Docker CLI에 운영체제 경로만 전달하고 제공사 자격 증명은 제외한다."""
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def docker_environment() -> dict[str, str]:
    value = clean_environment()
    allowed = {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS", "DOCKER_TLS_VERIFY",
               "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_RUNTIME_DIR"}
    value.update({key: item for key, item in os.environ.items() if key.upper() in allowed})
    return value


def container_running(name):
    """데몬이 확인한 실행 여부만 반환하고 접속 실패는 미확정으로 남긴다."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) or shutil.which("docker") is None:
        return None
    try:
        result = subprocess.run(["docker", "container", "inspect", "--format", "{{json .State}}", name],
            capture_output=True, timeout=5, check=False, env=docker_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode == 0:
            value = json.loads(result.stdout.decode("utf-8", errors="strict"))
            if type(value.get("Running")) is bool and type(value.get("Restarting", False)) is bool:
                return value["Running"] or value.get("Restarting", False)
        elif "no such container" in result.stderr.decode("utf-8", errors="replace").lower() or "no such object" in result.stderr.decode("utf-8", errors="replace").lower():
            return False
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        pass
    return None


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, timeout=5, check=False, env=docker_environment()).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class PythonSandboxTool:
    name = "python.execute"
    version = "1.0"
    image = "probe-sandbox:3.13"

    def __init__(self, state: StateService, contract_id: str):
        self.state, self.contract_id = state, contract_id

    def _input_mounts(self, research_id: str, refs: list[ContextRef]) -> list[str]:
        mounts = []
        for ref in refs:
            if not re.fullmatch(r"[A-Za-z0-9-]+", ref.id):
                raise UnsafeWorkspacePathError("invalid artifact ID")
            if ref.type == RefType.dataset:
                record = self.state.dataset_record(ref.id, research_id)
                path = self.state.workspace.path(research_id, record["stored_path"])
                target = f"/work/input/{ref.id}.csv"
            elif ref.type == RefType.artifact:
                record = self.state.file_artifact(ref.id, research_id)
                path = self.state.workspace.path(research_id, record["relative_path"])
                target = f"/work/input/{ref.id}{path.suffix}"
            else:
                raise UnsafeWorkspacePathError("sandbox accepts dataset or artifact refs only")
            mounts += ["--mount", f"type=bind,src={path},dst={target},readonly"]
        return mounts

    @classmethod
    def build_command(cls, script: Path, output: Path, input_mounts: list[str]) -> list[str]:
        container_name = "probe-" + script.stem.split("-")[1].lower()
        return ["docker", "run", "-i", "--name", container_name, "--pull=never", "--network=none",
                "--user=65534:65534", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=64",
                "--memory=512m", "--cpus=1", "--tmpfs", "/tmp:rw,nosuid,size=16m",
                "--workdir=/work", "--env", "PYTHONNOUSERSITE=1",
                "--mount", f"type=bind,src={script},dst=/work/script/analysis.py,readonly",
                "--tmpfs", "/work/output:rw,nosuid,nodev,noexec,size=10000000,mode=1777",
                *input_mounts, cls.image, "python", "-I", "-c", SUPERVISOR]

    def run(self, request: ToolRequest) -> ToolResult:
        try:
            args = PythonExecuteArgs.model_validate(request.args)
            mounts = self._input_mounts(request.research_id, args.input_artifact_refs)
        except (ValidationError, UnsafeWorkspacePathError, DatasetIntegrityError,
                ArtifactIntegrityError, EntityNotFoundError, ValueError, OSError) as exc:
            return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                              error=f"INVALID_INPUT_ARTIFACT: {exc}")
        if not docker_available():
            return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                              error="DOCKER_UNAVAILABLE: Docker CLI or daemon is unavailable")
        script_id = new_id("ART")
        script_relative = f"scripts/{script_id}-analysis.py"
        script = self.state.workspace.path(request.research_id, script_relative)
        code_bytes = args.code.encode("utf-8", errors="strict")
        script.write_bytes(code_bytes)
        self.state.register_file_artifact(script_id, request.research_id, self.contract_id,
                                          "PYTHON_SCRIPT", script_relative, "tool", request.request_id,
                                          sha256_file(script))
        output_relative = f"results/{request.request_id}"
        output = self.state.workspace.path(request.research_id, output_relative)
        output.mkdir(parents=True, exist_ok=False)
        if os.name != "nt":
            output.chmod(0o777)
        command = self.build_command(script, output, mounts)
        from .analysis_process import _process_record
        process_record = {"pid": os.getpid(), "research_id": request.research_id, "container": command[command.index('--name') + 1]}
        _process_record(self.state, request.request_id, {**process_record, "status": "RUNNING"})
        started = time.perf_counter()
        try:
            from .analysis_process import check_boundary, deadline
            contract, _ = self.state.contract(self.contract_id)
            timeout = min(args.timeout_sec, max(0, deadline(self.state, contract) - time.monotonic()))
            value = execute_container(command, output, timeout, boundary=lambda: check_boundary(self.state, request.research_id))
            stdout, stderr = value["stdout"], value["stderr"]
            _process_record(self.state, request.request_id, {**process_record, "status": "COMPLETED"})
        except (SandboxFailure, OSError, subprocess.TimeoutExpired) as exc:
            if output.resolve().is_relative_to(self.state.workspace.research_root(request.research_id).resolve()):
                shutil.rmtree(output)
            code = getattr(exc, "code", "TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired) else "DOCKER_UNAVAILABLE")
            if code == "ANALYSIS_TERMINATION_FAILED":
                from .control_plane import ControlError
                _process_record(self.state, request.request_id, {**process_record, "status": "TERMINATION_FAILED"})
                raise ControlError(code) from exc
            _process_record(self.state, request.request_id, {**process_record, "status": "CANCELLED"})
            return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name, error=code,
                result={"exit_code": None, "stdout": "", "stderr": "", "runtime_ms": (time.perf_counter()-started)*1000, "generated_artifacts": []})
        except BaseException:
            _process_record(self.state, request.request_id, {**process_record, "status": "CANCELLED"})
            if output.resolve().is_relative_to(self.state.workspace.research_root(request.research_id).resolve()):
                shutil.rmtree(output)
            raise
        runtime_ms = (time.perf_counter() - started) * 1000
        generated = []
        artifacts = [ContextRef(type=RefType.artifact, id=script_id)]
        total_size = 0
        for path in sorted(output.rglob("*")):
            if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
                shutil.rmtree(output)
                return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                                  error="SANDBOX_VIOLATION: output path escaped")
            if not path.is_file():
                continue
            total_size += path.stat().st_size
            if total_size > 10_000_000:
                shutil.rmtree(output)
                return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                                  error="OUTPUT_LIMIT: generated files exceed limit")
            artifact_id = new_id("ART")
            relative = path.relative_to(self.state.workspace.research_root(request.research_id)).as_posix()
            generated.append({"artifact_id": artifact_id, "relative_path": relative})
            artifacts.append(ContextRef(type=RefType.artifact, id=artifact_id))
        structured = None
        result_file = output / "result.json"
        if result_file.is_file():
            try:
                structured = json.loads(result_file.read_text(encoding="utf-8", errors="strict"))
            except (UnicodeError, ValueError):
                shutil.rmtree(output)
                return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                                  error="INVALID_RESULT_JSON: result.json is not UTF-8 JSON")
        from .database import transaction
        with transaction(self.state._db):
            for item in generated:
                path = self.state.workspace.path(request.research_id, item["relative_path"])
                self.state.register_file_artifact(item["artifact_id"], request.research_id, self.contract_id,
                    "PYTHON_RESULT", item["relative_path"], "tool", request.request_id, sha256_file(path))
        return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name,
                          result={"exit_code": 0, "stdout": stdout, "stderr": stderr,
                                  "runtime_ms": runtime_ms, "generated_artifacts": generated,
                                  "structured_result": structured}, artifacts=artifacts)
