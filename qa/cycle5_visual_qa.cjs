// 실제 Chrome에서 의미 상세·반응형·기본 OFF·문자열 이스케이프를 검사한다.
const fs=require('fs'),path=require('path'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),fixture=JSON.parse(fs.readFileSync(path.join(root,'build/cycle5_visual_fixture.json'),'utf8'));
const output=path.join(root,'output/playwright/cycle5');fs.mkdirSync(output,{recursive:true});
const server=spawn(path.join(root,'.venv/Scripts/python.exe'),['-m','htrsa.workbench',fixture.database,fixture.workspace,'--mode','DEMO','--no-browser','--port','0'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
let browser;const checks=[],shots=[],errors=[],egress=[];
function check(name,passed){if(!passed)throw Error('검사 실패: '+name);checks.push({name,passed:true});}
async function main(){
 const handoff=await new Promise((resolve,reject)=>{let text='';server.stdout.on('data',b=>{text+=b.toString();const match=text.match(/(http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43})/);if(match)resolve(match[1]);});server.on('exit',c=>reject(Error('서버 종료 '+c)));setTimeout(()=>reject(Error('서버 시작 시간 초과')),15000).unref();});
 const origin=new URL(handoff).origin;
 browser=await chromium.launch({channel:'chrome',headless:true});const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();page.setDefaultTimeout(15000);
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith(origin+'/'))egress.push(r.url());});
 await page.goto(handoff);await page.locator('#research-table').waitFor();
 async function tab(rid,name){await page.goto(origin+'/#research/'+rid+'/'+name);await page.locator('[data-tab="'+name+'"][aria-current="page"]').waitFor();await page.waitForFunction(()=>document.querySelector('#run-content')?.children.length>0);}
 async function capture(name){const file=path.join(output,name+'.png');await page.screenshot({path:file,fullPage:true});shots.push(file);check(name+' 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));}
 for(const width of [1440,390]){
  await page.setViewportSize({width,height:900});
  for(const name of ['evidence','verification','report']){
   await tab(fixture.research_id,name);await page.locator('#semantic-details').waitFor();
   const text=await page.locator('#semantic-details').innerText();check(name+' 실제 의미·단위·기간 '+width,text.includes('a-units')&&text.includes('고정 관측 기간')&&text.includes('사용자 검토 완료'));
   check(name+' 네 검사 범위 표시 '+width,['자료 의미','변환 이력','연구 질문 범위','주장 근거'].every(s=>text.includes(s)));
   check(name+' 내부 schema 원문 숨김 '+width,!text.includes('target_hash')&&!text.includes('semantic_refs'));
   check(name+' 공격 문자열 텍스트 처리 '+width,text.includes('<img src=')&&await page.locator('#semantic-details img').count()===0&&await page.evaluate(()=>!window.__cycle5_xss));
   await capture(name+'-'+width);
  }
 }
 await page.setViewportSize({width:1280,height:900});await page.evaluate(()=>document.documentElement.style.zoom='2');await tab(fixture.research_id,'verification');await page.locator('#semantic-details').waitFor();await capture('verification-css-200');await page.evaluate(()=>document.documentElement.style.zoom='');
 await tab(fixture.legacy_id,'evidence');check('기본 OFF 연구는 의미 패널 없음',await page.locator('#semantic-details').count()===0);
 check('JavaScript 오류·외부 요청 0',errors.length===0&&egress.length===0);
 const result={all_passed:true,checks,screenshots:shots,errors,unexpected_network:egress,source_fingerprint:fixture.source_fingerprint,paid_live_calls:0,os_default_browser:'NOT_VALIDATED',live_efficacy:'NOT_VALIDATED'};
 const text=JSON.stringify(result,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 불가');fs.writeFileSync(path.join(root,'qa/results/cycle5_visual_results.json'),Buffer.from(text,'utf8'));console.log(JSON.stringify({all_passed:true,checks:checks.length,screenshots:shots.length,errors:errors.length,unexpected_network:egress.length}));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill();});
