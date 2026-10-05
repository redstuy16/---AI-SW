// 격리된 비용 원장과 실제 Chrome 화면의 갱신·이동을 검사한다.
const {chromium}=require('./browser_runtime.cjs');
const fs=require('fs'),path=require('path'),{spawn}=require('child_process');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/cost-display-browser-'+(process.env.PROBE_COST_MOBILE==='1'?'mobile':'desktop')+'-'+Date.now());
const output=path.join(root,'output/playwright/research_cost');
fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(output,{recursive:true});
const checks=[],errors=[];let browser,child;
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw Error(name);}
async function main(){
 const env={...process.env,PYTHONDONTWRITEBYTECODE:'1'};for(const key of Object.keys(env))if(key.endsWith('API_KEY'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/research_cost_browser_fixture.py',folder],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env});
 let stderr='';child.stderr.on('data',v=>stderr+=v);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',v=>{text+=v;const m=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m)resolve(m[0]);});child.on('exit',()=>reject(Error(stderr.slice(-1400))));setTimeout(()=>reject(Error('서버 시간 초과')),15000).unref();});
 const origin=new URL(url).origin,mobile=process.env.PROBE_COST_MOBILE==='1',zoom=process.env.PROBE_COST_ZOOM==='1';
 let page;
 if(zoom){
  const profile=path.join(folder,'chrome-profile');fs.mkdirSync(path.join(profile,'Default'),{recursive:true});
  save(path.join(profile,'Default/Preferences'),{partition:{default_zoom_level:{x:Math.log(2)/Math.log(1.2)}}});
  browser=await chromium.launchPersistentContext(profile,{channel:'chrome',headless:false,viewport:{width:1280,height:850}});
  page=await browser.newPage();
 }else{
  browser=await chromium.launch({channel:'chrome',headless:false});
  page=await browser.newPage({viewport:{width:mobile?390:1280,height:850}});
 }
 page.setDefaultTimeout(15000);
 page.on('pageerror',x=>errors.push(x.message));page.on('request',r=>{if(!r.url().startsWith(origin+'/'))errors.push('외부 요청');});
 await page.goto(url);await page.locator('[data-cost-history]').first().waitFor();
 const before=await page.evaluate(()=>api('/qa/state'));
 const rows=await page.locator('.research-list-card').allTextContents();
 check('목록의 연구별 비용',rows.some(t=>t.includes('비용 추적 연구')&&t.includes('$0.006'))&&rows.some(t=>t.includes('별도 연구')&&t.includes('$0.015')));
 const row=page.locator('.research-list-card').filter({hasText:'비용 추적 연구'}),rid=await row.locator('[data-run]').getAttribute('data-run');
 await row.locator('[data-cost-history]').click();await page.locator('#usage-table').waitFor();
 check('연구에 속한 비용 기록',await page.locator('#usage-table tbody tr').count()===3);
 await page.locator('#usage-back').click();await page.locator('#run-content').waitFor();
 await page.locator('#research-workspace .research-money details>summary').click();
 save(path.join(folder,'initial-cost.json'),{spent:await page.locator('[data-cost-field=spent]').textContent(),reserved:await page.locator('[data-cost-field=reserved]').textContent()});
 check('사용 비용과 펼친 예약 비용 표시',await page.locator('[data-cost-field=spent]').innerText()==='$0.006'&&await page.locator('[data-cost-field=reserved]').innerText()==='$0.02');
 await page.evaluate(()=>api('/qa/cost',{}));
 await page.waitForFunction(()=>document.querySelector('[data-cost-field=spent]')?.textContent==='$0.009',{},{timeout:12000});
 check('5초 자동 비용 갱신',await page.locator('[data-cost-field=reserved]').innerText()==='$0.00');
 check('좁은 화면 비용 영역 유지',await page.locator('#research-cost').evaluate(el=>el.getBoundingClientRect().right<=innerWidth&&el.scrollWidth<=el.clientWidth));
 const screen=path.join(output,zoom?'desktop-zoom.png':mobile?'mobile.png':'desktop.png');await page.screenshot({path:screen,fullPage:true});
 await page.locator('#run-usage').click();await page.locator('#usage-table').waitFor();await page.locator('#usage-role').selectOption('search');
 await page.waitForFunction(()=>document.querySelectorAll('#usage-table tbody tr').length===1);
 check('웹 검색 비용 기록 필터',await page.locator('#usage-table').innerText().then(t=>t.includes('$0.002')));
 await page.locator('#usage-role').focus();await page.waitForTimeout(5500);
 check('갱신 중 필터와 키보드 초점 유지',await page.locator('#usage-role').inputValue()==='search'&&await page.locator('#usage-role').evaluate(el=>el===document.activeElement));
 await page.locator('#usage-back').click();await page.locator('#run-content').waitFor();
 await page.route('**/usage?*',route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":"QA_READ_FAILED"}'}));
 await page.waitForFunction(()=>document.querySelector('#cost-refresh-status')?.textContent.includes('갱신 실패'),{},{timeout:12000});
 check('조회 실패 시 마지막 금액 유지',await page.locator('[data-cost-field=spent]').innerText()==='$0.009');
 await page.unroute('**/usage?*');await page.waitForFunction(()=>document.querySelector('#cost-refresh-status')?.textContent.startsWith('갱신 '),{},{timeout:12000});
 check('조회 복구 후 자동 갱신 재개',true);
 await page.locator('[data-primary-tab=timeline]').click();await page.locator('[data-tab=timeline][aria-current=page]').waitFor();
 check('진행 기록에서도 비용 표시',await page.locator('[data-cost-field=spent]').innerText()==='$0.009');
 await page.locator('[data-primary-tab=overview]').click();await page.locator('[data-tab=overview][aria-current=page]').waitFor();
 await page.locator('[data-view=settings]').click();await page.locator('.settings-nav').waitFor();await page.waitForTimeout(5500);
 check('설정 이동 후 비용 패널 제거',await page.locator('#research-cost').count()===0);
 await page.locator('[data-view=research]').click();await page.locator('#research-table').waitFor();
 await page.reload();await page.locator('#research-table').waitFor();
 const refreshed=page.locator('.research-list-card').filter({hasText:'비용 추적 연구'});
 check('새로고침 후 비용 기록 보존',await refreshed.innerText().then(t=>t.includes('$0.009')));
 await refreshed.locator('[data-run]').click();await page.locator('#run-content').waitFor();
 check(zoom?'Chrome 실제 200% 확대에서 비용 읽기 가능':'비용 텍스트 표시',await page.locator('[data-cost-field=spent]').isVisible()&&(!zoom||await page.evaluate(()=>devicePixelRatio===2&&innerWidth===640&&!document.documentElement.style.zoom)));
 const after=await page.evaluate(()=>api('/qa/state'));
 check('조회와 이동은 원장·연구·모델 호출을 변경하지 않음',before.researches===after.researches&&before.agents===after.agents&&before.requests===after.requests&&after.checks===0&&after.agents===0);
 check('브라우저 오류와 외부 요청 없음',errors.length===0);
 save(path.join(folder,'result.json'),{status:'PASS',checks,errors,paid_calls:0,before,after,research_id:rid,screenshot:screen});
 console.log(JSON.stringify({status:'PASS',checks:checks.length,paid_calls:0,result:path.join(folder,'result.json')}));
}
main().catch(e=>{errors.push(e.message);save(path.join(folder,'result.json'),{status:'FAIL',checks,errors});console.error(e.message);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(child)child.kill();});
