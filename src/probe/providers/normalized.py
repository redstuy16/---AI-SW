"""정본 상태와 자격 증명을 포함하지 않는 공통 생성 계약."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, PrivateAttr, field_validator

from ..schemas import StrictModel
from .base import ModelProviderError


class CapabilityStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class CapabilityEvidence(StrictModel):
    status: CapabilityStatus = CapabilityStatus.UNKNOWN
    source: Literal["LIVE_CAPABILITY_TEST", "PROVIDER_METADATA", "STATIC_ADAPTER_RULE", "USER_DECLARED", "UNKNOWN"] = "UNKNOWN"
    checked_at: str | None = None
    details: str = Field(default="", max_length=1000)


CAPABILITIES = ("text", "tool_calling", "parallel_tools", "structured_output", "json_mode", "reasoning", "streaming", "usage_reporting", "temperature", "top_p", "stop", "seed", "storage", "web_search")


class ReasoningPolicy(StrEnum):
    AUTO = "AUTO"
    DISABLED = "DISABLED"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTRA_HIGH = "EXTRA_HIGH"
    MAX = "MAX"


class NormalizedTool(StrictModel):
    tool_name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    description: str = Field(default="", max_length=2000)
    parameters: dict[str, Any]


class NormalizedToolCall(StrictModel):
    call_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    arguments: dict[str, Any]
    provider_call_type: str


class NormalizedToolResult(StrictModel):
    call_id: str = Field(min_length=1, max_length=256)
    tool_name: str
    result: Any
    is_error: bool = False


class HostedWebSearchTool(StrictModel):
    type: Literal["web_search"] = "web_search"
    search_context_size: Literal["low", "medium", "high"] = "medium"
    external_web_access: bool = True


class HostedToolCall(StrictModel):
    id: str
    status: str
    action: Literal["search", "open_page", "find_in_page"]
    queries: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


class URLCitation(StrictModel):
    url: str
    title: str = ""
    start_index: int = Field(ge=0)
    end_index: int = Field(ge=0)
    text: str = ""


class SearchSource(StrictModel):
    url: str
    title: str = ""


class GenerationMessage(StrictModel):
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[NormalizedToolCall] = Field(default_factory=list)
    tool_results: list[NormalizedToolResult] = Field(default_factory=list)
    # 정확한 프로토콜 연속성은 비공개 상태로만 보존한다.
    _provider_content: list[dict] | None = PrivateAttr(default=None)
    _adapter_id: str | None = PrivateAttr(default=None)


class GenerationRequest(StrictModel):
    request_id: str
    research_id: str
    task_id: str | None = None
    role: str
    model_profile_id: str
    system_instructions: str = ""
    developer_instructions: str = ""
    input_text: str = ""
    messages: list[GenerationMessage] = Field(default_factory=list)
    tools: list[NormalizedTool] = Field(default_factory=list)
    hosted_tools: list[HostedWebSearchTool] = Field(default_factory=list, max_length=1)
    max_tool_calls: int | None = Field(default=None, ge=1, le=40)
    include: list[Literal["web_search_call.action.sources"]] = Field(default_factory=list)
    # 원장에 예약할 도구 문맥 입력 상한이며 제공사 payload에는 보내지 않는다.
    hosted_input_token_bound: int | None = Field(default=None, ge=1)
    tool_choice: str | dict | None = None
    structured_output_schema: dict | None = None
    native_schema_strict: bool = False
    explicit_parallel_tool_control: bool = False
    max_output_tokens: int = Field(default=2048, ge=1, le=32768)
    reasoning_policy: ReasoningPolicy = ReasoningPolicy.AUTO
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    stop: list[str] | None = Field(default=None, max_length=4)
    seed: int | None = None
    parallel_tool_calls: bool = False
    stream: bool = False
    timeout: float = Field(default=60, gt=0, le=120)
    metadata: dict[str, Any] = Field(default_factory=dict)
    budget_reservation_id: str | None = None
    data_egress_policy: Literal["none", "selected", "research"] = "none"
    store_preference: bool = False
    probe_capability: str | None = None

    @field_validator("stop")
    @classmethod
    def stop_not_empty(cls, value):
        if value is not None and any(not s or len(s) > 200 for s in value):
            raise ValueError("invalid stop sequence")
        return value


class GenerationUsage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    web_search_calls: int | None = Field(default=None, ge=0)
    provider_reported_cost: str | None = None
    provider_usage_raw_ref: str | None = None


class GenerationResult(StrictModel):
    response_id: str | None = None
    provider_request_id: str | None = None
    model_id: str
    model_revision: str | None = None
    status: Literal["COMPLETED", "REQUIRES_TOOL", "REFUSED", "INCOMPLETE"] = "COMPLETED"
    output_text: str = ""
    structured_output: Any = None
    tool_calls: list[NormalizedToolCall] = Field(default_factory=list)
    hosted_tool_calls: list[HostedToolCall] = Field(default_factory=list)
    citations: list[URLCitation] = Field(default_factory=list)
    search_sources: list[SearchSource] = Field(default_factory=list)
    finish_reason: str | None = None
    refusal: str | None = None
    usage: GenerationUsage = Field(default_factory=GenerationUsage)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    reasoning_metadata: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = Field(default=0, ge=0)
    raw_response_ref: str | None = None
    _provider_content: list[dict] | None = PrivateAttr(default=None)
    _adapter_id: str | None = PrivateAttr(default=None)

    def assistant_message(self):
        value = GenerationMessage(role="assistant", text=self.output_text, tool_calls=self.tool_calls)
        value._provider_content, value._adapter_id = self._provider_content, self._adapter_id
        return value


class StreamEvent(StrictModel):
    event: Literal["response_start", "text_delta", "tool_call_start", "tool_call_delta", "tool_call_end", "usage", "response_end", "error"]
    data: dict[str, Any] = Field(default_factory=dict)


class DiscoveredModel(StrictModel):
    model_id: str
    display_name: str | None = None
    provider_model_revision: str | None = None
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    capabilities: dict[str, CapabilityEvidence] = Field(default_factory=dict)
    reasoning_levels: list[ReasoningPolicy] = Field(default_factory=list)
    provider_settings: dict[str, Any] = Field(default_factory=dict)
    candidate_pricing: dict[str, Any] | None = None


class GenerationError(ModelProviderError):
    """고정된 공개 오류 코드만 사용하고 응답·헤더의 비밀을 넣지 않는다."""
    def __init__(self, code: str, *, retryable=False):
        super().__init__(code, retryable=retryable)
        self.http_status = None
        self.provider_request_id = None
        self.provider_error_code = None


def merge_evidence(current, incoming, *, explicit_override=False):
    """실제 검사 실패를 정적 규칙·메타데이터 추정으로 지우지 않는다."""
    result = dict(current)
    for name, item in incoming.items():
        if name not in CAPABILITIES:
            continue
        prior = result.get(name)
        if prior and prior.source == "LIVE_CAPABILITY_TEST" and prior.status != CapabilityStatus.SUPPORTED and item.source != "LIVE_CAPABILITY_TEST" and not explicit_override:
            continue
        result[name] = item
    return result
