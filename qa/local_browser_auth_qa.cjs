// 메모리 핸드오프를 받는 QA 브라우저 컨트롤러와 실제 Chrome을 사용한다.
const fs=require('fs'),path=require('path'),http=require('http'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),run=crypto.randomUUID();
const base=path.join(root,'build','browser-auth-visual',run),out=path.join(root,'output/playwright/local-auth');
fs.mkdirSync(base,{recursive:true});fs.mkdirSync(out,{recursive:true});
const privateValues=[],children=[],captures=[],checks=[],consoleValues=[],pageErrors=[];
const relayToken=crypto.randomBytes(32).toString('hex'),waiters=new Map();
let browser,relay;
function check(name,value){if(!value)throw Error('검사 실패: '+name);checks.push({name,passed:true});save(path.join(base,'progress.json'),{last_check:name,checks:checks.length,children:children.length});}
function remember(value){if(value)privateValues.push(value);}
function save(name,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 불가');if(privateValues.some(v=>text.includes(v)))throw Error('검증 결과 인증 값 차단');fs.writeFileSync(name,Buffer.from(text,'utf8'));}
async function stop(child){if(child.exitCode!==null||child.signalCode!==null)return;await new Promise(resolve=>{child.once('exit',resolve);child.kill();});}
async function start({port=0,expired=false,headless=false,failed=false}={}){
 const id=crypto.randomUUID(),dir=path.join(base,id);fs.mkdirSync(dir);
 const handoff=new Promise((resolve,reject)=>{waiters.set(id,resolve);const timer=setTimeout(()=>reject(Error('시작 제한 시간')),15000);timer.unref();});
 const py=`import os,sys,webbrowser,httpx\nfrom probe.workbench import main,OwnerSession\nclass QABrowser(webbrowser.BaseBrowser):\n def open(self,url,new=0,autoraise=True):\n  if os.environ.get('QA_OPEN_FAILED')=='1': return False\n  with httpx.Client(trust_env=False) as client:\n   return client.post(os.environ['QA_RELAY'],json={'id':os.environ['QA_INSTANCE'],'url':url},headers={'Authorization':'Bearer '+os.environ['QA_RELAY_TOKEN']}).status_code==200\nwebbrowser.register('probe-qa-browser',None,QABrowser(),preferred=True)\nif os.environ.get('QA_OPEN_FAILED')=='1': webbrowser.open=lambda *_a,**_k: False\nif os.environ.get('QA_EXPIRED')=='1': OwnerSession.ticket_lifetime=0\nsys.argv=['probe.workbench',*sys.argv[1:]]\nmain()\n`;
 const args=['-c',py,path.join(dir,'state.sqlite'),path.join(dir,'workspace'),'--port',String(port),'--mode','DEMO'];if(headless)args.push('--no-browser');
 const child=spawn(path.join(root,'.venv/Scripts/python.exe'),args,{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,QA_RELAY:relay.origin+'/handoff',QA_RELAY_TOKEN:relayToken,QA_INSTANCE:id,QA_EXPIRED:expired?'1':'0',QA_OPEN_FAILED:failed?'1':'0'}});
 children.push(child);child.output='';child.errors='';
 child.stdout.on('data',b=>{child.output+=b.toString();if(headless||failed){const m=child.output.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m&&waiters.has(id)){waiters.get(id)(m[0]);waiters.delete(id);}}});
 child.stderr.on('data',b=>child.errors+=b.toString());
 const url=await handoff,origin=new URL(url).origin,ticket=url.split('#bootstrap=')[1];remember(ticket);
 return {child,url,origin,ticket,dir};
}
function watch(page){page.on('console',m=>consoleValues.push(m.text()));page.on('pageerror',e=>pageErrors.push(e.message));}
async function connected(page,url){await page.goto(url);await page.locator('#research-table').waitFor();check('자동 연구 목록',await page.locator('#pair').evaluate(e=>!e.open));}
async function capture(page,name){const p=path.join(out,name+'.png');await page.screenshot({path:p,fullPage:true});captures.push(p);check(name+' 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));}
async function storage(page){return page.evaluate(async()=>({local:localStorage.length,session:sessionStorage.length,indexedDB:(await indexedDB.databases()).length,serviceWorkers:(await navigator.serviceWorker.getRegistrations()).length,visibleCookie:document.cookie}));}
async function main(){
 relay=http.createServer((req,res)=>{
  if(req.url==='/frame'){res.writeHead(200,{'Content-Type':'text/html'});res.end('<html><body><iframe src="'+relay.frameOrigin+'/"></iframe></body></html>');return;}
  if(req.method!=='POST'||req.url!=='/handoff'||req.headers.authorization!=='Bearer '+relayToken){res.writeHead(403);res.end();return;}
  let body='';req.on('data',b=>body+=b);req.on('end',()=>{const value=JSON.parse(body);if(waiters.has(value.id)){waiters.get(value.id)(value.url);waiters.delete(value.id);}res.writeHead(200);res.end();});
 });await new Promise(resolve=>relay.listen(0,'127.0.0.1',resolve));relay.origin='http://127.0.0.1:'+relay.address().port;
 browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();watch(page);
 const first=await start();let bootstrapHeaders;
 page.on('response',r=>{if(r.url().endsWith('/auth/bootstrap'))bootstrapHeaders=r.headers();});
 await connected(page,first.url);
 check('정상 stdout 비밀 없음',!privateValues.some(v=>first.child.output.includes(v)));
 check('주소 조각 제거',!page.url().includes('bootstrap')&&!page.url().includes(first.ticket));
 check('no-store·referrer·frame CSP',bootstrapHeaders['cache-control']==='no-store'&&bootstrapHeaders['referrer-policy']==='no-referrer'&&bootstrapHeaders['content-security-policy'].includes("frame-ancestors 'none'"));
 let owner=(await context.cookies(first.origin)).find(c=>c.name.startsWith('probe_owner_'));remember(owner.value);
 check('독립 HttpOnly 세션',owner.httpOnly&&owner.sameSite==='Strict'&&!owner.secure&&owner.value!==first.ticket);
 const metadata=await page.evaluate(async()=>await(await fetch('/api/session')).json());remember(metadata.csrf);
 check('브라우저 저장 없음',Object.values(await storage(page)).every(v=>v===0||v===''));
 await capture(page,'01-auto-connected');
 const repeat=await context.newPage();watch(repeat);await repeat.goto(first.url);await repeat.locator('body[data-auth-state="failed"]').waitFor();
 check('두 번째 탭 재사용 거부',await repeat.locator('#auth-status').isVisible());
 check('실패 화면 비밀 없음',!privateValues.some(v=>(repeat.url()).includes(v))&&!privateValues.some(v=>consoleValues.some(s=>s.includes(v))));
 await repeat.setViewportSize({width:390,height:844});await capture(repeat,'02-replay-narrow');
 const shared=await context.newPage();watch(shared);await connected(shared,first.origin);
 check('동일 세션 새 탭 사용',true);
 const second=await start(),a=await context.newPage(),b=await context.newPage();watch(a);watch(b);
 await Promise.all([a.goto(second.origin),b.goto(second.origin)]);
 await Promise.all([a.locator('body[data-auth-state="failed"]').waitFor(),b.locator('body[data-auth-state="failed"]').waitFor()]);
 await Promise.all([a.goto(second.url),b.goto(second.url)]);
 await Promise.all([a.waitForFunction(()=>document.body.dataset.authState!=='connecting'),b.waitForFunction(()=>document.body.dataset.authState!=='connecting')]);
 const states=await Promise.all([a.evaluate(()=>document.body.dataset.authState),b.evaluate(()=>document.body.dataset.authState)]);
 check('동시 탭 정확히 한 번 교환',states.filter(x=>x==='authenticated').length===1&&states.filter(x=>x==='failed').length===1);
 const cookies=await context.cookies();check('인스턴스 별 쿠키 이름',cookies.filter(c=>c.name.startsWith('probe_owner_')).length===2);
 for(const c of cookies)remember(c.value);
 const deny=await context.request.post(second.origin+'/auth/bootstrap',{headers:{Origin:second.origin,'X-Probe-Bootstrap':'1'},data:{ticket:first.ticket}});
 check('다른 인스턴스 티켓 거부',deny.status()===403);
 relay.frameOrigin=first.origin;const framed=await context.newPage();watch(framed);await framed.goto(relay.origin+'/frame');
 await framed.waitForTimeout(300);check('실제 브라우저 framing 차단',!framed.frames().some(f=>f.url()===first.origin+'/'));
 await stop(first.child);
 const restarted=await start();
 await shared.goto(restarted.origin);await shared.locator('body[data-auth-state="failed"]').waitFor();check('재시작 옛 세션 거부',true);
 const old=await context.request.post(restarted.origin+'/auth/bootstrap',{headers:{Origin:restarted.origin,'X-Probe-Bootstrap':'1'},data:{ticket:first.ticket}});
 check('재시작 옛 티켓 거부',old.status()===403);await connected(shared,restarted.url);
 const expiry=await start({expired:true}),expiredPage=await context.newPage();watch(expiredPage);await expiredPage.goto(expiry.url);await expiredPage.locator('body[data-auth-state="failed"]').waitFor();
 check('만료 한국어 재실행 안내',(await expiredPage.locator('#auth-status').innerText()).includes('다시 실행'));
 await expiredPage.setViewportSize({width:390,height:844});await capture(expiredPage,'03-expired-narrow');
 const fallback=await start({failed:true}),fallbackPage=await context.newPage();watch(fallbackPage);await connected(fallbackPage,fallback.url);
 check('브라우저 실패 1회용 링크 하나',fallback.child.output.split('#bootstrap=').length===2);await capture(fallbackPage,'04-fallback-connected');
 const headless=await start({headless:true}),headlessPage=await context.newPage();watch(headlessPage);await connected(headlessPage,headless.url);
 check('headless 1회용 링크 하나',headless.child.output.split('#bootstrap=').length===2);
 check('콘솔·화면 canary 없음',!privateValues.some(v=>consoleValues.some(s=>s.includes(v))));
 for(const c of await context.cookies())remember(c.value);
 for(const p of [page,repeat,shared,a,b,expiredPage,fallbackPage,headlessPage]){
  remember(await p.evaluate(()=>app.csrf));const bodyText=await p.locator('body').innerText();
  check('화면·저장소 비밀 없음',!privateValues.some(v=>p.url().includes(v)||bodyText.includes(v)));
  check('페이지 저장소 0',Object.values(await storage(p)).every(v=>v===0||v===''));
 }
 function files(dir){return fs.readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?files(path.join(dir,e.name)):[path.join(dir,e.name)]);}
 const persisted=files(base);check('DB·workspace 인증 값 없음',persisted.every(p=>!privateValues.some(v=>fs.readFileSync(p).includes(Buffer.from(v)))));
 check('페이지 JS 오류 0',pageErrors.length===0);
 for(const item of children){check('서버 stderr 비밀 없음',!privateValues.some(v=>item.errors.includes(v)));}
 const result={passed:true,checks,screenshots:captures,page_errors:pageErrors,console_secret_exposures:0,storage_secret_exposures:0,normal_stdout_exposures:0,paid_live_calls:0,default_browser_association:'NOT_VALIDATED',mode:'실제 Chrome + 등록한 QA 브라우저 컨트롤러',expired_handoff:'QA 전용 TTL=0 고장 주입; 기본 60초는 단위 테스트로 확인',note:'브라우저의 Local Network 보호와 CSP를 변경하지 않았다. fallback stdout의 명시적 1회용 링크는 메모리에서만 수신했고 산출물에 기록하지 않았다.'};
 save(path.join(root,'qa/results/local_browser_auth_visual_results.json'),result);
 console.log(JSON.stringify({passed:true,checks:checks.length,screenshots:captures.length,secret_exposures:0,paid_live_calls:0}));
}
main().catch(e=>{let message=e.message;for(const value of privateValues)message=message.split(value).join('[인증 값 제거됨]');console.error(message);process.exitCode=1;}).finally(async()=>{for(const child of children)await stop(child);if(browser)await browser.close();if(relay)await new Promise(resolve=>relay.close(resolve));});
