// 많은 연구·통합 화면·상태 갱신을 실제 Chrome과 별도 고정 작업 공간에서 확인한다.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/research-base-v8/browser',crypto.randomUUID());
fs.mkdirSync(folder,{recursive:true});
const checks=[],errors=[],external=[],violations=[];let child,browser,page;
function save(name,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8');fs.writeFileSync(path.join(folder,name),Buffer.from(text,'utf8'));}
function check(name,value){checks.push({name,passed:!!value});save('progress.json',{checks,errors});if(!value)throw Error(name);}
(async()=>{
 try{
  const env={...process.env};for(const key of Object.keys(env))if(key.endsWith('API_KEY'))delete env[key];
  child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/research_report_browser_fixture.py',folder,'--board','--many'],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
  let stderr='';child.stderr.on('data',s=>stderr+=s);
  const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',v=>{text+=v;const m=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m)resolve(m[0]);});child.on('exit',()=>reject(Error(stderr.slice(-2000))));setTimeout(()=>reject(Error('서버 시간 초과')),45000).unref();});
  const origin=new URL(url).origin,ids=JSON.parse(fs.readFileSync(path.join(folder,'fixture.json'),'utf8'));
  browser=await chromium.launch({channel:'chrome',headless:true});page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(20000);
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith(origin+'/')&&!r.url().startsWith('blob:'))external.push(r.url());});
  await page.addInitScript(()=>document.addEventListener('securitypolicyviolation',e=>(window.v8Violations??=[]).push(e.effectiveDirective)));
  await page.goto(url);await page.locator('#research-table').waitFor();
  const observed=()=>page.evaluate(()=>api('/qa/observed')),before=await observed();
  const open=async(rid,tab='overview')=>{await page.goto(origin+'/#research/'+rid+'/'+tab);await page.locator('#research-workspace[data-research-id="'+rid+'"][data-active-tab="'+(['progress','live','current','technical'].includes(tab)?'flow':tab)+'"]').waitFor();};
  const search=async q=>{await page.locator('#research-search').fill(q);await page.locator('#research-list-search button').click();};
  await search('목록 검사');await page.waitForFunction(()=>document.querySelectorAll('.research-list-card').length===50);
  check('60개 연구를 50개 카드로 분할',await page.locator('.research-list-card').count()===50&&await page.locator('#list-next').isEnabled());
  const first=await page.locator('[data-run]').first().getAttribute('data-run');
  check('목록 제목은 두 줄',await page.locator('.research-card-title').first().evaluate(e=>getComputedStyle(e).webkitLineClamp==='2'));
  await page.locator('#list-next').click();await page.waitForFunction(()=>document.querySelectorAll('.research-list-card').length===10);
  check('다음 목록은 나머지 10개',await page.locator('.research-list-card').count()===10&&await page.locator('#list-next').isDisabled());
  await search('목록 검사 07');await page.waitForFunction(()=>document.querySelectorAll('.research-list-card').length===1);
  check('연구 제목·질문 검색',await page.locator('.research-card-title').innerText().then(t=>t.includes('07')));
  await page.locator('[data-run]').click();await page.locator('[data-tab=overview][aria-current=page]').waitFor();
  check('카드의 기본 진입은 개요',page.url().endsWith('/overview'));
  check('통합 연구 헤더·공식 상태 각각 하나',await page.locator('.research-header').count()===1&&await page.locator('#run-state').count()===1);
  check('주 탭 순서·일곱 개',JSON.stringify(await page.locator('[data-primary-tab]').allTextContents())===JSON.stringify(['개요','연구 흐름','진행 기록','근거·자료','실험/분석','검증','보고서']));
  check('상단 긴 제목 두 줄',await page.locator('#research-title').evaluate(e=>getComputedStyle(e).webkitLineClamp==='2'));
  await page.locator('#title-expand').click();check('전체 제목 펼치기',await page.locator('#title-expand').getAttribute('aria-expanded')==='true'&&await page.locator('#research-title').evaluate(e=>getComputedStyle(e).display==='block'));await page.locator('#title-expand').click();
  await open(ids.research_id,'progress');await page.locator('#stage-map').waitFor();
  check('이전 진행 주소도 같은 화면',await page.locator('.research-header').count()===1&&await page.locator('#phase-rail,.current-work,#technical-research').count()===0);
  check('흐름 범주 첫 항목은 전체 연구',await page.locator('[data-flow-category]').first().innerText()==='전체 연구'&&await page.locator('[data-flow-category]').count()===5);
  check('7단계 텍스트·상태·연결 표시',await page.locator('.stage-card').count()===7&&await page.locator('.stage-status').count()===7&&await page.locator('.stage-card').first().evaluate(e=>getComputedStyle(e,':after').content.includes('→')));
  check('최근 상태 변화는 다섯 개',await page.locator('#flow-timeline li').count()===5);
  check('현재·마지막·다음 작업 요약',await page.locator('.work-summary section').count()===3&&!((await page.locator('.work-summary').innerText()).includes('evidence_text')));
  await page.locator('[data-flow-category=knowledge]').click();await page.locator('.claim-card').first().waitFor();
  check('주장과 검증 근거 연결 카드',await page.locator('.claim-counts').count()>0&&await page.locator('.claim-counts').first().innerText().then(t=>t.includes('인용 확인')));
  await page.locator('[data-claim-evidence]').first().click();await page.locator('#research-detail').waitFor();
  check('연결 근거의 우측 상세',await page.locator('#detail-body').innerText().then(t=>t.includes('입력·결과·근거·오류')));await page.locator('#detail-close').click();
  await page.locator('[data-flow-category=experiment]').click();await page.locator('.variable-map').waitFor();
  check('변인 구조·측정 절차·설계 표시',await page.locator('.variable-map>div').count()===3&&await page.locator('.design-preview .tag').innerText()==='설계안');
  await page.locator('[data-flow-category=verification]').click();await page.locator('.verification-summary').waitFor();
  check('검증과 결론 현재성 표시',await page.locator('.verification-summary').innerText().then(t=>t.includes('현재 검증')&&t.includes('재검증 필요')));
  await page.locator('[data-flow-category=tools]').click();await page.locator('#flow-board-content').getByText(/산출물/).waitFor();
  check('실행 도구·산출물 집계',await page.locator('#flow-board-content').innerText().then(t=>t.includes('도구 종류')&&t.includes('산출물')));
  await page.locator('[data-flow-category=all]').click();
  check('세부 관계는 기본으로 접어서 보기',!await page.locator('.flow-viewport').isVisible());await page.locator('.relationship-details summary').click();await page.locator('.flow-node').first().waitFor();
  await page.locator('.flow-node').first().click();await page.locator('#research-detail').waitFor();
  const nodeState=await page.evaluate(()=>{const host=document.querySelector('#research-flow'),v=document.querySelector('.flow-viewport');window.v8Node=document.querySelector('.flow-node');v.scrollLeft=250;return {layout:host.dataset.layoutRuns,scroll:v.scrollLeft};});
  await page.evaluate(()=>window.ResearchWorkspace.refresh());
  check('갱신은 흐름 노드·스크롤·선택 보존',await page.evaluate(s=>window.v8Node===document.querySelector('.flow-node')&&document.querySelector('#research-flow').dataset.layoutRuns===s.layout&&document.querySelector('.flow-viewport').scrollLeft===s.scroll&&!document.querySelector('#research-detail').hidden,nodeState));
  await page.locator('#detail-close').click();
  await page.locator('[data-flow-category=all]').focus();await page.keyboard.press('ArrowDown');check('범주 키보드 이동',await page.locator('[data-flow-category=knowledge]').evaluate(e=>document.activeElement===e));
  const viewed=await observed();check('목록·탭·관계 조회는 연구·비용 기록 불변',JSON.stringify(viewed.counts)===JSON.stringify(before.counts)&&viewed.model_calls.length===before.model_calls.length&&viewed.searches===before.searches);
  await page.evaluate(()=>api('/qa/state',{status:'RUNNING'}));await page.evaluate(()=>window.ResearchWorkspace.refresh());
  check('실행 중 자동 갱신·파란 상태',await page.locator('#run-state').getAttribute('data-tone')==='running'&&await page.locator('[data-phase=report]').getAttribute('data-status')==='RUNNING');
  await page.evaluate(()=>api('/qa/state',{status:'COMPLETED'}));await page.waitForFunction(()=>document.querySelector('#run-state')?.textContent==='완료',{},{timeout:15000});check('자동 종료 갱신·초록 상태',await page.locator('#run-state').innerText()==='완료'&&await page.locator('#run-state').getAttribute('data-tone')==='complete');
  await page.screenshot({path:path.join(folder,'flow-desktop.png'),fullPage:true});
  for(const width of [390,1440]){await page.setViewportSize({width,height:1000});check('흐름 '+width+'px 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));if(width===390)await page.screenshot({path:path.join(folder,'flow-390.png'),fullPage:true});}
  await open(ids.paused_id);check('일시정지·계속 실행 제어',await page.locator('#run-state').innerText()==='일시정지'&&await page.locator('[data-command=resume]').isVisible());
  await open(ids.stale_id);check('오래된 결과는 공식 재검증 상태',await page.locator('#run-state').innerText()==='재검증 필요'&&await page.locator('[data-currentness=STALE]').count()>0);
  await page.locator('[data-primary-tab=report]').click();await page.locator('#report-currentness').waitFor();check('오래된 보고서 자동 렌더 차단',await page.locator('#pdf-canvas').count()===0);
  await open(ids.blocked_id);check('차단 원인과 보완 조치 표시',await page.locator('#run-blocker').innerText().then(t=>t.includes('공개 검색 동의가 없어'))&&await page.locator('#repair-search').isVisible());
  await open(ids.research_id,'timeline');await page.locator('.research-timeline').waitFor();check('공통 타임라인·내부 사고 비노출',await page.locator('.research-timeline').count()===1&&!((await page.locator('#content').innerText()).includes('private-ui-v8-canary')));
  await page.locator('#run-back').click();await page.locator('#research-table').waitFor();await search('');await page.locator('[data-filter=completed]').click();await page.waitForFunction(()=>document.querySelectorAll('.research-list-card').length===2);
  check('완료 필터·공식 재검증 상태 일치',await page.locator('.research-list-card .tag').allTextContents().then(t=>t.includes('완료')&&t.includes('재검증 필요')));
  violations.push(...await page.evaluate(()=>window.v8Violations||[]));check('브라우저 오류·CSP·외부 요청 없음',!errors.length&&!violations.length&&!external.length);
 }catch(e){errors.push(e.stack||e.message);if(page)await page.screenshot({path:path.join(folder,'failure.png'),fullPage:true}).catch(()=>{});process.exitCode=1;}
 finally{if(browser)await browser.close();if(child)child.kill();save('result.json',{checks,errors,external,violations,paid_calls:0,execution:'REAL_CHROME_OFFLINE_RECORDED_STATE_FIXTURE'});console.log(JSON.stringify({folder,checks:checks.length,passed:checks.every(v=>v.passed)&&!errors.length,errors},null,2));}
})();
