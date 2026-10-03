"""실제 인터페이스를 사용하는 결정적 모의 Agent와 도구."""
from __future__ import annotations

from math import isfinite
from typing import Protocol

from .schemas import (
    AcceptanceCheck, AgentResult, ContextRef, RefType, ResearchContract,
    ToolRequest, ToolResult, new_id, utc_now,
)


class Agent(Protocol):
    role: str


class Tool(Protocol):
    name: str

    def run(self, request: ToolRequest) -> ToolResult: ...


class MockManager:
    role = "manager"

    def create_contract(self, research_id: str, goal: str) -> ResearchContract:
        return ResearchContract(
            contract_id=new_id("CTR"), research_id=research_id, task_type="coordinate_experiment",
            issued_by=self.role, assigned_role="experiment_coordinator", objective=goal,
            output_schema_id="research_contract.v1",
        )


class MockExperimentCoordinator:
    role = "experiment_coordinator"

    def create_contract(self, parent: ResearchContract, parent_task_id: str) -> ResearchContract:
        if parent.assigned_role != self.role:
            raise ValueError("contract is not assigned to coordinator")
        return ResearchContract(
            contract_id=new_id("CTR"), research_id=parent.research_id,
            parent_task_id=parent_task_id, task_type="analyze_association",
            issued_by=self.role, assigned_role="analysis_worker", objective=parent.objective,
            inputs=[ContextRef(type=RefType.contract, id=parent.contract_id)],
            allowed_tools=["mock.analysis"],
            acceptance=[AcceptanceCheck(id="tool_ok", kind="automatic"), AcceptanceCheck(id="result_present", kind="automatic")],
            output_schema_id="agent_result.v1",
        )


class MockAnalysisTool:
    name = "mock.analysis"
    version = "1.0"

    def run(self, request: ToolRequest) -> ToolResult:
        started = utc_now().isoformat()
        try:
            if request.tool_name != self.name:
                raise ValueError("wrong tool name")
            x, y = request.args["x"], request.args["y"]
            if not isinstance(x, list) or not isinstance(y, list) or not x or len(x) != len(y):
                raise ValueError("x and y must be nonempty equal-length lists")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in x + y):
                raise ValueError("all values must be finite numbers")
            result = {"n": len(x), "x_mean": sum(x) / len(x), "y_mean": sum(y) / len(y)}
            ok, error = True, None
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            result, ok, error = {}, False, str(exc)
        return ToolResult(
            ok=ok, request_id=request.request_id, tool_name=request.tool_name,
            result=result, error=error,
            provenance={"tool_name": self.name, "tool_version": self.version,
                        "started_at": started, "finished_at": utc_now().isoformat()},
        )


class MockWorker:
    role = "analysis_worker"

    def make_request(self, contract: ResearchContract, task_id: str, x: list[float], y: list[float]) -> ToolRequest:
        if contract.assigned_role != self.role or MockAnalysisTool.name not in contract.allowed_tools:
            raise ValueError("worker lacks an allowed analysis tool")
        request_id = new_id("TREQ")
        return ToolRequest(
            request_id=request_id, research_id=contract.research_id, task_id=task_id,
            actor_id=self.role, idempotency_key=request_id,
            tool_name=MockAnalysisTool.name, args={"x": x, "y": y},
        )

    def consume(self, contract: ResearchContract, tool_result: ToolResult) -> AgentResult:
        return AgentResult(
            result_id=new_id("ART"), research_id=contract.research_id,
            contract_id=contract.contract_id, actor_role=self.role,
            status="completed" if tool_result.ok else "failed", output=tool_result.result,
            confidence=1.0 if tool_result.ok else 0.0,
            provenance={"tool_request_id": tool_result.request_id},
        )
