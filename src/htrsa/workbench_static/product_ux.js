'use strict';
const productRoles=['manager','experiment_coordinator','analysis_planner_worker','verification_coordinator'];
const performanceKeys=['FAST','BALANCED','DEEP','MAX'];
const helpTopics={
 key:['API 키','모델 제공사에서 발급받은 인증 키입니다. 노출된 키는 제공사에서 폐기해 주세요.'],
 model:['모델 선택','역할별 설정을 바꾸지 않으면 모두 같은 모델을 사용합니다.'],
 performance:['연구 성능','검토할 자료와 후속 작업의 범위를 정합니다.'],
 budget:['예산','이번 작업의 최대 지출 한도입니다.'],
 adaptive:['예산 자동 조정','필수 작업을 남기고 추가 분석을 줄입니다.'],
 reasoning:['추론 수준','선택한 모델이 한 번의 판단에 사용할 숙고 수준을 정합니다.'],
 roles:['고급 모델 설정','역할에 사용할 모델을 고릅니다. 바꾸지 않으면 위에서 선택한 모델을 사용합니다.'],
 scope:['연구 범위','가설과 추가 검토의 횟수를 제한합니다. 필수 검증은 유지합니다.'],
 verification:['검증과 복구','분석 결과를 검사하고 실패한 분석을 다시 검증합니다.'],
 egress:['자료 전송','선택한 제공사에 보낼 자료를 정합니다.'],
 report:['보고서','검증된 결과와 근거, 한계를 PDF로 정리합니다.'],
 settings:['연구 설정 변경','이후 작업에 적용할 설정을 바꿉니다. 이전 결과는 보존합니다.'],
 readiness:['실행 환경','현재 작업에 필요한 환경과 검사 상태를 확인합니다.'],
 search:['웹 검색 시도 상한','이 연구에서 검색을 시도할 수 있는 최대 횟수입니다.'],
 attachments:['자료 첨부','연구에 사용할 파일을 추가하거나 삭제할 수 있습니다.']
};
function helpButton(topic){return `<button type="button" class="context-help" data-help="${topic}" aria-label="${esc(helpTopics[topic][0])} 도움말">?</button>`;}
function bindHelp(root=document){root.querySelectorAll('[data-help]').forEach(b=>b.onclick=()=>showHelp(b.dataset.help));}
function showHelp(topic='readiness'){
 const v=helpTopics[topic];
 inspect('도움말',`<label>주제<select id="help-topic">${Object.entries(helpTopics).map(([k,x])=>`<option value="${k}" ${k===topic?'selected':''}>${esc(x[0])}</option>`).join('')}</select></label><section class="help-card"><h3>${esc(v[0])}</h3><p>${esc(v[1])}</p></section>`);
 $('#help-topic').onchange=e=>showHelp(e.target.value);
}
$('#help-open').onclick=()=>showHelp('readiness');
function modelOptions(selected='',all=false){const rows=app.settings.catalog.models.filter(m=>{const c=app.settings.connections.find(c=>c.connection_id===m.connection_id);return m.profile_id&&c?.enabled&&c.destination_approved&&!['UNAVAILABLE','DEPRECATED'].includes(m.status);});return Object.entries(app.settings.providers).map(([provider,d])=>{const group=rows.filter(m=>m.provider===provider);return group.length?`<optgroup label="${esc(d.name)}">${group.map(m=>`<option value="${esc(m.profile_id)}" ${m.profile_id===selected?'selected':''}>${esc(m.display_name)}</option>`).join('')}</optgroup>`:'';}).join('');}
function onboardingHTML(v){return `<ol class="onboarding-steps" aria-label="첫 연구 준비 단계">${(v?.steps||[]).map((s,i)=>`<li><span class="step-number">${i+1}</span><strong>${esc(s.title)}</strong><span class="step-status ${s.complete?'good':'warn'}">${s.complete?'확인됨':'확인 필요'}</span></li>`).join('')}</ol><div class="setup-status" role="status"><span>GPT 종합 검사 · ${v?.gpt_connectivity==='VALIDATED'?'검사 통과':'검증 전'}</span><span>연구 정확성 · 검증 전</span></div>`;}

