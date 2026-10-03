// 실제 pythonw 실행과 Chrome에서 온보딩·동의·예산·좁은 화면을 확인한다.
const fs=require('fs'),path=require('path'),http=require('http'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),base=path.join(root,'build/productization-visual',crypto.randomUUID()),out=path.join(root,'output/playwright/productization');
fs.mkdirSync(base,{recursive:true});fs.mkdirSync(out,{recursive:true});
const relayToken=crypto.randomBytes(32).toString('hex'),privateValues=[relayToken],checks=[],shots=[],errors=[],logs=[],egress=[];
let browser,relay,child,resolveHandoff;
function check(name,passed){if(!passed)throw Error('검사 실패: '+name);checks.push({name,passed:true});save(path.join(base,'progress.json'),{checks});}
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 불가');if(privateValues.some(v=>text.includes(v)))throw Error('비밀 산출물 차단');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
async function main(){
 relay=http.createServer((req,res)=>{if(req.method!=='POST'||req.url!=='/handoff'||req.headers.authorization!=='Bearer '+relayToken){res.writeHead(403);res.end();return;}let body='';req.on('data',b=>body+=b);req.on('end',()=>{resolveHandoff(JSON.parse(body).url);res.writeHead(200);res.end();});});
 await new Promise(r=>relay.listen(0,'127.0.0.1',r));
 const handoff=new Promise((resolve,reject)=>{resolveHandoff=resolve;setTimeout(()=>reject(Error('숨김 앱 시작 시간 초과')),15000).unref();});
 const py=`import os,webbrowser,httpx\nfrom pathlib import Path\nfrom htrsa.control_plane import Credentials\nfrom htrsa.desktop import main\noriginal=Credentials.__init__\ndef isolated(self,repository,workspace,file=None): original(self,repository,workspace,Path(os.environ['QA_FOLDER'])/'blocked-key.env')\nCredentials.__init__=isolated\nclass Browser(webbrowser.BaseBrowser):\n def open(self,url,new=0,autoraise=True):\n  with httpx.Client(trust_env=False) as c: return c.post(os.environ['QA_RELAY'],json={'url':url},headers={'Authorization':'Bearer '+os.environ['QA_TOKEN']}).status_code==200\nwebbrowser.register('product-qa',None,Browser(),preferred=True)\nmain(['--data-dir',os.environ['QA_FOLDER']],dialog=lambda *_a,**_k:False)\n`;
 const env={...process.env,QA_FOLDER:base,QA_RELAY:'http://127.0.0.1:'+relay.address().port+'/handoff',QA_TOKEN:relayToken};for(const key of Object.keys(env))if(key.endsWith('API_KEY'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/pythonw.exe'),['-c',py],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env});let childOutput='';child.stdout.on('data',b=>childOutput+=b);child.stderr.on('data',b=>childOutput+=b);
 const url=await handoff,origin=new URL(url).origin;privateValues.push(url.split('#bootstrap=')[1]);
 browser=await chromium.launch({channel:'chrome',headless:true});const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();page.setDefaultTimeout(15000);
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>logs.push(m.text()));page.on('request',r=>{if(!r.url().startsWith(origin+'/'))egress.push(r.url());});
 await page.goto(url);await page.locator('#research-table').waitFor();
 check('실제 pythonw에서 기존 작업대 자동 인증',!page.url().includes('bootstrap')&&child.exitCode===null);
 const session=await page.evaluate(async()=>await(await fetch('/api/session')).json());privateValues.push(session.csrf);for(const c of await context.cookies())privateValues.push(c.value);
 async function settings(){if(await page.locator('#editor[open]').count())await page.keyboard.press('Escape');await page.locator('[data-view="settings"]').click();await page.locator('#settings-tab-connections').waitFor();await page.locator('#settings-tab-connections').click();}
 async function models(){await page.locator('#new-research').click();await page.locator('#research-form').waitFor();}
 async function qualify(consent){return page.evaluate(async consent=>{const s=await(await fetch('/api/session')).json(),config=await(await fetch('/api/control/settings')).json();const r=await fetch('/api/control/models/'+config.models[0].profile_id+'/qualify',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf},body:JSON.stringify({consent,idempotency_key:crypto.randomUUID()})});return {status:r.status,body:await r.json()};},consent);}
 async function capture(name){const target=path.join(out,name+'.png');await page.screenshot({path:target,fullPage:true});shots.push(target);check(name+' 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));}
 check('초기 5단계 실제 연결 검증 전',await page.locator('.onboarding-steps li').count()===5&&(await page.locator('#first-research').innerText()).includes('검증 전'));await settings();await models();
 const labels=await page.locator('#featured-models .model-option span').allTextContents();check('상단 모델 5개와 제공사 구분 · 추천 표기 없음',labels.length===5&&labels.every(x=>!/(권장|추천|고성능|빠른)/.test(x))&&await page.locator('#featured-models .model-option small').count()===5);
 check('개발자 설정 기본 접힘',!await page.locator('#research-advanced').evaluate(e=>e.open));await page.keyboard.press('Escape');
 await models();await page.locator('#prepare-selected-model>summary').click();await page.locator('#research-model-consent').check();await page.locator('#research-model-save').click();await page.waitForFunction(()=>document.querySelector('#notice')?.textContent.includes('모델을 등록했습니다.'));await page.keyboard.press('Escape');await settings();
 check('카탈로그 모델 저장 완료',true);
 await page.locator('#settings-tab-checks').click();const refused=await qualify(false);check('기존 연결 종합 검사 동의 거부',refused.status===403||JSON.stringify(refused.body).includes('CONSENT_REQUIRED'));
 check('유료 검사 동의 없으면 차단',true);
 const missing=await qualify(true);check('기존 연결 종합 검사 키 누락',JSON.stringify(missing.body).includes('CREDENTIAL_UNCONFIGURED'));
 check('키 없으면 실제 요청 이전 차단',true);
 await page.locator('#settings-tab-budget').click();await page.locator('#defaults-form [name="monthly_limit_usd"]').fill('5');await page.locator('#defaults-form [name="request_limit_usd"]').fill('0.10');const budgetSaved=page.waitForResponse(r=>r.url().endsWith('/api/control/defaults')&&r.request().method()==='POST');await page.locator('#defaults-form button').click();check('공통 예산 설정 저장', (await budgetSaved).ok());await page.waitForFunction(()=>Number(app.settings?.defaults.monthly_limit_usd)===5);await page.locator('[data-view="research"]').click();await page.locator('#first-research').waitFor();
 await page.waitForFunction(()=>document.querySelector('.onboarding-steps li:nth-child(4)')?.textContent.includes('확인됨'));
 check('예산 저장이 온보딩 확인 상태에 반영',true);
 for(const size of [{width:1440,height:900},{width:390,height:844}]){await page.setViewportSize(size);await capture('onboarding-'+size.width);await page.locator('[data-view="research"]').click();await page.locator('#new-research').click();await page.locator('#research-form').waitFor();check('일반 연구 선택에 직접 모델 ID 없음 '+size.width,!(await page.locator('#research-form').innerText()).includes('gpt-6.1-sol'));await page.keyboard.press('Escape');await settings();}
 await page.setViewportSize({width:1280,height:900});await page.evaluate(()=>document.documentElement.style.zoom='2');await capture('onboarding-css-200');await page.evaluate(()=>document.documentElement.style.zoom='');
 const hostile=`javascript:document.body.dataset.injection='yes'`;
 await page.route('**/api/control/settings',async route=>{const response=await route.fetch(),value=await response.json();value.catalog.models[0].source=hostile;await route.fulfill({response,json:value});});
 await settings();await models();
 check('공식 출처 링크 이외 URL 실행 차단',await page.locator('#catalog-detail a[href^="javascript:"]').count()===0&&await page.evaluate(()=>!document.body.dataset.injection));
 const ledger=await page.evaluate(async()=>await(await fetch('/api/control/usage')).json());check('화면·동의 거부·키 누락·예산 저장은 과금 원장 0',ledger.requests.length===0);
 const storage=await page.evaluate(async()=>({local:localStorage.length,session:sessionStorage.length,indexedDB:(await indexedDB.databases()).length,workers:(await navigator.serviceWorker.getRegistrations()).length}));
 check('브라우저 영구 저장소 0',Object.values(storage).every(v=>v===0));
 check('정상 stdout·콘솔 비밀 없음',!privateValues.some(v=>childOutput.includes(v)||logs.some(x=>x.includes(v))));
 check('JS 오류·외부 요청 0',errors.length===0&&egress.length===0);
 const result={passed:true,checks,screenshots:shots,actual_pythonw:true,wsf_association:'NOT_VALIDATED',os_default_browser:'NOT_VALIDATED',browser:'실제 Chrome + QA 등록 컨트롤러',storage,errors,unexpected_network:egress,secret_exposures:0,paid_live_calls:0,reasoning_efficacy:'NOT_VALIDATED'};
 save(path.join(root,'qa/results/productization_visual_results.json'),result);console.log(JSON.stringify({passed:true,checks:checks.length,screenshots:shots.length,actual_pythonw:true,paid_live_calls:0,secret_exposures:0}));
}
main().catch(e=>{let text=e.message;for(const value of privateValues)text=text.split(value).join('[비밀 제거됨]');console.error((checks.at(-1)?.name||'시작')+' 이후: '+text);process.exitCode=1;}).finally(async()=>{if(child&&child.exitCode===null&&child.signalCode===null)await new Promise(r=>{child.once('exit',r);child.kill();});if(browser)await browser.close();if(relay)await new Promise(r=>relay.close(r));});
