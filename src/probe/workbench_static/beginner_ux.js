'use strict';

const beginnerStatus=value=>({DRAFT:'준비 중',PREFLIGHT_BLOCKED:'확인 필요',STARTING:'준비 중',RESUMING:'진행 중',RUNNING:'진행 중',ACTIVE:'진행 중',COMPLETED:'완료',PAUSED:'일시정지',STOPPED:'중단됨',FAILED:'확인 필요',BUDGET_BLOCKED:'확인 필요',NEEDS_RECONCILIATION:'확인 필요',INSUFFICIENT_DATA:'확인 필요'})[value]||'확인 필요';
const guidebook=window.ProbeTutorialContent.topics.map(topic=>[topic.title,topic.summary]);

showHelp=function(topic='readiness'){return window.ProbeTutorial.showHelp(topic);};
$('#help-open').onclick=()=>showHelp();

function beginnerRecovery(){
 inspect('문제가 생겼어요',`<ul class="recovery-actions"><li><button id="recover-ai">AI 연결 확인</button></li><li><button id="recover-internet">인터넷 확인</button><span id="internet-status" role="status"></span></li><li><button id="recover-model">모델 확인</button></li><li><button id="recover-file">파일 다시 선택</button></li><li><button id="recover-run">연구 계속하기</button></li></ul>`);
 const settings=()=>{$('#inspector').close();app.view='settings';app.settingsPane='connections';app.developerSettings=false;app.rid=null;render();};
 $('#recover-ai').onclick=settings;$('#recover-model').onclick=settings;
 $('#recover-internet').onclick=()=>{$('#internet-status').textContent=navigator.onLine?'브라우저가 연결 상태를 감지했습니다. 제공사 접속 여부는 AI 연결 확인으로 검사해 주세요.':'인터넷 연결을 확인해 주세요.';};
 $('#recover-file').onclick=()=>{$('#inspector').close();newResearch().then(()=>$('#research-form [name="attachment_files"]')?.click()).catch(x=>message(x.message));};
 $('#recover-run').onclick=()=>{$('#inspector').close();app.view='research';app.rid=null;render();};
}

async function explanationPrompt(force=false,isCurrent=()=>true){
 if(!app.settings)app.settings=await api('/api/control/settings');
 if(!isCurrent())return false;
 if(app.settings.preferences.explanation_prompt_dismissed&&!force)return true;
 editor('새 연구 안내',`<p>새 연구 화면에서 각 항목 옆에 자세한 설명을 표시할까요?</p><p>처음 사용하는 경우 '보기'를 권장합니다.</p><label class="check"><input id="dismiss-explanations" type="checkbox">다시 물어보지 않기</label><div class="inline-actions"><button id="without-explanations">보지 않기</button><button id="with-explanations" class="primary">보기</button></div><p>설정 &gt; 도움말 및 안내에서 언제든 다시 변경할 수 있습니다.</p>`);
 return new Promise(resolve=>{
  let chosen=false;
  const marker=$('#with-explanations'),dialog=$('#editor');
  const closed=()=>{if(dialog.open&&marker.isConnected)return;dialog.removeEventListener('close',closed);if(!chosen)resolve(false);};
  const choose=async enabled=>{try{const pref=await api('/api/control/preferences',{new_research_explanations:enabled,explanation_prompt_dismissed:$('#dismiss-explanations').checked});if(app.settings)app.settings.preferences=pref;if(!dialog.open||!marker.isConnected||!isCurrent()){closed();resolve(false);return;}chosen=true;dialog.removeEventListener('close',closed);dialog.close();resolve(true);}catch(x){message(x.message);}};
  $('#without-explanations').onclick=()=>choose(false);$('#with-explanations').onclick=()=>choose(true);
  dialog.addEventListener('close',closed);
 });
}