function modelSourceHTML(value){try{const u=new URL(value);if(u.protocol==='https:'&&['developers.openai.com','platform.openai.com','platform.claude.com','ai.google.dev','docs.x.ai','api-docs.deepseek.com','docs.mistral.ai'].includes(u.hostname)&&!u.username&&!u.password)return `<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">공식 모델 안내</a>`;}catch{}return '<span class="muted">저장된 검사 기록</span>';}
const performanceNames={FAST:'low',BALANCED:'medium',DEEP:'high',MAX:'max'};
function apiBadge(level){return level?`<span class="api-use-badge" data-level="${level}">${({low:'낮음',medium:'중간',high:'높음'})[level]}</span>`:'';}
function performanceControl(value='BALANCED'){
 return `<label for="performance"><span class="setting-heading">연구 성능 <span id="performance-badge">${apiBadge('medium')}</span>${helpButton('performance')}</span></label><input id="performance" name="performance" type="range" min="0" max="3" step="1" value="${Math.max(0,performanceKeys.indexOf(value))}" aria-describedby="performance-detail"><output id="performance-detail" for="performance"></output>`;
}
function bindPerformance(form){
 const range=form.elements.performance;
 function update(){
  const main=performanceKeys[Number(range.value)],effective=form.elements.advanced_performance_profile?.value||main;
  range.setAttribute('aria-valuetext',performanceNames[main]);
  form.querySelector('#performance-detail').textContent=performanceNames[main];
  form.querySelector('#performance-badge').innerHTML=apiBadge(effective==='FAST'?'low':effective==='BALANCED'?'medium':'high');
  const custom=form.elements.advanced_performance_profile?.value||form.elements.model_reasoning?.value||form.elements.manual_role_override?.checked;
  const marker=form.querySelector('#preset-customized');if(marker)marker.textContent=custom?'고급 설정 적용 중':'';
 }
 range.addEventListener('input',update);form.addEventListener('input',update);form.addEventListener('change',update);update();
}
function costHint(){return '';}
function searchControl(s={}){
 return `<label>웹 검색<select name="search_policy">${[['AUTO','자동'],['DISABLED','사용 안 함'],['ALLOWED','허용']].map(([k,v])=>`<option value="${k}" ${(s.search_policy??'AUTO')===k?'selected':''}>${v}</option>`).join('')}</select></label>`;
}
function advancedFields(s={},mutable=false){
 const groups=[['최상위',['manager']],['중간',['experiment_coordinator','verification_coordinator']],['하위',['analysis_planner_worker']]];
 return `<details id="research-advanced"><summary>고급 설정 <span id="preset-customized" role="status"></span></summary>
 <fieldset><legend>연구 범위</legend><label>연구 성능<select name="advanced_performance_profile"><option value="">위 설정 사용</option>${performanceKeys.map(k=>`<option value="${k}" ${s.advanced_performance_profile===k?'selected':''}>${performanceNames[k]}</option>`).join('')}</select></label><label>추가 분석 횟수<input name="max_followups" type="number" min="0" max="2" value="${s.max_followups??1}"></label><label>최대 실행 시간 (초)<input type="number" name="max_elapsed_sec" min="10" max="3600" value="${s.max_elapsed_sec??300}"></label></fieldset>
 <fieldset><legend>모델 구성 ${helpButton('roles')}</legend><label>추론 수준<select name="model_reasoning"><option value="">모델 기본 설정</option>${['AUTO','LOW','MEDIUM','HIGH','MAX'].map(k=>`<option value="${k}" ${s.model_reasoning===k?'selected':''}>${k==='AUTO'?'제공사 기본값':k.toLowerCase()}</option>`).join('')}</select></label>
 <label class="check"><input type="checkbox" name="manual_role_override" ${s.manual_role_override?'checked':''}>단계별 모델 지정</label>
 <div id="role-models" ${s.manual_role_override?'':'hidden'}>${groups.map(([title,roles])=>`<section class="tier-group"><h4>${title}</h4>${roles.map(r=>`<label>${labels[r]}<select name="${r}"><option value="">선택한 모델 사용</option>${modelOptions(s.routing?.[r],true)}</select></label><label>추론<select name="reasoning_${r}"><option value="">위 추론 설정 사용</option>${['AUTO','LOW','MEDIUM','HIGH','MAX'].map(k=>`<option value="${k}" ${s.role_reasoning?.[r]===k?'selected':''}>${k==='AUTO'?'제공사 기본값':k.toLowerCase()}</option>`).join('')}</select></label>`).join('')}</section>`).join('')}</div>
 <label>분석 모델 자동 변경<select name="approved_worker"><option value="">변경 안 함</option>${modelOptions((s.approved_worker_profiles||[])[0],true)}</select></label><button type="button" id="research-model-add">모델 상세 설정</button></fieldset>
 <fieldset><legend>검증 · 가벼운 순서</legend>${[['verified_analysis_skills','분석 도구 검사','고정 분석 도구로 결과와 출처를 검사합니다.'],['ridge_arithmetic_check','Ridge 수치 검사','계수·절편·예측값을 다시 계산합니다. F3-P가 필요합니다.'],['verification_repair','F3-P 오류 복구','실패한 분석을 다시 실행하고 검증합니다.']].map(([k,title,detail])=>`<label class="check verification-option"><input name="${k}" type="checkbox" ${s[k]?'checked':''} ${mutable?'disabled':''}><span>${title}<small>${detail}</small></span></label>`).join('')}</fieldset>
 <fieldset><legend>문헌과 자료</legend><label>검색·자료 요청 상한 ${helpButton('search')}<input name="search_attempt_limit" type="number" min="0" max="20" step="1" value="${s.search_attempt_limit??app.settings.defaults.search_attempt_limit??10}"></label><label class="check"><input name="search_required" type="checkbox" ${s.search_required===true?'checked':''}>근거가 없으면 연구 중단</label>
 <label class="check"><input name="fulltext_enabled" type="checkbox" ${s.fulltext_enabled?'checked':''}>공개 PDF 자동 수집 · 최대 3건</label><label class="check"><input name="openalex_archive_enabled" type="checkbox" ${s.openalex_archive_enabled?'checked':''}>OpenAlex 원문 저장소 사용 · 무료 키 필요</label><button type="button" id="openalex-key">OpenAlex 키 관리</button><label>SearXNG 서버 · 선택<input name="searxng_url" type="url" value="${esc(s.searxng_url??'')}" placeholder="https://search.example.org"></label>
 <label>모델에 보낼 자료<select name="egress"><option value="none">질문만</option><option value="selected" ${s.egress==='selected'?'selected':''}>선택한 분석 자료</option><option value="research" ${s.egress==='research'?'selected':''}>현재 연구 자료 · 공개 검색어 포함</option></select></label><label>검색어 직접 지정 · 선택<input name="public_search_query" maxlength="500" value="${esc(s.public_search_query??'')}" placeholder="비워 두면 AI가 자동으로 검색합니다"></label><input type="hidden" name="public_search_consent" value="true"></fieldset>
 <button type="button" id="reset-recommended">고급 설정 초기화</button></details>`;
}

