'use strict';
(() => {
 const tabs=[['overview','개요'],['flow','연구 흐름'],['timeline','진행 기록'],['evidence','근거·자료'],['experiments','실험/분석'],['verification','검증'],['report','보고서']];
 const categories=[['all','전체 연구'],['knowledge','자료·근거'],['experiment','실험·분석'],['verification','검증·복구'],['tools','실행 도구']];
 const statusNames={WAITING:'대기',QUEUED:'대기',RUNNING:'진행 중',STARTING:'진행 중',COMPLETED:'완료',READY:'완료',PARTIAL:'부분 완료',EMPTY:'자료 없음',FAILED:'실패',SKIPPED:'건너뜀',NEEDS_REVIEW:'확인 필요',SEARCH_EGRESS_DENIED:'동의 필요',SEARCH_QUERY_REQUIRED:'검색어 필요',SEARCH_DISABLED:'건너뜀',SEARCH_REQUIRED_BUT_DISABLED:'설정 필요',SEARCH_NOT_NEEDED:'건너뜀',SEARCH_ATTEMPT_LIMIT:'횟수 소진',COMPLETION_RESERVE_BLOCKED:'예산 부족',PRICE_UNKNOWN:'단가 확인'};
 const phaseName=v=>statusNames[v]||'확인 필요';
 const phaseTone=v=>['COMPLETED','READY'].includes(v)?'complete':['RUNNING','STARTING'].includes(v)?'running':['FAILED','STOPPED'].includes(v)?'failed':['WAITING','QUEUED','SKIPPED','SEARCH_NOT_NEEDED','SEARCH_DISABLED'].includes(v)?'waiting':'review';
 const endpoint=(rid,suffix)=>'/api/control/research/'+encodeURIComponent(rid)+'/'+suffix;
 const useNames={CURRENT:'현재 검증 범위에서 사용',STALE:'재검증 필요',DESIGN_ONLY:'설계안 · 정량 결론 미확인',UNCONFIRMED:'결론 미확인'};
 let current=null,refreshing=false;const pdfSessions=new Map();
 function disposePDF(root){const session=pdfSessions.get(root);pdfSessions.delete(root);if(session){session.resize?.disconnect();session.task?.cancel();session.loading.destroy().catch(()=>{});}}
 new MutationObserver(()=>{for(const root of pdfSessions.keys())if(!root.isConnected)disposePDF(root);}).observe(document.body,{childList:true,subtree:true});
 function patchHTML(node,markup){if(node&&node.innerHTML!==markup)node.innerHTML=markup;}
 function controls(s){
  if(!s.controlled)return '';
  return [['start','연구 실행',['DRAFT','PREFLIGHT_BLOCKED']],['pause','일시정지',['RUNNING','STARTING','RESUMING']],['resume','계속하기',['PAUSED']],['stop','중단',['RUNNING','STARTING','RESUMING','PAUSED']]].filter(([, ,list])=>list.includes(s.status)).map(([key,name])=>'<button data-command="'+key+'" '+(key==='start'?'class="primary"':'')+'>'+name+'</button>').join('');
 }
 function blocker(s){return s.blocker?.message?'<strong>'+esc(s.blocker.message)+'</strong><button id="repair-search">'+esc(s.blocker.action||'설정 보완해 다시 연구')+'</button>':'';}
 function collectionHTML(s){return '<div class="collection-counts" aria-label="자료 수집 현황">'+[['requests','요청'],['sources','수집 자료'],['relevant','관련 문헌'],['readable','읽을 수 있는 자료'],['verified','검증된 근거']].map(([key,label])=>'<span>'+label+' <strong>'+Number(s.counts[key]||0)+'</strong></span>').join('')+'</div>';}
 function currentness(s){return '<span class="currentness-badge '+(s.currentness==='STALE'?'warn':'good')+'" data-currentness="'+s.currentness+'">'+(s.currentness==='STALE'?'재검증 필요':'현재 기록')+'</span>';}
 function stageMap(s){return '<ol class="research-stage-map" id="stage-map" aria-label="연구 단계">'+s.stages.map((p,i)=>'<li class="stage-card" data-phase="'+p.id+'" data-status="'+p.status+'" data-tone="'+phaseTone(p.status)+'"><button data-phase-select="'+p.id+'"><span class="stage-index" aria-hidden="true">'+(p.status==='COMPLETED'||p.status==='READY'?'✓':i+1)+'</span><strong>'+esc(p.label)+'</strong><span class="stage-status">'+esc(phaseName(p.status))+'</span></button></li>').join('')+'</ol>';}
 function eventLabel(e){
  const names={AGENT_RUN_COMPLETED:'AI 작업 완료',ACTION_COMPLETED:'작업 완료',REPORT_RENDERED:'보고서 저장',RESEARCH_STOPPED:'연구 실행 종료',RESEARCH_DESIGN_SAVED:'연구 조건 저장','data.import':'자료 불러오기','data.profile':'자료 확인','stats.run':'분석 실행','literature.search':'자료 검색'};
  const label=names[e.event_type]||state(e.label||e.event_type);
  return /^[A-Z][A-Z0-9_:. -]*$/.test(label)?'연구 기록':label;
 }
 function timelineHTML(events){return '<ol class="research-timeline">'+events.map((e,i)=>'<li><time datetime="'+esc(e.timestamp)+'">'+esc(time(e.timestamp))+'</time><div><strong>'+esc(eventLabel(e))+'</strong><button data-event="'+i+'" data-event-id="'+esc(e.id)+'">기록 상세</button></div></li>').join('')+'</ol>'+(events.length?'':'<div class="empty">진행 기록이 없습니다.</div>');}
 function bindTimeline(root){
  root?.querySelectorAll('[data-event-id]').forEach(b=>b.onclick=async()=>{const entry=current;try{const record=await api(endpoint(entry.rid,'activity-event?id='+encodeURIComponent(b.dataset.eventId)));if(current!==entry)return;inspect('저장된 진행 기록',details(record,'의사결정·도구·검증 기록'));}catch(e){message(e.message);}});
 }
 function designHTML(s){
  const draft=s.report?.draft;if(!draft)return '';
  return '<section class="design-preview"><div class="section-head"><h2>실험 설계</h2><span class="tag">설계안</span>'+currentness(s)+'</div><div class="variable-map">'+(draft.variables||[]).map(v=>'<div><span>'+esc(v.role)+'</span><strong>'+esc(v.name)+'</strong></div>').join('<span class="variable-arrow" aria-hidden="true">→</span>')+'</div><ol>'+(draft.procedure||[]).map(p=>'<li>'+esc(p)+'</li>').join('')+'</ol>'+(draft.measurement?'<dl><dt>측정 방법</dt><dd>'+esc(draft.measurement)+'</dd></dl>':'')+'</section>';
 }
 async function openDetail(identity){
  const entry=current,record=await api(endpoint(entry.rid,'flow-node?id='+encodeURIComponent(identity)));if(current!==entry)return;
  const drawer=$('#research-detail');drawer.hidden=false;$('#detail-body').innerHTML='<h2>'+esc(record.title)+'</h2>'+fields(record.summary)+(record.detail.abstract?'<p>'+esc(record.detail.abstract)+'</p>':'')+details(record.detail,'입력·결과·근거·오류')+(record.repair_contract?'<h3>복구와 재검증</h3>'+details(record.repair_contract,'원본 분석 계약')+'<h3>분석 결과</h3>'+details(record.analysis_result,'분석 결과')+details(record.verifier_result,'검증 결과'):'');$('#detail-close').focus();
 }
 async function openSource(sourceId,page){
  const entry=current,record=await api(endpoint(entry.rid,'source-document?source_id='+encodeURIComponent(sourceId)));if(current!==entry)return;
  $('#research-detail').hidden=false;$('#detail-body').innerHTML='<h2>공개 원문</h2><span class="tag">'+record.pages+'페이지 확인'+(record.truncated?' · 일부 페이지':'')+'</span><div id="source-pdf-viewer"></div>';await mountPDF($('#source-pdf-viewer'),entry.rid,{sourceId,page});$('#detail-close').focus();
 }
 function showTab(tab){app.tab=tab;app.technicalResearch=false;app.activityOffset=0;app.itemOffset=0;history.replaceState(null,'','#research/'+current.rid+'/'+tab);return render();}
 function bindLive(){
  document.querySelectorAll('[data-command]').forEach(b=>b.onclick=async()=>{const entry=current;b.disabled=true;try{await api(endpoint(entry.rid,b.dataset.command),{expected_version:entry.summary.version,idempotency_key:crypto.randomUUID()});if(current===entry)await refresh();}catch(e){message(e.message);}finally{if(b.isConnected)b.disabled=false;}});
  $('#repair-search')?.addEventListener('click',async()=>{
   if(['REPORT_WRITING_LIMITED','SEARCH_ATTEMPT_LIMIT','SEARCH_RATE_LIMITED','LITERATURE_DESIGN_COMPLETED'].includes(current.summary.blocker?.code)){await showTab('report');return;}
   if(current.summary.blocker?.code==='COMPLETION_RESERVE_BLOCKED'){await renderUsage(current.rid);return;}
   const s=current.control.effective_snapshot||current.control.snapshot;if(!s){await showTab('verification');return;}
   app.researchDraft={...(s.requested_settings||{}),title:current.summary.title,question:s.question,public_search_query:s.public_search_query,public_search_consent:s.public_search_consent,search_policy:s.search_policy,model_profile_id:s.model_profile_id,run_limit_usd:s.run_limit_usd,egress:s.egress,submission_key:crypto.randomUUID()};app.researchAttachments=null;await newResearch({skipExplanation:true});$('#research-form')?.setResearchPage(1);
  });
  document.querySelectorAll('[data-open-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.openTab));
  document.querySelectorAll('[data-phase-select]').forEach(b=>b.onclick=()=>{
   const selected=current.summary.stages.find(p=>p.id===b.dataset.phaseSelect);$('#research-detail').hidden=false;$('#detail-body').innerHTML='<h2>'+esc(selected.label)+'</h2><p>'+esc(phaseName(selected.status))+'</p>'+fields(selected.id==='search'?{'검색 요청 수':current.summary.counts.requests,'관련 문헌':current.summary.counts.relevant,'읽을 수 있는 자료':current.summary.counts.readable}:selected.id==='verification'?{'검증 통과':current.summary.counts.checks_passed,'저장된 검증':current.summary.counts.checks_total,'다시 확인할 항목':current.summary.counts.revalidation}:{'단계 상태':phaseName(selected.status),'현재성':current.summary.currentness==='STALE'?'재검증 필요':'현재 기록'});$('#detail-close').focus();
  });
 }
 async function evidencePanel(root){
  const entry=current,rid=entry.rid,offset=entry.evidenceOffset||0,page=await api(endpoint(rid,'items?kind=evidence&limit=25&offset='+offset));if(current!==entry||!root.isConnected)return;
  root.innerHTML='<div class="section-head"><h2>근거·자료</h2>'+currentness(entry.summary)+'<div class="inline-actions"><button id="evidence-prev" '+(!offset?'disabled':'')+'>이전</button><button id="evidence-more" '+(page.next_offset===null?'disabled':'')+'>더 보기</button></div></div>'+collectionHTML(entry.summary)+page.items.map(v=>'<article class="evidence-row"><span class="tag '+(v.status==='VERIFIED'?'good':'warn')+'">'+esc(v.status==='VERIFIED'?'인용 확인':'확인 필요')+'</span>'+(v.source_scope==='DIRECT'?'<span class="tag">대상·조건 일치</span>':v.source_scope==='INDIRECT'?'<span class="tag">원리 설명</span>':'')+'<h3>'+esc(v.source_title||'분석 자료')+'</h3><p>'+esc(v.claim)+'</p><div class="inline-actions"><button data-evidence="'+esc(v.evidence_id)+'">근거 보기</button>'+(v.source_id?'<button data-source="'+esc(v.source_id)+'">출처 보기</button>':'')+'</div></article>').join('')+(page.items.length?'':'<div class="empty">확인된 근거가 아직 없습니다.</div>');
  root.querySelectorAll('[data-evidence]').forEach(b=>b.onclick=()=>openDetail('evidence:'+b.dataset.evidence).catch(e=>message(e.message)));root.querySelectorAll('[data-source]').forEach(b=>b.onclick=()=>openDetail('source:'+b.dataset.source).catch(e=>message(e.message)));
  for(const item of page.items.filter(v=>v.text_field==='fulltext'&&v.status==='VERIFIED')){const row=[...root.querySelectorAll('[data-evidence]')].find(b=>b.dataset.evidence===item.evidence_id)?.closest('article');if(row){const b=document.createElement('button');b.type='button';b.textContent='원문 '+item.evidence_location.split(' ').at(-1)+'페이지';b.dataset.sourceDocument=item.source_id;b.onclick=()=>openSource(item.source_id,Number(item.evidence_location.split(' ').at(-1))).catch(e=>message(e.message));row.querySelector('.inline-actions').append(b);}}
  root.querySelector('#evidence-more').onclick=()=>{entry.evidenceOffset=page.next_offset;evidencePanel(root).catch(e=>message(e.message));};root.querySelector('#evidence-prev').onclick=()=>{entry.evidenceOffset=Math.max(0,offset-25);evidencePanel(root).catch(e=>message(e.message));};
 }
 async function visuals(root){
  if(!root)return;const entry=current,s=entry.summary;if(!s.report_ready){patchHTML(root,designHTML(s));return;}
  try{const view=await api(endpoint(entry.rid,'report-view'));if(current!==entry||!root.isConnected)return;patchHTML(root,(view.images||[]).map(v=>'<figure class="result-figure"><img src="'+esc(v.url)+'" alt="'+esc(v.caption)+'"><figcaption>'+esc(v.caption)+'</figcaption></figure>').join('')+designHTML(s));}catch(e){if(root.isConnected)root.innerHTML='<p role="alert">결과 수정본을 확인해 주세요.</p>';}
 }
 function claimsHTML(s){return '<div class="claim-links">'+s.claims.map(c=>'<article class="claim-card"><h3>'+esc(c.statement)+'</h3>'+tag(c.status)+'<div class="claim-counts"><span>연결된 근거 <strong>'+c.evidence_count+'</strong></span><span>인용 확인 <strong>'+(c.verified_count||0)+'</strong></span></div>'+(c.evidence_id?'<button data-claim-evidence="'+esc(c.evidence_id)+'">연결 근거 보기</button>':'')+'</article>').join('')+'</div>'+(s.claims.length?'':'<div class="empty">연결된 주장과 근거가 아직 없습니다.</div>');}
 function workHTML(s){return '<div class="work-summary"><section><span>현재 작업</span><strong>'+esc(s.work.stage)+'</strong><small>'+esc(s.work.current?labels[s.work.current.role]||'담당 Agent':'담당 작업 없음')+'</small></section><section><span>마지막 작업</span><strong>'+esc(s.work.last?.objective||'기록 대기')+'</strong></section><section><span>다음 예상 단계</span><strong>'+esc(s.work.next||'예정 없음')+'</strong></section></div>';}
 function factsHTML(s){return '<div class="flow-facts">'+[['현재 상태',s.official_status.label],['확인 필요',s.counts.review],['검증된 근거',s.counts.verified],['검증 통과',s.counts.checks_passed+' / '+s.counts.checks_total],['보고서',phaseName(s.report?.status||(s.report_ready?'COMPLETED':'WAITING'))]].map(([label,v])=>'<div><span>'+label+'</span><strong>'+esc(v)+'</strong></div>').join('')+'</div>';}
 function bindClaims(root){root?.querySelectorAll('[data-claim-evidence]').forEach(b=>b.onclick=()=>openDetail('evidence:'+b.dataset.claimEvidence).catch(e=>message(e.message)));}
 function verificationHTML(s){return '<h3>'+esc(useNames[s.conclusion_use])+'</h3>'+currentness(s)+'<div class="collection-counts"><span>검증 통과 '+s.counts.checks_passed+' / '+s.counts.checks_total+'</span><span>재검증 필요 '+s.counts.revalidation+'</span></div><button data-open-tab="verification">검증·복구 기록 보기</button>';}
 function actionsHTML(s){return '<div class="inline-actions">'+(s.currentness==='STALE'?'<button data-open-tab="verification">재검증 항목 확인</button>':s.blocker?'<button data-open-tab="report">확보한 결과 보기</button>':'<span>확인할 사용자 작업 없음</span>')+(s.status==='PAUSED'?'<button data-command="resume">계속하기</button>':'')+'</div>';}
 async function overviewPanel(root){
  const s=current.summary;root.innerHTML='<section class="overview-question"><span class="eyebrow">연구 질문</span><h2>'+esc(s.question)+'</h2></section><div class="overview-result" id="core-result"></div><div id="overview-counts">'+collectionHTML(s)+'</div><section class="conclusion-use"><h2>결론 확인 상태</h2><strong>'+esc(useNames[s.conclusion_use])+'</strong>'+currentness(s)+'</section><section><div class="section-head"><h2>최근 변화</h2><button data-open-tab="timeline">전체 기록</button></div><div id="overview-timeline">'+timelineHTML(s.timeline)+'</div></section><div class="research-shortcuts"><button data-open-tab="flow"><strong>연구 흐름</strong></button><button data-open-tab="report"><strong>보고서</strong></button><button data-open-tab="verification"><strong>확인할 항목</strong><span>'+s.counts.review+'</span></button></div>';
  updateCoreResult();bindTimeline(root);bindLive();
 }
 function updateCoreResult(){
  const s=current.summary,card=s.card,root=$('#core-result');if(!root)return;
  patchHTML(root,card?.record?.profile_id?cardHTML(card):'<section><div class="section-head"><h2>최근 핵심 결과</h2>'+currentness(s)+'</div><p>'+esc(s.report?.draft?.summary||card?.calculation||s.blocker?.message||'결과를 기다리고 있습니다.')+'</p></section>');
  if(card?.record?.profile_id)bindProfileCard(card,encodeURIComponent(current.rid));
 }
 async function flowPanel(root){
  const entry=current;root.innerHTML='<div class="research-flow-board"><nav class="flow-categories" aria-label="흐름 범주">'+categories.map(([key,label])=>'<button data-flow-category="'+key+'" aria-pressed="'+(entry.category===key)+'">'+label+'</button>').join('')+'</nav><section id="flow-board-content"></section></div>';
  root.querySelectorAll('[data-flow-category]').forEach(b=>{b.onclick=async()=>{entry.category=b.dataset.flowCategory;(app.flowCategories??={})[entry.rid]=entry.category;root.querySelectorAll('[data-flow-category]').forEach(x=>x.setAttribute('aria-pressed',x===b));await flowCategory();};b.onkeydown=e=>{if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key))return;e.preventDefault();const all=[...root.querySelectorAll('[data-flow-category]')],i=all.indexOf(b);all[e.key==='Home'?0:e.key==='End'?all.length-1:Math.max(0,Math.min(all.length-1,i+(e.key==='ArrowDown'?1:-1)))].focus();};});
  await flowCategory();
 }
 async function flowCategory(){
  const entry=current,s=entry.summary,kind=entry.category,root=$('#flow-board-content');if(!root)return;
  const title=categories.find(([key])=>key===kind)[1];
  root.innerHTML='<div class="section-head"><h2>'+title+'</h2>'+currentness(s)+'</div><div id="flow-work">'+workHTML(s)+'</div>'+
   (kind==='all'?'<div id="flow-facts">'+factsHTML(s)+'</div><div id="flow-stages">'+stageMap(s)+'</div><section class="user-actions"><h3>사용자 할 일</h3><div id="flow-user-actions">'+actionsHTML(s)+'</div></section><section><div class="section-head"><h3>최근 상태 변화</h3><button data-open-tab="timeline">전체 기록</button></div><div id="flow-timeline">'+timelineHTML(s.timeline)+'</div></section>':
    kind==='knowledge'?'<div id="flow-collection">'+collectionHTML(s)+'</div><div id="flow-claims">'+claimsHTML(s)+'</div><button data-open-tab="evidence">근거·자료 전체 보기</button>':
    kind==='experiment'?'<div id="run-visuals"></div><div class="collection-counts"><span>분석 '+s.counts.experiments+'</span><span>검증된 분석 '+s.counts.verified_experiments+'</span></div><button data-open-tab="experiments">분석 기록 보기</button>':
    kind==='verification'?'<section class="verification-summary">'+verificationHTML(s)+'</section>':
    '<div class="collection-counts"><span>도구 종류 '+s.counts.tools+'</span><span>산출물 '+s.counts.artifacts+'</span></div>')+'<details class="relationship-details"><summary>저장된 연구 관계</summary><div id="flow-graph"></div></details>';
  bindLive();bindTimeline(root);bindClaims(root);
  if(kind==='experiment')await visuals($('#run-visuals'));
  if(!window.ResearchFlowMap)await new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='/assets/research_flow.js';script.onload=resolve;script.onerror=()=>reject(Error('연구 흐름을 불러올 수 없습니다.'));document.head.append(script);});
  if(current!==entry||app.tab!=='flow')return;
  await window.ResearchFlowMap.mount($('#flow-graph'),entry.rid,{view:'all',lane:kind==='all'?null:kind,onSelect:openDetail});
 }
 async function refresh(){
  if(refreshing||!current||app.view!=='research'||app.rid!==current.rid||!$('#research-workspace'))return;
  refreshing=true;const entry=current,epoch=app.epoch;
  try{const s=await api(endpoint(entry.rid,'screen'));if(current!==entry||epoch!==app.epoch||!$('#research-workspace'))return;const previous=entry.summary;entry.summary=s;app.control={...app.control,status:s.status,version:s.version};$('#run-state').textContent=s.official_status.label;$('#run-state').dataset.tone=s.official_status.tone;$('#last-update').textContent=time(s.updated_at);$('#review-count').textContent=s.counts.review;$('#revalidation-count').textContent=s.counts.revalidation;patchHTML($('#run-controls'),controls(s));patchHTML($('#run-blocker'),blocker(s));$('#run-blocker').hidden=!s.blocker?.message;
   if(entry.tab==='overview'){patchHTML($('#overview-counts'),collectionHTML(s));patchHTML($('#overview-timeline'),timelineHTML(s.timeline));patchHTML($('.conclusion-use'),'<h2>결론 확인 상태</h2><strong>'+esc(useNames[s.conclusion_use])+'</strong>'+currentness(s));updateCoreResult();bindTimeline($('#run-content'));}
   if(entry.tab==='flow'){patchHTML($('#flow-work'),workHTML(s));patchHTML($('#flow-facts'),factsHTML(s));patchHTML($('#flow-stages'),stageMap(s));patchHTML($('#flow-collection'),collectionHTML(s));patchHTML($('#flow-claims'),claimsHTML(s));patchHTML($('#flow-user-actions'),actionsHTML(s));patchHTML($('#flow-timeline'),timelineHTML(s.timeline));patchHTML($('.verification-summary'),verificationHTML(s));bindTimeline($('#run-content'));bindClaims($('#run-content'));await window.ResearchFlowMap?.refresh();}
   document.querySelectorAll('[data-currentness]').forEach(b=>{b.dataset.currentness=s.currentness;b.className='currentness-badge '+(s.currentness==='STALE'?'warn':'good');b.textContent=s.currentness==='STALE'?'재검증 필요':'현재 기록';});
   document.querySelectorAll('[data-cost-field]').forEach(b=>{if(s.ledger[b.dataset.costField]!==undefined)b.textContent=costUSD(s.ledger[b.dataset.costField]);});
   bindLive();if(previous.report_ready!==s.report_ready||previous.report?.revision!==s.report?.revision){if(entry.tab==='report')await reportPanel($('#run-content'));else if(entry.tab==='flow'&&entry.category==='experiment')await visuals($('#run-visuals'));}
   $('#research-workspace').dataset.version=s.version??s.state_version;
  }catch(e){if(current===entry)message(e.message);}finally{refreshing=false;}
 }
 renderRun=async function(epoch){
  const rid=app.rid,control=await api(endpoint(rid,'control'));if(epoch!==app.epoch)return;
  const s=await api(endpoint(rid,'screen'));if(epoch!==app.epoch)return;
  const aliases={progress:'flow',live:'flow',current:'flow',technical:'flow'};
  app.tab=aliases[app.tab]||app.tab;if(!tabs.some(([key])=>key===app.tab))app.tab='overview';app.technicalResearch=false;
  current={rid,summary:s,control,tab:app.tab,category:(app.flowCategories??={})[rid]||'all',evidenceOffset:0};app.control={...control,status:s.status,version:s.version};
  $('#mode').textContent=s.overview.mode==='DEMO'?'예시 데이터':state(s.overview.mode);
  $('#content').innerHTML='<section id="research-workspace" data-research-id="'+esc(rid)+'" data-active-tab="'+esc(app.tab)+'" data-version="'+(s.version??s.state_version)+'"><header class="research-header"><div class="research-heading"><div class="research-breadcrumb"><button id="run-back">기본 화면으로</button><span>연구</span></div><h1 id="research-title">'+esc(s.title||s.question)+'</h1><div class="inline-actions"><span id="run-state" class="tag" data-tone="'+s.official_status.tone+'">'+esc(s.official_status.label)+'</span><button id="title-expand" aria-expanded="false">전체 제목</button></div></div><div class="research-actions"><div class="inline-actions" id="run-controls">'+controls(s)+'</div><div class="inline-actions secondary-actions"><button id="run-settings" '+(!s.controlled?'disabled':'')+'>연구 설정</button><button id="design-amend" '+(!s.controlled?'disabled':'')+'>연구 조건</button><button id="run-usage">예산 확인</button></div></div></header><section class="research-money" id="research-cost" data-research-id="'+esc(rid)+'" aria-label="연구 요약"><span>USD(미화 달러)</span><span>사용 비용 <strong data-cost-field="spent">'+costUSD(s.ledger.spent)+'</strong></span><span>남은 예산 <strong data-cost-field="available">'+costUSD(s.ledger.available)+'</strong></span><span class="summary-update">마지막 갱신 <time id="last-update">'+esc(time(s.updated_at))+'</time></span><span class="summary-chip">확인 필요 <strong id="review-count">'+s.counts.review+'</strong></span><span class="summary-chip">재검증 필요 <strong id="revalidation-count">'+s.counts.revalidation+'</strong></span><span id="cost-refresh-status" class="sr-only"></span><details><summary>예산 상세</summary><p>진행 중 확보 <span data-cost-field="reserved">'+costUSD(s.ledger.reserved)+'</span> · 미확정 <span data-cost-field="unresolved">'+costUSD(s.ledger.unresolved)+'</span></p></details></section><section id="run-blocker" class="run-blocker" '+(!s.blocker?.message?'hidden':'')+'>'+blocker(s)+'</section><nav class="tabs research-tabs" aria-label="연구 메뉴">'+tabs.map(([key,label])=>'<button data-primary-tab="'+key+'" data-tab="'+key+'" aria-current="'+(app.tab===key?'page':'false')+'">'+label+'</button>').join('')+'</nav><div class="research-body"><div id="run-content"></div><aside id="research-detail" hidden aria-label="선택한 항목"><button id="detail-close">상세 닫기</button><div id="detail-body"></div></aside></div></section>';
  $('#breadcrumb').textContent='연구 / '+(s.title||s.question);$('#run-back').onclick=()=>{current=null;app.rid=null;app.tab='overview';history.replaceState(null,'','#research');render();};$('#run-usage').onclick=()=>{app.usageOffset=0;app.usageFilter={scope:'all',role:''};renderUsage(rid).catch(e=>message(e.message));};$('#run-settings').onclick=()=>researchSettings().catch(e=>message(e.message));$('#design-amend').onclick=()=>window.HtrsaResearchDesign.amend(rid).catch(e=>message(e.message));$('#detail-close').onclick=()=>{disposePDF($('#source-pdf-viewer'));$('#research-detail').hidden=true;$('#detail-body').replaceChildren();$('[data-primary-tab][aria-current=page]').focus();};
  $('#title-expand').onclick=b=>{const expanded=b.target.getAttribute('aria-expanded')!=='true';b.target.setAttribute('aria-expanded',String(expanded));$('#research-title').classList.toggle('expanded',expanded);b.target.textContent=expanded?'제목 접기':'전체 제목';};
  document.querySelectorAll('[data-primary-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.primaryTab));
  const root=$('#run-content');
  if(app.tab==='overview')await overviewPanel(root);else if(app.tab==='flow')await flowPanel(root);else if(app.tab==='evidence')await evidencePanel(root);else if(app.tab==='report')await reportPanel(root);else{
   if(app.tab==='experiments'){root.innerHTML='<div id="run-visuals"></div><section id="analysis-records"></section>';await visuals($('#run-visuals'));await renderResearchDetail(epoch,control,s.overview,'analysis-records');}
   else await renderResearchDetail(epoch,control,s.overview);
  }
  if(epoch!==app.epoch||!$('#research-workspace'))return;bindLive();$('#statusbar').textContent='저장된 연구 기록 · 비용은 앱 계산값';
 };
 async function reportPanel(root){
  const s=current.summary;root.innerHTML='<div class="section-head"><h2>보고서</h2><div class="inline-actions"><button id="pdf-reopen">PDF 열기</button><button data-pdf="'+esc(current.rid)+'">다운로드</button><button id="rewrite-report">AI 보고서 다시 작성 · API 호출</button><button id="report-evidence">근거 보기</button><button id="report-export">내보내기</button></div></div><p id="report-currentness" role="status"></p><div id="pdf-viewer"></div>';
  const active=['DRAFT','RUNNING','STARTING','RESUMING','PAUSED','NEEDS_RECONCILIATION'].includes(s.status);
  $('#rewrite-report').disabled=active;$('#rewrite-report').onclick=async b=>{b.target.disabled=true;try{const result=await api(endpoint(current.rid,'rewrite-report'),{expected_version:current.summary.version,state_version:current.summary.state_version,idempotency_key:crypto.randomUUID()});await refresh();await render();message(result.status==='READY'?'보고서 작성 완료':'부분 보고서를 남겼습니다.',result.status==='READY'?'success':'error');}catch(e){message(e.message);}finally{if(b.target.isConnected)b.target.disabled=false;}};
  $('#report-evidence').onclick=()=>{const drawer=$('#research-detail');drawer.hidden=false;evidencePanel($('#detail-body')).catch(e=>message(e.message));$('#detail-close').focus();};
  $('#report-export').onclick=async()=>{try{const v=await api(endpoint(current.rid,'export'),{});inspect('내보내기',fields(v));}catch(e){message(e.message);}};
  if(s.card?.record?.profile_id){root.insertAdjacentHTML('beforeend',cardHTML(s.card));bindProfileCard(s.card,encodeURIComponent(current.rid));}
  $('#pdf-reopen').onclick=async()=>{try{await mountPDF($('#pdf-viewer'),current.rid);}catch(e){message(e.message);}};
  if(!s.report_ready){$('#report-currentness').textContent=s.report&&!s.report.current?'이전 보고서입니다. 현재 근거로 다시 작성해 주세요.':s.card?.available&&!s.card.current?'현재 결론을 다시 확인해야 합니다. 계산 다시 확인하기를 눌러 주세요.':'보고서를 준비하고 있습니다. 확보한 기록은 진행과 근거·자료에서 확인할 수 있습니다.';return;}
  $('#report-currentness').textContent=s.report?'수정본 '+s.report.revision+' · '+(s.report.status==='READY'?'AI 작성':'부분 보고서'):'저장된 연구 보고서';
  await mountPDF($('#pdf-viewer'),current.rid);
 }
 async function mountPDF(root,rid,{sourceId=null,page:initialPage=null}={}){
  delete root.dataset.ready;
  disposePDF(root);
  if(!sourceId)await requireCurrentReport(rid);
  const pdfjs=await import('/assets/pdfjs/build/pdf.mjs');if(!root.isConnected)return;
  pdfjs.GlobalWorkerOptions.workerSrc='/assets/pdfjs/build/pdf.worker.mjs';
  const loading=pdfjs.getDocument({url:endpoint(rid,sourceId?'source-document.pdf?source_id='+encodeURIComponent(sourceId):'report.pdf'),cMapUrl:'/assets/pdfjs/web/cmaps/',cMapPacked:true,standardFontDataUrl:'/assets/pdfjs/web/standard_fonts/',wasmUrl:'/assets/pdfjs/web/wasm/',isEvalSupported:false,disableAutoFetch:true});
  const session={loading,task:null,root};pdfSessions.set(root,session);let pdf;
  try{pdf=await loading.promise;}catch(e){if(root.isConnected)root.innerHTML='<p role="alert">PDF를 열 수 없습니다. 보고서 수정본과 근거를 확인해 주세요.</p>';return;}
  if(!root.isConnected||pdfSessions.get(root)!==session){await loading.destroy();return;}
  const saved=(app.pdfPositions??={})[rid+(sourceId||'')]??={page:1,zoom:1,fit:true};saved.page=Math.max(1,Math.min(pdf.numPages,initialPage||saved.page));
  root.innerHTML='<div class="pdf-toolbar"><button id="pdf-prev" aria-label="이전 페이지">‹</button><label>페이지 <input id="pdf-page" type="number" min="1" max="'+pdf.numPages+'" value="'+saved.page+'"></label><span>/ '+pdf.numPages+'</span><button id="pdf-next" aria-label="다음 페이지">›</button><button id="pdf-minus" aria-label="축소">−</button><output id="pdf-zoom"></output><button id="pdf-plus" aria-label="확대">+</button><button id="pdf-fit">너비 맞춤</button></div><div class="pdf-tools"><details id="pdf-outline"><summary>목차</summary><div id="pdf-outline-items"></div></details><form id="pdf-search-form"><label>PDF 검색<input id="pdf-search" type="search" maxlength="100"></label><button>검색</button></form><div id="pdf-search-results" role="status"></div></div><div class="pdf-canvas-wrap" tabindex="0" aria-label="PDF 페이지"><canvas id="pdf-canvas" role="img"></canvas></div><details><summary>페이지 텍스트</summary><p id="pdf-text"></p></details>';
  const prefix=sourceId?'source-pdf-':'pdf-';if(sourceId)root.innerHTML=root.innerHTML.replaceAll('id="pdf-', 'id="'+prefix);const pick=selector=>root.querySelector(selector.replace('#pdf-','#'+prefix));
  let token=0;
  async function draw(){
   delete root.dataset.ready;
   session.task?.cancel();const mine=++token,page=await pdf.getPage(saved.page);if(mine!==token||!root.isConnected||pdfSessions.get(root)!==session)return;
   const natural=page.getViewport({scale:1}),available=Math.max(100,root.querySelector('.pdf-canvas-wrap').clientWidth-24),scale=saved.fit?available/natural.width:saved.zoom,viewport=page.getViewport({scale}),canvas=pick('#pdf-canvas'),ratio=Math.min(2,window.devicePixelRatio||1);
   canvas.width=Math.round(viewport.width*ratio);canvas.height=Math.round(viewport.height*ratio);canvas.style.width=viewport.width+'px';canvas.style.height=viewport.height+'px';canvas.setAttribute('aria-label',saved.page+'페이지');
   pick('#pdf-page').value=saved.page;pick('#pdf-zoom').textContent=Math.round(scale*100)+'%';pick('#pdf-prev').disabled=saved.page===1;pick('#pdf-next').disabled=saved.page===pdf.numPages;
   session.task=page.render({canvasContext:canvas.getContext('2d'),viewport,transform:ratio===1?null:[ratio,0,0,ratio,0,0]});
   try{await session.task.promise;}catch(e){if(pdfSessions.get(root)!==session)return;if(e.name!=='RenderingCancelledException')throw e;}
   if(mine!==token||!root.isConnected||pdfSessions.get(root)!==session)return;const text=await page.getTextContent();if(pdfSessions.get(root)!==session)return;pick('#pdf-text').textContent=text.items.map(v=>v.str).join(' ');root.dataset.ready='true';
  }
  const redraw=()=>draw().catch(e=>{if(root.isConnected)message('PDF 표시를 다시 확인해 주세요.');});
  const go=n=>{saved.page=Math.max(1,Math.min(pdf.numPages,n));redraw();};
  pick('#pdf-prev').onclick=()=>go(saved.page-1);pick('#pdf-next').onclick=()=>go(saved.page+1);pick('#pdf-page').onchange=e=>go(Number(e.target.value)||1);
  const zoom=change=>{saved.fit=false;saved.zoom=Math.max(.4,Math.min(2.5,(saved.zoom||1)+change));redraw();};
  pick('#pdf-minus').onclick=()=>zoom(-.2);pick('#pdf-plus').onclick=()=>zoom(.2);pick('#pdf-fit').onclick=()=>{saved.fit=true;redraw();};
  root.querySelector('.pdf-canvas-wrap').onkeydown=e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();go(saved.page+(e.key==='ArrowRight'?1:-1));}};
  const outline=await pdf.getOutline();if(!root.isConnected||pdfSessions.get(root)!==session)return;
  for(const item of outline||[]){const b=document.createElement('button');b.type='button';b.textContent=item.title;b.onclick=async()=>{const dest=typeof item.dest==='string'?await pdf.getDestination(item.dest):item.dest;if(dest)go((await pdf.getPageIndex(dest[0]))+1);};pick('#pdf-outline-items').append(b);}
  pick('#pdf-search-form').onsubmit=async e=>{e.preventDefault();const q=pick('#pdf-search').value.trim().toLocaleLowerCase();const hits=[];if(!q)return;pick('#pdf-search-results').textContent='검색 중';for(let i=1;i<=Math.min(pdf.numPages,100);i++){if(!root.isConnected)return;const p=await pdf.getPage(i),text=(await p.getTextContent()).items.map(v=>v.str).join(' ');if(text.toLocaleLowerCase().includes(q))hits.push(i);}if(!root.isConnected)return;pick('#pdf-search-results').innerHTML=hits.length?hits.map(i=>'<button data-pdf-hit="'+i+'">'+i+'페이지</button>').join(''):'검색 결과 없음';root.querySelectorAll('[data-pdf-hit]').forEach(b=>b.onclick=()=>go(Number(b.dataset.pdfHit)));};
  const resize=new ResizeObserver(()=>{if(saved.fit&&root.isConnected)redraw();});resize.observe(root);session.resize=resize;
  await draw();
 }
 window.ResearchWorkspace={refresh,isMounted:()=>Boolean($('#research-workspace')),openDetail,timelineHTML};
})();
