"""결정적 계약·예산·상위 판단 정책."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .agent_schemas import EscalationDecision, FailureEvent
from .schemas import ResearchContract
from .service import ContractViolationError, StateService


ROLE_TOOLS = {
    "manager": set(),
    "experiment_coordinator": {"data.import", "data.profile"},
    "analysis_planner_worker": {"stats.run", "visualization.render", "evidence.record", "analysis.skill",
                                "science.calculate", "science.dataset", "science.fetch_data", "science.fetch_source"},
    "verification_coordinator": set(),
    "literature_verification_coordinator": set(),
}
STRATEGIC_CODES = {
    "CORE_RESULT_REVERSAL", "COORDINATOR_CONFLICT", "CANONICAL_CONTRADICTION",
    "MAJOR_UNSUPPORTED_CONCLUSION", "ALL_HYPOTHESES_FAILED",
    "GLOBAL_PLAN_CHANGE_REQUIRED", "BUDGET_SCOPE_CHANGE_REQUIRED",
    "RESEARCH_TERMINATION_DECISION",
}


def validate_contract(state: StateService, contract: ResearchContract, *,
                      expected_research_id: str, remaining_usd: float) -> None:
    if contract.research_id != expected_research_id:
        raise ContractViolationError("CONTRACT_INVALID: research ID mismatch")
    allowed = ROLE_TOOLS.get(contract.assigned_role)
    if allowed is None or not set(contract.allowed_tools) <= allowed:
        raise ContractViolationError("TOOL_NOT_ALLOWED: role tool allowlist")
    if "analysis.skill" in contract.allowed_tools and not getattr(state, "verified_analysis_skills_enabled", False):
        raise ContractViolationError("TOOL_NOT_ALLOWED: verified analysis Skills are disabled")
    if set(contract.allowed_tools) & {'science.calculate', 'science.dataset', 'science.fetch_data', 'science.fetch_source'}:
        if contract.task_type != 'ScienceToolResult' or not getattr(state, 'science_tools_enabled', False):
            raise ContractViolationError('TOOL_NOT_ALLOWED: scientific tools are disabled')
    limits = contract.constraints
    if limits.max_tool_calls > 10 or limits.max_retries > 2 or limits.max_runtime_sec > 300:
        raise ContractViolationError("CONTRACT_INVALID: hard cap exceeded")
    if limits.max_cost_usd > remaining_usd:
        raise ContractViolationError("BUDGET_EXCEEDED: contract cost cap")
    if contract.parent_task_id:
        row = state._db.execute("SELECT research_id,status FROM tasks WHERE task_id=?",
                                (contract.parent_task_id,)).fetchone()
        if row is None or row["research_id"] != expected_research_id or row["status"] in {"FAILED", "CANCELLED"}:
            raise ContractViolationError("CONTRACT_INVALID: parent task")
    for ref in contract.inputs + contract.context_policy.must_preserve:
        if not state.resolve_ref(expected_research_id, ref):
            raise ContractViolationError("CONTRACT_INVALID: stale or unresolved ref")
        if ref.type.value in {"evidence", "experiment"}:
            table, key = ("evidence", "evidence_id") if ref.type.value == "evidence" else ("experiments", "experiment_id")
            row = state._db.execute(f"SELECT status FROM {table} WHERE {key}=? AND research_id=?",
                                    (ref.id, expected_research_id)).fetchone()
            if row is None or row["status"] != "VERIFIED":
                raise ContractViolationError("CONTRACT_INVALID: unverified scientific ref")


@dataclass(frozen=True)
class Price:
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    max_call_usd: float


class PricingRegistry:
    def __init__(self, prices: dict[tuple[str, str], Price] | None = None):
        self.prices = prices or {}

    def estimate(self, provider: str, model: str, usage: dict) -> float | None:
        price = self.prices.get((provider, model))
        if price is None or usage.get("input_tokens") is None or usage.get("output_tokens") is None:
            return None
        cached = usage.get("cached_input_tokens") or 0
        fresh = max(0, usage["input_tokens"] - cached)
        return (fresh * price.input_per_million + cached * price.cached_input_per_million
                + usage["output_tokens"] * price.output_per_million) / 1_000_000

    def reservation(self, provider: str, model: str) -> float | None:
        price = self.prices.get((provider, model))
        return price.max_call_usd if price else None

    @classmethod
    def from_json_file(cls, path: str | Path) -> "PricingRegistry":
        document = json.loads(Path(path).read_text(encoding="utf-8", errors="strict"))
        prices = {}
        for entry in document["entries"]:
            price = Price(input_per_million=float(entry["input_per_million"]),
                          cached_input_per_million=float(entry["cached_input_per_million"]),
                          output_per_million=float(entry["output_per_million"]),
                          max_call_usd=float(entry["max_call_usd"]))
            if min(price.input_per_million, price.cached_input_per_million,
                   price.output_per_million, price.max_call_usd) < 0:
                raise ValueError("pricing values must be nonnegative")
            key = (entry["provider"], entry["model"])
            if key in prices:
                raise ValueError("duplicate provider/model price")
            prices[key] = price
        return cls(prices)


class BudgetExceededError(Exception):
    """다음 요청의 알려진 비용이 연구의 최대 예산을 넘는다."""


@dataclass(frozen=True)
class UnknownPriceLimits:
    max_calls: int = 20
    max_manager_calls: int = 5
    max_input_tokens: int = 100_000
    max_output_tokens: int = 20_000
    reserve_input_tokens: int = 4_000
    reserve_output_tokens: int = 1_000


class BudgetController:
    def __init__(self, state: StateService, pricing: PricingRegistry,
                 unknown_limits: UnknownPriceLimits | None = None):
        self.state, self.pricing = state, pricing
        self.unknown_limits = unknown_limits or UnknownPriceLimits()

    def before_call(self, research_id: str, provider: str, model: str, contract: ResearchContract) -> None:
        # 중개자의 정산 원장이 가격·예약·미정산 노출과 하드 상한을 검사한다.
        if provider == "control_broker":
            return
        budget = self.state.budget(research_id)
        reserve = self.pricing.reservation(provider, model)
        if reserve is None:
            limits = self.unknown_limits
            if (budget["unknown_price_calls"] >= limits.max_calls or
                    (contract.assigned_role == "manager" and budget["manager_calls"] >= limits.max_manager_calls) or
                    budget["unknown_price_input_tokens"] + limits.reserve_input_tokens > limits.max_input_tokens or
                    budget["unknown_price_output_tokens"] + limits.reserve_output_tokens > limits.max_output_tokens):
                self.state.runtime_event(research_id, "BUDGET_HARD_LIMIT", {"model": model, "reason": "unknown_price_cap"})
                raise BudgetExceededError("BUDGET_EXCEEDED")
            self.state.runtime_event(research_id, "BUDGET_UNKNOWN_PRICE", {"model": model})
        row = self.state._db.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd),0) AS spent FROM agent_runs WHERE contract_id=?",
            (contract.contract_id,)).fetchone()
        contract_spent = row["spent"]
        if budget["spent_usd"] >= budget["hard_limit_usd"] or (reserve is not None and
                reserve > min(budget["remaining_usd"], contract.constraints.max_cost_usd - contract_spent)):
            self.state.runtime_event(research_id, "BUDGET_HARD_LIMIT", {"model": model})
            raise BudgetExceededError("BUDGET_EXCEEDED")
        if budget["spent_usd"] >= budget["soft_limit_usd"]:
            self.state.runtime_event(research_id, "BUDGET_SOFT_LIMIT", {"spent_usd": budget["spent_usd"]})


def decide_escalation(failure: FailureEvent, max_retries: int) -> EscalationDecision:
    if failure.code in STRATEGIC_CODES:
        return EscalationDecision(level=3, action="manager_escalation", reason=failure.code)
    if failure.attempt <= max_retries and failure.code in {"PROVIDER_TIMEOUT", "PROVIDER_RATE_LIMIT", "PROVIDER_ERROR", "TOOL_FAILURE"}:
        return EscalationDecision(level=0, action="retry", reason=failure.code)
    if failure.attempt == 1 and failure.code in {"STRUCTURED_OUTPUT_INVALID", "FORMAT_REPAIR", "SINGLE_BAD_TOOL_ARGUMENT"}:
        return EscalationDecision(level=1, action="worker_repair", reason=failure.code)
    return EscalationDecision(level=2, action="coordinator_diagnosis", reason=failure.code)
