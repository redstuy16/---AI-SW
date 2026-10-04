// 기존 번들 Playwright와 실제 Chrome으로 기본·고급·보고서·키 입력 경계를 검사한다.
const fs=require('fs'),path=require('path'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),fixture=JSON.parse(fs.readFileSync(path.join(root,'build/product_visual_fixture.json'),'utf8'));
const output=path.join(root,'output/playwright/product');fs.mkdirSync(output,{recursive:true});
const server=spawn(path.join(root,'.venv/Scripts/python.exe'),['-m','htrsa.workbench',fixture.database,fixture.workspace,'--mode','DEMO','--no-browser','--port','8881'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,LOCALAPPDATA:path.join(root,'build/qa-key-disabled')}});
let browser;
async function main(){
 const handoff=await new Promise((resolve,reject)=>{let text='';server.stdout.on('data',b=>{text+=b.toString();const m=text.match(/(http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43})/);if(m)resolve(m[1]);});server.on('exit',c=>reject(Error('서버 종료 '+c)));setTimeout(()=>reject(Error('서버 시작 시간 초과')),15000).unref();});
 browser=await chromium.launch({channel:'chrome',headless:true});const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
 const errors=[],egress=[],logs=[],shots=[],checks=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>logs.push(m.text()));page.on('request',r=>{if(!r.url().startsWith('http://127.0.0.1:8881'))egress.push(r.url());});
 await page.goto(handoff);await page.locator('#research-table').waitFor();
 async function capture(name){const target=path.join(output,name+'.png');await page.screenshot({path:target,fullPage:!(await page.locator('dialog[open]').count())});shots.push(target);const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2);checks.push({name,viewport:page.viewportSize(),overflow});if(overflow){const nodes=await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+2).slice(0,12).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right})));throw Error('가로 넘침 '+name+' '+JSON.stringify(nodes));}}
 async function list(){await page.locator('[data-view="research"]').click();await page.locator('#research-table').waitFor();}
 async function settings(){await page.locator('[data-view="settings"]').click();await page.locator('#settings-tab-connections').waitFor();await page.locator('#settings-tab-connections').click();}
 async function run(id,tab){await page.goto(`http://127.0.0.1:8881/#research/${id}/${tab}`);await page.locator(`[data-tab="${tab}"][aria-current="page"]`).waitFor();await page.waitForFunction(()=>document.querySelector('#run-content')?.textContent.length>10);}
 for(const size of [{width:1440,height:900},{width:390,height:844}]){
  await page.setViewportSize(size);const suffix=size.width===390?'narrow':'desktop';
  await list();await page.locator('#new-research').click();await page.locator('#research-form').waitFor();
  if(await page.locator('#research-advanced').evaluate(e=>e.open))throw Error('고급 기본 펼침');if(!await page.locator('[name="adaptive_budget"]').isChecked())throw Error('자동 예산 기본 OFF');
  await capture('01-simple-'+suffix);await page.locator('#quick-recommended').click();await page.locator('#performance').focus();await page.keyboard.press('ArrowRight');if(await page.locator('#performance').inputValue()!=='2')throw Error('성능 키보드 조작 실패');
  await page.locator('#research-advanced>summary').click();await page.locator('[name="manual_role_override"]').check();await capture('02-advanced-'+suffix);await page.keyboard.press('Escape');
  await page.locator('#help-open').click();await page.locator('#guide-search').fill('예산');await page.locator('[data-guide-step=budget]').click();await capture('03-help-'+suffix);await page.locator('#inspector-close').click();
  await settings();if(await page.locator('#settings-2,#settings-3,#gpt-setup').count())throw Error('중복 모델 설정');await capture('04-catalog-'+suffix);
  await page.locator('#settings-tab-connections').click();await page.locator('[data-key="qa-gpt"]').click();await page.locator('#key-form').waitFor();await capture('05-key-'+suffix);await page.keyboard.press('Escape');
  await run(fixture.controlled_research_id,'overview');await page.locator('#research-settings').click();await page.locator('#run-settings-form').waitFor();await capture('06-research-settings-'+suffix);await page.locator('#run-settings-form #research-advanced>summary').click();await page.getByRole('heading',{name:'연구 완료에 필요한 예산'}).scrollIntoViewIfNeeded();await capture('07-completion-budget-'+suffix);await page.keyboard.press('Escape');
  await run(fixture.research_id,'report');await page.locator('.friendly-report').waitFor();if(await page.locator('.friendly-report>section').count()!==9)throw Error('보고서 섹션 누락');await capture('08-report-'+suffix);
  await page.getByRole('heading',{name:'8. 한계와 미해결 문제',exact:true}).scrollIntoViewIfNeeded();await capture('09-limitations-'+suffix);
 }
 const styleRoute=`**/api/control/research/${fixture.research_id}/report-view`;
 await page.route(styleRoute,async route=>{const response=await route.fetch();const view=await response.json();view.report_style='technical';await route.fulfill({response,json:view});});
 await run(fixture.research_id,'overview');await run(fixture.research_id,'report');await page.locator('.friendly-report').waitFor();
 if(await page.locator('[data-pdf]').count()!==1||await page.locator('.technical-report').count())throw Error('PDF 고정 형식 미반영');await page.unroute(styleRoute);
 await settings();await page.locator('#settings-tab-connections').click();await page.locator('[data-key="qa-gpt"]').click();const canary='browser-canary-not-a-real-key-0123456789';await page.locator('#key-form [name="value"]').fill(canary);await page.locator('#key-form button.primary').click();
 await page.waitForFunction(()=>document.querySelector('#key-form input')?.value==='');if((await page.locator('body').innerText()).includes(canary)||logs.some(v=>v.includes(canary)))throw Error('키 노출');
 const storage=await page.evaluate(()=>({local:localStorage.length,session:sessionStorage.length}));if(storage.local||storage.session)throw Error('브라우저 비밀 저장소 사용');await page.keyboard.press('Escape');
 await list();await page.locator('#new-research').click();await page.locator('#performance').focus();await page.keyboard.press('ArrowRight');const focus=await page.locator('#performance').evaluate(e=>({outline:getComputedStyle(e).outlineStyle,visible:e.getBoundingClientRect().width>0}));if(focus.outline==='none'||!focus.visible)throw Error('키보드 포커스 표시 없음');await page.keyboard.press('Escape');
 // 200% CSS 확대와 좁은 레이아웃을 직접 검사하며 브라우저 OS 확대 검증으로 기록하지 않는다.
 await page.setViewportSize({width:1280,height:900});await settings();await page.evaluate(()=>document.documentElement.style.zoom='2');await capture('10-css-200-percent');await page.evaluate(()=>document.documentElement.style.zoom='');
 if(errors.length||egress.length)throw Error(JSON.stringify({errors,egress}));
 const result={screenshots:shots,checks,errors,unexpected_network:egress,keyboard_slider:true,focus,advanced_collapsed:true,adaptive_default_on:true,report_sections:9,pdf_download_choice:true,legacy_technical_metadata_compatible:true,key_failure_input_cleared:true,covered_canary_exposures:0,browser_storage:storage,paid_calls:0,mode:'DEMO',live_efficacy:'NOT_VALIDATED',zoom:'실제 CSS zoom 200% + 390px 재배치 · OS 브라우저 확대는 미검증'};
 const text=JSON.stringify(result,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 불가');fs.writeFileSync(path.join(root,'qa/results/product_visual_results.json'),Buffer.from(text,'utf8'));console.log(JSON.stringify({screenshots:shots.length,checks:checks.length,paid_calls:0,canary_exposures:0}));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill();});