const v4NewResearch=newResearch;
function arrangeResearchPages(form){
 const names=['주제와 자료','모델과 예산','고급 설정'];
 const progress=document.createElement('nav');progress.className='research-progress';progress.setAttribute('aria-label','연구 설정 단계');
 progress.innerHTML=names.map((name,i)=>`<span data-step="${i}">${i+1}. ${name}</span>`).join('');form.prepend(progress);
 const pages=names.map((name,i)=>{const page=document.createElement('section');page.className='research-page';page.dataset.page=i;page.innerHTML=`<h3 tabindex="-1">${name}</h3>`;form.append(page);return page;});
 const move=(name,page)=>{const field=form.elements[name];if(field)pages[page].append(field.closest('label')||field);};
 move('title',0);move('question',0);move('attachment_files',0);pages[0].append(form.querySelector('#attachment-list'));
 const picker=form.querySelector('#research-model-picker');pages[1].append(picker.previousElementSibling.previousElementSibling,form.elements.model_profile_id,picker);
 pages[1].append(form.querySelector('label[for=performance]'));move('performance',1);pages[1].append(form.querySelector('#performance-detail'));
 for(const name of ['search_policy','run_limit_usd','adaptive_budget'])move(name,1);
 form.elements.search_policy.addEventListener('change',()=>{form.elements.search_required.checked=form.elements.search_policy.value!=='DISABLED';form.dispatchEvent(new Event('input',{bubbles:true}));});
 pages[2].append(form.querySelector('#research-advanced'));
 const advanced=pages[2].querySelector('#research-advanced');advanced.open=true;
 const egress=form.elements.egress;egress.closest('label').firstChild.textContent='선택한 분석 자료도 같이 보내기 ';
 const actions=form.querySelector('.inline-actions');pages[2].append(actions);
 $('#research-smoke').textContent='AI 연결 확인';$('#quick-recommended').hidden=true;
 const footer=document.createElement('div');footer.className='research-page-actions inline-actions';footer.innerHTML='<button type="button" id="research-prev">이전 장</button><button type="button" id="research-next" class="primary">다음 장</button>';form.append(footer);
 const start=form.querySelector('#research-start');footer.append(start);
 const error=form.querySelector('#research-preflight');form.append(error);
 const cancel=document.createElement('button');cancel.type='button';cancel.id='research-cancel';cancel.textContent='취소';cancel.onclick=()=>$('#editor').close();footer.append(cancel);
 const tutorial=document.createElement('button');tutorial.type='button';tutorial.id='research-tutorial';tutorial.textContent='튜토리얼 보기';tutorial.onclick=()=>replayTutorial().catch(x=>message(x.message));progress.after(tutorial);
 let index=0;
 form.setResearchPage=(next,{focus=true}={})=>{
  index=Math.max(0,Math.min(2,next));pages.forEach((page,i)=>page.hidden=i!==index);progress.querySelectorAll('[data-step]').forEach((step,i)=>step.setAttribute('aria-current',i===index?'step':'false'));
  $('#research-prev').hidden=index===0;$('#research-next').hidden=index===2;start.hidden=index!==2;form.dataset.page=index;
  if(focus){const heading=pages[index].querySelector('h3');heading.focus({preventScroll:true});heading.scrollIntoView({block:'start'});}
 };
 const valid=page=>{for(const field of pages[page].querySelectorAll('input,textarea,select'))if(!field.checkValidity()){form.setResearchPage(page);field.reportValidity();return false;}return true;};
 $('#research-prev').onclick=()=>form.setResearchPage(index-1);
 $('#research-next').onclick=()=>{if(valid(index))form.setResearchPage(index+1);};
 form.addEventListener('invalid',e=>{const page=e.target.closest('.research-page');if(page)form.setResearchPage(Number(page.dataset.page));},true);
 form.addEventListener('submit',e=>{if(index!==2){e.preventDefault();e.stopImmediatePropagation();if(valid(index))form.setResearchPage(index+1);}},true);
 const updateStart=()=>start.textContent='연구 시작 · 최대 $'+form.elements.run_limit_usd.value;
 form.elements.run_limit_usd.addEventListener('input',updateStart);updateStart();
 form.setResearchPage(0,{focus:false});
 form.querySelectorAll('.field-explanation,.muted,.verification-option small').forEach(node=>node.remove());
 for(const field of form.querySelectorAll('[required]')){
  const label=field.closest('label');if(!label)continue;
  const star=document.createElement('span');star.className='required-mark';star.textContent=' *';star.setAttribute('aria-label','필수');const heading=document.createElement('span');heading.className='field-heading';while(label.firstChild&&label.firstChild!==field)heading.append(label.firstChild);heading.append(star);label.prepend(heading);
 }
 const modelHeading=form.elements.model_profile_id.previousElementSibling;
 const modelStar=document.createElement('span');modelStar.className='required-mark';modelStar.textContent=' *';modelStar.setAttribute('aria-label','필수');modelHeading.append(modelStar);

}
newResearch=async function({skipExplanation=false}={}){
 const token=app.researchOpenToken=(app.researchOpenToken||0)+1,epoch=app.epoch;
 const isCurrent=()=>token===app.researchOpenToken&&epoch===app.epoch;
 if(!skipExplanation&&!app.tutorialActive&&!await explanationPrompt(false,isCurrent))return;
 if(!isCurrent())return;
 await v4NewResearch({isCurrent});
 if(!isCurrent())return;
 const form=$('#research-form');if(!form)return;
 arrangeResearchPages(form);await window.ProbeResearchDesign?.ensureMounted(form);if(app.tutorialActive){window.ProbeTutorial?.researchOpened(form);drawTutorial();}
};


