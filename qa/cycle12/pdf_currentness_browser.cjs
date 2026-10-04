// 질문 변경·화면 지연·파일 변조에서 PDF 안내와 기존 서버 차단을 검사한다.
const {chromium}=require('../browser_runtime.cjs');
const {spawn}=require('child_process');
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const root=path.resolve(__dirname,'../..');
const folder=path.join(root,'output/playwright/pdf-currentness',crypto.randomUUID());
fs.mkdirSync(folder,{recursive:true});
const checks=[],errors=[];let child,browser,reportPath,originalReport;
const actionMessage='현재 결론을 다시 확인해야 합니다. 계산 다시 확인하기를 눌러 주세요.';
function save(value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 검사 실패');fs.writeFileSync(path.join(folder,'result.json'),Buffer.from(text,'utf8'));}
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw Error(name);}
async function main(){
 const env={...process.env};for(const key of Object.keys(env))if(key.endsWith('KEY')||key.includes('TOKEN')||key.startsWith('HTRSA_CONN_'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/cycle12/browser_fixture.py',folder],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
 let stderr='';child.stderr.on('data',v=>stderr+=v);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',v=>{text+=v;const m=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m)resolve(m[0]);});child.on('exit',()=>reject(Error(stderr.slice(-1200))));setTimeout(()=>reject(Error('서버 시작 시간 초과')),30000).unref();});
 const origin=new URL(url).origin;
 browser=await chromium.launch({channel:'chrome',headless:process.env.HTRSA_UI_TEST_HEADED!=='1'});
 const page=await browser.newPage({viewport:{width:1280,height:900}});page.setDefaultTimeout(20000);
 const requests=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{requests.push(r.url());if(!r.url().startsWith(origin+'/')&&!r.url().startsWith('blob:'))errors.push('외부 요청');});
 await page.addInitScript(()=>{window.previewPolicyFailures=[];document.addEventListener('securitypolicyviolation',e=>window.previewPolicyFailures.push(e.effectiveDirective));});
 check('인증 없는 PDF 조회 차단',(await fetch(origin+'/api/control/research/unknown/report.pdf')).status===401);
 await page.goto(url);await page.locator('#research-table').waitFor();if(await page.locator('#tutorial-skip').count())await page.locator('#tutorial-skip').click();
 await page.locator('[data-run]').first().click();await page.locator('.conclusion-card').waitFor();await page.locator('#normal-report').click();await page.locator('[data-pdf]').waitFor();
 const rid=JSON.parse(fs.readFileSync(path.join(folder,'execution.json'),'utf8')).research_id;
 const downloading=page.waitForEvent('download');await page.locator('[data-pdf]').click();const download=await downloading;await download.saveAs(path.join(folder,'before.pdf'));
 check('현재 결론 PDF 실제 다운로드',fs.readFileSync(path.join(folder,'before.pdf')).subarray(0,5).toString()==='%PDF-');
 await page.locator('#preview-pdf').click();await page.locator('#inspector[open] .friendly-report').waitFor();check('검증된 보고서 내용 미리보기',await page.locator('#inspector .friendly-report section h2').count()===9&&(await page.locator('#inspector .friendly-report').innerText()).includes('0.405')&&await page.locator('#inspector iframe').count()===0);await page.locator('#inspector').screenshot({path:path.join(folder,'current-preview.png')});await page.locator('#inspector-close').click();
 await page.locator('#profile-amend').click();await page.locator('#profile-question-form textarea').fill('1986~1995년과 2011~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요.');await page.locator('#profile-question-form button.primary').click();await page.getByText('다시 확인 필요',{exact:true}).waitFor();
 check('질문 변경 후 현재 결론 해제',(await page.locator('.conclusion-card').innerText()).includes('변경한 질문 · 재확인 대기'));
 const priorPdf=requests.filter(u=>u.endsWith('/report.pdf')).length;
 await page.locator('[data-pdf]').click();await page.locator('#notice').filter({hasText:actionMessage}).waitFor();check('PDF 저장에 실제 재확인 조치 안내',await page.locator('#notice').innerText()===actionMessage);
 await page.locator('#preview-pdf').click();await page.locator('#notice').filter({hasText:actionMessage}).waitFor();check('PDF 미리보기에 같은 조치 안내',await page.locator('#notice').innerText()===actionMessage&&await page.locator('#inspector[open]').count()===0);
 check('재확인 전 PDF 생성 요청 생략',requests.filter(u=>u.endsWith('/report.pdf')).length===priorPdf);
 const blocked=await page.evaluate(async id=>(await fetch('/api/control/research/'+id+'/report.pdf',{credentials:'same-origin'})).status,rid);check('직접 PDF 요청의 서버 검증 차단 유지',blocked===409);
 await page.screenshot({path:path.join(folder,'stale-action.png'),fullPage:true});
 await page.locator('#profile-recalculate').click();await page.getByText('현재 결론',{exact:true}).waitFor();check('재계산 후 현재 결론 복원',(await page.locator('.conclusion-card').innerText()).includes('0.512'));
 const changed=page.waitForEvent('download');await page.locator('[data-pdf]').click();await (await changed).saveAs(path.join(folder,'after.pdf'));check('재계산 후 PDF 다운로드',fs.readFileSync(path.join(folder,'after.pdf')).subarray(0,5).toString()==='%PDF-');
 const card=await page.evaluate(id=>api('/api/control/research/'+id+'/conclusion-card'),rid);
 await page.evaluate(({id,version})=>api('/api/control/research/'+id+'/amend-question',{question:'1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요.',expected_version:version}),{id:rid,version:card.state_version});
 check('저장 상태 변경 후 이전 화면 상태 유지',await page.getByText('현재 결론',{exact:true}).count()===1);
 await page.locator('[data-pdf]').click();await page.locator('#notice').filter({hasText:actionMessage}).waitFor();check('이전 화면에서도 최신 현재성 조회로 차단',await page.locator('#notice').innerText()===actionMessage);
 await page.locator('#profile-recalculate').click();await page.getByText('현재 결론',{exact:true}).waitFor();
 await page.waitForFunction(()=>!document.querySelector('#profile-recalculate').disabled);
 reportPath=path.join(folder,'workspace',rid,'research_output/final_report.md');originalReport=fs.readFileSync(reportPath);fs.writeFileSync(reportPath,Buffer.concat([originalReport,Buffer.from('\n검사 중 보고서 변조\n','utf8')]));
 const current=await page.evaluate(id=>api('/api/control/research/'+id+'/conclusion-card'),rid);check('파일 변조 검사는 현재성 검사와 별도로 유지',current.current===true);
 const response=page.waitForResponse(r=>r.url().endsWith('/report.pdf'));await page.locator('[data-pdf]').click();check('현재 결론이어도 변조 보고서 생성 차단',(await response).status()===409);
 await page.locator('#notice').filter({hasText:'내보내기 검사 결과'}).waitFor();check('무결성 실패의 기존 내보내기 안내 유지',(await page.locator('#notice').innerText()).includes('내보내기 검사 결과'));
 check('브라우저 오류·외부 요청 없음',errors.length===0);
 check('미리보기 CSP 위반 없음',await page.evaluate(()=>window.previewPolicyFailures.length===0));
 const execution=JSON.parse(fs.readFileSync(path.join(folder,'execution.json'),'utf8'));const usage=await page.evaluate(()=>api('/api/control/usage'));check('초기 모의 Agent 1회·실제 비용 예약 없음',execution.fake_model_calls===1&&execution.paid_calls===0&&usage.requests.length===0);
 save({execution:'REAL_CHROME_OFFLINE_REAL_TOOLS_FAKE_AGENT',passed:true,checks,errors,paid_calls:0,live_efficacy:'NOT_VALIDATED',output_dir:path.relative(root,folder)});
 console.log(JSON.stringify({passed:true,checks:checks.length,output_dir:path.relative(root,folder),paid_calls:0}));
}
main().catch(e=>{save({passed:false,checks,errors,error:e.message,output_dir:path.relative(root,folder)});console.error(e.message);process.exitCode=1;}).finally(async()=>{if(reportPath&&originalReport)fs.writeFileSync(reportPath,originalReport);if(browser)await browser.close();if(child)child.kill();});
