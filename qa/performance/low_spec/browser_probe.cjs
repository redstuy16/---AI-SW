// 같은 합성 자료와 실제 로컬 인증으로 브라우저 준비 시간을 측정한다.
const {chromium}=require('../../browser_runtime.cjs');
const {spawn}=require('child_process'),fs=require('fs'),path=require('path');
(async()=>{
 const samples=[];const root=process.cwd();
 for(let i=0;i<3;i++){
  const t=performance.now();const child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-X','utf8','-m','probe.workbench','build/flow-opt-fixture/state.sqlite','build/flow-opt-fixture/workspace','--no-browser'],{cwd:root,windowsHide:true});
  let buffer='',resolve;const url=new Promise(r=>resolve=r);child.stdout.on('data',d=>{buffer+=d;const m=buffer.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]+/);if(m)resolve(m[0]);});
  let browser;try{
   browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage();let requests=0,bytes=0;const pending=[];
   page.on('response',r=>{requests++;pending.push((async()=>{try{bytes+=(await r.body()).length;}catch{}})());});
   await page.goto(await url);await page.locator('#research-table button[data-run]').first().waitFor();
   const ready=performance.now()-t,atReady=bytes;await page.waitForLoadState('networkidle');await Promise.all(pending);samples.push({browser_ready_ms:ready,requests,response_bytes_at_ready:atReady,completed_response_bytes:bytes});
  }finally{if(browser)await browser.close();child.kill();}
 }
 const result={samples,scope:'Chrome headless + 실제 loopback 인증 · OS 기본 브라우저 열기 제외'};
 const value=JSON.stringify(result,null,2)+'\n';if(/[\uD800-\uDFFF]/u.test(value))throw Error('UTF-8 검사 실패');
 fs.writeFileSync(process.argv[2],Buffer.from(value,'utf8'));console.log(JSON.stringify(result));
})().catch(e=>{console.error(e.message);process.exitCode=1;});
