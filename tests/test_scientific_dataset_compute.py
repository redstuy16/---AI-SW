"""자료를 바꾸거나 다른 연구 자료를 섞어 계산을 통과하지 못하는지 검사한다."""
import hashlib
from pathlib import Path
import pytest
from probe.storage import Workspace,DatasetIntegrityError
from probe.real_schemas import DatasetRecord
from probe.schemas import utc_now
from probe.control_plane import ControlError
from probe.scientific_dataset_compute import DatasetCalculationPlan,compute_dataset,save_dataset_calculation,checked_dataset_calculations


def supplied(cycle):
    db,state,rid,contract,payload=cycle
    state.workspace=Workspace(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent/'workspace');state.workspace.prepare(rid)
    import json
    text=json.loads(Path('qa/climate_questions.json').read_text(encoding='utf-8'))[3]['csv']
    data=text.encode('utf-8',errors='strict');relative='inputs/test.csv';target=state.workspace.path(rid,relative);target.parent.mkdir(exist_ok=True);target.write_bytes(data)
    state.register_dataset(DatasetRecord(dataset_id='D-test',research_id=rid,original_name='test.csv',stored_path=relative,sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data),created_at=utc_now().isoformat()))
    return state,rid,contract


@pytest.mark.parametrize('predictors,expected',[(['t'],[-.043,.0218181818181818]),(['t','ENSO','V'],[0,.018,.120,-.150])])
def test_ols_independent_recheck(cycle,predictors,expected):
    state,rid,contract=supplied(cycle)
    result=compute_dataset(state,rid,DatasetCalculationPlan(dataset_id='D-test',name='fit',operation='OLS',target='T',predictors=predictors,target_unit='°C'))
    coefficients=[v['value'] for v in result['results'] if 'intercept' in v['field'] or '_coef_' in v['field']]
    assert coefficients==pytest.approx(expected,abs=1e-12)
    assert result['verification']=='SVD_QR_AND_RESIDUAL_AGREE'


def test_statistical_report_rechecks_dataset_hash(cycle):
    state,rid,contract=supplied(cycle)
    plan=DatasetCalculationPlan(dataset_id='D-test',name='fit',operation='CORRELATION',target='T',predictors=['t'])
    save_dataset_calculation(state,rid,contract.contract_id,plan)
    assert checked_dataset_calculations(state,rid)
    state.workspace.path(rid,'inputs/test.csv').write_bytes(b't,T\n1,999\n')
    with pytest.raises(DatasetIntegrityError):checked_dataset_calculations(state,rid)


def test_missing_and_invalid_columns_do_not_become_zero(cycle):
    state,rid,contract=supplied(cycle)
    plan=DatasetCalculationPlan(dataset_id='D-test',name='fit',operation='OLS',target='absent',predictors=['t'])
    with pytest.raises(ControlError,match='INSUFFICIENT_NUMERIC_DATA'):compute_dataset(state,rid,plan)


def test_official_data_parser_rejects_duplicates_and_excludes_partial_year():
    from probe.climate_data import parse_series,joined_series,ClimateDataPlan
    with pytest.raises(ControlError):parse_series('noaa_co2',b'1990 354 .1\n1990 355 .1\n')
    last=utc_now().year
    data,meta=joined_series({'noaa_co2':{year:float(year) for year in range(last-3,last+1)}},ClimateDataPlan(sources=['noaa_co2'],year_min=last-3))
    assert meta['last_year']==last-1
    assert str(last)+',' not in data.decode('utf-8')


def test_temperature_baseline_requires_all_reference_years():
    from probe.climate_data import joined_series,ClimateDataPlan
    with pytest.raises(ControlError,match='SOURCE_BASELINE_INCOMPLETE'):
        joined_series({'nasa_gistemp':{1990:.4,1991:.3,1992:.2}},ClimateDataPlan(sources=['nasa_gistemp']))


def test_canonical_sorted_metadata_keeps_original_csv_column_order(cycle, monkeypatch):
    from probe.climate_data import joined_series, ClimateDataPlan, checked_climate_dataset
    from probe.database import to_json, from_json
    db,state,rid,contract,payload=cycle
    state.workspace=Workspace(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent/'workspace')
    state.workspace.prepare(rid)
    plan=ClimateDataPlan(sources=['noaa_oni','noaa_co2'],year_max=1992)
    records={'noaa_oni':{'format':'noaa_oni'},'noaa_co2':{'format':'noaa_co2'}}
    series={'noaa_oni':{1990:0,1991:1,1992:2},'noaa_co2':{1990:354,1991:355,1992:356}}
    data,meta=joined_series(series,plan)
    relative='inputs/test.csv'
    state.workspace.path(rid,relative).write_bytes(data)
    state.register_dataset(DatasetRecord(dataset_id='D-order',research_id=rid,original_name='test.csv',stored_path=relative,
        sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data),created_at=utc_now().isoformat()))
    state.finish_runtime_step(rid,'science:climate_dataset:D-order',{'plan':plan.model_dump(mode='json'),'sources':records,'preprocessing':meta})
    monkeypatch.setattr('probe.climate_data._raw_record',lambda state,rid,kind:(records[kind],kind.encode('utf-8',errors='strict')))
    monkeypatch.setattr('probe.climate_data.parse_series',lambda kind,raw:series[kind])
    assert checked_climate_dataset(state,rid,'D-order')['preprocessing']['columns']==['year','enso','co2_ppm']