function bindAdvanced(form){
 form.querySelector('#openalex-key').onclick=()=>{if(form.id==='research-form')rememberDraft(form);editor('OpenAlex 무료 키',`<form id="openalex-key-form"><a href="https://openalex.org/settings/api" target="_blank" rel="noopener noreferrer">키 발급</a><label>새 키<input name="value" type="password" autocomplete="off" required maxlength="512"></label><div class="inline-actions"><button class="primary">저장</button><button id="openalex-delete" type="button">삭제</button></div></form>`);const keyForm=$('#openalex-key-form');keyForm.onsubmit=async e=>{e.preventDefault();let value=keyForm.elements.value.value;keyForm.elements.value.value='';try{const pending=api('/api/control/search/credential',{value});value=null;await pending;$('#editor').close();app.settings=null;message('키를 저장했습니다.','success');}catch(e){message(e.message);}finally{value=null;}};$('#openalex-delete').onclick=async()=>{keyForm.elements.value.value='';try{await api('/api/control/search/credential',{delete:true});$('#editor').close();app.settings=null;message('저장된 키를 삭제했습니다.','success');}catch(e){message(e.message);}};};
 form.elements.manual_role_override.onchange=()=>{form.querySelector('#role-models').hidden=!form.elements.manual_role_override.checked;};
 form.querySelector('#reset-recommended').onclick=()=>{
  form.elements.advanced_performance_profile.value='';form.elements.model_reasoning.value='';
  form.elements.manual_role_override.checked=false;form.querySelector('#role-models').hidden=true;
  productRoles.forEach(r=>{form.elements[r].value='';form.elements['reasoning_'+r].value='';});
  form.dispatchEvent(new Event('input',{bubbles:true}));
 };
 form.querySelector('#research-model-add').onclick=()=>{if(form.id==='research-form')rememberDraft(form);modelForm(app.settings.models.find(m=>m.profile_id===form.elements.model_profile_id.value));};
 if(form.elements.verification_repair&&!form.elements.verification_repair.disabled){
  const sync=()=>{form.elements.ridge_arithmetic_check.disabled=!form.elements.verification_repair.checked;if(!form.elements.verification_repair.checked)form.elements.ridge_arithmetic_check.checked=false;};
  form.elements.verification_repair.addEventListener('change',sync);sync();
 }
 bindHelp(form);bindReasoning(form);
}
function readForm(form){
 const r=Object.fromEntries(new FormData(form));
 const data={performance_profile:performanceKeys[Number(r.performance)],advanced_performance_profile:r.advanced_performance_profile||null,
  model_reasoning:r.model_reasoning||null,run_limit_usd:r.run_limit_usd,adaptive_budget:form.elements.adaptive_budget.checked,
  manual_role_override:form.elements.manual_role_override.checked,model_profile_id:r.model_profile_id||null,role_reasoning:{},routing:{},
  max_elapsed_sec:Number(r.max_elapsed_sec),egress:r.egress||'none',report_format:'pdf',search_policy:r.search_policy??'AUTO',
  public_search_query:r.public_search_query??'',public_search_consent:r.public_search_consent==='true',search_required:form.elements.search_required.checked,
  search_attempt_limit:Number(r.search_attempt_limit),max_followups:Number(r.max_followups),
  fulltext_enabled:form.elements.fulltext_enabled?.checked??false,openalex_archive_enabled:form.elements.openalex_archive_enabled?.checked??false,searxng_url:r.searxng_url||null,
  approved_worker_profiles:r.approved_worker?[r.approved_worker]:[]};
 productRoles.forEach(k=>{if(form.elements.manual_role_override.checked&&r[k])data.routing[k]=r[k];if(r['reasoning_'+k])data.role_reasoning[k]=r['reasoning_'+k];});
 return data;
}
function rememberDraft(form){
 const question=form.elements.question?.value??app.researchDraft?.question??'';
  app.researchDraft={...readForm(form),question,title:form.elements.title?.value.trim()||question.slice(0,100),settings_version:2,beginner_mode:true,research_profile_mode:'AUTO',ai_report_enabled:true,
  draft_id:form.dataset.draftId,submission_key:form.dataset.submissionKey,draft_revision:Number(form.dataset.draftRevision||0),
  selected_model_pool:JSON.parse(form.dataset.modelPool||'[]'),attachments:(app.researchAttachments||[]).filter(x=>x.attachment_id&&!x.removed&&['READ','OPAQUE','ATTACHED'].includes(x.status)).map(x=>x.attachment_id)};
 if(form.readDetailedDesign)app.researchDraft.detailed_design=form.readDetailedDesign();
}
function connectionAvailable(connection){
 return Boolean(connection?.enabled&&(connection.credential?.configured||connection.credential?.active_source==='not_required'));
}
function connectionConfig(connection){
 const {credential,compatibility,revision,...value}=connection;return value;
}
function researchModelPicker(form){
 const candidates=app.settings.catalog.models.filter(m=>!['UNAVAILABLE','DEPRECATED'].includes(m.status)&&(!m.profile_id||app.settings.models.some(x=>x.profile_id===m.profile_id))).map(model=>{
  const m=model,connection=app.settings.connections.find(c=>(m.connection_id?c.connection_id===m.connection_id:c.adapter_id===m.provider)&&connectionAvailable(c));
  return {model,connection};
 });
 const models=candidates.filter(({model,connection})=>{
  if(!model.catalog_curated)return true;
  const matching=candidates.filter(x=>x.model.provider===model.provider&&x.model.model_id===model.model_id);
  return model.profile_id?Boolean(connection)||!matching.some(x=>x.connection):!matching.some(x=>x.model.profile_id&&(x.connection||!connection));
 });
 const host=form.querySelector('#research-model-picker'),nodes=new Map(),selected=[],pending=new Set(),modelTasks=new Map(),connectionTasks=new Map();let expanded=false;
 const saved=app.researchDraft?.selected_model_pool||[],main=form.elements.model_profile_id.value;
 models.forEach(({model:m,connection})=>{
  const key=m.profile_id||m.provider+'|'+m.model_id+'|'+(m.connection_id||'');
  if(nodes.has(key))return;
  const active=Boolean(connection);
  const row=document.createElement('label');row.className='model-option';row.dataset.active=String(active);row.dataset.modelKey=key;
  row.innerHTML=`<input type="checkbox" name="catalog-model" value="${esc(key)}" ${active?'':'disabled'}><span>${esc(m.display_name)} <span class="model-activation ${active?'active':'inactive'}">${active?'활성화':'비활성화'}</span></span><small>${esc(app.settings.providers[m.provider]?.name||m.provider)}</small>`;
  nodes.set(key,{row,model:m,connection});
 });
 for(const id of [...saved,main].filter(Boolean)){
  const entry=[...nodes.entries()].find(([,x])=>x.model.profile_id===id&&x.connection);if(entry&&!selected.includes(entry[0]))selected.push(entry[0]);
 }
 function syncRoleOptions(){
  const primary=app.settings.models.find(m=>m.profile_id===form.elements.model_profile_id.value);
  for(const role of productRoles){
   const select=form.elements[role],value=select.value;
   select.innerHTML=`<option value="">선택한 모델 사용 — ${esc(primary?.display_name||primary?.model_id||'모델 미선택')}</option>${modelOptions(value,true)}`;
   select.value=value;
  }
 }
 function draw(){
  if(!host.isConnected)return;
  const focused=document.activeElement?.name==='catalog-model'?document.activeElement.value:null;
  const moreFocused=document.activeElement?.id==='more-models';
  host.replaceChildren();const pinned=document.createElement('fieldset');pinned.className='model-list';pinned.id='selected-models';host.append(pinned);
  if(!expanded)selected.forEach(key=>pinned.append(nodes.get(key).row));pinned.hidden=expanded||!selected.length;
  const unselected=[...nodes].filter(([key])=>!selected.includes(key));
  if(expanded){
   for(const [provider,d]of Object.entries(app.settings.providers)){
    const group=[...nodes].filter(([,x])=>x.model.provider===provider);if(!group.length)continue;
    const field=document.createElement('fieldset');field.className='model-list provider-group';field.dataset.provider=provider;
    const title=document.createElement('legend');title.textContent=d.name;field.append(title);group.forEach(([,x])=>field.append(x.row));host.append(field);
   }
  }else{
   const field=document.createElement('fieldset');field.className='model-list';field.id='featured-models';
   unselected.filter(([,x])=>x.model.featured_order).sort((a,b)=>a[1].model.featured_order-b[1].model.featured_order).slice(0,5).forEach(([,x])=>field.append(x.row));host.append(field);
  }
  const more=document.createElement('button');more.type='button';more.id='more-models';more.textContent=expanded?'접기':'더보기';more.setAttribute('aria-expanded',String(expanded));more.onclick=()=>{expanded=!expanded;draw();};host.append(more);
  nodes.forEach((x,key)=>x.row.querySelector('input').checked=selected.includes(key));
  const pool=selected.map(key=>nodes.get(key).model.profile_id).filter(Boolean);
  if(!pool.includes(form.elements.model_profile_id.value))form.elements.model_profile_id.value=pool[0]||'';
  form.dataset.modelPool=JSON.stringify(pool);syncRoleOptions();form.elements.model_profile_id.dispatchEvent(new Event('change'));
  if(focused&&nodes.has(focused)&&nodes.get(focused).row.isConnected)nodes.get(focused).row.querySelector('input').focus({preventScroll:true});
  else if(focused||moreFocused)more.focus({preventScroll:true});
 }
 function prepareConnection(connection){
  if(connection.destination_approved)return Promise.resolve();
  if(connectionTasks.has(connection.connection_id))return connectionTasks.get(connection.connection_id);
  const task=(async()=>{
   const value=await api('/api/control/connections',{value:{...connectionConfig(connection),destination_approved:true},expected_revision:connection.revision});
   connection.destination_approved=true;connection.revision=value.revision;
  })();
  connectionTasks.set(connection.connection_id,task);
  task.finally(()=>connectionTasks.delete(connection.connection_id)).catch(()=>{});return task;
 }
 function prepareModel(key){
  const x=nodes.get(key);if(modelTasks.has(key))return modelTasks.get(key);
  if(!x.connection)return Promise.reject(Object.assign(Error('CONNECTION_REQUIRED'),{code:'CONNECTION_REQUIRED'}));
  if(x.model.profile_id&&x.connection.destination_approved&&!x.error)return Promise.resolve();
  const task=(async()=>{
   try{
    await prepareConnection(x.connection);
    if(!x.model.profile_id){
     const value=await api('/api/control/catalog/resolve',{provider:x.model.provider,model_id:x.model.model_id,connection_id:x.connection.connection_id});
     x.model.profile_id=value.profile_id;
    }
    x.error=null;app.settings=await api('/api/control/settings');
   }catch(error){x.error=error.code||error.message;researchError(form,x.error);throw error;}
   finally{draw();form.dispatchEvent(new Event('input',{bubbles:true}));}
  })();
  modelTasks.set(key,task);pending.add(task);
  task.finally(()=>{modelTasks.delete(key);pending.delete(task);}).catch(()=>{});return task;
 }
 nodes.forEach((x,key)=>x.row.querySelector('input').onchange=()=>{
  if(x.row.querySelector('input').checked){if(!selected.includes(key))selected.push(key);}else{const at=selected.indexOf(key);if(at>=0)selected.splice(at,1);}
  draw();form.dispatchEvent(new Event('input',{bubbles:true}));
  if(selected.includes(key))prepareModel(key).catch(()=>{});
 });
 form.modelReady=async()=>{await Promise.all(selected.map(prepareModel));await Promise.all([...pending]);const error=selected.map(k=>nodes.get(k).error).find(Boolean);if(error)throw Object.assign(Error(error),{code:error});};
 draw();
}
function researchError(form,code){
 if(!form.isConnected)return;
 const first=(Array.isArray(code)?code:[code])[0]||'INVALID_REQUEST';
 const searchNames={SEARCH_EGRESS_DENIED:'고급 설정의 자료 전송 범위를 확인해 주세요.',SEARCH_QUERY_REQUIRED:'연구 질문을 입력해 주세요.',SEARCH_REQUIRED_BUT_DISABLED:'웹 검색을 켜거나 필수 검색을 해제해 주세요.',SEARCH_ATTEMPT_LIMIT:'검색 횟수를 늘려 주세요.',SEARCH_PRIVATE_QUERY_BLOCKED:'질문이나 직접 지정한 검색어에서 개인정보·비밀값을 제외해 주세요.',COMPLETION_RESERVE_BLOCKED:'계획과 보고서 작성 예산이 부족합니다.'};
 const location={CREDENTIAL_UNCONFIGURED:'api',CONNECTION_REQUIRED:'api',CONNECTION_SELECTION_REQUIRED:'api',PRIMARY_MODEL_REQUIRED:'model',ROLE_MODELS_REQUIRED:'model',BUDGET_CAP_REQUIRED:'budget',PRICE_REQUIRED:'model',CAPABILITY_NOT_VALIDATED:'model',REASONING_UNSUPPORTED:'advanced',COMPLETION_RESERVE_BLOCKED:'budget'};
 if(first.startsWith('SEARCH_')||first==='PRICE_UNKNOWN')location[first]='search';
 const names={api:'API 연결 설정',model:first==='PRICE_REQUIRED'?'단가 확인':'모델 설정',budget:'예산 입력',advanced:'고급 설정',search:'검색 설정'};
 const target=location[first];form.setResearchPage?.(target==='advanced'?2:target?1:2);
 form.querySelector('#research-preflight').innerHTML=`<p role="alert">${esc(searchNames[first]||errors[first]||first)} ${target?`<button type="button" data-fix="${target}">${names[target]}</button>`:''}</p>`;
 form.querySelector('[data-fix]')?.addEventListener('click',()=>{
  if(first==='PRICE_REQUIRED')form.checkPreparation().catch(x=>researchError(form,x.code||x.message));
  else if(target==='api'){rememberDraft(form);$('#editor').close();app.view='settings';app.settingsPane='connections';render();}
  else if(target==='budget')form.elements.run_limit_usd.focus();
  else if(target==='advanced')form.querySelector('#research-advanced').open=true;
  else if(target==='search'){const name=first==='SEARCH_EGRESS_DENIED'?'egress':first==='SEARCH_QUERY_REQUIRED'?'question':first==='SEARCH_PRIVATE_QUERY_BLOCKED'?'public_search_query':first==='SEARCH_ATTEMPT_LIMIT'?'search_attempt_limit':'search_policy';form.setResearchPage?.(name==='question'?0:name==='search_policy'?1:2);if(!['question','search_policy'].includes(name))form.querySelector('#research-advanced').open=true;form.elements[name].scrollIntoView({block:'center'});form.elements[name].focus();}
  else form.querySelector('#research-model-picker').scrollIntoView({block:'center'});
 });
}
newResearch=async function({isCurrent=()=>true}={}){
 await app.researchDraftSave;
 if(!isCurrent())return;
 const settings=await api('/api/control/settings');
 if(!isCurrent())return;
 app.settings=settings;
 if(!app.researchDraft){const saved=await api('/api/control/research/draft');app.researchDraft=saved.draft;app.draftStoreRevision=saved.revision;}
 if(!isCurrent())return;
 const pref=app.settings.preferences||{},draft=app.researchDraft||{},defaults={egress:'selected',...pref,search_required:false,...draft};
 const draftId=draft.draft_id||crypto.randomUUID(),submission=draft.submission_key||crypto.randomUUID();
 if(!app.researchAttachments)app.researchAttachments=await api('/api/control/attachments?draft_id='+encodeURIComponent(draftId));
 if(!isCurrent())return;
 const budget=draft.run_limit_usd??String(Math.min(0.10,Number(app.settings.defaults.request_limit_usd)));
 if(!defaults.model_profile_id){const usable=app.settings.catalog.models.filter(m=>m.profile_id&&m.operational);const candidate=usable.find(m=>m.provider==='openai')||(new Set(usable.map(m=>m.connection_id)).size===1?usable[0]:null);if(candidate)defaults.model_profile_id=candidate.profile_id;}
 editor('새 연구',`<form id="research-form" data-draft-id="${esc(draftId)}" data-submission-key="${esc(submission)}" data-draft-revision="${draft.draft_revision??0}">
 <label>연구 주제<input name="title" maxlength="200" value="${esc(draft.title||'')}"></label>
 <label>연구 질문<textarea name="question" required maxlength="3000">${esc(draft.question||'')}</textarea></label>
 ${performanceControl(defaults.performance_profile||'BALANCED')}<label>모델 ${helpButton('model')}</label><input type="hidden" name="model_profile_id" value="${esc(defaults.model_profile_id||'')}"><div id="research-model-picker"></div>
 ${searchControl(defaults)}<label>이번 작업 예산 USD(미화 달러)<input type="number" name="run_limit_usd" min="0.000001" max="100" step="any" value="${esc(budget)}" required></label>
 <label class="check"><input name="adaptive_budget" type="checkbox" ${defaults.adaptive_budget===false?'':'checked'}>예산에 맞춰 자동 조정</label>
 <label>자료 첨부 (선택) ${helpButton('attachments')}<input name="attachment_files" type="file" multiple></label><div id="attachment-list" role="status"></div>
 ${advancedFields(defaults)}<div id="research-preflight" role="status"></div><div class="inline-actions"><button type="button" id="research-smoke">연결 확인 · API 호출</button><button type="button" id="quick-recommended">기본 설정 적용</button><button class="primary" id="research-start">질문 전송 · 연구 시작</button></div></form>`);
 const f=$('#research-form');let pending=false,timer=null,save=Promise.resolve();
 researchModelPicker(f);bindPerformance(f);bindAdvanced(f);bindAttachments(f);
 function snapshot(){rememberDraft(f);return {...app.researchDraft};}
 function requestData(){const data=snapshot();data.question_only=data.egress==='none';if(data.question_only)data.egress='selected';return data;}
 async function showPreparation(check){
  if(!f.isConnected)return;
  if(check.ready){f.querySelector('#research-preflight').innerHTML='<p class="preflight-ready">시작 준비 완료</p>';return;}
  researchError(f,check.first_blocker||check.reasons);
  if(check.first_blocker!=='PRICE_REQUIRED'){
   const issue=check.issues?.find(x=>x.code===check.first_blocker);
   if(issue&&!errors[check.first_blocker])f.querySelector('#research-preflight [role=alert]').textContent=issue.message+' '+(issue.next_step||'');
   return;
  }
  const area=f.querySelector('#research-preflight');
  area.innerHTML='<p>사용할 모델의 단가를 확인해 주세요.</p>';
  for(const identity of check.price_required_profiles||[f.elements.model_profile_id.value]){
   const quote=await api('/api/control/models/'+encodeURIComponent(identity)+'/pricing');
   if(!area.isConnected)return;
   if(!quote.required)continue;
   const model=app.settings.models.find(m=>m.profile_id===identity),candidate=quote.candidate;
   const row=document.createElement('div');row.className='research-price';
   row.innerHTML='<strong>'+esc(model?.display_name||model?.model_id||identity)+'</strong>';
   area.append(row);
   if(!candidate){row.insertAdjacentHTML('beforeend','<p>공식 단가를 찾지 못했습니다. 모델 상세 설정에서 단가를 확인해 주세요.</p><button type="button">모델 상세 설정</button>');row.querySelector('button').onclick=async()=>{try{await queueSave();modelForm(model);}catch(x){researchError(f,x.code||x.message);}};continue;}
   row.insertAdjacentHTML('beforeend','<p>백만 토큰당 입력 $'+esc(candidate.input_per_million)+' · 출력 $'+esc(candidate.output_per_million)+' <a href="'+esc(candidate.source)+'" target="_blank" rel="noopener noreferrer">가격 출처</a></p><button type="button" data-apply-research-price>단가 적용</button>');
   row.querySelector('button').onclick=async e=>{
    const button=e.currentTarget;button.disabled=true;
    try{await api('/api/control/models/'+encodeURIComponent(identity)+'/pricing',{approve_price:true,expected_revision:quote.expected_revision,quote_id:quote.quote_id});app.settings=await api('/api/control/settings');await f.checkPreparation();message('단가를 적용했습니다.','success');}
    catch(x){researchError(f,x.code||x.message);}finally{button.disabled=false;}
   };
  }
 }
 f.checkPreparation=async()=>{await f.modelReady();const check=await api('/api/control/research/preflight',requestData());await showPreparation(check);return check;};
 function queueSave(){const value=snapshot();const next=(app.researchDraftSave||Promise.resolve()).then(async()=>{const result=await api('/api/control/research/draft',{draft:value,expected_revision:app.draftStoreRevision||0});app.draftStoreRevision=result.revision;});save=next.catch(x=>{if(f.isConnected)researchError(f,x.code||x.message);});app.researchDraftSave=save;return next;}
 f.flushDraft=()=>{clearTimeout(timer);if(!pending&&!f.dataset.submitted)queueSave().catch(()=>{});};
 f.addEventListener('input',()=>{if(!f.isConnected)return;f.dataset.draftRevision=String(Number(f.dataset.draftRevision)+1);snapshot();clearTimeout(timer);timer=setTimeout(()=>queueSave().catch(()=>{}),350);});
 f.querySelector('#quick-recommended').onclick=()=>{f.elements.performance.value=1;f.elements.performance.dispatchEvent(new Event('input',{bubbles:true}));};
 f.querySelector('#research-smoke').onclick=async()=>{
  if(pending)return;pending=true;
  try{await f.modelReady();const cap=f.elements.run_limit_usd.value;if(!cap)throw Object.assign(Error('BUDGET_CAP_REQUIRED'),{code:'BUDGET_CAP_REQUIRED'});
   const profile=f.elements.model_profile_id.value;if(!profile)throw Object.assign(Error('PRIMARY_MODEL_REQUIRED'),{code:'PRIMARY_MODEL_REQUIRED'});
   const pricing=await api('/api/control/models/'+encodeURIComponent(profile)+'/pricing');
   if(pricing.required){await showPreparation({ready:false,first_blocker:'PRICE_REQUIRED',price_required_profiles:[profile]});return;}
   await api('/api/control/models/'+encodeURIComponent(profile)+'/check',{mode:'text',consent:true,budget_cap_usd:cap});
   app.settings=await api('/api/control/settings');await f.checkPreparation();message('AI 응답을 확인했습니다.','success');
  }catch(x){researchError(f,x.code||x.message);}finally{pending=false;}
 };
 f.onsubmit=async e=>{
  e.preventDefault();if(pending)return;pending=true;const button=f.querySelector('#research-start');button.disabled=true;clearTimeout(timer);
  try{
   await f.modelReady();await f.uploadReady();await queueSave();
   const data=requestData();
   const check=await api('/api/control/research/preflight',data);
   if(!check.ready){await showPreparation(check);return;}
   const value=await api('/api/control/research',data);
   if(!value.preflight.ready){researchError(f,value.preflight.first_blocker||value.preflight.reasons);return;}
   await api('/api/control/research/'+value.research_id+'/start',{idempotency_key:submission+'-start',expected_version:0});
   const cleared=await api('/api/control/research/draft',{draft:{},expected_revision:app.draftStoreRevision||0});app.draftStoreRevision=cleared.revision;
   clearTimeout(timer);f.dataset.submitted='true';app.researchDraft=null;app.researchAttachments=null;app.researchFile=null;app.settings=null;$('#editor').close();
   app.view='research';app.rid=value.research_id;app.tab='overview';history.replaceState(null,'','#research/'+encodeURIComponent(value.research_id)+'/flow');await render();
  }catch(x){researchError(f,x.code||x.message);}finally{pending=false;button.disabled=false;}
 };
};
keyForm=function(id){const c=app.settings.connections.find(x=>x.connection_id===id);editor('API 키',`<form id="key-form"><label>새 키<input name="value" type="password" autocomplete="off" required maxlength="512"></label>${fields({'저장소':c?.credential?.storage_backend||'Windows 자격 증명 관리자 / 권한 보호 파일','현재 키 사용 위치':c?.credential?.active_source||'미설정','환경변수 우선':c?.credential?.shadowed_saved_key?'사용 중':'—'})}<button class="primary">키 저장</button><button id="key-delete" type="button">저장된 키 삭제</button>${helpButton('key')}</form>`);bindHelp($('#key-form'));$('#key-form').onsubmit=async e=>{e.preventDefault();const input=e.target.elements.value;let value=input.value;input.value='';try{const pending=api(`/api/control/connections/${encodeURIComponent(id)}/credential`,{value});value=null;const v=await pending;$('#editor').close();app.settings=null;await render();message('키를 저장했습니다.','success');}catch(x){message(x.message);}finally{input.value='';value=null;}};$('#key-delete').onclick=async()=>{$('#key-form input').value='';try{await api(`/api/control/connections/${encodeURIComponent(id)}/credential`,{delete:true});$('#editor').close();app.settings=null;await render();message('저장된 키를 삭제했습니다.','success');}catch(x){message(x.message);}};};
const legacySettings=renderSettings;
renderSettings=async function(epoch){await legacySettings(epoch);if(epoch!==app.epoch)return;$('#settings-2').remove();$('#settings-3').remove();$('#settings-0').remove();const p=app.settings.preferences;const node=document.createElement('section');node.id='resource-settings';node.innerHTML=`<h2>성능 최적화</h2><label>실행 환경<select id="low-spec-mode">${[['AUTO','자동 · 추천'],['LOW_SPEC','저사양'],['NORMAL','일반']].map(([k,v])=>`<option value="${k}" ${(p.low_spec_mode==='ON'?'LOW_SPEC':p.low_spec_mode==='OFF'?'NORMAL':p.low_spec_mode)===k?'selected':''}>${v}</option>`).join('')}<option value="ON" hidden>저사양 (이전 설정)</option><option value="OFF" hidden>일반 (이전 설정)</option></select></label>${app.settings.resource_policy?.disk_warning?'<p role="alert">저장 공간 부족</p>':''}`;$('#settings-4').after(node);$('#low-spec-mode').onchange=async e=>{try{await api('/api/control/preferences',{low_spec_mode:e.target.value});app.settings=null;await render();message('성능 최적화 설정을 저장했습니다.','success');}catch(x){message(x.message);}};};
const legacyList=renderList;
renderList=async function(epoch){await legacyList(epoch);if(epoch!==app.epoch)return;const s=app.settings,ready=s.onboarding?.gpt_connectivity==='VALIDATED';const p=document.createElement('section');p.className='panel';p.id='first-research';p.innerHTML=`<h2>${ready?'GPT 연결 검사 통과':'첫 연구 준비'}</h2>${onboardingHTML(s.onboarding)}<button id="onboarding-settings">API 연결 설정</button> ${helpButton('readiness')}`;$('#research-table').before(p);$('#onboarding-settings').onclick=()=>{app.view='settings';app.rid=null;render();};bindHelp(p);};
function reportHTML(v){return `<article class="friendly-report"><p>${v.ai_narrative==='READY'?'AI 작성 보고서':v.ai_narrative==='PARTIAL'?'부분 보고서 · AI 작성 미완료':'저장된 분석 결과로 작성한 보고서 · AI 작성 미사용'}</p>${v.titles.map((title,i)=>`<section><h2>${i+1}. ${esc(title)}</h2>${i===0?`<p>${esc(v.question)}</p><p>${tag(v.status)} · ${esc(state(v.stop_reason)||'진행 중')} · ${v.complete?'실행 종료':'부분 보고서'}</p>`:i===1?`<p class="report-conclusion">${esc(v.conclusion)}</p>`:i===2?table(['검증 지표','값'],v.display_numbers.map(n=>[esc(n.label),esc(n.value)]))+details(v.evidence_refs,'근거 출처'):i===3?table(['분석 방법'],v.analyses.map(a=>[esc(({pearson_correlation:'Pearson 상관 분석',spearman_correlation:'Spearman 순위 상관 분석',ridge_holdout:'Ridge 분리 평가',ridge_rolling_origin:'Ridge 시계열 순차 평가'})[a.method]||a.method)]))+details(v.analyses,'분석 계획 상세'):i===4?(v.images.length?v.images.map(img=>`<figure><img class="artifact-preview" src="${esc(img.url)}" alt="검증된 분석 그림"><figcaption>${esc(img.caption)}</figcaption></figure>`).join(''):'<p>검증된 그림 없음</p>'):i===5?`<p>${esc(v.comparison)}</p>`:i===6?'<p>출처·파일 무결성·필수 재검증을 통과한 기록만 포함합니다.</p>'+details(v.numbers,'수치 출처')+details(v.contradictions,'상충 근거'):i===7?Object.entries(v.limitations).map(([k,x])=>`<h3>${esc(k)}</h3><p>${esc(Array.isArray(x)?x.join(' · '):x)}</p>`).join(''):`<p>${esc(v.reproduction)}</p>`}</section>`).join('')}</article>`;}
async function friendlyReportHTML(rid,canonical){const v=await api(`/api/control/research/${rid}/report-view`);return `<div class="actions"><button data-pdf="${esc(rid)}">PDF 다운로드</button><button id="export">검증 후 내보내기</button></div>${reportHTML(v)}${semanticPanel(v.source_semantics)}`;}
async function requireCurrentReport(rid){
 const card=await api('/api/control/research/'+encodeURIComponent(rid)+'/conclusion-card');
 if(card.available&&!card.current)throw Error('현재 결론을 다시 확인해야 합니다. 계산 다시 확인하기를 눌러 주세요.');
}
async function currentPDFResponse(rid,preview=false){
 await requireCurrentReport(rid);
 const response=await fetch('/api/control/research/'+encodeURIComponent(rid)+(preview?'/report-preview':'/report.pdf'),{credentials:'same-origin',cache:'no-store'});
 if(!response.ok){const result=await response.json();throw Error(errors[result.error]||result.error);}
 if(!response.headers.get('content-type')?.startsWith(preview?'application/json':'application/pdf'))throw Error('PDF_RENDER_FAILED');
 return response;
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-pdf]');if(!button)return;button.disabled=true;
 try{const response=await currentPDFResponse(button.dataset.pdf);
  const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download='H-TRSA-'+button.dataset.pdf+'.pdf';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('PDF를 저장했습니다.','success');
 }catch(x){message(x.message);}finally{button.disabled=false;}
});

