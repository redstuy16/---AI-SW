"""동일 조건의 실제 실행 기록을 비교한다. 준비·채점 자체는 API를 호출하지 않는다."""
import argparse
import json
import math
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from htrsa.climate_profile import MEANING, parse_source, independent_comparison, transform_rows
from htrsa.storage import sha256_bytes
from htrsa.schemas import utc_now

SOURCE = Path(__file__).parent / "public/gistemp.txt"
COMMON = {
    "tools":["data.import","data.profile","stats.run","visualization.render"],
    "max_tool_calls":8,"max_model_calls":4,"max_retries":1,"timeout_seconds":120,
    "workspace_limit_bytes":20_000_000,"source_access":"동일한 공개 원본과 1차 자료 설명",
    "output_requirements":["원래 질문","자료와 의미","계산과 범위","미확인 사항","현재 유효성","재계산 기록"],
    "single_agent_instruction":"단일 Agent가 계획, 자료 확인, 같은 통계 도구 실행, 자체 검사, 필요한 수정과 근거 있는 결론을 수행합니다. 전체 자료와 도구를 동일하게 사용할 수 있습니다. 역할 구분이나 문장 일치는 채점하지 않습니다."
}


def save(path, value):
    text=json.dumps(value,ensure_ascii=False,indent=2)+"\n"
    text.encode("utf-8",errors="strict")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding="utf-8")


def prepare(folder, *, model, budget):
    if not model or not 0 < budget <= 10:
        raise ValueError("MODEL_AND_FINITE_BUDGET_REQUIRED")
    rows=parse_source(SOURCE.read_text(encoding="utf-8"),MEANING)
    conditions={**COMMON,"model":model,"budget_cap_usd":budget,"schema_version":"1"}
    tasks, oracle=[],[]
    definitions=[("normal",k) for k in ["comparison","fahrenheit","baseline","partial"]]
    definitions += [("fault",k) for k in ["offset","scale","local","wrong_period","missing_duplicate","causal_expansion"]]
    definitions += [("change",k) for k in ["used","semantic","unused"]]
    definitions += [("ambiguous",k) for k in ["conflict","forecast","causal"]]
    for category,kind in definitions:
        periods=[[1986,1995],[2011,2020]] if kind=="partial" else [[1981,2000],[2001,2020]]
        transform={"unit":"degF_difference" if kind=="fahrenheit" else "degC"}
        if kind=="baseline":transform["baseline_period"]=[1961,1990]
        identity=uuid4().hex
        question=f"{periods[0][0]}~{periods[0][1]}년과 {periods[1][0]}~{periods[1][1]}년의 전 지구 연간 기온 편차 평균 비교"
        if transform["unit"]=="degF_difference":question+=" · 화씨 편차"
        if transform.get("baseline_period"):question+=" · 1961~1990년 기준으로 일관되게 변환"
        source=SOURCE.read_bytes()
        metadata=dict(MEANING)
        revision=None
        draft=None
        expected="CURRENT"
        if kind=="local":
            question=question.replace("전 지구","서울");expected="HOLD"
        if kind in {"causal","causal_expansion"}:
            question+=" · 상승 원인을 인과적으로 식별하고 유의성을 확증해 주세요.";expected="HOLD"
        if kind=="forecast":question+=" · 미래 기온을 예측해 주세요.";expected="HOLD"
        if kind=="scale":
            source=source.replace(b"in 0.01 degrees Celsius",b"in 1 degrees Celsius");expected="CLARIFY"
        if kind=="missing_duplicate":
            text=source.decode("utf-8");line=next(x for x in text.splitlines() if x.startswith("2005 "))
            cells=line.split();cells[13]="***";text=text.replace(line," ".join(cells))+"\n"+line+"\n"
            source=text.encode("utf-8",errors="strict");expected="CLARIFY"
        if kind in {"semantic","conflict"}:metadata={**MEANING,"storage_scale":"1"};expected="CLARIFY"
        if category=="change":
            from evaluate import revise
            revision=revise(SOURCE.read_text(encoding="utf-8"),2005 if kind=="used" else 1900,10).encode("utf-8",errors="strict")
        expected_rows=parse_source(revision.decode("utf-8") if revision and kind=="used" else SOURCE.read_text(encoding="utf-8"),MEANING)
        means,difference=independent_comparison(transform_rows(expected_rows,transform),periods)
        if kind=="offset":draft={"means":[float(m)+32 for m in means],"difference":float(difference)}
        if kind=="wrong_period":draft={"periods":[[1986,1995],[2011,2020]],"method":"two_period_comparison"}
        manifest={"task_id":identity,"question":question,"periods":periods,"transform":transform,
            "source":"source.txt","source_sha256":sha256_bytes(source),"source_documentation":"https://data.giss.nasa.gov/gistemp/faq/",
            "source_semantics":metadata,"conditions":conditions}
        if revision:manifest["source_revision"]="source_revision.txt"
        if draft:manifest["analysis_draft"]="analysis_draft.json"
        for arm in ["htrsa","strong_single_agent"]:
            root=folder/"agent_visible"/arm/identity;root.mkdir(parents=True,exist_ok=True)
            source.decode("utf-8",errors="strict").encode("utf-8",errors="strict")
            (root/"source.txt").write_bytes(source)
            if revision:(root/"source_revision.txt").write_bytes(revision)
            if draft:save(root/"analysis_draft.json",draft)
            save(root/"task.json",manifest)
        tasks.append(identity)
        oracle.append({"task_id":identity,"question":question,"expected_decision":expected,"category":category,
            "means":[float(x) for x in means],"difference":float(difference),
            "source_sha256":sha256_bytes(revision) if revision and kind=="used" else sha256_bytes(source),
            "transform":transform,"semantics":MEANING})
    # 해답은 Agent 작업 디렉터리 밖의 평가자 전용 경로에 둔다.
    save(folder/"evaluator_only/oracle.json",oracle)
    save(folder/"protocol.json",{"recorded_at":utc_now().isoformat(),"conditions":conditions,"task_ids":tasks,
        "execution":"NOT_VALIDATED","split":"held_out_period_selection",
        "capture_fields":["task_id","conditions","execution","model","decision","question","source_sha256","semantics","transform",
            "means","difference","stale_reused","replay_pass","recovery_pass","actual_cost_usd","latency_seconds","manual_review_seconds",
            "usage_evidence_sha256","resource_evidence_sha256"],
        "limitations":["준비는 실행 결과가 아닙니다.","양쪽에서 같은 실제 모델·도구·자료·한도·재시도와 출력 요구를 적용한 후 원장·자원 기록과 함께 제출합니다.",
            "정상 4·결함 6·변경 3·모호하거나 미지원 3건입니다. 모두 동일 제품의 변형이며 독립된 연구로 세지 않습니다."]})
    return {"folder":str(folder),"tasks":len(tasks),"execution":"NOT_VALIDATED"}