function researchListCards(rows,filter){
 return rows.length?'<div class="research-list-cards">'+rows.map(r=>'<article class="research-list-card"><button class="research-card-title" data-run="'+esc(r.research_id)+'">'+esc(r.title)+'</button><span class="tag" data-tone="'+esc(r.official_status?.tone||'waiting')+'">'+esc(filter==='trash'?'휴지통':r.official_status?.label||beginnerStatus(r.control_status))+'</span><div class="research-card-meta"><span>사용 비용 '+researchCostCell(r)+'</span><time>'+esc(time(r.updated_at))+'</time></div><div class="inline-actions">'+(filter==='trash'?'<button data-restore="'+esc(r.research_id)+'">복원</button><button data-purge="'+esc(r.research_id)+'">영구 삭제</button>':'<button data-rename="'+esc(r.research_id)+'">이름 변경</button><button data-trash="'+esc(r.research_id)+'">연구 삭제</button>')+'</div></article>').join('')+'</div>':'<div class="empty">'+(filter==='trash'?'휴지통이 비어 있습니다.':'아직 연구가 없습니다.')+'</div>';
}

renderList=async function(epoch){
 const filter=app.beginnerFilter||'all',query=new URLSearchParams({limit:'50',offset:String(app.listOffset||0),trash:filter==='trash'?'1':'0',status_group:filter,q:app.beginnerQuery||''});
 const page=await api('/api/control/research?'+query);if(epoch!==app.epoch)return;
 app.rows=page.items;let rows=app.rows;
 if(filter==='running')rows=rows.filter(r=>['준비 중','진행 중','일시정지'].includes(beginnerStatus(r.control_status)));
 if(filter==='completed')rows=rows.filter(r=>beginnerStatus(r.control_status)==='완료');
 $('#breadcrumb').textContent='작업 공간 / 연구';$('#mode').textContent=app.rows.some(r=>r.mode==='DEMO')?'예시 데이터 포함':'이 기기의 연구';
 $('#content').innerHTML=`<div class="page-head"><h1>연구</h1><button id="list-new" class="primary">새 연구 만들기</button></div><nav class="tabs" aria-label="연구 분류">${[['all','전체'],['running','진행 중'],['completed','완료'],['trash','휴지통']].map(([key,label])=>`<button data-filter="${key}" aria-current="${filter===key?'page':'false'}">${label}</button>`).join('')}</nav><form class="research-list-search" id="research-list-search"><label for="research-search">검색</label><input id="research-search" type="search" maxlength="200" placeholder="연구 제목·질문" value="${esc(app.beginnerQuery||'')}"><button>검색</button></form><div id="research-table">${researchListCards(rows,filter)}</div><div class="inline-actions"><button id="list-prev" ${!app.listOffset?'disabled':''}>이전</button><button id="list-next" ${page.next_offset===null?'disabled':''}>다음</button><button id="list-recovery">문제가 생겼어요</button></div>`;
 $('#list-new').onclick=()=>newResearch().catch(x=>message(x.message));
 $('#research-list-search').onsubmit=e=>{e.preventDefault();app.beginnerQuery=$('#research-search').value;app.listOffset=0;render();};
 $('#list-recovery').onclick=beginnerRecovery;
 $('[data-view="research"]').textContent='연구';
 document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{app.beginnerFilter=b.dataset.filter;app.listOffset=0;render();});
 $('#list-prev').onclick=()=>{app.listOffset=Math.max(0,(app.listOffset||0)-50);render();};$('#list-next').onclick=()=>{app.listOffset=page.next_offset;render();};
 document.querySelectorAll('[data-run]').forEach(b=>b.onclick=()=>{app.rid=b.dataset.run;app.tab='overview';app.technicalResearch=false;history.replaceState(null,'','#research/'+app.rid+'/overview');render();});
 document.querySelectorAll('[data-cost-history]').forEach(b=>b.onclick=()=>{app.usageOffset=0;app.usageFilter={scope:'all',role:''};renderUsage(b.dataset.costHistory).catch(x=>message(x.message));});
 bindLifecycle($('#content'));$('#statusbar').textContent='한국 시간';
 if(!app.settings?.submission_mode)await autoTutorial();
};

