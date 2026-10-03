import json
from pathlib import Path

import pytest

from htrsa.database import initialize
from htrsa.mock import MockExperimentCoordinator, MockManager
from htrsa.real_tools import DataImportTool, DataProfileTool, StatsTool, VisualizationTool, ToolRegistry, ToolNotAllowedError
from htrsa.schemas import Constraints, ToolRequest, new_id
from htrsa.service import StateService, DuplicateToolRequestError
from htrsa.storage import Workspace, sha256_file, DatasetIntegrityError, UnsafeWorkspacePathError


CSV = Path(__file__).parent / "fixtures" / "temperature_growth.csv"


@pytest.fixture
def real_context(tmp_path):
    db = initialize(tmp_path / "state.sqlite")
    workspace = Workspace(tmp_path / "workspace")
    state = StateService(db, workspace)
    research_id = state.create_research("temperature and growth")
    workspace.prepare(research_id)
    parent = MockManager().create_contract(research_id, "temperature and growth")
    parent_task = state.issue_contract(parent)
    contract = MockExperimentCoordinator().create_contract(parent, parent_task).model_copy(
        update={"allowed_tools": ["data.import", "data.profile", "stats.run", "visualization.render", "evidence.record", "python.execute"],
                "constraints": Constraints(max_tool_calls=10)})
    task_id = state.issue_contract(contract)
    registry = ToolRegistry(state)
    registry.register(DataImportTool(state))
    registry.register(DataProfileTool(state, contract.contract_id))
    registry.register(StatsTool(state, contract.contract_id))
    registry.register(VisualizationTool(state, contract.contract_id))
    yield db, state, workspace, registry, research_id, contract, task_id
    db.close()


def request(research_id, task_id, tool_name, args, key=None):
    request_id = new_id("TREQ")
    return ToolRequest(request_id=request_id, research_id=research_id, task_id=task_id,
                       actor_id="analysis_worker", idempotency_key=key or request_id,
                       tool_name=tool_name, args=args)


def imported(real_context):
    _, _, _, registry, research_id, contract, task_id = real_context
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "data.import",
                                                               {"source_path": str(CSV), "research_id": research_id}))
    assert result.ok, result.error
    return result.result["dataset_id"]


def test_import_profile_stats_and_figure(real_context):
    db, state, workspace, registry, research_id, contract, task_id = real_context
    dataset_id = imported(real_context)
    row = state.dataset_record(dataset_id, research_id)
    assert row["sha256"] == sha256_file(workspace.path(research_id, row["stored_path"]))
    assert row["stored_path"].startswith("inputs/datasets/")
    profile = registry.dispatch(contract.contract_id, request(research_id, task_id, "data.profile", {"dataset_id": dataset_id}))
    assert profile.ok, profile.error
    assert profile.result["row_count"] == 8
    assert profile.result["column_count"] == 2
    profile_row = state.file_artifact(profile.result["artifact_id"], research_id)
    assert profile_row["artifact_type"] == "DATA_PROFILE"
    profile_data = json.loads(workspace.path(research_id, profile_row["relative_path"]).read_text(encoding="utf-8"))
    assert profile_data["columns"]["temperature"]["mean"] == 17
    assert profile_data["columns"]["temperature"]["missing_count"] == 0
    assert profile_data["duplicate_rows"] == 0
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "stats.run",
                                                            {"dataset_id": dataset_id, "method": "pearson_correlation",
                                                             "variables": {"x": "temperature", "y": "growth"}}))
    assert result.ok, result.error
    assert result.result["n"] == 8
    assert result.result["estimate"] > 0.99
    assert 0 <= result.result["p_value"] < 0.001
    figure = registry.dispatch(contract.contract_id, request(research_id, task_id, "visualization.render",
                                                            {"dataset_id": dataset_id, "plot_type": "scatter",
                                                             "x": "temperature", "y": "growth"}))
    assert figure.ok, figure.error
    assert state.file_artifact(figure.result["artifact_id"], research_id)["sha256"] == figure.result["sha256"]
    assert db.execute("SELECT status FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()[0] == "PROFILED"
    call = db.execute("SELECT started_at,finished_at,latency_ms,status,estimated_cost_usd FROM tool_calls WHERE request_id=?",
                      (result.request_id,)).fetchone()
    assert call["started_at"] and call["finished_at"]
    assert call["latency_ms"] >= 0 and call["status"] == "SUCCESS" and call["estimated_cost_usd"] == 0


def test_invalid_csv_and_idempotency(real_context, tmp_path):
    _, _, _, registry, research_id, contract, task_id = real_context
    invalid = tmp_path / "invalid.csv"
    content = "a,b\n1\n"
    content.encode("utf-8", errors="strict")
    invalid.write_text(content, encoding="utf-8")
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "data.import",
                                                            {"source_path": str(invalid), "research_id": research_id}))
    assert not result.ok and "INVALID_CSV" in result.error
    good = request(research_id, task_id, "data.import", {"source_path": str(CSV), "research_id": research_id}, key="once")
    assert registry.dispatch(contract.contract_id, good).ok
    with pytest.raises(DuplicateToolRequestError):
        registry.dispatch(contract.contract_id, good)


