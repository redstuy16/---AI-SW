// 기존 번들 Playwright와 실제 Chrome으로 설정 화면을 검사한다.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),fixture=JSON.parse(fs.readFileSync(path.join(root,'build/gui_visual_fixture.json'),'utf8'));
const output=path.join(root,'output/playwright');fs.mkdirSync(output,{recursive:true});
const connectionId='browser-local-'+crypto.randomUUID();
const server=spawn(path.join(root,'.venv/Scripts/python.exe'),['-m','htrsa.workbench',fixture.database,fixture.workspace,'--mode','DEMO','--no-browser','--port','8878'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
let browser;
async function main(){
 const handoff=await new Promise((resolve,reject)=>{let text='';server.stdout.on('data',b=>{text+=b.toString();const m=text.match(/(http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43})/);if(m)resolve(m[1]);});server.on('exit',c=>reject(Error('서버 종료 '+c)));setTimeout(()=>reject(Error('서버 시작 제한 시간')),15000).unref();});
 browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage({viewport:{width:1440,height:900}});
 const errors=[],egress=[],checks=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith('http://127.0.0.1:8878'))egress.push(r.url());});
 await page.goto(handoff);await page.locator('#research-table').waitFor();
 await page.locator('[data-view="settings"]').click();await page.locator('#settings-tab-connections').waitFor();await page.locator('#settings-tab-connections').click();await page.locator('#connection-new').waitFor();await page.locator('#connection-new').click();await page.locator('#connection-form').waitFor();
 const select=page.locator('#connection-form [name="adapter_id"]'),ids=await select.locator('option').evaluateAll(ns=>ns.map(n=>n.value));
 if(ids.length!==7)throw Error('제공사 목록 누락');
 for(const id of ids){await select.selectOption(id);const native=id!=='openai_compatible',readonly=await page.locator('#connection-form [name="base_url"]').evaluate(e=>e.readOnly);if(readonly!==native)throw Error('공식 주소 잠금 실패');checks.push({provider:id,native_url_locked:readonly});}
 await select.selectOption('openai_compatible');await page.locator('#connection-form [name="connection_id"]').evaluate((el,value)=>el.value=value,connectionId);await page.locator('#connection-form [name="display_name"]').fill('브라우저 검사용 로컬');await page.locator('#connection-form [name="base_url"]').fill('http://127.0.0.1:1234/v1');await page.locator('#connection-form [name="endpoint_class"]').selectOption('loopback');await page.locator('#connection-form').getByRole('button',{name:'연결 저장',exact:true}).click();await page.locator('#editor[open]').waitFor({state:'hidden'});
 await page.locator('#new-research').click();await page.locator('#research-form').waitFor();await page.locator('#more-models>summary').click();await page.locator('#research-model-add').click();await page.locator('#model-form').waitFor();await page.locator('#model-form [name="connection_id"]').selectOption(connectionId);
 for(const name of ['temperature','top_p','seed','stop','stream','store_preference'])if(!await page.locator(`#model-form [name="${name}"]`).isDisabled())throw Error('미확인 기능 활성화 '+name);
 await page.locator('#model-form details').last().locator('summary').click();await page.locator('[data-capability="reasoning"]').selectOption('SUPPORTED');await page.locator('[data-level="HIGH"]').check();await page.locator('[name="reasoning_policy"]').selectOption('HIGH');await page.locator('[data-capability="temperature"]').selectOption('SUPPORTED');if(await page.locator('[name="temperature"]').isDisabled())throw Error('선언한 기능 사용 불가');
 await page.locator('[name="profile_id"]').fill('browser-model');await page.locator('[name="model_id"]').fill('manual-model-id');
 await page.screenshot({path:path.join(output,'multi-provider-model-desktop.png')});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'multi-provider-model-mobile.png')});const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2);if(overflow)throw Error('모바일 가로 넘침');
 await page.keyboard.press('Escape');await page.setViewportSize({width:1440,height:900});await page.screenshot({path:path.join(output,'multi-provider-settings.png'),fullPage:true});
 const storage=await page.evaluate(()=>({local:localStorage.length,session:sessionStorage.length}));if(storage.local||storage.session)throw Error('브라우저 비밀 저장소 사용');
 if(errors.length||egress.length)throw Error(JSON.stringify({errors,egress}));
 const result={checks,capability_controls:true,manual_id:true,native_origin_locked:true,errors,unexpected_network:egress,mobile_overflow:overflow,browser_storage:storage,live_calls:0,live_validation:'NOT_VALIDATED'};
 const text=JSON.stringify(result,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 불가');fs.writeFileSync(path.join(root,'qa/results/multi_provider_visual_results.json'),Buffer.from(text,'utf8'));console.log(JSON.stringify(result));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill();});
