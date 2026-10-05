"""제출 실행과 실패 복구가 비용·자료·실행 소유권을 보존하는지 검사한다."""
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import tomllib

import pytest

from probe.control_plane import ControlError, process_identity, process_owned
from probe.database import to_json
from probe.release import validate_report_snapshot
from probe.report_maintenance import admission, run_batch
from probe.report_publication import report_root
from probe.research_report import _save, rewrite_report
from probe.resource_queue import ResourcePool
from probe.schemas import utc_now
from test_workbench import app, configure, create
from test_research_report_flow import rig, run, request as report_request


def _rewrite_body(app, rid, key):
    return {"idempotency_key": key, "expected_version": app.store.run(rid)["version"],
            "state_version": app.read._state.state_version(rid)}


@pytest.mark.parametrize("action,expected", [("start", "STARTING"), ("resume", "RESUMING")])
def test_explicit_retry_from_failed_requires_a_new_key(app, action, expected):
    configure(app)
    rid = create(app)
    app.store.db.execute("UPDATE control_runs SET status='FAILED' WHERE research_id=?", (rid,))
    body = {"idempotency_key": "retry-failed-" + action, "expected_version": 0}
    first = app.command(rid, action, body)
    assert first["status"] == expected
    assert app.command(rid, action, body) == first
    assert app.store.db.execute("SELECT COUNT(*) FROM control_commands").fetchone()[0] == 1
    assert not app.store.ledger()["requests"]


def test_preflight_does_not_hold_a_database_write_transaction(app, monkeypatch):
    configure(app)
    rid = create(app)
    def check(snapshot):
        assert not app.store.db.in_transaction
        return {"ready": True}
    monkeypatch.setattr(app, "preflight", check)
    assert app.command(rid, "start", {"idempotency_key": "outside-db-lock", "expected_version": 0})["status"] == "STARTING"


def test_changed_snapshot_during_preflight_cannot_be_dispatched(app, monkeypatch):
    configure(app)
    rid = create(app)
    def check(snapshot):
        latest = app.store.run(rid)["snapshot"]
        latest["question"] = "변경된 질문"
        app.store.db.execute("UPDATE control_runs SET snapshot=? WHERE research_id=?", (to_json(latest), rid))
        return {"ready": True}
    monkeypatch.setattr(app, "preflight", check)
    with pytest.raises(ControlError, match="STATE_STALE"):
        app.command(rid, "start", {"idempotency_key": "changed-preflight", "expected_version": 0})
    assert app.store.run(rid)["status"] == "DRAFT"
    assert app.store.db.execute("SELECT COUNT(*) FROM control_commands").fetchone()[0] == 0


@pytest.mark.parametrize("identity_matches", [True, False])
def test_startup_recovery_observes_worker_owner_before_pid_confirmation(app, monkeypatch, identity_matches):
    configure(app)
    rid = create(app)
    app.store.db.execute("UPDATE control_runs SET status='RUNNING',pid=NULL WHERE research_id=?", (rid,))
    _save(app.store, "worker_owner", rid, {"owner_pid": os.getpid(), "owner_birth": "old",
          "worker_pid": None, "status": "STARTING", "created_at": utc_now().isoformat()})
    monkeypatch.setattr("probe.control_plane.process_identity", lambda pid: "old" if identity_matches else "new")
    app._recover()
    assert app.store.run(rid)["status"] == ("RUNNING" if identity_matches else "PAUSED")


def test_recovery_cannot_overwrite_a_run_completed_during_process_check(app, monkeypatch):
    configure(app)
    rid = create(app)
    app.store.db.execute("UPDATE control_runs SET status='RUNNING',pid=NULL WHERE research_id=?", (rid,))
    def check(*args):
        app.store.db.execute("UPDATE control_runs SET status='COMPLETED',version=version+1 WHERE research_id=?", (rid,))
        return False
    monkeypatch.setattr("probe.control_plane.worker_alive", check)
    app._recover()
    assert app.store.run(rid)["status"] == "COMPLETED"


def test_process_identity_detects_pid_reuse():
    identity = process_identity(os.getpid())
    if identity is None:
        pytest.skip("운영체제 시작 값 조회를 지원하지 않는 환경")
    assert process_owned(os.getpid(), identity)
    assert not process_owned(os.getpid(), identity + "-changed")


@pytest.mark.parametrize("confirmed", [False, True, None], ids=["stopped", "running", "unknown"])
def test_resource_recovery_requires_confirmed_container_shutdown(app, monkeypatch, confirmed):
    configure(app)
    rid = create(app)
    pool = ResourcePool(app.store)
    job = pool.enqueue(rid, "analysis_planner_worker", "heavy-analysis", capacity=1, purpose="test")
    assert pool.try_start(job)
    _save(app.store, "analysis_process", "failed-container", {"research_id": rid, "pid": None,
          "owner_pid": os.getpid(), "owner_birth": process_identity(os.getpid()),
          "container": "probe-test", "status": "TERMINATION_FAILED"})
    def inspect(name):
        assert not app.store.db.in_transaction
        return confirmed
    monkeypatch.setattr("probe.sandbox.container_running", inspect)
    if confirmed is False:
        pool.enqueue(rid, "manager", "network", capacity=1, purpose="test")
        assert app.store.config("analysis_process", "failed-container")["status"] == "CANCELLED"
        assert app.store.config("resource_job", job["id"])["status"] == "CANCELLED"
    else:
        with pytest.raises(ControlError, match="ANALYSIS_TERMINATION_FAILED"):
            pool.enqueue(rid, "manager", "network", capacity=1, purpose="test")
        assert app.store.config("analysis_process", "failed-container")["status"] == "TERMINATION_FAILED"
        assert app.store.config("resource_job", job["id"])["status"] == "RUNNING"


