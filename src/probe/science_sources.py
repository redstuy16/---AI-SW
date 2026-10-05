"""선택한 공식 공개 문서를 기존 원문·근거 검증 경로로 수집한다."""
from __future__ import annotations

from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field
from .schemas import StrictModel
from .control_plane import ControlError, ControlStore
from .database import to_json

SOURCE_CATALOG = {
    'nasa_climate_causes': {'url':'https://science.nasa.gov/climate-change/causes/', 'agency':'NASA', 'scope':'기후 변화의 원인과 강제력·피드백 설명'},
    'ipcc_ar6_summary': {'url':'https://www.ipcc.ch/report/ar6/wg1/chapter/summary-for-policymakers/', 'agency':'IPCC', 'scope':'AR6 제1실무그룹 평가 요약과 관측·귀속의 구분'},
}

class ScienceSourcePlan(StrictModel):
    sources: list[Literal['nasa_climate_causes','ipcc_ar6_summary']] = Field(min_length=1,max_length=2)
    topics: list[str] = Field(default_factory=list,max_length=4)

async def fetch_science_sources(runtime, rid, contract_id, plan, *, client=None):
    from .ai_web_search import SharedSearchSlots
    from .source_documents import PublicDocumentClient
    from .web_sources import MAX_HTML_BYTES, extract_html, save_web_document, checked_web_document
    from .scholarly import NormalizedSource, source_from_row
    from .literature import screen_source, extract_web_evidence, LiteralSentenceReviewer, synthesize_literature
    settings=runtime.science_settings
    if settings.get('search_policy')=='DISABLED' or not settings.get('public_search_consent'):
        raise ControlError('SEARCH_EGRESS_DENIED')
    if any(not topic.strip() or len(topic)>400 for topic in plan.topics):
        raise ControlError('SOURCE_TOPIC_INVALID')
    snapshot=settings['snapshot']; store=ControlStore(runtime.state._db)
    slots=SharedSearchSlots(store,rid,snapshot); state=runtime.state
    question=state._one('SELECT goal FROM research_runs WHERE research_id=?',(rid,))[0]
    protected=runtime.protected_values() if callable(getattr(runtime,'protected_values',None)) else getattr(runtime,'protected_values',[])
    client=client or PublicDocumentClient(timeout=30)
    if client.dispatch_guard is not None:raise ControlError('SEARCH_CLIENT_BUSY')
    def guard():
        if getattr(runtime,'control_boundary',None):runtime.control_boundary()
        identity=slots.reserve(1,kind='public_original');slots.finish(identity,1)
    client.dispatch_guard=guard
    documents=[]
    try:
        for kind in dict.fromkeys(plan.sources):
            key='science:public_source:'+kind; saved=state.runtime_step(rid,key)
            if saved and saved['status']=='COMPLETED':
                value=saved['output']; source_id=value['source_id']
                _,paragraphs=checked_web_document(state,rid,source_id)
                source=source_from_row(state._one('SELECT * FROM sources WHERE source_id=?',(source_id,)))
            else:
                if state.runtime_step(rid,key+':pending'):raise ControlError('SOURCE_DOCUMENT_REQUEST_ALREADY_ATTEMPTED')
                url=SOURCE_CATALOG[kind]['url']
                state.finish_runtime_step(rid,key+':pending',{'url':url},contract_id)
                data,final_url=await client.get_bytes(url,limit=MAX_HTML_BYTES)
                from .release import _secret_free
                if not _secret_free('source.html',data):raise ControlError('SOURCE_SECRET_BLOCKED')
                if any(value and value.encode('utf-8',errors='strict') in data for value in protected):
                    raise ControlError('SECRET_IN_DOCUMENT_RESPONSE')
                if urlsplit(final_url).hostname!=urlsplit(url).hostname:raise ControlError('SOURCE_DOCUMENT_ORIGIN_CHANGED')
                parsed=extract_html(data,client.last_content_type or '')
                parsed['content_type']=client.last_content_type or ''
                source=NormalizedSource(title=parsed['title'] or SOURCE_CATALOG[kind]['scope'],url=url,
                    source_name=SOURCE_CATALOG[kind]['agency'],provider='public.science',
                    provider_ids={'retrieved_url':final_url,'html_sha256':sha256(data).hexdigest()})
                source_id,_=state.upsert_source(rid,source)
                save_web_document(state,rid,source_id,data=data,parsed=parsed,url=final_url)
                value={'source_id':source_id,'url':url,'sha256':sha256(data).hexdigest()}
                state.finish_runtime_step(rid,key,value,contract_id);paragraphs=parsed['paragraphs']
            material=source.model_copy(update={'abstract':'\n'.join(p['text'] for p in paragraphs)})
            status=state._one('SELECT status FROM sources WHERE source_id=?',(source_id,))[0]
            if status in {'DISCOVERED','IRRELEVANT'}:
                state.set_source_relevance(rid,screen_source(source_id,material,question),recheck_original=status=='IRRELEVANT')
            evidence=[]
            if state._one('SELECT status FROM sources WHERE source_id=?',(source_id,))[0] not in {'IRRELEVANT','INVALIDATED'}:
                for topic in [question,*plan.topics]:
                    item=extract_web_evidence(state,rid,source_id,topic)
                    if item:
                        state.mark_source_extracted(rid,source_id)
                        eid=state.verify_literature_evidence(rid,item,reviewer=LiteralSentenceReviewer())
                        evidence.append(eid)
            documents.append({**value,'evidence_ids':evidence,'paragraph_count':len(paragraphs)})
        state.save_literature_synthesis(rid,synthesize_literature(state,rid))
        return {'status':'VERIFIED' if any(d['evidence_ids'] for d in documents) else 'NO_EVIDENCE','documents':documents}
    finally:
        client.dispatch_guard=None
