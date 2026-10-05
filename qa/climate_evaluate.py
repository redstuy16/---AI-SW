"""문제별 실제 저장 결과를 별도 기준값으로 채점하고 원장은 읽기만 한다."""
import json,math,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'qa')]
from probe.research_report import ReportDraft,requested_report_sections,missing_report_sections

root=ROOT/'build/climate-live';evaluation=json.loads((root/'evaluation_final.json').read_text(encoding='utf-8'))
expected={
 1:{'absorbed_flux':238.175,'equilibrium_temperature_K':(238.175/5.6704e-8)**.25,'equilibrium_temperature_C':(238.175/5.6704e-8)**.25-273.15},
 2:{'absorbed_heat_J':2.9527995e22,'absorbed_heat_10_22_J':2.9527995,'accumulation_time_years':2.9527995e22/(.70*5.10e14)/(365*24*3600)},
 3:{'radiative_forcing':5.35*math.log(1.5),'equilibrium_temperature_change':.80*5.35*math.log(1.5)},
 4:{'simple_trend_intercept':-.043,'simple_trend_coef_t':.0218181818181818,'adjusted_trend_intercept':0,'adjusted_trend_coef_t':.018,'adjusted_trend_coef_ENSO':.12,'adjusted_trend_coef_V':-.15},
}
checks=[]
for case in evaluation['cases']:
    folder=root/case['folder'];result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
    values={value['field']:value for observation in result['observations'] for value in observation['result'].get('results',[])}
    if case['case']<5:
        numbers=[{'field':key,'observed':values.get(key,{}).get('value'),'expected':value,'passed':key in values and math.isclose(values[key]['value'],value,rel_tol=1e-9,abs_tol=1e-10)} for key,value in expected[case['case']].items()]
        checks.append({'case':case['case'],'numeric_grade':'PASS' if all(v['passed'] for v in numbers) else 'FAIL','checks':numbers})
    else:
        recovered=json.loads((folder/'report-recovery.json').read_text(encoding='utf-8'));draft=ReportDraft.model_validate(recovered['report']['draft'])
        question=json.loads((folder/'attempt.json').read_text(encoding='utf-8'))
        manifest=json.loads((ROOT/'qa/climate_questions.json').read_text(encoding='utf-8'))[4]
        checks.append({'case':5,'runtime_status':result['status'],'report_status':recovered['status'],
            'required_sections':requested_report_sections(manifest['question']),'missing_sections':missing_report_sections(draft,requested_report_sections(manifest['question'])),
            'rubric_method':'수치·출처·실행 기록의 자동 검사와 보고서 내용의 수동 검토를 분리한다.'})
text=json.dumps(checks,ensure_ascii=False,indent=2);text.encode('utf-8',errors='strict');(root/'grading.json').write_text(text,encoding='utf-8')
print(json.dumps([{'case':r['case'],'numeric_grade':r.get('numeric_grade'),'missing_sections':r.get('missing_sections')} for r in checks],ensure_ascii=False))
