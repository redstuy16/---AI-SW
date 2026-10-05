"""과학 계획을 별도 작업 계약과 기존 도구 원장으로 실행한다."""
from __future__ import annotations

import time
from uuid import NAMESPACE_URL, uuid5

from .schemas import ToolRequest, ToolResult, utc_now
from .control_plane import ControlBoundary


class ScientificCalculationTool:
    version = '1.0'

    def __init__(self, state, contract_id, name):
        self.state, self.contract_id, self.name = state, contract_id, name

    def run(self, request):
        from .scientific_compute import CalculationPlan, save_calculation
        from .scientific_dataset_compute import DatasetCalculationPlan, save_dataset_calculation
        try:
            if self.name == 'science.calculate':
                value = save_calculation(self.state, request.research_id, self.contract_id,
                                         CalculationPlan.model_validate(request.args))
            else:
                value = save_dataset_calculation(self.state, request.research_id, self.contract_id,
                                                 DatasetCalculationPlan.model_validate(request.args))
        except (ValueError, ArithmeticError) as exc:
            value = {'status': 'NEEDS_REVIEW', 'reason': getattr(exc, 'code', str(exc)[:120])}
        return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name, result=value)


async def execute_science_plan(runtime, rid, iteration, decision, parent):
    from .science_loop import science_enabled
    if not science_enabled(runtime):
        raise ValueError('SCIENCE_TOOLS_DISABLED')
    runtime.state.science_tools_enabled = True
    names = {'CALCULATE': 'science.calculate', 'CALCULATE_DATASET': 'science.dataset',
             'FETCH_DATA': 'science.fetch_data', 'FETCH_SOURCE':'science.fetch_source'}
    name = names[decision.action]
    plan = {'CALCULATE': decision.calculation_plan, 'CALCULATE_DATASET': decision.dataset_calculation_plan,
            'FETCH_DATA': decision.data_plan, 'FETCH_SOURCE':decision.document_plan}[decision.action]
    worker, task = runtime._role_contract(rid, 'analysis_planner_worker',
        '검토된 과학 계획을 실행하고 실제 자료와 검산 결과를 기록합니다.', 'ScienceToolResult',
        parent_task_id=runtime.state.contract(parent.contract_id)[1],
        allowed_tools=[name], max_tool_calls=1, runtime_key=f'science:tool:{iteration}')
    args = plan.model_dump(mode='json')
    if name not in {'science.fetch_data','science.fetch_source'}:
        registry = runtime._registry(worker.contract_id)
        registry.register(ScientificCalculationTool(runtime.state, worker.contract_id, name))
        _, result = runtime._dispatch(registry, worker, task, name, args)
    else:
        from .climate_data import fetch_climate_data
        from .science_sources import fetch_science_sources
        from .research_design import check_tool, record_tool_outcome
        key = f'{worker.contract_id}:{name}:1'
        request = ToolRequest(request_id=f'TREQ-{uuid5(NAMESPACE_URL, key).hex}', research_id=rid,
            task_id=task, actor_id=worker.assigned_role, idempotency_key=key, tool_name=name, args=args)
        result = runtime.state.replay_tool(key, request)
        if result is None:
            if check_tool(runtime.state, worker, request):
                raise ValueError('RESEARCH_DESIGN_ACTION_BLOCKED')
            runtime._remaining_runtime(worker)
            if getattr(runtime, 'control_boundary', None):
                runtime.control_boundary()
            runtime.state.reserve_tool_request(worker.contract_id, request)
            started, clock = utc_now(), time.perf_counter()
            interrupted = None
            try:
                value = await (fetch_climate_data if name=='science.fetch_data' else fetch_science_sources)(runtime, rid, worker.contract_id, plan)
                result = ToolResult(ok=True, request_id=request.request_id, tool_name=name, result=value)
            except BaseException as exc:
                if isinstance(exc, (ControlBoundary, __import__('asyncio').CancelledError)) or not isinstance(exc, Exception):
                    interrupted = exc
                code = getattr(exc, 'code', 'SOURCE_FORMAT_INVALID')
                result = ToolResult(ok=False, request_id=request.request_id, tool_name=name, error=code,
                                    result={'status': 'NEEDS_REVIEW', 'reason': code})
            result = result.model_copy(update={'provenance': {'tool_name': name, 'tool_version': '1.0',
                'started_at': started.isoformat(), 'finished_at': utc_now().isoformat(),
                'latency_ms': (time.perf_counter()-clock)*1000, 'estimated_cost_usd': 0}})
            record_tool_outcome(runtime.state, request, result)
            runtime.state.record_execution(worker.contract_id, worker.assigned_role, request, result)
            runtime.state.finish_tool_request(request, result)
            if interrupted is not None:
                runtime.state.set_task_status(worker.contract_id, 'FAILED')
                raise interrupted
    runtime._complete_task(worker.contract_id)
    return result.result
