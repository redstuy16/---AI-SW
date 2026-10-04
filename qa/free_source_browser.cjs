// 공개 원문 페이지를 보고서 옆에서 읽는 실제 Chrome 회귀 검사.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/free-search/browser',crypto.randomUUID()),out=path.join(root,'output/playwright/free-search');
let child,context;const checks=[],errors=[],external=[];
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8');fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,value){checks.push({name,passed:!!value});if(!value)throw Error(name);}
(async()=>{try{
 fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(out,{recursive:true});const env={...process.env};for(const k of Object.keys(env))if(k.endsWith('API_KEY'))delete env[k];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/research_report_browser_fixture.py',folder,'--fulltext'],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});let stderr='';child.stderr.on('data',v=>stderr+=v);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',v=>{text+=v;const m=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m)resolve(m[0]);});child.on('exit',()=>reject(Error(stderr.slice(-1500))));setTimeout(()=>reject(Error('서버 시간 초과')),45000).unref();});
 const origin=new URL(url).origin,ids=JSON.parse(fs.readFileSync(path.join(folder,'fixture.json'),'utf8'));
 const profile=path.join(folder,'chrome-profile');fs.mkdirSync(path.join(profile,'Default'),{recursive:true});if(process.argv.includes('--zoom'))save(path.join(profile,'Default/Preferences'),{partition:{default_zoom_level:{x:Math.log(2)/Math.log(1.2)}}});
 context=await chromium.launchPersistentContext(profile,{channel:'chrome',headless:true,viewport:{width:1280,height:900}});const page=await context.newPage();page.setDefaultTimeout(25000);
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith(origin+'/')&&!r.url().startsWith('blob:'))external.push(r.url());});
 await page.goto(url);await page.locator('#research-table').waitFor();const before=await page.evaluate(()=>api('/qa/observed'));
 if(process.argv.includes('--zoom'))check('브라우저 자체 200% 확대',await page.evaluate(()=>devicePixelRatio===2&&innerWidth===640&&!document.documentElement.style.zoom));
 await page.goto(origin+'/#research/'+ids.research_id+'/flow');await page.locator('[data-flow-category=knowledge]').waitFor();await page.locator('[data-flow-category=knowledge]').click();await page.locator('#flow-collection').waitFor();
 check('요청·자료 수집의 다섯 집계',await page.locator('#flow-collection .collection-counts>span').count()===5);
 check('원문을 읽은 연구 완료',await page.locator('#run-state').innerText()==='완료');
 await page.locator('[data-primary-tab=report]').click();await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');
 await page.locator('#report-evidence').click();await page.locator('[data-source-document]').click();await page.waitForFunction(()=>document.querySelector('#source-pdf-viewer')?.dataset.ready==='true');
 check('인용에서 원문 두 번째 페이지로 이동',await page.locator('#source-pdf-page').inputValue()==='2');
 check('보고서와 원문 동시 렌더링',await page.locator('#pdf-canvas').evaluate(c=>c.width>100)&&await page.locator('#source-pdf-canvas').evaluate(c=>c.width>100));
 const sourceText=await page.locator('#source-pdf-text').textContent();check('원문에 실제 인용 텍스트',/CO2\s+release\s+increased/.test(sourceText));
 const reportPage=await page.locator('#pdf-page').inputValue();await page.locator('#source-pdf-viewer .pdf-canvas-wrap').focus();await page.keyboard.press('ArrowLeft');await page.waitForFunction(()=>document.querySelector('#source-pdf-page').value==='1');
 check('원문 키보드 이동은 보고서 위치 보존',await page.locator('#pdf-page').inputValue()===reportPage);
 await page.locator('#source-pdf-next').click();await page.waitForFunction(()=>document.querySelector('#source-pdf-viewer')?.dataset.ready==='true');
 check('원문 버튼 간격',await page.locator('#source-pdf-viewer .pdf-toolbar').evaluate(e=>parseFloat(getComputedStyle(e).gap)>=8));
 check('일반·확대 화면 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 await page.locator('#source-pdf-canvas').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,process.argv.includes('--zoom')?'source-zoom200.png':'source-wide.png')});
 if(!process.argv.includes('--zoom')){await page.setViewportSize({width:390,height:844});await page.locator('#source-pdf-fit').click();await page.waitForFunction(()=>document.querySelector('#source-pdf-viewer')?.dataset.ready==='true');check('390px 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.locator('#source-pdf-canvas').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'source-390.png')});}
 const after=await page.evaluate(()=>api('/qa/observed'));check('원문 조회·페이지 이동은 추가 검색·모델 요청 없음',after.searches===before.searches&&after.model_calls.length===before.model_calls.length);
 check('브라우저 오류·외부 요청 없음',!errors.length&&!external.length);
}catch(e){errors.push(e.stack||e.message);process.exitCode=1;}finally{if(context)await context.close();if(child)child.kill();save(path.join(folder,'result.json'),{checks,errors,external,paid_calls:0,execution:'CHROME_OFFLINE_MOCK_PROVIDER'});console.log(JSON.stringify({folder,checks:checks.length,passed:checks.every(c=>c.passed)&&!errors.length,errors},null,2));}})();
