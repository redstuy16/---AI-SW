"""명시한 산출물의 누락을 완료로 처리하지 않도록 검사한다."""
import asyncio
from probe.research_report import requested_report_sections,missing_report_sections,ReportDraft
from probe.science_loop import _execute,ScienceDecision
from test_science_tool_authority import prepared

def test_only_explicit_output_list_is_checked():
    assert requested_report_sections('후속 연구가 필요할 수 있다.')==[]
    assert requested_report_sections('최종 산출물은 연구 계획, 결론, 후속 연구다. 계산도 검산한다.')==['연구 계획','결론','후속 연구']
    assert requested_report_sections('최종 산출물\n1. 연구 계획\n2. 결론')==['연구 계획','결론']

def test_missing_requested_sections_block_completion_before_writer(tmp_path):
    db,state,loop,rid,parent=prepared(tmp_path)
    draft=ReportDraft(summary='검산 완료',explanation='결론만 작성했다.')
    decision=ScienceDecision(action='COMPLETE',rationale='완료 판단',report_draft=draft)
    result=asyncio.run(_execute(loop,rid,0,decision,parent,None,'최종 산출물은 연구 계획, 결론, 후속 연구다.',[]))
    assert result['status']=='NEEDS_REVIEW' and result['missing']==['연구 계획','후속 연구']
    assert state.runtime_step(rid,'report-draft:1') is None
    assert missing_report_sections(ReportDraft(summary='완료',explanation='연구 계획\n결론\n후속 연구'),['연구 계획','결론','후속 연구'])==[]
    db.close()

def test_graph_or_table_accepts_an_actual_result_table():
    table=ReportDraft(summary='계산 표',results='| 지표 | 값 |\n|---|---|\n| 변화량 | 검산된 값 |',purpose='목적',explanation='배경',method='방법',conclusion='결론')
    assert missing_report_sections(table,['그래프 또는 표'])==[]
    assert missing_report_sections(ReportDraft(summary='아직 없음',explanation='그래프와 표를 만들지 않았다.'),['그래프 또는 표'])==['그래프 또는 표']

def test_pdf_blocks_preserve_values_and_separate_actual_table():
    from probe.report_composition import inquiry_blocks
    blocks=inquiry_blocks('### 결과\n| 지표 | 값 |\n|---|---|\n| 변화 | -0.5 °C |\n설명 <script>')
    assert blocks==[('heading','결과'),('table',(['지표','값'],[['변화','-0.5 °C']])),('paragraph','설명 <script>')]