def test_unreadable_secret_storage_fails_before_any_request_side_effect(app, monkeypatch):
    request = Mock()
    monkeypatch.setattr(app, "_request", request)
    monkeypatch.setattr(app.credentials, "active_secrets", Mock(side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")))
    result = app.request("POST", "/api/control/research", {"question": "질문"})
    assert result.status == 409
    assert result.body == {"error": "SECRET_STORAGE_OR_ARTIFACT_BLOCKED"}
    request.assert_not_called()


def test_submission_mode_is_read_only_in_independent_request_connections(app):
    app.submission = True
    with app.request_context() as context:
        assert context.submission
        assert context.request("GET", "/api/control/settings").body["submission_mode"]
        denied = context.request("POST", "/api/control/research", {"question": "새 연구"})
        assert denied.status == 409 and denied.body["error"] == "SUBMISSION_READ_ONLY"
    assert not app.store.ledger()["requests"]
    assert not app.store.db.execute("SELECT 1 FROM research_runs").fetchone()


def test_damaged_previous_pdf_does_not_block_a_verified_new_revision(app, monkeypatch):
    gateway, _, _ = rig(app, monkeypatch)
    rid = run(app, gateway)
    previous = report_root(app.read._state, rid)
    (previous / "report.pdf").write_bytes(b"damaged")
    result = asyncio.run(rewrite_report(app, rid, _rewrite_body(app, rid, "replace-damaged-pdf"), provider_factory=gateway))
    assert result["status"] == "READY", result
    current = report_root(app.read._state, rid)
    assert current != previous
    assert (previous / "report.pdf").read_bytes() == b"damaged"
    assert "report.pdf" in validate_report_snapshot(app.read._state, rid)["files"]


def test_invalid_only_report_does_not_require_price_or_send_a_model_request(app, monkeypatch):
    gateway, calls, _ = rig(app, monkeypatch)
    rid = run(app, gateway)
    source = app.store.db.execute("SELECT source_id FROM sources WHERE research_id=?", (rid,)).fetchone()[0]
    app.read._state.invalidate_source(rid, source, "무효 원문 검사", reopen=False)
    snapshot = app.store.run(rid)["snapshot"]
    for model in snapshot["models"].values():
        model.update(price=None, local_api_unmetered=False)
    app.store.db.execute("UPDATE control_runs SET snapshot=? WHERE research_id=?", (to_json(snapshot), rid))
    assert admission(app, rid)["execution"] == "LOCAL_LIMITATION"
    before = len(calls)
    result = asyncio.run(rewrite_report(app, rid, _rewrite_body(app, rid, "local-limit-report"), provider_factory=gateway))
    assert result["status"] == "PARTIAL" and not result.get("error"), result
    assert len(calls) == before
    assert app.store.config("ai_report", rid)["author"] == "LOCAL_LIMITATION"
    validate_report_snapshot(app.read._state, rid)


def test_explicit_batch_resume_retries_blocked_items_without_resending_completed_items(app, monkeypatch):
    gateway, calls, _ = rig(app, monkeypatch)
    rid = run(app, gateway)
    with monkeypatch.context() as patch:
        patch.setattr("probe.report_maintenance.admission", Mock(side_effect=ControlError("PRICE_REQUIRED")))
        blocked = asyncio.run(run_batch(app, provider_factory=gateway))
    assert blocked["status"] == "PARTIAL" and blocked["items"][0]["reason"] == "PRICE_REQUIRED"
    resumed = asyncio.run(run_batch(app, resume=blocked["batch_id"], provider_factory=gateway))
    assert resumed["status"] == "COMPLETED", resumed
    assert resumed["items"][0]["retry_count"] == 1
    assert resumed["items"][0]["previous_requests"][0]["reason"] == "PRICE_REQUIRED"
    before = len(calls)
    reused = asyncio.run(run_batch(app, resume=blocked["batch_id"], provider_factory=gateway))
    assert reused["status"] == "COMPLETED" and len(calls) == before
    assert reused["items"][0]["research_id"] == rid


def test_batch_resume_preserves_an_unresolved_charge(app, monkeypatch):
    gateway, calls, _ = rig(app, monkeypatch)
    rid = run(app, gateway)
    with monkeypatch.context() as patch:
        patch.setattr("probe.report_maintenance.admission", Mock(side_effect=ControlError("PRICE_REQUIRED")))
        blocked = asyncio.run(run_batch(app, provider_factory=gateway))
    now = utc_now().isoformat()
    app.store.db.execute("INSERT INTO spend_ledger(id,research_id,connection_id,model,role,purpose,month,status,reserved,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
         ("unresolved-for-test", rid, "local", "manual-id", "manager", "report", now[:7], "UNRESOLVED", 1, now))
    before = len(calls)
    result = asyncio.run(run_batch(app, resume=blocked["batch_id"], provider_factory=gateway))
    assert result["status"] == "PARTIAL" and result["items"][0]["reason"] == "NEEDS_RECONCILIATION"
    assert len(calls) == before
    assert app.store.db.execute("SELECT status,reserved FROM spend_ledger WHERE id='unresolved-for-test'").fetchone()[:] == ("UNRESOLVED", 1)


def test_submission_example_forces_optional_features_off_and_reuses_the_same_report(tmp_path, monkeypatch):
    from probe import demo
    from probe.submission import prepare_example
    original = demo.LiteratureResearchRuntime
    observed = []
    def runtime(*args, **kwargs):
        observed.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(demo, "LiteratureResearchRuntime", runtime)
    marker = Mock()
    monkeypatch.setattr(demo, "record_demo_validation", marker)
    for flag in ("PROBE_VERIFIED_ANALYSIS_SKILLS_ENABLED", "PROBE_VERIFICATION_REPAIR_ENABLED",
                 "PROBE_F3P_RIDGE_ARITHMETIC_CHECK", "PROBE_CLAIM_EVIDENCE_PROVENANCE", "PROBE_VERIFIER_DEPENDENCY_CATALOG"):
        monkeypatch.setenv(flag, "1")
    database, workspace = tmp_path / "state.sqlite", tmp_path / "workspace"
    first = prepare_example(database, workspace)
    assert prepare_example(database, workspace) == first
    assert first["paid_calls"] == 0 and first["mode"] == "DEMO"
    assert len(observed) == 1
    assert all(value is False for key, value in observed[0].items() if key.endswith("enabled"))
    marker.assert_not_called()


def test_submission_example_rejects_the_live_workbench_paths():
    from probe import submission
    root = Path(submission.__file__).resolve().parents[2]
    with pytest.raises(ValueError):
        submission.prepare_example(root / "build/workbench/state.sqlite", root / "build/workbench/workspace")


def test_silent_launcher_reports_a_damaged_example(tmp_path, monkeypatch):
    from probe import desktop, submission
    monkeypatch.setattr(desktop, "ROOT", tmp_path)
    monkeypatch.setattr(submission, "prepare_example", Mock(side_effect=ControlError("REPORT_PUBLICATION_INVALID")))
    runner, notices = Mock(), []
    assert desktop.main(["--submission"], runner=runner, dialog=notices.append) == 1
    assert notices and "시작하지 못했습니다" in notices[0]
    runner.assert_not_called()


def test_installation_declares_required_pdf_dependency_and_python_version():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.12"
    assert any(item.startswith("pypdf") for item in project["dependencies"])


def test_source_invalidation_rolls_back_if_synthesis_fails(app, monkeypatch):
    gateway, _, _ = rig(app, monkeypatch)
    rid = run(app, gateway)
    source = app.store.db.execute("SELECT source_id FROM sources WHERE research_id=?", (rid,)).fetchone()[0]
    version = app.read._state.state_version(rid)
    stopped = app.read._state._one("SELECT run_status FROM research_runs WHERE research_id=?", (rid,))[0]
    monkeypatch.setattr(app.read._state, "save_literature_synthesis", Mock(side_effect=RuntimeError("저장 실패")))
    with pytest.raises(RuntimeError, match="저장 실패"):
        app.read._state.invalidate_source(rid, source, "무효화 검사", reopen=False)
    assert app.read._state.state_version(rid) == version
    assert app.store.db.execute("SELECT status FROM sources WHERE source_id=?", (source,)).fetchone()[0] == "VERIFIED"
    assert app.read._state._one("SELECT run_status FROM research_runs WHERE research_id=?", (rid,))[0] == stopped
    assert not app.store.db.in_transaction


def test_worker_completion_before_parent_pid_confirmation_is_preserved(app, monkeypatch):
    configure(app)
    rid = create(app)
    app.launch = True
    def spawn(args, **kwargs):
        token = args[args.index("--ready-token") + 1]
        app.store.db.execute("UPDATE control_runs SET status='COMPLETED',pid=NULL,version=version+1 WHERE research_id=?", (rid,))
        return SimpleNamespace(pid=12345, stdout=BytesIO(("PROBE_WORKER_READY " + token + "\n").encode("ascii")), poll=lambda: 0)
    monkeypatch.setattr("probe.workbench.subprocess.Popen", spawn)
    result = app.command(rid, "start", {"idempotency_key": "worker-already-completed", "expected_version": 0})
    assert result["status"] == "COMPLETED"
    assert app.store.run(rid)["status"] == "COMPLETED" and app.store.run(rid)["pid"] is None
    assert app.store.config("worker_owner", rid)["status"] == "COMPLETED"
    assert not app.store.ledger()["requests"]
