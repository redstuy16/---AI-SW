"""StateService와 검증된 산출물을 조회하는 읽기 전용 대시보드. 브라우저에 DB 쓰기를 노출하지 않는다."""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import html
import json
from pathlib import Path
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field

from .database import from_json, initialize
from .preflight import environment_status
from .research_tree import build_research_tree
from .service import EntityNotFoundError, StateService
from .storage import ArtifactIntegrityError, UnsafeWorkspacePathError, Workspace, sha256_file
from .schemas import StrictModel


Mode = Literal["LIVE", "DEMO", "OFFLINE-CACHED"]


class ResearchOverview(StrictModel):
    research_id: str
    question: str
    status: str
    current_stage: str
    current_agent: str | None = None
    current_contract_id: str | None = None
    current_contract: dict[str, Any] | None = None
    state_version: int = Field(ge=0)
    stop_reason: str | None = None
    action_count: int = Field(ge=0)
    verified_evidence_count: int = Field(ge=0)
    verified_experiment_count: int = Field(ge=0)
    api_calls: int = Field(ge=0)
    estimated_cost_usd: float | None = None
    mode: Mode = "LIVE"
    resume_recovered: bool = False
    resume_status: str = "NOT_RECOVERED"


class ResearchTreeNode(StrictModel):
    id: str
    type: str
    label: str
    status: str
    parent_id: str | None = None


class ResearchTreeProjection(StrictModel):
    research_id: str
    state_version: int
    root_id: str
    nodes: list[ResearchTreeNode]
    edges: list[dict[str, Any]] = Field(default_factory=list)


class HypothesisProjection(StrictModel):
    hypothesis_id: str
    statement: str
    status: str
    score_summary: dict[str, Any] = Field(default_factory=dict)
    linked_evidence: list[str] = Field(default_factory=list)
    linked_experiments: list[str] = Field(default_factory=list)
    parent_hypothesis_id: str | None = None
    branch_depth: int = Field(default=0, ge=0)


class EvidenceProjection(StrictModel):
    evidence_id: str
    claim: str
    polarity: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    status: str
    valid: bool
    limitations: list[str] = Field(default_factory=list)
    provenance_refs: dict[str, Any] = Field(default_factory=dict)
    source_title: str | None = None
    source_doi: str | None = None
    source_openalex_id: str | None = None
    verification: dict[str, Any] = Field(default_factory=dict)


class ExperimentProjection(StrictModel):
    experiment_id: str
    hypothesis_ids: list[str] = Field(default_factory=list)
    dataset: dict[str, Any] | None = None
    method: str | None = None
    status: str | None = None
    sample_size: int | None = None
    result_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)


class VerificationProjection(StrictModel):
    subject_type: str
    subject_id: str
    verdict: str
    automatic_checks: list[dict[str, Any]] = Field(default_factory=list)
    scientific_review: dict[str, Any] | None = None
    artifact_hashes: dict[str, str | None] = Field(default_factory=dict)
    dataset_hash: str | None = None
    numeric_provenance: dict[str, Any] = Field(default_factory=dict)
    state_version: int | None = None
    invalidation_reason: str | None = None


class TimelineEvent(StrictModel):
    timestamp: str
    kind: str
    event_type: str
    label: str
    state_version: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    low_level: bool = False


class UsageProjection(StrictModel):
    manager_calls: int = 0
    coordinator_calls: int = 0
    worker_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    estimated_cost_usd: float | None = None
    cost_status: str = "Unknown"
    local_tool_latency_ms: float = 0.0
    wall_clock_seconds: float | None = None
    model_runs: list[dict[str, Any]] = Field(default_factory=list)


class ArtifactProjection(StrictModel):
    artifact_id: str
    artifact_type: str
    producer: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    status: str | None = None
    relative_path: str | None = None
    path_safe: bool = True
    preview_allowed: bool = False
    preview_url: str | None = None


class ReportProjection(StrictModel):
    available: bool
    relative_path: str | None = None
    content: str = ""
    rendered_html: str = ""
    safe: bool = True


