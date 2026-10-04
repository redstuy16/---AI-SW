"""상세 설계의 새 프로세스 복구와 고정 입력 조건 대조를 실행한다."""
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from htrsa.workbench import WorkbenchAPI
from htrsa.research_design import current_design, summary
from htrsa.qualified_profiles import conclusion_card
from test_research_design import execute, intent, variable
from test_beginner_v4 import QUESTION


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict"))


def main():
    base = ROOT / "build/design-v6/validation" / uuid4().hex
    boundaries = []
    for boundary in ("profile_after_verify", "profile_after_commit"):
        folder = base / boundary
        for phase in ("crash", "resume"):
            code = ("import qa.cycle12.recovery_probe as probe;"
                    "from test_research_design import detailed,intent;"
                    "probe.prepare=lambda app:detailed(app,{'fields':{'purpose':intent('고정 조건의 복구')}});"
                    "probe.main()")
            result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code,
                "--phase", phase, "--folder", str(folder), "--boundary", boundary], cwd=ROOT,
                capture_output=True, encoding="utf-8", timeout=120)
            expected = 79 if phase == "crash" else 0
            save(folder / (phase + "-process.json"), {"exit_code": result.returncode, "expected": expected,
                "stdout": result.stdout, "stderr": result.stderr})
            assert result.returncode == expected, result.stderr
        observed = json.loads((folder / "resume.json").read_text(encoding="utf-8"))
        app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
        try:
            rid = json.loads((folder / "request.json").read_text(encoding="utf-8"))["research_id"]
            design, used = current_design(app.read._state, rid), summary(app.read._state, rid)
            assert observed["passed"] and design["revision"] == 1 and used["trace"]
            assert all(t["design_hash"] == design["hash"] for t in used["trace"])
            observed.update(design_hash=design["hash"], design_revision=design["revision"],
                            used_arguments=[t["arguments"] for t in used["trace"]])
            boundaries.append(observed)
        finally:
            app.close()
    paired = []
    rubric = {"method": "two_period_comparison", "A": list(range(1981, 2001)),
              "B": list(range(2001, 2021)), "difference": 0.405}
    designs = [{}, {"approach": "EXISTING", "fields": {"purpose": intent("지정 기간 관측 비교"),
        "interpretation_limits": intent("관측 평균 차이만 설명"), "method": intent("two_period_comparison")},
        "variables": [variable("같은 자료 기준", "fixed", details={"maintain": intent("동일 원본 사용"), "check": intent("해시 확인")})],
        "comparisons": [{"id": "comparison-a", "text": "기간 A", "start": 1981, "end": 2000},
                        {"id": "comparison-b", "text": "기간 B", "start": 2001, "end": 2020}]}]
    for index, design in enumerate(designs):
        folder = base / ("paired-" + str(index))
        folder.mkdir(parents=True)
        app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
        try:
            rid, snap, runtime, provider, result = execute(app, design, QUESTION)
            card = conclusion_card(app.read._state, rid)
            assert result["selection"]["A"] == rubric["A"] and result["selection"]["B"] == rubric["B"]
            assert card["current"] and "0.405" in card["calculation"]
            paired.append({"condition": "simple" if index == 0 else "explicit_detail", "passed": True,
                "question": snap["question"], "selection": result["selection"], "calculation": card["calculation"],
                "design": summary(app.read._state, rid), "fake_model_calls": len(provider.calls),
                "paid_calls": 0, "wrong_approval": False, "normal_task_overblocked": False})
        finally:
            app.close()
    negative = []
    from htrsa.research_design import resolve_design
    unclear = resolve_design(QUESTION, {"fields": {"period": intent("2020년 상반기")}})
    natural = resolve_design(QUESTION, {"fields": {"period": intent("1981년부터 2000년까지와 2001년부터 2020년까지")}})
    assert any(i["field"] == "period" for i in unclear["issues"]) and not natural["issues"]
    negative.append({"case": "unresolved_period_and_natural_year_ranges", "passed": True, "paid_calls": 0})
    for name, design in (("physical_without_measurements", {"approach": "PHYSICAL"}),
                         ("causal_goal", {"fields": {"target": intent("인과관계 확인")}}),
                         ("forecast_goal", {"fields": {"primary_outcome": intent("미래 예측")}})):
        folder = base / name
        folder.mkdir(parents=True)
        app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
        try:
            from test_research_design import detailed, SOURCE
            from htrsa.control_plane import ControlError
            from htrsa.qualified_workflow import execute_profile
            import asyncio
            rid, snap, runtime, provider = detailed(app, design)
            try:
                asyncio.run(execute_profile(runtime, rid, snap, source_text=SOURCE.read_text(encoding="utf-8")))
            except ControlError as error:
                assert "RESEARCH_DESIGN_ACTION_BLOCKED" in str(error)
            else:
                raise AssertionError("미지원 목표를 실제 분석으로 승인했습니다")
            assert not provider.calls and not summary(app.read._state, rid)["trace"]
            negative.append({"case": name, "passed": True, "saved": bool(current_design(app.read._state, rid)),
                             "blocked_before_model": True, "paid_calls": 0})
        finally:
            app.close()
    folder = base / "question_design_conflict"
    folder.mkdir(parents=True)
    app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    try:
        from htrsa.qualified_workflow import amend_question
        rid, snap, runtime, provider, result = execute(app, {"fields": {"period": intent("1981~2000, 2001~2020")}})
        amend_question(app.read._state, rid, QUESTION.replace("2001~2020", "2011~2020"),
                       expected_version=app.read._state.state_version(rid))
        try:
            asyncio.run(execute_profile(runtime, rid, snap, replay=True))
        except ControlError as error:
            assert "RESEARCH_DESIGN_ACTION_BLOCKED" in str(error)
        else:
            raise AssertionError("남아 있는 상세 기간과 다른 질문의 계산을 승인했습니다")
        assert len(provider.calls) == 1 and not conclusion_card(app.read._state, rid)["current"]
        negative.append({"case": "question_design_conflict", "passed": True, "paid_calls": 0})
    finally:
        app.close()
    from test_verification_repair import prepare_case, corrupt_first_output
    from htrsa.research_design import initialize_design
    from htrsa.service import StateService
    from unittest.mock import patch
    (base / "skills-f3p").mkdir(parents=True)
    original_issue = StateService.issue_contract
    def issue_with_design(state, contract, **kwargs):
        if contract.assigned_role == "analysis_planner_worker" and not current_design(state, contract.research_id):
            dataset = state._one("SELECT dataset_id,sha256 FROM datasets WHERE research_id=?", (contract.research_id,))
            design = {"fields": {"method": intent("pearson_correlation")},
                      "variables": [variable("관측 결과", "outcome", binding={"dataset_id": dataset["dataset_id"],
                                    "sha256": dataset["sha256"], "column": "y"})]}
            initialize_design(state, contract.research_id, {"question": "Association in this sample?", "detailed_design": design})
        return original_issue(state, contract, **kwargs)
    with patch.object(StateService, "issue_contract", issue_with_design):
        db, state, agent, prepared = prepare_case(base / "skills-f3p", skill=True)
    try:
        rid = prepared["research_id"]
        frozen_hash = current_design(state, rid)["hash"]
        corrupt_first_output(state)
        result = asyncio.run(agent.resume(rid))
        assert result["verdict"] == "PASS" and current_design(state, rid)["hash"] == frozen_hash
        calls = db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name='analysis.skill'").fetchone()[0]
        assert calls == 2 and summary(state, rid)["trace"]
        negative.append({"case": "opt_in_skills_f3p_design_binding", "passed": True,
                         "analysis_skill_calls": calls, "intent_unchanged": True, "paid_calls": 0})
        from test_verified_analysis_skills import backtest_plan, series_rows
        from htrsa.real_tools import ToolRegistry, DataImportTool, DataProfileTool, VerifiedAnalysisSkillTool
        from htrsa.schemas import ToolRequest, ContextRef, RefType, Constraints, new_id
        from htrsa.mock import MockManager
        from htrsa.analysis_skills import SkillRequest
        from htrsa.research_design import amend_design
        inputs = state.workspace.root / "inputs"
        inputs.mkdir(exist_ok=True)
        rows = series_rows()
        source = inputs / "temporal.csv"
        source.write_bytes(("time,y\\n".replace("\\n", "\n") + "".join(r["time"] + "," + r["y"] + "\n" for r in rows)).encode("utf-8", errors="strict"))
        importing = MockManager().create_contract(rid, "고정 시계열 자료 준비").model_copy(update={"allowed_tools": ["data.import", "data.profile"], "constraints": Constraints(max_tool_calls=2)})
        import_task = state.issue_contract(importing)
        tools = ToolRegistry(state); tools.register(DataImportTool(state))
        key = new_id("TREQ")
        request = ToolRequest(request_id=key, research_id=rid, task_id=import_task, actor_id=importing.assigned_role, idempotency_key=key,
                              tool_name="data.import", args={"source_path": str(source), "research_id": rid})
        imported = tools.dispatch(importing.contract_id, request)
        assert imported.ok
        did = imported.result["dataset_id"]
        tools.register(DataProfileTool(state, importing.contract_id))
        key = new_id("TREQ")
        profile = ToolRequest(request_id=key, research_id=rid, task_id=import_task, actor_id=importing.assigned_role, idempotency_key=key,
                              tool_name="data.profile", args={"dataset_id": did})
        assert tools.dispatch(importing.contract_id, profile).ok
        dataset = state.dataset_record(did, rid)
        binding = {"dataset_id": did, "sha256": dataset["sha256"]}
        temporal = {"fields": {"method": intent("ridge_rolling_origin")}, "variables": [
            variable("시간", "time", binding={**binding, "column": "time"}),
            variable("측정값", "outcome", index=2, binding={**binding, "column": "y"})]}
        amend_design(state, rid, temporal, expected_version=state.state_version(rid))
        contract = MockManager().create_contract(rid, "고정 시계열 재검사").model_copy(update={"assigned_role": "analysis_planner_worker",
            "inputs": [ContextRef(type=RefType.dataset, id=did)], "allowed_tools": ["analysis.skill"], "constraints": Constraints(max_tool_calls=1)})
        task = state.issue_contract(contract)
        plan = backtest_plan(dataset_id=did, dataset_sha256=dataset["sha256"])
        skill = SkillRequest(research_id=rid, task_id=task, contract_id=contract.contract_id, plan_ref=contract.contract_id,
                             plan_version="1", plan_hash=plan.fingerprint(), plan=plan)
        key = new_id("TREQ")
        request = ToolRequest(request_id=key, research_id=rid, task_id=task, actor_id=contract.assigned_role, idempotency_key=key,
                              tool_name="analysis.skill", args=skill.model_dump(mode="json"))
        tools = ToolRegistry(state); tools.register(VerifiedAnalysisSkillTool(state, contract.contract_id))
        result = tools.dispatch(contract.contract_id, request)
        assert result.ok and result.result["execution"] == "succeeded"
        negative.append({"case": "temporal_skill_bound_time_and_outcome", "passed": True,
                         "execution": "REAL_TOOL_FIXED_OFFLINE_FIXTURE", "scientific_verification": "NOT_RUN", "paid_calls": 0})
    finally:
        db.close()
    record = {"passed": True, "execution": "FRESH_PROCESS_OFFLINE_FAKE_AGENT_REAL_TOOLS", "negative_cases": negative,
        "recovery": boundaries, "paired_protocol": {"rubric": rubric, "cases": paired,
        "interpretation": "입력 정보량이 다른 조건 대조입니다. UI 효과·사람의 사용성·Live 효능 비교가 아닙니다."},
        "paid_calls": 0, "live_efficacy": "NOT_VALIDATED", "output_dir": str(base)}
    save(base / "results.json", record)
    save(ROOT / "build/design-v6/validation_results.json", record)
    print(json.dumps({"passed": True, "boundaries": len(boundaries), "paired_cases": len(paired), "output_dir": str(base)}))


if __name__ == "__main__":
    main()
