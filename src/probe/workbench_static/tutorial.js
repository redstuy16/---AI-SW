'use strict';

(function(){
 const catalog=window.ProbeTutorialContent;
 const steps=catalog.chapters.flatMap(chapter=>chapter.steps.map(step=>({...step,chapter_id:chapter.id,chapter_title:chapter.title})));
 const byId=new Map(steps.map(step=>[step.id,step]));
 const quickSteps=catalog.quick_start;
 const quickFor=id=>quickSteps.find(step=>step.lessons.includes(id))||quickSteps[0];
 const officialHosts=new Set(['platform.openai.com','developers.openai.com','platform.claude.com','aistudio.google.com','ai.google.dev','console.x.ai','docs.x.ai','platform.deepseek.com','api-docs.deepseek.com','console.mistral.ai','docs.mistral.ai','lmstudio.ai','ollama.com','docs.ollama.com','linear.app','www.notion.com']);
 let progress=null,saving=Promise.resolve(),busy=false,guideStep='overview',guideTopic=null,guideProvider='openai',guideLocal='LM_STUDIO';
 let paused=false,returnFocus=null,inviting=false,neverAsk=false,navigating=false,pendingStop=false,pendingStopTarget=null;
 let coach=null,coachTarget=null,coachKey='',coachIndex=0,coachFrame=0;
 const popup=$('#tutorial-popup');
 const defaults=()=>({course_version:4,chapter_id:'api',step_id:'provider_key',mode:'GUIDED',status:'IN_PROGRESS',completed_steps:[],provider_id:'openai',local_tool:'LM_STUDIO'});
 function currentProgress(stored){
  if(stored.course_version===4)return structuredClone(stored);
  const quick=quickFor(stored.step_id),original=byId.get(stored.step_id),step=quick.id==='question'&&original?.screen==='research'?original:byId.get(quick.id),read=new Set(stored.completed_steps),completed=[];
  if(stored.course_version===3){
   for(const id of ['provider_key','save_connection'])if(read.has(id))completed.push(id);
   if(read.has('question')&&read.has('model'))completed.push('question');
  }else for(const item of quickSteps)if(item.id!=='first_path'&&item.lessons.every(id=>read.has(id)))completed.push(item.id);
  return {...structuredClone(stored),course_version:4,chapter_id:step.chapter_id,step_id:step.id,mode:'GUIDED',completed_steps:completed};
 }
 const providerFor=id=>catalog.providers.find(provider=>provider.id===id)||catalog.providers[0];
 function lessonFor(step,providerId,localTool){
  const tool=catalog.local_tools.find(value=>value.id===localTool)||catalog.local_tools[0];
  const material={...step,...providerFor(providerId).lessons?.[step.id]};
  const fill=value=>typeof value==='string'?value.replaceAll('{local_tool}',tool.name).replaceAll('{local_address}',tool.address):value;
  return Object.fromEntries(Object.entries(material).map(([key,value])=>[key,Array.isArray(value)?value.map(fill):fill(value)]));
 }
 const rich=value=>esc(value).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
 const list=values=>'<ul>'+values.map(value=>'<li>'+rich(value)+'</li>').join('')+'</ul>';
 const ordered=values=>'<ol>'+values.map(value=>'<li>'+rich(value)+'</li>').join('')+'</ol>';
 function officialLink(label,url){
  try{const parsed=new URL(url);if(parsed.protocol!=='https:'||parsed.username||parsed.password||!officialHosts.has(parsed.hostname))return esc(label);}
  catch{return esc(label);}
  return '<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(label)+' <span class="muted">새 창</span></a>';
 }
 function providerSelect(id,selected){return '<label>사용할 제공사<select id="'+id+'">'+catalog.providers.map(provider=>'<option value="'+provider.id+'" '+(provider.id===selected?'selected':'')+'>'+esc(provider.name)+'</option>').join('')+'</select></label>';}
 function providerHTML(providerId,localTool,stepId,{selectTool=true}={}){
  const provider=providerFor(providerId),local=provider.id==='openai_compatible';
  let body='<section class="provider-lesson" data-provider="'+provider.id+'"><h4>'+esc(provider.name)+' 연결 준비</h4>';
  if(local){
   const tool=catalog.local_tools.find(value=>value.id===localTool)||catalog.local_tools[0];
   if(selectTool)body+='<label>사용할 로컬 도구<select data-local-tool>'+catalog.local_tools.map(value=>'<option value="'+value.id+'" '+(value.id===tool.id?'selected':'')+'>'+esc(value.name)+'</option>').join('')+'</select></label>';
   body+='<div class="guide-links">'+officialLink('설치 파일 받기',tool.download)+'</div>'+ordered(tool.steps)+'<p>앱에 입력할 주소: <code>'+esc(tool.address)+'</code></p>';
  }else{
   body+='<p>'+rich(provider.key_intro)+'</p><div class="guide-links">'+officialLink('API 키 발급 화면 열기',provider.keys)+'</div>'+ordered(provider.key_steps)+'<p>'+rich(provider.essential_notice)+'</p>';
  }
  body+='<details class="provider-extra"><summary>결제·계정·키 관리 등 추가 안내</summary>'+ordered(provider.account)+'<p>'+rich(provider.billing)+'</p>'+(provider.key_terms?'<p>'+rich(provider.key_terms)+'</p>':'')+'<p>'+rich(provider.trouble)+'</p>'+(local?'':'<div class="guide-links">'+officialLink('계정·결제 화면',provider.console)+'</div>')+'<div class="guide-links">'+provider.sources.map(([title,url])=>officialLink(title,url)).join('')+'</div><p>공식 안내 확인: '+catalog.verified_at+'</p></details></section>';
  return body;
 }
 function issueHTML(issue){
  return '<section class="guide-issue" data-issue="'+issue.id+'"><h4>'+esc(issue.title)+'</h4><p class="muted">'+esc(issue.codes.join(' · '))+'</p><p>'+esc(issue.cause)+'</p><p><strong>다음 조치</strong> '+esc(issue.fix)+'</p></section>';
 }
 function lessonHTML(step,providerId,localTool,options){
  step=lessonFor(step,providerId,localTool);
  const apiPreparation=['provider_account','provider_key'].includes(step.id);
  let body='<div class="lesson-body">';
  if(apiPreparation)body+=providerHTML(providerId,localTool,step.id,options)+'<details><summary>준비물과 자세한 사용 순서</summary>';
  body+='<h4>준비물</h4>'+list(step.ready)+'<h4>사용 순서</h4>'+ordered(step.actions);
  body+='<h4>입력 예시</h4><p class="lesson-example">'+rich(step.example)+'</p><h4>완료되면</h4>'+list(step.expected);
  body+='<details class="lesson-trouble"><summary>문제가 생겼을 때</summary>'+list(step.trouble);
  const issues=step.id==='issues'?catalog.issues:catalog.issues.filter(issue=>step.trouble.some(value=>issue.codes.some(code=>value.includes(code)))).slice(0,3);
  body+=issues.map(issueHTML).join('')+'</details>';
  if(step.extra.length)body+='<details><summary>더 알아보기</summary>'+list(step.extra)+'</details>';
  if(apiPreparation)body+='</details>';
  if(step.id==='save_connection')body+='<details><summary>API 키를 아직 만들지 않았다면</summary>'+providerHTML(providerId,localTool,'provider_key',options)+'</details>';
  return body+'</div>';
 }
 function clearTutorial({keepPopup=false}={}){
  clearCoach();
  document.querySelectorAll('.tutorial-focus').forEach(element=>element.classList.remove('tutorial-focus'));
  $('#tutorial-card')?.remove();
  $('#tutorial-resume')?.remove();
  if(!keepPopup&&popup.open)popup.close();
 }
 function pauseTutorial(){paused=true;clearCoach();if(popup.open)popup.close();}
 function returnTarget(fallback=coachTarget){
  const host=$('#inspector').open?$('#inspector'):$('#editor').open?$('#editor'):document.body;
  return returnFocus?.isConnected&&returnFocus.getClientRects().length&&host.contains(returnFocus)?returnFocus:fallback;
 }
 function resumeButton(){
  if($('#guidebook'))return;
  const button=document.createElement('button');button.type='button';button.id='tutorial-resume';button.className='tutorial-resume';button.textContent='튜토리얼 계속';
  button.onclick=()=>{paused=false;enterStage().catch(error=>message(error.message));};
  if($('#inspector').open)$('#inspector').insertBefore(button,$('#inspector-content'));
  else if($('#editor').open)$('#editor').insertBefore(button,$('#editor-content'));
  else document.body.append(button);
 }
 function clearCoach(){
  if(coachFrame)cancelAnimationFrame(coachFrame);coachFrame=0;
  if(coachTarget){
   coachTarget.classList.remove('tutorial-coach-target','tutorial-focus');
   const ids=(coachTarget.getAttribute('aria-describedby')||'').split(/\s+/).filter(id=>id&&id!=='tutorial-coach-text');
   if(ids.length)coachTarget.setAttribute('aria-describedby',ids.join(' '));else coachTarget.removeAttribute('aria-describedby');
  }
  coachTarget=null;coach?.remove();coach=null;
 }
 function coachRoute(){
  if(!app.tutorialActive||inviting||navigating||paused||!progress||$('#guidebook'))return null;
  const host=$('#inspector').open?$('#inspector'):$('#editor').open?$('#editor'):document.body,form=host.querySelector('#research-form'),quick=quickFor(progress.step_id);
  let id;
  if(progress.step_id==='check_connection'&&host.querySelector('#connection-check'))id='check';
  else if(quick.id==='provider_key')id='provider_issue';
  else if(quick.id==='save_connection')id=host.querySelector('#connection-form')?(host.querySelector('#connection-form').elements.adapter_id.value==='openai_compatible'?'local_connection':'connection'):'connection_entry';
  else if(quick.id==='first_path')id=host.querySelector('#list-new')?'research_new':'research_entry';
  else if(form?.dataset.page!==undefined)id=Number(form.dataset.page)===0&&form.readDetailedDesign?.().editor_open?'research_design':['research_input','research_model','research_start'][Number(form.dataset.page)||0];
  else id=host.querySelector('#list-new')?'research_new':'research_entry';
  const rendered=element=>Boolean(element?.getClientRects().length)&&!element.closest('[hidden]')&&![...ancestorDetails(element)].some(details=>!details.open&&!details.querySelector(':scope>summary')?.contains(element));
 const entries=catalog.coach_targets[id].targets.flatMap(entry=>{
   let selector=entry.target,target=host.querySelector(selector);
   if(!rendered(target)&&entry.fallback){selector=entry.fallback;target=host.querySelector(selector);}
   return rendered(target)&&!target.disabled?[{...entry,target:selector,element:target}]:[];
  });
  return entries.length?{id,host,entries,key:[id,quick.id,progress.provider_id,progress.local_tool,Boolean($('#connection-form'))].join(':')}:null;
 }
 function providerPreparation(){
  const provider=providerFor(progress.provider_id),local=provider.id==='openai_compatible';
  let body='<section id="provider-preparation" data-provider="'+provider.id+'">';
  if(!$('#connection-form'))body+=providerSelect('tutorial-provider',provider.id);
  if(local){
   const tool=catalog.local_tools.find(value=>value.id===progress.local_tool);
   body+='<label>사용할 로컬 도구<select data-local-tool>'+catalog.local_tools.map(value=>'<option value="'+value.id+'" '+(value.id===tool.id?'selected':'')+'>'+esc(value.name)+'</option>').join('')+'</select></label><h3>'+esc(tool.name)+' 준비하기</h3>'+ordered(tool.steps)+'<p>앱에 입력할 서버 주소: <code>'+esc(tool.address)+'</code></p>'+officialLink('공식 설치 파일 받기',tool.download);
  }else body+='<h3>'+esc(provider.name)+' API 키 발급</h3><p>'+rich(provider.key_intro)+'</p><div class="guide-links">'+officialLink('API 키 발급 화면 열기',provider.keys)+'</div>'+ordered(provider.key_steps)+'<p>'+rich(provider.key_finish)+'</p><p>'+rich(provider.essential_notice)+'</p>';
  return body+'<details class="provider-extra"><summary>결제·계정·키 관리 등 추가 안내</summary>'+list(provider.account)+'<p>'+rich(provider.billing)+'</p>'+(provider.key_terms?'<p>'+rich(provider.key_terms)+'</p>':'')+'<p>'+rich(provider.trouble)+'</p>'+(local?'':'<div class="guide-links">'+officialLink('계정·결제 화면',provider.console)+'</div>')+'<div class="guide-links">'+provider.sources.map(([title,url])=>officialLink(title,url)).join('')+'</div><p>공식 안내 확인: '+catalog.verified_at+'</p></details></section>';
 }
 function* ancestorDetails(element){for(let node=element?.parentElement;node;node=node.parentElement)if(node.tagName==='DETAILS')yield node;}
 function targetViewport(element){
  let view={left:8,top:8,right:innerWidth-8,bottom:innerHeight-8};
  for(let node=element.parentElement;node;node=node.parentElement){
   const style=getComputedStyle(node),rect=node.getBoundingClientRect();
   if(/auto|scroll|hidden|clip/.test(style.overflowY)){view.top=Math.max(view.top,rect.top+node.clientTop);view.bottom=Math.min(view.bottom,rect.top+node.clientTop+node.clientHeight);}
   if(/auto|scroll|hidden|clip/.test(style.overflowX)){view.left=Math.max(view.left,rect.left+node.clientLeft);view.right=Math.min(view.right,rect.left+node.clientLeft+node.clientWidth);}
  }
  return view;
 }
 function placeCoach(){
  if(!coach||!coachTarget?.isConnected)return;
  const target=coachTarget.getBoundingClientRect(),view=targetViewport(coachTarget),box=coach.querySelector('.tutorial-coach-box'),width=Math.min(coach.dataset.route==='provider_issue'?440:300,innerWidth-32);
  box.style.width=width+'px';box.style.maxHeight=(coach.dataset.route==='provider_issue'?Math.max(180,Math.min(innerHeight-32,Math.max(target.top-40,innerHeight-target.bottom-40))):Math.max(120,innerHeight-32))+'px';
  const height=box.getBoundingClientRect().height,edge=16,gap=20;
  const tooTall=target.height>view.bottom-view.top,tooWide=target.width>view.right-view.left;
  const below=tooTall?target.top>=view.bottom:target.bottom>view.bottom+1,above=tooTall?target.bottom<=view.top:target.top<view.top-1;
  const right=tooWide?target.left>=view.right:target.right>view.right+1,left=tooWide?target.right<=view.left:target.left<view.left-1;
  const direction=below?'아래':above?'위':right?'오른쪽':left?'왼쪽':'';
  const hint=coach.querySelector('#tutorial-scroll-hint');hint.hidden=!direction;hint.textContent=direction?direction+'로 스크롤해 표시된 항목을 찾으세요.':'';coach.dataset.scrollDirection=direction;
  const arrow=coach.querySelector('svg');arrow.hidden=Boolean(direction);coachTarget.classList.toggle('tutorial-coach-target',!direction);coachTarget.classList.toggle('tutorial-focus',!direction);
  const clamp=position=>({x:Math.max(edge,Math.min(innerWidth-width-edge,position.x)),y:Math.max(edge,Math.min(innerHeight-height-edge,position.y))});
  let position;
  if(direction&&box.style.left&&coach.dataset.placedTarget===coach.dataset.target)position=clamp({x:parseFloat(box.style.left),y:parseFloat(box.style.top)});
  else{
   const anchor={left:Math.max(view.left,Math.min(view.right,target.left)),right:Math.max(view.left,Math.min(view.right,target.right)),top:Math.max(view.top,Math.min(view.bottom,target.top)),bottom:Math.max(view.top,Math.min(view.bottom,target.bottom))};
   const choices=[{x:anchor.right+gap,y:anchor.top},{x:anchor.left-width-gap,y:anchor.top},{x:anchor.left,y:anchor.bottom+gap},{x:anchor.left,y:anchor.top-height-gap}].map(clamp);
   const area=p=>Math.max(0,Math.min(p.x+width,target.right)-Math.max(p.x,target.left))*Math.max(0,Math.min(p.y+height,target.bottom)-Math.max(p.y,target.top));
   const distance=p=>Math.hypot(p.x+width/2-(anchor.left+anchor.right)/2,p.y+height/2-(anchor.top+anchor.bottom)/2);
   const controls=[...coach.parentElement.querySelectorAll('button,a[href],input,select,textarea,summary')].filter(element=>!coach.contains(element)&&element!==coachTarget&&element.getClientRects().length&&!element.closest('[hidden]')).map(element=>element.getBoundingClientRect());
   const covered=p=>controls.reduce((total,rect)=>total+Math.max(0,Math.min(p.x+width,rect.right)-Math.max(p.x,rect.left))*Math.max(0,Math.min(p.y+height,rect.bottom)-Math.max(p.y,rect.top)),0);
   const score=p=>area(p)*1000+covered(p)*10+distance(p);
   position=choices.reduce((best,value)=>score(value)<score(best)?value:best,choices[0]);
  }
  box.style.left=position.x+'px';box.style.top=position.y+'px';coach.dataset.placedTarget=coach.dataset.target;
  if(direction){coach.querySelector('.tutorial-coach-arrow').removeAttribute('d');return;}
  const cx=position.x+width/2,cy=position.y+height/2,tx=Math.max(view.left,Math.min(view.right,target.left+target.width/2)),ty=Math.max(view.top,Math.min(view.bottom,target.top+target.height/2));
  const dx=tx-cx,dy=ty-cy,scale=1/Math.max(Math.abs(dx)/(width/2),Math.abs(dy)/(height/2),1);
  const start={x:cx+dx*scale,y:cy+dy*scale},endScale=1/Math.max(Math.abs(dx)/Math.max(target.width/2+7,8),Math.abs(dy)/Math.max(target.height/2+7,8),1),end={x:tx-dx*endScale,y:ty-dy*endScale};
  arrow.setAttribute('viewBox','0 0 '+innerWidth+' '+innerHeight);
  coach.querySelector('.tutorial-coach-arrow').setAttribute('d','M '+start.x+' '+start.y+' L '+end.x+' '+end.y);
 }
 function queueCoach(){
  if(coachFrame)return;
  coachFrame=requestAnimationFrame(()=>{coachFrame=0;refreshCoach();});
 }
 function refreshCoach(){
  const form=$('#research-form');
  if(app.tutorialActive&&!inviting&&!navigating&&!paused&&!busy&&form?.dataset.page!==undefined&&quickFor(progress.step_id).id==='first_path'){
   const step=byId.get('question');change({...progress,chapter_id:step.chapter_id,step_id:step.id,completed_steps:[...new Set([...progress.completed_steps,'first_path'])]});return;
  }
  if(app.tutorialActive&&!inviting&&!navigating&&!paused&&!busy&&form?.dataset.page!==undefined&&quickFor(progress.step_id).id==='question'&&byId.get(progress.step_id).page!==Number(form.dataset.page)){
   const step=byId.get(['question','model','optional_features'][Number(form.dataset.page)]);change({...progress,chapter_id:step.chapter_id,step_id:step.id});return;
  }
  const route=coachRoute();if(!route){clearCoach();return;}
  if(route.key!==coachKey){coachKey=route.key;coachIndex=0;}
  coachIndex=Math.min(coachIndex,route.entries.length-1);
  const entry=route.entries[coachIndex],quick=quickFor(progress.step_id),stage=quickSteps.indexOf(quick);
  if(!coach||coach.parentElement!==route.host){
   clearCoach();coach=document.createElement('section');coach.id='tutorial-coach';coach.setAttribute('popover','manual');coach.setAttribute('aria-label','따라 하기 안내');
   coach.innerHTML='<svg aria-hidden="true"><defs><marker id="tutorial-arrowhead" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M 0 0 L 8 4 L 0 8 Z"/></marker></defs><path class="tutorial-coach-arrow" marker-end="url(#tutorial-arrowhead)"/></svg><div class="tutorial-coach-box"><div class="tutorial-coach-top"><span id="tutorial-coach-count"></span><div class="tutorial-tools"><details class="tutorial-menu"><summary aria-label="튜토리얼 메뉴">⋯</summary><div><button type="button" id="tutorial-guide">사용 안내</button><button type="button" id="tutorial-restart">처음부터</button></div></details><button type="button" id="tutorial-coach-close" aria-label="튜토리얼 닫기">×</button></div></div><div class="tutorial-coach-content"><h2 id="tutorial-coach-title" tabindex="-1"></h2><p id="tutorial-coach-text" aria-live="polite"></p><p id="tutorial-scroll-hint" role="status" hidden></p><div id="tutorial-coach-detail"></div></div><p id="tutorial-save-status" role="status"></p><div class="tutorial-coach-actions"><button type="button" id="tutorial-coach-prev">이전</button><button type="button" id="tutorial-coach-next">다음</button></div></div>';
   route.host.append(coach);coach.showPopover();
   $('#tutorial-coach-close').onclick=stop;$('#tutorial-coach-prev').onclick=()=>moveCoach(-1);$('#tutorial-coach-next').onclick=()=>moveCoach(1);
   $('#tutorial-guide').onclick=()=>showHelp(progress.step_id);$('#tutorial-restart').onclick=()=>replayTutorial({restart:true});
  }
  if(coachTarget!==entry.element){
   if(coachTarget){coachTarget.classList.remove('tutorial-coach-target','tutorial-focus');const ids=(coachTarget.getAttribute('aria-describedby')||'').split(/\s+/).filter(id=>id&&id!=='tutorial-coach-text');if(ids.length)coachTarget.setAttribute('aria-describedby',ids.join(' '));else coachTarget.removeAttribute('aria-describedby');}
   document.querySelectorAll('.tutorial-focus').forEach(element=>element.classList.remove('tutorial-focus'));
   coachTarget=entry.element;coachTarget.classList.add('tutorial-coach-target','tutorial-focus');
   coachTarget.setAttribute('aria-describedby',[...new Set((coachTarget.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean).concat('tutorial-coach-text'))].join(' '));
  }
  coach.dataset.route=route.id;coach.dataset.target=entry.target;coach.classList.toggle('tutorial-coach-reading',route.id==='provider_issue');
  $('#tutorial-coach-count').textContent=(stage+1)+' / '+quickSteps.length+(route.entries.length>1?' · '+(coachIndex+1)+' / '+route.entries.length:'');
  $('#tutorial-coach-title').textContent=catalog.coach_targets[route.id].title;
  if($('#tutorial-coach-text').textContent!==entry.text)$('#tutorial-coach-text').textContent=entry.text;
  const detail=$('#tutorial-coach-detail');
  const detailKey=route.key+':'+coachIndex;
  if(detail.dataset.key!==detailKey){detail.dataset.key=detailKey;detail.innerHTML=route.id==='provider_issue'?providerPreparation():entry.detail?'<p>'+rich(entry.detail)+'</p>':'';detail.querySelector('#tutorial-provider')?.addEventListener('change',event=>change({...progress,provider_id:event.target.value}));detail.querySelector('[data-local-tool]')?.addEventListener('change',event=>change({...progress,local_tool:event.target.value}));}
  coach.querySelectorAll('button,select').forEach(element=>element.disabled=busy);
  $('#tutorial-coach-prev').disabled=busy||(coachIndex===0&&stage===0);
  const last=coachIndex===route.entries.length-1,waiting=last&&['research_new','research_input','research_design','research_model'].includes(route.id);
  $('#tutorial-coach-next').disabled=busy||waiting;$('#tutorial-coach-next').hidden=waiting;
  $('#tutorial-coach-next').textContent=!last?'다음 안내':route.id==='provider_issue'?'키 준비 완료':route.id==='connection_entry'?'연결 화면 열기':route.id==='research_entry'?'연구 화면 보기':route.id==='research_start'?'안내 마치기':route.id==='check'?'검사 안내 마치기':'새 연구 안내';
  placeCoach();
 }
 async function moveCoach(direction){
  if(busy)return;
  const route=coachRoute();if(!route)return;
  if(direction<0&&coachIndex===0){
   const index=quickSteps.indexOf(quickFor(progress.step_id)),step=byId.get(quickSteps[Math.max(0,index-1)].id);await change({...progress,chapter_id:step.chapter_id,step_id:step.id},{navigate:true});return;
  }
  if(direction>0&&coachIndex===route.entries.length-1){
   if(['research_new','research_input','research_design','research_model'].includes(route.id))return;
   if(route.id==='connection_entry'){await enterStage();return;}
   if(route.id==='research_entry'){await openScreen({screen:'research_list',page:null,target:'#list-new'});return;}
   if(route.id==='research_start'||route.id==='check'){await finishTutorial();return;}
   await advance();return;
  }
  coachIndex=Math.max(0,Math.min(route.entries.length-1,coachIndex+direction));refreshCoach();
  coachTarget?.focus({preventScroll:true});queueCoach();
 }
 async function finishTutorial(){
  const id=quickFor(progress.step_id).id,completed=[...new Set([...progress.completed_steps,id])];
  await change({...progress,completed_steps:completed,status:completed.length===quickSteps.length?'COMPLETED':'DISMISSED'});
  if(progress.status!=='IN_PROGRESS'){const target=returnTarget();app.tutorialActive=false;clearTutorial();target?.isConnected&&target.focus({preventScroll:true});returnFocus=null;message('안내를 마쳤습니다. '+catalog.invitation.reminder,'success');}
 }
 async function persist(next,extra={}){
  const snapshot=structuredClone(next);
  const task=saving.catch(()=>{}).then(async()=>{
   const value=await api('/api/control/preferences',{tutorial_progress:snapshot,...extra,...(snapshot.status==='IN_PROGRESS'?{}:{tutorial_completed:true})});
   if(app.settings)app.settings.preferences={...app.settings.preferences,tutorial_progress:value.tutorial_progress,tutorial_completed:value.tutorial_completed,tutorial_do_not_ask:value.tutorial_do_not_ask};
  });
  saving=task;await task;
 }
 async function change(next,{navigate=false}={}){
  if(busy)return;
  busy=true;$('#tutorial-coach')?.querySelectorAll('button,select').forEach(element=>element.disabled=true);
  try{await persist(next);progress=next;if(navigate)await enterStage();else drawTutorial();}
  catch(error){const note=$('#tutorial-save-status');if(note){note.textContent='학습 위치를 저장하지 못했습니다. 다시 눌러 주세요.';note.setAttribute('role','alert');}else message(error.message);}
  finally{busy=false;if(pendingStop){pendingStop=false;await stop();}else queueCoach();}
 }
 async function advance(){
  const current=quickFor(progress.step_id),index=quickSteps.indexOf(current),completed=[...new Set([...progress.completed_steps,current.id])],next=byId.get(quickSteps[Math.min(index+1,quickSteps.length-1)].id);
  await change({...progress,step_id:next.id,chapter_id:next.chapter_id,completed_steps:completed},{navigate:true});
 }
 async function connectionSaved(providerId){
  if(!app.tutorialActive||inviting||busy||!progress||progress.status!=='IN_PROGRESS'||quickFor(progress.step_id).id!=='save_connection'||providerId!==progress.provider_id)return;
  await advance();
 }
 function researchOpened(form){
  if(!app.tutorialActive||inviting||!progress||quickFor(progress.step_id).id!=='question')return;
  form.setResearchPage(byId.get(progress.step_id).page??0,{focus:false});
 }
 async function stop(){
  if(busy){pendingStop=true;pendingStopTarget=coachTarget;return;}
  if(inviting)return answerInvitation(false);
  busy=true;
  let next={...progress,status:'DISMISSED'};
  const form=$('#research-form');
  if(form?.dataset.page!==undefined&&quickFor(progress.step_id).id==='question'&&byId.get(progress.step_id).page!==Number(form.dataset.page)){const step=byId.get(['question','model','optional_features'][Number(form.dataset.page)]);next={...next,chapter_id:step.chapter_id,step_id:step.id};}
  try{await persist(next);progress=next;const target=pendingStopTarget?.isConnected?pendingStopTarget:returnTarget();pendingStopTarget=null;app.tutorialActive=false;clearTutorial();target?.isConnected&&target.focus({preventScroll:true});returnFocus=null;message(catalog.invitation.reminder,'success');}
  catch(error){busy=false;message(error.message);return;}
  busy=false;
 }
 async function answerInvitation(show){
  if(busy)return;
  busy=true;popup.querySelectorAll('button,input').forEach(element=>element.disabled=true);
  try{
   if(show){
    const next={...progress,status:'IN_PROGRESS',mode:progress.mode==='WELCOME'?'GUIDED':progress.mode};
    const previous=progress;progress=next;inviting=false;
    try{await enterStage();await persist(next,{tutorial_do_not_ask:neverAsk});$('#tutorial-coach-title')?.focus({preventScroll:true});}
    catch(error){progress=previous;inviting=true;drawTutorial();throw error;}
   }else{
    const task=saving.catch(()=>{}).then(()=>api('/api/control/preferences',{tutorial_do_not_ask:neverAsk}));saving=task;
    const value=await task;if(app.settings)app.settings.preferences.tutorial_do_not_ask=value.tutorial_do_not_ask;
    inviting=false;app.tutorialActive=false;clearTutorial();returnFocus?.isConnected&&returnFocus.focus({preventScroll:true});returnFocus=null;message(catalog.invitation.reminder,'success');
   }
  }catch{const note=$('#tutorial-save-status');if(note){note.textContent='설정을 저장하지 못했습니다. 다시 눌러 주세요.';note.setAttribute('role','alert');}}
  finally{busy=false;popup.querySelectorAll('button,input').forEach(element=>element.disabled=false);if(pendingStop){pendingStop=false;await stop();}else queueCoach();}
 }
 function drawTutorial(){
  const hadFocus=popup.open&&popup.contains(document.activeElement);
  clearTutorial({keepPopup:app.tutorialActive&&inviting});
  if(!app.tutorialActive||!progress||navigating||$('#guidebook'))return;
  if(!inviting){if(paused)resumeButton();else refreshCoach();return;}
  const card=document.createElement('section');card.id='tutorial-card';card.className='tutorial-card';
  card.innerHTML='<header class="tutorial-top"><span>튜토리얼</span><button type="button" id="tutorial-skip" aria-label="튜토리얼 닫기">×</button></header><h2 id="tutorial-title" tabindex="-1" autofocus>'+esc(catalog.invitation.title)+'</h2><p class="tutorial-invite-note">'+esc(catalog.invitation.summary)+'</p><label class="check tutorial-never"><input id="tutorial-never" type="checkbox" '+(neverAsk?'checked':'')+'>'+esc(catalog.invitation.never)+'</label><div class="inline-actions"><button type="button" id="tutorial-decline">'+esc(catalog.invitation.decline)+'</button><button type="button" id="tutorial-accept" class="primary">'+esc(catalog.invitation.accept)+'</button></div><p class="tutorial-reminder">'+esc(catalog.invitation.reminder)+'</p><p id="tutorial-save-status" role="status"></p>';
  popup.append(card);if(!popup.open)popup.showModal();else if(hadFocus)$('#tutorial-title').focus({preventScroll:true});
  $('#tutorial-never').onchange=event=>{neverAsk=event.target.checked;};$('#tutorial-accept').onclick=()=>answerInvitation(true);$('#tutorial-decline').onclick=()=>answerInvitation(false);$('#tutorial-skip').onclick=stop;
 }
 async function enterStage(){
  navigating=true;clearCoach();if(popup.open)popup.close();
  try{
   const quick=quickFor(progress.step_id),step=byId.get(progress.step_id),form=$('#research-form');
   if(quick.id==='provider_key'){
    if(!$('#connection-form')){$('#inspector').close();$('#editor').close();app.view='settings';app.rid=null;app.settingsPane='connections';app.developerSettings=false;history.replaceState(null,'','#settings');await render();}
   }else if(quick.id==='save_connection')await openScreen(step.id==='check_connection'?step:byId.get('save_connection'));
   else if(quick.id==='first_path')await openScreen({screen:'research_list',page:null,target:'#list-new'});
   else if(form?.dataset.page!==undefined)form.setResearchPage(step.page??0,{focus:false});
   else await openScreen({screen:'research_list',page:null,target:'#list-new'});
  }finally{paused=false;navigating=false;drawTutorial();}
 }
 async function replayTutorial({restart=false}={}){
  if(busy)return;
  inviting=false;if(!popup.contains(document.activeElement)&&!coach?.contains(document.activeElement))returnFocus=document.activeElement;
  paused=false;if(!app.settings)app.settings=await api('/api/control/settings');
  const stored=app.settings.preferences.tutorial_progress;
  progress=restart||!stored||stored.status==='COMPLETED'?defaults():{...currentProgress(stored),status:'IN_PROGRESS',mode:'GUIDED'};
  if(!restart&&$('#research-form')){const step=byId.get(['question','model','optional_features'][Number($('#research-form').dataset.page)||0]);progress={...progress,chapter_id:step.chapter_id,step_id:step.id};}
  app.tutorialAutoShown=true;app.tutorialActive=true;
  busy=true;
  try{await enterStage();await persist(progress);}
  catch{drawTutorial();const note=$('#tutorial-save-status');if(note){note.textContent='안내 위치를 저장하지 못했습니다. 다시 눌러 주세요.';note.setAttribute('role','alert');}}
  finally{busy=false;if(pendingStop){pendingStop=false;await stop();}else queueCoach();}
 }
 async function autoTutorial(){
  if(app.settings?.submission_mode)return;
  if(app.tutorialActive){drawTutorial();return;}
  if(app.tutorialAutoShown||app.settings.preferences.tutorial_do_not_ask)return;
  const pref=app.settings.preferences;
  if(pref.tutorial_progress?pref.tutorial_progress.status==='IN_PROGRESS':!pref.tutorial_completed){
   app.tutorialAutoShown=true;returnFocus=document.activeElement;paused=false;neverAsk=false;inviting=true;
   progress=pref.tutorial_progress?currentProgress(pref.tutorial_progress):defaults();app.tutorialActive=true;drawTutorial();
  }
 }
 async function openScreen(step){
  pauseTutorial();
  if($('#inspector').open)$('#inspector').close();
  if(step.screen==='research'){
   if(!$('#research-form'))await newResearch({skipExplanation:true});
   $('#research-form')?.setResearchPage?.(step.page??0,{focus:false});
  }else if(step.screen==='connection'&&$('#editor').open&&$('#connection-form')?.elements.adapter_id.value===progress.provider_id){
   paused=false;drawTutorial();
   const target=step.target?$('#editor-content').querySelector(step.target):null;
   target?.focus?.({preventScroll:true});target?.scrollIntoView({block:'center'});
   return;
  }else{
   $('#editor').close();
   app.view=['research_list'].includes(step.screen)?'research':step.screen==='usage'?'usage':'settings';
   app.rid=null;
   app.developerSettings=false;app.settingsPane=step.screen==='settings_help'?'help':'connections';
   history.replaceState(null,'','#'+app.view);
   await render();
   if(step.screen==='connection')connectionForm(app.settings.connections.find(connection=>connection.adapter_id===progress.provider_id),progress.provider_id);
   if(step.screen==='check'){
    const connection=app.settings.connections.find(connection=>connection.adapter_id===progress.provider_id&&connectionAvailable(connection));
    if(!connection){message('먼저 선택한 제공사의 API 연결을 저장해 주세요.');return;}
    await connectionCheck(connection.connection_id);
   }
  }
  paused=false;drawTutorial();
  const host=$('#editor').open?$('#editor-content'):$('#content'),target=step.target?host.querySelector(step.target):null;
  if(target&&target.getClientRects().length){target.focus?.({preventScroll:true});target.scrollIntoView({block:'center'});}
 }
 const aliases={readiness:'overview',model:'model',performance:'performance',attachments:'files',roles:'roles',search:'search',key:'save_connection',adaptive:'budget'};
 function matchingIssue(value){
  return catalog.issues.find(issue=>issue.codes.some(code=>String(value).includes(code)||(errors[code]&&String(value).includes(errors[code]))));
 }
 function showHelp(topic='overview'){
  if(app.tutorialActive)pauseTutorial();
  const index=String(topic).match(/^\d+$/)?Number(topic):null;
  const item=index!==null?catalog.topics[index]:catalog.topics.find(value=>value.id===topic)||(topic==='detailed_design'?window.ProbeResearchDesign?.guideTopic:null);
  guideStep=item?.step_id||aliases[topic]||(byId.has(topic)?topic:'issues');
  guideTopic=item||null;guideProvider=progress?.provider_id||'openai';guideLocal=progress?.local_tool||'LM_STUDIO';
  inspect('사용 안내','<section id="guidebook"><div class="inline-actions"><button id="guide-tutorial">튜토리얼 이어 보기</button></div><label>검색<input id="guide-search" type="search" placeholder="API 발급, 키, CSV, 예산, 401…" autocomplete="off" maxlength="100"></label><p id="guide-count" role="status"></p><div class="guide-layout"><details class="guide-toc" open><summary>목차</summary><nav id="guide-navigation" aria-label="사용 안내 목차"></nav></details><article id="guide-article" class="beginner-guide"></article></div></section>');
  $('#inspector').classList.add('usage-guide');
  $('#guide-search').oninput=event=>renderGuideNavigation(event.target.value);
  $('#guide-tutorial').onclick=async()=>{$('#inspector').close();await replayTutorial();};
  renderGuideNavigation('');renderGuideArticle();
  $('#guide-search').focus({preventScroll:true});
 }
 function renderGuideNavigation(query){
  const words=query.trim().toLocaleLowerCase('ko-KR').split(/\s+/).filter(Boolean);
  const matches=value=>words.every(word=>JSON.stringify(value).toLocaleLowerCase('ko-KR').includes(word));
  const matchingSteps=steps.filter(step=>matches(['provider_account','provider_key','save_connection'].includes(step.id)?{...step,providers:catalog.providers,local_tools:catalog.local_tools}:step)),topics=catalog.topics.filter(matches),issues=words.length?catalog.issues.filter(matches):[];
  let body=catalog.chapters.map(chapter=>{
   const children=matchingSteps.filter(step=>step.chapter_id===chapter.id);
   return children.length?'<section class="guide-chapter" data-guide-chapter="'+chapter.id+'"><h3>'+esc(chapter.title)+'</h3>'+children.map(step=>'<button type="button" data-guide-step="'+step.id+'">'+esc(lessonFor(step,guideProvider,guideLocal).title)+'</button>').join('')+'</section>':'';
  }).join('');
  if(topics.length)body+='<details class="guide-terms" '+(words.length?'open':'')+'><summary>용어와 기능 '+topics.length+'개</summary>'+topics.map(topic=>'<button type="button" data-guide-topic="'+topic.id+'">'+esc(topic.title)+'</button>').join('')+'</details>';
  if(issues.length)body+='<section><h3>오류 해결</h3>'+issues.map(issue=>'<button type="button" data-guide-issue="'+issue.id+'">'+esc(issue.title)+'</button>').join('')+'</section>';
  const designTopic=window.ProbeResearchDesign?.guideTopic;
  if(designTopic&&matches(designTopic))body+='<section class="guide-chapter"><h3>연구 설계</h3><button type="button" data-design-guide>연구를 자세히 만들기</button></section>';
  $('#guide-navigation').innerHTML=body||'<p>찾은 항목이 없습니다. 검색어를 줄이거나 오류명으로 검색해 주세요.</p>';
  $('#guide-count').hidden=!words.length;$('#guide-count').textContent=words.length?'검색 결과 '+(matchingSteps.length+topics.length+issues.length)+'개':'';
  $('#guide-navigation').querySelectorAll('[data-guide-step]').forEach(button=>button.onclick=()=>{guideStep=button.dataset.guideStep;guideTopic=null;renderGuideArticle(true);});
  $('#guide-navigation').querySelectorAll('[data-guide-topic]').forEach(button=>button.onclick=()=>{guideTopic=catalog.topics.find(topic=>topic.id===button.dataset.guideTopic);guideStep=guideTopic.step_id;renderGuideArticle(true);});
  $('#guide-navigation [data-design-guide]')?.addEventListener('click',()=>{guideTopic=designTopic;renderGuideArticle(true);});
  $('#guide-navigation').querySelectorAll('[data-guide-issue]').forEach(button=>button.onclick=()=>{const issue=catalog.issues.find(value=>value.id===button.dataset.guideIssue);$('#guide-article').innerHTML='<h3 tabindex="-1">'+esc(issue.title)+'</h3>'+issueHTML(issue)+'<button id="guide-to-issues">문제 해결 전체 보기</button>';$('#guide-to-issues').onclick=()=>{guideStep='issues';guideTopic=null;renderGuideArticle(true);};$('#guide-article h3').focus();});
 }
 function renderGuideArticle(focus=false){
  if(guideTopic?.id==='detailed_design'){
   window.ProbeResearchDesign.renderGuide($('#guide-article'));
   return;
  }
  const step=lessonFor(byId.get(guideStep)||steps[0],guideProvider,guideLocal);
  let body='<p class="eyebrow">'+esc(step.chapter_title)+'</p><h3 tabindex="-1">'+esc(guideTopic?.title||step.title)+'</h3>';
  if(guideTopic)body+='<p>'+esc(guideTopic.summary)+'</p>';
  if(['api','connection'].includes(step.chapter_id))body+=providerSelect('guide-provider',guideProvider);
  const followable=quickSteps.some(value=>value.lessons.includes(step.id))&&step.screen!=='none';
  body+=lessonHTML(step,guideProvider,guideLocal)+'<div class="inline-actions">'+(followable?'<button id="guide-follow-step">이 화면에서 따라 하기</button>':'')+'<button id="guide-recovery">문제 해결 안내</button></div>';
  $('#guide-article').innerHTML=body;
  $('#guide-provider')?.addEventListener('change',event=>{guideProvider=event.target.value;renderGuideNavigation($('#guide-search').value);renderGuideArticle();});
  $('#guide-article [data-local-tool]')?.addEventListener('change',event=>{guideLocal=event.target.value;renderGuideArticle();});
  $('#guide-follow-step')?.addEventListener('click',async()=>{$('#inspector').close();await replayTutorial();await change({...progress,mode:'GUIDED',status:'IN_PROGRESS',chapter_id:step.chapter_id,step_id:step.id,provider_id:guideProvider,local_tool:guideLocal});await openScreen(step);});
  $('#guide-recovery').onclick=()=>{guideStep='issues';guideTopic=null;renderGuideArticle(true);};
  if(focus)$('#guide-article h3').focus({preventScroll:true});
 }
 function attachError(code,host){
  const issue=matchingIssue(code);if(!host||!issue||host.querySelector('[data-guide-error]'))return;
  const button=document.createElement('button');button.type='button';button.dataset.guideError=issue.id;button.textContent='해결 방법 보기';button.onclick=()=>{showHelp('issues');$('#guide-search').value=issue.codes[0];renderGuideNavigation(issue.codes[0]);$('#guide-navigation [data-guide-issue="'+issue.id+'"]')?.click();};host.append(button);
 }
 const originalMessage=message;
 message=function(value,status='error'){originalMessage(value,status);if(value&&status!=='success')attachError(value,$('#notice'));};
 const originalEditor=editor,originalInspect=inspect,originalRender=render;
 editor=function(title,body){originalEditor(title,body);if(app.tutorialActive)drawTutorial();};
 inspect=function(title,body){originalInspect(title,body);if(app.tutorialActive)drawTutorial();};
 render=async function(){const task=originalRender(),epoch=app.epoch;await task;if(epoch!==app.epoch)return;if(app.tutorialActive)drawTutorial();else if(app.settings)await autoTutorial();};
 for(const id of ['editor','inspector'])$('#'+id).addEventListener('close',()=>{if(!$('#'+id).open){$('#'+id).classList.remove('usage-guide');queueMicrotask(()=>{if(app.tutorialActive)drawTutorial();});}});
 popup.addEventListener('cancel',event=>{event.preventDefault();stop().catch(error=>message(error.message));});
 popup.addEventListener('keydown',event=>{
  if(event.key!=='Tab')return;
  const controls=[...popup.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),summary,[tabindex]:not([tabindex="-1"])')].filter(element=>element.getClientRects().length);
  const first=controls[0],last=controls.at(-1),active=document.activeElement;
  if(event.shiftKey&&(active===first||!controls.includes(active))){event.preventDefault();last?.focus();}
  else if(!event.shiftKey&&(active===last||!controls.includes(active))){event.preventDefault();first?.focus();}
 });
 popup.addEventListener('click',event=>{const box=popup.getBoundingClientRect();if(event.target===popup&&(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom))stop().catch(error=>message(error.message));});
 const coachObserver=new MutationObserver(queueCoach);
 for(const id of ['content','editor-content','inspector-content'])coachObserver.observe($('#'+id),{childList:true,subtree:true,attributes:true,attributeFilter:['hidden','data-page','open']});
 window.addEventListener('resize',queueCoach);document.addEventListener('scroll',queueCoach,true);
 document.addEventListener('focusin',event=>{
  const route=coachRoute();if(!route||coach?.contains(event.target))return;
  const current=route.entries[coachIndex];if(current&&(current.element===event.target||current.element.contains(event.target)))return;
  const index=route.entries.findIndex(entry=>entry.element===event.target||entry.element.contains(event.target));
  if(index>=0){coachIndex=index;queueCoach();}
 });
 document.addEventListener('pointerdown',event=>{
  const menu=coach?.querySelector('.tutorial-menu[open]');if(menu&&!menu.contains(event.target))menu.open=false;
 },true);
 document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!popup.open&&coach){event.preventDefault();event.stopPropagation();stop();}},true);
 document.addEventListener('change',event=>{if(app.tutorialActive&&!inviting&&!busy&&event.target.matches('#connection-form [name=adapter_id]'))change({...progress,provider_id:event.target.value});});
 window.ProbeTutorial={draw:drawTutorial,start:replayTutorial,stop,auto:autoTutorial,showHelp,attachError,connectionSaved,researchOpened};
 window.drawTutorial=drawTutorial;window.clearTutorial=clearTutorial;window.replayTutorial=replayTutorial;window.autoTutorial=autoTutorial;
 $('#help-open').onclick=()=>showHelp();
})();
