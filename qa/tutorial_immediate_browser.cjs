// 실제 Chrome에서 목록 조회·학습 상태 저장 지연 중의 즉시 표시를 검사한다.
const {chromium}=require('./browser_runtime.cjs');
const {spawn}=require('child_process'),fs=require('fs'),path=require('path'),crypto=require('crypto');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/tutorial/immediate',crypto.randomUUID());
let child,browser;const checks=[],errors=[],held=[];
function save(value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 검사 실패');fs.writeFileSync(path.join(folder,'result.json'),Buffer.from(text,'utf8'));}
function check(name,passed){checks.push({name,passed:!!passed});}
async function main(){
 fs.mkdirSync(folder,{recursive:true});const env={...process.env};for(const key of Object.keys(env))if(key.endsWith('API_KEY'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/tutorial_browser_fixture.py',folder],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env});
 let stderr='';child.stderr.on('data',value=>stderr+=value);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',value=>{text+=value;const match=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(match)resolve(match[0]);});child.on('exit',()=>reject(Error(stderr.slice(-1000))));setTimeout(()=>reject(Error('서버 시간 초과')),15000).unref();});
 const origin=new URL(url).origin;browser=await chromium.launch({channel:'chrome',headless:false});const page=await browser.newPage({viewport:{width:1280,height:900}});page.setDefaultTimeout(10000);
 page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(!request.url().startsWith(origin+'/')&&!request.url().startsWith('blob:'))errors.push('외부 요청');});
 const state=()=>page.evaluate(()=>api('/qa/state'));
 const visibleSoon=async selector=>{try{await page.locator(selector).waitFor({timeout:1000});return true;}catch{return false;}};
 async function hold(pattern,action){let release,finished,once=true;const gate=new Promise(resolve=>release=resolve),handled=new Promise(resolve=>finished=resolve);held.push(release);await page.route(pattern,async route=>{const heldRequest=once;if(heldRequest){once=false;await gate;}await route.continue();if(heldRequest)finished();});const requested=page.waitForRequest(request=>request.url().includes(pattern.replaceAll('**','').replace('*','')));await action();await requested;return async()=>{release();await handled;await page.unroute(pattern);};}
 let releaseList,listFinished,firstList=true;const listGate=new Promise(resolve=>releaseList=resolve),listHandled=new Promise(resolve=>listFinished=resolve);held.push(releaseList);
 await page.route('**/api/control/research?*',async route=>{const heldRequest=firstList;if(heldRequest){firstList=false;await listGate;}await route.continue();if(heldRequest)listFinished();});
 const requestedList=page.waitForRequest(request=>new URL(request.url()).pathname==='/api/control/research');await page.goto(url);await requestedList;
 check('연구 목록 응답을 기다리는 중에도 최초 참여 팝업 표시',await visibleSoon('#tutorial-accept'));
 check('참여 팝업 전에 학습 위치·유료 기록을 생성하지 않음',(await state()).preferences.tutorial_progress===null);
 releaseList();await listHandled;await page.unroute('**/api/control/research?*');await page.locator('#tutorial-accept').waitFor();
 const releaseAccept=await hold('**/api/control/preferences',()=>page.locator('#tutorial-accept').click());
 check('보기 선택의 저장 응답 전에도 실제 발급 안내 표시',await visibleSoon('#provider-preparation'));
 await releaseAccept();await page.locator('#provider-preparation').waitFor();await page.waitForFunction(()=>app.settings.preferences.tutorial_progress?.status==='IN_PROGRESS');
 await page.locator('#tutorial-coach-close').click();await page.locator('#tutorial-coach').waitFor({state:'detached'});
 await page.locator('#settings-tab-help').click();
 const releaseReplay=await hold('**/api/control/preferences',()=>page.locator('#replay-tutorial').click());
 check('설정의 다시 보기 저장 응답 전에도 안내 표시',await visibleSoon('#provider-preparation'));
 await releaseReplay();await page.locator('#provider-preparation').waitFor();await page.waitForFunction(()=>app.settings.preferences.tutorial_progress?.status==='IN_PROGRESS');
 await page.locator('#tutorial-coach-close').click();await page.locator('#tutorial-coach').waitFor({state:'detached'});
 await page.locator('[data-view=research]').click();await page.locator('#list-new').click();await page.locator('#without-explanations').click();await page.locator('#research-form').waitFor();
 await page.locator('[name=title]').fill('안내 즉시 표시');await page.locator('[name=question]').fill('현재 입력 화면에서 안내해 주세요.');
 const releaseResearch=await hold('**/api/control/preferences',()=>page.locator('#research-tutorial').click());
 check('이전 API 과정 중단 뒤 새 연구에서는 현재 입력 안내 즉시 표시',await visibleSoon('#tutorial-coach[data-route=research_input]'));
 await releaseResearch();await page.locator('#tutorial-coach').waitFor();
 check('새 연구 안내에서 기존 질문·연구 창 보존',await page.locator('#research-form').count()===1&&await page.locator('[name=title]').inputValue()==='안내 즉시 표시');
 const after=await state();check('안내 이동에서 키·연구·Agent·검사·원장 생성 없음',after.keys===0&&after.researches===0&&after.agents===0&&after.checks===0&&after.requests===0);
 check('브라우저 오류·외부 요청 없음',errors.length===0);
 const passed=checks.every(check=>check.passed);save({execution:'REAL_CHROME_OFFLINE_HELD_LOCAL_RESPONSES',checks,errors,passed,paid_calls:0,folder:path.relative(root,folder)});console.log(JSON.stringify({passed,checks:checks.length,failed:checks.filter(check=>!check.passed).map(check=>check.name),paid_calls:0,folder:path.relative(root,folder)}));if(!passed)process.exitCode=1;
}
main().catch(error=>{save({checks,errors,error:error.message,passed:false});console.error(error.stack);process.exitCode=1;}).finally(async()=>{held.forEach(release=>release());if(browser)await browser.close();child?.kill();});
