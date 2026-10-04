// 실제 Chrome에서 조사·검색 사전 검사·PDF.js·작은 화면을 확인한다.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/research-report/browser',crypto.randomUUID()),out=path.join(root,'output/playwright/research-report'),weak=process.argv.includes('--weak');
fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(out,{recursive:true});
let child,browser;const checks=[],errors=[],violations=[],external=[];let stderr='';
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,value){checks.push({name,passed:!!value});save(path.join(folder,'progress.json'),{checks,errors});if(!value)throw Error(name);}
(async()=>{
 try{
 const env={...process.env};for(const k of Object.keys(env))if(k.endsWith('API_KEY'))delete env[k];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/research_report_browser_fixture.py',folder,...(process.argv.includes('--many')?['--many']:[]),...(weak?['--weak']:[])],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
 child.stderr.on('data',v=>stderr+=v);
 const url=await new Promise((resolve,reject)=>{let t='';child.stdout.on('data',v=>{t+=v;const m=t.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m)resolve(m[0]);});child.on('exit',()=>reject(Error(stderr.slice(-2000))));setTimeout(()=>reject(Error('서버 시간 초과')),45000).unref();});
 const origin=new URL(url).origin,ids=JSON.parse(fs.readFileSync(path.join(folder,'fixture.json'),'utf8'));
 browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage({viewport:{width:1280,height:900}});page.setDefaultTimeout(20000);
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith(origin+'/')&&!r.url().startsWith('blob:'))external.push(r.url());});
 await page.addInitScript(()=>document.addEventListener('securitypolicyviolation',e=>{(window.pdfViolations??=[]).push(e.effectiveDirective);}));
 await page.goto(url);await page.locator('#research-table').waitFor();
 const observed=()=>page.evaluate(()=>api('/qa/observed'));
 const before=await observed();
 await page.goto(origin+'/#research/'+ids.blocked_id+'/flow');await page.locator('#stage-map').waitFor();
 check('기존 연구의 정확한 검색 동의 차단 원인',(await page.locator('#run-blocker').innerText()).includes('공개 검색 동의가 없어'));
 check('기존 기록 보완은 새 초안',await page.locator('#repair-search').isVisible());
 await page.goto(origin+'/#research/'+ids.research_id+'/flow');await page.locator('#stage-map').waitFor();
 await page.goto(origin+'/#research/'+ids.research_id+'/timeline');await page.locator('[data-tab=timeline][aria-current=page]').waitFor();check('기존 진행 기록 주소 유지',await page.locator('[data-tab=timeline][aria-current=page]').isVisible());await page.locator('[data-primary-tab=overview]').click();await page.locator('#core-result').waitFor();check('기본 화면 복귀 주소 보존',page.url().endsWith('/overview'));await page.locator('[data-primary-tab=flow]').click();await page.locator('#stage-map').waitFor();
 check('단일 연구 메뉴 일곱 개',await page.locator('[data-primary-tab]').count()===7);
 check('일곱 연구 단계',await page.locator('.research-stage-map li').count()===7);
 await page.locator('[data-flow-category=experiment]').click();check('설계안 표시',await page.locator('.design-preview .tag').innerText()==='설계안');
 check('실제 완료 상태',(await page.locator('#run-state').innerText())==='완료');
 check('지출과 잔여 예산 표시',await page.locator('[data-cost-field=spent]').count()===1&&await page.locator('[data-cost-field=available]').count()===1);
 await page.evaluate(({rid,title})=>api('/api/control/research/'+rid+'/rename',{title}),{rid:ids.research_id,title:'탄산음료 온도와 기체 방출 연구의 조건과 측정 방법을 확인하는 긴 제목 '.repeat(6).slice(0,200)});
 await page.reload();await page.locator('#stage-map').waitFor();check('긴 제목은 상단 두 줄로 표시',await page.locator('.research-header h1').evaluate(e=>getComputedStyle(e).webkitLineClamp==='2'));
 for(const width of [1280,390]){await page.setViewportSize({width,height:900});const layout=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,items:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+2&&getComputedStyle(e).position!=='absolute').slice(0,30).map(e=>({tag:e.tagName,id:e.id,cls:e.className,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right}))}));save(path.join(folder,'layout-'+width+'.json'),layout);await page.screenshot({path:path.join(folder,'flow-'+width+'.png'),fullPage:true});check('진행 화면 '+width+'px 가로 넘침 없음',layout.scroll<=width+2);}
 await page.setViewportSize({width:1280,height:900});await page.screenshot({path:path.join(out,'progress.png'),fullPage:true});
 await page.locator('[data-phase-select=search]').click();check('선택 단계의 오른쪽 상세',await page.locator('#research-detail').isVisible());await page.locator('#detail-close').click();
 await page.locator('[data-primary-tab=evidence]').click();await page.locator('.evidence-row').first().waitFor();if(process.argv.includes('--many'))check('여러 문헌을 검토한 기록 표시',await page.locator('.evidence-row').count()>=5);await page.locator('[data-source]').first().click();
 await page.locator('#detail-body h2').filter({hasText:'Temperature'}).waitFor();check('출처 상세 제공',(await page.locator('#detail-body').innerText()).includes('Temperature'));await page.locator('#detail-close').click();
 await page.locator('[data-primary-tab=report]').click();await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');
 check('앱에 포함된 PDF.js 실제 렌더',await page.locator('#pdf-canvas').evaluate(c=>c.width>200&&c.height>200));
 check('외부 iframe 사용 없음',await page.locator('iframe').count()===0);
 check('페이지·확대·검색·목차·다운로드 도구',await page.locator('#pdf-page,#pdf-plus,#pdf-minus,#pdf-fit,#pdf-search,#pdf-outline,[data-pdf]').count()===7);
 await page.locator('#pdf-outline summary').click();check('여섯 PDF 목차',await page.locator('#pdf-outline-items button').count()===6);
 await page.locator('#pdf-outline-items button').last().click();await page.waitForFunction(()=>Number(document.querySelector('#pdf-page').value)>1);
 await page.locator('#pdf-page').fill('1');await page.locator('#pdf-page').dispatchEvent('change');
 await page.locator('#pdf-plus').click();await page.waitForFunction(()=>document.querySelector('#pdf-zoom').textContent==='120%');check('확대 조작',await page.locator('#pdf-zoom').innerText()==='120%');await page.locator('#pdf-fit').click();
 await page.locator('#pdf-search').fill('온도');await page.locator('#pdf-search-form button').click();await page.locator('[data-pdf-hit]').first().waitFor();check('PDF 텍스트 검색',await page.locator('[data-pdf-hit]').count()>0);
 await page.locator('.pdf-canvas-wrap').focus();const oldPage=Number(await page.locator('#pdf-page').inputValue());await page.keyboard.press('ArrowRight');await page.waitForFunction(n=>Number(document.querySelector('#pdf-page').value)===n,oldPage+1);check('키보드 페이지 이동',Number(await page.locator('#pdf-page').inputValue())===oldPage+1);
 await page.locator('#report-evidence').click();await page.locator('#detail-body .evidence-row').first().waitFor();check('PDF와 근거 함께 읽기',await page.locator('#pdf-canvas').isVisible()&&await page.locator('#research-detail').isVisible());await page.locator('#detail-close').click();
 const selected=await page.locator('#pdf-page').inputValue();await page.evaluate(()=>window.ResearchWorkspace.refresh());check('갱신 시 PDF 선택 보존',await page.locator('#pdf-page').inputValue()===selected);
 const viewing=await observed();check('조회·탭 이동·PDF 조작은 모델과 검색 요청 없음',viewing.model_calls.length===before.model_calls.length&&viewing.searches===before.searches);
 const download=page.waitForEvent('download');await page.locator('[data-pdf]').click();await (await download).saveAs(path.join(folder,'download.pdf'));check('PDF 실제 다운로드',fs.readFileSync(path.join(folder,'download.pdf')).subarray(0,5).toString()==='%PDF-');
 for(const [name,width,zoom] of [['desktop',1280,1],['mobile',390,1],['zoom200',1280,2]]){
  await page.setViewportSize({width,height:900});await page.evaluate(z=>document.documentElement.style.zoom=String(z),zoom);await page.locator('#pdf-fit').click();
  await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');
  check(name+' 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
  check(name+' 버튼 간격',await page.locator('.pdf-toolbar').evaluate(e=>parseFloat(getComputedStyle(e).gap)>=8));
  await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});
 }
 await page.evaluate(()=>document.documentElement.style.zoom='1');await page.setViewportSize({width:1280,height:900});
 await page.locator('#run-back').click();await page.locator('#list-new').click();await page.locator('#research-input-mode').waitFor();await page.locator('[name=question]').fill('탄산음료의 온도에 따른 CO₂ 방출 속도');await page.locator('#research-next').click();
 check('둘째 장에 검색어·동의 입력 표시 없음',!await page.locator('[name=public_search_query]').isVisible()&&!await page.locator('[name=public_search_consent]').isVisible());
 check('자동 검색 허용 기본 적용',await page.locator('[name=public_search_consent]').inputValue()==='true');
 await page.locator('#more-models').click();await page.locator('input[name=catalog-model][value=m]').check();await page.locator('#research-next').click();
 check('선택 검색어를 비워 둔 자동 연구',await page.locator('[name=public_search_query]').inputValue()===''&&await page.locator('[name=public_search_query]').evaluate(e=>!e.required&&e.closest('#research-advanced')!==null));
 check('근거 부족 중단 기본 꺼짐',!await page.locator('[name=search_required]').isChecked());
 await page.locator('[name=search_attempt_limit]').fill('0');await page.locator('#research-start').click();await page.waitForFunction(()=>document.querySelector('#research-preflight')?.textContent.includes('검색 횟수'));
 await page.locator('#research-preflight [data-fix=search]').click();
 check('검색 한도 누락은 고급 설정으로 안내',await page.locator('[name=search_attempt_limit]').isVisible()&&(await page.locator('#research-preflight').innerText()).includes('검색 횟수'));
 const blocked=await observed();check('시작 차단 전에 과금 요청 없음',blocked.model_calls.length===viewing.model_calls.length&&blocked.searches===viewing.searches);
 await page.locator('[name=search_attempt_limit]').fill('10');await page.locator('#research-start').click();await page.locator('#research-workspace').waitFor();
 check('CSV 없이 실제 실행 경로 완료',await page.locator('#run-state').innerText()===(weak?'설계안 완료':'완료'));
 if(weak){check('근거 부족 이유와 설계안 보기 제공',(await page.locator('#run-blocker').innerText()).includes('초록·원문')&&await page.locator('#repair-search').innerText()==='설계안 보기');await page.locator('#repair-search').click();await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');check('설계안 보기 버튼은 새 연구 양식을 만들지 않음',await page.locator('#research-form').count()===0);}
 await page.locator('[data-primary-tab=report]').click();await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');
 const completed=await observed();check('계획·보고서 두 모델 요청과 양 언어·확장 검색 실행',completed.model_calls.length===blocked.model_calls.length+2&&completed.searches===blocked.searches+(weak?4:5));
 if(weak){await page.locator('#pdf-page').fill('1');await page.locator('#pdf-page').dispatchEvent('change');await page.waitForFunction(()=>document.querySelector('#pdf-text')?.textContent.includes('정량'));await page.locator('#pdf-viewer details>summary').filter({hasText:'페이지 텍스트'}).click();const pdfText=(await page.locator('#pdf-text').innerText()).replace(/\s/g,'');save(path.join(folder,'design-pdf-text.json'),{page:Number(await page.locator('#pdf-page').inputValue()),text:pdfText});check('설계안 PDF는 정량 결과 미확인 표시',pdfText.includes('부분보고서')&&pdfText.includes('정량결론'));}
 violations.push(...await page.evaluate(()=>window.pdfViolations||[]));check('콘솔 실행 오류 없음',errors.length===0);check('CSP 위반 없음',violations.length===0);check('외부 요청 없음',external.length===0);
 }catch(e){errors.push(e.stack||e.message);process.exitCode=1;}
 finally{if(browser)await browser.close();if(child)child.kill();save(path.join(folder,'browser.json'),{checks,errors,violations,external,paid_calls:0,execution:'CHROME_OFFLINE_MOCK_PROVIDER'});console.log(JSON.stringify({folder,checks:checks.length,passed:checks.every(c=>c.passed)&&!errors.length,errors},null,2));}
})();
