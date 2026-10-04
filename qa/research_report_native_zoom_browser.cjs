// 별도 Chrome 프로필에서 브라우저 자체 200% 확대를 확인한다.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/research-report/native-zoom',crypto.randomUUID()),out=path.join(root,'output/playwright/research-report');
const checks=[],errors=[],external=[];let child,context,geometry;
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,value){checks.push({name,passed:!!value});if(!value)throw Error(name);}
(async()=>{
 try{
 fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(out,{recursive:true});const profile=path.join(folder,'chrome-profile');fs.mkdirSync(path.join(profile,'Default'),{recursive:true});
 save(path.join(profile,'Default/Preferences'),{partition:{default_zoom_level:{x:Math.log(2)/Math.log(1.2)}}});
 const env={...process.env};for(const k of Object.keys(env))if(k.endsWith('API_KEY'))delete env[k];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/research_report_browser_fixture.py',folder,'--many'],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
 let stderr='';child.stderr.on('data',v=>stderr+=v);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',v=>{text+=v;const m=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m)resolve(m[0]);});child.on('exit',()=>reject(Error(stderr.slice(-2000))));setTimeout(()=>reject(Error('서버 시간 초과')),45000).unref();});
 const origin=new URL(url).origin,ids=JSON.parse(fs.readFileSync(path.join(folder,'fixture.json'),'utf8'));
 context=await chromium.launchPersistentContext(profile,{channel:'chrome',headless:true,viewport:{width:1280,height:900}});const page=await context.newPage();page.setDefaultTimeout(20000);
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith(origin+'/')&&!r.url().startsWith('blob:'))external.push(r.url());});
 await page.goto(url);await page.locator('#research-table').waitFor();const before=await page.evaluate(()=>api('/qa/observed'));
 geometry=await page.evaluate(()=>({dpr:devicePixelRatio,width:innerWidth,height:innerHeight,css:document.documentElement.style.zoom}));check('Chrome 자체 200% 확대',geometry.dpr===2&&geometry.width===640&&!geometry.css);
 await page.goto(origin+'/#research/'+ids.research_id+'/flow');await page.locator('#stage-map').waitFor();
 check('확대 시 실제 일곱 단계',await page.locator('.research-stage-map li').count()===7);
 check('확대 시 현재 상태·비용 표시',await page.locator('#run-state').isVisible()&&await page.locator('[data-cost-field=spent]').isVisible());
 await page.locator('[data-flow-category=experiment]').click();check('확대 시 설계안 표시',await page.locator('.design-preview .tag').innerText()==='설계안');
 check('확대 진행 화면 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 await page.locator('[data-primary-tab=evidence]').click();await page.locator('.evidence-row').first().waitFor();check('확대 시 다섯 문헌 표시',await page.locator('.evidence-row').count()===5);
 check('긴 문헌 제목 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 await page.locator('[data-primary-tab=report]').click();await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');
 check('확대 시 PDF 실제 렌더',await page.locator('#pdf-canvas').evaluate(c=>c.width>200&&c.height>200));
 check('확대 보고서 버튼 간격',await page.locator('.pdf-toolbar').evaluate(e=>parseFloat(getComputedStyle(e).gap)>=8));
 await page.locator('.pdf-canvas-wrap').focus();await page.keyboard.press('ArrowRight');await page.waitForFunction(()=>document.querySelector('#pdf-page').value==='2');check('확대 PDF 키보드 페이지 이동',await page.locator('#pdf-page').inputValue()==='2');
 await page.locator('#pdf-fit').click();await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');
 check('확대 PDF 너비 맞춤과 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 const pixels=await page.locator('#pdf-canvas').evaluate(c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let count=0;for(let i=0;i<d.length;i+=4)if(d[i+3]&&Math.min(d[i],d[i+1],d[i+2])<230)count++;return count;});check('확대 PDF 캔버스에 실제 글자와 그림 픽셀',pixels>1000);
 const canvas=await page.locator('#pdf-canvas').evaluate(c=>c.toDataURL('image/png'));fs.writeFileSync(path.join(out,'native-zoom200-canvas.png'),Buffer.from(canvas.split(',')[1],'base64'));
 await page.locator('#pdf-canvas').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'native-zoom200.png'),fullPage:false});
 const after=await page.evaluate(()=>api('/qa/observed'));check('확대·조회는 AI·검색 요청 없음',after.model_calls.length===before.model_calls.length&&after.searches===before.searches);
 check('브라우저 오류·외부 요청 없음',!errors.length&&!external.length);
 }catch(e){errors.push(e.stack||e.message);process.exitCode=1;}
 finally{if(context)await context.close();if(child)child.kill();save(path.join(folder,'result.json'),{checks,errors,external,geometry,paid_calls:0,execution:'REAL_CHROME_NATIVE_200_PERCENT_OFFLINE'});console.log(JSON.stringify({folder,checks:checks.length,passed:checks.every(c=>c.passed)&&!errors.length,errors},null,2));}
})();
