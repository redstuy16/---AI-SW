"""GISTEMP v4 연간 전 지구 편차의 두 기간 비교만 지원한다."""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import io
import math
import re

from .qualified_profiles import fingerprint
from .storage import sha256_bytes
from .period_comparison import selected_values, compare_keyed, numeric_value, require_meaning

PROFILE_ID = "public_climate_timeseries_comparison_v1"
SOURCE_URL = "https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.txt"
DOC_URL = "https://data.giss.nasa.gov/gistemp/faq/"
SECONDARY_URL = "https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.csv"
CHECKER_VERSION = "cycle12-climate-1"
MEANING = {"product": "NASA GISTEMP v4 L-OTI", "quantity_kind": "temperature_anomaly",
           "unit": "degC", "storage_scale": "0.01", "baseline": [1951, 1980],
           "spatial_scope": "global", "temporal_resolution": "annual", "value_column": "J-D"}
CSV_MEANING = dict(MEANING, storage_scale="1")
SOURCE_POLICY = {"secondary_required": False, "relation": "SAME_UPSTREAM_REPRESENTATION",
                 "checker_version": CHECKER_VERSION, "method": "two_period_comparison"}
RESIDUAL_LIMIT = "두 형식은 같은 NASA 제품을 사용합니다. 형식 대조는 독립 관측이나 과학적 진위 인증이 아니며, 공통 원자료·질문 해석의 오류는 남을 수 있습니다."


def read_capture(state, rid, capture):
    path = state.workspace.path(rid, capture["source_relative"])
    with path.open("rb") as stream:
        data = stream.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("PROFILE_SOURCE_LIMIT")
    if sha256_bytes(data) != capture["source_sha256"]:
        raise ValueError("SOURCE_HASH_MISMATCH")
    return data.decode("utf-8", errors="strict")


def parse_source(text, metadata):
    require_meaning(MEANING, metadata)
    lines = text.splitlines()
    if not lines or "GLOBAL Land-Ocean Temperature Index in 0.01 degrees Celsius" not in lines[0] or "1951-1980" not in lines[0]:
        raise ValueError("공식 제품·단위·기준 기간을 확인할 수 없습니다.")
    header = next((line.split() for line in lines if line.startswith("Year") and "J-D" in line), None)
    if not header:
        raise ValueError("연간 J-D 열이 없습니다.")
    column = header.index("J-D")
    rows, seen = [], set()
    for line in lines:
        if not re.match(r"^\d{4}\s", line):
            continue
        fields = line.split()
        if len(fields) != len(header) or fields[0] != fields[-1]:
            raise ValueError("연도와 값 열의 위치가 일치하지 않습니다.")
        year = int(fields[0])
        if year in seen:
            raise ValueError("원본에 중복된 연도가 있습니다.")
        seen.add(year)
        value = fields[column]
        if "*" in value:
            rows.append({"year": year, "value": None})
        else:
            try:
                amount = Decimal(value) * Decimal(metadata["storage_scale"])
            except InvalidOperation:
                raise ValueError("SELECTED_VALUE_INVALID: 관측값을 읽을 수 없습니다.") from None
            if not amount.is_finite():
                raise ValueError("유한하지 않은 관측값입니다.")
            rows.append({"year": year, "value": str(amount)})
    if not rows:
        raise ValueError("관측 연도가 없습니다.")
    return rows


def parse_csv_source(text, metadata):
    require_meaning(CSV_MEANING, metadata)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "Land-Ocean: Global Means":
        raise ValueError("SOURCE_PRODUCT_UNVERIFIED")
    reader = csv.DictReader(lines[1:])
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not {"Year", "J-D"} <= set(reader.fieldnames):
        raise ValueError("SOURCE_ANNUAL_COLUMN_MISSING")
    rows, seen = [], set()
    for row in reader:
        key = row.get("Year", "")
        if not key or not key.isdigit() or len(key) != 4 or None in row:
            raise ValueError("SOURCE_KEY_INVALID")
        year = int(key)
        if year in seen:
            raise ValueError("SELECTED_KEY_DUPLICATE")
        seen.add(year)
        value = row.get("J-D")
        if value is None or not value.strip() or "*" in value:
            value = None
        else:
            value = str(numeric_value(value, year))
        rows.append({"year": year, "value": value})
    if not rows:
        raise ValueError("SOURCE_EMPTY")
    return rows


