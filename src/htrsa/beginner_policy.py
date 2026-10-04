"""기존 점검 결과를 사용자 조치 분류로 표시한다."""
from .qualified_profiles import registry


def classify(snapshot, preflight):
    result = dict(preflight)
    issues = []
    actions = {"CREDENTIAL_UNCONFIGURED": ("AI 연결에 키가 없습니다.", "설정에서 키를 등록해 주세요.", "AI 연결"),
               "PRICE_REQUIRED": ("안전한 비용 상한을 계산할 단가가 없습니다.", "AI 모델의 단가를 확인해 주세요.", "AI 연결"),
               "PRIMARY_MODEL_REQUIRED": ("사용할 AI 모델이 없습니다.", "AI 연결에서 모델을 확인해 주세요.", "AI 연결"),
               "NEEDS_RECONCILIATION": ("이전 요청의 비용이 아직 확정되지 않았습니다.", "사용량에서 해당 요청을 확인해 주세요.", "사용량")}
    for code in result["reasons"]:
        category = "USER_ACTION_REQUIRED" if code in actions or "BUDGET" in code or "APPROVAL" in code else "HARD_BLOCK"
        what, next_step, action = actions.get(code, ("연구를 시작할 수 없습니다.", "설정과 자료를 확인해 주세요.", "문제가 생겼어요"))
        issues.append({"code": code, "category": category, "message": what, "next_step": next_step, "action": action})
    if snapshot.get("research_profile_mode") == "AUTO":
        from .research_design import resolve_design
        resolution = resolve_design(snapshot["question"], snapshot.get("detailed_design"))
        profile = resolution["profile"]
        result["research_design"] = resolution
        issues += [{"code": "RESEARCH_DESIGN_" + item["status"], "category": "WARNING", "message": item["message"], "field": item["field"]}
                   for item in resolution["issues"]]
        if profile["status"] == "SUPPORTED" and snapshot.get("source_relative"):
            profile = profile | {"status": "CLARIFICATION_REQUIRED", "message": "첨부 자료의 단위·기준 기간·지역 범위를 먼저 확인해야 합니다. 현재 공개 자료 비교는 검증된 공식 원본을 사용합니다."}
        result["research_profile"] = profile
        if profile["status"] in {"UNSUPPORTED", "CLARIFICATION_REQUIRED"}:
            code = "PROFILE_" + profile["status"]
            result["reasons"] = list(dict.fromkeys(result["reasons"] + [code]))
            issues.append({"code": code, "category": "USER_ACTION_REQUIRED", "message": profile["message"],
                           "next_step": "원래 질문을 유지하거나 직접 질문의 범위를 바꿔 주세요.", "action": "질문 확인"})
        if profile["status"] == "SUPPORTED":
            issues.append({"code": "LITERATURE_NOT_APPLICABLE", "category": "NOT_APPLICABLE", "message": "공식 자료를 이용한 결정론적 비교 단계입니다."})
    issues += [{"code": "DEFAULT_" + name.upper(), "category": "AUTO_FIXABLE", "message": "기본값 적용 완료"} for name, source in snapshot.get("field_sources", {}).items() if source in {"application", "owner"}]
    if snapshot.get("attachments") and not snapshot.get("source_relative"):
        issues.append({"code":"ATTACHMENTS_STORAGE_ONLY","category":"WARNING","message":"파일은 저장되지만 현재 분석 입력으로 지정되지 않았습니다.","next_step":"분석할 CSV를 선택하거나 질문만으로 진행해 주세요.","action":"파일 확인"})
    result.update(issues=issues, ready=not result["reasons"], first_blocker=next(iter(result["reasons"]), None))
    return result
