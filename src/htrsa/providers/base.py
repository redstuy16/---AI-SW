"""제공사와 독립적인 구조화 모델 실행."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class ModelRunResult:
    provider: str
    model: str
    output: BaseModel | dict | None
    request_count: int | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    latency_ms: float | None = None
    provider_run_id: str | None = None

    def usage(self) -> dict:
        return {key: getattr(self, key) for key in (
            "request_count", "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_tokens", "latency_ms", "provider_run_id")}


class ModelProviderError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ModelProvider(Protocol):
    name: str

    async def run_structured(self, *, role: str, instructions: str, input_text: str,
                             output_type: type[BaseModel], model: str,
                             metadata: dict | None = None) -> ModelRunResult: ...