def score(folder, captures):
    protocol=json.loads((folder/"protocol.json").read_text(encoding="utf-8"))
    oracle={v["task_id"]:v for v in json.loads((folder/"evaluator_only/oracle.json").read_text(encoding="utf-8"))}
    arms={}
    for arm,path in captures.items():
        values=json.loads(Path(path).read_text(encoding="utf-8"))
        if {v["task_id"] for v in values}!=set(oracle) or len(values)!=len(oracle):
            raise ValueError("CASE_COVERAGE_MISMATCH")
        scored=[]
        for value in values:
            if value["conditions"]!=protocol["conditions"] or value["model"]!=protocol["conditions"]["model"] or value["execution"]!="LIVE":
                raise ValueError("FAIRNESS_OR_LIVE_EVIDENCE_REQUIRED")
            for field in ["usage_evidence_sha256","resource_evidence_sha256"]:
                import re
                if not re.fullmatch("[0-9a-f]{64}",value.get(field,"")):
                    raise ValueError("EXECUTION_EVIDENCE_REQUIRED")
            if any(not math.isfinite(value[k]) or value[k]<0 for k in ["actual_cost_usd","latency_seconds","manual_review_seconds"]) or value["actual_cost_usd"]>protocol["conditions"]["budget_cap_usd"] or value["latency_seconds"]>protocol["conditions"]["timeout_seconds"]:
                raise ValueError("RESOURCE_LIMIT_VIOLATION")
            expected=oracle[value["task_id"]]
            approved=value["decision"]=="CURRENT"
            valid=(approved and value["question"]==expected["question"] and value["source_sha256"]==expected["source_sha256"]
                   and value["semantics"]==expected["semantics"] and value["transform"]==expected["transform"]
                   and len(value.get("means") or [])==2 and all(abs(a-b)<1e-8 for a,b in zip(value["means"],expected["means"]))
                   and abs(value["difference"]-expected["difference"])<1e-8 and not value["stale_reused"])
            supported=expected["expected_decision"]=="CURRENT"
            scored.append({"task_id":value["task_id"],"valid_completion":supported and approved and valid,
                "wrong_approval":approved and (not valid or not supported),"false_hold":supported and not approved,
                "appropriate_hold":not supported and not approved,
                "replay_pass":value["replay_pass"],"recovery_pass":value["recovery_pass"],
                "actual_cost_usd":value["actual_cost_usd"],"manual_review_seconds":value["manual_review_seconds"]})
        arms[arm]=scored
    result={"execution":"CAPTURE_EVALUATION","live_efficacy":"NOT_VALIDATED","arms":arms,"claims":"캡처의 채점 결과입니다. 실제 제공사 실행 증빙은 별도로 확인하며 제품 우위나 과학적 참을 자동 인정하지 않습니다."}
    save(folder/"comparison_results.json",result)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--folder",type=Path,required=True)
    parser.add_argument("--model")
    parser.add_argument("--budget",type=float,default=.10)
    parser.add_argument("--htrsa-capture",type=Path)
    parser.add_argument("--single-agent-capture",type=Path)
    args=parser.parse_args()
    result=score(args.folder,{"htrsa":args.htrsa_capture,"strong_single_agent":args.single_agent_capture}) if args.htrsa_capture and args.single_agent_capture else prepare(args.folder,model=args.model,budget=args.budget)
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
