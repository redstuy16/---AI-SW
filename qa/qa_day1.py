"""동결된 Day 1 파괴적 검사를 수행하고 기계 판독 결과를 저장한다."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
QA = ROOT / "qa/results"
BUILD = ROOT / "build" / "qa-day1" / f"run-{uuid4().hex[:12]}"

from htrsa.database import initialize
from htrsa.demo import run_demo_a, run_demo_b
from htrsa.final_report import build_final_conclusion, validate_final_conclusion
from htrsa.preflight import (api_preflight, docker_preflight, environment_status,
                             record_qa_validation, _source_fingerprint)
from htrsa.release import export_release
from htrsa.service import StateService
from htrsa.storage import Workspace, sha256_file


SCENARIOS = {
    "QA-01 Early Fact Recall": ["test_mandatory_early_fact_and_recent_events"],
    "QA-02 Evidence Invalidation": ["test_invalidated_committed_experiment_cannot_be_reused",
                                    "test_qa_invalidation_and_dashboard_match_canonical_state"],
    "QA-03 Dependency Recall": ["test_direct_dependency_survives_low_semantic_similarity"],
    "QA-04 Context Distractor": ["test_context_budget_drops_semantic_but_preserves_mandatory"],
    "QA-05 Handoff Preservation": ["test_qa_handoff_must_preserve_survives_fresh_database_open"],
    "QA-06 Rollback / State Recovery": ["test_checkpoint_and_rollback", "test_forged_numeric_output_blocks_commit"],
    "QA-07 Python Failure Recovery": ["test_transient_stats_tool_failure_retries_without_manager_recall",
                                     "test_retry_exhaustion_reaches_coordinator_without_manager_recall"],
    "QA-09 Artifact Corruption": ["test_corrupted_stats_artifact_blocks_commit",
                                  "test_qa_release_rejects_post_commit_artifact_corruption"],
    "QA-10 Dataset Corruption": ["test_dataset_tamper_after_stage_blocks_commit",
                                 "test_qa_release_rejects_post_import_dataset_corruption"],
    "QA-11 Scientific Method Trap": ["test_constant_pearson_rejected",
                                      "test_high_severity_final_critic_blocks_answered_conclusion"],
    "QA-12 API / Budget Failure": ["test_fake_provider_failures_are_bounded_and_noncanonical",
                                   "test_soft_budget_warns_and_hard_budget_blocks",
                                   "test_unknown_price_call_and_token_caps_block_new_invocation"],
    "QA-13 Loop / Idempotency": ["test_action_fingerprint_count_survives_restart",
                                  "test_resume_replays_completed_tool_without_side_effect"],
    "QA-14 Crash After Commit": ["test_resume_recovers_commit_before_completion_checkpoint"],
    "QA-15 Crash During Critic/Replan": ["test_each_crash_boundary_resumes_without_duplicate_effects"],
    "QA-16 Literature Provenance Tampering": ["test_qa_literature_metadata_and_abstract_tampering_blocks_report"],
    "QA-17 Contradictory Evidence Preservation": ["test_qa_contradiction_survives_synthesis_and_report"],
    "QA-18 Search Rate Limit": ["test_429_is_mapped_to_rate_limited_and_bounded"],
    "QA-19 Report Hallucination": ["test_qa_report_hallucinated_ids_doi_and_numbers_are_rejected",
                                   "test_qa_release_rejects_stale_or_forged_report"],
    "QA-20 Secret Leakage": ["test_release_secret_exclusion", "test_qa_release_omits_local_absolute_paths"],
}

TEST_FILES = [
    "tests/test_qa_day1.py", "tests/test_context_compiler.py",
    "tests/test_autonomous_recovery.py", "tests/test_agent_runtime.py",
    "tests/test_units.py", "tests/test_real_cycle.py",
    "tests/test_real_tools.py", "tests/test_autonomous_loop.py",
    "tests/test_day4b.py",
]


def save(name: str, value: object) -> None:
    QA.mkdir(exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    data.encode("utf-8", errors="strict")
    (QA / name).write_text(data, encoding="utf-8")


def run(command: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False)


def pytest_run(label: str, extra: list[str]) -> dict:
    BUILD.mkdir(parents=True, exist_ok=True)
    temp = BUILD / f"pytest-{label}"
    xml = BUILD / f"{label}.xml"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--basetemp", str(temp), "--junitxml", str(xml), *extra]
    started = time.monotonic()
    completed = run(command)
    (BUILD / f"{label}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    match = re.search(r"(\d+) passed", completed.stdout)
    skipped = re.search(r"(\d+) skipped", completed.stdout)
    deselected = re.search(r"(\d+) deselected", completed.stdout)
    return {"passed": completed.returncode == 0, "exit_code": completed.returncode,
            "passed_count": int(match.group(1)) if match else 0,
            "skipped_count": int(skipped.group(1)) if skipped else 0,
            "deselected_count": int(deselected.group(1)) if deselected else 0,
            "duration_sec": round(time.monotonic() - started, 3),
            "xml": str(xml), "log": str(BUILD / f"{label}.log")}


def stress_suite() -> dict:
    result = pytest_run("stress", TEST_FILES)
    cases = []
    if Path(result["xml"]).is_file():
        for case in ET.parse(result["xml"]).iter("testcase"):
            cases.append({"name": case.attrib["name"],
                          "passed": not any(case.find(tag) is not None
                                            for tag in ("failure", "error", "skipped"))})
    scenarios = {}
    for scenario, functions in SCENARIOS.items():
        matched = {function: [case for case in cases if case["name"].split("[", 1)[0] == function]
                   for function in functions}
        scenarios[scenario] = {
            "status": "PASS" if all(items and all(item["passed"] for item in items)
                                     for items in matched.values()) else "FAIL",
            "tests": {function: len(items) for function, items in matched.items()},
        }
    docker = docker_preflight()
    if docker["available"]:
        docker_result = pytest_run("docker", ["-m", "docker_integration"])
        scenarios["QA-08 Sandbox Escape"] = {"status": "PASS" if docker_result["passed"] and
                                            docker_result["passed_count"] >= 4 and
                                            docker_result["skipped_count"] == 0 else "FAIL",
                                            "result": docker_result}
    else:
        scenarios["QA-08 Sandbox Escape"] = {"status": "NOT_VALIDATED", "reason": "DOCKER_UNAVAILABLE"}
    return {"pytest": result, "scenarios": scenarios,
            "all_executable_passed": result["passed"] and all(
                item["status"] == "PASS" for key, item in scenarios.items() if key != "QA-08 Sandbox Escape")}


def demo_repeatability() -> tuple[dict, dict]:
    entries = {"A": [], "B": []}
    exemplar = {}
    for label, demo in (("A", run_demo_a), ("B", run_demo_b)):
        for index in range(5):
            folder = BUILD / f"demo-{label.lower()}-{index + 1}"
            folder.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            result = demo(folder / "state.sqlite", folder / "workspace")
            db = initialize(folder / "state.sqlite")
            rid = result["research_id"]
            run_row = db.execute("SELECT run_status,stop_reason FROM research_runs WHERE research_id=?", (rid,)).fetchone()
            actions = db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=?", (rid,)).fetchone()[0]
            duplicates = db.execute("SELECT COUNT(*) FROM (SELECT logical_key FROM research_actions WHERE research_id=? AND logical_key IS NOT NULL GROUP BY logical_key HAVING COUNT(*)>1)", (rid,)).fetchone()[0]
            state = StateService(db, Workspace(folder / "workspace"))
            conclusion = build_final_conclusion(state, rid)
            validate_final_conclusion(state, rid, conclusion)
            report = Path(result["final_report"]).read_text(encoding="utf-8", errors="strict")
            unresolved = bool(re.search(r"\{\{(?:NUM|SRC|EVIDENCE):", report))
            item = {"iteration": index + 1, "research_id": rid, "run_status": run_row["run_status"],
                    "stop_reason": run_row["stop_reason"], "hypothesis_status": db.execute(
                        "SELECT status FROM hypotheses WHERE hypothesis_id=?", (result["hypothesis_id"],)).fetchone()[0],
                    "verified_experiment_count": result["validation"]["verified_experiment_count"],
                    "invalidated_count": result["validation"]["invalidated_count"],
                    "action_count": actions, "duplicate_logical_actions": duplicates,
                    "unresolved_refs": unresolved, "report_exists": True,
                    "duration_sec": round(time.monotonic() - started, 3)}
            db.close()
            entries[label].append(item)
            if index == 0:
                exemplar[label] = (folder, result)
    expected = {"A": (2, 0), "B": (2, 1)}
    status = {}
    for label in ("A", "B"):
        semantic = {(item["run_status"], item["stop_reason"], item["hypothesis_status"],
                     item["verified_experiment_count"], item["invalidated_count"], item["action_count"])
                    for item in entries[label]}
        status[label] = (len(semantic) == 1 and all(
            item["run_status"] == "COMPLETED" and item["stop_reason"] == "GOAL_ANSWERED"
            and item["hypothesis_status"] == "SUPPORTED"
            and (item["verified_experiment_count"], item["invalidated_count"]) == expected[label]
            and item["duplicate_logical_actions"] == 0 and not item["unresolved_refs"]
            for item in entries[label]))
    return {"runs": entries, "passed": status, "failures": sum(not value for value in status.values()),
            "random_seed": None}, exemplar


def validate_release(exemplar: tuple[Path, dict]) -> dict:
    folder, result = exemplar
    db = initialize(folder / "state.sqlite")
    try:
        state = StateService(db, Workspace(folder / "workspace"))
        export = export_release(state, result["research_id"], BUILD / "clean-release")
        manifest = json.loads(Path(export["manifest"]).read_text(encoding="utf-8", errors="strict"))
        mismatches, missing, secrets = [], [], []
        secret_pattern = re.compile(rb"OPENAI_API_KEY\s*=|CROSSREF_MAILTO\s*=|sk-[A-Za-z0-9_-]{12,}|[A-Za-z]:\\")
        for item in manifest["files"]:
            path = Path(export["output"]) / item["path"]
            if not path.is_file():
                missing.append(item["path"])
                continue
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                mismatches.append(item["path"])
            if secret_pattern.search(data):
                secrets.append(item["path"])
        conclusion = build_final_conclusion(state, result["research_id"])
        validate_final_conclusion(state, result["research_id"], conclusion)
        report = (Path(export["output"]) / "research_output" / "final_report.md").read_text(encoding="utf-8")
        unresolved = bool(re.search(r"\{\{(?:NUM|SRC|EVIDENCE):", report))
        return {"passed": not (mismatches or missing or secrets or unresolved),
                "file_count": len(manifest["files"]), "hash_mismatches": mismatches,
                "missing_artifacts": missing, "secret_files": secrets, "unresolved_refs": int(unresolved),
                "manifest": export["manifest"], "manifest_sha256": sha256_file(Path(export["manifest"])),
                "clean_database": str(folder / "state.sqlite")}
    finally:
        db.close()


def probe(phase: str, folder: Path) -> None:
    sys.path.insert(0, str(ROOT / "tests"))
    from test_autonomous_loop import CSV, MODELS, fake_replies
    from htrsa.autonomous_loop import AutonomousResearchLoop
    from htrsa.providers.fake import FakeProvider
    from htrsa.recovery import FaultInjector, InjectedCrash
    if phase == "crash":
        folder.mkdir(parents=True, exist_ok=True)
        db = initialize(folder / "state.sqlite")
        state = StateService(db, Workspace(folder / "workspace"))
        provider = FakeProvider(fake_replies())
        def trigger(point):
            if point == "AFTER_COMMIT":
                raise InjectedCrash(point)
        loop = AutonomousResearchLoop(state, provider, MODELS, faults=FaultInjector(trigger))
        try:
            asyncio.run(loop.run("Analyze temperature and growth without inferring causality.", CSV))
            raise AssertionError("crash injection was not reached")
        except InjectedCrash:
            pass
        rid = db.execute("SELECT research_id FROM research_runs").fetchone()[0]
        save_path = folder / "probe.json"
        save_path.write_text(json.dumps({"research_id": rid, "consumed": len(provider.calls)}), encoding="utf-8")
        db.close()
    else:
        record = json.loads((folder / "probe.json").read_text(encoding="utf-8"))
        db = initialize(folder / "state.sqlite")
        state = StateService(db, Workspace(folder / "workspace"))
        provider = FakeProvider(fake_replies()[record["consumed"]:])
        loop = AutonomousResearchLoop(state, provider, MODELS)
        result = asyncio.run(loop.resume(record["research_id"]))
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE research_id=?",
                                    (record["research_id"],)).fetchone()[0]
                  for table in ("experiments", "evidence", "state_events")}
        counts["stats_calls"] = db.execute(
            "SELECT COUNT(*) FROM tool_calls tc JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id "
            "JOIN contracts c ON c.contract_id=ar.contract_id "
            "WHERE c.research_id=? AND tc.tool_name='stats.run'",
            (record["research_id"],)).fetchone()[0]
        db.close()
        output = {"passed": result["stop_reason"] == "GOAL_ANSWERED" and
                  counts == {"experiments": 2, "evidence": 2, "state_events": 2, "stats_calls": 2}
                  and not provider.replies, "counts": counts, "new_process": True}
        (folder / "resume.json").write_text(json.dumps(output), encoding="utf-8")
        if not output["passed"]:
            raise AssertionError(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", choices=["crash", "resume"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output-dir", type=Path,
                        help="write this run's reports separately from historical QA records")
    args = parser.parse_args()
    if args.output_dir:
        global QA
        QA = args.output_dir.resolve()
    if args.probe:
        probe(args.probe, args.root)
        return
    BUILD.mkdir(parents=True, exist_ok=True)
    stress = stress_suite()
    save("stress_results.json", stress)
    repeat, exemplars = demo_repeatability()
    save("demo_repeatability.json", repeat)
    fresh = BUILD / "fresh-process"
    first = run([sys.executable, str(ROOT / "qa" / "qa_day1.py"), "--probe", "crash", "--root", str(fresh)])
    second = run([sys.executable, str(ROOT / "qa" / "qa_day1.py"), "--probe", "resume", "--root", str(fresh)]) if first.returncode == 0 else first
    resume = json.loads((fresh / "resume.json").read_text(encoding="utf-8")) if second.returncode == 0 else {
        "passed": False, "crash_exit_code": first.returncode, "resume_exit_code": second.returncode,
        "error": (first.stderr + second.stderr)[-3000:]}
    release = validate_release(exemplars["A"])
    release["fresh_process_resume"] = resume
    save("artifact_validation.json", release)
    record_qa_validation("stress", stress["all_executable_passed"] and resume["passed"],
                         {"scenarios": len(stress["scenarios"]), "fresh_process_resume": resume["passed"]})
    record_qa_validation("artifact", release["passed"], {"file_count": release["file_count"]})
    full = pytest_run("full-regression", [])
    core = run([sys.executable, "-m", "htrsa.preflight", "validate-core"])
    try:
        core_marker = json.loads(core.stdout)
    except ValueError:
        core_marker = {"passed": False, "error": core.stderr[-1000:]}
    environment = environment_status()
    environment["docker"]["qa_status"] = stress["scenarios"]["QA-08 Sandbox Escape"]["status"]
    environment["provider"]["qa_status"] = "NOT_VALIDATED" if not api_preflight()["configured"] else (
        "VALIDATED" if environment["provider"]["live_smoke_validated"] else "NOT_VALIDATED")
    save("environment_validation.json", environment)
    summary = {"baseline": {"passed": 135, "skipped": 4, "deselected": 2},
               "current": full, "core_marker": core_marker,
               "destructive": {key: value["status"] for key, value in stress["scenarios"].items()},
               "demo_repeatability": repeat["passed"],
               "release_validation": release,
               "environment": environment,
               "p0": ["release export accepted post-commit scientific artifact and dataset hash mismatch"],
               "p1": ["release export accepted stale report/state version",
                      "demo manifest exported absolute workspace paths"],
               "p2": []}
    save("qa_day1_summary.json", summary)
    print(json.dumps({"stress_passed": stress["all_executable_passed"],
                      "demo_repeatability": repeat["passed"], "release_passed": release["passed"],
                      "fresh_process_resume": resume["passed"], "full_test": full,
                      "core_marker": core_marker,
                      "demo_ready": environment["demo_ready"],
                      "release_ready": environment["release_ready"]}, ensure_ascii=False))
    if not (stress["all_executable_passed"] and all(repeat["passed"].values()) and
            release["passed"] and resume["passed"] and full["passed"] and core_marker.get("passed")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
