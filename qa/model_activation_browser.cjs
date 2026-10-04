// OpenAI 키 등록·기존 미동의 연결·키 삭제·재등록을 실제 Chrome에서 확인한다.
const {chromium}=require('./browser_runtime.cjs');
const {spawn}=require('child_process'),fs=require('fs'),path=require('path'),crypto=require('crypto');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/model-activation/browser',crypto.randomUUID()),out=path.join(root,'output/playwright/model-activation');
fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(out,{recursive:true});
let child,browser,testPage,serverStderr='';const checks=[],errors=[],privateValues=['qa-model-activation-initial-canary'];
const replacement='qa-model-activation-replacement-'+crypto.randomUUID();privateValues.push(replacement);
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text)||privateValues.some(v=>text.includes(v)))throw Error('산출물 비밀·UTF-8 검사 실패');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,passed){checks.push({name,passed:!!passed});save(path.join(folder,'progress.json'),{checks,errors});if(!passed)throw Error(name);}
async function main(){
 const env={...process.env};for(const key of Object.keys(env))if(key.endsWith('KEY')||key.includes('TOKEN')||key.startsWith('HTRSA_CONN_'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/model_activation_browser_fixture.py',folder],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env});
 let stderr='';child.stderr.on('data',v=>{stderr+=v;serverStderr+=v;});
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',v=>{text+=v;const m=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m)resolve(m[0]);});child.on('exit',()=>reject(Error(stderr.slice(-1000))));setTimeout(()=>reject(Error('서버 시간 초과')),15000).unref();});
 privateValues.push(url.split('#bootstrap=')[1]);const origin=new URL(url).origin;
 browser=await chromium.launch({channel:'chrome',headless:true});const page=testPage=await browser.newPage({viewport:{width:1280,height:850}});page.setDefaultTimeout(12000);
 page.on('console',e=>{if(e.type()==='error')errors.push(e.text());});page.on('requestfailed',r=>errors.push(new URL(r.url()).pathname+' '+r.failure()?.errorText));page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith(origin+'/')&&!r.url().startsWith('blob:'))errors.push('외부 요청');});
 const source=fs.readFileSync(path.join(root,'src/htrsa/workbench_static/product_ux.js'),'utf8');
 const previous=source.replace('&&connectionAvailable(c));','&&connectionAvailable(c)&&c.destination_approved);');
 check('이전 활성화 조건 대조 준비',previous!==source);
 await page.route('**/assets/product_ux.js',route=>route.fulfill({status:200,contentType:'text/javascript; charset=utf-8',body:previous}));
 async function settings(){if(await page.locator('#editor[open]').count())await page.locator('#editor-close').click();await page.locator('[data-view=settings]').click();await page.locator('#settings-tab-connections').waitFor();await page.locator('#settings-tab-connections').click();}
 async function models(){if(await page.locator('#editor[open]').count())await page.locator('#editor-close').click();await page.locator('[data-view=research]').click();await page.locator('#list-new').click();await page.locator('#research-form').waitFor();await page.locator('[name=question]').fill('공개 자료의 관계를 비교해 주세요.');await page.locator('#research-next').click();await page.locator('#more-models').click();}
 const openai=()=>page.locator('.provider-group[data-provider=openai] .model-option');
 await page.goto(url);await page.locator('#list-new').waitFor();await settings();
 check('이전 조건에서 키 등록 연결됨 표시',await page.locator('#beginner-settings-connections').innerText().then(t=>t.includes('연결됨')));
 await models();check('기존 오류 재현: 키 등록됐지만 OpenAI 전부 비활성화',await openai().count()>0&&await openai().locator('.model-activation').allTextContents().then(values=>values.every(v=>v==='비활성화')));
 await page.screenshot({path:path.join(out,'before.png')});
 await page.unroute('**/assets/product_ux.js');await page.reload();await page.waitForFunction(()=>app.authenticated&&app.settings&&document.querySelector('#content h1'));await settings();
 await page.locator('#connection-new').click();check('새 연결의 전송 동의 기본 true',await page.locator('[name=destination_approved]').isChecked());await page.locator('#editor-close').click();
 await models();
 check('기존 키 등록만으로 OpenAI 활성화 표시',await openai().locator('.model-activation').allTextContents().then(values=>values.every(v=>v==='활성화')));
 check('OpenAI 선택 가능',await openai().locator('input').evaluateAll(inputs=>inputs.every(e=>!e.disabled)));
 check('활성화 초록색 유지',await openai().locator('.model-activation').first().evaluate(e=>getComputedStyle(e).color)==='rgb(23, 100, 61)');
 check('다른 제공사는 비활성화 유지',await page.locator('.provider-group[data-provider=anthropic] .model-activation').allTextContents().then(values=>values.length>0&&values.every(v=>v==='비활성화')));
 check('목록 열기만으로 전송 동의 변경하지 않음',await page.evaluate(()=>!app.settings.connections.find(c=>c.connection_id==='qa-openai').destination_approved));
 let release;let approvals=0;const approvalRequest=page.waitForRequest(r=>r.url().endsWith('/api/control/connections')&&r.method()==='POST');
 await page.route('**/api/control/connections',async route=>{if(route.request().method()==='POST'){approvals++;await new Promise(r=>release=r);}await route.continue();});
 await openai().locator('input').nth(0).check();await approvalRequest;await openai().locator('input').nth(1).check();
 check('동일 연결의 동의 저장 중복 방지',approvals===1);release();
 await page.waitForFunction(()=>app.settings.models.length===2&&JSON.parse($('#research-form').dataset.modelPool||'[]').length===2);
 await page.unroute('**/api/control/connections');
 check('선택한 연결에 기본 전송 동의 저장',await page.evaluate(()=>app.settings.connections.find(c=>c.connection_id==='qa-openai').destination_approved===true));
 check('두 모델 모두 실제 등록 연결 사용',await page.evaluate(()=>app.settings.models.length===2&&app.settings.models.every(m=>m.connection_id==='qa-openai')));
 check('활성화 표시가 실제 API 검사 통과로 바뀌지 않음',await page.evaluate(()=>app.settings.models.every(m=>m.capability_status==='unknown')));
 await page.screenshot({path:path.join(out,'after.png')});
 await settings();await page.locator('[data-key=qa-openai]').click();await page.locator('#key-delete').click();await page.locator('#editor[open]').waitFor({state:'hidden'});
 await page.waitForFunction(()=>$('#beginner-settings-connections')?.textContent.includes('키 필요')&&!$('#beginner-settings-connections')?.textContent.includes('연결됨'));
 check('키 삭제 후 설정 표시 갱신',await page.locator('#beginner-settings-connections').innerText().then(t=>t.includes('키 필요')&&!t.includes('연결됨')));
 await models();check('키 삭제 후 OpenAI 전부 비활성화',await openai().locator('.model-activation').allTextContents().then(values=>values.every(v=>v==='비활성화')));
 check('키 삭제 후 선택 차단',await openai().locator('input').evaluateAll(inputs=>inputs.every(e=>e.disabled)));
 await settings();await page.locator('[data-key=qa-openai]').click();await page.locator('#key-form [name=value]').fill(replacement);await page.locator('#key-form button.primary').click();await page.locator('#editor[open]').waitFor({state:'hidden'});
 await page.waitForFunction(()=>$('#beginner-settings-connections')?.textContent.includes('연결됨'));
 check('키 재등록 후 연결됨 표시',await page.locator('#beginner-settings-connections').innerText().then(t=>t.includes('연결됨')));
 await page.evaluate(()=>{app.settings.connections.find(c=>c.connection_id==='qa-openai').credential.configured=false;});
 await models();check('새 연구에서 이전 키 상태 캐시 갱신',await openai().locator('.model-activation').allTextContents().then(values=>values.every(v=>v==='활성화')));
 for(const input of await openai().locator('input').all())if(!await input.isChecked())await input.check();
 await page.waitForFunction(()=>JSON.parse($('#research-form').dataset.modelPool||'[]').length===$('#research-model-picker .provider-group[data-provider=openai]').querySelectorAll('input').length);
 const oldProfiles=await page.evaluate(()=>app.settings.models.map(m=>({profile_id:m.profile_id,connection_id:m.connection_id,revision:m.revision})));
 await settings();await page.locator('[data-edit-connection=qa-openai]').click();await page.locator('#connection-advanced').evaluate(e=>e.open=true);await page.locator('#connection-form [name=enabled]').uncheck();await page.locator('#connection-form button.primary').click();await page.locator('#editor[open]').waitFor({state:'hidden'});
 await page.waitForFunction(()=>$('#beginner-settings-connections')?.textContent.includes('사용 안 함'));
 check('사용 중지 연결은 연결됨으로 표시하지 않음',await page.locator('#beginner-settings-connections').innerText().then(t=>t.includes('사용 안 함')&&!t.includes('연결됨')));
 await models();check('사용 중지 연결 모델 비활성화',await openai().locator('input').evaluateAll(inputs=>inputs.every(e=>e.disabled)));
 await settings();await page.locator('#connection-new').click();await page.locator('#connection-form [name=api_key]').fill(replacement);await page.locator('#connection-form [name=display_name]').fill('OpenAI 새 연결');
 const newConnection=await page.locator('#connection-form [name=connection_id]').inputValue();await page.locator('#connection-form button.primary').click();await page.locator('#editor[open]').waitFor({state:'hidden'});
 await page.waitForFunction(()=>$('#beginner-settings-connections')?.textContent.includes('OpenAI 새 연결'));
 await models();check('이전 연결의 모델이 있어도 새 연결 모델 활성화',await openai().locator('.model-activation').allTextContents().then(values=>values.length===oldProfiles.length&&values.every(v=>v==='활성화')));
 check('사용 중지 연결의 이전 선택을 새 연구에서 제외',await page.evaluate(()=>JSON.parse($('#research-form').dataset.modelPool||'[]').length===0));
 await openai().locator('input').first().check();await page.waitForFunction(id=>app.settings.models.some(m=>m.connection_id===id)&&JSON.parse($('#research-form').dataset.modelPool||'[]').length===1,newConnection);
 check('선택 모델을 새 키가 등록된 연결에 저장',await page.evaluate(id=>app.settings.models.find(m=>m.profile_id===JSON.parse($('#research-form').dataset.modelPool)[0]).connection_id===id,newConnection));
 check('이전 연결의 모델 구성·이력 보존',await page.evaluate(old=>old.every(m=>{const current=app.settings.models.find(x=>x.profile_id===m.profile_id);return current?.connection_id===m.connection_id&&current?.revision===m.revision;}),oldProfiles));
 const ledger=await page.evaluate(()=>api('/api/control/usage'));check('유료 호출·비용 예약 없음',ledger.requests.length===0);
 const visible=await page.locator('body').innerText();check('화면에 키·인증 값 노출 없음',!privateValues.some(v=>visible.includes(v)));check('브라우저 오류·외부 요청 없음',errors.length===0);
 const result={execution:'REAL_CHROME_OFFLINE_MEMORY_CREDENTIALS',passed:true,checks,errors,live_api_calls:0,live_llm:'NOT_VALIDATED',folder:path.relative(root,folder)};
 save(path.join(folder,'result.json'),result);save(path.join(root,'qa/results/model_activation_browser_results.json'),result);process.stdout.write(JSON.stringify({checks:checks.length,passed:true,folder:path.relative(root,folder),live_api_calls:0})+'\n');
}
main().catch(async e=>{const diagnostic=testPage?await testPage.evaluate(()=>({settingsFunction:renderSettings.toString().slice(0,160),newResearchFunction:newResearch.toString().slice(0,100),controller:typeof HtrsaTutorial,designController:typeof HtrsaResearchDesign,ready:document.readyState,view:app.view,epoch:app.epoch,developer:app.developerSettings,hash:location.hash,pane:app.settingsPane,notice:$('#notice')?.textContent,content:$('#content')?.innerText.slice(0,1000),scripts:performance.getEntriesByType('resource').filter(r=>r.name.includes('/assets/')).map(r=>new URL(r.name).pathname)})).catch(()=>null):null;let message=e.message;for(const value of privateValues)message=message.split(value).join('[비밀 제거됨]');save(path.join(folder,'result.json'),{checks,errors,error:message,diagnostic,server_exit:child?.exitCode,server_stderr:serverStderr.slice(-3000)});process.stderr.write(message+'\n');process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(child)child.kill();});
