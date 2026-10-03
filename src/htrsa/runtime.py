"""네트워크와 모델 호출이 없는 첫 결정적 실행 경로."""
from __future__ import annotations

import argparse
from pathlib import Path

from .database import initialize
from .mock import MockAnalysisTool, MockExperimentCoordinator, MockManager, MockWorker
from .schemas import StagedResult
from .service import StateService


def run_cycle(database_path: str | Path) -> dict:
    db = initialize(database_path)
    try:
        state = StateService(db)
        research_id = state.create_research("Determine whether variable X is associated with variable Y.")
        manager_contract = MockManager().create_contract(research_id, "Analyze X and Y")
        manager_task = state.issue_contract(manager_contract)
        worker_contract = MockExperimentCoordinator().create_contract(manager_contract, manager_task)
        worker_task = state.issue_contract(worker_contract)
        worker = MockWorker()
        request = worker.make_request(worker_contract, worker_task, [1, 2, 3, 4], [2, 4, 6, 8])
        tool_result = MockAnalysisTool().run(request)
        result = worker.consume(worker_contract, tool_result)
        state.record_execution(worker_contract.contract_id, worker.role, request, tool_result)
        mutation_id = state.stage(StagedResult(agent_result=result, tool_request=request, tool_result=tool_result))
        if state.artifact(result.result_id) is not None:
            raise RuntimeError("검증 전 결과가 확정 상태에 포함됐습니다.")
        verification = state.verify(mutation_id)
        if verification.verdict.value != "PASS":
            raise RuntimeError(verification.errors)
        version = state.commit(mutation_id)
        return {"research_id": research_id, "mutation_id": mutation_id, "artifact_id": result.result_id,
                "verdict": verification.verdict.value, "state_version": version,
                "canonical_result": state.artifact(result.result_id)}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    from .database import to_json
    print(to_json(run_cycle(args.database)))


if __name__ == "__main__":
    main()
