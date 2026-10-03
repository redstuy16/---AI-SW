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


class SandboxUnavailableError(Exception):
    """Docker가 없거나 데몬에 연결할 수 없다."""


def clean_environment() -> dict[str, str]:
    """Docker CLI에 운영체제 경로만 전달하고 제공사 자격 증명은 제외한다."""
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, timeout=5, check=False, env=clean_environment()).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class PythonSandboxTool:
    name = "python.execute"
    version = "1.0"
    image = "htrsa-sandbox:3.13"

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
        container_name = "htrsa-" + script.stem.split("-")[1].lower()
        return ["docker", "run", "--rm", "--name", container_name, "--pull=never", "--network=none",
                "--user=65534:65534", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=64",
                "--memory=512m", "--cpus=1", "--tmpfs", "/tmp:rw,nosuid,size=16m",
                "--workdir=/work", "--env", "PYTHONNOUSERSITE=1",
                "--mount", f"type=bind,src={script},dst=/work/script/analysis.py,readonly",
                "--mount", f"type=bind,src={output},dst=/work/output",
                *input_mounts, cls.image, "python", "-I", "/work/script/analysis.py"]

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
        output.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            output.chmod(0o777)
        command = self.build_command(script, output, mounts)
        started = time.perf_counter()
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=args.timeout_sec, check=False, env=clean_environment())
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(["docker", "rm", "-f", command[command.index("--name") + 1]],
                               capture_output=True, timeout=5, check=False, env=clean_environment())
            except (OSError, subprocess.TimeoutExpired):
                pass
            return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                              error="TIMEOUT: sandbox wall-clock limit exceeded",
                              result={"exit_code": None, "stdout": "", "stderr": "", "runtime_ms": (time.perf_counter()-started)*1000,
                                      "generated_artifacts": []})
        except OSError as exc:
            return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                              error=f"DOCKER_UNAVAILABLE: {exc}")
        runtime_ms = (time.perf_counter() - started) * 1000
        stdout, stderr = completed.stdout, completed.stderr
        if len(stdout) + len(stderr) > 100_000:
            return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                              error="OUTPUT_LIMIT: console output exceeds limit")
        if completed.returncode != 0:
            code = "OOM" if completed.returncode == 137 else "DOCKER_UNAVAILABLE" if completed.returncode == 125 else "NON_ZERO_EXIT"
            return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                              error=f"{code}: exit {completed.returncode}",
                              result={"exit_code": completed.returncode, "stdout": stdout,
                                      "stderr": stderr, "runtime_ms": runtime_ms, "generated_artifacts": []})
        generated = []
        artifacts = [ContextRef(type=RefType.artifact, id=script_id)]
        total_size = 0
        for path in sorted(output.rglob("*")):
            if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
                return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                                  error="SANDBOX_VIOLATION: output path escaped")
            if not path.is_file():
                continue
            total_size += path.stat().st_size
            if total_size > 10_000_000:
                return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                                  error="OUTPUT_LIMIT: generated files exceed limit")
            artifact_id = new_id("ART")
            relative = path.relative_to(self.state.workspace.research_root(request.research_id)).as_posix()
            self.state.register_file_artifact(artifact_id, request.research_id, self.contract_id,
                                              "PYTHON_RESULT", relative, "tool", request.request_id,
                                              sha256_file(path))
            generated.append({"artifact_id": artifact_id, "relative_path": relative})
            artifacts.append(ContextRef(type=RefType.artifact, id=artifact_id))
        structured = None
        result_file = output / "result.json"
        if result_file.is_file():
            try:
                structured = json.loads(result_file.read_text(encoding="utf-8", errors="strict"))
            except (UnicodeError, ValueError):
                return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                                  error="INVALID_RESULT_JSON: result.json is not UTF-8 JSON")
        return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name,
                          result={"exit_code": 0, "stdout": stdout, "stderr": stderr,
                                  "runtime_ms": runtime_ms, "generated_artifacts": generated,
                                  "structured_result": structured}, artifacts=artifacts)