class DashboardProjection(StrictModel):
    overview: ResearchOverview
    tree: ResearchTreeProjection
    hypotheses: list[HypothesisProjection]
    evidence: list[EvidenceProjection]
    experiments: list[ExperimentProjection]
    verification: list[VerificationProjection]
    timeline: list[TimelineEvent]
    usage: UsageProjection
    artifacts: list[ArtifactProjection]
    report: ReportProjection
    environment: dict[str, Any]


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return from_json(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _dict_rows(state: StateService, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in state._db.execute(sql, params).fetchall()]


def _row(state: StateService, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    value = state._db.execute(sql, params).fetchone()
    return dict(value) if value is not None else None


def _mode(state: StateService, research_id: str, value: str | None) -> Mode:
    if value in {"LIVE", "DEMO", "OFFLINE-CACHED"}:
        return value  # type: ignore[return-value]
    providers = [row[0] for row in state._db.execute(
        "SELECT provider FROM agent_runs WHERE research_id=? AND provider IS NOT NULL", (research_id,))]
    if providers and all(provider in {"fake", "scholarly.fake"} for provider in providers):
        return "DEMO"
    if any(provider == "scholarly.fake" for provider in providers):
        return "OFFLINE-CACHED"
    return "LIVE"


def _current_stage(state: StateService, research_id: str) -> str:
    cursor = _row(state, "SELECT cursor_json FROM research_runtime_state WHERE research_id=?", (research_id,))
    if cursor:
        document = _json_value(cursor["cursor_json"], {})
        if isinstance(document, dict) and document.get("stage"):
            return str(document["stage"])
    action = _row(state, "SELECT action_type FROM research_actions WHERE research_id=? ORDER BY action_index DESC LIMIT 1",
                  (research_id,))
    if action:
        return str(action["action_type"])
    event = _row(state, "SELECT event_type FROM runtime_events WHERE research_id=? ORDER BY seq DESC LIMIT 1",
                 (research_id,))
    return str(event["event_type"]) if event else "START"


def _current_contract(state: StateService, research_id: str) -> tuple[str | None, str | None]:
    row = _row(state, "SELECT contract_id,contract_json FROM contracts WHERE research_id=? "
                    "ORDER BY CASE WHEN status IN ('ISSUED','RUNNING','WAITING_RETRY','WAITING_ESCALATION') THEN 0 ELSE 1 END, rowid DESC LIMIT 1",
               (research_id,))
    if not row:
        return None, None
    document = _json_value(row["contract_json"], {})
    return row["contract_id"], document.get("assigned_role") if isinstance(document, dict) else None


def _current_contract_projection(state: StateService, research_id: str,
                                 contract_id: str | None) -> dict[str, Any] | None:
    if not contract_id:
        return None
    row = _row(state, "SELECT contract_id,contract_json,status,task_id FROM contracts WHERE research_id=? AND contract_id=?",
               (research_id, contract_id))
    if not row:
        return None
    document = _json_value(row["contract_json"], {})
    if not isinstance(document, dict):
        return {"contract_id": contract_id, "status": row["status"]}
    constraints = document.get("constraints") or {}
    failures = state._db.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE contract_id=? AND status='FAILED'", (contract_id,)).fetchone()[0]
    return {"contract_id": contract_id, "task_id": row["task_id"], "status": row["status"],
            "agent_role": document.get("assigned_role"), "objective": document.get("objective"),
            "allowed_tools": document.get("allowed_tools", []),
            "remaining_retries": max(0, int(constraints.get("max_retries", 0)) - failures),
            "acceptance_criteria": document.get("acceptance", []),
            "constraints": constraints}


def project_overview(state: StateService, research_id: str, *, mode: str | None = None) -> ResearchOverview:
    run = state._one("SELECT * FROM research_runs WHERE research_id=?", (research_id,))
    current_contract, current_agent = _current_contract(state, research_id)
    evidence = state._db.execute(
        "SELECT COUNT(*) FROM evidence WHERE research_id=? AND status='VERIFIED'", (research_id,)).fetchone()[0]
    experiments = state._db.execute(
        "SELECT COUNT(*) FROM experiments WHERE research_id=? AND status='VERIFIED'", (research_id,)).fetchone()[0]
    actions = state._db.execute("SELECT COUNT(*) FROM research_actions WHERE research_id=?", (research_id,)).fetchone()[0]
    api_calls = state._db.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE research_id=? AND provider IS NOT NULL", (research_id,)).fetchone()[0]
    costs = state._db.execute(
        "SELECT SUM(estimated_cost_usd),COUNT(*) FROM agent_runs WHERE research_id=? AND provider IS NOT NULL",
        (research_id,)).fetchone()
    recovered = state._db.execute(
        "SELECT 1 FROM runtime_events WHERE research_id=? AND event_type IN "
        "('RUNTIME_RECONCILED','RECOVERY_COMPLETED','COMMITTED_ACTION_RECONCILED') LIMIT 1", (research_id,)).fetchone()
    return ResearchOverview(
        research_id=research_id, question=run["research_question"] or run["goal"],
        status=run["run_status"], current_stage=_current_stage(state, research_id),
        current_agent=current_agent, current_contract_id=current_contract,
        current_contract=_current_contract_projection(state, research_id, current_contract),
        state_version=run["state_version"], stop_reason=run["stop_reason"], action_count=actions,
        verified_evidence_count=evidence, verified_experiment_count=experiments, api_calls=api_calls,
        estimated_cost_usd=float(costs[0]) if costs[0] is not None else None,
        mode=_mode(state, research_id, mode), resume_recovered=bool(recovered),
        resume_status="RECOVERED" if recovered else "NOT_RECOVERED")


def _edge_rows(state: StateService, research_id: str) -> list[dict[str, Any]]:
    return _dict_rows(state, "SELECT from_type,from_id,edge_type,to_type,to_id,status,created_at "
                       "FROM entity_edges WHERE research_id=? ORDER BY rowid", (research_id,))


def project_tree(state: StateService, research_id: str) -> ResearchTreeProjection:
    """기존 관계에서 트리 화면을 만든다. 별도의 정본 트리는 저장하지 않는다."""
    state._one("SELECT research_id FROM research_runs WHERE research_id=?", (research_id,))
    nodes: dict[str, ResearchTreeNode] = {}
    nodes[research_id] = ResearchTreeNode(id=research_id, type="research", label="Research", status=
                                          state._one("SELECT run_status FROM research_runs WHERE research_id=?", (research_id,))["run_status"])
    for row in _dict_rows(state, "SELECT hypothesis_id,statement,status,parent_hypothesis_id FROM hypotheses WHERE research_id=? ORDER BY rowid", (research_id,)):
        nodes[row["hypothesis_id"]] = ResearchTreeNode(id=row["hypothesis_id"], type="hypothesis",
                                                        label=row["statement"], status=row["status"],
                                                        parent_id=row["parent_hypothesis_id"] or research_id)
    for row in _dict_rows(state, "SELECT source_id,title,status FROM sources WHERE research_id=? ORDER BY rowid", (research_id,)):
        nodes[row["source_id"]] = ResearchTreeNode(id=row["source_id"], type="source", label=row["title"],
                                                    status=row["status"], parent_id=research_id)
    for row in _dict_rows(state, "SELECT evidence_id,claim,status FROM evidence WHERE research_id=? ORDER BY rowid", (research_id,)):
        nodes[row["evidence_id"]] = ResearchTreeNode(id=row["evidence_id"], type="evidence", label=row["claim"],
                                                       status=row["status"], parent_id=research_id)
    for row in _dict_rows(state, "SELECT experiment_id,method,status FROM experiments WHERE research_id=? ORDER BY rowid", (research_id,)):
        nodes[row["experiment_id"]] = ResearchTreeNode(id=row["experiment_id"], type="experiment",
                                                        label=row["method"] or row["experiment_id"],
                                                        status=row["status"] or "UNKNOWN", parent_id=research_id)
    for row in _dict_rows(state, "SELECT dataset_id,original_name,status FROM datasets WHERE research_id=? ORDER BY rowid", (research_id,)):
        nodes[row["dataset_id"]] = ResearchTreeNode(id=row["dataset_id"], type="dataset",
                                                     label=row["original_name"], status=row["status"], parent_id=research_id)
    for row in _dict_rows(state, "SELECT artifact_id,COALESCE(artifact_type,kind) AS label,status FROM artifacts WHERE research_id=? ORDER BY rowid", (research_id,)):
        nodes[row["artifact_id"]] = ResearchTreeNode(id=row["artifact_id"], type="artifact",
                                                      label=row["label"] or row["artifact_id"],
                                                      status=row["status"] or "UNKNOWN", parent_id=research_id)
    for row in _edge_rows(state, research_id):
        child = nodes.get(row["to_id"])
        parent = nodes.get(row["from_id"])
        if child is not None and parent is not None and row["edge_type"] in {"supports", "generated_from", "tests", "uses_dataset", "produced_by"}:
            # 저장 관계는 실험→가설이며 화면은 가설→실험으로 표시한다.
            if row["edge_type"] == "tests" and child.type == "hypothesis" and parent.type == "experiment":
                parent.parent_id = child.id
            elif child.parent_id == research_id:
                child.parent_id = parent.id
    conclusion = _row(state, "SELECT run_status FROM research_runs WHERE research_id=?", (research_id,))
    nodes[f"{research_id}:conclusion"] = ResearchTreeNode(id=f"{research_id}:conclusion", type="conclusion",
                                                           label="Final Conclusion", status=conclusion["run_status"],
                                                           parent_id=research_id)
    return ResearchTreeProjection(research_id=research_id, state_version=state.state_version(research_id),
                                  root_id=research_id, nodes=list(nodes.values()), edges=_edge_rows(state, research_id))


def _branch_depth(state: StateService, research_id: str, hypothesis_id: str) -> int:
    depth = 0
    seen: set[str] = set()
    current = hypothesis_id
    while current and current not in seen:
        seen.add(current)
        row = state._db.execute("SELECT parent_hypothesis_id FROM hypotheses WHERE research_id=? AND hypothesis_id=?",
                                (research_id, current)).fetchone()
        if row is None or not row["parent_hypothesis_id"]:
            break
        depth += 1
        current = row["parent_hypothesis_id"]
    return depth


def project_hypotheses(state: StateService, research_id: str) -> list[HypothesisProjection]:
    output = []
    for row in _dict_rows(state, "SELECT * FROM hypotheses WHERE research_id=? ORDER BY rowid", (research_id,)):
        evidence = [r["to_id"] for r in state._db.execute(
            "SELECT to_id FROM entity_edges WHERE research_id=? AND from_type='hypothesis' AND from_id=? "
            "AND to_type='evidence' AND edge_type IN ('supports','contradicts') ORDER BY rowid",
            (research_id, row["hypothesis_id"]))]
        experiments = [r["from_id"] for r in state._db.execute(
            "SELECT from_id FROM entity_edges WHERE research_id=? AND from_type='experiment' AND to_type='hypothesis' "
            "AND to_id=? AND edge_type='tests' ORDER BY rowid", (research_id, row["hypothesis_id"]))]
        output.append(HypothesisProjection(hypothesis_id=row["hypothesis_id"], statement=row["statement"],
                                           status=row["status"], score_summary=_json_value(row["score_json"], {}),
                                           linked_evidence=evidence, linked_experiments=experiments,
                                           parent_hypothesis_id=row["parent_hypothesis_id"],
                                           branch_depth=_branch_depth(state, research_id, row["hypothesis_id"])))
    return output


def project_evidence(state: StateService, research_id: str) -> list[EvidenceProjection]:
    rows = _dict_rows(state, "SELECT e.*,s.title AS source_title,s.doi AS source_doi,s.openalex_id AS source_openalex_id "
                       "FROM evidence e LEFT JOIN sources s ON s.source_id=e.source_id "
                       "WHERE e.research_id=? ORDER BY e.rowid", (research_id,))
    return [EvidenceProjection(evidence_id=row["evidence_id"], claim=row.get("claim") or "",
                               polarity=row.get("polarity"), source_type=row.get("source_type"),
                               source_ref=row.get("source_ref") or row.get("source_id"), status=row.get("status") or "UNKNOWN",
                               valid=row.get("status") not in {"INVALIDATED", "PENDING"},
                               limitations=_json_value(row.get("limitations_json"), []),
                               provenance_refs=_json_value(row.get("provenance_json"), {}),
                               source_title=row.get("source_title"), source_doi=row.get("source_doi"),
                               source_openalex_id=row.get("source_openalex_id"),
                               verification=_json_value(row.get("verification_json"), {})) for row in rows]


def _stats_payload(state: StateService, research_id: str, artifact_id: str | None) -> dict[str, Any]:
    if not artifact_id or state.workspace is None:
        return {}
    row = _row(state, "SELECT relative_path,sha256,status FROM artifacts WHERE artifact_id=? AND research_id=?",
               (artifact_id, research_id))
    if not row or not row["relative_path"] or row["status"] in {"INVALIDATED", "SUPERSEDED"}:
        return {}
    try:
        path = state.workspace.path(research_id, row["relative_path"])
        if not path.is_file() or (row["sha256"] and sha256_file(path) != row["sha256"]):
            return {}
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError, UnsafeWorkspacePathError):
        return {}


def _experiment_artifacts(state: StateService, research_id: str, experiment_id: str) -> list[str]:
    values: list[str] = []
    for row in state._db.execute("SELECT payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED'", (research_id,)):
        payload = _json_value(row["payload_json"], {})
        science = payload.get("scientific") if isinstance(payload, dict) else None
        if isinstance(science, dict) and science.get("experiment_id") == experiment_id:
            values.extend(str(science[key]) for key in ("profile_artifact_id", "stats_artifact_id", "figure_artifact_id")
                          if science.get(key))
    row = _row(state, "SELECT result_artifact_id FROM experiments WHERE research_id=? AND experiment_id=?",
               (research_id, experiment_id))
    if row and row.get("result_artifact_id"):
        values.append(row["result_artifact_id"])
    return list(dict.fromkeys(values))


def project_verification(state: StateService, research_id: str) -> list[VerificationProjection]:
    output: list[VerificationProjection] = []
    for row in _dict_rows(state, "SELECT sm.*,e.experiment_id FROM staged_mutations sm "
                       "LEFT JOIN experiments e ON json_extract(sm.payload_json,'$.scientific.experiment_id')=e.experiment_id "
                       "WHERE sm.research_id=? AND sm.verification_json IS NOT NULL ORDER BY sm.rowid", (research_id,)):
        verification = _json_value(row["verification_json"], {})
        payload = _json_value(row["payload_json"], {})
        science = payload.get("scientific") if isinstance(payload, dict) else {}
        subject = row.get("experiment_id") or row["mutation_id"]
        artifact_ids = [science.get(key) for key in ("profile_artifact_id", "stats_artifact_id", "figure_artifact_id")
                        if isinstance(science, dict) and science.get(key)]
        hashes = {}
        for artifact_id in artifact_ids:
            artifact = _row(state, "SELECT sha256 FROM artifacts WHERE artifact_id=? AND research_id=?",
                            (artifact_id, research_id))
            hashes[artifact_id] = artifact.get("sha256") if artifact else None
        dataset_hash = None
        if isinstance(science, dict) and science.get("dataset_id"):
            dataset = _row(state, "SELECT sha256 FROM datasets WHERE dataset_id=? AND research_id=?",
                           (science["dataset_id"], research_id))
            dataset_hash = dataset.get("sha256") if dataset else None
        critic = None
        if subject:
            critic_row = _row(state, "SELECT output_json FROM critic_reviews WHERE research_id=? AND experiment_id=?",
                              (research_id, subject))
            critic = _json_value(critic_row["output_json"], {}) if critic_row else None
        invalid = _row(state, "SELECT quality_flags_json FROM evidence WHERE research_id=? AND experiment_id=?",
                       (research_id, subject)) if subject else None
        invalidation = _json_value(invalid.get("quality_flags_json"), []) if invalid else []
        output.append(VerificationProjection(
            subject_type="experiment" if row.get("experiment_id") else "staged_mutation",
            subject_id=subject, verdict="FAIL" if invalidation else verification.get("verdict", "UNKNOWN"),
            automatic_checks=verification.get("checks", []) if isinstance(verification, dict) else [],
            scientific_review=critic, artifact_hashes=hashes, dataset_hash=dataset_hash,
            numeric_provenance=science.get("numeric_provenance", {}) if isinstance(science, dict) else {},
            state_version=row.get("base_state_version"),
            invalidation_reason="; ".join(invalidation) if invalidation else None))
    for row in _dict_rows(state, "SELECT evidence_id,verification_json,verification_json FROM evidence "
                       "WHERE research_id=? AND source_type='LITERATURE' AND verification_json IS NOT NULL ORDER BY rowid", (research_id,)):
        verification = _json_value(row["verification_json"], {})
        output.append(VerificationProjection(subject_type="literature_evidence", subject_id=row["evidence_id"],
                                             verdict="PASS" if verification.get("passed") else "FAIL",
                                             automatic_checks=[verification],
                                             state_version=state.state_version(research_id)))
    return output


def project_experiments(state: StateService, research_id: str) -> list[ExperimentProjection]:
    verifications = {item.subject_id: item.model_dump(mode="json") for item in project_verification(state, research_id)}
    output = []
    for row in _dict_rows(state, "SELECT * FROM experiments WHERE research_id=? ORDER BY rowid", (research_id,)):
        hypotheses = [item["to_id"] for item in state._db.execute(
            "SELECT to_id FROM entity_edges WHERE research_id=? AND from_type='experiment' AND from_id=? "
            "AND to_type='hypothesis' AND edge_type='tests' ORDER BY rowid", (research_id, row["experiment_id"]))]
        artifacts = _experiment_artifacts(state, research_id, row["experiment_id"])
        stats = _stats_payload(state, research_id, row.get("result_artifact_id"))
        dataset = _row(state, "SELECT dataset_id,original_name,sha256,row_count,status FROM datasets WHERE dataset_id=? AND research_id=?",
                       (row.get("dataset_id"), research_id)) if row.get("dataset_id") else None
        output.append(ExperimentProjection(experiment_id=row["experiment_id"], hypothesis_ids=hypotheses,
                                           dataset=dataset, method=row.get("method"), status=row.get("status"),
                                           sample_size=stats.get("n") if isinstance(stats.get("n"), int) else (dataset or {}).get("row_count"),
                                           result_refs=[key for key in stats if key in {"estimate", "p_value", "n", "x_mean", "y_mean"}],
                                           artifact_refs=artifacts,
                                           verification=verifications.get(row["experiment_id"], {})))
    return output


def _event_details(value: Any) -> dict[str, Any]:
    parsed = _json_value(value, {})
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def project_timeline(state: StateService, research_id: str) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    for row in _dict_rows(state, "SELECT action_index,action_type,details_json,created_at FROM research_actions WHERE research_id=?", (research_id,)):
        events.append(TimelineEvent(timestamp=row["created_at"], kind="action", event_type=row["action_type"],
                                    label=row["action_type"].replace("_", " ").title(),
                                    details=_event_details(row["details_json"]), low_level=False))
    for row in _dict_rows(state, "SELECT event_type,details_json,created_at FROM runtime_events WHERE research_id=?", (research_id,)):
        events.append(TimelineEvent(timestamp=row["created_at"], kind="runtime", event_type=row["event_type"],
                                    label=row["event_type"].replace("_", " ").title(),
                                    details=_event_details(row["details_json"]), low_level=False))
    for row in _dict_rows(state, "SELECT event_type,entity_type,entity_id,payload_json,state_version,created_at FROM planning_events WHERE research_id=?", (research_id,)):
        events.append(TimelineEvent(timestamp=row["created_at"], kind="planning", event_type=row["event_type"],
                                    label=row["event_type"].replace("_", " ").title(), state_version=row["state_version"],
                                    details={"entity_type": row["entity_type"], "entity_id": row["entity_id"], **_event_details(row["payload_json"])}, low_level=False))
    for row in _dict_rows(state, "SELECT operation,entity_type,entity_id,state_version,created_at FROM state_events WHERE research_id=?", (research_id,)):
        events.append(TimelineEvent(timestamp=row["created_at"], kind="commit", event_type=row["operation"],
                                    label=f"Commit {row['entity_type']}", state_version=row["state_version"],
                                    details={"entity_id": row["entity_id"]}, low_level=True))
    for row in _dict_rows(state, "SELECT tc.tool_name,tc.status,tc.latency_ms,tc.finished_at,tc.started_at "
                       "FROM tool_calls tc JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id "
                       "JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=?", (research_id,)):
        events.append(TimelineEvent(timestamp=row["finished_at"] or row["started_at"] or "", kind="tool",
                                    event_type=row["tool_name"], label=f"Tool {row['tool_name']}",
                                    details={"status": row["status"], "latency_ms": row["latency_ms"]}, low_level=True))
    return sorted(events, key=lambda item: (item.timestamp, item.kind, item.event_type))


def project_usage(state: StateService, research_id: str) -> UsageProjection:
    rows = _dict_rows(state, "SELECT actor_role,provider,model,status,input_tokens,output_tokens,cached_input_tokens,estimated_cost_usd,started_at,finished_at,latency_ms FROM agent_runs WHERE research_id=? ORDER BY rowid", (research_id,))
    tools = _dict_rows(state, "SELECT tc.latency_ms FROM tool_calls tc JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id "
                       "JOIN contracts c ON c.contract_id=ar.contract_id WHERE c.research_id=?", (research_id,))
    role_counts = {role: sum(row["actor_role"] == role for row in rows) for role in ("manager", "experiment_coordinator", "analysis_planner_worker")}
    known_costs = [row["estimated_cost_usd"] for row in rows if row["estimated_cost_usd"] is not None]
    unknown_cost = any(row["provider"] is not None and row["estimated_cost_usd"] is None for row in rows)
    timestamps = [value for row in rows for value in (row.get("started_at"), row.get("finished_at")) if value]
    wall_clock = None
    if len(timestamps) >= 2:
        from datetime import datetime
        try:
            wall_clock = max(0.0, (max(datetime.fromisoformat(v) for v in timestamps) - min(datetime.fromisoformat(v) for v in timestamps)).total_seconds())
        except ValueError:
            wall_clock = None
    return UsageProjection(manager_calls=role_counts["manager"], coordinator_calls=role_counts["experiment_coordinator"],
                           worker_calls=role_counts["analysis_planner_worker"],
                           tool_calls=len(tools), input_tokens=sum(row["input_tokens"] or 0 for row in rows),
                           output_tokens=sum(row["output_tokens"] or 0 for row in rows),
                           cached_tokens=sum(row["cached_input_tokens"] or 0 for row in rows),
                           estimated_cost_usd=None if unknown_cost else (sum(known_costs) if known_costs else None),
                           cost_status="Unknown" if unknown_cost or not rows else "KNOWN",
                           local_tool_latency_ms=sum(row["latency_ms"] or 0 for row in tools), wall_clock_seconds=wall_clock,
                           model_runs=rows)


def project_artifacts(state: StateService, research_id: str) -> list[ArtifactProjection]:
    output: list[ArtifactProjection] = []
    for row in _dict_rows(state, "SELECT artifact_id,COALESCE(artifact_type,kind) AS artifact_type,producer_type,producer_id,size_bytes,sha256,status,relative_path FROM artifacts WHERE research_id=? ORDER BY rowid", (research_id,)):
        safe = True
        if row.get("relative_path") and state.workspace is not None:
            try:
                state.workspace.path(research_id, row["relative_path"])
            except (UnsafeWorkspacePathError, ValueError):
                safe = False
        preview = bool(safe and row.get("status") not in {"INVALIDATED", "SUPERSEDED"} and row.get("relative_path") and
                       (row.get("artifact_type") or "").upper() in {"FIGURE", "FIGURE_PNG", "PNG"} and
                       Path(row["relative_path"]).suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"})
        output.append(ArtifactProjection(artifact_id=row["artifact_id"], artifact_type=row["artifact_type"] or "UNKNOWN",
                                        producer=(f"{row['producer_type']}:{row['producer_id']}" if row.get("producer_type") else None),
                                        size_bytes=row.get("size_bytes"), sha256=row.get("sha256"), status=row.get("status"),
                                        relative_path=row.get("relative_path"), path_safe=safe,
                                        preview_allowed=preview,
                                        preview_url=f"/api/research/{research_id}/artifacts/{row['artifact_id']}/preview" if preview else None))
    for row in _dict_rows(state, "SELECT dataset_id,original_name,size_bytes,sha256,status,stored_path FROM datasets WHERE research_id=? ORDER BY rowid", (research_id,)):
        output.append(ArtifactProjection(artifact_id=row["dataset_id"], artifact_type="DATASET", producer="data.import",
                                        size_bytes=row["size_bytes"], sha256=row["sha256"], status=row["status"],
                                        relative_path=row["stored_path"], path_safe=True))
    for row in _dict_rows(state, "SELECT source_id,title,metadata_hash,status FROM sources WHERE research_id=? ORDER BY rowid", (research_id,)):
        output.append(ArtifactProjection(artifact_id=row["source_id"], artifact_type="LITERATURE_SOURCE",
                                        producer="scholarly", sha256=row["metadata_hash"], status=row["status"], path_safe=True))
    return output


def project_report(state: StateService, research_id: str) -> ReportProjection:
    if state.workspace is None:
        return ReportProjection(available=False)
    try:
        path = state.workspace.path(research_id, "research_output/final_report.md")
    except (UnsafeWorkspacePathError, ValueError):
        return ReportProjection(available=False)
    if not path.is_file():
        return ReportProjection(available=False, relative_path="research_output/final_report.md")
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return ReportProjection(available=False, relative_path="research_output/final_report.md")
    # Markdown을 이스케이프하여 표시하고 HTML·스크립트는 실행하지 않는다.
    rendered = "<pre class=\"report\">" + html.escape(content, quote=True) + "</pre>"
    return ReportProjection(available=True, relative_path="research_output/final_report.md", content=content,
                            rendered_html=rendered, safe=True)


def project_environment() -> dict[str, Any]:
    status = environment_status()
    return status


def project_dashboard(state: StateService, research_id: str, *, mode: str | None = None) -> DashboardProjection:
    return DashboardProjection(overview=project_overview(state, research_id, mode=mode),
                               tree=project_tree(state, research_id),
                               hypotheses=project_hypotheses(state, research_id),
                               evidence=project_evidence(state, research_id),
                               experiments=project_experiments(state, research_id),
                               verification=project_verification(state, research_id),
                               timeline=project_timeline(state, research_id),
                               usage=project_usage(state, research_id),
                               artifacts=project_artifacts(state, research_id),
                               report=project_report(state, research_id), environment=project_environment())



research_overview = project_overview
research_tree = project_tree
hypothesis_projection = project_hypotheses
evidence_projection = project_evidence
experiment_projection = project_experiments
verification_projection = project_verification
timeline_projection = project_timeline
usage_projection = project_usage
artifact_projection = project_artifacts
report_projection = project_report


def dashboard_html(api_base: str = "/api") -> str:
    """데모 서버의 대시보드 화면을 반환한다."""
    api = html.escape(api_base, quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>H-TRSA Research Dashboard</title>
<style>
:root{{font-family:Inter,system-ui,sans-serif;color:#172033;background:#f4f7fb}}body{{margin:0}}header{{background:#14213d;color:#fff;padding:20px 28px}}main{{display:grid;grid-template-columns:minmax(280px,1fr) minmax(340px,1.4fr);gap:16px;padding:16px;max-width:1400px;margin:auto}}section{{background:#fff;border:1px solid #dbe3ef;border-radius:12px;padding:16px;box-shadow:0 2px 8px #14213d12}}h1,h2{{margin:0 0 10px}}pre{{white-space:pre-wrap;overflow:auto;background:#f7f9fc;padding:12px;border-radius:8px}}.muted{{color:#637089}}.pill{{display:inline-block;padding:3px 8px;border-radius:999px;background:#e5efff;margin:2px;font-size:.8rem}}.fail{{background:#ffe6e6;color:#9d1c1c}}.pass{{background:#e5f8ed;color:#146c43}}@media(max-width:800px){{main{{grid-template-columns:1fr}}}}
</style></head><body><header><h1>H-TRSA Research Dashboard</h1><div id="mode" class="muted">Loading projection…</div></header>
<main><section><h2>Research Overview</h2><div id="overview">Loading…</div></section>
<section><h2>Research Tree</h2><pre id="tree">Loading…</pre></section>
<section><h2>Current Agent / Contract</h2><pre id="contract">Loading…</pre></section>
<section><h2>Evidence &amp; Verification</h2><pre id="evidence">Loading…</pre></section>
<section><h2>Experiments &amp; Usage</h2><pre id="experiments">Loading…</pre></section>
<section><h2>Environment Readiness</h2><pre id="environment">Loading…</pre></section>
<section><h2>Final Report</h2><div id="report" class="muted">Select a research ID in the URL as <code>?research_id=R-…</code>.</div></section>
</main><script>
const base='{api}'; const q=new URLSearchParams(location.search); const rid=q.get('research_id');
async function get(path){{const response=await fetch(path);if(!response.ok)throw new Error(response.status);return response.json();}}
async function load(){{if(!rid){{document.querySelector('#overview').textContent='Add ?research_id=… to inspect a run.';return;}}
try{{const [o,t,e,x,v,u,a,r,env]=await Promise.all([get(`${{base}}/research/${{rid}}`),get(`${{base}}/research/${{rid}}/tree`),get(`${{base}}/research/${{rid}}/evidence`),get(`${{base}}/research/${{rid}}/experiments`),get(`${{base}}/research/${{rid}}/verification`),get(`${{base}}/research/${{rid}}/usage`),get(`${{base}}/research/${{rid}}/artifacts`),get(`${{base}}/research/${{rid}}/report`),get(`${{base}}/environment`)]);
document.querySelector('#mode').textContent=`Mode: ${{o.mode}} · State v${{o.state_version}} · Resume: ${{o.resume_status}}`;
document.querySelector('#overview').textContent=JSON.stringify(o,null,2);document.querySelector('#tree').textContent=JSON.stringify(t.nodes,null,2);document.querySelector('#contract').textContent=JSON.stringify(o.current_contract||{{agent:o.current_agent,contract:o.current_contract_id,stage:o.current_stage}},null,2);document.querySelector('#evidence').textContent=JSON.stringify({{evidence:e,verification:v}},null,2);document.querySelector('#experiments').textContent=JSON.stringify({{experiments:x,usage:u,artifacts:a}},null,2);document.querySelector('#environment').textContent=JSON.stringify(env,null,2);document.querySelector('#report').innerHTML=r.available?r.rendered_html:'Final report unavailable';}}catch(error){{document.querySelector('#overview').textContent='Projection unavailable: '+error;}}}}
load();
</script></body></html>"""


@dataclass(frozen=True)
class APIResponse:
    status: int
    body: dict[str, Any] | str | bytes
    content_type: str = "application/json"


class DashboardReadAPI:
    """테스트·데모·HTTP 서버에서 사용하는 읽기 API."""

    def __init__(self, database: str | Path, workspace: str | Path, *, mode: str | None = None):
        self.database = Path(database)
        self.workspace = Workspace(workspace)
        self.mode = mode
        self._db = initialize(self.database)
        self._state = StateService(self._db, self.workspace)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _research(self, research_id: str) -> None:
        self._state._one("SELECT research_id FROM research_runs WHERE research_id=?", (research_id,))

    def request(self, path: str) -> APIResponse:
        parsed = urlparse(path)
        parts = [part for part in parsed.path.split("/") if part]
        try:
            with self._lock:
                if parts == ["api", "environment"]:
                    return APIResponse(200, project_environment())
                if len(parts) >= 3 and parts[0] == "api" and parts[1] == "research":
                    research_id = parts[2]
                    self._research(research_id)
                    if len(parts) == 6 and parts[3] == "artifacts" and parts[5] == "preview":
                        artifact = _row(self._state, "SELECT relative_path,sha256,status,artifact_type FROM artifacts "
                                         "WHERE research_id=? AND artifact_id=?", (research_id, parts[4]))
                        if not artifact or not artifact.get("relative_path") or artifact.get("status") in {"INVALIDATED", "SUPERSEDED"}:
                            return APIResponse(404, {"error": "artifact preview unavailable"})
                        if (artifact.get("artifact_type") or "").upper() not in {"FIGURE", "FIGURE_PNG", "PNG"}:
                            return APIResponse(415, {"error": "artifact preview type is not allowed"})
                        if Path(artifact["relative_path"]).suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
                            return APIResponse(415, {"error": "artifact preview extension is not allowed"})
                        try:
                            path = self.workspace.path(research_id, artifact["relative_path"])
                            if not path.is_file() or (artifact.get("sha256") and sha256_file(path) != artifact["sha256"]):
                                return APIResponse(409, {"error": "artifact integrity check failed"})
                            content_type = {".png": "image/png", ".jpg": "image/jpeg",
                                            ".jpeg": "image/jpeg", ".webp": "image/webp"}[path.suffix.casefold()]
                            return APIResponse(200, path.read_bytes(), content_type)
                        except (OSError, UnsafeWorkspacePathError):
                            return APIResponse(404, {"error": "artifact preview unavailable"})
                    endpoint = parts[3] if len(parts) > 3 else "overview"
                    if endpoint == "research-slice":
                        snapshot = self._state.research_slice.snapshot(research_id)
                        return APIResponse(200, snapshot or {"enabled": False})
                    if endpoint == "cycle5":
                        return APIResponse(200, self._state.cycle5.snapshot(research_id))
                    projections = {
                        "overview": lambda: project_overview(self._state, research_id, mode=self.mode),
                        "tree": lambda: project_tree(self._state, research_id),
                        "hypotheses": lambda: project_hypotheses(self._state, research_id),
                        "evidence": lambda: project_evidence(self._state, research_id),
                        "experiments": lambda: project_experiments(self._state, research_id),
                        "verification": lambda: project_verification(self._state, research_id),
                        "timeline": lambda: project_timeline(self._state, research_id),
                        "usage": lambda: project_usage(self._state, research_id),
                        "artifacts": lambda: project_artifacts(self._state, research_id),
                        "report": lambda: project_report(self._state, research_id),
                    }
                    if endpoint == "contract":
                        value = projections["overview"]().current_contract
                        return APIResponse(200, value or {})
                    if endpoint not in projections:
                        return APIResponse(404, {"error": "unknown dashboard endpoint"})
                    value = projections[endpoint]()
                    if hasattr(value, "model_dump"):
                        value = value.model_dump(mode="json")
                    elif isinstance(value, list):
                        value = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value]
                    return APIResponse(200, value)
                if parsed.path in {"", "/", "/dashboard"}:
                    return APIResponse(200, dashboard_html(), "text/html; charset=utf-8")
                return APIResponse(404, {"error": "not found"})
        except EntityNotFoundError:
            return APIResponse(404, {"error": "research not found"})
        except (OSError, ValueError) as exc:
            return APIResponse(400, {"error": type(exc).__name__})

    def get(self, path: str) -> dict[str, Any] | str:
        response = self.request(path)
        if response.status >= 400:
            raise KeyError(response.body)
        return response.body


def create_app(database: str | Path, workspace: str | Path, *, mode: str | None = None) -> DashboardReadAPI:
    return DashboardReadAPI(database, workspace, mode=mode)


class _DashboardHandler(BaseHTTPRequestHandler):
    api: DashboardReadAPI

    def do_GET(self) -> None:  # noqa: N802
        response = self.api.request(self.path)
        if isinstance(response.body, bytes):
            payload = response.body
        elif isinstance(response.body, str):
            payload = response.body.encode("utf-8", errors="strict")
        else:
            payload = json.dumps(response.body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8", errors="strict")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        return None


def serve(database: str | Path, workspace: str | Path, *, host: str = "127.0.0.1", port: int = 8765,
          mode: str | None = None) -> None:
    api = create_app(database, workspace, mode=mode)
    handler = type("DashboardHandler", (_DashboardHandler,), {"api": api})
    # 기존 동기식 SQLite 정책에 따라 서버는 단일 스레드로 실행한다.
    
    server = HTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        api.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the read-only H-TRSA dashboard projection")
    parser.add_argument("database", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", choices=["LIVE", "DEMO", "OFFLINE-CACHED"], default=None)
    args = parser.parse_args()
    serve(args.database, args.workspace, host=args.host, port=args.port, mode=args.mode)


if __name__ == "__main__":
    main()
