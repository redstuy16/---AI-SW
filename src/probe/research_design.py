"""선택 연구 설계의 입력·실행 조건을 기존 정본 경계에 연결한다."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from .schemas import StrictModel, utc_now
from .database import to_json
from .qualified_profiles import fingerprint, registry, uses_qualified_profile

SECTIONS = [
    ("무엇을 알아보나요?", [
        ("question_focus", "핵심 질문"), ("purpose", "연구 목적"), ("background", "연구를 하게 된 이유"),
        ("hypothesis_reason", "가설을 생각한 이유"), ("hypothesis_counter", "가설을 다시 생각하게 될 결과"),
        ("scope", "연구에서 꼭 지킬 범위")]),
    ("무엇을 비교하고 측정하나요?", []),
    ("어떤 대상과 자료를 사용하나요?", [
        ("subject", "연구 대상"), ("population", "결과를 설명할 대상·범위"), ("independent_unit", "하나의 측정 대상으로 셀 기준"),
        ("spatial_scope", "장소·공간 범위"), ("period", "기간"), ("repetition", "같은 대상의 반복 측정"),
        ("acquisition", "자료를 얻는 방법"), ("inclusion", "포함할 자료의 조건"), ("exclusion", "제외할 자료의 조건과 이유"),
        ("prior_exposure", "이미 살펴본 자료·결과")]),
    ("어떻게 진행하고 측정하나요?", [
        ("instrument", "측정 도구·방법"), ("frequency", "측정 시점·간격"), ("record_items", "기록할 항목"),
        ("uncertainty", "측정의 정밀도·오차 정보"), ("calibration", "도구 상태 확인·보정"),
        ("assignment", "실험 순서·배정 방법"), ("pairing", "짝지음·묶음 기준"), ("stop_rule", "진행을 멈출 조건")]),
    ("어떻게 분석하고 판단하나요?", [
        ("target", "확인하고 싶은 관계·차이"), ("method", "사용하고 싶은 분석 방법"), ("primary_outcome", "중요하게 볼 결과"),
        ("missing", "자료가 빠졌을 때의 처리"), ("outlier", "유난히 크거나 작은 값의 처리"),
        ("charts", "보고 싶은 그림·표"), ("interpretation_limits", "결론에서 주의할 점")]),
    ("참고 자료와 주의사항", [
        ("references", "참고할 자료"), ("source_questions", "자료 설명에서 확인할 내용"),
        ("assignment_constraints", "따라야 할 과제 조건"), ("forbidden", "사용하지 않을 자료·방법"),
        ("safety", "안전·개인정보 주의사항"), ("notes", "추가 요청")]),
]
LABELS = dict(item for _, items in SECTIONS for item in items)
APPROACHES = {"AUTO": "자료를 살펴보며 결정하기", "EXISTING": "기존 자료 분석하기",
              "PHYSICAL": "직접 실험하거나 관측하기", "LITERATURE": "문헌을 비교해 알아보기",
              "SIMULATION": "시뮬레이션 결과 살펴보기"}
ROLE_LABELS = {"item": "비교·설명에 사용할 항목", "outcome": "살펴볼 결과 항목", "manipulated": "내가 바꿀 조건(조작 변인·독립 변인)",
               "fixed": "같게 유지할 조건(통제 변인)", "comparison": "비교의 기준이 되는 조건·집단",
               "other_factor": "결과에 영향을 줄 수 있는 다른 요인", "covariate": "분석에서 함께 고려하고 싶은 항목",
               "association": "관련성을 볼 두 항목", "time": "시간 항목", "literature": "비교할 주장·주제",
               "simulation_input": "바꿀 입력 조건", "simulation_output": "확인할 출력"}
CARD_FIELDS = {"definition": "무엇을 어떻게 측정하나요?", "quantity_kind": "값의 종류", "unit": "단위",
               "dimensionless_reason": "단위가 없는 이유", "measurement": "측정·계산 방법", "levels": "비교할 값·조건",
               "analysis_role": "중요하게 볼 순서", "stage": "적용할 진행 단계", "fixed_value": "유지할 값·상태",
               "maintain": "같게 유지하는 방법", "check": "확인하는 방법", "tolerance": "허용할 차이",
               "difficulty": "유지하기 어려운 점", "baseline": "기준 기간", "storage_scale": "저장 배율"}


class Intent(StrictModel):
    value: str = Field(default="", max_length=4000)
    state: Literal["UNKNOWN", "USER_DECLARED_NONE", "NOT_APPLICABLE", "SPECIFIED"] = "SPECIFIED"
    origin: Literal["user", "accepted_suggestion"] = "user"
    reason: str = Field(default="", max_length=1000)
    source_span: list[StrictInt] | None = Field(default=None, min_length=2, max_length=2)
    source_revision: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def safe(self):
        object.__setattr__(self, "value", self.value.strip())
        if self.state == "NOT_APPLICABLE" and not self.reason.strip():
            raise ValueError("해당 없음의 이유를 적어 주세요.")
        if self.origin == "accepted_suggestion" and (self.source_span is None or self.source_revision is None):
            raise ValueError("제안의 원문 위치와 초안 버전이 필요합니다.")
        if self.source_span and not 0 <= self.source_span[0] < self.source_span[1]:
            raise ValueError("원문 위치가 올바르지 않습니다.")
        self.value.encode("utf-8", errors="strict")
        return self


class SourceBinding(StrictModel):
    attachment_id: str | None = Field(default=None, max_length=100)
    dataset_id: str | None = Field(default=None, max_length=100)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    column: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def one_source(self):
        if bool(self.attachment_id) == bool(self.dataset_id):
            raise ValueError("하나의 자료에 연결해 주세요.")
        return self


class Variable(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    name: str = Field(default="", max_length=200)
    role: Literal[tuple(ROLE_LABELS)] = "item"
    active: bool = True
    inactive_reason: str = Field(default="", max_length=500)
    data_form: Literal["unknown", "number", "category", "ordinal", "datetime", "text"] = "unknown"
    details: dict[str, Intent] = Field(default_factory=dict)
    binding: SourceBinding | None = None

    @model_validator(mode="after")
    def fields(self):
        if set(self.details) - set(CARD_FIELDS):
            raise ValueError("지원하지 않는 항목입니다.")
        object.__setattr__(self, "details", clean_entries(self.details))
        return self


class DesignRow(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    text: str = Field(default="", max_length=4000)
    start: StrictInt | None = Field(default=None, ge=1, le=9999)
    end: StrictInt | None = Field(default=None, ge=1, le=9999)
    variable_id: str | None = Field(default=None, max_length=100)


def clean_entries(entries):
    return {k: v for k, v in entries.items() if v.value or v.state != "SPECIFIED"}


class DetailedDesign(StrictModel):
    schema_version: Literal[1] = 1
    editor_open: bool = False
    approach: Literal[tuple(APPROACHES)] = "AUTO"
    intervention: Literal["unknown", "change", "observe"] = "unknown"
    fields: dict[str, Intent] = Field(default_factory=dict)
    variables: list[Variable] = Field(default_factory=list, max_length=40)
    hypotheses: list[DesignRow] = Field(default_factory=list, max_length=20)
    comparisons: list[DesignRow] = Field(default_factory=list, max_length=20)
    procedure: list[DesignRow] = Field(default_factory=list, max_length=40)
    sample_count: StrictInt | None = Field(default=None, ge=1, le=10000000)

    @model_validator(mode="after")
    def normalize(self):
        if set(self.fields) - set(LABELS):
            raise ValueError("지원하지 않는 연구 조건입니다.")
        object.__setattr__(self, "fields", clean_entries(self.fields))
        object.__setattr__(self, "variables", [v for v in self.variables if v.name.strip()])
        for key in ("hypotheses", "comparisons", "procedure"):
            object.__setattr__(self, key, [r for r in getattr(self, key) if r.text.strip() or r.start is not None or r.end is not None])
        rows = self.variables + self.hypotheses + self.comparisons + self.procedure
        ids = [r.id for r in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("입력 항목의 식별자가 중복되었습니다.")
        active = {v.id for v in self.variables}
        if any(r.variable_id and r.variable_id not in active for r in self.comparisons + self.procedure):
            raise ValueError("연결한 항목이 없습니다.")
        if len(to_json(self).encode("utf-8", errors="strict")) > 100000:
            raise ValueError("상세 조건은 100KB까지 저장할 수 있습니다.")
        return self


def normalize_design(raw):
    value = DetailedDesign.model_validate(raw or {}).model_dump(mode="json", exclude_none=True)
    if re.search(r"(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{35}|ghp_[A-Za-z0-9]{36})", to_json(value)):
        raise ValueError("연구 조건에 비밀키를 입력하지 마세요.")
    return value


def semantic_design(raw):
    design = normalize_design(raw)
    design.pop("editor_open", None)
    for key in ("hypotheses", "comparisons", "procedure"):
        if not design[key]:
            design.pop(key)
    if not design["variables"]:
        design.pop("variables")
    if not design["fields"]:
        design.pop("fields")
    # 표시 순서는 항목의 의미를 바꾸지 않는다. 진행 순서와 비교 순서는 보존한다.
    if "variables" in design:
        design["variables"] = sorted(design["variables"], key=lambda v: v["id"])
    return design


def meaningful_count(raw):
    d = normalize_design(raw)
    return (len(d["fields"]) + sum(v["active"] for v in d["variables"]) +
            sum(len(d[k]) for k in ("hypotheses", "comparisons", "procedure")) +
            int(d.get("sample_count") is not None) + int(d["approach"] != "AUTO") +
            int(d["intervention"] != "unknown"))


def _value(design, key):
    entry = design["fields"].get(key, {})
    return entry.get("value", "") if entry.get("state") == "SPECIFIED" else ""


def _issue(field, message, status="CLARIFICATION_REQUIRED"):
    return {"field": field, "message": message, "status": status}


def resolve_design(question, raw, *, profile=None, use_profile=True):
    choose = registry().choose if use_profile else lambda text: {"status": "GENERAL", "original_question": text}
    d = normalize_design(raw)
    issues, dispositions = [], []
    effective_question = question
    focus = _value(d, "question_focus")
    if focus:
        # 명시된 범위가 없는 본문은 핵심 질문으로 구체화할 수 있다.
        old = choose(question)
        new = choose(focus)
        if old["status"] == "SUPPORTED" and (new.get("periods") != old.get("periods") or new.get("transform") != old.get("transform")):
            issues.append(_issue("question_focus", "내용과 핵심 질문의 기간·값이 다릅니다. 사용할 질문을 확인해 주세요."))
        elif re.findall(r"(?:18|19|20)\d{2}", question) and re.findall(r"(?:18|19|20)\d{2}", question) != re.findall(r"(?:18|19|20)\d{2}", focus):
            issues.append(_issue("question_focus", "내용과 핵심 질문의 기간이 다릅니다. 사용할 기간을 확인해 주세요."))
        else:
            effective_question = focus
    candidate = profile or choose(effective_question)
    periods = [[r["start"], r["end"]] for r in d["comparisons"] if "start" in r and "end" in r]
    if len(periods) == 2 and not re.search(r"(?:18|19|20)\d{2}", effective_question):
        refined = effective_question + " " + "과 ".join(f"{a}~{b}년" for a, b in periods) + " 비교"
        proposed = choose(refined)
        if proposed["status"] == "SUPPORTED":
            effective_question, candidate = refined, proposed
    for row in d["comparisons"]:
        if ("start" in row) != ("end" in row) or ("start" in row and row["start"] > row["end"]):
            issues.append(_issue("comparisons:" + row["id"], "비교 기간의 시작과 끝을 확인해 주세요."))
    if periods and candidate.get("periods") and periods != candidate["periods"]:
        issues.append(_issue("comparisons", "내용과 상세 조건의 비교 기간이 다릅니다. 두 입력을 같은 범위로 맞춰 주세요."))
    period = _value(d, "period")
    if period and candidate.get("periods"):
        stated = [[int(a), int(b)] for a, b in re.findall(r"((?:18|19|20)\d{2})\s*[~∼–—-]\s*((?:18|19|20)\d{2})", period)]
        if not stated:
            years = [int(y) for y in re.findall(r"(?:18|19|20)\d{2}", period)]
            if len(years) == 4:
                stated = [years[:2], years[2:]]
            else:
                issues.append(_issue("period", "입력한 기간을 연간 자료의 두 구간으로 확인할 수 없습니다. 사용할 시작·끝 연도를 확인해 주세요."))
        if stated and stated != candidate["periods"]:
            issues.append(_issue("period", "내용과 상세 조건의 기간이 다릅니다. 사용할 기간을 확인해 주세요."))
    # 이름이 같아도 진행 단계가 다르면 통제 계획과 조작 계획을 함께 둘 수 있다.
    variables = [v for v in d["variables"] if v["active"]]
    for v in variables:
        if d["approach"] in {"EXISTING", "LITERATURE"} and v["role"] in {"manipulated", "simulation_input", "simulation_output"}:
            issues.append(_issue("variables:" + v["id"], "자료 관측과 직접 조건을 바꾸는 계획을 구분해 주세요. 항목을 보류하거나 역할을 수정할 수 있습니다."))
        if v["role"] == "manipulated":
            for fixed in variables:
                phase = lambda x: x["details"].get("stage", {}).get("value", "")
                if fixed["role"] == "fixed" and fixed["name"].strip().casefold() == v["name"].strip().casefold() and (not phase(v) or not phase(fixed) or phase(v) == phase(fixed)):
                    issues.append(_issue("variables:" + v["id"], "같은 단계에서 바꿀 조건과 같게 유지할 조건이 겹칩니다. 단계나 조건을 확인해 주세요."))
    method = _value(d, "method")
    if method:
        from typing import get_args
        from .agent_schemas import AnalysisPlan
        methods = set(get_args(AnalysisPlan.model_fields["method"].annotation)) | {"two_period_comparison"}
        if method not in methods or (candidate.get("method") and method != candidate["method"]):
            issues.append(_issue("method", "요청한 분석 방법은 이 절차에서 실행할 수 없습니다. 요청은 보존하며 방법을 직접 확인해야 합니다.", "METHOD_UNSUPPORTED"))
    if d["approach"] in {"PHYSICAL", "SIMULATION"}:
        issues.append(_issue("approach", "계획을 저장했습니다. 직접 측정·장비 조작·시뮬레이션 실행은 지원하지 않습니다. 실제 자료가 필요합니다.", "METHOD_UNSUPPORTED"))
    for key in ("target", "primary_outcome"):
        if re.search(r"인과\s*(?:관계|추론)|미래\s*예측|\b(?:causal|forecast|predict)\b", _value(d, key), re.IGNORECASE):
            issues.append(_issue(key, "입력한 인과·예측 목표는 현재 검증된 분석 범위에서 실행할 수 없습니다. 목표를 보존하며 해당 분석을 보류합니다.", "METHOD_UNSUPPORTED"))
    for key, entry in d["fields"].items():
        dispositions.append({"field": key, "label": LABELS[key], "intent": entry,
            "status": "PLANNING_CONSTRAINT" if key in {"question_focus", "method", "period", "interpretation_limits"} else "PENDING_EVIDENCE",
            "message": "입력한 연구 의도이며 실제 관측이나 검증 완료를 뜻하지 않습니다."})
    for v in d["variables"]:
        dispositions.append({"field": "variables:" + v["id"], "label": v["name"],
            "status": "PENDING_BINDING" if v["active"] else "INACTIVE", "role": v["role"],
            "message": "자료·열 연결과 실행 증거를 확인합니다." if v["active"] else v["inactive_reason"]})
    for key in ("hypotheses", "comparisons", "procedure"):
        for row in d[key]:
            dispositions.append({"field": key + ":" + row["id"], "label": row["text"] or key,
                "status": "PLANNING_CONSTRAINT" if key == "comparisons" and periods else "PENDING_EVIDENCE",
                "message": "가설은 정답이 아닙니다." if key == "hypotheses" else "계획과 실제 사용 여부를 구분합니다."})
    if "sample_count" in d:
        dispositions.append({"field": "sample_count", "label": "대상 수", "status": "PENDING_EVIDENCE",
                             "message": "계획한 대상 수이며 CSV 행 수나 반복 횟수와 다릅니다."})
    return {"schema_version": 1, "design": d, "hash": fingerprint(semantic_design(d)),
            "condition_count": meaningful_count(d), "effective_question": effective_question,
            "profile": candidate, "profile_enabled": use_profile, "issues": issues, "dispositions": dispositions, "paid_calls": 0}


def catalog():
    from typing import get_args
    from .agent_schemas import AnalysisPlan
    return {"sections": SECTIONS, "approaches": APPROACHES, "roles": ROLE_LABELS, "guide": guide_lines(),
            "card_fields": CARD_FIELDS, "methods": [m for m in get_args(AnalysisPlan.model_fields["method"].annotation) if not m.startswith("ridge_")] + ["two_period_comparison"],
            "help": {key: label + "를 알고 있다면 적어 주세요. 모르면 비워 두어도 됩니다." for key, label in LABELS.items()}}


def guide_lines():
    lines = ["맞춤 연구 설계", "첫 장 위에서 맞춤 연구 설계를 선택합니다. 모든 항목은 선택 사항이며 아는 내용만 적습니다.",
        "간단히 보기는 화면만 접습니다. 상세 조건은 계속 적용됩니다. 새 연구에는 이전 조건을 자동으로 적용하지 않습니다.",
        "상세 조건 지우기는 확인 후 상세 입력만 제거합니다. 주제·질문·첨부·실행 설정은 유지됩니다.",
        "항목 추가에서 이름과 역할을 적고 측정·단위·자료 연결을 펼칩니다. 원본 CSV와 열을 선택하여 현재 해시에 연결합니다.",
        "항목·가설·기간·진행 순서는 위로·아래로·삭제로 관리합니다. 접근 방식을 바꾸어 보류한 항목은 역할을 확인한 뒤 다시 적용합니다.",
        "모름·없음·해당 없음은 서로 다릅니다. 해당 없음에는 이유가 필요합니다. 대상 수와 반복 측정, 파일 행 수는 구분합니다.",
        "같게 유지할 조건에는 값·유지 방법·확인 방법·허용 차이·어려운 점을 기록할 수 있습니다. 입력만으로 통제 완료가 되지는 않습니다.",
        "내용에서 항목 정리하기는 원문에 직접 표시된 항목만 로컬로 제안합니다. 선택한 제안 적용 전에는 입력을 덮어쓰지 않습니다. 입력이 달라지면 다시 정리합니다.",
        "입력 내용 확인에서 기간·역할·방법의 충돌을 확인합니다. 자료의 단위·기준과 다르면 원본을 바꾸지 않으며 해당 계산을 보류합니다.",
        "실행 중 조건을 바꾸려면 먼저 일시정지합니다. 연구 조건에서 저장한 변경은 관련 결과를 무효화하며 현재 조건으로 다시 검증해야 합니다.",
        "실제로 지원하지 않는 측정·방법·선택 기준은 의도로 보존하고 실행 제한을 표시합니다. 직접 실험을 했다는 결과나 측정값을 만들어 넣지 않습니다."]
    for title, fields in SECTIONS:
        lines.append(title)
        lines.extend(label + ": 알고 있다면 적습니다. 모르면 비워 둡니다." for key, label in fields)
    lines.extend(label for label in CARD_FIELDS.values())
    return lines


def authorize_bindings(store, workspace, raw, draft_id, attachments, *, state=None, rid=None):
    from .control_plane import ControlError
    from .input_upload import Attachments
    design = normalize_design(raw)
    manager = Attachments(store, workspace)
    for v in design["variables"]:
        b = v.get("binding")
        if not b:
            continue
        if b.get("attachment_id"):
            if b["attachment_id"] not in attachments:
                raise ControlError("RESEARCH_DESIGN_SOURCE_DENIED")
            manager.get(b["attachment_id"], draft_id)
        elif not state or not rid:
            raise ControlError("RESEARCH_DESIGN_SOURCE_DENIED")
        else:
            state.dataset_record(b["dataset_id"], rid)


def organize(question, revision):
    proposals = []
    for match in re.finditer(r"(?m)^\s*([^:\n]{1,30})\s*[:：]\s*(.+)$", question):
        field = next((k for k, label in LABELS.items() if label == match[1].strip()), None)
        if field:
            proposals.append({"field": field, "value": match[2].strip(), "source_span": list(match.span(2)),
                "source_revision": revision, "generator": "LOCAL_LABELED_TEXT_V1",
                "reason": "원문에 항목 이름이 직접 적혀 있어 정리했습니다. 연구 의도만 제안합니다."})
    return {"proposals": proposals, "draft_revision": revision, "text_hash": fingerprint(question),
            "paid_calls": 0, "network_calls": 0}


def current_design(state, rid):
    row = state._db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'research_design:%' AND status='COMPLETED' ORDER BY rowid DESC LIMIT 1", (rid,)).fetchone()
    return json.loads(row[0]) if row else None


def _profile_enabled(state, rid, current=None):
    """저장된 이전 설정도 현재 실행 모드에 맞게 해석한다."""
    if state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_runs'").fetchone():
        run = state._db.execute("SELECT snapshot FROM control_runs WHERE research_id=?", (rid,)).fetchone()
        if run:
            return uses_qualified_profile(json.loads(run[0]))
    return (current or {}).get("profile_enabled", True)


def initialize_design(state, rid, snapshot):
    if not meaningful_count(snapshot.get("detailed_design")):
        return None
    current = current_design(state, rid)
    if current:
        return current
    resolved = resolve_design(snapshot["question"], snapshot["detailed_design"],
                              use_profile=uses_qualified_profile(snapshot))
    document = {**resolved, "revision": 1, "original_question": snapshot["question"],
                "author": "owner", "created_at": utc_now().isoformat()}
    state.finish_runtime_step(rid, "research_design:1", document)
    return document


def bind_contract(state, contract):
    current = current_design(state, contract.research_id)
    if current:
        state._db.execute("INSERT INTO runtime_steps(research_id,step_key,status,output_json,attempt,updated_at) VALUES (?,?,'COMPLETED',?,1,?)",
            (contract.research_id, "research_design_contract:" + contract.contract_id,
             to_json({"hash": current["hash"], "revision": current["revision"]}), utc_now().isoformat()))


def binding_issues(state, rid, design, dataset_id=None):
    from .control_plane import ControlError
    from .storage import sha256_file
    issues = []
    for v in design["variables"]:
        b = v.get("binding")
        if not v["active"] or not b:
            continue
        try:
            if b.get("dataset_id"):
                record = state.dataset_record(b["dataset_id"], rid)
                columns = json.loads(record["schema_json"] or "{}")
                valid = record["sha256"] == b["sha256"] and b["column"] in columns and (dataset_id is None or dataset_id == b["dataset_id"])
            else:
                row = state._db.execute("SELECT payload FROM control_configs WHERE kind='attachment' AND id=?", (b["attachment_id"],)).fetchone()
                record = json.loads(row[0]) if row else {}
                # 미실행 초안의 첨부는 생성 시 검사하며 실행된 연구는 references로 소유 범위를 확인한다.
                valid = rid in record.get("referenced_by", []) and record.get("sha256") == b["sha256"]
                if valid:
                    from .input_upload import Attachments
                    from .control_plane import ControlStore
                    manager = Attachments(ControlStore(state._db), state.workspace.root)
                    path = manager.path(record)
                    import csv
                    with path.open(encoding="utf-8-sig", newline="") as stream:
                        columns = next(csv.reader(stream))
                    valid = sha256_file(path) == b["sha256"] and b["column"] in columns
                    if valid and dataset_id:
                        dataset = state.dataset_record(dataset_id, rid)
                        valid = dataset["sha256"] == b["sha256"]
            if not valid:
                raise ValueError("binding")
        except Exception:
            issues.append(_issue("variables:" + v["id"], "연결한 자료의 수정본이나 열이 달라졌습니다. 자료를 다시 선택해 주세요."))
    return issues


def check_profile_design(state, rid, plan):
    from .control_plane import ControlError
    current = current_design(state, rid)
    if not current:
        return plan
    issues = list(resolve_design(plan["question"], current["design"], profile=registry().choose(plan["question"]))["issues"])
    for v in current["design"]["variables"]:
        if not v["active"]:
            continue
        for key, semantic in {"unit": "unit", "quantity_kind": "quantity_kind", "baseline": "baseline", "storage_scale": "storage_scale"}.items():
            intent = v["details"].get(key, {})
            requested = intent.get("value") if intent.get("state") == "SPECIFIED" else None
            actual = plan["semantics"].get(semantic)
            if v["role"] == "time" or (v.get("binding") or {}).get("column") == "year":
                if key in {"baseline", "storage_scale"}:
                    continue
                actual = "year" if key == "unit" else "time"
            matches = not requested or requested == str(actual) or requested == to_json(actual)
            if requested and key == "baseline":
                years = [int(y) for y in re.findall(r"(?:18|19|20)\d{2}", requested)]
                matches = len(years) == 2 and years == actual
            if requested and key == "unit":
                matches = {"°C": "degC", "℃": "degC", "년": "year"}.get(requested, requested) == actual
            if requested and key == "storage_scale":
                from decimal import Decimal, InvalidOperation
                try:
                    matches = Decimal(requested).is_finite() and Decimal(requested) == Decimal(actual)
                except (InvalidOperation, TypeError):
                    matches = False
            if not matches:
                issues.append(_issue("variables:" + v["id"], CARD_FIELDS[key] + "와 공식 자료 설명이 다릅니다. 원본 의미는 바꾸지 않습니다."))
        if v["role"] in {"manipulated", "simulation_input", "simulation_output"}:
            issues.append(_issue("variables:" + v["id"], "역사적 기후 관측값을 직접 조작한 실험으로 해석할 수 없습니다."))
    spatial = _value(current["design"], "spatial_scope")
    if spatial and spatial not in {"전 지구", "전지구", "세계", "global"}:
        issues.append(_issue("spatial_scope", "전 지구 자료로 입력한 지역의 결론을 낼 수 없습니다."))
    if issues:
        state.runtime_event(rid, "RESEARCH_DESIGN_ACTION_BLOCKED", {"issues": issues})
        raise ControlError("RESEARCH_DESIGN_ACTION_BLOCKED")
    return plan


def check_tool(state, contract, request):
    current = current_design(state, contract.research_id)
    if not current or request.tool_name not in {"stats.run", "analysis.skill"}:
        return []
    saved = state.runtime_step(contract.research_id, "research_design_contract:" + contract.contract_id)
    issues = list(resolve_design(current["effective_question"], current["design"],
                                 use_profile=_profile_enabled(state, contract.research_id, current))["issues"])
    if not saved or saved["output"]["hash"] != current["hash"]:
        issues.append(_issue("revision", "연구 조건이 변경되었습니다. 현재 조건으로 계획을 다시 확인해 주세요."))
    args = request.args
    plan = args.get("plan", {}) if request.tool_name == "analysis.skill" else args
    did = plan.get("dataset_id")
    issues += binding_issues(state, contract.research_id, current["design"], did)
    method = _value(current["design"], "method")
    if method and plan.get("method") != method:
        issues.append(_issue("method", "실행할 분석 방법이 입력한 조건과 다릅니다."))
    variables = [v for v in current["design"]["variables"] if v["active"] and v.get("binding")]
    selected = set(plan.get("variables", {}).values())
    if plan.get("timestamp_column"):
        selected.add(plan["timestamp_column"])
    required = {v["binding"]["column"] for v in variables if v["role"] in {"item", "outcome", "manipulated", "association", "time"}}
    if not required <= selected:
        issues.append(_issue("variables", "실행할 열이 선택한 연구 항목과 다릅니다."))
    periods = [[r["start"], r["end"]] for r in current["design"]["comparisons"] if "start" in r and "end" in r]
    if periods and args.get("parameters", {}).get("periods") != periods:
        issues.append(_issue("comparisons", "실행할 비교 기간이 입력한 조건과 다릅니다."))
    for key in ("inclusion", "exclusion", "missing", "outlier", "forbidden"):
        if _value(current["design"], key):
            issues.append(_issue(key, LABELS[key] + "의 자동 실행은 지원하지 않습니다. 원자료를 바꾸지 않으며 이 계산은 조건 확인 후에 진행할 수 있습니다.", "METHOD_UNSUPPORTED"))
    if any(v["role"] == "covariate" and v["active"] for v in current["design"]["variables"]):
        issues.append(_issue("variables", "입력한 공변량의 조정은 현재 자동 실행하지 않습니다.", "METHOD_UNSUPPORTED"))
    if issues:
        state.runtime_event(contract.research_id, "RESEARCH_DESIGN_ACTION_BLOCKED", {"contract_id": contract.contract_id, "issues": issues})
    else:
        controls = []
        fixed = [v for v in current["design"]["variables"] if v["active"] and v["role"] == "fixed" and v.get("binding")]
        if fixed and did:
            import csv
            record = state.dataset_record(did, contract.research_id)
            path = state.workspace.path(contract.research_id, record["stored_path"])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            for v in fixed:
                values = sorted({r[v["binding"]["column"]] for r in rows})
                target = v["details"].get("fixed_value", {}).get("value")
                controls.append({"variable_id": v["id"], "name": v["name"], "planned": target, "observed": values[:10],
                    "status": "UNKNOWN" if not target else "MATCHED_RECORDED_VALUES" if values == [target] else "DEVIATION",
                    "limitation": "파일의 기록값 대조이며 실제 통제 수행·장비 상태를 보증하지 않습니다."})
        state.finish_runtime_step(contract.research_id, "research_design_request:" + request.request_id,
            {"design_hash": current["hash"], "design_revision": current["revision"], "contract_id": contract.contract_id, "request_id": request.request_id,
             "tool": request.tool_name, "arguments": args, "requested_controls": [v for v in current["design"]["variables"] if v["role"] == "fixed"],
             "control_compliance": "UNKNOWN", "control_observations": controls, "observed_sample_count": None, "executed": False})
    return issues


def record_tool_outcome(state, request, result):
    key = "research_design_execution:" + request.request_id
    step = state.runtime_step(request.research_id, "research_design_request:" + request.request_id)
    if step:
        state.finish_runtime_step(request.research_id, key, {**step["output"], "executed": result.ok, "result_refs": [r.model_dump(mode="json") for r in result.artifacts]})


def verify_design(state, payload):
    if not payload.scientific:
        return []
    contract, _ = state.contract(payload.agent_result.contract_id)
    current = current_design(state, contract.research_id)
    if not current:
        return []
    saved = state.runtime_step(contract.research_id, "research_design_contract:" + contract.contract_id)
    execution = state.runtime_step(contract.research_id, "research_design_execution:" + payload.tool_request.request_id)
    valid = (saved and execution and execution["output"].get("executed")
             and saved["output"]["hash"] == current["hash"] == execution["output"]["design_hash"]
             and execution["output"]["contract_id"] == contract.contract_id
             and execution["output"]["arguments"] == payload.tool_request.args
             and execution["output"]["tool"] == payload.tool_request.tool_name
             and not binding_issues(state, contract.research_id, current["design"], payload.scientific.dataset_id))
    return [{"check_id": "RESEARCH_DESIGN_CURRENTNESS", "passed": bool(valid), "message": "현재 연구 조건·계약·실제 도구 호출에 연결되어야 합니다."}]


def design_context(state, rid, role):
    current = current_design(state, rid)
    if not current:
        return None
    d = current["design"]
    keys = set(LABELS) if role == "manager" else ({"scope", "period", "method", "target", "interpretation_limits", "inclusion", "exclusion", "missing", "outlier", "forbidden"} if role in {"analysis_planner_worker", "verification_coordinator"} else {"subject", "acquisition", "independent_unit", "repetition", "frequency", "instrument", "safety", "scope"})
    return {"revision": current["revision"], "hash": current["hash"], "intent_only": True,
            "fields": {k: v for k, v in d["fields"].items() if k in keys},
            "variables": [v for v in d["variables"] if v["active"]], "comparisons": d["comparisons"],
            "hypotheses": d["hypotheses"], "procedure": d["procedure"],
            "issues": current["issues"], "sample_count": d.get("sample_count")}


def summary(state, rid):
    current = current_design(state, rid)
    if not current:
        return {"available": False}
    executions = [json.loads(r[0]) for r in state._db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'research_design_execution:%' ORDER BY rowid", (rid,))]
    executions = [e for e in executions if e["design_hash"] == current["hash"] and e.get("executed")]
    planned = [LABELS[k] + ": " + v["value"] for k, v in current["design"]["fields"].items() if v["value"]]
    planned += [ROLE_LABELS[v["role"]] + ": " + v["name"] for v in current["design"]["variables"] if v["active"]]
    planned += ["예상하는 결과(가설): " + h["text"] for h in current["design"]["hypotheses"]]
    planned += ["비교할 집단·기간: " + to_json(r) for r in current["design"]["comparisons"]]
    limitations = [i["message"] for i in current["issues"]] + [x["label"] + " · " + x["message"] for x in current["dispositions"] if x["status"] not in {"PLANNING_CONSTRAINT", "INACTIVE"}]
    for e in executions:
        for control in e.get("control_observations", []):
            limitations.append(control["name"] + " · " + ("계획한 통제 값과 기록값이 다릅니다." if control["status"] == "DEVIATION" else "파일의 기록값만 대조했습니다.") + " " + control["limitation"])
    return {"available": True, "revision": current["revision"], "hash": current["hash"],
            "처음 정한 조건": planned, "실제로 사용한 자료·방법": [to_json(e["arguments"]) for e in executions],
            "확인하지 못한 조건 또는 달라진 점": limitations,
            "이 결론으로 말할 수 있는 범위": _value(current["design"], "interpretation_limits") or "실제 자료와 검증된 분석의 지원 범위",
            "control_compliance": "UNKNOWN", "trace": executions}


def amend_design(state, rid, raw, *, expected_version):
    from .control_plane import ControlError
    if type(expected_version) is not int or expected_version != state.state_version(rid):
        raise ControlError("RESEARCH_DESIGN_STALE")
    current = current_design(state, rid)
    question = state._one("SELECT goal,research_question FROM research_runs WHERE research_id=?", (rid,))
    prose = question["research_question"] or question["goal"]
    if current and prose == current["effective_question"]:
        prose = current["original_question"]
    resolved = resolve_design(prose, raw, use_profile=_profile_enabled(state, rid, current))
    if not current and not resolved["condition_count"]:
        return {"changed": False, "revision": 0, "state_version": expected_version, "paid_calls": 0}
    if current and current["hash"] == resolved["hash"]:
        return {"changed": False, "revision": current["revision"], "state_version": state.state_version(rid), "paid_calls": 0}
    revision = (current["revision"] if current else 0) + 1
    document = {**resolved, "revision": revision, "original_question": current["original_question"] if current else question["goal"],
                "author": "owner", "created_at": utc_now().isoformat()}
    authority_update = None
    effective = resolved["effective_question"]
    from .qualified_workflow import latest_authority
    authority = latest_authority(state, rid)
    candidate = resolved["profile"]
    if authority and not resolved["issues"] and candidate["status"] == "SUPPORTED" and effective != authority["plan"]["question"]:
        old = authority["plan"]
        if candidate["profile_id"] == old["profile_id"]:
            plan = dict(old, question=effective, periods=candidate["periods"], transform=candidate["transform"], question_revision=old["question_revision"] + 1)
            authority_update = {"plan": plan, "reviewed_by": "owner", "previous_question": old["question"],
                                "amendment_kind": "RESEARCH_DESIGN_AMENDMENT", "approved_at": utc_now().isoformat()}
    version = state.record_qualified_revision(rid, "RESEARCH_DESIGN_AMENDED", "research_design:" + str(revision),
        document, expected_version=expected_version, affected=True,
        question=effective if not resolved["issues"] and (not authority or authority_update) else None,
        qualified_authority=authority_update)
    return {"changed": True, "revision": revision, "state_version": version, "status": "NEEDS_REVALIDATION", "paid_calls": 0}
