"""실제 resolver의 값과 기본 화면·고급·내부·호환 필드 분류를 기록한다."""
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tests")]
from htrsa.control_plane import NewResearch
from htrsa.product_policy import resolve_effective_settings
from htrsa.workbench import WorkbenchAPI
from test_workbench import configure


def main():
    folder=ROOT/"build/beginner-v4/defaults"/uuid4().hex
    app=WorkbenchAPI(folder/"state.sqlite",folder/"workspace",launch=False)
    configure(app)
    value,origins=resolve_effective_settings(app.store,{"beginner_mode":True,"question":"기본값 검사"})
    normalized=NewResearch.model_validate(value).model_dump(mode="json")
    normal={"question","performance_profile","model_profile_id","attachments"}
    advanced={"run_limit_usd","adaptive_budget","advanced_performance_profile","model_reasoning","role_reasoning",
        "manual_role_override","routing","approved_worker_profiles","max_elapsed_sec","search_policy","search_attempt_limit",
        "search_required","public_search_query","egress","max_followups","verified_analysis_skills","verification_repair",
        "ridge_arithmetic_check","claim_evidence_provenance","cycle5_enabled"}
    legacy={"source_relative","research_depth","routing_profile_id","model_overrides","external_search"}
    rows=[{"field":name,"classification":"기본 화면" if name in normal else "선택적 고급 설정" if name in advanced else "호환 유지" if name in legacy else "내부 관리",
        "value":normalized[name],"source":origins.get(name,"내부 규칙"),"required_user_input":name=="question",
        "security_requirement":"모델·키·승인된 목적지·단가·유한 예산은 실제 전송 전에 필수","schema_required":field.is_required(),
        "precedence":"명시 고급 값 > 명시 주 설정 > 소유자 값 > 앱 기본값. false와 0을 값으로 보존."}
        for name,field in NewResearch.model_fields.items()]
    result={"fields":rows,"count":len(rows),"paid_calls":0,"profile_default":{"new_beginner":"AUTO","legacy_api":"DISABLED"}}
    text=json.dumps(result,ensure_ascii=False,indent=2)+"\n";text.encode("utf-8",errors="strict")
    (ROOT/"qa/qualified_profiles/defaults.json").write_text(text,encoding="utf-8")
    app.close()
    print(json.dumps({"fields":len(rows),"paid_calls":0}))


if __name__=="__main__":
    main()
