// 실제 Chrome·로컬 Python 앱에서 학습 과정과 모의 오류를 검사한다.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/tutorial/browser',crypto.randomUUID()),out=path.join(root,'output/playwright/tutorial');
fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(out,{recursive:true});
let child,browser,testPage;const checks=[],errors=[],requests=[],shots=[],preferenceErrors=[],localRequests=[];
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 검사 실패');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,value){checks.push({name,passed:!!value});save(path.join(folder,'progress.json'),{checks,errors});if(!value)throw Error(name);}
async function main(){
 const env={...process.env};for(const key of Object.keys(env))if(key.endsWith('API_KEY'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/tutorial_browser_fixture.py',folder],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env});
 let stderr='';child.stderr.on('data',value=>stderr+=value);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',value=>{text+=value;const match=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(match)resolve(match[0]);});child.on('exit',()=>reject(Error(stderr.slice(-1400))));setTimeout(()=>reject(Error('서버 시간 초과')),15000).unref();});
 const origin=new URL(url).origin;browser=await chromium.launch({channel:'chrome',headless:process.env.PROBE_UI_TEST_HEADED!=='1'});
 const page=testPage=await browser.newPage({viewport:{width:1280,height:900}});page.setDefaultTimeout(16000);
 page.on('pageerror',error=>errors.push(error.message));
 page.on('response',async response=>{if(new URL(response.url()).pathname==='/api/control/preferences'&&response.status()>=400){const body=await response.json().catch(()=>({}));preferenceErrors.push({status:response.status(),error:body.error});}});
 page.on('request',request=>{if(!request.url().startsWith(origin+'/')&&!request.url().startsWith('blob:'))errors.push('외부 요청');const requestPath=new URL(request.url()).pathname;if(request.url().startsWith(origin+'/')&&/^\/(api|qa)\//.test(requestPath))localRequests.push(Date.now());if(request.method()==='POST')requests.push({path:requestPath,body:request.postData()});});
 // 실제 앱의 분당 180회 제한 안에서 자동 검사를 진행한다.
 const pace=async()=>{while(localRequests.filter(time=>Date.now()-time<61000).length>=100)await new Promise(resolve=>setTimeout(resolve,500));};
 const state=async()=>{await pace();return page.evaluate(()=>api('/qa/state'));};
 const fault=async code=>{await pace();return page.evaluate(value=>api('/qa/fault',{code:value}),code);};
 const shot=async name=>{const target=path.join(out,name+'.png');await page.screenshot({path:target,fullPage:true});shots.push(path.relative(root,target).replaceAll('\\','/'));};
 const resume=async()=>{await pace();await page.waitForFunction(()=>document.querySelector('#tutorial-resume')?.getClientRects().length||document.querySelector('#tutorial-coach')?.matches(':popover-open'));if(!await page.locator('#tutorial-coach').count())await page.locator('#tutorial-resume').click();await page.locator('#tutorial-coach').waitFor();};
 const menu=async()=>{await resume();if(!await page.locator('.tutorial-menu').evaluate(element=>element.open))await page.locator('.tutorial-menu>summary').click();};
 const guide=async()=>{await menu();await page.locator('#tutorial-guide').click();await page.locator('#guidebook').waitFor();};
 const restart=async()=>{await pace();await menu();await page.locator('#tutorial-restart').click();await page.locator('#provider-preparation').waitFor();};
 const stage=()=>page.evaluate(()=>{const id=app.settings.preferences.tutorial_progress.step_id;return ProbeTutorialContent.quick_start.find(value=>value.lessons.includes(id)).id;});
 const acceptIfAsked=async()=>{await page.locator('#tutorial-accept').waitFor();await page.locator('#tutorial-accept').click();await page.locator('#tutorial-coach').waitFor();await page.waitForFunction(()=>app.settings?.preferences.tutorial_progress?.status==='IN_PROGRESS');};
 const coachAt=async selector=>{await page.waitForFunction(value=>document.querySelector('#tutorial-coach')?.dataset.target===value&&document.querySelector('#tutorial-coach')?.matches(':popover-open'),selector);};
 const clearTarget=async()=>page.evaluate(()=>{const target=document.querySelector('.tutorial-coach-target');if(!target)return false;const box=target.getBoundingClientRect(),top=document.elementFromPoint(box.left+box.width/2,box.top+box.height/2);return target===top||target.contains(top);});
 const inViewport=()=>page.locator('.tutorial-coach-box').evaluate(element=>{const box=element.getBoundingClientRect();return box.left>=0&&box.right<=innerWidth&&box.top>=0&&box.bottom<=innerHeight;});
 const follow=async id=>{await guide();await page.locator('#guide-search').fill('');await page.locator('[data-guide-step="'+id+'"]').click();await page.locator('#guide-follow-step').click();await page.waitForFunction(value=>app.settings?.preferences.tutorial_progress?.step_id===value,id);await page.locator('#tutorial-coach').waitFor();};
 const nextStage=async()=>{const previous=await stage();await page.locator('#tutorial-coach-next').click();await page.waitForFunction(value=>{const id=app.settings.preferences.tutorial_progress.step_id;return ProbeTutorialContent.quick_start.find(step=>step.lessons.includes(id)).id!==value;},previous);await page.locator('#tutorial-coach').waitFor();};
 const waitDraft=async profile=>{const deadline=Date.now()+16000;while(Date.now()<deadline){const saved=await page.evaluate(()=>api('/api/control/research/draft'));if(saved.draft?.model_profile_id===profile&&profile)return;await new Promise(resolve=>setTimeout(resolve,50));}throw Error('선택 모델의 초안 자동 저장을 확인하지 못했습니다.');};
 await page.route('**/assets/tutorial.js',async route=>{await new Promise(resolve=>setTimeout(resolve,500));await route.continue();});
 await page.goto(url);await page.locator('#tutorial-card').waitFor();await page.unroute('**/assets/tutorial.js');
 check('안내 스크립트 로딩 지연에도 최초 화면 정상 표시',!await page.locator('#notice').innerText().then(text=>text.includes('not defined')));
 check('최초 팝업에서 보기·건너뛰기·다시 보지 않기 선택',await page.locator('#tutorial-accept').innerText()==='튜토리얼 보기'&&await page.locator('#tutorial-decline').innerText()==='건너뛰기'&&await page.locator('#tutorial-never').isVisible()&&await page.locator('.tutorial-menu').count()===0);
 check('참여 선택은 본문·설정과 분리된 독립 팝업',await page.locator('body>#tutorial-popup[open] #tutorial-card').count()===1&&await page.locator('#content #tutorial-card,#editor #tutorial-card,#inspector #tutorial-card').count()===0);
 await page.keyboard.press('Tab');await page.keyboard.press('Shift+Tab');check('참여 선택의 키보드 포커스 유지',await page.evaluate(()=>$('#tutorial-popup').contains(document.activeElement)));
 const initial=await state();check('키·연구 없이 최초 학습 가능',initial.keys===0&&initial.researches===0&&initial.agents===0&&initial.requests===0);
 check('참여 선택 전에 학습 위치·화살표 생성 없음',initial.preferences.tutorial_progress===null&&await page.locator('#tutorial-coach').count()===0);
 await page.locator('#tutorial-decline').click();await page.locator('#tutorial-card').waitFor({state:'detached'});
 const skipped=await state();check('이번만 건너뛰기는 완료·중단·다시 묻지 않기와 구분',!skipped.preferences.tutorial_do_not_ask&&!skipped.preferences.tutorial_completed&&skipped.preferences.tutorial_progress===null);
 check('건너뛰면 설정의 다시 보기 위치를 초록 알림으로 표시',await page.locator('#notice[data-status=success]').innerText().then(text=>text.includes('설정 → 도움말 및 안내')));
 await pace();await page.reload();await page.locator('#tutorial-accept').waitFor();check('이번만 건너뛴 뒤 다음 실행에서 다시 선택',await page.locator('#tutorial-accept').isVisible());
 await page.route('**/api/control/preferences',route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:'CONFIG_STALE'})}));
 await page.locator('#tutorial-accept').click();await page.locator('#tutorial-save-status[role=alert]').waitFor();check('참여 선택 저장 실패 시 기존 학습 상태 보존',await page.locator('#tutorial-accept').isVisible()&&(await state()).preferences.tutorial_progress===null);
 await page.unroute('**/api/control/preferences');await page.locator('#tutorial-never').check();await acceptIfAsked();
 check('보기는 네 과정 안내를 시작하고 다시 묻지 않기도 저장',await stage()==='provider_key'&&(await state()).preferences.tutorial_do_not_ask);
 await coachAt('#connection-new');check('독립 안내 상자 하나·화살표 하나와 실제 연결 버튼 접근',await page.locator('.tutorial-coach-box').count()===1&&await page.locator('.tutorial-coach-arrow').count()===1&&await clearTarget());
 check('참여 후 중앙 정보 팝업·옛 작업 버튼·단계 선택 제거',!await page.locator('#tutorial-popup').isVisible()&&await page.locator('#tutorial-card,#tutorial-open,#tutorial-next,[data-tutorial-step],#tutorial-example').count()===0);
 await page.locator('#tutorial-coach-close').click();await page.locator('#tutorial-coach').waitFor({state:'detached'});
 await page.evaluate(()=>api('/api/control/preferences/reset',{}));await pace();await page.reload();await page.locator('#tutorial-never').check();await page.locator('#tutorial-decline').click();await page.locator('#tutorial-card').waitFor({state:'detached'});
 check('다시 보지 않기는 학습 완료와 별개로 저장',(await state()).preferences.tutorial_do_not_ask&&!(await state()).preferences.tutorial_completed);
 await page.locator('[data-view=research]').click();await pace();await page.reload();await page.locator('#research-table').waitFor();check('다시 보지 않기 이후 자동 팝업 없음',await page.locator('#tutorial-card').count()===0);
 await page.locator('[data-view=settings]').click();await page.locator('#settings-tab-help').click();await page.locator('#replay-tutorial').click();await page.locator('#provider-preparation').waitFor();
 check('다시 보지 않기를 선택해도 설정에서 재실행 가능',await page.locator('#tutorial-coach').isVisible()&&!await page.locator('#tutorial-popup').isVisible());
 await page.evaluate(()=>api('/api/control/preferences',{tutorial_do_not_ask:false}));
 await guide();const beforeGuide=await state();
 check('도움말 무료 예시·총 과정 문구 제거',await page.locator('#guide-example,#learning-example').count()===0&&!await page.locator('#inspector').innerText().then(t=>t.includes('9개 과정')||t.includes('상세 검사')||t.includes('무료 예시')));
 check('검색 이름 간소화',await page.locator('#guide-search').locator('..').innerText()==='검색');
 await page.locator('#guide-search').fill('API 키');await page.locator('[data-guide-step=provider_key]').click();
 check('필수 발급 순서가 계정·결제 추가 안내보다 먼저',await page.locator('.provider-lesson').evaluate(el=>el.querySelector('ol').getBoundingClientRect().top<el.querySelector('.provider-extra').getBoundingClientRect().top&&el.querySelectorAll('strong').length>=3));
 const afterGuide=await state();
 check('도움말 이동은 연구·Agent·원장·검사 생성 없음',beforeGuide.researches===afterGuide.researches&&beforeGuide.agents===afterGuide.agents&&beforeGuide.requests===afterGuide.requests&&beforeGuide.checks===afterGuide.checks);
 check('도움말 이동은 학습 진행 상태 보존',JSON.stringify(afterGuide.preferences.tutorial_progress)===JSON.stringify(beforeGuide.preferences.tutorial_progress));
 check('안내 이동이 실행 API를 호출하지 않음',!requests.some(request=>/\/research$|\/start$|\/resume$|\/check$/.test(request.path)));
 await page.locator('#inspector-close').click();await resume();
 check('핵심 과정은 발급·연결·새 연구·항목 입력 순서',await page.evaluate(()=>ProbeTutorialContent.quick_start.map(step=>step.id).join(',')==='provider_key,save_connection,first_path,question'));
 for(const provider of ['openai','anthropic','google_gemini','xai','deepseek','mistral']){
  await page.locator('#tutorial-provider').selectOption(provider);await page.waitForFunction(value=>$('#provider-preparation')?.dataset.provider===value,provider);
  check('발급 안내 '+provider+'만 자세히 표시',await page.locator('#provider-preparation').count()===1&&await page.locator('#provider-preparation .provider-extra ul>li').count()>=2&&await page.locator('#provider-preparation>ol>li').count()>=5);
  check('발급 안내 '+provider+'의 복사·요금·공식 링크',await page.locator('#provider-preparation').innerText().then(text=>text.includes('복사'))&&await page.locator('#provider-preparation a').count()>=2);
 }
 await page.locator('#tutorial-provider').selectOption('openai');await page.waitForFunction(()=>$('#provider-preparation')?.dataset.provider==='openai');
 for(const width of [390,1280]){await page.setViewportSize({width,height:900});await page.waitForFunction(()=>{const box=$('.tutorial-coach-box').getBoundingClientRect();return box.left>=0&&box.right<=innerWidth&&box.top>=0&&box.bottom<=innerHeight;});await shot('provider-'+width);check('발급 글 '+width+'px 안내 상자·가로 넘침·실제 버튼 접근',await inViewport()&&await clearTarget()&&await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));}
 check('화살표 안내의 버튼 간격 유지',await page.locator('.tutorial-coach-actions').evaluate(element=>parseFloat(getComputedStyle(element).gap)>=8));
 await page.route('**/api/control/preferences',route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:'CONFIG_STALE'})}));
 await page.locator('#tutorial-coach-next').click();await page.locator('#tutorial-save-status[role=alert]').waitFor();
 check('학습 위치 저장 실패에 이전 단계 보존',await stage()==='provider_key'&&(await state()).preferences.tutorial_progress.completed_steps.length===0);
 check('저장 실패 후 이동 버튼·제공사 선택 재시도 가능',await page.locator('#tutorial-coach-next').isEnabled()&&await page.locator('#tutorial-provider').isEnabled());
 await page.unroute('**/api/control/preferences');await nextStage();await page.locator('#connection-form').waitFor();
 check('발급 다음 과정에서 실제 앱 API 연결 양식 열기',await stage()==='save_connection'&&!await page.locator('#tutorial-popup').isVisible());
 check('읽은 단계와 현재 위치 저장',(await state()).preferences.tutorial_progress.completed_steps.join(',')==='provider_key');
 await pace();await page.reload();await acceptIfAsked();check('새로고침 후 참여 선택으로 연결 위치 복구',await stage()==='save_connection'&&await page.locator('#connection-form').count()===1);
 for(let i=0;i<2;i++){await pace();await page.reload();await acceptIfAsked();check('반복 새로고침 '+(i+1)+' 학습 위치 보존',await stage()==='save_connection');}
 await page.locator('#tutorial-coach-close').click();await page.locator('#tutorial-coach').waitFor({state:'detached'});
 const dismissed=(await state()).preferences.tutorial_progress;check('닫기는 완료와 구분',dismissed.status==='DISMISSED'&&dismissed.completed_steps.length===1);
 await pace();await page.reload();await page.locator('#settings-tab-help').waitFor();check('닫은 안내 자동 강제 재표시 없음',await page.locator('#tutorial-card,#tutorial-coach').count()===0);
 check('참여 선택과 다시 보기 이후에도 설정 화면 유지',await page.locator('[data-view=settings]').getAttribute('aria-current')==='page');
 await page.locator('#help-open').click();await page.locator('#guidebook').waitFor();
 check('사용 안내 9개 과정·30개 단계·41개 기존 주제',await page.locator('[data-guide-chapter]').count()===9&&await page.locator('[data-guide-step]').count()===30&&await page.locator('[data-guide-topic]').count()===41);
 await page.locator('#guide-search').fill('401');await page.locator('[data-guide-issue=auth]').click();
 check('오류명 검색과 구체적인 다음 조치',await page.locator('#guide-article').innerText().then(text=>text.includes('API 키 인증 실패')&&text.includes('새 키를 저장')));
 await page.locator('#guide-search').fill('발급');check('키 발급 검색 결과 있음',await page.locator('[data-guide-step=provider_key]').count()===1);
 await page.locator('[data-guide-step=provider_key]').click();
 for(const provider of ['openai','anthropic','google_gemini','xai','deepseek','mistral','openai_compatible']){
  await page.locator('#guide-provider').selectOption(provider);
  check('선택 제공사 '+provider+'만 표시',await page.locator('.provider-lesson').count()===1&&await page.locator('.provider-lesson').getAttribute('data-provider')===provider);
  await page.locator('.provider-lesson .provider-extra>summary').click();
  check('제공사 '+provider+' 발급·오류·공식 문서 제공',await page.locator('.provider-lesson').innerText().then(text=>text.includes('공식 안내 확인: 2026-10-04'))&&await page.locator('.provider-lesson a').count()>0);
 }
 await page.locator('[data-local-tool]').selectOption('OLLAMA');check('Ollama 실제 로컬 주소 안내',await page.locator('.provider-lesson').innerText().then(text=>text.includes('127.0.0.1:11434/v1')));
 await page.locator('[data-local-tool]').selectOption('LM_STUDIO');check('LM Studio 실제 로컬 주소 안내',await page.locator('.provider-lesson').innerText().then(text=>text.includes('127.0.0.1:1234/v1')));
 check('로컬 안내에 클라우드 키 발급 조작 없음',!await page.locator('#guide-article').innerText().then(text=>text.includes('API 키 발급 화면')||text.includes('키 생성 버튼')));
 await page.locator('#guide-search').fill('Ollama');check('로컬 도구 이름으로 전체 준비·연결 절차 검색',await page.locator('[data-guide-step=provider_account]').count()===1&&await page.locator('[data-guide-step=provider_key]').count()===1&&await page.locator('[data-guide-step=save_connection]').count()===1);await page.locator('#guide-search').fill('발급');
 await page.locator('#guide-provider').selectOption('openai');
 check('공식 링크 새 창·보안 속성',await page.locator('.provider-lesson a').evaluateAll(elements=>elements.every(element=>element.target==='_blank'&&element.rel.includes('noopener')&&element.rel.includes('noreferrer')&&element.href.startsWith('https://'))));
 await shot('api-key-guide');
 await page.locator('#guide-search').focus();await page.keyboard.press('Tab');check('키보드로 목차 접근',await page.evaluate(()=>document.activeElement.tagName==='SUMMARY'||document.activeElement.tagName==='BUTTON'));
 for(const width of [390,1280]){
  await page.setViewportSize({width,height:900});check('사용 안내 '+width+'px 가로 넘침 없음',await page.locator('#inspector').evaluate(element=>element.scrollWidth<=element.clientWidth+2));await shot('guide-'+width);
 }
 await page.evaluate(()=>document.documentElement.style.zoom='2');check('사용 안내 200% 확대 가로 넘침 없음',await page.locator('#inspector').evaluate(element=>element.scrollWidth<=element.clientWidth+2));await page.evaluate(()=>document.documentElement.style.zoom='1');

 await page.locator('#inspector-close').click();await page.locator('[data-view=settings]').click();await page.locator('#settings-tab-help').click();await page.locator('#replay-tutorial').click();await page.locator('#connection-form').waitFor();
 check('설정에서 중단 위치 이어 보기',await stage()==='save_connection');
 await page.keyboard.press('Escape');await page.locator('#tutorial-coach').waitFor({state:'detached'});
 check('Escape 닫기에서 완료로 잘못 저장하지 않음',(await state()).preferences.tutorial_progress.status==='DISMISSED');
 check('안내 종료는 연결 창을 닫지 않음',await page.locator('#editor').isVisible());
 await page.locator('#editor-close').click();await page.locator('#settings-tab-help').click();await page.locator('#show-explanations').check();await page.waitForFunction(()=>app.settings?.preferences.new_research_explanations);
 check('설명 설정 변경이 학습 위치를 덮어쓰지 않음',(await state()).preferences.tutorial_progress.step_id==='save_connection');
 await page.locator('#show-explanations').uncheck();await page.waitForFunction(()=>app.settings&&!app.settings.preferences.new_research_explanations);
 await page.locator('#replay-tutorial').click();await restart();await nextStage();await page.locator('#connection-form').waitFor();await coachAt('#connection-form [name=api_key]');
 check('실제 입력에 중앙 팝업 없이 입력 위치 강조',!await page.locator('#tutorial-popup').isVisible()&&await page.locator('#editor-content #tutorial-card').count()===0&&await page.locator('#connection-form [name=api_key].tutorial-focus').count()===1);
 check('안내가 실제 입력 창을 분할하거나 넓히지 않음',await page.locator('.tutorial-layout,.tutorial-workspace').count()===0&&await page.locator('#editor').evaluate(element=>element.getBoundingClientRect().width<=680));
 check('유료 실행 없이 연결 양식만 열기',(await state()).checks===0&&requests.filter(request=>request.path.endsWith('/register')).length===0);
 for(const width of [390,1280]){await page.setViewportSize({width,height:900});await page.locator('#connection-form [name=api_key]').scrollIntoViewIfNeeded();await coachAt('#connection-form [name=api_key]');check('API 입력 화살표 '+width+'px 위치·가로 넘침·입력 접근',await clearTarget()&&await inViewport());await shot('visual-key-'+width);}
 await page.locator('#tutorial-coach-next').click();await coachAt('#connection-form [name=display_name]');check('다음 안내로 실제 연결 이름 입력칸 하나만 강조',await clearTarget()&&await page.locator('.tutorial-focus').count()===1&&await page.locator('#connection-form [name=display_name]').getAttribute('aria-describedby').then(value=>value.includes('tutorial-coach-text')));
 await page.locator('#tutorial-coach-prev').click();await coachAt('#connection-form [name=api_key]');check('이전 안내로 돌아와도 실제 입력칸 접근 가능',await clearTarget());
 await page.locator('#tutorial-coach-next').click();await page.locator('#tutorial-coach-next').click();await coachAt('#connection-form button.primary');
 check('연결 전송 허용은 기본 적용하고 고급 설정에 숨김',await page.locator('#connection-form [name=destination_approved]').isChecked()&&!await page.locator('#connection-form [name=destination_approved]').isVisible());
 check('저장 버튼 화살표 안내 이동은 연결·키·유료 검사 생성 없음',await clearTarget()&&(await state()).keys===0&&(await state()).checks===0&&requests.filter(request=>request.path.endsWith('/register')).length===0);
 check('연결됨·활성화와 실제 응답 확인을 안내에서 구분',await page.locator('#tutorial-coach-text').innerText().then(text=>text.includes('실제 API 응답')));
 const canary='qa-tutorial-secret-canary';
 await fault('SECRET_READBACK_FAILED');await page.locator('#connection-form [name=api_key]').fill(canary);await guide();await page.locator('#inspector-close').click();await resume();
 check('사용 안내를 다시 열어도 기존 연결 입력 유지',await page.locator('#connection-form [name=api_key]').inputValue()===canary);
 check('화살표 안내에 API 키 입력값을 복사하지 않음',!await page.locator('#tutorial-coach').innerText().then(text=>text.includes(canary)));
 await page.locator('#connection-form button.primary').click();await page.locator('#connection-error [data-guide-error=secret]').waitFor();
 check('키 저장 실패에 해결 안내 연결·키 입력 즉시 제거',await page.locator('#connection-form [name=api_key]').inputValue()===''&&(await state()).keys===0);
 await page.locator('#connection-error [data-guide-error]').click();await page.locator('#guide-article').waitFor();check('키 저장 실패 구체적 해결 안내',await page.locator('#guide-article').innerText().then(text=>text.includes('Windows')&&text.includes('키 저장 실패')));
 await page.locator('#inspector-close').click();await fault(null);await page.locator('#connection-form [name=api_key]').fill(canary);await page.locator('#connection-form button.primary').click();await page.locator('#connection-form').waitFor({state:'detached'});
 check('기존 안전 저장 경로로 모의 키 저장',(await state()).keys===1&&await page.locator('#notice[data-status=success]').count()===1);
 await coachAt('#list-new');check('연결 저장 후 실제 새 연구 만들기 버튼을 가리킴',await stage()==='first_path'&&await clearTarget()&&await page.locator('#research-form').count()===0);
 check('새 연구 과정에 자동 진행용 가짜 버튼 없음',!await page.locator('#tutorial-coach-next').isVisible()&&(await state()).researches===0);await shot('new-research-button');
 await page.locator('#list-new').click();await page.locator('#research-form').waitFor();await coachAt('#research-input-mode');await page.locator('#tutorial-coach-next').click();await coachAt('#research-form [name=title]');
 check('새 연구를 직접 누르면 입력 과정으로 이동',await stage()==='question'&&(await state()).preferences.tutorial_progress.completed_steps.includes('first_path')&&await page.locator('#without-explanations').count()===0);
 check('연구 첫 장에서 주제 입력을 화살표로 안내',await clearTarget());
 await page.locator('[name=title]').fill('튜토리얼 초안 유지');await page.locator('[name=question]').fill('자료의 관계를 검토해 주세요.');
 await page.locator('[name=attachment_files]').setInputFiles({name:'학습자료.csv',mimeType:'text/csv',buffer:Buffer.from('x,y\n1,2\n2,4\n3,6\n','utf8')});await page.getByText('내용 읽기 완료',{exact:false}).waitFor();
 await page.locator('#research-next').focus();await coachAt('#research-next');check('연구 첫 장 이동도 실제 다음 장 버튼을 사용',!await page.locator('#tutorial-coach-next').isVisible());
 await page.locator('#research-next').click();await page.waitForFunction(()=>document.querySelector('#tutorial-coach')?.dataset.route==='research_model');await page.locator('.model-option[data-active=true] input').first().check();await page.waitForFunction(()=>app.settings.models.length>0);
 check('연구 다음 장에서 모델·성능·검색·예산 안내로 변경',await page.locator('#tutorial-coach').getAttribute('data-route')==='research_model');
 await page.waitForFunction(()=>app.settings.preferences.tutorial_progress.step_id==='model'&&$('#research-form').elements.model_profile_id.value);const selectedProfile=await page.locator('[name=model_profile_id]').inputValue();await waitDraft(selectedProfile);await pace();await page.reload();await acceptIfAsked();await page.locator('#list-new').click();await page.locator('#research-form').waitFor();
 check('둘째 장 새로고침 뒤 모델·예산 안내와 장 복구',await page.locator('#research-form').getAttribute('data-page')==='1'&&await page.locator('#tutorial-coach').getAttribute('data-route')==='research_model');
 for(const selector of ['#performance','#research-form [name=search_policy]','#research-form [name=run_limit_usd]','#research-form [name=adaptive_budget]']){
  await page.locator(selector).focus();await coachAt(selector);check(selector+' 실제 입력 위치와 짧은 안내',await clearTarget()&&await page.locator('#tutorial-coach-text').innerText().then(text=>text.length<=70));
 }
 await page.locator('#research-next').click();await page.waitForFunction(()=>document.querySelector('#tutorial-coach')?.dataset.route==='research_start');await page.locator('#research-start').focus();await coachAt('#research-start');
 check('시작 버튼 안내가 실제 연구를 자동 실행하지 않음',(await state()).researches===0&&requests.filter(request=>request.path.endsWith('/start')).length===0);
 check('새 연구에서 연결 모델 선택·등록',await page.locator('[name=model_profile_id]').inputValue().then(value=>value.length>0));
 await page.waitForFunction(()=>app.settings.preferences.tutorial_progress.step_id==='optional_features');await pace();await page.reload();await acceptIfAsked();await page.locator('#list-new').click();await page.locator('#research-form').waitFor();
 check('마지막 장 새로고침 뒤 고급 설정·시작 안내와 장 복구',await page.locator('#research-form').getAttribute('data-page')==='2'&&await page.locator('#tutorial-coach').getAttribute('data-route')==='research_start');
 await guide();await page.locator('#guide-search').fill('CSV');await page.locator('#inspector-close').click();await resume();
 check('사용 안내 열기 후 마지막 장과 초안·첨부 보존',await page.locator('#research-form').getAttribute('data-page')==='2'&&await page.locator('#research-start').isVisible());
 await page.locator('#research-prev').click();await page.locator('#research-prev').click();await page.locator('[name=title]').focus();await coachAt('#research-form [name=title]');
 check('이전 장에서 질문·첨부 보존',await page.locator('[name=title]').inputValue()==='튜토리얼 초안 유지'&&await page.locator('#attachment-list').innerText().then(text=>text.includes('학습자료.csv')));
 await page.locator('[name=question]').focus();await coachAt('#research-form [name=question]');await page.keyboard.press('Escape');await page.locator('#tutorial-coach').waitFor({state:'detached'});
 check('화살표 안내 Escape 종료는 연구 창·질문·첨부 보존',await page.locator('#editor').isVisible()&&await page.locator('[name=question]').inputValue()==='자료의 관계를 검토해 주세요.'&&await page.locator('#attachment-list').innerText().then(text=>text.includes('학습자료.csv')));
 check('안내 종료 후 입력칸의 임시 접근성 연결 제거',!await page.locator('[name=question]').getAttribute('aria-describedby').then(value=>value?.includes('tutorial-coach-text')));
 check('안내 종료 후 실제 입력에 포커스 복귀',await page.locator('[name=question]').evaluate(element=>element===document.activeElement));
 await page.locator('#research-tutorial').click();await page.locator('#tutorial-coach').waitFor();await page.locator('#editor-close').click();await page.locator('[data-view=library]').click();await page.locator('#library-table').waitFor();
 check('다른 화면에서도 현재 단계의 이동 안내 유지',await page.locator('body>#tutorial-coach').isVisible()&&await page.locator('#content #tutorial-card').count()===0);
 await follow('check_connection');await page.locator('#connection-check').waitFor();check('연결 검사는 자동 실행하지 않음',(await state()).checks===0);
 for(const [code,issue] of [['PROVIDER_AUTH','auth'],['PROVIDER_PERMISSION','permission'],['PROVIDER_SPEND_LIMIT','balance'],['PROVIDER_RATE_LIMIT','rate'],['MODEL_NOT_FOUND','model']]){
  await fault(code);await page.locator('#connection-check button.primary').click();await page.locator('#connection-check-result [data-guide-error='+issue+']').waitFor();check(code+'의 모의 실패에 대응하는 해결 안내',await page.locator('#connection-check-result').innerText().then(text=>text.includes('해결 방법 보기')));
 }
 await page.locator('#editor-close').click();await follow('files');await page.locator('#research-form').waitFor();
 await page.route('**/api/control/attachments/*/upload',route=>route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'UPLOAD_PARSE_FAILED'})}));
 await page.locator('[name=attachment_files]').setInputFiles({name:'실패.csv',mimeType:'text/csv',buffer:Buffer.from('x,y\nbad,row\n','utf8')});await page.locator('#attachment-list [data-guide-error=file]').waitFor();check('파일 내용 실패도 해결 안내 연결',await page.locator('#attachment-list').innerText().then(text=>text.includes('파일')));await page.unroute('**/api/control/attachments/*/upload');
 await restart();const beforeFinish=await state();await nextStage();await page.locator('#connection-form button.primary').focus();await coachAt('#connection-form button.primary');await nextStage();await page.locator('#list-new').click();await page.locator('#research-form').waitFor();await page.locator('#research-next').click();await page.locator('#research-next').click();await page.locator('#research-start').focus();await coachAt('#research-start');
 check('마지막 안내에서도 실제 시작 버튼과 안내 완료를 분리',await page.locator('#tutorial-coach-next').innerText()==='안내 마치기'&&await page.locator('#research-start').isVisible());
 await page.locator('#tutorial-coach-next').click();await page.locator('#tutorial-coach').waitFor({state:'detached'});const completed=await state();
 check('핵심 네 과정 완료를 실제로 저장',completed.preferences.tutorial_progress.course_version===4&&completed.preferences.tutorial_progress.status==='COMPLETED'&&completed.preferences.tutorial_progress.completed_steps.length===4);
 check('안내 완료 후 연구 양식·입력 유지',await page.locator('#research-form').count()===1);
 check('전체 안내 읽기에서 유료 검사·연구·원장 변화 없음',completed.checks===beforeFinish.checks&&completed.researches===0&&completed.agents===0&&completed.requests===0);
 check('API 키가 학습 위치·초안 저장·알림에 없음',!requests.filter(request=>request.path.includes('preferences')||request.path.endsWith('/draft')).some(request=>request.body?.includes(canary))&&!JSON.stringify(completed.preferences).includes(canary)&&!await page.locator('#notice').innerText().then(text=>text.includes(canary)));
 await page.locator('#editor-close').click();await page.locator('[data-view=research]').click();await pace();await page.reload();await page.locator('#research-table').waitFor();check('완료 후 자동 강제 재표시 없음',await page.locator('#tutorial-card,#tutorial-coach').count()===0);
 await page.evaluate(()=>api('/api/control/preferences',{tutorial_progress:{course_version:2,chapter_id:'input',step_id:'files',mode:'GUIDED',status:'IN_PROGRESS',completed_steps:['overview'],provider_id:'openai',local_tool:'LM_STUDIO'}}));
 await pace();await page.reload();await acceptIfAsked();check('이전 30단계 과정의 중단 위치를 네 과정으로 이전',await stage()==='question'&&(await state()).preferences.tutorial_progress.course_version===4&&(await state()).preferences.tutorial_progress.completed_steps.length===0);
 await page.locator('#list-new').click();await page.locator('#research-form').waitFor();check('과정 변경 후에도 기존 초안과 첨부 보존',await page.locator('[name=title]').inputValue()==='튜토리얼 초안 유지'&&await page.locator('#attachment-list').innerText().then(text=>text.includes('학습자료.csv')));
 await page.locator('#tutorial-coach-close').click();await page.locator('#editor-close').click();
 await page.evaluate(()=>api('/api/control/preferences',{tutorial_progress:{course_version:3,chapter_id:'choices',step_id:'model',mode:'GUIDED',status:'IN_PROGRESS',completed_steps:['provider_key','save_connection','question'],provider_id:'openai',local_tool:'LM_STUDIO'}}));
 await pace();await page.reload();await acceptIfAsked();check('이전 다섯 과정의 기록을 완료 사실을 추가하지 않고 이전',await stage()==='question'&&(await state()).preferences.tutorial_progress.completed_steps.join(',')==='provider_key,save_connection');
 await page.locator('#tutorial-coach-close').click();
 await page.evaluate(()=>api('/api/control/preferences',{...app.settings.preferences,tutorial_progress:null,tutorial_completed:true}));await pace();await page.reload();await page.locator('#research-table').waitFor();check('이전 튜토리얼 종료 선호 보존',await page.locator('#tutorial-card,#tutorial-coach').count()===0);
 await page.locator('#list-new').click();await page.locator('#without-explanations').waitFor();await page.locator('#dismiss-explanations').check();await page.locator('#without-explanations').click();await page.locator('#research-form').waitFor();await page.locator('#research-tutorial').click();await page.locator('#tutorial-coach').waitFor();await page.waitForFunction(()=>app.settings.preferences.tutorial_progress?.status==='IN_PROGRESS');check('새 연구에서는 현재 입력 안내로 직접 진입',await stage()==='question'&&await page.locator('#tutorial-card').count()===0);
 check('기본 설명 선호 유지',!(await state()).preferences.new_research_explanations);
 const beforeLocal=await state();await restart();await page.locator('#tutorial-provider').selectOption('openai_compatible');await page.waitForFunction(()=>$('#provider-preparation')?.dataset.provider==='openai_compatible');
 for(const [tool,address] of [['LM_STUDIO','http://127.0.0.1:1234/v1'],['OLLAMA','http://127.0.0.1:11434/v1']]){
  await page.locator('#provider-preparation [data-local-tool]').selectOption(tool);await page.waitForFunction(value=>app.settings.preferences.tutorial_progress.local_tool===value,tool);
  check(tool+'의 서버 준비 단계·주소 표시',await page.locator('#provider-preparation').innerText().then(text=>text.includes(address))&&await page.locator('#provider-preparation>ol>li').count()>=3);
  check(tool+'의 발급 안내에 클라우드 키 발급 조작 없음',await page.locator('#provider-preparation').innerText().then(text=>!text.includes('API 키 발급 화면')));
  await nextStage();await page.locator('#connection-form').waitFor();await coachAt('#connection-form [name=base_url]');
  check(tool+'의 실제 로컬 주소 입력 위치 강조',await page.locator('#connection-form [name=base_url].tutorial-focus').count()===1&&!await page.locator('#connection-key-label').isVisible());
  await page.locator('#connection-form [name=base_url]').fill(address);await guide();await page.locator('#inspector-close').click();await resume();
  check(tool+'의 연결 안내 재열기에서 입력값 유지',await page.locator('#connection-form [name=base_url]').inputValue()===address);
  await page.locator('#editor-close').click();await restart();await page.locator('#tutorial-provider').selectOption('openai_compatible');await page.waitForFunction(()=>$('#provider-preparation')?.dataset.provider==='openai_compatible');
 }
 const afterLocal=await state();check('로컬 안내 이동도 키·연구·검사·비용 기록 변경 없음',afterLocal.keys===beforeLocal.keys&&afterLocal.researches===beforeLocal.researches&&afterLocal.agents===beforeLocal.agents&&afterLocal.checks===beforeLocal.checks&&afterLocal.requests===beforeLocal.requests);
 check('JavaScript 오류·자동 외부 요청 없음',errors.length===0);
 save(path.join(folder,'result.json'),{execution:'REAL_CHROME_OFFLINE_MOCK_ERRORS',paid_calls:0,checks,errors,shots,preferences:(await state()).preferences,folder:path.relative(root,folder)});
 process.stdout.write(JSON.stringify({passed:true,checks:checks.length,paid_calls:0,folder:path.relative(root,folder)})+'\n');
}
main().catch(async error=>{
 if(testPage)await testPage.screenshot({path:path.join(folder,'failure.png')}).catch(()=>{});
 const modelDiagnostic=testPage?await testPage.evaluate(async()=>{const draft=(await api('/api/control/research/draft')).draft,form=$('#research-form');return {input:form?.elements.model_profile_id.value,pool:form?.dataset.modelPool,draft_profile:draft?.model_profile_id,draft_pool:draft?.selected_model_pool,memory_profile:app.researchDraft?.model_profile_id,active_models:[...document.querySelectorAll('.model-option[data-active=true]')].map(element=>element.dataset.modelKey),stored_models:app.settings?.models.map(model=>model.profile_id)};}).catch(()=>null):null;
 const diagnostic=testPage?await testPage.evaluate(()=>({notice:$('#notice')?.textContent,ready_state:document.readyState,auto_tutorial:typeof autoTutorial,tutorial_controller:typeof ProbeTutorial,script_gate:typeof workbenchScriptsReady,assets:performance.getEntriesByType('resource').filter(value=>value.name.includes('/assets/')).map(value=>({path:new URL(value.name).pathname,status:value.responseStatus})),connection_error:$('#connection-error')?.textContent,invalid_fields:[...document.querySelectorAll('#connection-form input,#connection-form select')].filter(element=>!element.validity.valid).map(element=>element.name)})).catch(()=>null):null;
 const progressRequests=requests.filter(request=>request.path==='/api/control/preferences').slice(-6).map(request=>{const value=JSON.parse(request.body);return {tutorial_progress:value.tutorial_progress,tutorial_completed:value.tutorial_completed};});
 save(path.join(folder,'result.json'),{checks,errors,error:error.message,diagnostic,modelDiagnostic,progressRequests,preferenceErrors,request_paths:requests.map(request=>request.path)});process.stderr.write(error.stack+'\n');process.exitCode=1;
}).finally(async()=>{if(browser)await browser.close();if(child)child.kill();});