def selected_rows(rows, periods):
    years = set()
    for start, end in periods:
        if not 1880 <= start <= end <= 9999:
            raise ValueError("기간을 확인해 주세요.")
        part = set(range(start, end + 1))
        if years & part:
            raise ValueError("비교 기간이 겹칩니다.")
        years |= part
    selected_values(rows, sorted(years))
    return [r for r in rows if r["year"] in years]


def dependency_hash(rows, plan):
    groups = [selected_rows(rows, plan["periods"])]
    if plan["transform"].get("baseline_period"):
        groups.append(selected_rows(rows, [plan["transform"]["baseline_period"]]))
    # 비가중 평균은 같은 키·값의 순열에 불변이다. 원본 순서는 원본 파일에 남는다.
    return fingerprint([[{"year": r["year"], "value": str(numeric_value(r["value"], r["year"]).normalize())}
                         for r in sorted(group, key=lambda r: r["year"])] for group in groups])


def representation_check(state, rid, source, plan):
    required = plan.get("source_policy", SOURCE_POLICY)["secondary_required"]
    secondary = source.get("secondary")
    if secondary is None:
        return {"status": "CHECK_PENDING" if required else "NOT_PRESENT_OPTIONAL", "eligible": not required,
                "required": required, "performed": False, "differing_keys": [], "limitation": RESIDUAL_LIMIT}
    result = {"status": "SOURCE_CONFLICT", "eligible": False, "required": required, "performed": False,
              "differing_keys": [], "differing_fields": [], "primary_sha256": source["source_sha256"],
              "secondary_sha256": secondary["source_sha256"], "limitation": RESIDUAL_LIMIT}
    try:
        primary_text = read_capture(state, rid, source)
        secondary_text = read_capture(state, rid, secondary)
        for label, capture, expected in (("primary", source, MEANING), ("secondary", secondary, CSV_MEANING)):
            actual = capture.get("semantics", {})
            result["differing_fields"] += [label + "." + field for field in expected if actual.get(field) != expected[field]]
        if result["differing_fields"]:
            raise ValueError("SOURCE_MEANING_CONFLICT")
        if source.get("capture_group") != secondary.get("capture_group"):
            raise ValueError("SOURCE_CAPTURE_INCOMPARABLE")
        a, b = source.get("capture", {}), secondary.get("capture", {})
        if a.get("http_observed") and b.get("http_observed"):
            from datetime import datetime
            first = datetime.fromisoformat(a["retrieved_at"])
            second = datetime.fromisoformat(b["retrieved_at"])
            if first.tzinfo is None or second.tzinfo is None or abs((first - second).total_seconds()) > 300:
                raise ValueError("SOURCE_CAPTURE_INCOMPARABLE")
        primary = parse_source(primary_text, source["semantics"])
        alternate = parse_csv_source(secondary_text, secondary["semantics"])
        keys = [r["year"] for r in selected_rows(primary, plan["periods"])]
        if plan["transform"].get("baseline_period"):
            baseline = selected_rows(primary, [plan["transform"]["baseline_period"]])
            keys += [r["year"] for r in baseline if r["year"] not in keys]
        different = compare_keyed(primary, alternate, keys)
        result.update(performed=True, differing_keys=different, eligible=not different,
                      status="MATCHED_SELECTED_KEYS" if not different else "SOURCE_CONFLICT")
    except (ValueError, KeyError, TypeError, OSError, UnicodeError) as error:
        result["reason"] = getattr(error, "code", str(error))
        result["differing_keys"] = getattr(error, "keys", [])
    return result


def transform_rows(rows, transform):
    if set(transform) - {"unit", "baseline_period"} or transform.get("unit", "degC") not in {"degC", "degF_difference"}:
        raise ValueError("이 변환은 현재 지원하지 않습니다.")
    offset = Decimal(0)
    baseline = transform.get("baseline_period")
    if baseline:
        group = selected_rows(rows, [baseline])
        offset = sum(Decimal(r["value"]) for r in group) / len(group)
    factor = Decimal("1.8") if transform.get("unit") == "degF_difference" else Decimal(1)
    return [{"year": r["year"], "value": str((Decimal(r["value"]) - offset) * factor) if r["value"] is not None else None} for r in rows]


def independent_comparison(rows, periods):
    """도구의 부동소수 평균과 별개로 Decimal 합계를 계산한다."""
    selected_rows(rows, periods)
    means = []
    for start, end in periods:
        values = [Decimal(r["value"]) for r in rows if start <= r["year"] <= end]
        means.append(sum(values) / len(values))
    return means, means[1] - means[0]


