"""등록된 CSV의 회귀·상관·추세를 서로 다른 계산으로 검산한다."""
from __future__ import annotations
import csv, hashlib, io, json, math
from typing import Literal
import numpy as np
from pydantic import Field
from .schemas import StrictModel, new_id
from .database import to_json
from .control_plane import ControlError

class DatasetCalculationPlan(StrictModel):
    dataset_id: str
    name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,30}$')
    operation: Literal['OLS','CORRELATION','SUMMARY']
    target: str = Field(min_length=1,max_length=80)
    predictors: list[str] = Field(default_factory=list,max_length=6)
    predictor_units: dict[str,str] = Field(default_factory=dict,max_length=6)
    transform: Literal['none','difference','detrend'] = 'none'
    year_min: int | None = None
    year_max: int | None = None
    target_unit: str = Field(default='무차원',min_length=1,max_length=40)

def dataset_values(state,rid,plan):
    if len(set(plan.predictors)) != len(plan.predictors) or plan.target in plan.predictors:
        raise ControlError('DATASET_VARIABLES_INVALID')
    from .climate_data import checked_climate_dataset
    checked_climate_dataset(state,rid,plan.dataset_id)
    record=state.dataset_record(plan.dataset_id,rid)
    data=state.workspace.path(rid,record['stored_path']).read_bytes()
    if len(data)>10000000 or len(data)!=record['size_bytes']:raise ControlError('DATASET_CHANGED')
    rows=list(csv.DictReader(io.StringIO(data.decode('utf-8-sig',errors='strict'))))
    cols=list(dict.fromkeys([plan.target,*plan.predictors]))
    usable=[];years=[];missing=0
    for row in rows:
        year=float(row['year']) if 'year' in row else None
        if year is not None and ((plan.year_min is not None and year<plan.year_min) or (plan.year_max is not None and year>plan.year_max)):continue
        if any(not row.get(c,'').strip() for c in cols):missing+=1;continue
        values=[float(row[c]) for c in cols]
        if any(not math.isfinite(x) for x in values):raise ControlError('NONFINITE_DATA')
        usable.append(values);years.append(year)
    if not 3<=len(usable)<=100000:raise ControlError('INSUFFICIENT_NUMERIC_DATA')
    values=np.asarray(usable,dtype=float)
    if plan.transform=='difference':
        if years[0] is not None and any(b-a!=1 for a,b in zip(years,years[1:])):raise ControlError('NONCONTIGUOUS_YEARS')
        values=np.diff(values,axis=0);years=years[1:]
    elif plan.transform=='detrend':
        time=np.arange(len(values),dtype=float)
        design=np.column_stack([np.ones(len(time)),time])
        values=values-design@np.linalg.lstsq(design,values,rcond=None)[0]
    return record,values,cols,years,missing