function bindLifecycle(root){
 root.querySelectorAll('[data-rename]').forEach(b=>b.onclick=()=>{
  const row=app.rows.find(r=>r.research_id===b.dataset.rename);
  editor('연구 이름 변경',`<form id="rename-form"><label>연구 제목<input name="title" maxlength="200" value="${esc(row?.title||'')}" required></label><button class="primary">변경</button></form>`);
  $('#rename-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/control/research/'+encodeURIComponent(b.dataset.rename)+'/rename',{title:e.target.elements.title.value});$('#editor').close();await render();message('이름을 변경했습니다.','success');}catch(x){message(x.message);}};
 });
 for(const action of ['trash','restore','purge'])root.querySelectorAll('[data-'+action+']').forEach(b=>b.onclick=async()=>{
  const rid=b.dataset[action];
  const run=async()=>{try{await api('/api/control/research/'+encodeURIComponent(rid)+'/'+action,action==='purge'?{confirm:true}:{});$('#editor').close();app.rid=null;await render();message(action==='restore'?'연구를 복원했습니다.':action==='purge'?'연구를 영구 삭제했습니다.':'휴지통으로 이동했습니다.','success');}catch(x){message(x.message);}};
  if(action!=='purge')return run();
  editor('영구 삭제',`<p>영구 삭제하면 이 연구를 다시 복원할 수 없습니다.</p><button id="confirm-purge">영구 삭제</button><button id="cancel-purge">취소</button>`);
  $('#confirm-purge').onclick=run;$('#cancel-purge').onclick=()=>$('#editor').close();
 });
}

function cardHTML(card){
 if(!card.available)return '<div class="empty">검증된 결론이 아직 없습니다.</div>';
 const pairs=[['무엇을 알아본 결과인가?',card.question],['어떤 자료를 사용했나?',card.source],['무엇을 계산했나?',card.calculation],['어디까지 말할 수 있나?',card.scope],['무엇은 아직 확인하지 못했나?',card.unconfirmed.join(' · ')],['이 결론은 현재도 유효한가?',card.currentness]];
 const change=card.changes;
 const changes=change?`<details><summary>변경 내역</summary><p>이전 질문: ${esc(change.old_question)}</p><p>현재 질문: ${esc(change.new_question)}</p>${Object.entries(change.groups).map(([name,g])=>`<p>${esc(name)} · 추가 ${esc(g.added.join(', ')||'없음')} · 제외 ${esc(g.removed.join(', ')||'없음')}</p>`).join('')}<table><thead><tr><th>수치</th><th>이전</th><th>현재</th><th>변경</th></tr></thead><tbody>${change.values.map(v=>`<tr><td>${esc(({mean_a:'앞 기간 평균',mean_b:'뒤 기간 평균',difference:'차이'})[v.name]||v.name)}</td><td>${esc(v.before??'기록 없음')}</td><td>${esc(v.after)}</td><td>${v.changed?'변경':'동일'}</td></tr>`).join('')}</tbody></table></details>`:'';
 const audit=card.representation;
 const auditLabel=audit?(audit.status==='MATCHED_SELECTED_KEYS'?'선택 키별 형식 대조 완료':audit.status==='NOT_PRESENT_OPTIONAL'?'선택 형식 대조 미수행':audit.status==='CHECK_PENDING'?'필수 형식 대조 대기':'자료 형식 간 충돌'):'';
 return `<article class="conclusion-card"><h2>결론 검토 카드</h2>${pairs.map(([k,v])=>`<section><h3>${esc(k)}</h3><p>${esc(v)}</p></section>`).join('')}${card.message?`<p role="alert">${esc(card.message)}</p>`:''}${card.pending_question?`<p>변경한 질문 · 재확인 대기: ${esc(card.pending_question)}</p>`:''}${auditLabel?`<p>${esc(auditLabel)}</p>`:''}${card.record?.profile_id?`<div class="inline-actions"><button id="profile-refresh">자료 변경 확인</button><button id="profile-recalculate">계산 다시 확인하기</button><button id="profile-amend">질문 변경</button></div><details id="profile-source-details"><summary>자료 근거</summary><div id="profile-source-content"></div></details>`:''}${changes}<details><summary>이전 결론</summary>${(card.analysis_history||[]).filter(h=>h.historical).map(h=>`<p>이전 결과 · ${esc(h.question)}<br>${esc(h.calculation)}</p>`).join('')||card.history.filter(h=>!h.current).map(h=>`<p>${esc(JSON.parse(h.payload_json).text)}</p>`).join('')||'<p>이전 결론이 없습니다.</p>'}</details></article>`;
}


