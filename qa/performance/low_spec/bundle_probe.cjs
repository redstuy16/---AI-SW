// Chrome이 완료한 응답의 실제 네트워크 바이트를 별도로 기록한다.
'use strict';
const fs=require('fs'),path=require('path'),{spawn}=require('child_process');
const {chromium}=require('../../browser_runtime.cjs');
const root=process.cwd(),env={...process.env};for(const k of Object.keys(env))if(/API_KEY|TOKEN|SECRET/.test(k))delete env[k];
const child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-X','utf8','-m','htrsa.workbench','build/flow-opt-fixture/state.sqlite','build/flow-opt-fixture/workspace','--no-browser'],{cwd:root,env,windowsHide:true});
let browser,buffer='';
async function main(){const url=await new Promise((resolve,reject)=>{child.stdout.on('data',b=>{buffer+=b;const m=buffer.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]+/);if(m)resolve(m[0]);});child.on('exit',c=>reject(Error('서버 종료 '+c)));setTimeout(()=>reject(Error('서버 시작 시간 초과')),20000).unref();});
 browser=await chromium.launch({channel:'chrome',headless:true});const context=await browser.newContext(),page=await context.newPage(),client=await context.newCDPSession(page);const response=new Map(),finished=[];let outside=0;
 await client.send('Network.enable');await client.send('Network.setCacheDisabled',{cacheDisabled:true});
 client.on('Network.responseReceived',e=>{const value=new URL(e.response.url);if(value.origin!==new URL(url).origin)outside++;response.set(e.requestId,value.pathname);});
 client.on('Network.loadingFinished',e=>{if(response.has(e.requestId))finished.push({path:response.get(e.requestId),encoded_bytes:e.encodedDataLength});});
 await page.goto(url);await page.locator('#research-table').waitFor();await page.waitForLoadState('networkidle');
 if(outside)throw Error('외부 요청 발생');const result={scope:'초기 화면 · Chrome CDP loadingFinished encodedDataLength · HTTP 헤더 포함, 캐시 비활성',requests:finished,total_encoded_bytes:finished.reduce((n,v)=>n+v.encoded_bytes,0),before:'NOT_VALIDATED',paid_live_calls:0};
 const text=JSON.stringify(result,null,2)+'\n';if(/[\uD800-\uDFFF]/u.test(text)||text.includes('#bootstrap='))throw Error('기록 인코딩·비밀 차단');fs.writeFileSync(path.join(root,'qa/performance/low_spec/bundle_after.json'),Buffer.from(text,'utf8'));console.log(JSON.stringify({responses:finished.length,total_encoded_bytes:result.total_encoded_bytes}));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();child.kill();});