def test_allowlist_and_workspace_path(real_context):
    _, _, workspace, registry, research_id, contract, task_id = real_context
    with pytest.raises(ToolNotAllowedError):
        registry.dispatch(contract.contract_id, request(research_id, task_id, "unknown.tool", {}))
    with pytest.raises(UnsafeWorkspacePathError):
        workspace.path(research_id, "../../secret.txt")
    class ForbiddenTool:
        name = "forbidden.tool"
        version = "1"

        def run(self, request):
            raise AssertionError("must never execute")
    registry.register(ForbiddenTool())
    with pytest.raises(ToolNotAllowedError):
        registry.dispatch(contract.contract_id, request(research_id, task_id, "forbidden.tool", {}))


def test_dataset_tamper_detected(real_context):
    _, state, workspace, registry, research_id, contract, task_id = real_context
    dataset_id = imported(real_context)
    row = state.dataset_record(dataset_id, research_id)
    path = workspace.path(research_id, row["stored_path"])
    with path.open("ab") as stream:
        stream.write(b"X")
    with pytest.raises(DatasetIntegrityError):
        state.dataset_record(dataset_id, research_id)
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "stats.run",
                                                            {"dataset_id": dataset_id, "method": "pearson_correlation",
                                                             "variables": {"x": "temperature", "y": "growth"}}))
    assert not result.ok and result.error.startswith("DATASET_INTEGRITY_ERROR")


def test_constant_pearson_rejected(real_context, tmp_path):
    _, _, _, registry, research_id, contract, task_id = real_context
    path = tmp_path / "constant.csv"
    content = "x,y\n1,2\n1,3\n1,4\n"
    content.encode("utf-8", errors="strict")
    path.write_text(content, encoding="utf-8")
    imported_result = registry.dispatch(contract.contract_id, request(research_id, task_id, "data.import",
                                                                      {"source_path": str(path), "research_id": research_id}))
    dataset_id = imported_result.result["dataset_id"]
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "stats.run",
                                                            {"dataset_id": dataset_id, "method": "pearson_correlation",
                                                             "variables": {"x": "x", "y": "y"}}))
    assert not result.ok and "INVALID_METHOD_INPUT" in result.error