async function researchSettings(){const rid=encodeURIComponent(app.rid),v=await api(`/api/control/research/${rid}/settings`),budget=await api(`/api/control/research/${rid}/completion-budget`),s=v.effective;editor('연구 설정',`<form id="run-settings-form">${performanceControl(s.performance_profile||'BALANCED')}${searchControl(s)}<label>연구 예산 USD(미화 달러)<input name="run_limit_usd" type="number" min="0.01" max="100" step="0.01" value="${esc(s.run_limit_usd)}" required></label><label class="check"><input name="adaptive_budget" type="checkbox" ${s.adaptive_budget?'checked':''}>예산 자동 조정 ${helpButton('adaptive')}</label><label>이후 작업에 사용할 모델<select name="model_profile_id"><option value="">현재 설정 유지</option>${modelOptions(s.model_profile_id,true)}</select></label>${advancedFields(s,true)}<h3>연구 완료에 필요한 예산</h3>${fields({'남은 한도':usd(budget.available_usd),'완료에 필요한 예산':usd(budget.completion_reserve_usd),'미확정 비용':usd(budget.unsettled_exposure_usd),'상태':budget.status,'AI 보고서':budget.ai_narrative==='PLANNED'?'작성 비용 포함':'기존 로컬 보고서'})}${v.pending?details(v.pending,'설정 적용 상태'):''}${details(v.history,'변경 이력')}<button class="primary">설정 변경 요청</button></form><details><summary>분석 변경 · 새 계획 작성</summary><form id="plan-revision-form"><label>새 질문<textarea name="question" maxlength="3000" required>${esc(s.question)}</textarea></label><label>새 분석 계획 (JSON)<textarea name="analysis_plan" required></textarea></label><button>새 분석 계획으로 연구 만들기</button></form></details>`);const f=$('#run-settings-form');bindPerformance(f);bindAdvanced(f);f.onsubmit=async e=>{e.preventDefault();const value=readForm(f);try{await api(`/api/control/research/${rid}/settings`,{expected_version:v.expected_version,value});$('#editor').close();await render();message('설정 변경을 요청했습니다.','success');}catch(x){message(x.message);}};$('#plan-revision-form').onsubmit=async e=>{e.preventDefault();try{const plan=JSON.parse(e.target.analysis_plan.value);const result=await api(`/api/control/research/${rid}/analysis-plan-revision`,{expected_version:v.expected_version,question:e.target.question.value,analysis_plan:plan});$('#editor').close();app.rid=result.child_research_id;app.tab='overview';await render();message('새 분석 계획의 승인 대기 중입니다.','success');}catch(x){message(x instanceof SyntaxError?'분석 계획의 JSON 형식을 확인해 주세요.':x.message);}};}
Object.assign(errors,{COMPLETION_RESERVE_BLOCKED:'연구를 마칠 예산이 부족합니다. 예산을 늘리거나 추가 작업을 줄여 주세요.',REASONING_UNSUPPORTED:'이 모델은 선택한 추론 수준을 지원하지 않습니다. 지원하는 수준이나 기본값을 선택해 주세요.',CATALOG_OWNER_APPROVAL_REQUIRED:'제공사 주소와 모델 단가를 확인한 뒤 모델을 저장해 주세요.',CATALOG_RECHECK_REQUIRED:'모델 정보가 오래됐습니다. 공식 문서와 단가를 다시 확인해 주세요.',SECRET_PATH_UNSAFE:'키 저장 위치에 바로가기나 연결된 폴더를 사용할 수 없습니다.',ADAPTIVE_MODEL_NOT_APPROVED:'자동 변경에는 같은 제공사의 검사 통과 모델만 사용할 수 있습니다.'});
Object.assign(errors,{UPLOAD_INVALID:'CSV 파일 형식을 확인해 주세요.',UPLOAD_UTF8_REQUIRED:'UTF-8로 저장한 CSV를 선택해 주세요.',UPLOAD_SIZE_OR_CONTENT:'CSV는 최대 5MB까지 사용할 수 있습니다.',UPLOAD_CSV_INVALID:'열 이름과 행의 열 개수가 같은 CSV를 선택해 주세요.',SEARCH_REQUIRED_BUT_DISABLED:'필수 문헌 검색이 꺼져 있습니다.',SEARCH_QUERY_REQUIRED:'연구 질문을 입력해 주세요.',SEARCH_EGRESS_DENIED:'고급 설정의 자료 전송 범위를 확인해 주세요.',SEARCH_PRIVATE_QUERY_BLOCKED:'질문이나 검색어에 비공개 정보가 포함돼 전송을 차단했습니다.',SEARCH_REQUIRED_EVIDENCE_MISSING:'필수 문헌 근거를 확보하지 못했습니다.',LOW_SPEC_BUSY:'저사양 모드에서는 연구를 하나씩 실행합니다.',PRICE_UNKNOWN:'검색 단가가 확인되지 않아 전송을 차단했습니다.'});
Object.assign(displayNames,{AUTO:'자동',DISABLED:'사용 안 함',ALLOWED:'허용'});
Object.assign(errors,{SECRET_READBACK_FAILED:'저장한 키를 확인할 수 없습니다. 다시 등록해 주세요.',PAID_TEST_CONSENT_REQUIRED:'검사 항목과 예산 한도에 동의해 주세요.',CREDENTIAL_UNCONFIGURED:'API 키가 없습니다. 저장된 키나 환경변수를 확인해 주세요.',QUALIFICATION_PROVIDER_DENIED:'종합 검사는 GPT 또는 승인한 로컬 서버에서 사용할 수 있습니다.'});

