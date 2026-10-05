"""잠정 과학 결과와 파일 출처를 독립적으로 검사한다."""
from __future__ import annotations

import math

from .database import from_json
from .storage import sha256_bytes, UnsafeWorkspacePathError


def numeric_fields(value: object, prefix: str = "") -> dict[str, int | float]:
    fields: dict[str, int | float] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            fields.update(numeric_fields(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            fields.update(numeric_fields(item, f"{prefix}.{index}"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        fields[prefix] = value
    return fields


def verify_scientific(state, mutation_row, payload, contract, check) -> None:
    science = payload.scientific
    research_id = mutation_row["research_id"]
    request, tool, agent = payload.tool_request, payload.tool_result, payload.agent_result
    db = state._db
    from .research_design import verify_design
    for item in verify_design(state, payload):
        check(item["check_id"], item["passed"], item["message"])
    from .qualified_profiles import verify_profile
    for item in verify_profile(state, payload):
        check(item["check_id"], item["passed"], item["message"])
    for item in state.cycle5.checks(research_id, payload):
        check(item["check_id"], item["passed"], item["message"])

    check("CONTRACT_EXISTS", contract is not None, "scientific contract exists")
    skill_call = request.tool_name == "analysis.skill"
    check("TOOL_ALLOWED", request.tool_name in {"stats.run", "analysis.skill"}
          and request.tool_name in contract.allowed_tools,
          "scientific tool is explicitly allowed")
    check("TOOL_SUCCESS", tool.ok, "stats tool succeeded")
    tool_history = db.execute(
        "SELECT tc.tool_name FROM tool_calls tc JOIN agent_runs ar USING(agent_run_id) WHERE ar.contract_id=?",
        (contract.contract_id,),
    ).fetchall()
    check("CONTRACT_TOOL_HISTORY", 0 < len(tool_history) <= contract.constraints.max_tool_calls
          and all(item["tool_name"] in contract.allowed_tools for item in tool_history),
          "all recorded contract tools are allowed and within budget")
    check("STATE_VERSION_MATCH", state.state_version(research_id) == mutation_row["base_state_version"],
          "state version is current")
    check("NO_DUPLICATE_COMMIT", db.execute("SELECT 1 FROM state_events WHERE mutation_id=?", (mutation_row["mutation_id"],)).fetchone() is None,
          "mutation has no commit event")

    dataset = db.execute("SELECT * FROM datasets WHERE dataset_id=? AND research_id=?", (science.dataset_id, research_id)).fetchone()
    check("DATASET_EXISTS", dataset is not None, "dataset record exists")
    check("DATASET_ACTIVE", dataset is not None and dataset["status"] != "INVALID",
          "dataset is not invalidated")
    dataset_ok = False
    if dataset is not None and state.workspace is not None:
        try:
            path = state.workspace.path(research_id, dataset["stored_path"])
            data = path.read_bytes()
            dataset_ok = sha256_bytes(data) == dataset["sha256"] and len(data) == dataset["size_bytes"]
        except (OSError, UnsafeWorkspacePathError):
            dataset_ok = False
    check("DATASET_HASH_MATCH", dataset_ok, "stored dataset matches SHA-256 and size")

    expected = {"profile": (science.profile_artifact_id, "DATA_PROFILE"),
                "stats": (science.stats_artifact_id, "SKILL_RESULT" if skill_call else "STATS_RESULT"),
                "figure": (science.figure_artifact_id, "FIGURE")}
    if skill_call and science.plan_artifact_id:
        expected["plan"] = (science.plan_artifact_id, "SKILL_PLAN")
    records = {}
    for name, (artifact_id, kind) in expected.items():
        record = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND research_id=?", (artifact_id, research_id)).fetchone()
        allowed_status = {"PENDING", "VERIFIED"} if name == "profile" else {"PENDING"}
        if record is not None and record["artifact_type"] == kind and record["status"] in allowed_status:
            records[name] = record
    check("ARTIFACT_EXISTS", len(records) == len(expected), "profile and new scientific artifacts are pending")
    hashes_ok = len(records) == len(expected) and state.workspace is not None
    artifact_bytes = {}
    for name, record in records.items():
        try:
            path = state.workspace.path(research_id, record["relative_path"])
            data = path.read_bytes()
            artifact_bytes[name] = data
            hashes_ok = hashes_ok and sha256_bytes(data) == record["sha256"] and len(data) == record["size_bytes"]
        except (OSError, UnsafeWorkspacePathError):
            hashes_ok = False
    check("ARTIFACT_HASH_MATCH", hashes_ok, "all file artifacts match registered SHA-256 and size")

    document = None
    profile_document = None
    if hashes_ok:
        try:
            document = from_json(artifact_bytes["stats"].decode("utf-8", errors="strict"))
            profile_document = from_json(artifact_bytes["profile"].decode("utf-8", errors="strict"))
        except (OSError, UnicodeError, ValueError):
            document = None
            profile_document = None
    check("PROFILE_SOURCE_MATCH", isinstance(profile_document, dict) and dataset is not None
          and profile_document.get("dataset_id") == science.dataset_id
          and profile_document.get("dataset_sha256") == dataset["sha256"]
          and profile_document.get("row_count") == dataset["row_count"],
          "profile artifact identifies the registered dataset and row count")
    expected_result = document.get("result") if isinstance(document, dict) else None
    match = (document is not None and expected_result == tool.result == agent.output
             and document.get("dataset_id") == science.dataset_id
             and dataset is not None and document.get("dataset_sha256") == dataset["sha256"]
             and document.get("tool_call_id") == request.request_id
             and (request.args.get("plan", {}).get("dataset_id") if skill_call else request.args.get("dataset_id")) == science.dataset_id
             and (request.args.get("plan", {}).get("method") if skill_call else request.args.get("method")) == science.method
             and tool.result.get("method") == science.method
             and tool.provenance.get("stats_artifact_id") == science.stats_artifact_id)
    check("RESULT_FIELD_MATCH", match, "agent and tool fields match trusted stats artifact")
    if skill_call:
        from .analysis_skills import SkillPlan, SkillResult
        plan_ok = False
        try:
            planned = SkillPlan.model_validate(request.args["plan"])
            parsed = SkillResult.model_validate(tool.result)
            saved = from_json(artifact_bytes["plan"].decode("utf-8", errors="strict"))
            plan_ok = (science.plan_artifact_id is not None
                       and request.args["plan_hash"] == planned.fingerprint() == saved["fingerprint"]
                       == parsed.plan_fingerprint == document["plan_fingerprint"]
                       and saved["plan"] == planned.model_dump(mode="json")
                       and saved["dataset_sha256"] == dataset["sha256"]
                       and parsed.applicability == "applicable" and parsed.execution == "succeeded"
                       and request.args["plan_ref"] == contract.contract_id
                       and request.args["plan_version"] == "1"
                       and tool.provenance.get("plan_artifact_id") == science.plan_artifact_id)
        except (KeyError, TypeError, ValueError, UnicodeError):
            pass
        check("SKILL_PLAN_MATCH", plan_ok, "frozen plan, version and result match trusted artifacts")

    # 그림 파일 해시 외에 생성 요청·실제 점·축과 분석 자료의 연결도 검사한다.
    figure_matches = False
    try:
        if not dataset_ok:
            raise ValueError("그림의 원자료가 등록된 해시와 일치하지 않습니다.")
        figure = records["figure"]
        history = db.execute("SELECT request_json,result_json FROM tool_calls WHERE request_id=?", (figure["producer_id"],)).fetchone()
        plotted_request, plotted_result = from_json(history[0]), from_json(history[1])
        if skill_call:
            figure_matches = (figure["producer_id"] == request.request_id
                              and plotted_request["tool_name"] == "analysis.skill"
                              and plotted_result["provenance"]["figure_artifact_id"] == science.figure_artifact_id
                              and plotted_result["provenance"]["dataset_sha256"] == dataset["sha256"])
        else:
            import csv
            import io
            import json
            args = plotted_request["args"]
            x_name, y_name = args["x"], args["y"]
            rows = list(csv.DictReader(io.StringIO(state.workspace.path(research_id, dataset["stored_path"]).read_text(encoding="utf-8-sig", errors="strict"))))
            if any(not isinstance(row.get(x_name), str) or not isinstance(row.get(y_name), str) for row in rows):
                raise ValueError("그림의 분석 변인에 누락된 CSV 셀이 있습니다.")
            pairs = [(float(row[x_name]), float(row[y_name])) for row in rows if row[x_name].strip() and row[y_name].strip()]
            x, y = [v[0] for v in pairs], [v[1] for v in pairs]
            plot = plotted_result["result"]["plot"]
            points_hash = sha256_bytes(json.dumps({"x": x, "y": y}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8", errors="strict"))
            period_plot = science.method == "two_period_comparison"
            expected_columns = ({"x": request.args["variables"]["year"], "y": request.args["variables"]["value"]}
                                if period_plot else request.args["variables"])
            figure_matches = (plotted_request["tool_name"] == "visualization.render" and plotted_result["ok"]
                              and plotted_request["research_id"] == research_id and args["dataset_id"] == science.dataset_id
                              and args["plot_type"] == plot["plot_type"] == ("line" if period_plot else "scatter")
                              and {"x": x_name, "y": y_name} == expected_columns
                              and plot["x"] == x_name and plot["y"] == y_name
                              and plot["x_label"] == (args.get("x_label") or x_name)
                              and plot["y_label"] == (args.get("y_label") or y_name)
                              and plot["point_count"] == len(pairs)
                              and (period_plot or len(pairs) == tool.result["n"])
                              and plot["data_sha256"] == points_hash
                              and plotted_result["provenance"]["dataset_sha256"] == dataset["sha256"]
                              and plotted_result["result"]["artifact_id"] == science.figure_artifact_id)
    except (KeyError, TypeError, ValueError, UnicodeError, OSError):
        pass
    check("FIGURE_SOURCE_MATCH", figure_matches, "그림의 점·축·분석 변인·자료 출처가 일치합니다.")

    trusted_fields = numeric_fields(expected_result) if isinstance(expected_result, dict) else {}
    fields = numeric_fields(agent.output)
    provenance = science.numeric_provenance
    provenance_ok = bool(fields) and set(fields) == set(trusted_fields) == set(provenance)
    if provenance_ok:
        for name, value in fields.items():
            entry = provenance[name]
            provenance_ok = (entry.field == name and entry.artifact_id == science.stats_artifact_id
                             and entry.tool_call_id == request.request_id and entry.dataset_id == science.dataset_id
                             and dataset is not None and entry.dataset_sha256 == dataset["sha256"]
                             and math.isfinite(float(value)) and math.isfinite(float(entry.value))
                             and math.isclose(float(value), float(entry.value), rel_tol=1e-12, abs_tol=1e-12)
                             and math.isclose(float(value), float(trusted_fields[name]), rel_tol=1e-12, abs_tol=1e-12))
            if not provenance_ok:
                break
    check("NUMERIC_PROVENANCE", provenance_ok, "every numeric output field resolves to stats artifact, call, and dataset")
    check("SAMPLE_SIZE_PRESENT", isinstance(expected_result, dict) and isinstance(expected_result.get("n"), int)
          and expected_result["n"] > 0, "stats artifact contains a positive sample size")

    experiment = db.execute("SELECT * FROM experiments WHERE experiment_id=? AND research_id=?", (science.experiment_id, research_id)).fetchone()
    evidence = db.execute("SELECT * FROM evidence WHERE evidence_id=? AND research_id=?", (science.evidence_id, research_id)).fetchone()
    check("PENDING_ENTITIES", experiment is not None and experiment["status"] == "PENDING_VERIFICATION"
          and experiment["dataset_id"] == science.dataset_id and experiment["result_artifact_id"] == science.stats_artifact_id
          and evidence is not None and evidence["status"] == "PENDING"
          and evidence["experiment_id"] == science.experiment_id and evidence["source_ref"] == science.stats_artifact_id,
          "experiment and evidence are pending until commit")
