import pytest

from htrsa.database import initialize
from htrsa.mock import MockAnalysisTool, MockExperimentCoordinator, MockManager, MockWorker
from htrsa.schemas import StagedResult
from htrsa.service import StateService


@pytest.fixture
def cycle(tmp_path):
    db = initialize(tmp_path / "test.sqlite")
    state = StateService(db)
    research_id = state.create_research("Study X and Y")
    parent = MockManager().create_contract(research_id, "Study X and Y")
    parent_task = state.issue_contract(parent)
    contract = MockExperimentCoordinator().create_contract(parent, parent_task)
    task_id = state.issue_contract(contract)
    worker = MockWorker()
    request = worker.make_request(contract, task_id, [1, 2, 3, 4], [2, 4, 6, 8])
    tool_result = MockAnalysisTool().run(request)
    result = worker.consume(contract, tool_result)
    state.record_execution(contract.contract_id, worker.role, request, tool_result)
    payload = StagedResult(agent_result=result, tool_request=request, tool_result=tool_result)
    yield db, state, research_id, contract, payload
    db.close()
