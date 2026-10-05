"""공식 계산의 실행 차단·독립 검산·파일 무결성과 원래 입력 보존 검사."""
import math
import pytest
from probe.control_plane import ControlError
from probe.scientific_compute import CalculationPlan,compute,evaluate,save_calculation,checked_calculations
from probe.research_report import ReportDraft,ReportNumber,validate_draft,report_inputs

def test_equilibrium_arithmetic():
    result=compute(CalculationPlan(inputs={'S':1361,'A':.3,'sigma':5.6704e-8},calculations=[
        {'name':'flux','expression':'S*(1-A)/4','unit':'W/m²'},
        {'name':'kelvin','expression':'(flux/sigma)**0.25','unit':'K'}]))
    assert result['results'][0]['value']==pytest.approx(238.175)
    assert result['results'][1]['value']==pytest.approx(254.5778529950979)
    assert result['verification']=='FLOAT_AND_DECIMAL_AGREE'


def test_mathematical_lambda_is_an_input_not_python_code():
    result=compute(CalculationPlan(inputs={'lambda':.8,'forcing':2.1692383283786794},calculations=[{'name':'delta','expression':'lambda*forcing','unit':'K'}]))
    assert result['results'][0]['value']==pytest.approx(1.7353906627029436)
    with pytest.raises(ValueError,match='EXPRESSION_SYNTAX_INVALID'):evaluate('x +',{'x':1})


def test_long_goal_becomes_topic_but_private_goal_stays_blocked():
    from probe.search_policy import public_query,private_query
    topic='1990년 이후 CO₂ 증가와 지구 평균기온 관계'
    snapshot={'settings_version':2,'question':topic+'. '+('자료를 정렬하고 검산하라. '*80)}
    assert public_query(snapshot)==topic.replace('₂','2')
    snapshot['question']+=' confidential'
    assert private_query(public_query(snapshot))

@pytest.mark.parametrize('expression',["__import__('os').system('echo BAD')",'().__class__','[1]*100000000','2**999','open("secret")'])
def test_no_code_execution(expression):
    with pytest.raises((ValueError,TypeError)):evaluate(expression,{})

def test_artifact_tamper_is_blocked(cycle):
    db,state,rid,contract,payload=cycle
    from probe.storage import Workspace,ArtifactIntegrityError
    from pathlib import Path
    state.workspace=Workspace(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent/'workspace')
    state.workspace.prepare(rid)
    result=save_calculation(state,rid,contract.contract_id,CalculationPlan(inputs={'x':2},calculations=[{'name':'square','expression':'x*x','unit':'무차원'}]))
    assert checked_calculations(state,rid)[0]['results'][0]['value']==4
    artifact=state.file_artifact(result['artifact_id'],rid);state.workspace.path(rid,artifact['relative_path']).write_bytes(b'{}')
    with pytest.raises(ArtifactIntegrityError):checked_calculations(state,rid)

def test_provided_input_survives_question_summary(cycle):
    db,state,rid,contract,payload=cycle
    db.execute('UPDATE research_runs SET goal=?,research_question=? WHERE research_id=?',('원래 입력 1361 W/m², 알베도 0.30','요약된 질문',rid))
    draft=ReportDraft(summary='입력 알베도 0.3',numeric_mentions=[ReportNumber(text='0.3',kind='provided',location='/summary')])
    assert validate_draft(state,rid,draft)['summary']==draft.summary
    invalid=ReportDraft(summary='입력 알베도 0.4',numeric_mentions=[ReportNumber(text='0.4',kind='provided',location='/summary')])
    with pytest.raises(ControlError):validate_draft(state,rid,invalid)


def test_input_scientific_notation_is_same_value():
    from probe.research_report import _input_numbers
    assert _input_numbers('5.6704×10^-8 W m^-2 K^-4')==_input_numbers('5.6704e-8 W m⁻² K⁻⁴')
    assert _input_numbers('5.6704×10⁻⁸ W m⁻² K⁻⁴')==_input_numbers('5.6704e-8')


def test_rounded_calculation_requires_correct_field_value_and_unit():
    from probe.research_report import _calculated_display
    values=[{'artifact_id':'ART-a','field':'temp','value':-18.572147,'unit':'°C','sentence':'검산된 계산 temp: -18.572147 °C.'}]
    def check(text,field='temp'):
        return _calculated_display(ReportNumber(text=text,kind='calculated',artifact_id='ART-a',field=field),values)
    assert check('−18.57 °C')
    assert not check('-18.50 °C')
    assert not check('-18.57 K')
    assert not check('-18.57 °C','other')
    energy=[{'artifact_id':'ART-a','field':'energy','value':2.9527995e22,'unit':'J','sentence':'검산된 계산 energy: 2.9527995e22 J.'}]
    assert _calculated_display(ReportNumber(text='2.95×10²² J',kind='calculated',artifact_id='ART-a',field='energy'),energy)


def test_display_binding_does_not_change_or_fabricate_numbers(cycle):
    from probe.research_report import bind_calculation_mentions
    from probe.storage import Workspace
    from pathlib import Path
    db,state,rid,contract,payload=cycle
    state.workspace=Workspace(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent/'workspace');state.workspace.prepare(rid)
    save_calculation(state,rid,contract.contract_id,CalculationPlan(inputs={'x':-18.572147},calculations=[{'name':'temp','expression':'x','unit':'°C'}]))
    for text,valid in [('약 −18.57 °C',True),('약 −18.57 °C입니다.',True),('약 −17.57 °C',False)]:
        draft=bind_calculation_mentions(state,rid,ReportDraft(summary=text))
        assert draft.summary==text
        if valid:assert validate_draft(state,rid,draft)['summary']==text
        else:
            with pytest.raises(ControlError):validate_draft(state,rid,draft)
