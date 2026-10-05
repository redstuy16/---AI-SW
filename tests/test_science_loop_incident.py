"""실제 반복 실패의 수치 표기·번역·불필요한 검색·재전송 회귀 검사."""
import asyncio
from pathlib import Path

import pytest

from probe.control_plane import ControlError
from probe.research_report import (ReportDraft, ReportNumber, bind_calculation_mentions,
    validate_draft, _input_numbers, _quoted_number_display)
from probe.scientific_compute import CalculationPlan, save_calculation
from probe.search_policy import self_contained_calculation, decision
from probe.storage import Workspace
from test_science_loop import runtime, complete


@pytest.mark.parametrize('notation', [
    r'5.6704\times10^{-8}Wm^{-2}K^{-4}',
    r'5.6704\cdot10^{-8}\,\mathrm{W}\,\mathrm{m}^{-2}\,\mathrm{K}^{-4}',
    '5.6704×10⁻⁸ W·m⁻²·K⁻⁴', '5.6704e-8 W m^-2 K^-4',
])
def test_input_scientific_notation_is_value_not_display(notation):
    assert _input_numbers(notation) == _input_numbers('5.6704e-8')
    assert _input_numbers(notation) != _input_numbers('5.6704e-7')


def test_subtraction_does_not_turn_albedo_into_negative_input():
    assert _input_numbers('1−0.30') == _input_numbers('1 - 0.30')
    assert _input_numbers('-18.57 °C') == _input_numbers('−18.57 °C')
    assert _input_numbers('-18.57 °C') != _input_numbers('18.57 °C')


def test_translated_literature_number_keeps_value_and_temperature_unit():
    quote = 'The temperature is about 15 degrees Celsius.'
    assert _quoted_number_display('약 15 °C', quote)
    assert not _quoted_number_display('약 16 °C', quote)
    assert not _quoted_number_display('15 K', quote)


def test_valid_formula_and_repeated_given_inputs_bind_without_rewriting(cycle):
    db, state, rid, contract, _ = cycle
    state.workspace = Workspace(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent/'workspace')
    state.workspace.prepare(rid)
    original = r'S=1361 W/m^2, A=0.30, sigma=5.6704\times10^{-8}Wm^{-2}K^{-4}를 가정하고 계산하라.'
    db.execute('UPDATE research_runs SET goal=?,research_question=? WHERE research_id=?', (original, original, rid))
    save_calculation(state, rid, contract.contract_id, CalculationPlan(inputs={'S':1361,'A':.3,'sigma':5.6704e-8},
        calculations=[{'name':'flux','expression':'S*(1-A)/4','unit':'W/m²'}]))
    draft = ReportDraft(summary='구형 지구의 평균 복사량을 계산했다.',
        purpose='주어진 조건으로 계산한다.', explanation='구형 지구가 받는 복사를 전체 표면에 평균한다.',
        method='주어진 S=1361 W/m², A=0.30, σ=5.6704×10⁻⁸ W m⁻² K⁻⁴를 사용한다.',
        results='흡수 복사량 S(1−A)/4 = 1361 W/m² × (1−0.30)/4이다. 계산값은 238.175 W/m²이다.',
        conclusion='구형 지구의 면적비와 알베도를 반영한 계산이다.',
        numeric_mentions=[
            ReportNumber(text='S=1361 W/m²',kind='provided',location='/method'),
            ReportNumber(text='A=0.30',kind='provided',location='/method'),
            ReportNumber(text='σ=5.6704×10⁻⁸ W m⁻² K⁻⁴',kind='provided',location='/method')])
    bound = bind_calculation_mentions(state, rid, draft)
    checked = validate_draft(state, rid, bound)
    assert checked['results'] == draft.results
    invalid = bind_calculation_mentions(state, rid, bound.model_copy(
        update={'results': draft.results.replace('238.175', '999.175')}))
    with pytest.raises(ControlError, match='REPORT_UNPROVEN_NUMBER'):
        validate_draft(state, rid, invalid)


def test_auto_given_calculation_skips_initial_search_and_finishes(tmp_path):
    db, _, provider, loop = runtime(tmp_path, [complete()])
    loop.science_settings.update(search_policy='AUTO')
    calls=[]
    async def acquire(queries=None):
        calls.append(queries)
        return False
    loop.evidence_acquisition = acquire
    result = asyncio.run(loop.run('S=1361, A=0.30이라고 가정하고 평균 복사량을 계산하라.', None))
    assert result['stop_reason'] == 'SCIENCE_INQUIRY_COMPLETED'
    assert calls == [] and len(provider.calls) == 1
    assert loop.state.runtime_step(result['research_id'], 'science:initial_search')['output']['reason'] == 'SELF_CONTAINED_CALCULATION'
    db.close()


def test_explicit_literature_and_long_observation_research_keep_search():
    base={'execution_mode':'SCIENCE_AUTO','search_policy':'AUTO','question':'S=1361, A=0.30을 가정하고 계산하라.'}
    assert self_contained_calculation(base)
    assert decision(base) == 'SEARCH_NOT_NEEDED'
    assert not self_contained_calculation({**base,'search_required':True})
    assert not self_contained_calculation({**base,'public_search_query':'greenhouse effect'})
    assert not self_contained_calculation({**base,'question':'1990년 이후 CO2와 기온 자료를 직접 찾아 회귀 분석하고 출처를 기록하라.'})


def test_repeated_search_is_not_dispatched_twice(tmp_path):
    search={'action':'SEARCH','rationale':'문헌을 찾습니다.','search_queries':['Photosynthesis   Evidence']}
    second={**search,'search_queries':['photosynthesis evidence']}
    db, _, provider, loop = runtime(tmp_path, [search, second, complete()])
    calls=[]
    async def acquire(queries=None):
        calls.append(queries)
        return False
    loop.evidence_acquisition = acquire
    result = asyncio.run(loop.run('광합성 원리를 확인하라.', None))
    assert result['stop_reason'] == 'SCIENCE_INQUIRY_COMPLETED'
    assert len(calls) == 2  # 최초 수집과 서로 다른 첫 검색만 실행한다.
    assert result['observations'][1]['result']['reason'] == 'SEARCH_ALREADY_ATTEMPTED'
    assert len(provider.calls) == 3
    db.close()
