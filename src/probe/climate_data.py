"""기후 기관의 공개 연도 자료를 실제로 가져와 정의와 해시를 보존한다."""
from __future__ import annotations
import csv, hashlib, io, json, math
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field
from .schemas import StrictModel, new_id, utc_now
from .database import to_json
from .control_plane import ControlError
from .real_schemas import DatasetRecord

CATALOG={
 'noaa_co2':{'url':'https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_gl.txt','agency':'NOAA GML','variable':'co2_ppm','unit':'ppm','definition':'건조 공기의 전 지구 연평균 CO₂ 몰분율','baseline':'절대 농도'},
 'nasa_gistemp':{'url':'https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.csv','agency':'NASA GISS','variable':'nasa_temp','unit':'°C','definition':'GISTEMP v4 전 지구 육지·해양 연평균 온도편차 J-D','baseline':'1951–1980'},
 'hadcrut5':{'url':'https://www.metoffice.gov.uk/hadobs/hadcrut5/data/HadCRUT.5.2.0.0/analysis/diagnostics/HadCRUT.5.2.0.0.analysis.summary_series.global.annual.csv','agency':'Met Office / UEA CRU','variable':'hadcrut_temp','unit':'°C','definition':'HadCRUT.5.2.0.0 analysis 전 지구 연평균 온도편차','baseline':'1961–1990'},
 'noaa_oni':{'url':'https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt','agency':'NOAA CPC','variable':'enso','unit':'°C','definition':'Niño 3.4 해수면 온도편차 ONI의 중앙 월 연도별 중첩 계절 평균','baseline':'중심 30년 기준, 5년마다 갱신'}
}

class ClimateDataPlan(StrictModel):
    sources:list[Literal['noaa_co2','nasa_gistemp','hadcrut5','noaa_oni']]=Field(min_length=1,max_length=4)
    year_min:int=Field(default=1990,ge=1900,le=2100)
    year_max:int|None=Field(default=None,ge=1900,le=2100)

def parse_series(kind,data):
    text=data.decode('utf-8-sig',errors='strict');points={}
    def add(year,value):
        year=int(year);value=float(value)
        if year in points or not math.isfinite(value):raise ControlError('SOURCE_DATA_INVALID')
        points[year]=value
    if kind=='noaa_co2':
        for line in text.splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                row=line.split()
                if len(row)!=3:raise ControlError('SOURCE_DATA_INVALID')
                add(row[0],row[1])
    elif kind=='nasa_gistemp':
        lines=text.splitlines()
        if not lines or lines[0]!='Land-Ocean: Global Means':raise ControlError('SOURCE_DATA_INVALID')
        for row in csv.DictReader(lines[1:]):
            if row['J-D']!='***':add(row['Year'],row['J-D'])
    elif kind=='hadcrut5':
        for row in csv.DictReader(io.StringIO(text)):
            add(row['Time'],row['Anomaly (deg C)'])
    elif kind=='noaa_oni':
        groups={}
        for line in text.splitlines()[1:]:
            row=line.split()
            if len(row)!=4:raise ControlError('SOURCE_DATA_INVALID')
            groups.setdefault(int(row[1]),{})[row[0]]=float(row[3])
        seasons={'DJF','JFM','FMA','MAM','AMJ','MJJ','JJA','JAS','ASO','SON','OND','NDJ'}
        for year,values in groups.items():
            if set(values)==seasons:add(year,math.fsum(values.values())/12)
    else:raise ControlError('SOURCE_FORMAT_UNSUPPORTED')
    if not points:raise ControlError('SOURCE_DATA_EMPTY')
    return points

def joined_series(series,plan):
    maximum=min(plan.year_max or utc_now().year-1,utc_now().year-1)
    years=sorted(y for y in set.intersection(*(set(v) for v in series.values())) if plan.year_min<=y<=maximum)
    if len(years)<3:raise ControlError('SOURCE_YEAR_OVERLAP_MISSING')
    baselines={}
    for key in ('nasa_gistemp','hadcrut5'):
        if key in series:
            base=[series[key][year] for year in range(1981,2011) if year in series[key]]
            if len(base)!=30:raise ControlError('SOURCE_BASELINE_INCOMPLETE')
            baselines[key]=math.fsum(base)/30
    output=io.StringIO(newline='');writer=csv.writer(output,lineterminator='\n')
    names=['year',*[CATALOG[key]['variable'] for key in series]];writer.writerow(names)
    for year in years:writer.writerow([year,*[format(values[year]-baselines.get(key,0),'.12g') for key,values in series.items()]])
    omitted={key:[year for year in range(plan.year_min,maximum+1) if year not in values] for key,values in series.items()}
    return output.getvalue().encode('utf-8',errors='strict'),{'columns':names,'row_count':len(years),'first_year':years[0],'last_year':years[-1],
        'temperature_baseline':'1981–2010','baseline_offsets':baselines,'missing_years':omitted,'join':'공통 연도의 내부 결합, 보간 없음',
        'excluded_partial_year':maximum+1,'enso_aggregation':'중첩 3개월 계절 12개의 산술 평균; 독립 월 관측 12개가 아님'}

