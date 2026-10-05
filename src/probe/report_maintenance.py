"""현재 등록된 보고서를 고정 목록·기존 예산으로 한 연구씩 재작성한다."""
import argparse
import asyncio
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

from .control_plane import ControlError, ModelProfile, admitted_cost, process_owned, process_identity
from .database import to_json
from .research_report import _save, input_fingerprint, report_inputs, rewrite_report
from .schemas import utc_now
from .storage import sha256_file, DatasetIntegrityError, ArtifactIntegrityError

_OWNERS, _LOCK = set(), threading.RLock()


def maintenance_fingerprint(state, rid):
    """검증 실패 자료도 포함한 입력 지문으로 재개 중 입력 교체를 감지한다."""
    value = {"validation_version": 2, "state_version": state.state_version(rid)}
    for table, columns in (("research_runs", "goal,research_question"), ("datasets", "dataset_id,stored_path,sha256,status"),
                           ("artifacts", "artifact_id,relative_path,sha256,status"), ("sources", "source_id,title,abstract,metadata_hash,status"),
                           ("evidence", "evidence_id,source_id,experiment_id,claim,evidence_text,provenance_json,polarity,target_hypothesis_id,status"),
                           ("experiments", "experiment_id,dataset_id,payload_json,status")):
        rows = [dict(r) for r in state._db.execute(f"SELECT {columns} FROM {table} WHERE research_id=? ORDER BY rowid", (rid,))]
        for row in rows:
            relative = row.get("stored_path") or row.get("relative_path")
            if relative:
                try:
                    row["current_sha256"] = sha256_file(state.workspace.path(rid, relative))
                except (OSError, ValueError):
                    row["current_sha256"] = "UNREADABLE"
        value[table] = rows
    from .research_design import current_design
    value["design"] = current_design(state, rid)
    value["hypotheses"] = [{**dict(row), "criterion": state.hypothesis_criterion(rid, row["hypothesis_id"])}
        for row in state._db.execute("SELECT hypothesis_id,statement,status FROM hypotheses WHERE research_id=? ORDER BY rowid", (rid,))]
    return sha256(to_json(value).encode("utf-8", errors="strict")).hexdigest()


def targets(api):
    from .research_lifecycle import lifecycle
    rows = api.store.db.execute("SELECT c.id,r.status FROM control_configs c JOIN control_runs r ON r.research_id=c.id WHERE c.kind='ai_report' AND r.status NOT IN ('DRAFT','STARTING','RUNNING','RESUMING','PAUSED','PAUSE_REQUESTED','STOP_REQUESTED') ORDER BY c.rowid").fetchall()
    return [{"research_id": r["id"], "status": "QUEUED", "input_fingerprint": maintenance_fingerprint(api.read._state, r["id"])}
            for r in rows if lifecycle(api, r["id"])["status"] == "ACTIVE"]


def excluded_targets(api):
    from .research_lifecycle import lifecycle
    idle = {'DRAFT', 'STARTING', 'RUNNING', 'RESUMING', 'PAUSED', 'PAUSE_REQUESTED', 'STOP_REQUESTED'}
    rows = api.store.db.execute("SELECT c.id,r.status FROM control_configs c JOIN control_runs r ON r.research_id=c.id WHERE c.kind='ai_report' ORDER BY c.rowid").fetchall()
    return [{"research_id": row['id'], "reason": "RESEARCH_IN_TRASH" if lifecycle(api, row['id'])['status'] != 'ACTIVE' else "RESEARCH_NOT_IDLE"}
            for row in rows if row['status'] in idle or lifecycle(api, row['id'])['status'] != 'ACTIVE']


