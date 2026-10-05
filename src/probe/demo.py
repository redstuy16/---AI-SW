"""Day 4B 기능 동결을 위한 결정적 오프라인 데모."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import hashlib
import json
from pathlib import Path
from typing import Any

from .database import initialize, to_json
from .final_report import export_final_report
from .literature_runtime import LiteratureResearchRuntime
from .preflight import record_demo_validation, _source_fingerprint
from .providers.fake import FakeProvider
from .research_schemas import ConclusionCandidate, ResearchAction, StopReason
from .scholarly import NormalizedSource, SearchRequest, SearchResult, metadata_digest
from .service import StateService
from .storage import Workspace, sha256_file
from .schemas import ContextRef, RefType
from .autonomous_loop import AutonomousResearchLoop
from .context_compiler import ContextConfig


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "demo_scenarios" / "adaptive_research.csv"
MODELS = {"manager": "demo-manager", "experiment_coordinator": "demo-coordinator",
          "analysis_planner_worker": "demo-worker", "verification_coordinator": "demo-critic"}
GOAL = "Analyze temperature and growth without inferring causality."
MANAGER = {"decision_type": "INITIAL_PLAN", "research_question": "How are temperature and growth associated?",
           "rationale": "Use a fixed sample and report association without claiming causality.",
           "coordinator_role": "experiment_coordinator", "objective": "Test temperature and growth."}
SCORE = {"plausibility": 0.8, "testability": 0.9, "data_availability": 1,
         "information_value": 0.9, "cost": 0.1}


class DemoValidationError(RuntimeError):
    """고정 검증 자료가 선언된 검사 조건을 충족하지 못했다."""


def _shortlist() -> dict[str, Any]:
    return {"hypotheses": [
        {"statement": "Temperature and growth have a monotonic association.",
         "rationale": "Both columns are measured in the same fixed sample.", "score": SCORE, "criterion": {"method": "pearson_correlation", "confirmatory_methods": ["spearman_correlation"], "variables": {"x": "temperature", "y": "growth"}, "expected_direction": "positive", "alpha": 0.05}},
        {"statement": "Temperature and growth are unrelated.",
         "rationale": "The null alternative is testable.",
         "score": {**SCORE, "information_value": 0.2}},
        {"statement": "Growth changes only after a temperature threshold.",
         "rationale": "A nonlinear alternative is testable.",
         "score": {**SCORE, "plausibility": 0.5}}],
            "rationale": "Keep differentiated, bounded hypotheses."}


def _coordinator(call: dict[str, Any]) -> dict[str, Any]:
    active = json.loads(call["input_text"])["active_state"]
    dataset = active["datasets"][0]["dataset_id"]
    profile = active["profile_artifacts"][0]["artifact_id"]
    return {"decision_type": "DELEGATE", "objective": "First deterministic association test.",
            "rationale": "Start with a sensitivity check.", "assigned_role": "analysis_planner_worker",
            "input_refs": [{"type": "dataset", "id": dataset}, {"type": "artifact", "id": profile}],
            "allowed_tools": ["stats.run", "visualization.render", "evidence.record"],
            "max_tool_calls": 3, "max_retries": 1, "max_runtime_sec": 120, "max_cost_usd": 0.5}


def _worker(method: str):
    def reply(call: dict[str, Any]) -> dict[str, Any]:
        refs = json.loads(call["input_text"])["active_state"]["contract"]["inputs"]
        dataset = next(ref["id"] for ref in refs if ref["type"] == "dataset")
        return {"dataset_id": dataset, "selected_variables": ["temperature", "growth"],
                "method": method, "justification": "Fixed fixture has paired numeric columns.",
                "requested_tools": ["stats.run", "visualization.render", "evidence.record"],
                "reported_r": 0.5}
    return reply


def _first_critic(call: dict[str, Any]) -> dict[str, Any]:
    active = json.loads(call["input_text"])["active_state"]
    hypothesis = active["active_hypotheses"][0]["hypothesis_id"]
    return {"verdict": "FOLLOW_UP_REQUIRED",
            "issues": [{"code": "NONLINEARITY", "severity": "MEDIUM",
                        "detail": "Check the rank-based sensitivity of the fixed sample."}],
            "alternative_explanations": ["Endpoint influence"], "confounders": [],
            "requested_followups": [{"method": "spearman_correlation",
                                     "rationale": "Run a rank sensitivity analysis.",
                                     "hypothesis_id": hypothesis}], "conclusion_strength": "NONE"}


def _second_critic(call: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": "ACCEPT_WITH_LIMITATION",
            "issues": [{"code": "CAUSALITY", "severity": "LOW",
                        "detail": "Association does not establish causation."}],
            "alternative_explanations": ["Unmeasured confounding"],
            "confounders": ["Unmeasured factors"], "requested_followups": [],
            "conclusion_strength": "MODERATE"}


def _status(call: dict[str, Any]) -> dict[str, Any]:
    active = json.loads(call["input_text"])["active_state"]
    hypothesis = active["active_hypotheses"][0]["hypothesis_id"]
    return {"hypothesis_id": hypothesis, "new_status": "SUPPORTED",
            "rationale": "Two verified methods support a bounded association.",
            "evidence_refs": [{"type": "evidence", "id": item["evidence_id"]}
                              for item in active["verified_evidence"]]}


def _agent_replies() -> list[Any]:
    return [MANAGER, _shortlist(), _coordinator, _worker("pearson_correlation"), _first_critic,
            {"approve": True, "rationale": "A rank sensitivity check addresses the critic issue.",
             "method": "spearman_correlation", "objective": "Run bounded Spearman follow-up."},
            _worker("spearman_correlation"), _second_critic, _status,
            {"stop": True, "reason": "GOAL_ANSWERED", "rationale": "The fixed fixture is answered with a limitation."}]


class DemoScholarlyProvider:
    name = "scholarly.fake"

    def __init__(self):
        self.calls: list[str] = []

    @staticmethod
    def _source(kind: str) -> NormalizedSource:
        if kind == "contradiction":
            source = NormalizedSource(title="Temperature growth null association study",
                                      authors=["Demo Research Group"], publication_year=2024,
                                      doi="10.5555/demo-null", abstract=
                                      "No association between temperature and growth was observed in the fixed cohort.",
                                      url="https://example.invalid/demo-null", provider="scholarly.fake",
                                      provider_ids={"demo": "null"})
        else:
            source = NormalizedSource(title="Temperature growth association study",
                                      authors=["Demo Research Group"], publication_year=2024,
                                      doi="10.5555/demo-support", abstract=
                                      "Temperature and growth were associated in the fixed cohort.",
                                      url="https://example.invalid/demo-support", provider="scholarly.fake",
                                      provider_ids={"demo": "support"})
        source.metadata_hash = metadata_digest(source)
        return source

    async def search(self, request: SearchRequest) -> SearchResult:
        self.calls.append(request.query)
        lowered = request.query.casefold()
        kind = "contradiction" if any(word in lowered for word in ("null", "negative", "not", "contradiction")) else "support"
        source = self._source(kind)
        return SearchResult(provider=self.name, request=request, sources=[source], total_results=1)

    async def get_work(self, external_id: str) -> NormalizedSource:
        return self._source("contradiction" if "null" in external_id else "support")


def _manifest(state: StateService, research_id: str, scenario: str, result: dict[str, Any], expected_invalidated: int) -> dict[str, Any]:
    report = Path(result["final_report"])
    artifacts = [dict(row) for row in state._db.execute(
        "SELECT artifact_id,artifact_type,relative_path,sha256,status FROM artifacts WHERE research_id=? ORDER BY rowid",
        (research_id,))]
    return {"scenario": scenario, "fixture": str(FIXTURE), "fixture_sha256": sha256_file(FIXTURE),
            "research_id": research_id, "expected_stop_reason": "GOAL_ANSWERED",
            "expected_hypothesis_status": "SUPPORTED",
            "expected_verified_experiment_count": 2, "expected_invalidated_count": expected_invalidated,
            "expected_final_artifact_list": [item["artifact_id"] for item in artifacts if item["status"] == "VERIFIED"],
            "final_report": str(report), "state_version": state.state_version(research_id),
            "source_fingerprint": _source_fingerprint()}


def _write_demo_manifest(state: StateService, research_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    if state.workspace is None:
        raise DemoValidationError("demo workspace is required")
    from .report_publication import report_root, new_revision, publish_revision
    import shutil
    previous = report_root(state, research_id)
    root = new_revision(state, research_id)
    shutil.copytree(previous, root, dirs_exist_ok=True)
    path = root / "demo_manifest.json"
    manifest["manifest_path"] = str(path)
    data = (to_json(manifest) + "\n").encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    from .storage import sha256_bytes
    record = root / "manifests/artifact_manifest.json"
    declared = json.loads(record.read_text(encoding="utf-8", errors="strict"))
    declared["files"]["demo_manifest.json"] = sha256_bytes(data)
    record.write_bytes((to_json(declared) + "\n").encode("utf-8", errors="strict"))
    publish_revision(state, research_id, root)
    return manifest


def validate_demo(state: StateService, manifest: dict[str, Any]) -> dict[str, Any]:
    research_id = manifest["research_id"]
    run = state._one("SELECT run_status,stop_reason FROM research_runs WHERE research_id=?", (research_id,))
    if run["run_status"] not in {"COMPLETED", "STOPPED"} or run["stop_reason"] != manifest["expected_stop_reason"]:
        raise DemoValidationError("demo did not reach the expected terminal state")
    report = Path(manifest["final_report"])
    if not report.is_file() or not any(label in report.read_text(encoding="utf-8", errors="strict") for label in ("## 결론", "## Conclusion")):
        raise DemoValidationError("final report is missing or incomplete")
    verified_evidence = state._db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=? AND status='VERIFIED'", (research_id,)).fetchone()[0]
    verified_experiments = state._db.execute("SELECT COUNT(*) FROM experiments WHERE research_id=? AND status='VERIFIED'", (research_id,)).fetchone()[0]
    invalidated = state._db.execute("SELECT COUNT(*) FROM experiments WHERE research_id=? AND status='INVALIDATED'", (research_id,)).fetchone()[0]
    if verified_experiments < manifest["expected_verified_experiment_count"] or invalidated < manifest["expected_invalidated_count"]:
        raise DemoValidationError("demo verified/invalidation counts do not match the manifest")
    if state._db.execute("SELECT COUNT(*) FROM critic_reviews WHERE research_id=?", (research_id,)).fetchone()[0] < 1:
        raise DemoValidationError("demo has no critic review")
    if state._db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=? AND action_type='REPLAN'", (research_id,)).fetchone()[0] < 1:
        raise DemoValidationError("demo has no replan action")
    duplicate = state._db.execute("SELECT logical_key,COUNT(*) FROM research_actions WHERE research_id=? AND logical_key IS NOT NULL GROUP BY logical_key HAVING COUNT(*)>1", (research_id,)).fetchone()
    if duplicate:
        raise DemoValidationError("demo contains a duplicate logical action")
    for row in state._db.execute("SELECT payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED'", (research_id,)):
        payload = json.loads(row["payload_json"])
        science = payload.get("scientific")
        if science and not science.get("numeric_provenance"):
            raise DemoValidationError("verified experiment lacks numeric provenance")
    return {"passed": True, "research_id": research_id, "verified_evidence_count": verified_evidence,
            "verified_experiment_count": verified_experiments, "invalidated_count": invalidated,
            "report_exists": True}


def _database_and_workspace(database: Path | None, workspace: Path | None, scenario: str) -> tuple[Path, Path]:
    root = ROOT / "build" / "demo" / scenario
    return database or (root / "state.sqlite"), workspace or (root / "workspace")


def run_demo_a(database: Path | None = None, workspace: Path | None = None, *, submission=False) -> dict[str, Any]:
    database, workspace = _database_and_workspace(database, workspace, "adaptive_research")
    database.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    db = initialize(database)
    state = StateService(db, Workspace(workspace))
    try:
        provider = DemoScholarlyProvider()
        options = {name: False for name in ("verified_analysis_skills_enabled", "verification_repair_enabled",
                   "ridge_arithmetic_check_enabled", "claim_evidence_provenance_enabled", "verifier_dependency_catalog_enabled")} if submission else {}
        runtime = LiteratureResearchRuntime(state, FakeProvider(_agent_replies()), provider,
                                            models=MODELS, semantic_model_review=False, **options)
        result = asyncio.run(runtime.run(GOAL, FIXTURE))
        manifest = _write_demo_manifest(state, result["research_id"],
                                        _manifest(state, result["research_id"], "adaptive_research", result, 0))
        checks = validate_demo(state, manifest)
        if not submission:
            record_demo_validation("a", True, checks)
        return {**result, "mode": "DEMO", "demo_manifest": manifest, "validation": checks,
                "scholarly_calls": provider.calls}
    except Exception:
        if not submission:
            record_demo_validation("a", False, {"error": "demo validation failed"})
        raise
    finally:
        db.close()


def run_demo_b(database: Path | None = None, workspace: Path | None = None) -> dict[str, Any]:
    database, workspace = _database_and_workspace(database, workspace, "invalidation_recovery")
    database.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    db = initialize(database)
    state = StateService(db, Workspace(workspace))
    try:
        provider = DemoScholarlyProvider()
        runtime = LiteratureResearchRuntime(state, FakeProvider(_agent_replies()), provider,
                                            models=MODELS, semantic_model_review=False)
        result = asyncio.run(runtime.run(GOAL, FIXTURE))
        research_id = result["research_id"]
        invalidated_id = result["experiment_ids"][1]
        surviving_id = result["experiment_ids"][0]
        state.invalidate_experiment(research_id, invalidated_id, "Synthetic integrity/leakage issue discovered after review")
        hypothesis_id = result["hypothesis_id"]
        state.set_hypothesis_status(research_id, hypothesis_id, "ACTIVE", decided_by="system",
                                    rationale="Reopen after invalidation for a fresh independent check")
        experiment = state._one("SELECT dataset_id FROM experiments WHERE experiment_id=?", (surviving_id,))
        science = state._db.execute(
            "SELECT json_extract(payload_json,'$.scientific.profile_artifact_id') AS profile_id FROM staged_mutations "
            "WHERE research_id=? AND json_extract(payload_json,'$.scientific.experiment_id')=?",
            (research_id, surviving_id)).fetchone()
        decision = state._one("SELECT decision_id FROM decisions WHERE research_id=? ORDER BY rowid LIMIT 1", (research_id,))
        recovery_provider = FakeProvider([
            {"approve": True, "rationale": "Run a new independent check after invalidation.",
             "method": "pearson_correlation", "objective": "Recheck the association after integrity failure."},
            _worker("pearson_correlation")])
        recovery_runtime = AutonomousResearchLoop(state, recovery_provider, MODELS)
        state.context_config = ContextConfig(role_budgets={
            "manager": 10000, "experiment_coordinator": 10000,
            "analysis_planner_worker": 100000, "verification_coordinator": 8000,
            "literature_verification_coordinator": 8000})
        recovery_runtime.context_budget = 100000
        outcome = asyncio.run(recovery_runtime._experiment(
            research_id, hypothesis_id, experiment["dataset_id"], science["profile_id"],
            ContextRef(type=RefType.decision, id=decision["decision_id"]),
            followup_method="pearson_correlation", objective="Recheck after invalidation", branch_index=2))
        if outcome is None:
            raise DemoValidationError("recovery experiment did not run")
        state.record_action(research_id, ResearchAction.REPLAN.value, "recover after invalidation",
                            details={"invalidated_experiment_id": invalidated_id, "replacement_experiment_id": outcome["experiment_id"]},
                            max_actions=50, logical_key="demo:recovery:replan")
        refs = [ContextRef(type=RefType.evidence, id=result["evidence_ids"][0]),
                ContextRef(type=RefType.evidence, id=outcome["evidence_id"])]
        state.set_hypothesis_status(research_id, hypothesis_id, "SUPPORTED", decided_by="system",
                                    rationale="The surviving and replacement experiments are verified.", evidence_refs=refs)
        conclusion = ConclusionCandidate(statement="The integrity-reviewed fixed sample supports a bounded association.",
                                         support_level="MODERATE", evidence_refs=refs,
                                         unresolved_questions=["A later integrity issue invalidated one earlier branch."])
        state.record_action(research_id, ResearchAction.STOP.value, "GOAL_ANSWERED", input_refs=refs,
                            details={"reason": "GOAL_ANSWERED"}, max_actions=50, logical_key="demo:recovery:stop")
        state.stop_research(research_id, StopReason.GOAL_ANSWERED, conclusion)
        state.runtime_event(research_id, "DEMO_INVALIDATION_RECOVERED", {
            "invalidated_experiment_id": invalidated_id, "replacement_experiment_id": outcome["experiment_id"]})
        paths = export_final_report(state, research_id)
        result = {**result, "research_id": research_id, "experiment_ids": [surviving_id, outcome["experiment_id"]],
                  "evidence_ids": [result["evidence_ids"][0], outcome["evidence_id"]],
                  "final_report": paths["report"], "research_summary": paths["summary"],
                  "invalidated_experiment_id": invalidated_id, "replacement_experiment_id": outcome["experiment_id"]}
        manifest = _write_demo_manifest(state, research_id,
                                        _manifest(state, research_id, "invalidation_recovery", result, 1))
        checks = validate_demo(state, manifest)
        record_demo_validation("b", True, checks)
        return {**result, "mode": "DEMO", "demo_manifest": manifest, "validation": checks}
    except Exception:
        record_demo_validation("b", False, {"error": "demo validation failed"})
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a deterministic offline Probe Day 4B demo")
    parser.add_argument("--scenario", choices=["adaptive_research", "invalidation_recovery", "both"],
                        default="adaptive_research")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    if args.scenario == "adaptive_research":
        result = run_demo_a(args.database, args.workspace)
    elif args.scenario == "invalidation_recovery":
        result = run_demo_b(args.database, args.workspace)
    else:
        result = {"adaptive_research": run_demo_a(), "invalidation_recovery": run_demo_b()}
    print(to_json(result))


if __name__ == "__main__":
    main()