def _raw_record(state,rid,kind):
    saved=state.runtime_step(rid,'science:climate_source:'+kind)
    if not saved or saved['status']!='COMPLETED':return None
    value=saved['output'];artifact=state.file_artifact(value['artifact_id'],rid)
    if artifact['sha256']!=value['sha256']:raise ControlError('SOURCE_DATA_CHANGED')
    raw=state.workspace.path(rid,artifact['relative_path']).read_bytes()
    return value,raw

async def fetch_climate_data(runtime,rid,contract_id,plan):
    from .ai_web_search import SharedSearchSlots
    from .control_plane import ControlStore
    from .source_documents import PublicDocumentClient
    plan=plan.model_copy(update={'year_max':min(plan.year_max or utc_now().year-1,utc_now().year-1)})
    settings=runtime.science_settings
    if settings.get('search_policy')=='DISABLED' or not settings.get('public_search_consent'):raise ControlError('SEARCH_EGRESS_DENIED')
    store=ControlStore(runtime.state._db);snapshot=settings['snapshot'];slots=SharedSearchSlots(store,rid,snapshot)
    records={};series={}
    for kind in dict.fromkeys(plan.sources):
        old=_raw_record(runtime.state,rid,kind)
        if old:
            record,raw=old
        else:
            pending=runtime.state.runtime_step(rid,'science:climate_pending:'+kind)
            if pending:raise ControlError('SOURCE_DATA_REQUEST_ALREADY_ATTEMPTED')
            client=PublicDocumentClient(timeout=30);slot_ids=[]
            def guard():
                if getattr(runtime,'control_boundary',None):runtime.control_boundary()
                slot=slots.reserve(1,kind='public_original');slot_ids.append(slot)
                runtime.state.finish_runtime_step(rid,'science:climate_pending:'+kind,{'slot_id':slot,'format':kind})
            client.dispatch_guard=guard
            try:
                raw,url=await client.get_bytes(CATALOG[kind]['url'],limit=500000)
                if urlsplit(url).hostname!=urlsplit(CATALOG[kind]['url']).hostname:raise ControlError('SOURCE_ORIGIN_CHANGED')
                parse_series(kind,raw)
                from .release import _secret_free
                if not _secret_free('source.txt',raw):raise ControlError('SOURCE_SECRET_BLOCKED')
                aid=new_id('ART');relative=f'inputs/sources/{aid}.txt';target=runtime.state.workspace.path(rid,relative);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
                digest=hashlib.sha256(raw).hexdigest()
                runtime.state.register_file_artifact(aid,rid,contract_id,'SCIENTIFIC_DATA_SOURCE',relative,'tool',contract_id,digest)
                record={**CATALOG[kind],'format':kind,'final_url':url,'retrieved_at':utc_now().isoformat(),'artifact_id':aid,'sha256':digest,'parser_version':'climate-annual-v1'}
                runtime.state.finish_runtime_step(rid,'science:climate_source:'+kind,record,contract_id)
            finally:
                for slot in slot_ids:slots.finish(slot,1)
        records[kind]=record;series[kind]=parse_series(kind,raw)
    data,preprocessing=joined_series(series,plan)
    did=new_id('D');relative=f'inputs/datasets/{did}.csv';target=runtime.state.workspace.path(rid,relative);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    dataset=DatasetRecord(dataset_id=did,research_id=rid,original_name='public-climate.csv',stored_path=relative,sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data),
        row_count=preprocessing['row_count'],column_count=len(preprocessing['columns']),created_at=utc_now().isoformat())
    runtime.state.register_dataset(dataset)
    result={'status':'IMPORTED','dataset_id':did,'dataset_sha256':dataset.sha256,'plan':plan.model_dump(mode='json'),'sources':records,'preprocessing':preprocessing,'preview':data.decode('utf-8').splitlines()[:4]}
    runtime.state.finish_runtime_step(rid,'science:climate_dataset:'+did,result,contract_id)
    return result

def checked_climate_dataset(state,rid,did):
    saved=state.runtime_step(rid,'science:climate_dataset:'+did)
    if not saved:return None
    value=saved['output'];series={}
    for kind in dict.fromkeys(value['plan']['sources']):
        source=value['sources'][kind]
        current,raw=_raw_record(state,rid,kind)
        if current!=source:raise ControlError('SOURCE_DATA_CHANGED')
        series[kind]=parse_series(kind,raw)
    data,meta=joined_series(series,ClimateDataPlan.model_validate(value['plan']))
    record=state.dataset_record(did,rid)
    if hashlib.sha256(data).hexdigest()!=record['sha256'] or meta!=value['preprocessing']:raise ControlError('DATASET_SOURCE_CHANGED')
    return value

def climate_datasets(state,rid):
    records=[]
    for row in state._db.execute("SELECT dataset_id FROM datasets WHERE research_id=?",(rid,)):
        value=checked_climate_dataset(state,rid,row[0])
        if value:records.append(value)
    return records