async function connectionCheck(connectionId){
 if(!app.settings)app.settings=await api('/api/control/settings');
 const models=app.settings.models.filter(m=>m.connection_id===connectionId);
 if(!models.length){message('새 연구에서 사용할 모델을 먼저 선택해 주세요.');return;}
 const cap=String(Math.min(0.10,Number(app.settings.defaults.request_limit_usd)));
 editor('AI 연결 확인',`<form id="connection-check"><label>AI 모델<select name="profile">${models.map(m=>`<option value="${esc(m.profile_id)}">${esc(m.display_name||m.model_id)}</option>`).join('')}</select></label><div id="connection-price" role="status"></div><div class="inline-actions"><button id="check-ai" class="primary" disabled>AI 연결 확인 · 최대 ${costUSD(cap)}</button><button type="button" id="check-model-details">모델 상세 설정</button></div><div id="connection-check-result" role="status"></div></form>`);
 const form=$('#connection-check');let quote=null,token=0;
 async function pricePreview(){
  const turn=++token;quote=null;$('#check-ai').disabled=true;$('#connection-price').textContent='비용 설정 확인 중';
  $('#connection-check-result').textContent='';
  try{
   const value=await api('/api/control/models/'+encodeURIComponent(form.elements.profile.value)+'/pricing');
   if(turn!==token||!form.isConnected)return;quote=value;
   const candidate=value.candidate;
   $('#connection-price').innerHTML=value.required?(candidate?`<p><strong>공식 단가를 적용하면 연결을 확인할 수 있습니다.</strong></p><p>백만 토큰당 입력 <strong>$${esc(candidate.input_per_million)}</strong> · 출력 <strong>$${esc(candidate.output_per_million)}</strong> <a href="${esc(candidate.source)}" target="_blank" rel="noopener noreferrer">가격 출처</a></p><button id="check-price-only" type="button">단가만 적용</button>`:'<p>이 모델의 단가를 직접 확인해 주세요.</p>'):'';
   $('#check-ai').disabled=value.required&&!candidate;
   $('#check-ai').textContent=(value.required&&candidate?'단가 적용 후 AI 연결 확인':'AI 연결 확인')+' · 최대 '+costUSD(cap);
   $('#check-price-only')?.addEventListener('click',async e=>{e.target.disabled=true;try{await applyPrice();await pricePreview();app.settings=null;message('공식 단가를 적용했습니다.','success');}catch(x){$('#connection-check-result').textContent=errors[x.code]||x.message;window.ProbeTutorial.attachError(x.code,$('#connection-check-result'));}});
  }catch(x){if(turn===token&&form.isConnected){$('#connection-price').textContent=errors[x.code]||x.message;}}
 }
 async function applyPrice(){
  if(quote?.required){
   await api('/api/control/models/'+encodeURIComponent(form.elements.profile.value)+'/pricing',{approve_price:true,expected_revision:quote.expected_revision,quote_id:quote.quote_id});
   app.settings=null;
   quote=await api('/api/control/models/'+encodeURIComponent(form.elements.profile.value)+'/pricing');
   $('#connection-price').textContent='';
   $('#check-ai').textContent='AI 연결 확인 · 최대 '+costUSD(cap);
  }
 }
 form.elements.profile.onchange=()=>pricePreview();
 $('#check-model-details').onclick=async()=>{try{if(!app.settings)app.settings=await api('/api/control/settings');modelForm(app.settings.models.find(m=>m.profile_id===form.elements.profile.value));}catch(x){message(x.message);}};
 form.onsubmit=async e=>{
  e.preventDefault();const button=$('#check-ai'),identity=form.elements.profile.value;button.disabled=true;form.elements.profile.disabled=true;
  try{
   await applyPrice();
   await api('/api/control/models/'+encodeURIComponent(identity)+'/check',{mode:'text',consent:true,budget_cap_usd:cap});
   $('#connection-check-result').textContent='AI 응답 확인 완료';app.settings=null;message('AI 응답을 확인했습니다.','success');
  }catch(x){if(form.isConnected){$('#connection-check-result').textContent=errors[x.code]||x.message;window.ProbeTutorial.attachError(x.code,$('#connection-check-result'));}}
  finally{if(form.isConnected){form.elements.profile.disabled=false;button.disabled=false;}}
 };
 await pricePreview();
}

