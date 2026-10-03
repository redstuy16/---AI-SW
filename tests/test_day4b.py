"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from htrsa.dashboard import (DashboardReadAPI, project_artifacts, project_environment,
                             project_evidence, project_experiments, project_hypotheses,
                             project_overview, project_report, project_timeline,
                             project_tree, project_usage)
from htrsa.database import initialize
from htrsa.demo import run_demo_a, run_demo_b
from htrsa.preflight import search_status_from_error
from htrsa.release import ReleaseExportError, export_release
from htrsa.scholarly import ScholarlyError, ScholarlyHTTPClient
from htrsa.schemas import ResearchContract, new_id
from htrsa.service import StateService
from htrsa.storage import Workspace


@pytest.fixture(scope="module")
def demo_a(tmp_path_factory):
    root = tmp_path_factory.mktemp("day4b-a")
    result = run_demo_a(root / "state.sqlite", root / "workspace")
    return root, result


@pytest.fixture(scope="module")
def demo_b(tmp_path_factory):
    root = tmp_path_factory.mktemp("day4b-b")
    result = run_demo_b(root / "state.sqlite", root / "workspace")
    return root, result


def _api(root, result, mode=None):
    return DashboardReadAPI(root / "state.sqlite", root / "workspace", mode=mode)


def test_research_overview_projection(demo_a):
    root, result = demo_a
    api = _api(root, result, "DEMO")
    overview = api.get(f"/api/research/{result['research_id']}")
    assert overview["mode"] == "DEMO" and overview["status"] == "COMPLETED"
    assert overview["verified_experiment_count"] == 2 and overview["state_version"] > 0
    api.close()


