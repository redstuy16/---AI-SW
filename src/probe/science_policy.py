"""고교 과학 탐구의 실행 설정과 비용 단위를 분리한다."""
from __future__ import annotations

from decimal import Decimal
import json

from .control_plane import ControlError, ModelProfile, ROLES
from .database import to_json
from .schemas import utc_now

SEARCH_PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing"
MODEL_LIMIT_FIELDS = frozenset({"input_byte_limit", "context_limit", "output_limit", "task_output_limits",
                                "max_input_tokens", "max_output_tokens"})
_OWNER_LIMIT_FIELDS = ("input_byte_limit", "context_limit", "output_limit", "task_output_limits")


def record_model_limit_policy(store, profile_id, fields):
    """모델 저장과 같은 트랜잭션에서 소유자가 명시한 상한을 별도로 기록한다."""
    declared = sorted(MODEL_LIMIT_FIELDS.intersection(fields))
    if not declared:
        return
    old = store.db.execute("SELECT revision FROM control_configs WHERE kind='model_limit_policy' AND id=?", (profile_id,)).fetchone()
    value = {"profile_id": profile_id, "explicit_limits": True, "declared_fields": declared,
             "source": "OWNER_MODEL_SETTINGS", "declared_at": utc_now().isoformat()}
    store.db.execute("INSERT OR REPLACE INTO control_configs VALUES('model_limit_policy',?,?,?)",
                     (profile_id, old[0] + 1 if old else 1, to_json(value)))
    store.audit(None, "MODEL_LIMITS_DECLARED", value)


def _historical_owner_limits(store, profile_id):
    """자동 생성 이후 상한 자체를 변경한 과거 기록만 명시 설정의 근거로 쓴다."""
    for row in store.db.execute("SELECT payload FROM control_audit WHERE kind='CONFIG_UPDATED' AND json_extract(payload,'$.kind')='model' AND json_extract(payload,'$.id')=? ORDER BY seq DESC", (profile_id,)):
        record = json.loads(row[0])
        if record.get("old") is None:
            # 삭제 후 같은 ID로 다시 생성한 구성에는 이전 소유자 설정을 옮기지 않는다.
            break
        try:
            before = ModelProfile.model_validate(record["old"]).model_dump(mode="json")
            after = ModelProfile.model_validate(record["new"]).model_dump(mode="json")
        except (KeyError, ValueError):
            continue
        if any(before[key] != after[key] for key in _OWNER_LIMIT_FIELDS):
            return True
    return False


def explicit_model_limits(store, models):
    """선택된 구성의 상한 출처를 스냅숏에 동결하며 모델·캐시 스키마는 바꾸지 않는다."""
    result = {}
    for identity in sorted({raw["profile_id"] for raw in models.values()}):
        try:
            policy = store.config("model_limit_policy", identity)
        except ControlError as exc:
            if exc.code != "CONFIG_MISSING":
                raise
            explicit = identity.startswith("AUTO-") and _historical_owner_limits(store, identity)
        else:
            if policy.get("profile_id") != identity or type(policy.get("explicit_limits")) is not bool:
                raise ControlError("MODEL_LIMIT_POLICY_INVALID")
            explicit = policy["explicit_limits"]
        if explicit:
            result[identity] = True
    return result


