"""고정 모의 자료의 확보·선별·인용·보고서·측정값을 따로 평가한다."""
import json
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
import pytest
from probe.workbench import WorkbenchAPI
from probe.scholarly import NormalizedSource
from probe.literature import screen_source
from probe.final_report import _validate_literature_provenance
from probe.research_report import report_record
from probe.report_pdf import render_pdf
from probe.preflight import _source_fingerprint
from probe.schemas import utc_now
from test_free_search_recovery import free_rig, TITLE, ABSTRACT, QUESTION
from test_research_report_flow import run


def metric(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'rate': numerator / denominator if denominator else None}


def main():
    started = perf_counter()
    folder = ROOT / 'build/free-search/evaluation' / uuid4().hex
    cases = []
    settings = [('abstract', 'abstract', False, 'ok', 'READY'),
                ('public-pdf', 'pdf', True, 'ok', 'READY'),
                ('titles-only', 'pdf', False, 'ok', 'READY'),
                ('no-results', 'empty', True, 'ok', 'READY'),
                ('provider-429', 'rate', False, 'ok', 'READY'),
                ('report-repaired', 'abstract', False, 'length_then_ok', 'READY'),
                ('report-partial', 'abstract', False, 'always_length', 'PARTIAL')]
    for identity, mode, fulltext, report, expected in settings:
        target = folder / identity
        app = WorkbenchAPI(target / 'state.sqlite', target / 'workspace', launch=False)
        try:
            with pytest.MonkeyPatch.context() as patch:
                gateway, calls, requests = free_rig(app, patch, mode=mode, report=report)
                rid = run(app, gateway, fulltext_enabled=fulltext, question=QUESTION + ' 대표 음료 5종을 비교해 표로 작성해')
                saved = report_record(app.read._state, rid)
                assert saved['status'] == expected, saved
                summary = app.request('GET', f'/api/control/research/{rid}/execution-summary').body
                checks = []
                for raw in app.store.db.execute("SELECT * FROM evidence WHERE research_id=? AND status='VERIFIED' AND source_type='LITERATURE'", (rid,)):
                    source = app.store.db.execute('SELECT * FROM sources WHERE research_id=? AND source_id=?', (rid, raw['source_id'])).fetchone()
                    _validate_literature_provenance(dict(source), dict(raw), state=app.read._state)
                    checks.append({'evidence_id': raw['evidence_id'], 'text_field': raw['text_field'], 'passed': True})
                pdf = render_pdf(app.read._state, rid)['data']
                assert pdf.startswith(b'%PDF-')
                if identity == 'public-pdf':
                    (target / 'report.pdf').write_bytes(pdf)
                value = {'id': identity, 'research_id': rid, 'status': summary['status'], 'counts': summary['counts'],
                         'main_cause': summary['blocker'], 'additional_errors': summary['additional_errors'],
                         'report_status': saved['status'], 'research_completed': summary['status'] == 'COMPLETED', 'pdf_generated': True, 'report_attempts': len(saved['attempts']),
                         'model_calls': calls, 'actual_http_requests': len(requests), 'citation_checks': checks,
                         'requested_measurements': saved['requested_measurements'], 'passed': True}
                cases.append(value)
        finally:
            app.close()
    labels = [("독도의 이동 속도", '독도 지형의 이동 속도가 증가하였다.', 'IRRELEVANT'),
              ('Dental surface hardness', 'Carbonated drinks changed dental hardness with temperature.', 'IRRELEVANT'),
              ('Carbon footprint of beverages', 'CO2 emissions increased with shipment speed.', 'IRRELEVANT'),
              (TITLE, ABSTRACT, 'DIRECT'), (TITLE, None, 'INDIRECT'),
              ('CO2 degassing in carbonated water', 'CO2 release increased with temperature in carbonated water.', 'INDIRECT'),
              ('High-speed imaging of degassing kinetics of CO2–water mixtures',
               'The exsolution of gas molecules is studied. This study improves understanding of free gas bubbles in CO2–water mixtures.', 'INDIRECT')]
    relevance = [{'title': title, 'expected': expected,
                  'observed': screen_source('fixture', NormalizedSource(title=title, abstract=abstract, provider='scholarly.fake'), QUESTION).relevance}
                 for title, abstract, expected in labels]
    assert all(v['expected'] == v['observed'] for v in relevance)
    frozen = json.loads((ROOT / 'qa/fixtures/free_search_failure_case.json').read_text(encoding='utf-8'))
    historical = [{'title': item['title'], 'original_relevance': item['original_relevance'],
                   'expected': item['expected_relevance'], 'observed': screen_source('frozen',
                       NormalizedSource(title=item['title'], abstract=item['abstract'], provider='scholarly.crossref'), frozen['question']).relevance}
                  for item in frozen['sources']]
    assert all(v['expected'] == v['observed'] for v in historical)
    positive = [v for v in relevance if v['observed'] != 'IRRELEVANT']
    related = sum(v['counts']['relevant'] for v in cases)
    citations = sum(len(v['citation_checks']) for v in cases)
    result = {'execution': 'OFFLINE_FIXED_MOCK_PROVIDERS', 'completed_at': utc_now().isoformat(),
              'source_fingerprint': _source_fingerprint(), 'cases': cases, 'relevance_cases': relevance,
              'historical_failure_titles': historical,
              'metrics': {'readable_acquisition': metric(sum(v['counts']['readable'] for v in cases), related),
                          'relevance_precision': metric(sum(v['expected'] != 'IRRELEVANT' for v in positive), len(positive)),
                          'relevance_label_accuracy': metric(sum(v['expected'] == v['observed'] for v in relevance), len(relevance)),
                          'citation_validation': metric(citations, citations),
                          'ai_report_generation': metric(sum(v['report_status'] == 'READY' for v in cases), len(cases)),
                          'research_with_verified_evidence': metric(sum(v['research_completed'] and v['counts']['verified'] > 0 for v in cases), len(cases)),
                          'pdf_generation_including_partial': metric(sum(v['pdf_generated'] for v in cases), len(cases)),
                          'requested_numeric_values': metric(sum(v['requested_measurements']['verified'] for v in cases), sum(v['requested_measurements']['requested'] for v in cases))},
              'duration_sec': round(perf_counter() - started, 3), 'passed': True, 'paid_calls': 0,
              'live_agent_efficacy': 'NOT_VALIDATED', 'success_rate_improvement': 'NOT_VALIDATED',
              'limitation': '작고 공개된 고정 모의 사례의 정확성 검사다. 실제 연구 성공률이나 비교 우위를 검증하지 않는다.'}
    data = (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode('utf-8', errors='strict')
    (folder / 'result.json').write_bytes(data)
    print(json.dumps({'passed': True, 'cases': len(cases), 'metrics': result['metrics'], 'duration_sec': result['duration_sec'],
                      'output': str(folder / 'result.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
