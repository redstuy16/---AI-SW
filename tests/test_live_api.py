"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
import os

import pytest
from pydantic import BaseModel

from htrsa.providers.openai_agents import OpenAIAgentsProvider


class SmokeOutput(BaseModel):
    status: str


@pytest.mark.live_api
def test_openai_agents_sdk_structured_smoke():
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("HTRSA_MANAGER_MODEL"):
        pytest.skip("OPENAI_API_KEY and HTRSA_MANAGER_MODEL are required")
    output = asyncio.run(OpenAIAgentsProvider(timeout_sec=30).run_structured(
        role="manager", instructions="Return structured status exactly ready.",
        input_text="Return ready.", output_type=SmokeOutput, model=os.environ["HTRSA_MANAGER_MODEL"]))
    assert output.output.status == "ready"
    assert output.request_count is not None and output.request_count <= 2
    assert output.input_tokens is not None and output.output_tokens is not None
    assert output.latency_ms is not None and output.latency_ms > 0
    assert os.environ["OPENAI_API_KEY"] not in repr(output)