def test_other_supported_stats_methods(real_context, tmp_path):
    _, _, _, registry, research_id, contract, task_id = real_context
    dataset_id = imported(real_context)
    for method, variables in [
        ("descriptive", {"x": "temperature"}),
        ("spearman_correlation", {"x": "temperature", "y": "growth"}),
        ("linear_regression", {"x": "temperature", "y": "growth"}),
    ]:
        result = registry.dispatch(contract.contract_id, request(research_id, task_id, "stats.run",
                                {"dataset_id": dataset_id, "method": method, "variables": variables}))
        assert result.ok, result.error
        assert result.result["n"] == 8
        if method == "descriptive":
            assert result.result["mean"] == 17
        if method == "spearman_correlation":
            assert result.result["estimate"] == 1
        if method == "linear_regression":
            assert result.result["r_squared"] > 0.99
    group_path = tmp_path / "groups.csv"
    content = "value,group\n1,A\n2,A\n3,A\n5,B\n6,B\n7,B\n"
    content.encode("utf-8", errors="strict")
    group_path.write_text(content, encoding="utf-8")
    grouped = registry.dispatch(contract.contract_id, request(research_id, task_id, "data.import",
                                {"source_path": str(group_path), "research_id": research_id}))
    group_id = grouped.result["dataset_id"]
    for method in ("independent_t_test", "mann_whitney_u"):
        result = registry.dispatch(contract.contract_id, request(research_id, task_id, "stats.run",
                                {"dataset_id": group_id, "method": method,
                                 "variables": {"value": "value", "group": "group"}}))
        assert result.ok, result.error
        assert result.result["n"] == 6
        assert 0 <= result.result["p_value"] <= 1


@pytest.mark.parametrize("plot_type", ["line", "histogram", "bar"])
def test_other_plot_types_create_hashed_png(real_context, plot_type):
    _, state, _, registry, research_id, contract, task_id = real_context
    dataset_id = imported(real_context)
    args = {"dataset_id": dataset_id, "plot_type": plot_type, "x": "temperature"}
    if plot_type != "histogram":
        args["y"] = "growth"
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "visualization.render", args))
    assert result.ok, result.error
    artifact = state.file_artifact(result.result["artifact_id"], research_id)
    assert artifact["artifact_type"] == "FIGURE"
    assert artifact["sha256"] == result.result["sha256"]


def test_profile_detects_missing_constant_and_duplicate_rows(real_context, tmp_path):
    _, state, workspace, registry, research_id, contract, task_id = real_context
    path = tmp_path / "quality.csv"
    content = "constant,empty,value\n1,,2\n1,,2\n1,,4\n"
    content.encode("utf-8", errors="strict")
    path.write_text(content, encoding="utf-8")
    loaded = registry.dispatch(contract.contract_id, request(research_id, task_id, "data.import",
                                {"source_path": str(path), "research_id": research_id}))
    dataset_id = loaded.result["dataset_id"]
    profiled = registry.dispatch(contract.contract_id, request(research_id, task_id, "data.profile",
                                  {"dataset_id": dataset_id}))
    record = state.file_artifact(profiled.result["artifact_id"], research_id)
    profile = json.loads(workspace.path(research_id, record["relative_path"]).read_text(encoding="utf-8"))
    assert profile["duplicate_rows"] == 1
    assert profile["columns"]["constant"]["constant"] is True
    assert profile["columns"]["empty"]["all_null"] is True
    assert profile["columns"]["empty"]["missing_ratio"] == 1


def test_contract_tool_call_budget_is_enforced(real_context):
    db, state, _, _, research_id, contract, _ = real_context
    limited = contract.model_copy(update={"contract_id": new_id("CTR"),
                                  "allowed_tools": ["data.import"],
                                  "constraints": Constraints(max_tool_calls=1)})
    task_id = state.issue_contract(limited)
    registry = ToolRegistry(state)
    registry.register(DataImportTool(state))
    args = {"source_path": str(CSV), "research_id": research_id}
    assert registry.dispatch(limited.contract_id, request(research_id, task_id, "data.import", args)).ok
    with pytest.raises(ToolNotAllowedError):
        registry.dispatch(limited.contract_id, request(research_id, task_id, "data.import", args))
    assert db.execute("SELECT COUNT(*) FROM datasets WHERE research_id=?", (research_id,)).fetchone()[0] == 1
