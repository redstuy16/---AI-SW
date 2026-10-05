"""실제 연구와 구분되는 고정 합성 입력. 정답은 실행기 계산을 호출하지 않는다."""
import asyncio
from pathlib import Path

from f3p_eval import prepare
from probe.cycle5 import GoalScope, GoalWitness, SourceSemanticRecord, TransformationLineage
from probe.database import initialize
from probe.real_schemas import DatasetRecord
from probe.research_slice_schemas import ResearchSliceConfig
from probe.schemas import ResearchContract, utc_now
from probe.service import StateService
from probe.storage import Workspace, sha256_file

ON = ResearchSliceConfig(claim_evidence_provenance=True, verifier_dependency_catalog=True)
GOLD_ROWS = [{"year": "2019", "value": "1.8", "other": "a"},
             {"year": "2020", "value": "3.6", "other": "b"},
             {"year": "2021", "value": "5.4", "other": "c"}]


def csv_content(rows, headers=("year", "value", "other")):
    import csv
    import io
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(headers), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8", errors="strict")


def dataset(state, rid, identity, content):
    relative = f"inputs/datasets/{identity}.csv"
    path = state.workspace.path(rid, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    state.register_dataset(DatasetRecord(dataset_id=identity, research_id=rid, original_name=identity + ".csv",
        stored_path=relative, sha256=sha256_file(path), size_bytes=len(content), created_at=utc_now().isoformat()))
    return state.dataset_record(identity, rid)


def semantic(state, rid, identity, target, column, *, unit, kind="declared_other", name="합성 측정값", scale=1, baseline=None, proposed_by="analysis_planner_worker", target_kind="dataset", precision=None):
    current = state.cycle5._target(rid, target_kind, target)[0]
    record = SourceSemanticRecord(record_id=identity, research_id=rid, target_kind=target_kind, target_id=target,
        target_hash=current, column=column, quantity_kind=kind, quantity_name=name, unit=unit,
        storage_scale=scale, baseline=baseline, spatial_scope="합성 표본", temporal_scope="고정 관측 기간",
        column_meaning="고정 QA 입력이며 실제 기후 관측 자료가 아니다", source_precision=precision, proposed_by=proposed_by)
    state.cycle5.propose_source(record, actor_role=proposed_by)
    return state.cycle5.review_source(rid, identity, 1, actor_role="owner")


def transform_fixture(folder: Path, rows=None, *, child_kind="artifact", parent_rows=None,
                      quantity="temperature_anomaly", precision=None, parent_scale=1, child_scale=1):
    folder.mkdir(parents=True, exist_ok=True)
    db = initialize(folder / "state.sqlite")
    state = StateService(db, Workspace(folder / "workspace"))
    rid = state.create_research("고정 합성 온도 편차의 변환 정합성을 검사한다")
    state.workspace.prepare(rid)
    state.configure_research_slice(rid, ON)
    state.cycle5.enable(rid, actor_role="owner")
    state.finish_runtime_step(rid, "verification_repair_config", {"enabled": True, "version": "0.3.0", "ridge_arithmetic_check": False})
    parent_rows = parent_rows or [{"year": "2019", "value": "1", "other": "a"}, {"year": "2020", "value": "2", "other": "b"}, {"year": "2021", "value": "3", "other": "c"}]
    parent = dataset(state, rid, "D-parent", csv_content(parent_rows))
    contract = ResearchContract(contract_id="C-transform", research_id=rid, task_type="fixed_transformation",
        issued_by="owner", assigned_role="analysis_planner_worker", objective="고정된 입력·단위 변환만 허용",
        inputs=[{"type": "dataset", "id": parent["dataset_id"]}], allowed_tools=["analysis.skill"], output_schema_id="AnalysisPlan")
    state.issue_contract(contract)
    content = csv_content(GOLD_ROWS if rows is None else rows)
    if child_kind == "dataset":
        child = dataset(state, rid, "D-child", content)
        child_id = child["dataset_id"]
    else:
        child_id = "A-child"
        path = state.workspace.path(rid, "artifacts/child.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        state.register_file_artifact(child_id, rid, contract.contract_id, "TRANSFORM_CSV", "artifacts/child.csv", "fixture", contract.contract_id, sha256_file(path))
    baseline = "합성 기준 기간" if quantity == "temperature_anomaly" else None
    a = semantic(state, rid, "SM-parent", parent["dataset_id"], "value", unit="degC", kind=quantity,
                 name="합성 온도", baseline=baseline, scale=parent_scale)
    b = semantic(state, rid, "SM-child", child_id, "value", unit="degF", kind=quantity,
                 name="합성 온도", baseline=baseline, scale=child_scale, target_kind=child_kind, precision=precision)
    lineage = TransformationLineage(record_id="TL-fixed", research_id=rid, contract_id=contract.contract_id,
        parent_id=parent["dataset_id"], parent_hash=parent["sha256"], child_kind=child_kind, child_id=child_id,
        child_hash=state.cycle5._target(rid, child_kind, child_id)[0], key_columns=["year"], columns={"value": "value"},
        operation="unit_conversion", scale=1.8 * parent_scale / child_scale,
        offset=32 / child_scale if quantity == "absolute_temperature" else 0, semantic_refs={a.record_id: a.revision, b.record_id: b.revision})
    lineage = state.cycle5.declare_lineage(lineage, actor_role="owner")
    return db, state, rid, lineage


def science_fixture(folder: Path):
    db, state, runtime, prepared, oracle = prepare(folder, slice_config=ON)
    rid, did = prepared["research_id"], prepared["dataset_id"]
    state.cycle5.enable(rid, actor_role="owner")
    for col in ("a", "b"):
        semantic(state, rid, "SM-" + col, did, col, unit=col + "-units")
    scope = GoalScope(intent="association", estimand="corr(a,b)", analysis_method="pearson_correlation", quantity={"a": "declared_other", "b": "declared_other"},
        baseline={"a": None, "b": None}, units={"a": "a-units", "b": "b-units"}, population_scope="고정 합성 관측 단위",
        spatial_scope="합성 표본", temporal_scope="고정 관측 기간", limitations=["인과 해석 불가"])
    state.cycle5.set_goal(GoalWitness(research_id=rid, original_question="Association in the fixed sample?", scope=scope), actor_role="owner")
    worker = runtime.provider.replies[0]
    def witnessed(call):
        return {**worker(call), "semantic_scope": scope.model_dump(mode="json")}
    runtime.provider.replies[0] = witnessed
    return db, state, runtime, prepared, oracle


def completed_science(folder):
    db, state, runtime, prepared, oracle = science_fixture(folder)
    outcome = asyncio.run(runtime.resume(prepared["research_id"]))
    return db, state, runtime, prepared, oracle, outcome
