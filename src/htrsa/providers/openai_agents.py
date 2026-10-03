"""OpenAI Agents SDK 어댑터. SDK 객체는 이 경계를 넘지 않는다."""
from __future__ import annotations

import asyncio
import inspect
import os
import time

# SDK 초기화 전에 외부 추적을 끈다.
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

from pydantic import BaseModel, ValidationError

from .base import ModelProviderError, ModelRunResult


def normalize_openai_error(exc: Exception) -> ModelProviderError:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ModelProviderError("PROVIDER_TIMEOUT", retryable=True)
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    if status == 429 or "ratelimit" in name:
        return ModelProviderError("PROVIDER_RATE_LIMIT", retryable=True)
    if status in {401, 403} or "authentication" in name or "permission" in name:
        return ModelProviderError("PROVIDER_AUTH")
    if isinstance(exc, ValidationError) or "modelbehavior" in name or "output" in name:
        return ModelProviderError("STRUCTURED_OUTPUT_INVALID", retryable=True)
    return ModelProviderError("PROVIDER_ERROR", retryable=True)


class OpenAIAgentsProvider:
    name = "openai"

    def __init__(self, timeout_sec: float = 60):
        self.timeout_sec = timeout_sec

    async def run_structured(self, *, role: str, instructions: str, input_text: str,
                             output_type: type[BaseModel], model: str,
                             metadata: dict | None = None) -> ModelRunResult:
        if not model:
            raise ModelProviderError("MODEL_NOT_CONFIGURED")
        try:
            from agents import Agent, Runner, RunConfig, set_tracing_disabled
        except ImportError as exc:
            raise ModelProviderError("SDK_NOT_INSTALLED") from exc
        started = time.perf_counter()
        try:
            set_tracing_disabled(True)
            agent = Agent(name=role, instructions=instructions, model=model, output_type=output_type)
            # 기존 인자 2개의 실행기 호출 형태를 유지한다.
            # 전송 전 호출 형태를 결정하며 TypeError로 유료 요청을 반복하지 않는다.
            parameters = inspect.signature(Runner.run).parameters
            options = {}
            if "run_config" in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                options["run_config"] = RunConfig(tracing_disabled=True, trace_include_sensitive_data=False)
            result = await asyncio.wait_for(Runner.run(agent, input_text, **options), timeout=self.timeout_sec)
            output = output_type.model_validate(result.final_output)
        except Exception as exc:
            raise normalize_openai_error(exc) from exc
        usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        return ModelRunResult(
            provider=self.name, model=model, output=output,
            request_count=getattr(usage, "requests", None),
            input_tokens=getattr(usage, "input_tokens", None),
            cached_input_tokens=getattr(input_details, "cached_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            reasoning_tokens=getattr(output_details, "reasoning_tokens", None),
            latency_ms=(time.perf_counter() - started) * 1000,
            provider_run_id=getattr(result, "last_response_id", None),
        )