def normalized_csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["year", "value"])
    writer.writerows((r["year"], r["value"] or "") for r in rows)
    return stream.getvalue()


def claim_text(plan, result):
    a, b = plan["periods"]
    unit = "°F 편차" if plan["transform"].get("unit") == "degF_difference" else "°C 편차"
    return f"전 지구 연간 기온 편차의 관측 평균은 {a[0]}–{a[1]}년 {result['periods'][0]['mean']:.6g}, {b[0]}–{b[1]}년 {result['periods'][1]['mean']:.6g}이며, 뒤 기간에서 앞 기간을 뺀 차이는 {result['difference']:.6g} {unit}입니다."


class ClimateComparisonProfile:
    profile_id = PROFILE_ID
    display_name = "공개 기후 자료의 두 기간 비교"
    qualified = True

    def candidate(self, question):
        if not any(k in question.lower() for k in ("기온", "온도", "기후", "gistemp", "temperature", "climate")):
            return None
        if not any(k in question.lower() for k in ("기후", "climate", "gistemp", "nasa", "전 지구", "전지구", "세계", "global", "한국", "서울", "경남")):
            return None
        if not any(k in question.lower() for k in ("전 지구", "전지구", "세계", "global", "gistemp", "nasa")):
            return {"status": "UNSUPPORTED", "message": "현재 기후 연구는 전 지구 연간 편차의 두 기간 비교를 지원합니다. 지역 분석에는 지역별 관측 자료와 검증된 방법이 필요합니다."}
        if any(k in question.lower() for k in ("왜", "원인", "인과", "때문", "예측", "전망", "유의", "검정", "상관", "회귀", "모형", "p-value", "절대 기온", "absolute", "causal", "forecast")):
            return {"status": "UNSUPPORTED", "message": "이 질문에는 인과 식별·예측·확증 검정 또는 절대 온도 복원 방법이 필요합니다. 현재 프로필은 두 기간의 관측 평균 비교만 지원합니다."}
        range_pattern = r"((?:18|19|20)\d{2})\s*년?\s*(?:~|∼|–|—|-|부터)\s*((?:18|19|20)\d{2})"
        baseline = re.search(r"기준\s*(?:기간)?(?:을|이)?\s*" + range_pattern, question)
        if baseline is None:
            baseline = re.search(range_pattern + r"\s*년?\s*기준", question)
        comparison_question = question[:baseline.start()] + question[baseline.end():] if baseline else question
        periods = [[int(a), int(b)] for a, b in re.findall(range_pattern, comparison_question)]
        if len(periods) != 2 or any(a > b for a, b in periods):
            return {"status": "CLARIFICATION_REQUIRED", "message": "비교할 두 기간을 적어 주세요. 예: 1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균 비교"}
        if any(k in question for k in ("한국", "서울", "경남", "지역")):
            return {"status": "UNSUPPORTED", "message": "전 지구 자료로 지역별 기온 결론을 낼 수 없습니다. 해당 지역의 관측 자료가 필요합니다."}
        transform = {"unit": "degF_difference" if "화씨" in question else "degC"}
        if baseline:
            transform["baseline_period"] = [int(baseline[1]), int(baseline[2])]
        return {"status": "SUPPORTED", "periods": periods, "transform": transform,
                "method": "two_period_comparison", "literature": "NOT_APPLICABLE", "paid_roles":["manager"]}

    def checks(self, state, payload, binding):
        checks = []
        def check(name, passed, message):
            checks.append({"check_id": "PROFILE_" + name, "passed": bool(passed), "message": message})
        rid = payload.agent_result.research_id
        plan = binding["plan"]
        expected_scope = {"profile_id": self.profile_id, "periods": plan["periods"], "semantics":plan["semantics"],
                          "transform":plan["transform"], "used_rows_hash":binding["used_rows_hash"]}
        check("GOAL_BINDING", binding["contract_id"] == payload.agent_result.contract_id and
              payload.agent_result.provenance.get("qualified_scope") == expected_scope and
              payload.agent_result.provenance.get("qualified_claim_id") == binding["claim_id"] == "QL-" + rid[2:] and
              payload.agent_result.provenance.get("qualified_claim_revision") == binding["claim_revision"],
              "결론의 질문·의미·변경 이력이 고정한 계약과 일치합니다.")
        try:
            raw = read_capture(state, rid, binding)
        except (ValueError, OSError, UnicodeError):
            check("SOURCE_HASH", False, "저장한 자료가 없거나 해시·크기·인코딩이 바뀌었습니다.")
            return checks
        check("SOURCE_HASH", True, "저장한 UTF-8 자료의 해시가 수집 기록과 같습니다.")
        rows = parse_source(raw, plan["semantics"])
        transformed = transform_rows(rows, plan["transform"])
        selected = selected_rows(transformed, plan["periods"])
        check("DEPENDENCY", dependency_hash(rows, plan) == binding["used_rows_hash"], "사용한 연도와 기준 변환의 관측값이 연결됩니다.")
        dataset = state.dataset_record(payload.scientific.dataset_id, rid)
        stored = state.workspace.path(rid, dataset["stored_path"])
        actual = stored.read_text(encoding="utf-8", errors="strict")
        check("NORMALIZATION", actual == normalized_csv(transformed), "원본의 저장 배율과 의미 보존 변환만 적용되었습니다.")
        question = state._one("SELECT goal,research_question FROM research_runs WHERE research_id=?", (rid,))
        authority = state.runtime_step(rid, binding.get("authority_key", "qualified_authority:1"))
        check("QUESTION", question["goal"] == plan.get("original_question", plan["question"]) and
              (question["research_question"] or question["goal"]) == plan["question"], "원질문과 명시적으로 변경한 현재 질문이 기록된 절차와 일치합니다.")
        candidate = self.candidate(plan["question"])
        check("INTERPRETATION", candidate and candidate["status"] == "SUPPORTED" and candidate["periods"] == plan["periods"] and candidate["transform"] == plan["transform"], "지원하는 질문 표현의 기간·변환을 다시 확인합니다. 임의 문장의 의미를 보장하지 않습니다.")
        from .qualified_workflow import latest_authority
        active_authority = latest_authority(state, rid)
        check("AUTHORITY", authority and active_authority and active_authority["step_key"] == binding.get("authority_key") and
              authority["output"]["plan"] == plan and authority["output"]["reviewed_by"] in {"profile", "owner"}, "절차와 필수 대조 정책은 저장된 소유자·프로필 결정에서만 가져옵니다.")
        check("CHECKER", plan.get("checker_version") == CHECKER_VERSION, "현재 검증 절차의 버전과 일치합니다.")
        representation = representation_check(state, rid, binding, plan)
        checks.append({"check_id": "PROFILE_REPRESENTATION_POLICY", "passed": representation["eligible"],
                       "message": "선택 키별 형식 대조: " + representation["status"], "details": representation})
        check("METHOD", payload.tool_request.args == {"dataset_id": payload.scientific.dataset_id, "method": "two_period_comparison", "variables": {"year": "year", "value": "value"}, "parameters": {"periods": plan["periods"]}}, "승인된 두 기간과 연간 열만 계산합니다.")
        result = payload.tool_result.result
        means, difference = independent_comparison(transformed, plan["periods"])
        check("INDEPENDENT_CALCULATION", result["n"] == len(selected) and len(result["periods"]) == 2 and
              all(math.isclose(float(expected), result["periods"][i]["mean"], rel_tol=1e-12, abs_tol=1e-12) for i, expected in enumerate(means)) and
              math.isclose(float(difference), result["difference"], rel_tol=1e-12, abs_tol=1e-12), "독립 합계 계산으로 두 평균과 차이를 확인합니다.")
        check("CLAIM_SCOPE", payload.scientific.claim == claim_text(plan, result), "관측 편차 비교의 범위를 넘는 결론을 승인하지 않습니다.")
        from .qualified_workflow import latest_source
        latest = latest_source(state, rid)
        if latest:
            check("CURRENTNESS", latest["semantic_hash"] == fingerprint(plan["semantics"]) and latest.get("status") == "SUPPORTED", "최근 확인한 자료에서도 이 결론의 의미가 유지됩니다. 선택값은 실제 자료로 재검사합니다.")
            try:
                current_rows = parse_source(read_capture(state, rid, latest), latest["semantics"])
                intact = dependency_hash(current_rows, plan) == binding["used_rows_hash"]
            except (ValueError, KeyError, TypeError, OSError, UnicodeError):
                intact = False
            check("CURRENT_CAPTURE", intact, "최근 저장된 실제 자료도 다시 검사합니다.")
            latest_representation = representation_check(state, rid, latest, plan)
            checks.append({"check_id": "PROFILE_CURRENT_REPRESENTATION", "passed": latest_representation["eligible"],
                           "message": "최근 자료의 형식 대조: " + latest_representation["status"], "details": latest_representation})
        return checks