// 튜토리얼과 사용 안내는 tutorial.js의 공통 컨트롤러를 사용한다.

const v4DeveloperSettings=renderSettings;
renderSettings=async function(epoch){
 if(app.developerSettings){await v4DeveloperSettings(epoch);const b=document.createElement('button');b.textContent='기본 설정으로';b.onclick=()=>{app.developerSettings=false;render();};$('#content').prepend(b);return;}
 const s=app.settings,p=s.preferences;
 $('#content').innerHTML=`<div class="page-head"><h1>설정</h1></div><nav class="tabs settings-nav" role="tablist" aria-label="설정 항목"></nav><section id="beginner-settings-connections" class="section"><div class="page-head"><h2>AI 연결</h2><button id="connection-new">AI 연결하기</button></div><div class="beginner-connections">${table(['연결','상태','관리'],s.connections.map(c=>[esc(c.display_name),connectionAvailable(c)?'연결됨':c.enabled?'키 필요':'사용 안 함',`<button data-connection-check="${esc(c.connection_id)}">AI 연결 확인</button><button data-key="${esc(c.connection_id)}">키 교체</button><button data-edit-connection="${esc(c.connection_id)}">연결 수정</button><button data-delete-connection="${esc(c.connection_id)}">연결 삭제</button>`]))}</div></section><section id="beginner-settings-budget" class="section"><h2>예산</h2><form id="defaults-form"><label>월간 한도 USD(미화 달러)<input name="monthly_limit_usd" type="number" min="0.01" step="0.01" required value="${esc(s.defaults.monthly_limit_usd)}"></label><button>월간 한도 변경</button><button type="button" id="finish-budget">현재 한도에서 마무리</button><button type="button" id="cancel-budget">취소</button></form></section><section id="beginner-settings-resource" class="section"><h2>성능 최적화</h2><label>실행 환경<select id="low-spec-mode">${[['AUTO','자동'],['LOW_SPEC','저사양'],['NORMAL','일반']].map(([k,v])=>`<option value="${k}" ${p.low_spec_mode===k?'selected':''}>${v}</option>`).join('')}</select></label></section><section id="beginner-settings-help" class="section"><h2>도움말 및 안내</h2><label class="check"><input id="show-explanations" type="checkbox" ${p.new_research_explanations?'checked':''}>새 연구 항목 설명 표시</label><div class="inline-actions"><button id="reopen-guide">새 연구 안내 다시 보기</button><button id="replay-tutorial">초기 튜토리얼 다시 보기</button><button id="full-guidebook">사용 안내</button></div></section><div class="inline-actions"><button id="reset-preferences">기본값으로 되돌리기</button><button id="developer-settings">문제 해결 정보</button><button id="settings-recovery">문제가 생겼어요</button></div>`;
 arrangeSettings([['connections','AI 연결',['beginner-settings-connections']],['budget','예산',['beginner-settings-budget']],['advanced','성능 최적화',['beginner-settings-resource']],['help','도움말 및 안내',['beginner-settings-help']]]);
 $('#connection-new').onclick=()=>connectionForm();
 document.querySelectorAll('[data-connection-check]').forEach(b=>b.onclick=()=>connectionCheck(b.dataset.connectionCheck));
 document.querySelectorAll('[data-delete-connection]').forEach(b=>b.onclick=()=>{const c=s.connections.find(c=>c.connection_id===b.dataset.deleteConnection);editor('연결 삭제',`<p>${esc(c.display_name)} 연결을 삭제할까요? 이 연결만 사용하는 저장 키도 삭제됩니다.</p><button id="confirm-connection-delete">연결 삭제</button><button id="cancel-connection-delete">취소</button>`);$('#cancel-connection-delete').onclick=()=>$('#editor').close();$('#confirm-connection-delete').onclick=async()=>{try{await api('/api/control/connections/'+encodeURIComponent(c.connection_id)+'/delete',{confirm:true,expected_revision:c.revision});$('#editor').close();app.settings=null;await render();message('연결을 삭제했습니다.','success');}catch(x){message(x.message);}};});
 document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>keyForm(b.dataset.key));
 document.querySelectorAll('[data-edit-connection]').forEach(b=>b.onclick=()=>connectionForm(s.connections.find(c=>c.connection_id===b.dataset.editConnection)));
 const savePref=async value=>{try{await api('/api/control/preferences',value);app.settings=null;await render();message('설정을 저장했습니다.','success');}catch(x){message(x.message);}};
 $('#show-explanations').onchange=e=>savePref({new_research_explanations:e.target.checked});
 $('#low-spec-mode').onchange=e=>savePref({low_spec_mode:e.target.value});
 $('#reopen-guide').onclick=()=>explanationPrompt(true);$('#replay-tutorial').textContent='튜토리얼 다시 보기';$('#replay-tutorial').onclick=replayTutorial;$('#full-guidebook').onclick=()=>showHelp();
 $('#reset-preferences').onclick=async()=>{await api('/api/control/preferences/reset',{});app.settings=null;await render();message('표시 설정을 기본값으로 되돌렸습니다.','success');};
 $('#developer-settings').onclick=()=>{app.developerSettings=true;render();};$('#settings-recovery').onclick=beginnerRecovery;
 $('#defaults-form').onsubmit=async e=>{e.preventDefault();const requested=e.target.elements.monthly_limit_usd.value;editor('월간 한도 변경',`<p>월간 한도를 $${esc(requested)}로 변경할까요?</p><button id="approve-monthly">승인</button><button id="cancel-monthly">취소</button>`);$('#cancel-monthly').onclick=()=>$('#editor').close();$('#approve-monthly').onclick=async()=>{try{await api('/api/control/defaults',{value:{...s.defaults,monthly_limit_usd:requested},expected_revision:s.defaults_revision});$('#editor').close();app.settings=null;await render();message('월간 한도를 변경했습니다.','success');}catch(x){message(x.message);}};};
 $('#finish-budget').onclick=async()=>{if(app.budgetResearch){try{await api('/api/control/research/'+encodeURIComponent(app.budgetResearch)+'/finish-current-budget',{});app.budgetResearch=null;message('현재 한도에서 연구를 마무리했습니다.','success');}catch(x){message(x.message);}}else message('월간 한도를 유지합니다. 진행 중인 연구의 예산에서 마무리를 선택할 수 있습니다.','success');};$('#cancel-budget').onclick=()=>{$('#defaults-form').elements.monthly_limit_usd.value=s.defaults.monthly_limit_usd;};
};