def sync_ledger_budget(db, rid):
    """중개 호출과 AI 검색의 실제 비용을 같은 원장에서 읽어 중복 합산을 막는다."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='spend_ledger'").fetchone():
        return
    settled = db.execute("SELECT COALESCE(SUM(CASE WHEN status='SETTLED' THEN settled WHEN status='RELEASED' THEN 0 ELSE reserved END),0) FROM spend_ledger WHERE research_id=?", (rid,)).fetchone()[0]
    other = db.execute("SELECT COALESCE(SUM(estimated_cost_usd),0) FROM agent_runs WHERE research_id=? AND provider<>'control_broker'", (rid,)).fetchone()[0]
    db.execute("UPDATE research_budgets SET spent_usd=? WHERE research_id=?", (settled / 1000000 + other, rid))


def science_enabled(snapshot):
    return snapshot.get("execution_mode") == "SCIENCE_AUTO"


def startup_roles(snapshot):
    """과학 모드의 시작에는 판단 역할만 필요하며 선택 역할은 실제 호출 때 검사한다."""
    return ("manager",) if science_enabled(snapshot) else ROLES


def prepare_science_profiles(snapshot):
    """기존 설정을 덮어쓰지 않고 새 연구 스냅숏의 자동 한도를 정한다."""
    if not science_enabled(snapshot):
        return
    from .research_report import requested_report_sections
    if len(requested_report_sections(snapshot.get("question", ""))) >= 8:
        snapshot.setdefault("science_context_budget", 64000)
        snapshot.setdefault("science_max_actions", 200)
    if snapshot.get("depth_limits"):
        # 판단과 필요한 위임을 같은 루프 상한 안에서 허용한다. 금액·미정산 보호는 별도 유지한다.
        snapshot["depth_limits"]["attempts"] = min(20, max(snapshot["depth_limits"].get("attempts", 1), 3 * snapshot.get("science_max_decisions", 6)))
    for raw in snapshot.get("models", {}).values():
        automatic = (raw.get("profile_id", "").startswith("AUTO-") and
                     snapshot.get("explicit_model_limits", {}).get(raw.get("profile_id")) is not True)
        connection = snapshot.get("connections", {}).get(raw.get("connection_id"), {})
        hosted_search = automatic and connection.get("adapter_id") == "openai" and raw.get("model_id") == "gpt-6-luna"
        if hosted_search:
            raw["context_limit"] = max(raw.get("context_limit", 32768), 131072)
            if raw.get("max_input_tokens") is None:
                raw["max_input_tokens"] = 128000
        if automatic:
            # 과학 요청의 응답 스키마까지 담을 수 있도록 이전 전송 크기 기본값을 확장한다.
            raw["input_byte_limit"] = max(raw.get("input_byte_limit", 32000), min(128000, raw.get("max_input_tokens") or 128000))
            maximum = raw.get("max_output_tokens") or 32768
            report = min(32768, maximum, raw.get("context_limit", 32768) // 2)
            raw["output_limit"] = report
            raw["task_output_limits"] = {"planning": min(16384, report), "report": report}
        if hosted_search:
            # 검색 도구의 입력 예약에 늘어난 출력 공간을 더한다.
            raw["context_limit"] = max(raw["context_limit"], 128000 + raw["output_limit"])
        if automatic:
            # 실제 요청에는 연구 문맥 외에 역할 지침과 응답 스키마도 포함된다.
            available = min(131072, raw["context_limit"] - raw["output_limit"],
                            raw.get("max_input_tokens") or raw["context_limit"])
            raw["input_byte_limit"] = max(raw.get("input_byte_limit", 32000), available)
        if connection.get("adapter_id") == "openai" and raw.get("price"):
            if raw["price"].get("web_search_per_call") is None:
                raw["price"].update(web_search_per_call="0.01", web_search_price_source=SEARCH_PRICE_SOURCE)


def request_cost_bound(profile: ModelProfile, wire_bytes: int, input_tokens: int, output_tokens: int):
    """전송 크기, 문맥 상한, 토큰 비용을 서로 다른 단위로 검사한다."""
    if wire_bytes > profile.input_byte_limit or input_tokens + output_tokens > profile.context_limit:
        raise ControlError("CONTEXT_LIMIT_BLOCKED")
    if profile.max_input_tokens is not None and input_tokens > profile.max_input_tokens:
        raise ControlError("CONTEXT_LIMIT_BLOCKED")
    from .control_plane import admitted_cost
    admitted_cost(profile.model_copy(update={"output_limit": 32}), 0)
    price = profile.price
    rate = max(price.input_per_million, price.cached_input_per_million or 0, price.cache_write_per_million or 0)
    return (Decimal(input_tokens) * rate + Decimal(output_tokens) * price.output_per_million) / Decimal(1000000)


def context_budget(snapshot, *, role=None):
    if not science_enabled(snapshot):
        profiles = [ModelProfile.model_validate(v) for v in snapshot["models"].values()]
        return min(4096, min(p.input_byte_limit for p in profiles) // 4)
    # 사용하지 않는 작은 모델이 판단 역할의 문맥을 줄이지 않도록 역할별로 계산한다.
    raw = snapshot["models"].get(role or "manager", snapshot["models"]["manager"])
    profile = ModelProfile.model_validate(raw)
    requested = int(snapshot.get('science_context_budget', 16000))
    return min(max(1, requested), 65536, profile.input_byte_limit, profile.context_limit - profile.output_limit)