def compute_dataset(state,rid,plan):
    from scipy import stats
    record,values,cols,years,missing=dataset_values(state,rid,plan)
    y=values[:,0];n=len(y);numbers=[]
    def add(field,value,unit):
        value=float(value)
        if not math.isfinite(value):raise ControlError('NONFINITE_RESULT')
        field=plan.name+'_'+field
        numbers.append({'field':field,'value':value,'unit':unit,'sentence':f'검산된 계산 {field}: {value:.12g} {unit}.'})
    add('n',n,'개')
    if years[0] is not None:
        add('first_year',years[0],'년');add('last_year',years[-1],'년')
    if plan.operation=='OLS':
        if not plan.predictors or plan.target in plan.predictors:raise ControlError('REGRESSION_VARIABLES_INVALID')
        x=values[:,1:];design=np.column_stack([np.ones(n),x])
        if n<=design.shape[1] or np.linalg.matrix_rank(design)!=design.shape[1]:raise ControlError('RANK_DEFICIENT')
        coef=np.linalg.lstsq(design,y,rcond=None)[0]
        q,r=np.linalg.qr(design,mode='reduced');check=np.linalg.solve(r,q.T@y)
        if not np.allclose(coef,check,rtol=1e-8,atol=1e-10):raise ControlError('REGRESSION_CHECK_FAILED')
        add('intercept',coef[0],plan.target_unit)
        for field,value in zip(plan.predictors,coef[1:]):add('coef_'+field,value,plan.target_unit+'/'+plan.predictor_units.get(field,'년' if field in {'year','t'} else '단위'))
        residual=y-design@coef;sst=sum((v-math.fsum(y)/n)**2 for v in y)
        if sst<=0:raise ControlError('CONSTANT_TARGET')
        add('r_squared',1-float(residual@residual)/sst,'무차원');add('rmse',np.sqrt(np.mean(residual**2)),plan.target_unit)
        # 잔차와 설계행렬의 직교성도 독립적인 적합 조건으로 확인한다.
        if not np.allclose(design.T@residual,0,atol=1e-5):raise ControlError('REGRESSION_RESIDUAL_FAILED')
        verification='SVD_QR_AND_RESIDUAL_AGREE'
    elif plan.operation=='CORRELATION':
        if len(plan.predictors)!=1:raise ControlError('CORRELATION_VARIABLES_INVALID')
        x=values[:,1]
        if np.std(x)==0 or np.std(y)==0:raise ControlError('CONSTANT_VARIABLE')
        first=float(np.corrcoef(x,y)[0,1]);mx=math.fsum(x)/n;my=math.fsum(y)/n
        second=math.fsum((a-mx)*(b-my) for a,b in zip(x,y))/math.sqrt(math.fsum((a-mx)**2 for a in x)*math.fsum((b-my)**2 for b in y))
        if not math.isclose(first,second,abs_tol=1e-10):raise ControlError('CORRELATION_CHECK_FAILED')
        add('pearson',first,'무차원');add('spearman',stats.spearmanr(x,y).statistic,'무차원')
        verification='NUMPY_AND_CENTERED_SUM_AGREE'
    else:
        add('first_value',y[0],plan.target_unit);add('last_value',y[-1],plan.target_unit);add('change',y[-1]-y[0],plan.target_unit)
        add('mean',math.fsum(y)/n,plan.target_unit)
        verification='ENDPOINTS_AND_FSUM'
    return {'plan':plan.model_dump(mode='json'),'dataset_sha256':record['sha256'],'results':numbers,
            'verification':verification,'missing_rows':missing,'transform':plan.transform,
            'limitations':['통계적 적합만 검산하며 인과관계를 증명하지 않습니다.','단위는 분석 계획의 선언값입니다.']}

def save_dataset_calculation(state,rid,contract_id,plan):
    value=compute_dataset(state,rid,plan);aid=new_id('ART');relative=f'results/{aid}-statistics.json'
    data=(to_json(value)+'\n').encode('utf-8',errors='strict');state.workspace.path(rid,relative).write_bytes(data)
    state.register_file_artifact(aid,rid,contract_id,'SCIENTIFIC_STATISTICS',relative,'tool',contract_id,hashlib.sha256(data).hexdigest())
    return {'status':'VERIFIED','artifact_id':aid,**value}

def checked_dataset_calculations(state,rid):
    records=[]
    for row in state._db.execute("SELECT artifact_id FROM artifacts WHERE research_id=? AND artifact_type='SCIENTIFIC_STATISTICS' AND status NOT IN ('INVALIDATED','SUPERSEDED') ORDER BY rowid",(rid,)):
        artifact=state.file_artifact(row[0],rid);value=json.loads(state.workspace.path(rid,artifact['relative_path']).read_text(encoding='utf-8'))
        expected=compute_dataset(state,rid,DatasetCalculationPlan.model_validate(value['plan']))
        if value!=expected:raise ControlError('STATISTICS_TAMPERED')
        records.append({'artifact_id':row[0],'sha256':artifact['sha256'],**value})
    return records