function bindReasoning(form){
 function options(select,id){const model=app.settings.models.find(x=>x.profile_id===id);for(const option of select.options)option.disabled=Boolean(option.value&&option.value!=='AUTO'&&!(model?.reasoning_levels||[]).includes(option.value));}
 function update(){const primary=form.elements.model_profile_id.value;options(form.elements.model_reasoning,primary);productRoles.forEach(role=>options(form.elements['reasoning_'+role],form.elements[role].value||primary));}
 form.elements.model_profile_id.addEventListener('change',update);productRoles.forEach(role=>form.elements[role].addEventListener('change',update));update();
}

const attachmentStatus={QUEUED:'업로드 대기',RESERVED:'업로드 대기',UPLOADING:'업로드 중',ATTACHED:'첨부됨',READ:'내용 읽기 완료',OPAQUE:'직접 분석 미지원 — 원본 첨부',FAILED:'처리 실패',BLOCKED:'보안 정책으로 차단',DELETING:'삭제 중',DELETE_FAILED:'삭제 실패'};
function bindAttachments(form){
 app.refreshResearchAttachments=()=>{if(form.isConnected){draw();form.dispatchEvent(new Event('input',{bubbles:true}));}};
 const rows=app.researchAttachments||[],host=form.querySelector('#attachment-list');let chain=Promise.resolve();
 function draw(){
  if(!host.isConnected){app.refreshResearchAttachments?.();return;}
  host.innerHTML=rows.filter(r=>!r.removed).map(r=>`<div class="attachment-row" data-upload="${esc(r.local_id||r.attachment_id)}"><span>${esc(r.name)}<small>${esc(r.size_bytes)} 바이트 · ${esc(attachmentStatus[r.status]||r.status)}${r.processing_error?' · '+esc(errors[r.processing_error]||r.processing_error):''}</small></span><button type="button" data-delete-upload="${esc(r.local_id||r.attachment_id)}">삭제</button></div>`).join('');
  for(const item of rows.filter(value=>!value.removed&&value.processing_error)){
   const row=[...host.querySelectorAll('[data-upload]')].find(element=>element.dataset.upload===(item.local_id||item.attachment_id));
   window.HtrsaTutorial?.attachError(item.processing_error,row?.querySelector('span'));
  }
  host.querySelectorAll('[data-delete-upload]').forEach(b=>b.onclick=async()=>{
   const item=rows.find(r=>(r.local_id||r.attachment_id)===b.dataset.deleteUpload);item.removed=true;item.controller?.abort();draw();
   try{if(item.attachment_id){const result=await api('/api/control/attachments/'+item.attachment_id+'/delete',{draft_id:form.dataset.draftId});if(result.retained_for_provenance)message('첨부 목록에서 제거했습니다. 연구에서 사용한 원본은 보존합니다.','success');}}
   catch(x){item.removed=false;item.status='DELETE_FAILED';item.processing_error=x.code||x.message;draw();}
   form.dispatchEvent(new Event('input',{bubbles:true}));
  });
 }
 form.elements.attachment_files.onchange=()=>{
  const files=[...form.elements.attachment_files.files];form.elements.attachment_files.value='';
  for(const file of files){
   const item={local_id:crypto.randomUUID(),name:file.name,size_bytes:file.size,status:'QUEUED',removed:false};rows.push(item);draw();
   const task=chain.then(async()=>{
    if(item.removed)return;
    try{
     Object.assign(item,await api('/api/control/attachments/begin',{filename:file.name,size_bytes:file.size,mime:file.type,draft_id:form.dataset.draftId}));
     if(item.removed){await api('/api/control/attachments/'+item.attachment_id+'/delete',{draft_id:form.dataset.draftId});return;}
     item.status='UPLOADING';item.controller=new AbortController();draw();
     const response=await fetch('/api/control/attachments/'+item.attachment_id+'/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':app.csrf},credentials:'same-origin',body:file,signal:item.controller.signal});
     const value=await response.json();if(!response.ok)throw Object.assign(Error(value.error),{code:value.error});Object.assign(item,value);
     if(item.removed)await api('/api/control/attachments/'+item.attachment_id+'/delete',{draft_id:form.dataset.draftId});
    }catch(x){if(!item.removed){item.status='FAILED';item.processing_error=x.code||x.message;}}
    finally{item.controller=null;draw();form.dispatchEvent(new Event('input',{bubbles:true}));}
   });chain=task;
  }
 };
 form.uploadReady=async()=>{await chain;const bad=rows.find(r=>!r.removed&&!['READ','OPAQUE','ATTACHED'].includes(r.status));if(bad)throw Object.assign(Error('ATTACHMENT_NOT_READY'),{code:'ATTACHMENT_NOT_READY'});};
 draw();
}