Object.assign(errors,{PROFILE_POLICY_INCOMPATIBLE:'저장된 검증 정책과 이 절차가 다릅니다. 기존 연구는 유지하고 새 연구를 만들어 주세요.',CONNECTION_IN_USE:'이 연결을 사용하는 연구를 먼저 마무리해 주세요.',BUDGET_FINISH_REQUIRES_PAUSE:'연구를 먼저 일시정지해 주세요.',RESEARCH_IN_TRASH:'휴지통에서 연구를 복원해 주세요.',RESEARCH_PAUSE_BEFORE_DELETE:'진행 중인 연구를 먼저 일시정지해 주세요.',PROFILE_UNSUPPORTED:'현재 지원하지 않는 연구 범위입니다. 원래 질문을 확인해 주세요.',PROFILE_CLARIFICATION_REQUIRED:'비교 기간이나 자료의 의미를 확인해 주세요.',PROFILE_VERIFICATION_FAILED:'계산이나 자료 검증을 통과하지 못했습니다.'});

function bindProfileCard(card,rid){
 const bind=(selector,event,handler)=>{const node=$(selector);if(node)node['on'+event]=handler;};
 bind('#profile-recalculate','click',async e=>{e.target.disabled=true;try{await api('/api/control/research/'+rid+'/recalculate',{});await render();message('계산을 다시 확인했습니다.','success');}catch(x){message(x.message);}finally{e.target.disabled=false;}});
 bind('#profile-refresh','click',async e=>{e.target.disabled=true;try{const result=await api('/api/control/research/'+rid+'/refresh-source',{});await render();message(result.affected?'사용한 자료가 바뀌었습니다. 계산을 다시 확인해 주세요.':'사용한 자료와 결론의 범위가 유지됩니다.',result.affected?'error':'success');}catch(x){message(x.message);}finally{e.target.disabled=false;}});
 bind('#profile-amend','click',()=>{
  editor('질문 변경',`<form id="profile-question-form"><label>현재 질문<textarea name="question" maxlength="4000" required>${esc(card.pending_question||card.question)}</textarea></label><div class="inline-actions"><button class="primary">변경 저장</button><button type="button" id="cancel-profile-question">취소</button></div></form>`);
  $('#cancel-profile-question').onclick=()=>$('#editor').close();
  $('#profile-question-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/control/research/'+rid+'/amend-question',{question:e.target.elements.question.value,expected_version:card.state_version});$('#editor').close();await render();message('질문을 변경했습니다. 계산을 다시 확인해 주세요.','success');}catch(x){message(x.message);}};
 });
 bind('#profile-source-details','toggle',async e=>{
  if(!e.target.open||e.target.dataset.loaded)return;
  try{const source=await api('/api/control/research/'+rid+'/source-inspection');
   $('#profile-source-content').innerHTML=`<p>원질문: ${esc(source.original_question)}</p><p>현재 질문: ${esc(source.current_question)}</p><p>${esc(source.limitation)}</p><p>출처 설명 문서: HTML 미수집 · 링크만 기록</p><dl><dt>자료 의미</dt><dd>${esc(JSON.stringify(source.primary?.semantics))}</dd><dt>원본 SHA-256</dt><dd>${esc(source.primary?.source_sha256)}</dd><dt>해시 범위</dt><dd>${esc(source.primary?.hash_scope)}</dd><dt>수집 기록</dt><dd>${esc(source.primary?.capture?.http_observed?'앱에서 HTTP 응답 관측':'로컬 자료 · 원래 HTTP 수집 미관측')}</dd><dt>다른 형식 SHA-256</dt><dd>${esc(source.secondary?.source_sha256||'없음')}</dd></dl><button id="profile-selected-rows">선택한 관측값 보기</button><div id="profile-selected-content"></div>`;
   e.target.dataset.loaded='1';$('#profile-selected-rows').onclick=async()=>{try{const values=await api('/api/control/research/'+rid+'/source-inspection?rows=1');$('#profile-selected-content').innerHTML=values.rows?`<table><thead><tr><th>연도</th><th>원래 편차 (°C)</th></tr></thead><tbody>${values.rows.map(r=>`<tr><td>${esc(r.year)}</td><td>${esc(r.value)}</td></tr>`).join('')}</tbody></table>`:'<p>선택값을 다시 확인해야 합니다.</p>';}catch(x){message(x.message);}};
  }catch(x){message(x.message);}
 });
}
