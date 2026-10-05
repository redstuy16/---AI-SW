"""공식 원문의 재사용·전송 동의·변조·비밀값 경계를 검사한다."""
import asyncio
import pytest
from test_science_tool_authority import prepared
from probe.science_sources import fetch_science_sources, ScienceSourcePlan
from probe.control_plane import ControlError

class Client:
    dispatch_guard=None
    last_content_type='text/html; charset=utf-8'
    def __init__(self,data=None):
        self.calls=0;self.data=data or b'<html><title>Climate warming causes</title><p>Carbon dioxide and greenhouse gases cause climate warming and increased temperature.</p></html>'
    async def get_bytes(self,url,limit):
        self.dispatch_guard();self.calls+=1
        return self.data,url

def setup(tmp_path):
    db,state,loop,rid,parent=prepared(tmp_path)
    state.set_research_question(rid,'CO2 증가와 기후 온난화, 지구 기온의 관계')
    db.execute('UPDATE research_runs SET goal=? WHERE research_id=?',('CO2 증가와 기후 온난화, 지구 기온의 관계',rid))
    loop.science_settings.update(search_policy='AUTO',public_search_consent=True,snapshot={'source_fetch_attempt_limit':2})
    return db,state,loop,rid,parent

def test_official_document_uses_literal_provenance_and_cache(tmp_path):
    db,state,loop,rid,parent=setup(tmp_path);client=Client()
    plan=ScienceSourcePlan(sources=['nasa_climate_causes'])
    first=asyncio.run(fetch_science_sources(loop,rid,parent.contract_id,plan,client=client))
    second=asyncio.run(fetch_science_sources(loop,rid,parent.contract_id,plan,client=client))
    assert first==second and first['status']=='VERIFIED' and client.calls==1
    assert db.execute("SELECT COUNT(*) FROM evidence WHERE status='VERIFIED'").fetchone()[0]==1
    assert db.execute("SELECT text_field FROM evidence").fetchone()[0]=='webpage'
    assert client.dispatch_guard is None
    db.close()

def test_source_consent_blocks_before_request(tmp_path):
    db,state,loop,rid,parent=setup(tmp_path);client=Client();loop.science_settings['public_search_consent']=False
    with pytest.raises(ControlError,match='SEARCH_EGRESS_DENIED'):
        asyncio.run(fetch_science_sources(loop,rid,parent.contract_id,ScienceSourcePlan(sources=['nasa_climate_causes']),client=client))
    assert client.calls==0
    db.close()

def test_cached_source_tamper_is_not_downloaded_again(tmp_path):
    db,state,loop,rid,parent=setup(tmp_path);client=Client();plan=ScienceSourcePlan(sources=['nasa_climate_causes'])
    value=asyncio.run(fetch_science_sources(loop,rid,parent.contract_id,plan,client=client))
    from probe.web_sources import checked_web_document
    record,_=checked_web_document(state,rid,value['documents'][0]['source_id'])
    state.workspace.path(rid,record['html']['relative_path']).write_bytes(b'changed')
    from probe.storage import ArtifactIntegrityError
    with pytest.raises(ArtifactIntegrityError):
        asyncio.run(fetch_science_sources(loop,rid,parent.contract_id,plan,client=client))
    assert client.calls==1 and client.dispatch_guard is None
    db.close()

def test_source_secret_is_not_saved_and_ambiguous_request_is_not_retried(tmp_path):
    db,state,loop,rid,parent=setup(tmp_path);client=Client(b'<p>sk-abcdefghijklmnopqrstuvwx</p>');plan=ScienceSourcePlan(sources=['nasa_climate_causes'])
    with pytest.raises(ControlError,match='SECRET'):
        asyncio.run(fetch_science_sources(loop,rid,parent.contract_id,plan,client=client))
    with pytest.raises(ControlError,match='ALREADY_ATTEMPTED'):
        asyncio.run(fetch_science_sources(loop,rid,parent.contract_id,plan,client=client))
    assert client.calls==1 and db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==0
    db.close()
