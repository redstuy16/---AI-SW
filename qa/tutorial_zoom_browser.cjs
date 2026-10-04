// 별도 Chrome 프로필의 실제 200% 확대에서 안내와 입력 화면을 검사한다.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/tutorial/zoom',crypto.randomUUID()),shots=path.join(root,'output/playwright/tutorial');
const checks=[],errors=[];let child,context,testPage;
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 검사 실패');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,value){checks.push({name,passed:!!value});if(!value)throw Error(name);}
async function main(){
 fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(shots,{recursive:true});
 const profile=path.join(folder,'chrome-profile');fs.mkdirSync(path.join(profile,'Default'),{recursive:true});
 save(path.join(profile,'Default/Preferences'),{partition:{default_zoom_level:{x:Math.log(2)/Math.log(1.2)}}});
 const env={...process.env};for(const key of Object.keys(env))if(key.endsWith('API_KEY'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/tutorial_browser_fixture.py',folder],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env});
 let stderr='';child.stderr.on('data',value=>stderr+=value);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',value=>{text+=value;const match=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(match)resolve(match[0]);});child.on('exit',()=>reject(Error(stderr.slice(-1000))));setTimeout(()=>reject(Error('서버 시간 초과')),30000).unref();});
 context=await chromium.launchPersistentContext(profile,{channel:'chrome',headless:false,viewport:{width:1280,height:900}});
 const page=testPage=await context.newPage();page.setDefaultTimeout(20000);const origin=new URL(url).origin;
 page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(!request.url().startsWith(origin+'/')&&!request.url().startsWith('blob:'))errors.push('외부 요청');});
 await page.goto(url);await page.locator('#tutorial-card').waitFor();
 const zoom=await page.evaluate(()=>({dpr:devicePixelRatio,width:innerWidth,css:document.documentElement.style.zoom}));
 check('Chrome 자체 200% 확대 확인',zoom.dpr===2&&zoom.width===640&&!zoom.css);
 check('최초 안내 확대 시 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 await page.locator('#tutorial-accept').click();await page.locator('#provider-preparation').waitFor();
 check('발급 글 확대 시 안내 상자 가로·세로 넘침 없음',await page.locator('.tutorial-coach-box').evaluate(element=>{const box=element.getBoundingClientRect();return element.scrollWidth<=element.clientWidth+2&&box.left>=0&&box.right<=innerWidth&&box.top>=0&&box.bottom<=innerHeight;}));
 check('긴 발급 글은 상자 안에서 스크롤하고 다음 버튼 유지',await page.locator('.tutorial-coach-content').evaluate(element=>element.scrollHeight>element.clientHeight&&getComputedStyle(element).overflowY==='auto')&&await page.locator('#tutorial-coach-next').isVisible());
 await page.screenshot({path:path.join(shots,'native-zoom-provider.png'),fullPage:false});
 await page.locator('.tutorial-menu>summary').click();await page.locator('#tutorial-guide').click();
 check('사용 안내에서 무료 예시 제거',await page.locator('#guide-example,#learning-example').count()===0);
 check('사용 안내 확대 시 가로 넘침 없음',await page.locator('#inspector').evaluate(element=>element.scrollWidth<=element.clientWidth+2));
 await page.locator('#inspector-close').click();await page.locator('#tutorial-resume').click();await page.locator('.tutorial-menu>summary').click();await page.locator('#tutorial-guide').click();await page.locator('#guide-search').fill('발급');await page.locator('[data-guide-step=provider_key]').click();
 check('사용 안내 확대 시 가로 넘침 없음',await page.locator('#inspector').evaluate(element=>element.scrollWidth<=element.clientWidth+2));
 check('사용 안내 확대 시 읽기 내용 존재',await page.locator('.provider-lesson').innerText().then(text=>text.includes('OpenAI')&&text.includes('API 키 발급')));
 await page.locator('#guide-search').focus();await page.keyboard.press('Tab');check('확대 상태에서 키보드 목차 접근',await page.evaluate(()=>['BUTTON','SUMMARY'].includes(document.activeElement.tagName)));
 await page.screenshot({path:path.join(shots,'native-zoom-guide.png'),fullPage:false});
 await page.locator('#inspector-close').click();await page.locator('#tutorial-resume').click();await page.locator('#tutorial-coach-next').click();await page.locator('#connection-form').waitFor();
 check('안내 버튼 사이 간격 유지',await page.locator('.tutorial-coach-actions').evaluate(element=>parseFloat(getComputedStyle(element).gap)>=8));
 check('확대 상태에서 중앙 정보 팝업·옛 작업 버튼 제거',await page.locator('#tutorial-card,#tutorial-open,[data-tutorial-step]').count()===0);
 check('확대 상태에서 실제 입력과 화살표 안내 표시',await page.locator('#editor-content #tutorial-card').count()===0&&!await page.locator('#tutorial-popup').isVisible()&&await page.locator('#tutorial-coach').isVisible());
 check('확대 상태 연결 양식 가로 넘침 없음',await page.locator('#editor').evaluate(element=>element.scrollWidth<=element.clientWidth+2));
 await page.waitForFunction(()=>document.querySelector('#tutorial-coach')?.dataset.route==='connection');
 check('200% 확대에서도 화살표와 한 개 안내 상자',await page.locator('.tutorial-coach-box').count()===1&&await page.locator('.tutorial-coach-arrow').count()===1&&await page.locator('.tutorial-coach-box').evaluate(element=>{const box=element.getBoundingClientRect();return box.left>=0&&box.right<=innerWidth&&box.top>=0&&box.bottom<=innerHeight;}));
 await page.locator('#connection-form [name=api_key]').focus();await page.keyboard.press('Tab');check('확대 상태에서 실제 입력 키보드 이동',await page.evaluate(()=>document.activeElement.tagName!=='BODY'));
 check('키보드 이동 후 입력칸 하나만 강조',await page.locator('.tutorial-coach-target,.tutorial-focus').evaluateAll(elements=>new Set(elements).size===1));
 check('연결 화면에서도 Chrome 자체 200% 확대 유지',await page.evaluate(()=>devicePixelRatio===2&&innerWidth===640&&!document.documentElement.style.zoom));
 await page.screenshot({path:path.join(shots,'native-zoom-connection.png'),fullPage:false});
 const state=await page.evaluate(()=>api('/qa/state'));check('학습·화면 이동의 연구·원장·키·검사 생성 없음',state.researches===0&&state.agents===0&&state.requests===0&&state.keys===0&&state.checks===0);
 check('브라우저 오류·외부 요청 없음',errors.length===0);
 save(path.join(folder,'result.json'),{passed:true,execution:'REAL_CHROME_NATIVE_200_PERCENT_OFFLINE',checks,errors,zoom,paid_calls:0,folder:path.relative(root,folder)});
 process.stdout.write(JSON.stringify({passed:true,checks:checks.length,zoom,paid_calls:0,folder:path.relative(root,folder)})+'\n');
}
main().catch(async error=>{const diagnostic=testPage?await testPage.evaluate(()=>({notice:document.querySelector('#notice')?.textContent,view:typeof app==='undefined'?null:app.view,preferences:typeof app==='undefined'?null:app.settings?.preferences,body:document.body.innerText.slice(0,1200)})).catch(()=>null):null;save(path.join(folder,'result.json'),{passed:false,checks,errors,error:error.message,diagnostic});process.stderr.write(error.stack+'\n');process.exitCode=1;}).finally(async()=>{if(context)await context.close();if(child)child.kill();});