def admission(api, rid):
    from .product_policy import effective_snapshot, completion_budget, task_profile
    from .science_policy import prepare_science_profiles
    from .runtime_environment import interpreter_preflight
    if interpreter_preflight()["status"] != "READY":
        raise ControlError("PYTHON_ENVIRONMENT_BLOCKED")
    if api.store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status IN ('RESERVED','DISPATCHED')", (rid,)).fetchone():
        raise ControlError("NEEDS_RECONCILIATION")
    inputs = report_inputs(api.read._state, rid)
    if not inputs["evidence"] and not any(item["status"] == "VERIFIED" for item in inputs["experiments"]) and api.store.db.execute(
            "SELECT 1 FROM evidence WHERE research_id=? AND status='INVALIDATED' UNION ALL SELECT 1 FROM datasets WHERE research_id=? AND status='INVALID' LIMIT 1", (rid, rid)).fetchone():
        return {"available_usd": "0", "minimum_request_bound_usd": "0", "execution": "LOCAL_LIMITATION"}
    snapshot = effective_snapshot(api.store, rid)
    prepare_science_profiles(snapshot)
    model = task_profile(ModelProfile.model_validate(snapshot["models"]["manager"]), "report")
    connection = snapshot["connections"][model.connection_id]
    local = model.local_api_unmetered and connection["endpoint_class"] == "loopback"
    bound = Decimal(0) if local else admitted_cost(model, min(8192, model.input_byte_limit, model.context_limit - model.output_limit))
    available = completion_budget(api.store, rid, snapshot)["available_usd"]
    if bound > Decimal(available):
        raise ControlError("COMPLETION_RESERVE_BLOCKED")
    if snapshot.get("egress") == "none" and not local:
        raise ControlError("DATA_EGRESS_DENIED")
    return {"available_usd": available, "minimum_request_bound_usd": str(bound)}


