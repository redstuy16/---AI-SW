"""계산 자식에는 입력 복사본과 임시 출력만 전달하며 DB 저장은 부모가 담당한다."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from .control_plane import ControlError, ControlBoundary, process_alive, process_identity, process_owned
from .database import to_json, transaction
from .schemas import ResearchContract, ToolRequest, ToolResult
from .storage import sha256_file
from .sandbox import clean_environment


def deadline(state, contract):
    spent = state._db.execute("SELECT COALESCE(SUM(latency_ms),0) FROM agent_runs WHERE contract_id=?", (contract.contract_id,)).fetchone()[0]
    spent += state._db.execute("SELECT COALESCE(SUM(tc.latency_ms),0) FROM tool_calls tc JOIN agent_runs ar USING(agent_run_id) WHERE ar.contract_id=?", (contract.contract_id,)).fetchone()[0]
    remaining = contract.constraints.max_runtime_sec - spent / 1000
    if state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_runs'").fetchone():
        row = state._db.execute("SELECT started_at,status,snapshot FROM control_runs WHERE research_id=?", (contract.research_id,)).fetchone()
        if row and row[0] and row[1] in {'STARTING', 'RUNNING', 'RESUMING', 'PAUSE_REQUESTED', 'STOP_REQUESTED'}:
            from .control_runtime import snapshot_remaining
            remaining = min(remaining, snapshot_remaining(json.loads(row[2]), row[0]))
    return time.monotonic() + max(0, remaining)


def check_boundary(state, rid):
    callback = getattr(state, "analysis_boundary", None)
    if callback:
        callback()
    elif state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_runs'").fetchone():
        row = state._db.execute("SELECT status FROM control_runs WHERE research_id=?", (rid,)).fetchone()
        if row and row[0] in {"PAUSE_REQUESTED", "STOP_REQUESTED"}:
            raise ControlBoundary(row[0])


def _process_record(state, identity, value):
    value = {**value, "owner_pid": os.getpid(), "owner_birth": process_identity(os.getpid()), "pid_birth": process_identity(value.get("pid"))}
    if state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_configs'").fetchone():
        with transaction(state._db):
            state._db.execute("INSERT INTO control_configs VALUES('analysis_process',?,1,?) ON CONFLICT(kind,id) DO UPDATE SET revision=revision+1,payload=excluded.payload", (identity, to_json(value)))


def reconcile_processes(store):
    """종료를 확인한 실행만 회수하며 Docker 접속과 확인은 쓰기 잠금 밖에서 한다."""
    if store.db.in_transaction:
        return
    from .sandbox import container_running
    for row in store.db.execute("SELECT id,payload FROM control_configs WHERE kind='analysis_process'").fetchall():
        value = json.loads(row["payload"])
        if value.get("status") not in {"RUNNING", "TERMINATION_FAILED"}:
            continue
        if value["status"] == "RUNNING" and process_owned(value.get("owner_pid", value.get("pid")), value.get("owner_birth")):
            continue
        container = value.get("container")
        if container:
            if container_running(container) is not False:
                continue
        elif process_owned(value.get("pid"), value.get("pid_birth")):
            continue
        recovered = {**value, "status": "CANCELLED", "reason": "PROCESS_ABSENT_CONFIRMED"}
        with store.transaction():
            changed = store.db.execute("UPDATE control_configs SET revision=revision+1,payload=? WHERE kind='analysis_process' AND id=? AND payload=?",
                (to_json(recovered), row["id"], row["payload"])).rowcount
            if not changed:
                continue
            store.audit(value["research_id"], "ANALYSIS_RECOVERED", {"id": row["id"], "reason": recovered["reason"]})
            for job in store.db.execute("SELECT id,payload FROM control_configs WHERE kind='resource_job' AND json_extract(payload,'$.research_id')=? AND json_extract(payload,'$.resource')='heavy-analysis' AND json_extract(payload,'$.status')='RUNNING'", (value["research_id"],)).fetchall():
                payload = json.loads(job["payload"])
                if payload.get("pid") != value.get("owner_pid", value.get("pid")) or payload.get("pid_birth") and value.get("owner_birth") and payload["pid_birth"] != value["owner_birth"]:
                    continue
                payload.update(status="CANCELLED", reason="PROCESS_ABSENT_CONFIRMED")
                store.db.execute("UPDATE control_configs SET revision=revision+1,payload=? WHERE kind='resource_job' AND id=?", (to_json(payload), job["id"]))


def terminate(process):
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        raise ControlError("ANALYSIS_TERMINATION_FAILED") from None
    if process.poll() is None:
        raise ControlError("ANALYSIS_TERMINATION_FAILED")


def run_tool(state, contract, request, expires):
    if state._db.in_transaction:
        raise ControlError("COMPUTE_IN_TRANSACTION_BLOCKED")
    identity, process = uuid4().hex, None
    with tempfile.TemporaryDirectory(prefix="probe-analysis-") as directory:
        root = Path(directory)
        dataset_id = request.args.get("dataset_id") or request.args.get("plan", {}).get("dataset_id")
        dataset = dict(state.dataset_record(dataset_id, request.research_id))
        shutil.copyfile(state.workspace.path(request.research_id, dataset["stored_path"]), root / "input.csv")
        dataset["stored_path"] = "input.csv"
        value = {"contract": contract.model_dump(mode="json"), "request": request.model_dump(mode="json"),
                 "dataset": dataset, "skills": bool(getattr(state, "verified_analysis_skills_enabled", False))}
        (root / "input.json").write_bytes(to_json(value).encode("utf-8", errors="strict"))
        env = clean_environment()
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        try:
            check_boundary(state, request.research_id)
            if time.monotonic() >= expires:
                raise ControlError("TIME_LIMIT")
            process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "probe.analysis_process", str(root)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            _process_record(state, identity, {"pid": process.pid, "research_id": request.research_id, "status": "RUNNING"})
            while process.poll() is None:
                check_boundary(state, request.research_id)
                if time.monotonic() >= expires:
                    raise ControlError("TIME_LIMIT")
                time.sleep(.05)
            if process.returncode:
                raise ControlError("ANALYSIS_PROCESS_FAILED")
            output = root / "output.json"
            if not output.is_file() or output.stat().st_size > 10_000_000:
                raise ControlError("ANALYSIS_OUTPUT_INVALID")
            result = json.loads(output.read_text(encoding="utf-8", errors="strict"))
            parsed = ToolResult.model_validate(result["result"])
            if parsed.request_id != request.request_id or parsed.tool_name != request.tool_name:
                raise ControlError("ANALYSIS_OUTPUT_INVALID")
            total = 0
            for record in result["files"]:
                path = root / record["relative"]
                if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                    raise ControlError("ANALYSIS_OUTPUT_INVALID")
                total += path.stat().st_size
                if total > 10_000_000 or sha256_file(path) != record["sha256"]:
                    raise ControlError("ANALYSIS_OUTPUT_INVALID")
            check_boundary(state, request.research_id)
            for record in result["files"]:
                path = root / record["relative"]
                target = state.workspace.path(request.research_id, record["relative"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            state.computed_artifacts = result["files"]
            _process_record(state, identity, {"pid": process.pid, "research_id": request.research_id, "status": "COMPLETED"})
            return parsed
        except BaseException:
            if process:
                try:
                    terminate(process)
                except ControlError:
                    _process_record(state, identity, {"pid": process.pid, "research_id": request.research_id, "status": "TERMINATION_FAILED"})
                    raise
                _process_record(state, identity, {"pid": process.pid, "research_id": request.research_id, "status": "CANCELLED"})
            raise


class _Inputs:
    def __init__(self, root, value):
        self.root, self.value, self.files = root, value, []
        self.workspace = self
        self.verified_analysis_skills_enabled = value["skills"]

    def path(self, rid, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("계산 출력 경로 오류")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def dataset_record(self, identity, rid):
        if identity != self.value["dataset"]["dataset_id"]:
            raise ValueError("계산 입력 소유 범위 오류")
        return self.value["dataset"]

    def contract(self, identity):
        contract = ResearchContract.model_validate(self.value["contract"])
        if identity != contract.contract_id:
            raise ValueError("계산 계약 오류")
        return contract, None

    def register_file_artifact(self, identity, rid, contract, kind, relative, producer_type, producer, digest):
        self.files.append({"id": identity, "kind": kind, "relative": relative, "sha256": digest})


def main(directory):
    from .real_tools import StatsTool, VerifiedAnalysisSkillTool, VisualizationTool
    root = Path(directory).resolve()
    value = json.loads((root / "input.json").read_text(encoding="utf-8", errors="strict"))
    state = _Inputs(root, value)
    request = ToolRequest.model_validate(value["request"])
    tool = {"stats.run": StatsTool, "analysis.skill": VerifiedAnalysisSkillTool, "visualization.render": VisualizationTool}[request.tool_name](state, value["contract"]["contract_id"])
    result = tool.run(request)
    encoded = to_json({"result": result.model_dump(mode="json"), "files": state.files}).encode("utf-8", errors="strict")
    (root / "output.json").write_bytes(encoded)


if __name__ == "__main__":
    main(sys.argv[1])
