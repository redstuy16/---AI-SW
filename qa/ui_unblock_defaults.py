"""서비스가 해석한 모든 초안 필드와 기본값의 출처를 기록한다."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tests")]
from htrsa.control_plane import NewResearch, ModelProfile, ROLES
from htrsa.workbench import WorkbenchAPI
from htrsa.product_policy import resolve_effective_settings
from test_workbench import configure


def main():
    (ROOT/"build/ui_start_unblock").mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(dir=ROOT/"build/ui_start_unblock",prefix="defaults-") as folder:
        app=WorkbenchAPI(Path(folder)/"state.sqlite",Path(folder)/"workspace",launch=False)
        configure(app)
        requested={"question":"기본 설정 검사","model_profile_id":"m","run_limit_usd":".10"}
        value,origins=resolve_effective_settings(app.store,requested)
        NewResearch.model_validate(value)
        properties=NewResearch.model_json_schema()["properties"]
        model=ModelProfile.model_validate(app.store.config("model","m"))
        locations={
            "question":"주 화면 · 연구 질문","title":"내부 · 질문에서 제목 생성",
            "model_profile_id":"주 화면 · 모델","selected_model_pool":"주 화면 · 모델 체크박스",
            "performance_profile":"주 화면 · 연구 성능","run_limit_usd":"주 화면 · 작업 예산 / 연결 확인",
            "adaptive_budget":"주 화면 · 예산에 맞춰 자동 조정","attachments":"주 화면 · 자료 첨부",
            "search_policy":"주 화면 · 웹 검색","advanced_performance_profile":"고급 · 연구 범위",
            "max_followups":"고급 · 추가 분석 횟수","max_elapsed_sec":"고급 · 최대 실행 시간",
            "model_reasoning":"고급 · 추론 수준","manual_role_override":"고급 · 단계별 모델 지정",
            "routing":"고급 · 최상위/중간/하위 모델","role_reasoning":"고급 · 단계별 추론",
            "approved_worker_profiles":"고급 · 분석 모델 자동 변경",
            "verified_analysis_skills":"고급 · 분석 도구 검사","ridge_arithmetic_check":"고급 · Ridge 수치 검사",
            "verification_repair":"고급 · F3-P 오류 복구","search_attempt_limit":"고급 · 웹 검색 시도 상한",
            "search_required":"고급 · 문헌 근거가 없으면 연구 중단","egress":"고급 · 모델에 보낼 자료",
            "public_search_query":"고급 · 공개 문헌 검색어"}
        rows=[]
        context={"question","model_profile_id","run_limit_usd","title"}
        for name,field in NewResearch.model_fields.items():
            rows.append({"field":name,"value":value[name],"source":origins[name],
                "optional":not field.is_required(),"input_kind":"사용자·상황 값" if name in context else "선택 설정",
                "resolved":True,"valid_null":value[name] is None,
                "ui_location":locations.get(name,"내부 · 호환 설정 / 현재 제어 없음"),
                "genuinely_required":name in {"question","model_profile_id","run_limit_usd"},
                "requirement":"연구 질문은 연구 실행에만 필요. 모델과 유한 예산은 실제 전송에 필요." if name in context else "기본값 또는 상속으로 진행",
                "default_rule":"명시 값 > 고급 역할 > 고급 연구 > 주 설정 > 소유자 > 앱. 보안·예산·기능 한도 적용",
                "resolved_type":type(value[name]).__name__,"constraints":properties[name],
                "requested_value":requested.get(name),"user_overridden":name in requested,
                "apply_when":"이번 제출 시 해석·저장. 실행 중 변경은 기존 설정 적용 경계. 전송 직전 권한·예산 재검사"})
            if name in {"routing","role_reasoning"}:
                rows[-1]["sub_controls"]=[
                    {"control":role if name=="routing" else "reasoning_"+role,
                     "inherit":True,"user_overridden":False,
                     "effective":value["routing"][role] if name=="routing" else model.reasoning_policy.value,
                     "constraint":"승인된 호환 모델만" if name=="routing" else "역할별 명시 값 > 전역 명시 값 > 해당 모델의 지원 기본값"}
                    for role in ROLES]
        rows.append({"field":"reviewer_profile_id","value":None,"source":"선택적 RoutingProfile","optional":True,
            "input_kind":"사용 안 함","resolved":True,"valid_null":True,"ui_location":"내부 · 기존 RoutingProfile",
            "genuinely_required":False,"default_rule":"기존 프로필이 사용하는 경우에만 적용","resolved_type":"NoneType",
            "constraints":"승인된 호환 모델 또는 null","user_overridden":False,"apply_when":"이번 제출"})
        for name in ("output_limit","timeout_sec","max_retries","reasoning_policy","temperature","top_p"):
            setting=getattr(model,name)
            setting=getattr(setting,"value",setting)
            rows.append({"field":"model."+name,"value":setting,"source":"기존 ModelProfile",
                "optional":True,"genuinely_required":False,"ui_location":"고급 · 모델 상세 설정",
                "default_rule":"기존 유효 프로필 유지. 자동 생성 시 출력 1024·시간 120초·모호한 과금 재시도 0",
                "resolved_type":type(setting).__name__,"constraints":ModelProfile.model_json_schema()["properties"][name],
                "user_overridden":False,"resolved":True,"valid_null":setting is None,"apply_when":"전송 직전 어댑터 호환성 재검사"})
        result={"settings_version":2,"precedence":["단계별 명시 값","고급 연구 설정","주 설정","소유자 기본값","앱 기본값"],
            "fields":rows,"paid_calls":0,"egress_note":"서버 기본값 none. UI의 질문 전송 시작으로 관리자 질문에만 selected 허용. 파일 전송은 별도 선택.",
            "legacy_note":"기존 CSV 요청은 설정 버전 1의 검색·추론 정책을 유지. 새 UI와 파일 없는 요청은 버전 2. 출력은 PDF로 정규화."}
        app.close()
    text=json.dumps(result,ensure_ascii=False,indent=2)+"\n";text.encode("utf-8",errors="strict")
    (ROOT/"build/ui_start_unblock/defaults.json").write_text(text,encoding="utf-8")
    print(json.dumps({"fields":len(rows),"all_resolved":all(row["resolved"] for row in rows),"paid_calls":0}))


if __name__=="__main__":main()
