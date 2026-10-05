"""실행 순서 검증에 사용하는 결정적 모의 제공사."""
from __future__ import annotations

from collections import deque

from pydantic import BaseModel

from .base import ModelRunResult


class FakeProvider:
    name = "fake"

    def __init__(self, replies: list[object]):
        self.replies = deque(replies)
        self.calls: list[dict] = []

    async def run_structured(self, *, role: str, instructions: str, input_text: str,
                             output_type: type[BaseModel], model: str,
                             metadata: dict | None = None) -> ModelRunResult:
        self.calls.append({"role": role, "model": model, "input_text": input_text,
                           "output_type": output_type.__name__})
        if not self.replies:
            raise AssertionError("fake provider reply queue exhausted")
        reply = self.replies.popleft()
        if callable(reply):
            reply = reply(self.calls[-1])
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, ModelRunResult):
            return reply
        return ModelRunResult(provider=self.name, model=model, output=reply,
                              request_count=1, input_tokens=10, output_tokens=10, latency_ms=1)
