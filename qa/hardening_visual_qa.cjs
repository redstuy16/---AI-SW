// 실제 Chrome에서 연구 설정·파일 선택·오프라인 완료 흐름을 검사한다.
'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/hardening-ui',crypto.randomUUID());
const output=path.join(root,'output/playwright/hardening');fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(output,{recursive:true});
const env={...process.env};for(const k of Object.keys(env))if(/API_KEY|TOKEN|SECRET/.test(k))delete env[k];
const child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-X','utf8','qa/hardening_visual_fixture.py',folder],{cwd:root,windowsHide:true,env});
let browser,stdout='',stderr='';const checks=[],shots=[],errors=[],logs=[],egress=[],privateValues=[];
const handoff=new Promise((resolve,reject)=>{child.stdout.on('data',b=>{stdout+=b;const m=stdout.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=([A-Za-z0-9_-]+)/);if(m){privateValues.push(m[1]);resolve(m[0]);}});child.stderr.on('data',b=>stderr+=b);child.on('exit',c=>reject(Error('검증 서버 종료 '+c)));setTimeout(()=>reject(Error('검증 서버 시작 시간 초과')),15000).unref();});
function check(name,passed,details){if(!passed)throw Error('검사 실패: '+name);checks.push({name,passed:true,...details});}
function save(file,value){const s=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(s)||privateValues.some(v=>s.includes(v)))throw Error('검증 기록 인코딩·비밀 차단');fs.writeFileSync(file,Buffer.from(s,'utf8'));}
async function main(){
 const url=await handoff,origin=new URL(url).origin;
 browser=await chromium.launch({channel:'chrome',headless:true});const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();page.setDefaultTimeout(20000);
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>logs.push(m.text()));page.on('request',r=>{if(!r.url().startsWith(origin+'/'))egress.push(r.url());});
 await page.goto(url);await page.locator('#research-table').waitFor();
 const session=await page.evaluate(async()=>await(await fetch('/api/session')).json());privateValues.push(session.csrf);for(const c of await context.cookies())privateValues.push(c.value);
 async function research(){await page.locator('#new-research').click();await page.locator('#research-form').waitFor();}
 async function capture(name){const file=path.join(output,name+'.png');await page.screenshot({path:file,mask:[page.locator('input[type=password]')]});shots.push(path.relative(root,file).replaceAll('\\','/'));check(name+' 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));}
 await page.locator('[data-view=settings]').click();await page.locator('#show-explanations').waitFor();
 check('v4 AI 연결 기본 설정',await page.locator('#connection-new').isVisible());
 check('일반 설정에 중복 모델 선택 없음',await page.locator('#featured-models,#settings-tab-model,#gpt-setup').count()===0);
 check('예산 설정 한 곳',await page.locator('#defaults-form').count()===1);
 await page.locator('#low-spec-mode').selectOption('LOW_SPEC');await page.getByText('설정을 저장했습니다.',{exact:true}).waitFor();
 check('저사양 설정 저장 성공 초록 알림',await page.locator('#notice').evaluate(e=>e.dataset.status==='success'));
 await page.locator('[data-view=research]').click();await research();
 check('상단 모델 5개',await page.locator('#featured-models .model-option').count()===5);
 check('더보기 기본 접힘',await page.locator('#more-models').getAttribute('aria-expanded')==='false');
 check('고급 설정 기본 접힘',!await page.locator('#research-advanced').evaluate(e=>e.open));
 check('추천·속도·성능 배지 없음',!(/추천|권장|고성능|빠른/.test(await page.locator('#featured-models').innerText())));
 check('선택적 일반 파일 찾아보기',await page.locator('[name=attachment_files]').getAttribute('type')==='file'&&await page.locator('[name=attachment_files]').getAttribute('accept')===null);
 check('실험 기능 기본 꺼짐',await page.locator('[name=verified_analysis_skills],[name=verification_repair],[name=ridge_arithmetic_check]').evaluateAll(es=>es.every(e=>!e.checked)));
 check('모든 성능 라벨 제거',await page.locator('.performance-labels').count()===0);
 const size=await page.locator('#performance').evaluate(e=>({height:e.getBoundingClientRect().height}));
 check('성능 슬라이더 높이 52px 이상',size.height>=52,{measured:size});
 for(const v of [0,1,2,3]){await page.locator('#performance').fill(String(v));check('추론 단계만 표시 '+v,/^(빠르게|균형|깊게|최대)$/.test(await page.locator('#performance-detail').innerText()));}
 await page.locator('#performance').fill('1');await page.locator('[name=question]').fill('temperature와 growth의 관계를 분석');
 check('CSV 없이 초안 저장 가능',await page.locator('#attachment-list .attachment-row').count()===0);
 check('실패 뒤 질문 유지',await page.locator('[name=question]').inputValue()==='temperature와 growth의 관계를 분석');
 await page.keyboard.press('Escape');await research();check('양식 다시 열어도 질문 유지',await page.locator('[name=question]').inputValue()==='temperature와 growth의 관계를 분석');
 await page.locator('[name=attachment_files]').setInputFiles(path.join(root,'tests/fixtures/monotonic_nonlinear.csv'));
 await page.locator('#research-advanced>summary').click();
 check('검증 가벼운 순서와 각각 설명',JSON.stringify(await page.locator('.verification-option span').evaluateAll(es=>es.map(e=>e.firstChild.textContent)))===JSON.stringify(['분석 도구 검사','Ridge 수치 검사','F3-P 오류 복구'])&&await page.locator('.verification-option small:not(.cost-hint)').count()===3);
 check('Ridge는 F3-P 없이 선택 불가',await page.locator('[name=ridge_arithmetic_check]').isDisabled());
 await page.locator('[name=verification_repair]').check();await page.locator('[name=ridge_arithmetic_check]').check();await page.locator('[name=verification_repair]').uncheck();
 check('F3-P 해제 시 Ridge 해제',!await page.locator('[name=ridge_arithmetic_check]').isChecked());
 await page.locator('[name=manual_role_override]').check();
 check('역할 최상위·중간·하위 고정 묶음',JSON.stringify(await page.locator('.tier-group h4').allTextContents())===JSON.stringify(['최상위','중간','하위']));
 check('실제 역할 4개 보존',await page.locator('#role-models select:not([name^=reasoning_])').count()===4);await page.locator('[name=manual_role_override]').uncheck();
 check('PDF 고정 형식·선택 UI 없음',await page.locator('[name=report_format],[name=report_style]').count()===0);
 check('비용 설명 제거·상대 API 사용 배지',await page.locator('#research-form .cost-hint').count()===0&&['낮음','중간','높음'].includes(await page.locator('#performance-badge').innerText()));
 await page.locator('[name=egress]').selectOption('selected');await page.locator('[name=search_required]').uncheck();await page.locator('[name=run_limit_usd]').fill('0.10');
 await capture('research-advanced-desktop');
 await page.locator('#research-advanced>summary').click();await page.locator('#editor').evaluate(e=>e.scrollTop=0);await capture('research-slider-desktop');
 await page.setViewportSize({width:390,height:844});await page.locator('#editor').evaluate(e=>e.scrollTop=0);await capture('research-slider-mobile');
 await page.locator('#research-form [data-help=performance]').click();await page.locator('#guide-search').waitFor();
 check('도움말 한 주제만 표시',await page.locator('.beginner-guide').count()===1&&await page.locator('.help-nav').count()===0);await capture('help-mobile');await page.locator('#inspector-close').click();
 await page.setViewportSize({width:1440,height:900});
 await page.waitForFunction(()=>!document.querySelector('#attachment-list')?.textContent.includes('업로드'));
 check('준비 확인은 유료 전송 0',await page.evaluate(async()=>{const v=await(await fetch('/api/control/usage')).json();return v.requests.length===0;}));
 check('준비 확인만으로 연구 생성 안 됨',await page.evaluate(async()=>(await(await fetch('/api/control/research')).json()).length===0));
 // 기본 설정 저장 실패는 이미 시작한 연구의 이동을 막으면 안 된다.
 await page.route('**/api/control/preferences',route=>route.fulfill({status:503,json:{error:'OFFLINE_PREFS_FAILURE'}}));
 await page.locator('#research-start').click();await page.locator('#normal-flow').waitFor();
 const rid=await page.evaluate(()=>app.rid),control=await page.evaluate(async id=>await(await fetch('/api/control/research/'+id+'/control')).json(),rid);
 check('연구 시작부터 오프라인 완료',control.status==='COMPLETED',{status:control.status});
 check('검색 불필요한 CSV 계산 검색 전송 0',await page.evaluate(async id=>{const v=await(await fetch('/api/control/research/'+id+'/control')).json();return v.effective_snapshot.search_policy==='AUTO';},rid));
 await page.locator('[data-tab=timeline]').waitFor();await page.locator('[data-tab=timeline]').click();await page.locator('#activity-next').waitFor();
 check('저사양 활동 기록 페이지 25개 이하',await page.locator('#run-content tbody tr').count()<=25);
 await page.locator('[data-tab=report]').click();await page.locator('#run-content').waitFor();await page.waitForFunction(()=>document.querySelector('#run-content').textContent.length>100);await capture('offline-report');
 check('완료 후 자동 새로고침 중지',control.status==='COMPLETED');
 const storage=await page.evaluate(async()=>({local:localStorage.length,session:sessionStorage.length,indexedDB:(await indexedDB.databases()).length,workers:(await navigator.serviceWorker.getRegistrations()).length}));
 check('브라우저 영구 저장소 없음',Object.values(storage).every(v=>v===0));
 check('인증 비밀이 화면·콘솔에 없음',!privateValues.some(v=>logs.some(x=>x.includes(v))||stderr.includes(v)));
 check('JS 오류·외부 요청 0',errors.length===0&&egress.length===0);
 const result={passed:true,checks,screenshots:shots,storage,errors,unexpected_network:egress,worker:'기존 FakeProvider · HTTP 스레드 내 실행',credential_backend:'MOCK_OS_STORE',live_api_calls:0,live_validation:'NOT_VALIDATED'};
 save(path.join(root,'qa/prepublish/usability_visual_validation.json'),result);console.log(JSON.stringify({passed:true,checks:checks.length,screenshots:shots.length,live_api_calls:0}));
}
main().catch(e=>{let t=e.message;for(const v of privateValues)t=t.split(v).join('[비밀 제거됨]');console.error((checks.at(-1)?.name||'시작')+' 이후: '+t);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();child.kill();});
