/* 연구 입력과 같은 초안·시작 동작을 사용하는 선택 설계 편집기. */
(()=>{
 const clone=value=>structuredClone(value),empty=()=>({schema_version:1,editor_open:false,approach:'AUTO',intervention:'unknown',fields:{},variables:[],hypotheses:[],comparisons:[],procedure:[]});
 const labelState={SPECIFIED:'직접 입력',UNKNOWN:'모름',USER_DECLARED_NONE:'없음',NOT_APPLICABLE:'해당 없음'};
 const count=d=>Object.keys(d.fields||{}).length+(d.variables||[]).filter(v=>v.name.trim()&&v.active!==false).length+['hypotheses','comparisons','procedure'].reduce((n,k)=>n+(d[k]||[]).filter(r=>r.text.trim()||r.start!=null||r.end!=null).length,0)+(d.sample_count!=null?1:0)+(d.approach!=='AUTO'?1:0)+(d.intervention!=='unknown'?1:0);
 function notify(form){form.dispatchEvent(new Event('input',{bubbles:true}));}
 function roleLabels(d,c){
  const roles={...c.roles};
  if(d.approach==='PHYSICAL'&&d.intervention==='change')roles.outcome='결과로 측정할 값(종속 변인)';
  if(d.approach==='EXISTING'){roles.fixed='자료를 비교할 때 맞춰야 할 조건';roles.other_factor='통제하지 못한 다른 요인';}
  return roles;
 }
 function field(key,label,entry={},help='',methodChoices=null){
  const value=entry.value||'',state=entry.state||'SPECIFIED',id='design-field-'+key;
  const input=methodChoices?'<select data-field="'+esc(key)+'" id="'+esc(id)+'"><option value="">적합한 방법 제안받기</option>'+methodChoices.map(m=>'<option '+(value===m?'selected':'')+' value="'+esc(m)+'">'+esc(({pearson_correlation:'Pearson 상관 분석',spearman_correlation:'Spearman 순위 상관 분석',two_period_comparison:'두 기간 평균 비교',descriptive:'기술 통계',independent_t_test:'독립 표본 t 검정',mann_whitney_u:'Mann–Whitney 검정',linear_regression:'선형 회귀'})[m]||m)+'</option>').join('')+(value&&!methodChoices.includes(value)?'<option selected value="'+esc(value)+'">'+esc(value)+'</option>':'')+'</select><label>다른 방법 요청<input data-custom-method value=""></label>':'<textarea id="'+esc(id)+'" data-field="'+esc(key)+'" maxlength="4000" rows="2">'+esc(value)+'</textarea>';
  return '<div class="design-field"><label for="'+esc(id)+'">'+esc(label)+'</label>'+input+'<details class="design-state"><summary>입력 상태</summary><label>상태<select data-state="'+esc(key)+'">'+Object.entries(labelState).map(([k,v])=>'<option value="'+k+'" '+(state===k?'selected':'')+'>'+v+'</option>').join('')+'</select></label><label>이유<input data-reason="'+esc(key)+'" maxlength="1000" value="'+esc(entry.reason||'')+'"></label></details></div>';
 }
 function mount(form,c,raw){
  let d={...empty(),...clone(raw||{})},proposal=null;
  const host=document.createElement('section');host.id='research-design';host.className='research-design';
  const page=form.querySelector('.research-page[data-page="0"]');
  const core=page?document.createElement('section'):null;
  let modes=null;
  if(page){
   core.id='research-input-core';
   for(const name of ['title','question','attachment_files'])core.append(form.elements[name].closest('label'));
   core.append(form.querySelector('#attachment-list'));page.append(core,host);
   modes=document.createElement('fieldset');modes.id='research-input-mode';modes.className='research-input-mode';
   modes.innerHTML='<legend>연구 설정 방식</legend><label><input type="radio" name="design_mode" value="simple">일반 설정</label><label><input type="radio" name="design_mode" value="detailed">맞춤 연구 설계</label><span id="design-mode-count" role="status"></span>';
   page.prepend(modes);
   modes.addEventListener('change',()=>{read();d.editor_open=modes.querySelector('[value=detailed]').checked;summary();notify(form);});
  }else form.querySelector('[name=question]').closest('label').after(host);
  function arrangeCore(){
   if(!core)return;
   const title=form.elements.title.closest('label'),question=form.elements.question.closest('label'),files=form.elements.attachment_files.closest('label'),attachments=form.querySelector('#attachment-list');
   core.replaceChildren();
   if(d.editor_open){
    for(const [id,label,nodes] of [['design-overview','연구 주제와 질문',[title,question]],['design-materials','연구 자료',[files,attachments]]]){
     const group=document.createElement('details');group.id=id;group.open=true;group.innerHTML='<summary>'+label+'</summary>';group.append(...nodes);core.append(group);
    }
   }else core.append(title,question,files,attachments);
   core.classList.toggle('research-design',d.editor_open);
  }
  form.readDetailedDesign=()=>clone(d);
  function read(){
   for(const input of host.querySelectorAll('[data-field]')){
    const key=input.dataset.field,value=input.value.trim(),state=host.querySelector('[data-state="'+key+'"]')?.value||'SPECIFIED',reason=host.querySelector('[data-reason="'+key+'"]')?.value||'';
    if(value||state!=='SPECIFIED'){
     const old=d.fields[key];d.fields[key]={value,state,reason,...(old?.value===value?{origin:old.origin||'user',...(old.source_span?{source_span:old.source_span,source_revision:old.source_revision}:{})}:{origin:'user'})};
    }else delete d.fields[key];
   }
   const custom=host.querySelector('[data-custom-method]');if(custom?.value.trim())d.fields.method={value:custom.value.trim(),state:'SPECIFIED',origin:'user'};
   for(const node of host.querySelectorAll('[data-var]')){
    const v=d.variables.find(x=>x.id===node.dataset.var);if(!v)continue;
    v.name=node.querySelector('[data-v-name]').value;v.role=node.querySelector('[data-v-role]').value;
    v.data_form=node.querySelector('[data-v-form]').value;v.details=v.details||{};
    for(const input of node.querySelectorAll('[data-v-detail]')){const key=input.dataset.vDetail,value=input.value.trim();if(value)v.details[key]={value,state:'SPECIFIED',origin:'user'};else delete v.details[key];}
   }
   for(const node of host.querySelectorAll('[data-row]')){
    const r=d[node.dataset.kind].find(x=>x.id===node.dataset.row);r.text=node.querySelector('[data-row-text]').value;
    if(node.dataset.kind==='comparisons')for(const key of ['start','end']){const value=node.querySelector('[data-row-'+key+']').value;if(value!=='')r[key]=Number(value);else delete r[key];}
   }
   const n=host.querySelector('[data-sample-count]')?.value;if(n!=='')d.sample_count=Number(n);else delete d.sample_count;
  }
  function summary(){
   const toggle=host.querySelector('#design-toggle');toggle.textContent=d.editor_open?'일반 설정':'맞춤 연구 설계';toggle.setAttribute('aria-expanded',String(d.editor_open));
   host.querySelector('#design-body').hidden=!d.editor_open;
   host.querySelector('#design-count').textContent=!d.editor_open&&count(d)?'저장된 설계 조건 '+count(d)+'개':'';
   if(modes){
    toggle.parentElement.hidden=true;host.hidden=!d.editor_open;
    modes.querySelector('[value=simple]').checked=!d.editor_open;modes.querySelector('[value=detailed]').checked=d.editor_open;
    modes.querySelector('#design-mode-count').textContent=!d.editor_open&&count(d)?'저장된 설계 조건 '+count(d)+'개':'';
    if(core.dataset.mode!==String(d.editor_open)){arrangeCore();core.dataset.mode=String(d.editor_open);}
   }
  }
  function rowHTML(kind,row,index){
   return '<div class="design-row" data-row="'+esc(row.id)+'" data-kind="'+kind+'"><label>'+esc({hypotheses:'예상하는 결과(가설)',comparisons:'비교할 집단·기간',procedure:'연구 진행 순서'}[kind])+'<input data-row-text maxlength="4000" value="'+esc(row.text||'')+'"></label>'+(kind==='comparisons'?'<div class="design-grid"><label>시작 연도<input data-row-start type="number" min="1" max="9999" value="'+(row.start??'')+'"></label><label>끝 연도<input data-row-end type="number" min="1" max="9999" value="'+(row.end??'')+'"></label></div>':'')+'<div class="inline-actions"><button type="button" data-move="'+kind+':'+row.id+':-1" '+(!index?'disabled':'')+'>위로</button><button type="button" data-move="'+kind+':'+row.id+':1" '+(index===d[kind].length-1?'disabled':'')+'>아래로</button><button type="button" data-remove="'+kind+':'+row.id+'">삭제</button></div></div>';
  }
  function cardHTML(v,index){
   const roles=roleLabels(d,c),control=v.role==='fixed',basic=Object.entries(c.card_fields).filter(([k])=>!['fixed_value','maintain','check','tolerance','difficulty'].includes(k)||control);
   return '<fieldset class="design-card" data-var="'+esc(v.id)+'"><legend>항목 '+(index+1)+(v.active===false?' · 보류':'')+'</legend><div class="design-grid"><label>이름<input data-v-name maxlength="200" value="'+esc(v.name||'')+'"></label><label>역할<select data-v-role>'+Object.entries(roles).map(([k,label])=>'<option value="'+k+'" '+(v.role===k?'selected':'')+'>'+esc(label)+'</option>').join('')+'</select></label></div>'+(v.active===false?'<p role="status">'+esc(v.inactive_reason||'접근 방식에 맞는 역할을 확인해 주세요.')+'</p>':'')+'<details class="design-card-details"><summary>측정·단위·자료 연결</summary><label>자료 형태<select data-v-form>'+Object.entries({unknown:'아직 정하지 않음',number:'숫자',category:'분류',ordinal:'순서가 있는 분류',datetime:'날짜·시간',text:'글'}).map(([k,x])=>'<option value="'+k+'" '+((v.data_form||'unknown')===k?'selected':'')+'>'+x+'</option>').join('')+'</select></label>'+basic.map(([k,label])=>'<label>'+esc(label)+'<input data-v-detail="'+k+'" maxlength="4000" value="'+esc(v.details?.[k]?.value||'')+'"></label>').join('')+'<label>연결할 CSV<select data-v-source><option value="">자료에 연결하지 않음</option>'+(app.researchAttachments||[]).filter(a=>a.supported_parser==='csv'&&!a.removed).map(a=>'<option value="'+esc(a.attachment_id)+'" '+(v.binding?.attachment_id===a.attachment_id?'selected':'')+'>'+esc(a.name)+'</option>').join('')+'</select></label><label>연결할 열<input data-v-column maxlength="200" value="'+esc(v.binding?.column||'')+'"></label><button type="button" data-bind="'+esc(v.id)+'">자료·열 연결 확인</button><p data-bind-result>'+esc(v.binding?'현재 자료 수정본에 연결됨':'')+'</p></details><div class="inline-actions"><button type="button" data-move="variables:'+v.id+':-1" '+(!index?'disabled':'')+'>위로</button><button type="button" data-move="variables:'+v.id+':1" '+(index===d.variables.length-1?'disabled':'')+'>아래로</button><button type="button" data-active="'+v.id+'">'+(v.active===false?'다시 적용':'보류')+'</button><button type="button" data-remove="variables:'+v.id+'">삭제</button></div></fieldset>';
  }
  function draw(){
   const opened=[...host.querySelectorAll('[data-section]')].filter(n=>n.open).map(n=>n.dataset.section);
   host.innerHTML='<div class="inline-actions"><button type="button" id="design-toggle" aria-controls="design-body">맞춤 연구 설계</button><span id="design-count"></span></div><div id="design-body" hidden><h3>연구 설계</h3><label>연구 접근 방식<select id="design-approach">'+Object.entries(c.approaches).map(([k,label])=>'<option value="'+k+'" '+(d.approach===k?'selected':'')+'>'+esc(label)+'</option>').join('')+'</select></label>'+(d.approach==='PHYSICAL'?'<label>직접 조건을 바꾸나요?<select id="design-intervention"><option value="unknown">아직 정하지 않음</option><option value="change">조건을 바꾸며 측정하기</option><option value="observe">기존 조건 관측하기</option></select></label>':'')+c.sections.map(([name,fields],i)=>'<details data-section="'+i+'" '+((opened.length?opened.includes(String(i)):i<2)?'open':'')+'><summary>'+(i+1)+'. '+esc(name)+'</summary>'+fields.map(([k,label])=>field(k,label,d.fields[k],app.settings.preferences.new_research_explanations?c.help[k]:'',k==='method'?c.methods:null)).join('')+(i===0?d.hypotheses.map((r,j)=>rowHTML('hypotheses',r,j)).join('')+'<button type="button" data-add-row="hypotheses">가설 추가</button>':i===1?'<div id="design-variables">'+d.variables.map(cardHTML).join('')+'</div><div class="inline-actions">'+[['item','항목 추가'],['outcome','측정할 값 추가'],['fixed','같게 유지할 조건 추가'],['comparison','비교 조건 추가']].map(([k,label])=>'<button type="button" data-add-var="'+k+'">'+label+'</button>').join('')+'</div>':i===2?'<label>대상 수<input data-sample-count type="number" min="1" max="10000000" value="'+(d.sample_count??'')+'"></label>'+d.comparisons.map((r,j)=>rowHTML('comparisons',r,j)).join('')+'<button type="button" data-add-row="comparisons">집단·기간 추가</button>':i===3?d.procedure.map((r,j)=>rowHTML('procedure',r,j)).join('')+'<button type="button" data-add-row="procedure">진행 순서 추가</button>':'')+'</details>').join('')+'<div class="inline-actions"><button type="button" id="design-organize">내용에서 항목 정리하기</button><button type="button" id="design-review">입력 내용 확인</button><button type="button" id="design-clear">상세 조건 지우기</button><button type="button" id="design-help">사용 안내</button></div><div id="design-feedback" role="status"></div><div id="design-proposals"></div></div>';
   const intervention=host.querySelector('#design-intervention');if(intervention)intervention.value=d.intervention;
   summary();
  }
  function showReview(value){
   const out=host.querySelector('#design-feedback');out.replaceChildren();
   if(!value.issues.length)out.textContent='입력 내용 확인 완료 · 비워 둔 항목은 그대로 둡니다. 실제 분석에는 자료와 방법 확인이 필요합니다.';
   for(const issue of value.issues){
    const p=document.createElement('p');p.textContent=issue.message;
    const button=document.createElement('button');button.type='button';button.textContent='입력 확인하기';
    button.onclick=()=>{const key=issue.field.split(':')[0],node=key==='variables'?host.querySelector('[data-var="'+issue.field.split(':')[1]+'"]'):host.querySelector('[data-field="'+key+'"]')||host.querySelector('[data-section="2"]');if(node){for(let x=node;x&&x!==host;x=x.parentElement)if(x.tagName==='DETAILS')x.open=true;node.querySelector('input,select,textarea')?.focus();node.focus?.();node.scrollIntoView({block:'center'});}};
    p.append(button);out.append(p);
   }
  }
  host.addEventListener('input',e=>{read();summary();if(proposal)host.querySelector('#design-proposals').querySelector('[data-proposal-state]').textContent='입력이 바뀌었습니다. 다시 정리해 주세요.';});
  host.addEventListener('change',e=>{
   if(e.target.id==='design-approach'){
    read();const next=e.target.value;d.approach=next;
    for(const v of d.variables)if(['manipulated','simulation_input','simulation_output'].includes(v.role)&&next!=='PHYSICAL'&&next!=='SIMULATION'){v.active=false;v.inactive_reason='연구 접근 방식이 바뀌어 보류했습니다. 역할을 확인한 뒤 다시 적용해 주세요.';}
    draw();notify(form);
   }else if(e.target.id==='design-intervention'){d.intervention=e.target.value;read();draw();notify(form);}
   else if(e.target.matches('[data-v-role]')){read();draw();notify(form);}
  });
  host.addEventListener('click',async e=>{
   const b=e.target.closest('button');if(!b)return;
   try{
    if(b.id==='design-toggle'){read();d.editor_open=!d.editor_open;summary();notify(form);return;}
    if(b.dataset.addVar){read();const id=crypto.randomUUID();d.variables.push({id,name:'',role:b.dataset.addVar,active:true,details:{},data_form:'unknown'});draw();host.querySelector('[data-var="'+id+'"] [data-v-name]').focus();notify(form);}
    if(b.dataset.addRow){read();const kind=b.dataset.addRow,id=crypto.randomUUID();d[kind].push({id,text:''});draw();host.querySelector('[data-row="'+id+'"] input').focus();notify(form);}
    if(b.dataset.remove){read();const [kind,id]=b.dataset.remove.split(':');d[kind]=d[kind].filter(v=>v.id!==id);draw();host.querySelector('[data-add-var],[data-add-row]').focus();notify(form);}
    if(b.dataset.move){read();const [kind,id,direction]=b.dataset.move.split(':'),rows=d[kind],index=rows.findIndex(r=>r.id===id),next=index+Number(direction);if(next>=0&&next<rows.length)[rows[index],rows[next]]=[rows[next],rows[index]];draw();host.querySelector('[data-move="'+kind+':'+id+':'+direction+'"]')?.focus();notify(form);}
    if(b.dataset.active){read();const v=d.variables.find(v=>v.id===b.dataset.active);v.active=v.active===false;if(v.active)v.inactive_reason='';draw();notify(form);}
    if(b.dataset.bind){
     read();const node=b.closest('[data-var]'),v=d.variables.find(x=>x.id===b.dataset.bind),id=node.querySelector('[data-v-source]').value,column=node.querySelector('[data-v-column]').value.trim();
     if(!id){delete v.binding;node.querySelector('[data-bind-result]').textContent='자료 연결을 해제했습니다.';notify(form);return;}
     const result=await api('/api/control/research/design-columns',{draft_id:form.dataset.draftId,attachment_id:id});
     if(!result.columns.includes(column))throw Error('선택한 CSV에 해당 열이 없습니다.');
     v.binding={attachment_id:id,sha256:result.sha256,column};node.querySelector('[data-bind-result]').textContent='자료와 열을 연결했습니다.';notify(form);
    }
    if(b.id==='design-clear'){read();if(count(d)&&!confirm('상세 조건만 지울까요? 주제·질문·첨부·실행 설정은 유지합니다.'))return;d=empty();d.editor_open=true;proposal=null;draw();notify(form);}
    if(b.id==='design-review'){read();const result=await api('/api/control/research/design-review',{question:form.elements.question.value,detailed_design:d});showReview(result);}
    if(b.id==='design-organize'){
     read();const question=form.elements.question.value,revision=Number(form.dataset.draftRevision||0);
     b.disabled=true;
     const result=await api('/api/control/research/design-review',{action:'organize',question,draft_revision:revision});
     proposal={...result,question};
     const p=host.querySelector('#design-proposals');
     p.innerHTML='<p data-proposal-state>'+(result.proposals.length?'원문의 항목 이름을 정리한 제안입니다. 선택한 항목만 적용합니다.':'직접 이름이 적힌 항목을 찾지 못했습니다. 수동으로 입력할 수 있습니다.')+'</p>'+result.proposals.map((v,i)=>'<label class="check"><input type="checkbox" data-proposal-index="'+i+'" '+(d.fields[v.field]?'':'checked')+'>'+esc(c.sections.flatMap(s=>s[1]).find(x=>x[0]===v.field)?.[1]||v.field)+' · '+esc(d.fields[v.field]?.value||'미입력')+' → '+esc(v.value)+'</label>').join('')+'<div class="inline-actions"><button type="button" id="design-apply-proposals">선택한 제안 적용</button><button type="button" id="design-cancel-proposals">취소</button></div>';
     if(Number(form.dataset.draftRevision)!==revision||form.elements.question.value!==question)p.querySelector('[data-proposal-state]').textContent='입력이 바뀌었습니다. 다시 정리해 주세요.';
    }
    if(b.id==='design-apply-proposals'){
     if(!proposal||proposal.draft_revision!==Number(form.dataset.draftRevision)||proposal.question!==form.elements.question.value)throw Error('입력이 바뀌었습니다. 내용을 다시 정리해 주세요.');
     read();for(const box of host.querySelectorAll('[data-proposal-index]:checked')){const v=proposal.proposals[Number(box.dataset.proposalIndex)];d.fields[v.field]={value:v.value,state:'SPECIFIED',origin:'accepted_suggestion',source_span:v.source_span,source_revision:v.source_revision};}
     proposal=null;draw();notify(form);
    }
    if(b.id==='design-cancel-proposals'){proposal=null;host.querySelector('#design-proposals').replaceChildren();}
    if(b.id==='design-help')window.showHelp('detailed_design');
   }catch(x){host.querySelector('#design-feedback').textContent=errors[x.code]||x.message;}finally{if(b.isConnected)b.disabled=false;}
  });
  draw();
 }
 const mounting=new WeakMap();
 function ensureMounted(form){
  if(!form||form.readDetailedDesign)return Promise.resolve();
  if(!mounting.has(form))mounting.set(form,(async()=>{const c=await api('/api/control/research/design-catalog');if(form.isConnected&&!form.readDetailedDesign)mount(form,c,app.researchDraft?.detailed_design);})());
  return mounting.get(form);
 }
 const previous=newResearch;
 newResearch=async function(options={}){await previous(options);await ensureMounted($('#research-form'));};
 const renderSummary=value=>{
  if(!value?.available)return '';
  return '<section class="research-design-summary"><h2>이번 연구의 조건</h2><p>연구 설계 수정본 '+esc(value.revision)+'</p>'+['처음 정한 조건','실제로 사용한 자료·방법','확인하지 못한 조건 또는 달라진 점','이 결론으로 말할 수 있는 범위'].map(label=>'<details><summary>'+label+'</summary>'+(Array.isArray(value[label])?value[label]:[value[label]]).map(v=>'<p>'+esc(v)+'</p>').join('')+'</details>').join('')+'<details><summary>설계·계약·도구 연결</summary><pre>'+esc(JSON.stringify(value.trace,null,2))+'</pre></details></section>';
 };
 async function renderGuide(host){
  host.textContent='사용 안내를 불러오는 중입니다.';
  try{const c=await api('/api/control/research/design-catalog');if(!host.isConnected)return;host.innerHTML='<h3 tabindex="-1">'+esc(c.guide[0])+'</h3>'+c.guide.slice(1).map(v=>'<p>'+esc(v)+'</p>').join('');host.querySelector('h3').focus({preventScroll:true});}catch(x){host.textContent=x.message;}
 }
 window.HtrsaResearchDesign={mount,ensureMounted,renderSummary,renderGuide,guideTopic:{id:'detailed_design',title:'맞춤 연구 설계',step_id:'question',summary:'선택 연구 설계 · 변인 · 통제 · 기간 · 자료 연결 · 제안 · 조건 변경 · 대상 · 가설 · 측정 · 반복 · 분석 · 초기화'}};
 ensureMounted($('#research-form')).catch(error=>message(error.message));
 Object.assign(errors,{RESEARCH_DESIGN_ACTION_BLOCKED:'입력한 연구 조건과 실행 계획을 확인해 주세요.',RESEARCH_DESIGN_SOURCE_DENIED:'현재 초안에 첨부한 자료만 연결할 수 있습니다.',RESEARCH_DESIGN_STALE:'연구 조건이 다른 창에서 바뀌었습니다. 다시 열어 주세요.'});
 const oldReport=reportHTML;reportHTML=v=>renderSummary(v.research_design)+oldReport(v);
 const oldRun=renderRun;renderRun=async function(epoch){await oldRun(epoch);if(epoch!==app.epoch||!app.rid)return;const rid=app.rid,record=await api('/api/control/research/'+encodeURIComponent(rid)+'/design');if(epoch!==app.epoch||rid!==app.rid)return;
  if($('#run-content')&&!$('#run-content .research-design-summary'))$('#run-content').insertAdjacentHTML('beforeend',renderSummary(record.summary));
  if(app.control?.snapshot){const button=document.createElement('button');button.textContent='연구 조건';button.id='design-amend';const actions=$('#content .page-head .inline-actions')||$('#content .page-head .actions');actions?.append(button);button.onclick=async()=>{
   const c=await api('/api/control/research/design-catalog');editor('연구 조건', '<form id="design-amend-form"><label>연구 질문<textarea name="question" readonly>'+esc(record.current?.original_question||app.control.snapshot.question)+'</textarea></label><button type="submit" class="primary">변경 저장</button><p id="design-amend-result" role="status"></p></form>');
   const form=$('#design-amend-form');form.dataset.draftRevision=0;mount(form,c,record.current?.design||{editor_open:true});
   form.onsubmit=async e=>{e.preventDefault();try{await api('/api/control/research/'+encodeURIComponent(rid)+'/design',{detailed_design:form.readDetailedDesign(),expected_version:record.state_version});$('#editor').close();await render();message('연구 조건을 저장했습니다. 변경된 조건은 다시 검증해야 합니다.','success');}catch(x){$('#design-amend-result').textContent=errors[x.code]||x.message;}};
  };}
 };
})();