def revalidate_inputs(api, rid):
    from .final_report import _validate_literature_provenance, ReportValidationError
    state = api.read._state
    for row in state._db.execute("SELECT dataset_id FROM datasets WHERE research_id=? AND status<>'INVALID'", (rid,)).fetchall():
        try:
            state.dataset_record(row[0], rid)
        except (DatasetIntegrityError, OSError, ValueError):
            state.invalidate_dataset(rid, row[0], "보고서 재검증에서 원자료 무결성 확인 실패")
    for row in state._db.execute("SELECT experiment_id,result_artifact_id FROM experiments WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchall():
        try:
            state.file_artifact(row[1], rid)
        except (ArtifactIntegrityError, OSError, ValueError):
            state._planning_commit(rid, "EXPERIMENT_INVALIDATED", "experiment", row[0], {"reason": "REPORT_REVALIDATION"},
                lambda row=row: state._invalidate_experiment_records(rid, row[0], "통계 산출물 무결성 확인 실패", reopen=False))
    for row in state._db.execute("SELECT * FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND status='VERIFIED'", (rid,)).fetchall():
        source = state._one("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, row["source_id"]))
        try:
            _validate_literature_provenance(dict(source), dict(row), state=state)
        except (ReportValidationError, ControlError, ValueError, OSError):
            state.invalidate_source(rid, row["source_id"], "보고서 재검증에서 원문 근거 확인 실패", reopen=False)


def _result(batch):
    counts = {status: sum(v["status"] == status for v in batch["items"]) for status in ("QUEUED", "RUNNING", "COMPLETED", "BLOCKED", "FAILED")}
    return {**batch, "counts": counts}


async def run_batch(api, *, dry_run=False, resume=None, provider_factory=None):
    if dry_run:
        saved = api.store.config("report_batch", resume) if resume else None
        if saved and saved["validation_version"] != 2:
            raise ControlError("REPORT_VALIDATION_VERSION_REQUIRED")
        items = json.loads(to_json(saved["items"])) if saved else targets(api)
        for item in items:
            if maintenance_fingerprint(api.read._state, item["research_id"]) != item["input_fingerprint"]:
                item.update(status="BLOCKED", reason="STALE_INPUT")
                continue
            try:
                item["budget"] = admission(api, item["research_id"])
            except ControlError as exc:
                item.update(status="BLOCKED", reason=exc.code)
        return _result({"batch_id": resume, "status": "DRY_RUN", "validation_version": 2, "items": items, "excluded": saved.get("excluded", []) if saved else excluded_targets(api), "paid_calls": 0})
    identity, owner = resume or "BATCH-" + uuid4().hex, uuid4().hex
    with api.store.transaction():
        for value in api.store.configs("report_batch"):
            with _LOCK:
                alive = value.get("owner_pid") == os.getpid() and value.get("owner_token") in _OWNERS or value.get("owner_pid") and value["owner_pid"] != os.getpid() and process_owned(value["owner_pid"], value.get("owner_birth"))
            if value["status"] == "RUNNING" and alive:
                raise ControlError("REPORT_BATCH_IN_PROGRESS")
        batch = api.store.config("report_batch", identity) if resume else {"batch_id": identity, "validation_version": 2, "created_at": utc_now().isoformat(), "items": targets(api), "excluded": excluded_targets(api)}
        if batch["validation_version"] != 2:
            raise ControlError("REPORT_VALIDATION_VERSION_REQUIRED")
        batch.update(status="RUNNING", owner_pid=os.getpid(), owner_birth=process_identity(os.getpid()), owner_token=owner)
        _save(api.store, "report_batch", identity, batch)
    with _LOCK:
        _OWNERS.add(owner)
    try:
        for item in batch["items"]:
            rid = item["research_id"]
            current = maintenance_fingerprint(api.read._state, rid)
            if current != item["input_fingerprint"]:
                item.update(status="BLOCKED", reason="STALE_INPUT")
                _save(api.store, "report_batch", identity, batch)
                continue
            if item["status"] == "COMPLETED":
                from .release import validate_report_snapshot
                try:
                    validate_report_snapshot(api.read._state, rid)
                except Exception:
                    item.update(status="BLOCKED", reason="REPORT_PUBLICATION_INVALID")
                continue
            if item["status"] in {"BLOCKED", "FAILED"}:
                if not resume or item.get("reason") == "STALE_INPUT":
                    continue
                if api.store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status IN ('RESERVED','DISPATCHED')", (rid,)).fetchone():
                    item.update(status="BLOCKED", reason="NEEDS_RECONCILIATION")
                    continue
                item.setdefault("previous_requests", []).append({k: item[k] for k in ("request_key", "request_body", "status", "reason") if k in item})
                item.update(status="QUEUED", retry_count=item.get("retry_count", 0) + 1, reason=None)
                item.pop("request_key", None)
                item.pop("request_body", None)
            try:
                revalidate_inputs(api, rid)
                item["input_fingerprint"] = maintenance_fingerprint(api.read._state, rid)
                item["budget"] = admission(api, rid)
                key = item.get("request_key") or sha256(to_json({"batch": identity, "research": rid, "validation_version": 2, "input": item["input_fingerprint"], "retry": item.get("retry_count", 0)}).encode("utf-8", errors="strict")).hexdigest()
                body = item.get("request_body") or {"idempotency_key": key, "expected_version": api.store.run(rid)["version"], "state_version": api.read._state.state_version(rid)}
                item.update(status="RUNNING", request_key=key, request_body=body)
                _save(api.store, "report_batch", identity, batch)
                result = await rewrite_report(api, rid, body, provider_factory=provider_factory)
                if result["status"] not in {"READY", "PARTIAL"}:
                    raise ControlError(result.get("error") or "REPORT_REWRITE_INTERRUPTED")
                from .release import validate_report_snapshot
                manifest = validate_report_snapshot(api.read._state, rid)
                from .report_publication import report_root
                root = report_root(api.read._state, rid)
                if "report.pdf" not in manifest["files"]:
                    raise ControlError("PDF_RENDER_FAILED")
                if result.get("error"):
                    item.update(status="BLOCKED" if result["error"] in {"PRICE_REQUIRED", "COMPLETION_RESERVE_BLOCKED", "NEEDS_RECONCILIATION", "BUDGET_EXCEEDED"} else "FAILED", reason=result["error"], report=result)
                else:
                    item.update(status="COMPLETED", report=result, revision_path=str(root), pdf_sha256=sha256_file(root / "report.pdf"))
            except ControlError as exc:
                item.update(status="BLOCKED", reason=exc.code)
            except Exception as exc:
                item.update(status="FAILED", reason=getattr(exc, "code", "REPORT_MAINTENANCE_FAILED"))
            _save(api.store, "report_batch", identity, batch)
        batch["status"] = "COMPLETED" if all(v["status"] == "COMPLETED" for v in batch["items"]) else "PARTIAL"
    except BaseException:
        batch["status"] = "INTERRUPTED"
        raise
    finally:
        batch.update(owner_pid=None, finished_at=utc_now().isoformat())
        _save(api.store, "report_batch", identity, batch)
        with _LOCK:
            _OWNERS.discard(owner)
    return _result(batch)


def main(argv=None):
    parser = argparse.ArgumentParser(description="기존 보고서의 검증 버전 2 일괄 재작성")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--resume")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.database.is_file():
        parser.error("기존 작업대 DB를 지정하세요.")
    from .workbench import WorkbenchAPI
    api = WorkbenchAPI(args.database, args.workspace, initialize_schema=not args.dry_run, launch=False)
    try:
        result = asyncio.run(run_batch(api, dry_run=args.dry_run, resume=args.resume))
        text = to_json(result)
        text.encode("utf-8", errors="strict")
        print(text)
        return 0 if result["status"] in {"COMPLETED", "DRY_RUN"} else 2
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
