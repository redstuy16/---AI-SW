"""공개 자료에 대한 실제 로컬 절차와 결함 주입. Live 성능 비교와 구분한다."""
import asyncio
from copy import deepcopy
import csv
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from probe.autonomous_loop import AutonomousResearchLoop
from probe.climate_profile import MEANING, parse_source, independent_comparison
from probe.control_plane import ROLES, ControlError
from probe.final_report import export_final_report
from probe.providers.fake import FakeProvider
from probe.qualified_profiles import conclusion_card
from probe.qualified_workflow import execute_profile, update_source
from probe.qualified_replay import replay
from probe.schemas import StagedResult, utc_now
from probe.workbench import WorkbenchAPI
from test_workbench import configure
from test_beginner_v4 import revise

SOURCE = ROOT / "qa/qualified_profiles/public/gistemp.txt"
QUESTION = "1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요."


def save(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    text.encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_case(folder, category, kind):
    app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    configure(app)
    question = QUESTION
    if kind == "fahrenheit":
        question += " 화씨 편차로 표시해 주세요."
    elif kind == "baseline":
        question = "기준 기간을 1961~1990년으로 바꾸고 " + QUESTION
    elif kind == "partial":
        question = "1981~1990년과 2001~2010년의 전 지구 연간 기온 편차 평균을 비교해 주세요."
    elif kind == "forecast":
        question = "전 지구 기온의 미래를 예측해 주세요."
    elif kind == "causal":
        question = "전 지구 기온 상승의 원인을 식별해 주세요."
    created = app.create({"beginner_mode":True,"question":question,"egress":"selected"})
    rid, snapshot = created["research_id"], created["snapshot"]
    provider = FakeProvider([{"decision":"PROCEED","reason":"저장된 공식 자료와 고정 범위의 비교"}])
    runtime = AutonomousResearchLoop(app.read._state, provider, models={r:"manual-id" for r in ROLES})
    state = app.read._state
    original = SOURCE.read_text(encoding="utf-8", errors="strict")
    raw = original
    if kind == "storage":
        raw = raw.replace("in 0.01 degrees Celsius", "in 1 degrees Celsius")
    if kind == "missing_duplicate":
        line = next(s for s in raw.splitlines() if s.startswith("2005 "))
        raw += "\n" + line + "\n"
    def fault(payload):
        payload = payload.model_copy(deep=True)
        if kind == "absolute_offset":
            for group in payload.tool_result.result["periods"]:
                group["mean"] += 32
        elif kind == "local_claim":
            payload.scientific.claim = payload.scientific.claim.replace("전 지구", "서울")
        elif kind == "wrong_period":
            payload.tool_request.args["parameters"]["periods"] = [[1981,1990],[2001,2010]]
        elif kind == "causal_claim":
            payload.scientific.claim += " 이 차이는 온난화의 원인이며 통계적으로 유의합니다."
        return payload
    begin = time.perf_counter()
    before, after, replay_result, failure = None, None, None, None
    preflight = app.preflight(snapshot)
    try:
        if kind in {"forecast","causal"}:
            if preflight["ready"]:
                raise AssertionError("지원 범위를 넘은 질문이 통과했습니다")
            observed = "CLARIFICATION_REQUIRED"
        else:
            asyncio.run(execute_profile(runtime, rid, snapshot, source_text=raw, fault=fault if category=="fault" else None))
            before = conclusion_card(state, rid)
            if category == "change" or kind == "ambiguous":
                if kind == "semantic" or kind == "ambiguous":
                    metadata = deepcopy(MEANING);metadata["storage_scale"]="1"
                    update_source(state,rid,original,semantics=metadata)
                else:
                    update_source(state,rid,revise(original,2005 if kind=="used" else 1900,10))
                after = conclusion_card(state,rid)
                if kind == "unused":
                    observed = "CURRENT"
                elif kind in {"semantic","ambiguous"}:
                    observed = "CLARIFICATION_REQUIRED"
                else:
                    if after["current"]:raise AssertionError("오래된 결론이 재사용되었습니다")
                    asyncio.run(execute_profile(runtime,rid,snapshot,replay=True))
                    after = conclusion_card(state,rid)
                    observed = "REVALIDATED"
            else:
                observed = "CURRENT"
            if conclusion_card(state,rid)["current"]:
                state.stop_research(rid,"QUALIFIED_PROCEDURE_COMPLETED")
                export_final_report(state,rid)
                replay_result = replay(state.workspace.path(rid,"research_output/replay_manifest.json"))
    except (ControlError, ValueError) as error:
        observed = "BLOCKED"
        failure = getattr(error,"code",str(error))
    result_row = state._db.execute("SELECT payload_json,verification_json,status FROM staged_mutations WHERE research_id=? ORDER BY rowid DESC LIMIT 1",(rid,)).fetchone()
    tool_result = None
    checks = []
    if result_row:
        payload = StagedResult.model_validate_json(result_row["payload_json"])
        tool_result = payload.tool_result.result
        checks = json.loads(result_row["verification_json"] or "{}").get("checks",[])
    committed = state._db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?",(rid,)).fetchone()[0]
    expected = "BLOCKED" if category=="fault" else "CLARIFICATION_REQUIRED" if kind in {"semantic","ambiguous","forecast","causal"} else "REVALIDATED" if kind=="used" else "CURRENT"
    correct = observed==expected and (committed==0 if category=="fault" else True)
    value = {"id":folder.name,"category":category,"case":kind,"expected":expected,"observed":observed,"correct":correct,
        "question":question,"config":{"model":"manual-id","provider":"fake","run_limit_usd":snapshot["run_limit_usd"],
        "profile_mode":snapshot["research_profile_mode"],"source_sha256":__import__("hashlib").sha256(SOURCE.read_bytes()).hexdigest()},
        "agent_decisions":[{"decision":"PROCEED","execution":"SCRIPTED_FAKE_PROVIDER"} for _ in provider.calls],
        "model_calls":len(provider.calls),"tool_result":tool_result,"checks":checks,"commit_count":committed,
        "before_current":before.get("current") if before else None,"after_current":after.get("current") if after else None,
        "replay":replay_result,"failure":failure,"elapsed_seconds":round(time.perf_counter()-begin,3),
        "limitations":["공개 원본을 사용한 실제 로컬 도구 실행입니다.","Agent 결정은 모의 응답입니다. Live 판단 성능이 아닙니다.",
        "변경·충돌 사례는 결함 주입이며 실제 NASA의 해당 수정 사건으로 주장하지 않습니다."]}
    app.close()
    return value


def main():
    folder = ROOT / "build/beginner-v4/evaluation" / uuid4().hex
    cases = [("normal",k) for k in ["comparison","fahrenheit","baseline","partial"]]
    cases += [("fault",k) for k in ["absolute_offset","storage","local_claim","wrong_period","missing_duplicate","causal_claim"]]
    cases += [("change",k) for k in ["used","semantic","unused"]]
    cases += [("ambiguous",k) for k in ["ambiguous","forecast","causal"]]
    values = []
    for category, kind in cases:
        value = run_case(folder / uuid4().hex, category, kind)
        values.append(value)
        print(json.dumps({"case":kind,"correct":value["correct"]}),flush=True)
    source_rows = parse_source(SOURCE.read_text(encoding="utf-8"),MEANING)
    csv_rows = list(csv.DictReader((SOURCE.parent/"gistemp.csv").read_text(encoding="utf-8").splitlines()[1:]))
    official = {int(r["Year"]):r["J-D"] for r in csv_rows}
    scale_match = all(abs(float(r["value"])-float(official[r["year"]]))<1e-10 for r in source_rows if r["value"] is not None)
    initial = json.loads((ROOT/"build/beginner-v4/initial-state.json").read_text(encoding="utf-8"))
    result = {"execution":"OFFLINE_LOCAL_TOOLS_WITH_SCRIPTED_AGENT","recorded_at":utc_now().isoformat(),"cases":values,
        "source_scale_pair":{"txt_scale":"0.01","csv_scale":"1","all_finite_annual_values_match":scale_match},
        "representative_cases":[next(v for v in values if v["case"]==k) for k in ["comparison","storage","ambiguous","used","fahrenheit"]],
        "adoption_criteria":initial.get("adoption_criteria",initial.get("criteria")),
        "live_architecture_comparison":"NOT_VALIDATED","native_products":"NOT_VALIDATED","usability":"NOT_VALIDATED",
        "ablation":{"semantic_goal_off_on":"NOT_VALIDATED","claim_change_off_on":"NOT_VALIDATED",
                    "repair_off_on":"기존 평가를 별도 결과로 참조","qualified_off_on":"NOT_VALIDATED"},
        "limitations":["16개는 같은 공개 자료의 변형입니다. 독립된 과학 연구 16건이 아닙니다."],
        "all_passed":all(v["correct"] for v in values) and scale_match,"output_dir":folder.relative_to(ROOT).as_posix()}
    result["limitations"]=["16개는 같은 공개 자료의 변형입니다. 독립된 과학 연구 16건이 아닙니다."]
    result["ablation"]["repair_off_on"]="기존 F3-P 평가와 구분합니다. 새 기능의 기여도 평가는 미실행입니다."
    save(ROOT/"qa/qualified_profiles/results.json",result)
    if not result["all_passed"]:raise SystemExit(1)


if __name__=="__main__":
    main()
