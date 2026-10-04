'use strict';
// 기존 연결 설정 옆에 선택적 검사와 검사 기록 증거를 표시한다.
const priorLiveSettings = renderSettings;
renderSettings = async function(epoch) {
 await priorLiveSettings(epoch); if (epoch !== app.epoch) return;
 const s = app.settings, panel = document.createElement('section');
 panel.className = 'panel'; panel.id = 'live-api-tests';
 panel.innerHTML = `<h2>API 검사</h2>
 <label>저장 모델<select id="live-test-profile"><option value="">모델을 먼저 등록해 주세요.</option>${s.models.map(m=>`<option value="${esc(m.profile_id)}">${esc(m.display_name || m.model_id)}</option>`).join('')}</select></label>
 <div class="columns"><label>검사 예산 USD(미화 달러)<input id="live-test-cap" type="number" min="0.000001" max="0.25" step="any" value="0.10"></label><label>추론<select id="live-test-reasoning"><option value="">저장된 모델 설정</option>${['AUTO','DISABLED','LOW','MEDIUM','HIGH','EXTRA_HIGH','MAX'].map(v=>`<option value="${v}">${({AUTO:'기본값',DISABLED:'비활성',LOW:'낮음',MEDIUM:'보통',HIGH:'높음',EXTRA_HIGH:'매우 높음',MAX:'최대'})[v]}</option>`).join('')}</select></label></div>
 <fieldset class="test-cases"><legend>검사 항목</legend>${[['discovery','모델 목록'],['text','짧은 텍스트'],['structured','구조화 출력'],['tools','도구 호출과 응답 (2회)'],['stream','스트리밍'],['integration','앱 응답 형식 검사']].map(([v,label])=>`<label class="check"><input data-live-case="${v}" type="checkbox" ${['discovery','text'].includes(v)?'checked':''}>${label}</label>`).join('')}</fieldset>
 <div class="setup-status"><span>최대 출력 128토큰</span><span>최대 30초</span><span>자동 재시도 안 함</span><span>Skills / F3-P / Ridge · OFF</span><span>연구 성능 · 확인 전</span></div>
 <label class="check"><input id="live-test-consent" type="checkbox">선택한 검사의 비용과 예산 한도에 동의</label>
 <div class="actions"><button id="live-test-preflight">무료 사전 검사</button><button id="live-test-run" class="primary" disabled>선택 검사 실행</button></div><div id="live-test-result" role="status"></div>
 <details id="live-history"><summary>검사 이력</summary><div id="live-test-history"></div></details>`;
 $('#settings-1').after(panel);
 $('#live-test-consent').onchange=()=>{$('#live-test-run').disabled=!$('#live-test-consent').checked;};
 async function execute(live) {
  const profile=$('#live-test-profile').value;
  if(!profile){message('모델을 등록하고 API 키·단가·전송 동의를 확인해 주세요.');return;}
  const cap=$('#live-test-cap');
  if(!cap.checkValidity()){cap.reportValidity();return;}
  const cases=[...panel.querySelectorAll('[data-live-case]:checked')].map(e=>e.dataset.liveCase);
  if(!cases.length){message('검사를 하나 이상 선택하세요.');return;}
  const button=$('#live-test-run');button.disabled=true;
  try {
   const value=await api('/api/control/models/'+encodeURIComponent(profile)+'/live-test',{
    idempotency_key:crypto.randomUUID(), live_test_budget_cap:cap.value, live,
    consent:live && $('#live-test-consent').checked, cases, reasoning_level:$('#live-test-reasoning').value||null});
   if(epoch!==app.epoch)return;
   $('#live-test-result').dataset.status=['OFFLINE_PREFLIGHT_PASS','LIVE_SELECTED_TESTS_PASS','OFFLINE_SIMULATION_PASS'].includes(value.status)?'success':'error';
   $('#live-test-result').textContent=`검사 기록 ${value.api_test_session_id} · ${state(value.status)} · ${value.stop_reason?(errors[value.stop_reason]||state(value.stop_reason)):'검사 완료'} · 응답 생성 요청 ${value.totals.live_generation_request_count}회`;
   await history();
   inspect('API 검사 상세', liveTestHTML(value));
  } catch(x) {message(errors[x.message]||x.message);}
  finally {if(epoch===app.epoch)button.disabled=!$('#live-test-consent').checked;}
 }
 $('#live-test-preflight').onclick=()=>execute(false);
 $('#live-test-run').onclick=()=>execute(true);
 async function history() {
  const rows=await api('/api/control/live-api-tests');if(epoch!==app.epoch)return;
  $('#live-test-history').innerHTML=rows.length?table(['시작','실행','결과','검사 기록'],rows.map(r=>[
   esc(r.started_at),esc(state(r.execution)),esc(state(r.status)),`<button data-live-session="${esc(r.api_test_session_id)}">상세</button>`])):'<span class="muted">검사 기록 없음</span>';
  panel.querySelectorAll('[data-live-session]').forEach(b=>b.onclick=async()=>{try{
   const value=await api('/api/control/live-api-tests/'+encodeURIComponent(b.dataset.liveSession));
   if(epoch===app.epoch)inspect('API 검사 상세',liveTestHTML(value));
  }catch(x){message(x.message);}});
 }
 arrangeSettings();
 await history();
};
function arrangeSettings(groups = null) {
 const nav=$('.settings-nav'),host=document.createElement('div');host.className='settings-content';
 groups ??=[['connections','API 연결',['settings-1']],['checks','API 검사',['live-api-tests']],['budget','예산',['settings-4']],['advanced','성능 최적화',['resource-settings']],['environment','실행 환경',['settings-5','settings-6','settings-7']]];
 const panes=[],buttons=[];nav.after(host);
 for(const [key,label,ids] of groups){
  const pane=document.createElement('section');pane.id='settings-pane-'+key;pane.className='settings-pane';pane.setAttribute('role','tabpanel');pane.setAttribute('aria-labelledby','settings-tab-'+key);pane.tabIndex=0;
  ids.forEach(id=>pane.append($('#'+id)));host.append(pane);panes.push(pane);
  const button=document.createElement('button');button.type='button';button.id='settings-tab-'+key;button.dataset.settingsTab=key;button.textContent=label;button.setAttribute('role','tab');button.setAttribute('aria-controls',pane.id);nav.append(button);buttons.push(button);
 }

 function select(key,focus=false){
  app.settingsPane=key;
  buttons.forEach((b,i)=>{const active=b.dataset.settingsTab===key;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;panes[i].hidden=!active;if(active&&focus)b.focus();});
 }
 buttons.forEach((b,i)=>{b.onclick=()=>select(b.dataset.settingsTab);b.onkeydown=e=>{let next;if(e.key==='ArrowRight')next=(i+1)%buttons.length;else if(e.key==='ArrowLeft')next=(i+buttons.length-1)%buttons.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=buttons.length-1;else return;e.preventDefault();select(buttons[next].dataset.settingsTab,true);};});
 select(groups.some(g=>g[0]===app.settingsPane)?app.settingsPane:'connections');
}
function liveTestHTML(v) {
 const rows=v.attempts.map(a=>[esc(a.case_id),esc(a.identity.provider_reported_model||'미확인'),
  esc(state(a.requested_parameters.reasoning_policy||'NOT_TESTED')),esc(state(a.status)),
  esc(a.usage.input_tokens.value??'미확인'),esc(a.usage.output_tokens.value??'미확인'),
  esc(a.cost.observed_cost_usd??'미확인'),esc(a.latency.request_latency_ms??'미확인')]);
 return `<p>검사 기록 ${esc(v.api_test_session_id)} · ${esc(state(v.execution))} · ${esc(state(v.status))}</p><p>예산 ${esc(v.live_test_budget_cap)} USD(미화 달러) · 응답 생성 요청 ${v.totals.live_generation_request_count}회 · 자동 재시도 0회 · 연구 성능 확인 전</p>
 ${fields(v.connection_checks)}${table(['검사','모델','추론','결과','입력 토큰','출력 토큰','예상 비용 USD(미화 달러)','응답 시간 (ms)'],rows)}
 ${details(v.manifest,'전송 전에 고정한 검사 명세')}${details(v.attempts,'검사 요청 → 예산 확보 → 실제 전송 내용 → 응답')}
 ${details(v.ledger,'비용 내역 · 제공사 청구서와 다름')}
 ${fields({'저장 경로':'workspace/qa/live_api_test/'+v.api_test_session_id})}`;
}