Object.assign(errors,{PRIMARY_MODEL_REQUIRED:'연구에 사용할 모델을 선택해 주세요.',BUDGET_CAP_REQUIRED:'이번 작업의 예산 한도를 입력해 주세요.',CONNECTION_REQUIRED:'활성화된 모델을 선택해 주세요.',CONNECTION_SELECTION_REQUIRED:'사용할 API 연결을 지정해 주세요.',UPLOAD_FILENAME_INVALID:'파일 이름을 확인해 주세요.',UPLOAD_SIZE_OR_CONTENT:'파일은 각각 최대 5MB까지 첨부할 수 있습니다.',UPLOAD_PARSE_FAILED:'파일 내용을 읽을 수 없습니다. 형식을 확인하거나 파일을 삭제해 주세요.',UPLOAD_ACTIVE_CONTENT_BLOCKED:'실행 파일과 HTML·SVG는 첨부할 수 없습니다.',UPLOAD_BATCH_LIMIT:'한 번에 10개, 합계 20MB까지 첨부할 수 있습니다.',UPLOAD_DELETE_FAILED:'파일을 삭제하지 못했습니다. 다시 시도해 주세요.',ATTACHMENT_NOT_READY:'첨부 파일의 업로드나 처리를 확인해 주세요.',ATTACHMENT_OWNER_MISMATCH:'현재 초안의 첨부 파일만 사용할 수 있습니다.',PDF_RENDERER_UNAVAILABLE:'PDF 생성기를 사용할 수 없습니다.',PDF_KOREAN_FONT_REQUIRED:'PDF 생성에 필요한 한글 글꼴이 없습니다.',PDF_RENDER_FAILED:'PDF를 만들지 못했습니다.',SEARCH_ATTEMPT_LIMIT:'이 연구의 검색 시도 상한에 도달했습니다.'});
