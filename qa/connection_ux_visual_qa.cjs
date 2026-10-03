// 실제 Chrome과 모의 키 저장소로 연결 등록·키 삭제·모델 선택을 확인한다.
const fs=require('fs'),path=require('path'),http=require('http'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),base=path.join(root,'build/connection-ux-visual',crypto.randomUUID()),out=path.join(root,'output/playwright/connection-ux');
fs.mkdirSync(base,{recursive:true});fs.mkdirSync(out,{recursive:true});
const relayToken=crypto.randomBytes(32).toString('hex'),privateValues=[relayToken],checks=[],shots=[],errors=[],logs=[],egress=[];
let browser,relay,child,resolveHandoff;
function check(name,passed){if(!passed)throw Error('검사 실패: '+name);checks.push({name,passed:true});save(path.join(base,'progress.json'),{checks});}
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 불가');if(privateValues.some(v=>text.includes(v)))throw Error('비밀 산출물 차단');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
async function main(){
 relay=http.createServer((req,res)=>{if(req.method!=='POST'||req.url!=='/handoff'||req.headers.authorization!=='Bearer '+relayToken){res.writeHead(403);res.end();return;}let body='';req.on('data',b=>body+=b);req.on('end',()=>{resolveHandoff(JSON.parse(body).url);res.writeHead(200);res.end();});});
 await new Promise(r=>relay.listen(0,'127.0.0.1',r));
 const handoff=new Promise((resolve,reject)=>{resolveHandoff=resolve;setTimeout(()=>reject(Error('숨김 앱 시작 시간 초과')),15000).unref();});
 const py=`import os,webbrowser,httpx\nfrom pathlib import Path\nfrom htrsa.control_plane import Credentials,ControlError\nfrom htrsa.desktop import main\noriginal=Credentials.__init__\nclass MemoryStore:\n available=True\n def __init__(self): self.values={}\n def read(self,name): return self.values.get(name),None\n def write(self,name,value):\n  if value and value.startswith('qa-failure-'): raise ControlError('SECRET_READBACK_FAILED')\n  if value is None: self.values.pop(name,None)\n  else: self.values[name]=value\ndef isolated(self,repository,workspace,file=None):\n base=Path(os.environ['QA_FOLDER'])\n original(self,base/'repository',workspace,base/'private/unused.env')\n self.os_store=MemoryStore()\nCredentials.__init__=isolated\nclass Browser(webbrowser.BaseBrowser):\n def open(self,url,new=0,autoraise=True):\n  with httpx.Client(trust_env=False) as c: return c.post(os.environ['QA_RELAY'],json={'url':url},headers={'Authorization':'Bearer '+os.environ['QA_TOKEN']}).status_code==200\nwebbrowser.register('product-qa',None,Browser(),preferred=True)\nmain(['--data-dir',os.environ['QA_FOLDER']],dialog=lambda *_a,**_k:False)\n`;
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
 async function pane(key){if(key==='model'){await models();return;}if(await page.locator('#editor[open]').count())await page.keyboard.press('Escape');await page.locator('#settings-tab-'+key).click();await page.locator('#settings-pane-'+key).waitFor();}
 async function capture(name){const visible=await page.evaluate(()=>document.body.innerText+'\n'+Array.from(document.querySelectorAll('input:not([type="password"]),textarea')).map(e=>e.value).join('\n'));check(name+' 저장 전 화면 비밀 없음',!privateValues.some(v=>visible.includes(v)));const target=path.join(out,name+'.png');await page.screenshot({path:target,fullPage:true,mask:[page.locator('input[type="password"]')]});shots.push(target);check(name+' 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));}

 await settings();await models();
 check('상단 모델 5개',await page.locator('#featured-models .model-option').count()===5);
 check('추천·속도·성능 표시 없음',!(await page.locator('#featured-models').innerText()).match(/추천|권장|고성능|빠른/));
 check('더보기 기본 접힘',!await page.locator('#more-models').evaluate(e=>e.open));
 check('추가 모델 8개',await page.locator('#more-models .model-option').count()===8);
 await page.locator('#more-models>summary').click();
 check('제공사별 추가 모델 묶음',await page.locator('#more-models legend').count()===5);
 await capture('models-more-1440');
 await page.locator('#more-models>summary').click();
 await pane('advanced');check('고급 설정에 API 관리 없음',await page.locator('#settings-pane-advanced #settings-1').count()===0);
 await pane('connections');check('API 연결 전용 탭',await page.locator('#settings-pane-connections #settings-1').isVisible());
 check('연결 목록에서 주소 숨김',!(await page.locator('#settings-1').innerText()).includes('https://'));
 await page.locator('#connection-new').click();await page.locator('#connection-form').waitFor();
 check('주소·키 참조 기본 숨김',!await page.locator('#connection-form [name="base_url"]').isVisible()&&!await page.locator('#connection-form [name="credential_env_name"]').isVisible());
 check('키 password 입력',await page.locator('#connection-form [name="api_key"]').getAttribute('type')==='password');
 check('연결 ID 자동 생성·읽기 전용',await page.locator('#connection-form [name="connection_id"]').evaluate(e=>e.readOnly&&e.value.startsWith('api-')));
 const providers=['openai','anthropic','google_gemini','xai','deepseek','mistral'];
 for(const provider of providers){await page.locator('#connection-form [name="adapter_id"]').selectOption(provider);check(provider+' 공식 주소 자동 설정·읽기 전용',await page.locator('#connection-form [name="base_url"]').evaluate(e=>e.readOnly&&e.value.startsWith('https://')));}
 await page.locator('#connection-form [name="adapter_id"]').selectOption('openai_compatible');
 check('로컬 모델 서버 기본값',await page.locator('#connection-form [name="base_url"]').inputValue()==='http://127.0.0.1:1234/v1'&&!await page.locator('#connection-key-label').isVisible());
 await page.keyboard.press('Escape');
 const canary='qa-private-canary-'+crypto.randomUUID(),failure='qa-failure-'+crypto.randomUUID();privateValues.push(canary,failure);
 await page.locator('#connection-new').click();await page.locator('#connection-form [name="api_key"]').fill(failure);
 await page.locator('#connection-form [name="destination_approved"]').check();
 const failedId=await page.locator('#connection-form [name="connection_id"]').inputValue();
 await page.locator('#connection-form button.primary').click();
 await page.waitForFunction(()=>document.querySelector('#connection-error')?.textContent.includes('저장한 키'));
 check('키 저장 실패는 양식 안에서 표시·입력 제거',await page.locator('#connection-form [name="api_key"]').inputValue()===''&&await page.locator('#editor').isVisible());
 let config=await page.evaluate(async()=>await(await fetch('/api/control/settings')).json());
 check('키 실패 뒤 연결 저장 안 됨',!config.connections.some(c=>c.connection_id===failedId));
 await capture('connection-error-1440');await page.keyboard.press('Escape');
 const ids=[];for(const provider of ['openai','anthropic']){
  await page.locator('#connection-new').click();await page.locator('#connection-form [name="adapter_id"]').selectOption(provider);
  const id=await page.locator('#connection-form [name="connection_id"]').inputValue();ids.push(id);
  await page.locator('#connection-form [name="api_key"]').fill(canary+'-'+provider);privateValues.push(canary+'-'+provider);
  await page.locator('#connection-form [name="destination_approved"]').check();
  await capture('inline-key-'+provider+'-1440');
  await page.locator('#connection-form button.primary').click();await page.locator('#editor[open]').waitFor({state:'hidden'});
  await page.waitForFunction(n=>document.querySelectorAll('#saved-key-list tbody tr').length===n,ids.length);
  check(provider+' 연결·키 한 번에 저장',await page.locator('#saved-key-list tbody tr').count()===ids.length);
  check(provider+' 저장 완료 초록 알림',await page.locator('#notice').evaluate(e=>e.dataset.status==='success'&&getComputedStyle(e).color==='rgb(40, 84, 57)'&&e.getAttribute('role')==='status'));
 }
 config=await page.evaluate(async()=>await(await fetch('/api/control/settings')).json());
 check('연결별 키 참조 분리',new Set(config.connections.map(c=>c.credential_env_name)).size===2);
 check('설정 응답에 키 원문 없음',!privateValues.some(v=>JSON.stringify(config).includes(v)));
 await pane('model');await page.locator('#featured-models [name="catalog-model"]').nth(1).check();await page.locator('#prepare-selected-model>summary').click();
 check('제공사별 연결 필터',await page.locator('#research-model-connection option').count()===2&&await page.locator('#research-model-connection').inputValue()===ids[1]);
 await page.locator('#research-model-consent').check();await page.locator('#research-model-save').click();
 await page.waitForFunction(()=>document.querySelector('#notice')?.textContent.includes('모델을 등록했습니다.'));
 config=await page.evaluate(async()=>await(await fetch('/api/control/settings')).json());
 check('Claude 모델이 선택한 연결 사용·미검증 유지',config.models.length===1&&config.models[0].connection_id===ids[1]&&config.models[0].capability_status==='unknown');
 await page.keyboard.press('Escape');await page.locator('[data-view="research"]').click();await page.locator('#new-research').click();await page.locator('#research-form').waitFor();
 check('성능 이름 low·medium·high·max와 기본 medium',JSON.stringify(await page.locator('#research-form .performance-labels span').allTextContents())===JSON.stringify(['low','medium','high','max'])&&await page.locator('#research-form [name="performance"]').getAttribute('aria-valuetext')==='medium');
 check('새 연구에 다른 제공사 모델도 표시',(await page.locator('#research-model-picker').innerText()).includes('Claude Sonnet')&&await page.locator('#research-form [name="model_profile_id"]').inputValue()===config.models[0].profile_id);
 await page.keyboard.press('Escape');await settings();await pane('connections');
 await page.locator(`#saved-key-list [data-key="${ids[0]}"]`).click();await page.locator('#key-delete').click();await page.locator('#editor[open]').waitFor({state:'hidden'});
 await page.waitForFunction(()=>document.querySelectorAll('#saved-key-list tbody tr').length===1);
 check('키 삭제 뒤 해당 로컬 키 항목 제거',await page.locator(`#saved-key-list [data-key="${ids[0]}"]`).count()===0&&await page.locator(`#saved-key-list [data-key="${ids[1]}"]`).count()===1);
 config=await page.evaluate(async()=>await(await fetch('/api/control/settings')).json());
 check('삭제한 키 미설정 · 다른 연결 키 보존',!config.connections.find(c=>c.connection_id===ids[0]).credential.saved&&config.connections.find(c=>c.connection_id===ids[1]).credential.saved);
 check('키 삭제 후 연결 자체 보존',config.connections.length===2);
 await page.locator(`#saved-key-list [data-key="${ids[1]}"]`).click();await page.locator('#key-delete').click();await page.locator('#editor[open]').waitFor({state:'hidden'});
 await page.waitForFunction(()=>document.querySelectorAll('#saved-key-list tbody tr').length===0);
 check('마지막 키 삭제 뒤 목록 비움',await page.locator('#saved-key-list tbody tr').count()===0);
 check('키 삭제 완료 초록 알림',await page.locator('#notice').evaluate(e=>e.dataset.status==='success'&&getComputedStyle(e).backgroundColor==='rgb(237, 243, 237)'));
 await capture('keys-deleted-1440');
 const denied=await page.evaluate(async()=> (await fetch('/api/control/connections/register',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status);
 check('동시 등록에도 CSRF 필수',denied===403);
 for(const width of [1440,390]){await page.setViewportSize({width,height:900});await page.locator('#connection-new').click();await capture('connection-form-'+width);await page.keyboard.press('Escape');await pane('model');await page.locator('#more-models>summary').click();await capture('model-selection-'+width);await page.locator('#more-models>summary').click();await pane('connections');}
 await page.setViewportSize({width:1280,height:900});await page.evaluate(()=>document.documentElement.style.zoom='2');await page.locator('#connection-new').click();await capture('connection-css-200');await page.keyboard.press('Escape');await page.evaluate(()=>document.documentElement.style.zoom='');
 await page.setViewportSize({width:1440,height:900});
 await page.route('**/api/control/settings',async route=>{const response=await route.fetch(),value=await response.json(),row=value.catalog.models.find(m=>m.featured_order===1);value.catalog.models.unshift({...row,profile_id:'qa-duplicate-profile'});await route.fulfill({response,json:value});});
 await settings();await models();
 check('동일 모델의 저장 프로필이 중복돼도 상단 5종 유지',await page.locator('#featured-models .model-option').count()===5&&new Set(await page.locator('#featured-models .model-option span').allTextContents()).size===5);
 await capture('models-duplicate-profile-1440');
 await page.evaluate(()=>message('키 저장 실패'));
 check('성공 뒤 오류 알림은 빨강으로 복귀',await page.locator('#notice').evaluate(e=>e.dataset.status==='error'&&getComputedStyle(e).color==='rgb(142, 50, 50)'&&e.getAttribute('role')==='alert'));
 await page.evaluate(()=>message(''));
 check('빈 알림 숨김',!await page.locator('#notice').isVisible());
 const ledger=await page.evaluate(async()=>await(await fetch('/api/control/usage')).json());check('등록·선택·삭제 유료 원장 0',ledger.requests.length===0);
 const storage=await page.evaluate(async()=>({local:localStorage.length,session:sessionStorage.length,indexedDB:(await indexedDB.databases()).length,workers:(await navigator.serviceWorker.getRegistrations()).length}));
 check('브라우저 영구 저장소 0',Object.values(storage).every(v=>v===0));
 check('콘솔·stdout 비밀 없음',!privateValues.some(v=>childOutput.includes(v)||logs.some(x=>x.includes(v))));
 check('JS 오류·외부 요청 0',errors.length===0&&egress.length===0);
 const result={passed:true,checks,screenshots:shots,actual_pythonw:true,credential_backend:'MOCK_OS_STORE',windows_real_key:'NOT_VALIDATED',storage,errors,unexpected_network:egress,paid_live_calls:0,secret_exposures:0};
 save(path.join(root,'qa/results/connection_ux_visual_results.json'),result);console.log(JSON.stringify({passed:true,checks:checks.length,screenshots:shots.length,paid_live_calls:0,secret_exposures:0,credential_backend:'MOCK_OS_STORE'}));
}
main().catch(e=>{let text=e.message;for(const value of privateValues)text=text.split(value).join('[비밀 제거됨]');console.error((checks.at(-1)?.name||'시작')+' 이후: '+text);process.exitCode=1;}).finally(async()=>{if(child&&child.exitCode===null&&child.signalCode===null)await new Promise(r=>{child.once('exit',r);child.kill();});if(browser)await browser.close();if(relay)await new Promise(r=>relay.close(r));});
