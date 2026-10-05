"""탐구 한 건의 다음 행동을 선택하고 관찰을 저장하는 과학 전용 순환."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from .agent_schemas import AnalysisPlan
from .database import from_json, to_json
from .research_report import ReportDraft
from .providers.base import ModelProviderError
from .research_schemas import (HypothesisProposal, HypothesisScore, HypothesisShortlist,
                               ResearchAction, StopReason)
from .schemas import ContextRef, RefType, StrictModel
from .service import ContractViolationError
from .scientific_compute import CalculationPlan
from .scientific_dataset_compute import DatasetCalculationPlan
from .climate_data import ClimateDataPlan
from .science_sources import ScienceSourcePlan


class ScienceDecision(StrictModel):
    INSTRUCTIONS: ClassVar[str] = (
        "고등학생과 고등학교 과학 교사의 탐구 한 건을 완성한다. 이번 관찰을 평가하고 다음 행동 하나를 선택한다. "
        "보고서 작성 전에 관련 주제 검색을 먼저 시도한다. initial_search의 결과와 확보한 근거를 확인한 뒤 COMPLETE에 보고서 전체를 반환한다. "
        "확보한 근거가 부족하면 질문의 핵심 개념과 조건으로 SEARCH를 보완한다. 검색 실패나 근거 미확보만으로 같은 검색을 반복하지 않는다. "
        "search_queries는 한 번에 최대 세 개이고 각 검색어는 400자 이하이다. 추가 검색어는 다음 판단으로 나눈다. "
        "관련 VERIFIED 근거가 있으면 results의 해석에 연결하고 claims에 실제 근거 ID와 원문 인용을 포함한다. 모델 지식은 검증된 문헌이나 실측 결과가 아니다. 수치 결과와 인용을 만들지 않는다. "
        "ANALYZE는 제공된 dataset_id와 실제 열, 지원되는 분석 방법을 가진 analysis_plan을 반환한다. "
        "CSV의 회귀·상관·변화량은 CALCULATE_DATASET을 선택하고 dataset_calculation_plan에 실제 dataset_id, name(영문 식별자), operation(OLS/CORRELATION/SUMMARY), target, predictors를 작성한다. "
        "OLS는 단순·다중 선형회귀이며 각 모델을 순서대로 실행한다. SUMMARY는 처음·끝·변화량, CORRELATION은 Pearson·Spearman이다. "
        "변화량·장기 추세·상관이 요청되면 필요한 변수마다 SUMMARY, 시간 설명변수 OLS, 변수 간 CORRELATION을 실제로 실행한다. "
        "다중 회귀를 했다는 이유로 이 필수 계산을 생략하지 않는다. 독립 자료 비교와 차분·추세 제거 같은 대안은 필수 계산을 확보한 뒤 비교한다. "
        "CALCULATE_DATASET은 calculation_plan이나 analysis_plan 대신 dataset_calculation_plan을 사용한다. "
        "관측 자료를 직접 가져와야 하면 FETCH_DATA와 data_plan.sources에 data_catalog의 자료 종류를 선택한다. "
        "교란요인 분석이 요청되면 필요한 proxy 자료도 같은 FETCH_DATA에 포함한다. 제목 검색으로 관측 자료나 대안 분석을 대신하지 않는다. "
        "공식 평가 원문이 필요하면 FETCH_SOURCE와 document_plan.sources에서 source_catalog 항목을 선택하고 topics에 필요한 핵심 용어를 넣는다. 원문 문단을 실제 수집·검증한다. "
        "remaining_search_requests가 0이면 SEARCH를 반복하지 않는다. 검증된 문헌의 범위와 한계를 기록하고 남은 계산·방법 비교·반론 검토·보고서를 진행한다. "
        "remaining_decisions가 작아지면 확보한 자료의 필수 분석과 보고서에 집중하고 이미 확보한 자료를 재검색하지 않는다. "
        "관리자는 도구를 직접 호출하지 않으므로 관리자 계약의 allowed_tools=[]와 max_tool_calls=0은 정상이다. "
        "CALCULATE, CALCULATE_DATASET, FETCH_DATA는 실행 계획을 제출하는 행동이다. 별도 작업자가 허용 도구 한 개의 계약으로 실행하고 결과를 돌려준다. 이를 도구 사용 불가로 해석하지 않는다. "
        "공식과 주어진 수치 계산은 CALCULATE를 사용한다. calculation_plan.inputs에 입력값, calculations에 name·expression·unit을 넣는다. "
        "수식은 + - * / ** log sqrt exp와 앞선 이름만 지원한다. 결과는 float·Decimal로 검산되며 다음 판단에 돌아온다. "
        "계산 결과를 즉시 쓰지 말고 검산된 sentence를 보고서에 한 줄씩 그대로 사용하고 numeric_mentions.kind=calculated, artifact_id와 field를 연결한다. "
        "문제에서 주어진 수치는 kind=provided로 구분하고 임의 관측값으로 취급하지 않는다. "
        "어려운 분석 계획이나 독립 검토가 필요할 때만 DELEGATE를 선택한다. "
        "REFINE_DESIGN은 실험 조건·변인·측정 절차의 구체적인 개선안을 design_updates에 반환한다. "
        "이전 검토의 정확한 요청 방법과 미해결 문제를 보존하며 같은 행동을 이유 없이 반복하지 않는다. "
        "COMPLETE는 원래 질문을 충족한 한국어 report_draft 전체를 포함한다. 실측 자료 없는 탐구는 principle, 사용자가 명시한 설계만 design, "
        "원래 요청에 목차·가설·계획·방법 비교·반론·후속 연구가 있으면 일반적인 간결화 지침보다 우선해 각 항목을 작성한다. "
        "requested_outputs가 있으면 항목 이름을 본문 소제목으로 전부 사용한다. 다섯 기본 본문 필드 안에 나누어 배치한다. 요청된 후속 연구와 대응표를 생략하지 않는다. "
        "출처 검토는 literature, 실제 검증 계산은 analysis report_type을 쓴다. stop=false이면 계속 판단한다. "
        "search_required가 참이면 검증된 원문 근거와 claims 인용을 확보하기 전에는 완료하지 않는다. "
        "시각자료의 실제 그림 ID만 figure_refs에 넣는다. COMPLETE 외 판단은 다음 행동에 필요한 핵심만 간결하게 쓴다. "
        "모든 출력은 지정된 구조화 스키마를 따른다.\n" + ReportDraft.INSTRUCTIONS
    )
    action: Literal["SEARCH", "CALCULATE", "CALCULATE_DATASET", "FETCH_DATA", "FETCH_SOURCE", "ANALYZE", "REFINE_DESIGN", "DELEGATE", "COMPLETE", "NEED_INPUT"]
    rationale: str = Field(min_length=1, max_length=2400)
    research_question: str = Field(default="", max_length=2000)
    goal_evaluation: str = Field(default="", max_length=2400)
    search_queries: list[str] = Field(default_factory=list, max_length=3)
    analysis_plan: AnalysisPlan | None = None
    calculation_plan: CalculationPlan | None = None
    dataset_calculation_plan: DatasetCalculationPlan | None = None
    data_plan: ClimateDataPlan | None = None
    document_plan: ScienceSourcePlan | None = None
    design_updates: dict[str, Any] = Field(default_factory=dict)
    delegate_role: Literal["analysis_planner_worker", "verification_coordinator"] | None = None
    delegate_objectives: list[str] = Field(default_factory=list, max_length=2)
    report_draft: ReportDraft | None = None
    stop: bool = True

    @model_validator(mode="after")
    def required_payload(self):
        if self.action == "SEARCH" and not self.search_queries:
            raise ValueError("SEARCH에는 검색어가 필요합니다.")
        if any(not query.strip() or len(query) > 400 for query in self.search_queries):
            raise ValueError("검색어는 비어 있지 않은 400자 이하 문장이어야 합니다.")
        if self.action == "ANALYZE" and self.analysis_plan is None:
            raise ValueError("ANALYZE에는 고정 분석 계획이 필요합니다.")
        if self.action == "CALCULATE" and self.calculation_plan is None:
            raise ValueError("CALCULATE에는 입력값과 수식 계획이 필요합니다.")
        if self.action=='CALCULATE_DATASET' and self.dataset_calculation_plan is None:
            raise ValueError('CSV 계산 계획이 필요합니다.')
        if self.action=='FETCH_DATA' and self.data_plan is None:
            raise ValueError('공개 자료 수집 계획이 필요합니다.')
        if self.action=='FETCH_SOURCE' and self.document_plan is None:
            raise ValueError('공식 원문 수집 계획이 필요합니다.')
        if self.action == "REFINE_DESIGN" and not self.design_updates:
            raise ValueError("REFINE_DESIGN에는 구체적인 개선안이 필요합니다.")
        if self.action == "DELEGATE" and self.delegate_role is None:
            raise ValueError("DELEGATE에는 위임 역할이 필요합니다.")
        if any(not objective.strip() or len(objective) > 1000 for objective in self.delegate_objectives):
            raise ValueError("독립 검토 목표는 1000자 이하의 비어 있지 않은 문장이어야 합니다.")
        if self.action == "COMPLETE" and self.report_draft is None:
            raise ValueError("COMPLETE에는 보고서 초안이 필요합니다.")
        return self


class ScienceDesignReview(StrictModel):
    verdict: Literal["ACCEPT", "REVISE", "NEED_INPUT"]
    rationale: str = Field(min_length=1)
    issues: list[str] = Field(default_factory=list, max_length=8)
    design_updates: dict[str, Any] = Field(default_factory=dict)


def science_enabled(runtime) -> bool:
    settings = getattr(runtime, "science_settings", {}) or {}
    return bool(settings.get("enabled") or settings.get("execution_mode") == "SCIENCE_AUTO")


def _current_limits(runtime):
    settings = getattr(runtime, "science_settings", {}) or {}
    return (settings, min(20, max(1, int(settings.get("max_decisions", 6)))),
            min(3600, max(1, float(settings.get("max_runtime_sec", 300)))),
            min(5, max(1, int(settings.get("no_progress_limit", 5)))))


def _fingerprint(value) -> str:
    return hashlib.sha256(to_json(value).encode("utf-8", errors="strict")).hexdigest()


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _observations(state, rid):
    observations = [from_json(row[0]) for row in state._db.execute(
        "SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'science:observation:%' "
        "AND status='COMPLETED' ORDER BY rowid", (rid,))]
    corrections = {item["iteration"]: item for row in state._db.execute(
        "SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'science:revalidation:%' "
        "AND status='COMPLETED' ORDER BY rowid", (rid,)) for item in [from_json(row[0])]}
    return [corrections.get(item["iteration"], item) for item in observations]


def _required_literature_problem(runtime, rid, draft):
    if not (getattr(runtime, "science_settings", {}) or {}).get("search_required"):
        return None
    from .search_policy import qualified_literature
    from .research_report import report_inputs
    verified = {item["evidence_id"] for item in report_inputs(runtime.state, rid)["evidence"]}
    if not qualified_literature(runtime.state, rid) or not any(claim.evidence_id in verified for claim in draft.claims):
        return "REQUIRED_LITERATURE_CITATION_MISSING"
    return None


def _revalidate_completion(runtime, rid, observation):
    """완료 캐시도 현재 질문·근거·필수 인용을 확인하고 보정은 새 관측으로 남긴다."""
    from .control_plane import ControlError
    from .research_report import input_fingerprint, report_record, validate_draft
    saved = observation["result"].get("report", {})
    draft_data = saved.get("draft") or observation["result"].get("draft")
    try:
        if not draft_data:
            raise ControlError("REPORT_DRAFT_MISSING")
        current = report_record(runtime.state, rid, validate=True)
        if current is not None and (current.get("status") != "READY" or current.get("draft") != draft_data):
            raise ControlError("REPORT_CONTENT_CHANGED")
        if saved.get("input_fingerprint") and saved["input_fingerprint"] != input_fingerprint(runtime.state, rid):
            raise ControlError("REPORT_STALE")
        draft = ReportDraft.model_validate(draft_data)
        problem = _required_literature_problem(runtime, rid, draft)
        if problem:
            raise ControlError(problem)
        validate_draft(runtime.state, rid, draft)
        return observation
    except (ModelProviderError, ValueError) as exc:
        reason = getattr(exc, "code", "REPORT_VALIDATION_FAILED")
    corrected = {**observation, "result": {**observation["result"], "status": "NEEDS_REVIEW",
        "previous_status": "READY", "reason": reason, "draft": draft_data},
        "fingerprint": _fingerprint({"previous_fingerprint": observation["fingerprint"], "reason": reason,
            "state_version": runtime.state.state_version(rid),
            "search_required": bool((getattr(runtime, "science_settings", {}) or {}).get("search_required"))})}
    key = f"science:revalidation:{observation['iteration']}:{corrected['fingerprint']}"
    runtime.state.finish_runtime_step(rid, key, corrected)
    runtime.state.runtime_event(rid, "SCIENCE_COMPLETION_REVALIDATION_REQUIRED", {"iteration": observation["iteration"], "reason": reason})
    return corrected


def _compact_observations(observations):
    compact = []
    for item in observations[-6:]:
        result = dict(item["result"])
        for name in ("draft", "report"):
            if name in result:
                draft = result[name].get("draft", result[name])
                result[name] = {key: draft[key] for key in ("report_type", "summary", "variables", "procedure", "limitations") if key in draft}
        if item['action'] in {'CALCULATE', 'CALCULATE_DATASET'}:
            result = {key: value for key,value in result.items() if key not in {'results','dataset_sha256'}}
        if item['action'] == 'FETCH_DATA':
            result = {key:result[key] for key in ('status','reason','dataset_id','preprocessing') if key in result}
        if item['action'] == 'SEARCH' and 'acquisition' in result:
            result.pop('acquisition')
        compact.append({"iteration": item["iteration"], "action": item["action"],
                        "rationale": item["rationale"][:600], "result": result})
    return compact


def _finish(runtime, rid, reason, *, limitation=None):
    if limitation:
        runtime.state.runtime_event(rid, "SCIENCE_LIMITATION", {"reason": limitation})
    runtime.state.stop_research(rid, reason)
    runtime._save_cursor(rid, "STOPPED")
    observations = _observations(runtime.state, rid)
    return {"research_id": rid, "stop_reason": reason.value,
            "science_decisions": len(observations), "observations": observations,
            "state_version": runtime.state.state_version(rid)}


async def _dataset(runtime, rid, csv_source):
    if csv_source is None:
        return None
    saved = runtime.state.runtime_step(rid, "science:dataset")
    if saved and saved["status"] == "COMPLETED":
        return saved["output"]
    intake, task = runtime._role_contract(
        rid, "experiment_coordinator", "과학 탐구 자료를 가져오고 실제 열과 품질을 확인합니다.",
        "DatasetIntake", allowed_tools=["data.import", "data.profile"], max_tool_calls=2,
        runtime_key="science:intake")
    registry = runtime._registry(intake.contract_id)
    _, imported = runtime._dispatch(registry, intake, task, "data.import",
                                    {"source_path": str(csv_source), "research_id": rid})
    runtime.faults.at("AFTER_INTAKE_IMPORT")
    did = imported.result["dataset_id"]
    _, profile = runtime._dispatch(registry, intake, task, "data.profile", {"dataset_id": did})
    runtime.faults.at("AFTER_INTAKE_PROFILE")
    artifact = runtime.state.file_artifact(profile.result["artifact_id"], rid)
    document = from_json(runtime.state.workspace.path(rid, artifact["relative_path"]).read_text(
        encoding="utf-8", errors="strict"))
    value = {"dataset_id": did, "profile_artifact_id": profile.result["artifact_id"], "profile": document}
    runtime.state.finish_runtime_step(rid, "science:dataset", value, intake.contract_id)
    runtime._complete_task(intake.contract_id)
    runtime._save_cursor(rid, "SCIENCE_DATASET_READY")
    return value


def _hypothesis(runtime, rid, question):
    saved = runtime.state.runtime_step(rid, "science:hypothesis")
    if saved:
        return saved["output"]["hypothesis_id"]
    existing = runtime.state._db.execute("SELECT hypothesis_id,status FROM hypotheses WHERE research_id=? AND statement=? ORDER BY rowid LIMIT 1", (rid, question)).fetchone()
    if existing:
        identity = existing["hypothesis_id"]
        if existing["status"] == "SHORTLISTED":
            runtime.state.set_hypothesis_status(rid, identity, "ACTIVE", decided_by="manager", rationale="저장된 탐구 평가를 계속합니다.")
        runtime.state.finish_runtime_step(rid, "science:hypothesis", {"hypothesis_id": identity})
        return identity
    score = HypothesisScore(plausibility=.5, testability=1, data_availability=1,
                            information_value=.5, cost=.2)
    shortlist = HypothesisShortlist(hypotheses=[HypothesisProposal(statement=question,
        rationale="원래 탐구 질문을 실제 자료로 평가합니다.", score=score)], rationale="탐구 한 건에 집중합니다.")
    identity = runtime.state.shortlist_hypotheses(rid, shortlist, created_by="manager", max_shortlist=1)[0]
    runtime.state.set_hypothesis_status(rid, identity, "ACTIVE", decided_by="manager",
                                        rationale="실제 자료에 대한 탐구 평가를 시작합니다.")
    runtime.state.finish_runtime_step(rid, "science:hypothesis", {"hypothesis_id": identity})
    return identity


async def _analyze(runtime, rid, iteration, decision, decision_contract, dataset, goal):
    if dataset is None:
        return {"status": "NEED_INPUT", "reason": "ANALYSIS_DATA_REQUIRED"}
    hid = _hypothesis(runtime, rid, decision.research_question or goal)
    refs = [ContextRef(type=RefType.dataset, id=dataset["dataset_id"]),
            ContextRef(type=RefType.artifact, id=dataset["profile_artifact_id"]),
            ContextRef(type=RefType.hypothesis, id=hid)]
    prior = _observations(runtime.state, rid)
    requested_method = None
    reviews = [item["result"]["review"] for item in prior if "review" in item["result"]]
    if reviews and reviews[-1].get("verdict") == "FOLLOW_UP_REQUIRED":
        requested = reviews[-1].get("requested_followups", [])
        if requested:
            requested_method = requested[0]["method"]
    worker, task = runtime._role_contract(rid, "analysis_planner_worker",
        decision.rationale + "\n실제 자료 프로파일: " + to_json(dataset["profile"]) +
        "\n정확한 이전 검토와 요청: " + to_json(reviews[-1:] ), "AnalysisPlan",
        inputs=refs, preserve=refs,
        allowed_tools=["stats.run", "visualization.render", "evidence.record"] +
                      (["analysis.skill"] if runtime.verified_analysis_skills_enabled else []),
        max_tool_calls=3, runtime_key=f"auto:worker:science:{iteration}")
    plan_key = f"worker_plan:{worker.contract_id}"
    if decision.analysis_plan is not None:
        runtime.state.finish_runtime_step(rid, plan_key, decision.analysis_plan.model_dump(mode="json"), worker.contract_id)
    cursor = {"stage": "WORKER_READY", "active_task_ids": [runtime.state.contract(decision_contract.contract_id)[1], task],
              "active_contract_ids": [decision_contract.contract_id, worker.contract_id],
              "dataset_id": dataset["dataset_id"], "profile_artifact_id": dataset["profile_artifact_id"]}
    runtime.state.checkpoint(rid, "science worker ready", cursor)
    runtime._save_cursor(rid, f"SCIENCE_WORKER_{iteration}_READY")
    from .agent_runtime import RuntimeFailure
    try:
        if runtime.verification_repair_enabled and runtime.state.runtime_step(rid, f"repair_failure:{worker.contract_id}"):
            result = await runtime._repair_failed_worker(worker, task, cursor)
        else:
            result = await runtime._run_worker(worker, task, cursor, recovery_key=plan_key,
                                               expected_method=requested_method, faults=runtime.faults)
    except RuntimeFailure as exc:
        if not runtime.verification_repair_enabled or exc.code != "VERIFICATION_FAILED":
            raise
        result = await runtime._repair_failed_worker(worker, task, cursor)
    eid, evidence = runtime._science_ids(result["mutation_id"])
    runtime._link_experiment(rid, hid, dataset["dataset_id"], eid, evidence)
    return {"status": "VERIFIED", "experiment_id": eid, "evidence_id": evidence,
            "hypothesis_id": hid, "result": result["result"]}


async def _execute(runtime, rid, iteration, decision, contract, dataset, goal, observations):
    if decision.action in {'CALCULATE', 'CALCULATE_DATASET', 'FETCH_DATA', 'FETCH_SOURCE'}:
        from .science_tools import execute_science_plan
        return await execute_science_plan(runtime, rid, iteration, decision, contract)
    if decision.action == "SEARCH":
        acquire = getattr(runtime, "evidence_acquisition", None)
        if acquire is None:
            return {"status": "NEED_INPUT", "reason": "SEARCH_NOT_CONFIGURED"}
        before = runtime.state._db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone()[0]
        try:
            result = await _await(acquire(queries=decision.search_queries))
        except ModelProviderError as exc:
            return {"status": "NO_EVIDENCE", "reason": exc.code, "new_evidence_count": 0,
                    "queries": decision.search_queries}
        after = runtime.state._db.execute("SELECT COUNT(*) FROM evidence WHERE research_id=? AND status='VERIFIED'", (rid,)).fetchone()[0]
        return {"status": "COMPLETED" if result else "NO_EVIDENCE", "new_evidence_count": max(0, after-before),
                "queries": decision.search_queries, "acquisition": result if isinstance(result, dict) else bool(result)}
    if decision.action == "ANALYZE" or (decision.action == "DELEGATE" and decision.delegate_role == "analysis_planner_worker"):
        return await _analyze(runtime, rid, iteration, decision, contract, dataset, goal)
    if decision.action == "REFINE_DESIGN":
        refine = getattr(runtime, "science_design_refiner", None)
        result = await _await(refine(decision.design_updates)) if refine else decision.design_updates
        return {"status": "PROPOSED", "design_updates": result}
    if decision.action == "DELEGATE":
        analyses = [item["result"] for item in observations if item["result"].get("status") == "VERIFIED" and item['result'].get('experiment_id')]
        if analyses:
            latest = analyses[-1]
            review = await runtime._critic(rid, latest["experiment_id"], latest["evidence_id"], latest["hypothesis_id"],
                ContextRef(type=RefType.hypothesis, id=latest["hypothesis_id"]), f"science:{iteration}")
            return {"status": "REVIEWED", "review": review.model_dump(mode="json")}
        objectives = decision.delegate_objectives or [decision.rationale]
        contracts = [runtime._role_contract(rid, "verification_coordinator",
            "과학 탐구의 자료·계산·방법 비교·반론·인과 해석·한계와 필요한 측정의 실행 가능성을 독립 검토합니다.\n" + objective + "\n" +
            to_json({"goal": goal, "observations": _compact_observations(observations),
                "verified_numbers": __import__('probe.research_report', fromlist=['verified_numbers']).verified_numbers(runtime.state,rid)[-200:],
                "public_datasets": __import__('probe.climate_data', fromlist=['climate_datasets']).climate_datasets(runtime.state,rid)}),
            "ScienceDesignReview", runtime_key=f"science:review:{iteration}:{index}")[0]
            for index, objective in enumerate(objectives)]
        reviews = await asyncio.gather(*(runtime._model_once(reviewer, ScienceDesignReview,
            f"science:review_output:{iteration}:{index}") for index, reviewer in enumerate(contracts)), return_exceptions=True)
        failures = [review for review in reviews if isinstance(review, BaseException)]
        if failures:
            raise failures[0]
        for reviewer in contracts:
            runtime._complete_task(reviewer.contract_id)
        verdict = "ACCEPT" if all(review.verdict == "ACCEPT" for review in reviews) else (
            "NEED_INPUT" if any(review.verdict == "NEED_INPUT" for review in reviews) else "REVISE")
        return {"status": "REVIEWED", "review": {"verdict": verdict,
                    "rationale": "독립 검토 결과를 함께 보존합니다.",
                    "issues": list(dict.fromkeys(issue for review in reviews for issue in review.issues))},
                "independent_reviews": [review.model_dump(mode="json") for review in reviews]}
    if decision.action == "COMPLETE":
        if not decision.stop:
            return {"status": "CONTINUE", "draft": decision.report_draft.model_dump(mode="json")}
        from .research_report import requested_report_sections, missing_report_sections
        missing=missing_report_sections(decision.report_draft,requested_report_sections(goal))
        if missing:return {'status':'NEEDS_REVIEW','reason':'REPORT_REQUESTED_SECTION_MISSING','missing':missing,
                           'repair':{'instruction':'누락 항목 이름을 본문 소제목으로 쓰고 실제 자료·판단·한계를 작성하세요.'}}
        literature_problem = _required_literature_problem(runtime, rid, decision.report_draft)
        if literature_problem:
            return {"status": "NEEDS_REVIEW", "reason": literature_problem,
                    "draft": decision.report_draft.model_dump(mode="json")}
        reviews = [item["result"]["review"] for item in observations if "review" in item["result"]]
        if reviews:
            latest = reviews[-1]
            if latest.get("verdict") not in {"ACCEPT", "ACCEPT_WITH_LIMITATION"} or any(
                    isinstance(issue, dict) and issue.get("severity") == "HIGH" for issue in latest.get("issues", [])):
                return {"status": "NEEDS_REVIEW", "reason": "UNRESOLVED_CRITIC", "review": latest}
        writer = getattr(runtime, "science_report_writer", None)
        from .research_report import validate_draft
        try:
            record = await _await(writer(decision.report_draft, decision, request_key=f"science:{iteration}")) if writer else {
                "status": "READY", "draft": validate_draft(runtime.state, rid, decision.report_draft)}
        except (ModelProviderError, ValueError) as exc:
            return {"status": "PARTIAL", "reason": getattr(exc, "code", "REPORT_VALIDATION_FAILED"),
                    "repair": getattr(exc,'repair',None), "draft": decision.report_draft.model_dump(mode="json")}
        return {"status": record.get("status", "PARTIAL"), "report": record}
    return {"status": "NEED_INPUT", "reason": decision.rationale}



async def _initial_search(runtime, rid):
    """모델 판단 전에 기존 검색 경로를 한 번 사용하고 재개 시 결과를 재사용한다."""
    saved = runtime.state.runtime_step(rid, "science:initial_search")
    if saved and saved["status"] == "COMPLETED":
        return saved["output"]
    acquire = getattr(runtime, "evidence_acquisition", None)
    settings = getattr(runtime, "science_settings", {}) or {}
    if settings.get("search_policy") == "DISABLED":
        result = {"status": "SKIPPED", "reason": "SEARCH_DISABLED"}
    elif acquire is None:
        result = {"status": "SKIPPED", "reason": "SEARCH_NOT_CONFIGURED"}
    else:
        if getattr(runtime, "control_boundary", None):
            runtime.control_boundary()
        try:
            # 검색어·전송 동의·비용·횟수 검사는 기존 수집기에 맡긴다.
            await _await(acquire(queries=None))
            from .research_report import report_inputs
            evidence = report_inputs(runtime.state, rid)["evidence"]
            result = {"status": "COMPLETED" if evidence else "NO_EVIDENCE", "evidence_count": len(evidence)}
            if getattr(runtime, "input_limitation", None):
                result["reason"] = runtime.input_limitation
        except ModelProviderError as exc:
            # 정산·인증·사용량 오류를 자료 없음으로 숨겨 추가 유료 판단을 보내지 않는다.
            if not exc.code.startswith("SEARCH_") and exc.code not in {"WEB_SEARCH_UNSUPPORTED", "CONTEXT_LIMIT_BLOCKED"}:
                runtime.state.runtime_event(rid, "SCIENCE_INITIAL_SEARCH_FAILED", {"status": "ERROR", "reason": exc.code})
                raise
            result = {"status": "NO_EVIDENCE", "reason": exc.code}
    runtime.state.finish_runtime_step(rid, "science:initial_search", result)
    runtime.state.runtime_event(rid, "SCIENCE_INITIAL_SEARCH_FINISHED", result)
    return result


async def run_science_loop(runtime, rid: str, csv_source: str | Path | None, goal: str) -> dict:
    started = runtime.state.runtime_step(rid, "science:started")
    if started is None:
        runtime.state.finish_runtime_step(rid, "science:started", {"started_at": time.time()})
        started = runtime.state.runtime_step(rid, "science:started")
    # 최초 연구 시작 기록은 유지하고 재개 실행의 시간 한도를 별도로 계산한다.
    started_at = max(started["output"]["started_at"], getattr(runtime, "execution_started_at", 0))
    dataset = await _dataset(runtime, rid, csv_source)
    _, _, max_runtime, _ = _current_limits(runtime)
    remaining = max_runtime - (time.time() - started_at)
    if remaining <= 0:
        return _finish(runtime, rid, StopReason.ACTION_LIMIT_REACHED, limitation="TIME_LIMIT")
    try:
        initial_search = await asyncio.wait_for(_initial_search(runtime, rid), timeout=remaining)
    except asyncio.TimeoutError:
        return _finish(runtime, rid, StopReason.ACTION_LIMIT_REACHED, limitation="TIME_LIMIT")
    no_progress = 0
    for iteration in range(20):
        if getattr(runtime, "control_boundary", None):
            runtime.control_boundary()
        settings, max_decisions, max_runtime, no_progress_limit = _current_limits(runtime)
        if iteration >= max_decisions:
            return _finish(runtime, rid, StopReason.ACTION_LIMIT_REACHED, limitation="DECISION_LIMIT")
        observations = _observations(runtime.state, rid)
        remaining = max_runtime - (time.time() - started_at)
        if remaining <= 0:
            return _finish(runtime, rid, StopReason.ACTION_LIMIT_REACHED, limitation="TIME_LIMIT")
        saved = runtime.state.runtime_step(rid, f"science:observation:{iteration}")
        if saved and saved["status"] == "COMPLETED":
            observation = next(item for item in observations if item["iteration"] == iteration)
        else:
            from .research_report import report_inputs, verified_numbers
            inputs = report_inputs(runtime.state, rid)
            from .research_report import requested_report_sections
            from .ai_web_search import SharedSearchSlots
            from .control_plane import ControlStore
            slots=SharedSearchSlots(ControlStore(runtime.state._db),rid,settings.get('snapshot',{}))
            context = {"original_question": goal, "current_question": inputs["question"], "dataset": dataset,
                       "requested_outputs":requested_report_sections(goal),
                       "audience": settings.get("audience", "student"),
                       "science_field": settings.get("science_field", "general"),
                       "search_required": bool(settings.get("search_required")),
                       "remaining_search_requests":slots.remaining(),
                       "remaining_source_requests":slots.remaining(kind='public_original'),
                       "initial_search": initial_search,
                       "progress_feedback": {"consecutive_unproductive_actions": no_progress,
                           "stop_after": no_progress_limit,
                           "instruction": "성과 없는 반복이 2회 이상이면 같은 요청 대신 검색 표현·자료 출처·분석 방법을 바꾸세요. 확보한 자료로 답할 수 있으면 COMPLETE로 보고서를 작성하고 미확인 내용과 한계를 명시하세요. 부족한 근거를 검증 완료로 표시하거나 수치를 만들지 마세요."},
                       "observations": _compact_observations(observations), "remaining_decisions": max_decisions-iteration,
                       "report_evidence": [{key: item[key] for key in ("evidence_id", "claim", "evidence_text", "source_scope", "title", "url", "text_field", "evidence_location") if key in item}
                                           for item in inputs["evidence"][-8:]],
                       "figure_refs": [item["artifact_id"] for item in inputs["figures"][:8]],
                       "verified_numbers": verified_numbers(runtime.state, rid)[-200:],
                       "public_datasets": inputs.get('public_datasets',[]),
                       "data_tools": {'CALCULATE_DATASET':'dataset_calculation_plan: 실제 dataset_id, name, operation OLS/CORRELATION/SUMMARY, target, predictors, target_unit, predictor_units. transform none/difference/detrend. OLS는 SVD·QR 독립 검산. 요청한 회귀식마다 별도 계획을 실행한다.',
                                      'FETCH_DATA':'data_plan.sources로 아래 공식 자료 종류를 선택한다. 실제 원문을 다운로드하고 연도·기준기간을 맞춘 CSV와 출처 해시를 반환한다. 수치 자료가 필요하면 제목·본문 검색으로 대체하지 않는다.'},
                       "data_catalog": __import__('probe.climate_data',fromlist=['CATALOG']).CATALOG,
                       "source_catalog": __import__('probe.science_sources',fromlist=['SOURCE_CATALOG']).SOURCE_CATALOG,
                       "supported_analysis_methods": ["pearson_correlation", "spearman_correlation"] +
                           (["ridge_holdout", "ridge_rolling_origin"] if runtime.verified_analysis_skills_enabled else [])}
            contract, _ = runtime._role_contract(rid, "manager", "과학 탐구의 다음 행동을 선택합니다.\n" + to_json(context),
                "ScienceDecision", runtime_key=f"science:decision:{iteration}")
            try:
                decision = await asyncio.wait_for(runtime._model_once(contract, ScienceDecision, f"science:decision_output:{iteration}"), timeout=remaining)
            except asyncio.TimeoutError:
                return _finish(runtime, rid, StopReason.ACTION_LIMIT_REACHED, limitation="TIME_LIMIT")
            except Exception as exc:
                if getattr(exc,'code',None)!='STRUCTURED_OUTPUT_INVALID':raise
                repair={'instruction':'응답 스키마를 지키세요. SEARCH는 검색어 최대 세 개입니다. 행동에 맞는 계획 필드 하나를 사용하세요.'}
                if runtime.state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_audit'").fetchone():
                    trace=runtime.state._db.execute("SELECT payload FROM control_audit WHERE research_id=? AND kind='NORMALIZED_RESPONSE_SETTLED' ORDER BY seq DESC LIMIT 1",(rid,)).fetchone()
                    if trace:repair['errors']=from_json(trace[0]).get('structured_validation_errors',[])
                result={'status':'NEEDS_REVIEW','reason':'STRUCTURED_OUTPUT_INVALID','repair':repair}
                observation={'iteration':iteration,'action':'VALIDATE_OUTPUT','rationale':'실패한 응답의 형식만 수정합니다. 연구 목표와 완료한 계산은 보존합니다.','result':result,'fingerprint':_fingerprint(result)}
                runtime.state.fail_runtime_step(rid,f'science:decision_output:{iteration}',result)
                runtime.state.finish_runtime_step(rid,f'science:observation:{iteration}',observation,contract.contract_id)
                runtime.state.runtime_event(rid,'SCIENCE_SCHEMA_REPAIR_REQUIRED',{'iteration':iteration,**repair})
                runtime._save_cursor(rid,f'SCIENCE_SCHEMA_{iteration}_FAILED')
                no_progress+=1
                if no_progress>=no_progress_limit:return _finish(runtime,rid,StopReason.UNRESOLVED_VERIFICATION,limitation='SCHEMA_REPAIR_LIMIT')
                continue
            runtime._complete_task(contract.contract_id)
            if not runtime.state._one("SELECT research_question FROM research_runs WHERE research_id=?", (rid,))[0]:
                runtime.state.set_research_question(rid, decision.research_question or goal)
            try:
                if getattr(runtime, "control_boundary", None):
                    runtime.control_boundary()
                settings, max_decisions, max_runtime, no_progress_limit = _current_limits(runtime)
                remaining = max_runtime - (time.time() - started_at)
                if remaining <= 0:
                    return _finish(runtime, rid, StopReason.ACTION_LIMIT_REACHED, limitation="TIME_LIMIT")
                result = await asyncio.wait_for(_execute(runtime, rid, iteration, decision, contract, dataset, goal, observations), timeout=remaining)
            except asyncio.TimeoutError:
                return _finish(runtime, rid, StopReason.ACTION_LIMIT_REACHED, limitation="TIME_LIMIT")
            observation = {"iteration": iteration, "action": decision.action, "rationale": decision.rationale,
                           "goal_evaluation": decision.goal_evaluation, "result": result,
                           "fingerprint": _fingerprint({"action": decision.action, "queries": decision.search_queries,
                               "plan": decision.analysis_plan, "design": decision.design_updates,
                               "calculation_plan": decision.calculation_plan,
                               "dataset_calculation_plan": decision.dataset_calculation_plan,
                               "data_plan": decision.data_plan,
                               "document_plan":decision.document_plan,
                               "delegate_role": decision.delegate_role, "delegate_objectives": decision.delegate_objectives,
                               "draft": decision.report_draft,
                               "result": {key: result[key] for key in ("status", "new_evidence_count", "review") if key in result}})}
            runtime.state.finish_runtime_step(rid, f"science:observation:{iteration}", observation, contract.contract_id)
            action_type = {"SEARCH": ResearchAction.SEARCH_LITERATURE, "FETCH_DATA":ResearchAction.SEARCH_LITERATURE,"FETCH_SOURCE":ResearchAction.SEARCH_LITERATURE,"CALCULATE_DATASET":ResearchAction.RUN_EXPERIMENT,"CALCULATE": ResearchAction.RUN_EXPERIMENT, "ANALYZE": ResearchAction.RUN_EXPERIMENT,
                           "REFINE_DESIGN": ResearchAction.REPLAN, "DELEGATE": ResearchAction.CRITIQUE_RESULT,
                           "COMPLETE": ResearchAction.STOP if decision.stop else ResearchAction.REPLAN,
                           "NEED_INPUT": ResearchAction.STOP}[decision.action]
            runtime._action(rid, action_type, f"과학 판단 {iteration+1}: {decision.rationale}",
                            details={"science_action": decision.action, "status": result["status"]},
                            logical_key=f"science:action:{iteration}")
            runtime.state.runtime_event(rid, "SCIENCE_ACTION_OBSERVED", {"iteration": iteration, "action": decision.action, "status": result["status"]})
            runtime._save_cursor(rid, f"SCIENCE_STEP_{iteration}_READY")
            runtime.faults.at("AFTER_SCIENCE_OBSERVATION")
        if observation["action"] == "COMPLETE" and observation["result"]["status"] == "READY":
            observation = _revalidate_completion(runtime, rid, observation)
        result = observation["result"]
        if observation["action"] == "COMPLETE" and result["status"] == "READY":
            return _finish(runtime, rid, StopReason.SCIENCE_INQUIRY_COMPLETED)
        if observation["action"] == "NEED_INPUT" or result["status"] == "NEED_INPUT":
            return _finish(runtime, rid, StopReason.INSUFFICIENT_DATA, limitation=result.get("reason"))
        previous = [item for item in observations if item["iteration"] < iteration]
        repeated = any(item["fingerprint"] == observation["fingerprint"] for item in previous)
        new_evidence = result.get("new_evidence_count", 0) > 0
        empty = (result["status"] in {"NO_EVIDENCE", "PARTIAL", "NEEDS_REVIEW"} and not new_evidence) or (observation["action"] == "SEARCH" and result.get("new_evidence_count") == 0)
        no_progress = no_progress+1 if (repeated and not new_evidence) or empty else 0
        _, _, _, no_progress_limit = _current_limits(runtime)
        if no_progress >= no_progress_limit:
            return _finish(runtime, rid, StopReason.UNRESOLVED_VERIFICATION, limitation="NO_PROGRESS")
    return _finish(runtime, rid, StopReason.ACTION_LIMIT_REACHED, limitation="DECISION_LIMIT")