def test_tree_projection_is_relation_based(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    tree = project_tree(StateService(db, Workspace(root / "workspace")), result["research_id"])
    assert tree.root_id == result["research_id"]
    assert any(node.type == "hypothesis" for node in tree.nodes)
    assert any(node.type == "experiment" for node in tree.nodes)
    db.close()


def test_hypothesis_projection_contains_branch_and_links(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    rows = project_hypotheses(StateService(db, Workspace(root / "workspace")), result["research_id"])
    selected = next(row for row in rows if row.hypothesis_id == result["hypothesis_id"])
    assert selected.status == "SUPPORTED" and len(selected.linked_experiments) == 2
    assert selected.branch_depth == 0
    db.close()


def test_evidence_projection_keeps_polarity_and_validity(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    rows = project_evidence(StateService(db, Workspace(root / "workspace")), result["research_id"])
    assert any(row.polarity == "CONTRADICT" for row in rows)
    assert all(row.valid for row in rows if row.status == "VERIFIED")
    db.close()


def test_experiment_projection_reads_artifact_values(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    rows = project_experiments(StateService(db, Workspace(root / "workspace")), result["research_id"])
    assert {row.method for row in rows} == {"pearson_correlation", "spearman_correlation"}
    assert all(row.sample_size == 6 for row in rows)
    db.close()


def test_verification_projection_contains_numeric_provenance(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    state = StateService(db, Workspace(root / "workspace"))
    rows = project_experiments(state, result["research_id"])
    assert all(row.verification["verdict"] == "PASS" for row in rows)
    assert all(row.verification["numeric_provenance"] for row in rows)
    db.close()


def test_timeline_projection_has_high_level_and_low_level_events(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    events = project_timeline(StateService(db, Workspace(root / "workspace")), result["research_id"])
    assert any(event.kind == "action" and not event.low_level for event in events)
    assert any(event.kind == "commit" and event.low_level for event in events)
    db.close()


def test_usage_projection_unknown_cost_is_explicit(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    usage = project_usage(StateService(db, Workspace(root / "workspace")), result["research_id"])
    assert usage.manager_calls >= 3 and usage.tool_calls >= 6
    assert usage.cost_status == "Unknown" and usage.estimated_cost_usd is None
    db.close()


def test_environment_projection_separates_demo_and_release():
    value = project_environment()
    assert "demo_ready" in value and "release_ready" in value
    assert "search" in value and value["search"]["status"] in {"AVAILABLE", "RATE_LIMITED", "UNCONFIGURED", "FAILED", "VALIDATED"}


def test_invalidated_evidence_is_visible_and_marked(demo_b):
    root, result = demo_b
    db = initialize(root / "state.sqlite")
    rows = project_evidence(StateService(db, Workspace(root / "workspace")), result["research_id"])
    invalidated = [row for row in rows if row.status == "INVALIDATED"]
    assert invalidated and all(not row.valid for row in invalidated)
    db.close()


def test_artifact_browser_returns_relative_paths_only(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    rows = project_artifacts(StateService(db, Workspace(root / "workspace")), result["research_id"])
    assert rows and all(not (row.relative_path or "").startswith("C:") for row in rows)
    db.close()


def test_report_viewer_escapes_html(demo_a):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    state = StateService(db, Workspace(root / "workspace"))
    path = state.workspace.path(result["research_id"], "research_output/final_report.md")
    original = path.read_bytes()
    path.write_bytes(original + b"\n<script>alert(1)</script>\n")
    report = project_report(state, result["research_id"])
    assert "<script>" not in report.rendered_html and "&lt;script&gt;" in report.rendered_html
    path.write_bytes(original)
    db.close()


def test_dashboard_report_and_artifact_endpoints(demo_a):
    root, result = demo_a
    api = _api(root, result)
    assert api.request(f"/api/research/{result['research_id']}/report").status == 200
    assert api.request(f"/api/research/{result['research_id']}/artifacts").status == 200
    api.close()


def test_safe_figure_preview_endpoint(demo_a):
    root, result = demo_a
    api = _api(root, result)
    artifacts = api.get(f"/api/research/{result['research_id']}/artifacts")
    figure = next(item for item in artifacts if item.get("preview_allowed"))
    response = api.request(figure["preview_url"])
    assert response.status == 200 and response.content_type.startswith("image/")
    assert isinstance(response.body, bytes)
    api.close()


def test_dashboard_mode_indicator_distinguishes_live_demo_cached(demo_a):
    root, result = demo_a
    for mode in ("LIVE", "DEMO", "OFFLINE-CACHED"):
        api = _api(root, result, mode)
        assert api.get(f"/api/research/{result['research_id']}")["mode"] == mode
        api.close()


def test_demo_a_manifest_and_validation(demo_a):
    _, result = demo_a
    assert result["validation"]["passed"] is True
    manifest = result["demo_manifest"]
    assert Path(manifest["manifest_path"]).is_file()
    assert manifest["expected_verified_experiment_count"] == 2


def test_demo_b_invalidation_recovery_manifest(demo_b):
    _, result = demo_b
    assert result["validation"]["passed"] is True
    assert result["invalidated_experiment_id"] != result["replacement_experiment_id"]
    assert result["validation"]["invalidated_count"] >= 1


def test_release_export_and_hash_manifest(demo_a, tmp_path):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    exported = export_release(StateService(db, Workspace(root / "workspace")), result["research_id"], tmp_path / "release")
    manifest = json.loads(Path(exported["manifest"]).read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = tmp_path / "release" / item["path"]
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
    assert (tmp_path / "release" / "REPRODUCE.md").is_file()
    db.close()


def test_release_secret_exclusion(demo_a, tmp_path):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    state = StateService(db, Workspace(root / "workspace"))
    secret = state.workspace.path(result["research_id"], "research_output/.env")
    secret.write_text("OPENAI_API_KEY=should-not-export\n", encoding="utf-8")
    with pytest.raises(ReleaseExportError, match="secret"):
        export_release(state, result["research_id"], tmp_path / "secret-release")
    secret.unlink()
    db.close()


def test_clean_reproduction_instructions(demo_a, tmp_path):
    root, result = demo_a
    db = initialize(root / "state.sqlite")
    exported = export_release(StateService(db, Workspace(root / "workspace")), result["research_id"], tmp_path / "release")
    instructions = (tmp_path / "release" / "REPRODUCE.md").read_text(encoding="utf-8")
    assert "validate-core" in instructions and "htrsa.demo" in instructions
    db.close()


def test_429_is_mapped_to_rate_limited_and_bounded():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})
    client = ScholarlyHTTPClient(retries=2, transport=httpx.MockTransport(handler))
    with pytest.raises(ScholarlyError) as error:
        asyncio.run(client.get_json("https://api.openalex.org/works"))
    assert error.value.status == "RATE_LIMITED" and error.value.code == "SEARCH_RATE_LIMITED"
    assert len(calls) == 3 and search_status_from_error(error.value) == "RATE_LIMITED"


def test_http_retry_after_date_does_not_create_unbounded_wait():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, json={})
    client = ScholarlyHTTPClient(retries=1, transport=httpx.MockTransport(handler))
    with pytest.raises(ScholarlyError):
        asyncio.run(client.get_json("https://api.openalex.org/works"))
    assert len(calls) == 2 and client.last_status == "FAILED"


def test_unsafe_artifact_path_is_not_exposed(tmp_path):
    db = initialize(tmp_path / "state.sqlite")
    state = StateService(db, Workspace(tmp_path / "workspace"))
    rid = state.create_research("path safety")
    contract = ResearchContract(contract_id=new_id("C"), research_id=rid, task_type="read",
                                issued_by="system", assigned_role="manager", objective="read",
                                output_schema_id="read")
    state.issue_contract(contract)
    db.execute("INSERT INTO artifacts(artifact_id,research_id,contract_id,kind,payload_json,relative_path,status) VALUES(?,?,?,?,?,?,?)",
               ("ART-unsafe", rid, contract.contract_id, "file", "{}", "../../outside", "VERIFIED"))
    rows = project_artifacts(state, rid)
    assert next(row for row in rows if row.artifact_id == "ART-unsafe").path_safe is False
    db.close()
