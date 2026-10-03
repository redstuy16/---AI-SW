"""로컬 CSV를 신뢰할 수 있는 통계 도구로 분석하고 검증 후 정본에 반영한다."""
from __future__ import annotations

import argparse
import platform
from pathlib import Path

from .database import initialize, to_json
from .mock import MockExperimentCoordinator, MockManager, MockWorker
from .real_tools import (
    DataImportTool, DataProfileTool, EvidenceTool, StatsTool, ToolRegistry,
    VisualizationTool,
)
from .sandbox import PythonSandboxTool
from .schemas import Constraints, ContextRef, NumericProvenance, RefType, ScientificMutation, StagedResult, ToolRequest, new_id
from .scientific_verifier import numeric_fields
from .service import StateService
from .storage import Workspace, write_json


def execute_real_tools(state: StateService, csv_source: str | Path) -> tuple[StagedResult, dict]:
    research_id = state.create_research("Determine whether temperature and growth are associated.")
    state.workspace.prepare(research_id)
    parent = MockManager().create_contract(research_id, "Analyze temperature and growth")
    parent_task = state.issue_contract(parent)
    contract = MockExperimentCoordinator().create_contract(parent, parent_task).model_copy(
        update={"allowed_tools": ["data.import", "data.profile", "stats.run", "visualization.render", "evidence.record", "python.execute"],
                "constraints": Constraints(max_tool_calls=10)})
    task_id = state.issue_contract(contract)
    registry = ToolRegistry(state)
    for tool in (DataImportTool(state), DataProfileTool(state, contract.contract_id),
                 StatsTool(state, contract.contract_id), VisualizationTool(state, contract.contract_id),
                 EvidenceTool(state), PythonSandboxTool(state, contract.contract_id)):
        registry.register(tool)

    def dispatch(name: str, args: dict):
        request_id = new_id("TREQ")
        request = ToolRequest(request_id=request_id, research_id=research_id, task_id=task_id,
                              actor_id="analysis_worker", idempotency_key=request_id,
                              tool_name=name, args=args)
        result = registry.dispatch(contract.contract_id, request)
        if not result.ok:
            raise RuntimeError(result.error)
        return request, result

    _, imported = dispatch("data.import", {"source_path": str(csv_source), "research_id": research_id})
    dataset_id = imported.result["dataset_id"]
    _, profile = dispatch("data.profile", {"dataset_id": dataset_id})
    stats_request, stats_result = dispatch("stats.run", {"dataset_id": dataset_id,
        "method": "pearson_correlation", "variables": {"x": "temperature", "y": "growth"}, "parameters": {}})
    _, figure = dispatch("visualization.render", {"dataset_id": dataset_id, "plot_type": "scatter",
        "x": "temperature", "y": "growth", "title": "Temperature and growth"})
    claim = "Temperature and growth are positively associated in the imported sample."
    _, evidence = dispatch("evidence.record", {"claim": claim, "polarity": "support",
        "source_type": "artifact", "source_ref": stats_result.provenance["stats_artifact_id"]})
    dataset = state.dataset_record(dataset_id, research_id)
    stats_artifact_id = stats_result.provenance["stats_artifact_id"]
    provenance = {name: NumericProvenance(value=value, field=name, artifact_id=stats_artifact_id,
                   tool_call_id=stats_request.request_id, dataset_id=dataset_id,
                   dataset_sha256=dataset["sha256"])
                  for name, value in numeric_fields(stats_result.result).items()}
    science = ScientificMutation(dataset_id=dataset_id, profile_artifact_id=profile.result["artifact_id"],
        stats_artifact_id=stats_artifact_id, figure_artifact_id=figure.result["artifact_id"],
        experiment_id=new_id("EXP"), evidence_id=new_id("EV"), method="pearson_correlation",
        claim=evidence.result["claim"], polarity=evidence.result["polarity"],
        numeric_provenance=provenance)
    agent_result = MockWorker().consume(contract, stats_result).model_copy(update={
        "artifact_refs": [ContextRef(type=RefType.artifact, id=artifact_id) for artifact_id in
                          (science.profile_artifact_id, science.stats_artifact_id, science.figure_artifact_id)]})
    payload = StagedResult(agent_result=agent_result, tool_request=stats_request,
                           tool_result=stats_result, scientific=science)
    return payload, {"research_id": research_id, "dataset_id": dataset_id,
                     "profile_artifact_id": science.profile_artifact_id,
                     "stats_artifact_id": stats_artifact_id, "figure_artifact_id": science.figure_artifact_id}


def create_manifest(state: StateService, research_id: str) -> tuple[Path, str]:
    db = state._db
    datasets = [dict(row) for row in db.execute("SELECT dataset_id,sha256,stored_path,status FROM datasets WHERE research_id=?", (research_id,))]
    artifacts = [dict(row) for row in db.execute("SELECT artifact_id,artifact_type,relative_path,sha256,status FROM artifacts WHERE research_id=?", (research_id,))]
    experiments = [dict(row) for row in db.execute("SELECT experiment_id,dataset_id,method,status FROM experiments WHERE research_id=?", (research_id,))]
    tool_versions = {row["tool_name"]: row["tool_version"] for row in db.execute(
        "SELECT tool_name,json_extract(result_json,'$.provenance.tool_version') AS tool_version FROM tool_calls WHERE agent_run_id IN (SELECT agent_run_id FROM agent_runs WHERE contract_id IN (SELECT contract_id FROM contracts WHERE research_id=?))", (research_id,))}
    manifest = {"research_id": research_id, "python_version": platform.python_version(),
                "datasets": datasets, "artifacts": artifacts, "experiments": experiments,
                "tool_versions": tool_versions}
    path = state.workspace.path(research_id, "artifacts/manifest.json")
    digest = write_json(path, manifest)
    return path, digest


def run_real_cycle(database_path: str | Path, workspace_root: str | Path, csv_source: str | Path) -> dict:
    db = initialize(database_path)
    state = StateService(db, Workspace(workspace_root))
    try:
        payload, ids = execute_real_tools(state, csv_source)
        mutation_id = state.stage(payload)
        if state.artifact(payload.agent_result.result_id) is not None:
            raise RuntimeError("unverified result became canonical")
        verification = state.verify(mutation_id)
        if verification.verdict.value != "PASS":
            raise RuntimeError(verification.errors)
        version = state.commit(mutation_id)
        manifest_path, manifest_sha256 = create_manifest(state, ids["research_id"])
        return {**ids, "mutation_id": mutation_id, "result_artifact_id": payload.agent_result.result_id,
                "result": payload.agent_result.output, "verdict": verification.verdict.value,
                "state_version": version, "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha256}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("csv", type=Path)
    arguments = parser.parse_args()
    print(to_json(run_real_cycle(arguments.database, arguments.workspace, arguments.csv)))


if __name__ == "__main__":
    main()
