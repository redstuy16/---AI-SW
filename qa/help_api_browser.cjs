// 실제 Chrome에서 단가 적용·연결 확인을 모의 제공사로 검사한다.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/help-api/browser',crypto.randomUUID()),out=path.join(root,'output/playwright/help-api');
fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(out,{recursive:true});
const checks=[],errors=[],shots=[];let child,browser,page;
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8 검사 실패');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,value){checks.push({name,passed:!!value});if(!value)throw Error(name);}
async function main(){
 const env={...process.env};for(const key of Object.keys(env))if(key.endsWith('API_KEY'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/help_api_browser_fixture.py',folder,'--research'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env});
 let stderr='';child.stderr.on('data',value=>stderr+=value);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',value=>{text+=value;const match=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(match)resolve(match[0]);});child.on('exit',()=>reject(Error(stderr.slice(-1200))));setTimeout(()=>reject(Error('서버 시간 초과')),20000).unref();});
 const origin=new URL(url).origin;
 browser=await chromium.launch({channel:'chrome',headless:true});page=await browser.newPage({viewport:{width:1280,height:900}});page.setDefaultTimeout(16000);
 page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(!request.url().startsWith(origin+'/')&&!request.url().startsWith('blob:'))errors.push('외부 요청');});
 const state=()=>page.evaluate(()=>api('/qa/state'));
 const openCheck=async()=>{await page.locator('[data-view=settings]').click();await page.locator('#settings-tab-connections').click();await page.locator('[data-connection-check=qa-openai]').click();await page.locator('#check-price-only').waitFor();};
 await page.goto(url);await page.locator('#research-table').waitFor();await openCheck();
 const before=await state();check('등록된 키의 누락 단가를 읽기 전용으로 감지',before.keys===1&&before.price===null&&before.requests===0&&before.provider_calls===0);
 check('모델 선택과 공식 단가·출처 표시',await page.locator('[name=profile]').innerText()==='GPT-6 Luna'&&await page.locator('#connection-price').innerText().then(t=>t.includes('$0.1')&&t.includes('$0.5'))&&await page.locator('#connection-price a').getAttribute('href').then(t=>t.startsWith('https://developers.openai.com/')));
 check('명시적인 단가 적용과 지출 상한 표시',await page.locator('#check-ai').innerText()==='단가 적용 후 AI 연결 확인 · 최대 $0.10');
 for(const width of [390,1280]){await page.setViewportSize({width,height:900});check('연결 확인 '+width+'px 가로 넘침 없음',await page.locator('#editor').evaluate(el=>el.scrollWidth<=el.clientWidth+2));check('연결 확인 '+width+'px 버튼 간격',await page.locator('#connection-check .inline-actions').evaluate(el=>parseFloat(getComputedStyle(el).gap)>=8));const file=path.join(out,'price-'+width+'.png');await page.screenshot({path:file,fullPage:true});shots.push(path.relative(root,file));}
 await page.locator('#check-price-only').click();await page.locator('#check-price-only').waitFor({state:'detached'});
 const priced=await state();check('단가만 적용은 API·원장·연구 기록 생성 없음',priced.price.owner_verified&&priced.requests===0&&priced.provider_calls===0&&priced.researches===0&&priced.agents===0);
 check('단가 적용 완료는 초록 알림',await page.locator('#notice[data-status=success]').isVisible());
 await page.locator('#check-ai').click();await page.waitForFunction(()=>document.querySelector('#connection-check-result').textContent==='AI 응답 확인 완료');
 const first=await state();check('실제 앱 호출 경로의 모의 응답·정산 통과',first.provider_calls===1&&first.requests===1&&first.spent==='0.00001');
 check('단가 승인 뒤 같은 화면에서 API 응답 확인 가능',await page.locator('#notice[data-status=success]').innerText()==='AI 응답을 확인했습니다.');
 await page.locator('#check-ai').click();await page.waitForFunction(()=>document.querySelector('#connection-check-result').textContent==='AI 응답 확인 완료');
 check('반복 연결 확인은 오래된 단가 승인 오류 없음',(await state()).provider_calls===2);
 await page.locator('#check-model-details').click();await page.locator('#model-form').waitFor();check('상세 설정 재열기에서 기존 단가 승인 보존',await page.locator('[name=price_verified]').isChecked());
 await page.locator('#editor-close').click();await page.evaluate(()=>api('/qa/reset-price',{}));await page.evaluate(()=>{app.settings=null;});await openCheck();
 await page.locator('#check-ai').click();await page.waitForFunction(()=>document.querySelector('#connection-check-result').textContent==='AI 응답 확인 완료');
 const combined=await state();check('단가 적용 후 연결 확인 버튼은 승인·단일 요청 순서',combined.price.owner_verified&&combined.provider_calls===3&&combined.requests===3);
 check('결합 버튼 완료 뒤 표시 갱신',await page.locator('#check-ai').innerText()==='AI 연결 확인 · 최대 $0.10'&&await page.locator('#check-price-only').count()===0);
 await page.locator('#check-ai').click();await page.waitForFunction(()=>document.querySelector('#connection-check-result').textContent==='AI 응답 확인 완료');check('결합 버튼 뒤 반복 검사도 정상',(await state()).provider_calls===4);
 const checked=await state();check('검사 과정은 연구·Agent 생성 없음',checked.researches===0&&checked.agents===0);
 await page.locator('#editor-close').click();await page.locator('[data-view=research]').click();await page.locator('#list-new').click();
 await page.locator('[name=question]').fill('공개 자료의 관계를 검토해 주세요.');await page.locator('[name=title]').fill('단가 수정 후 실행');
 await page.locator('#research-next').click();await page.locator('#research-next').click();
 await page.evaluate(()=>api('/qa/reset-price',{}));await page.locator('#research-start').click();await page.locator('[data-apply-research-price]').waitFor();
 const blocked=await state();check('연구 시작의 단가 누락은 모델별 적용 버튼으로 해결',blocked.provider_calls===4&&blocked.researches===0&&await page.locator('.research-price strong').innerText()==='GPT-6 Luna');
 check('단가 오류 중복 설명 제거',await page.locator('#research-preflight').innerText().then(t=>!t.includes('안전한 비용 상한')));
 await page.locator('[data-apply-research-price]').click();await page.getByText('시작 준비 완료',{exact:true}).waitFor();
 const prepared=await state();check('연구 화면 단가 적용은 유료 요청·연구 생성 없음',prepared.provider_calls===4&&prepared.researches===0);
 check('단가 적용 후 질문·제목·예산 보존',await page.locator('[name=question]').inputValue()==='공개 자료의 관계를 검토해 주세요.'&&await page.locator('[name=title]').inputValue()==='단가 수정 후 실행'&&await page.locator('[name=run_limit_usd]').inputValue()==='0.1');
 await page.locator('#research-next').click();await page.locator('#research-start').click();await page.locator('#run-content').waitFor();
 const started=await state();check('유료 모델 경로에서 실제 연구 실행·모의 API 정산',started.provider_calls>4&&started.researches===1&&started.agents>0&&started.runs.every(r=>r.status!=='FAILED'&&r.status!=='STARTING'));
 await page.locator('#run-back').click();await page.locator('#list-new').click();await page.locator('#research-input-mode').waitFor();await page.locator('[name=question]').fill('공개 자료의 관계를 검토해 주세요.');
 await page.locator('#research-next').click();await page.locator('#research-form[data-page="1"]').waitFor();await page.locator('#research-next').click();await page.locator('#research-form[data-page="2"]').waitFor();await page.evaluate(()=>api('/qa/reset-price',{}));
 await page.locator('#research-smoke').click();await page.locator('[data-apply-research-price]').waitFor();check('연구 화면 연결 검사도 누락 단가부터 처리',(await state()).provider_calls===started.provider_calls);
 await page.locator('[data-apply-research-price]').click();await page.getByText('시작 준비 완료',{exact:true}).waitFor();await page.locator('#research-next').click();
 await page.locator('#research-smoke').click();await page.locator('#notice[data-status=success]').filter({hasText:'AI 응답을 확인했습니다.'}).waitFor();
 check('연구 화면 연결 응답 후 시작 준비 상태 갱신',await page.getByText('시작 준비 완료',{exact:true}).isVisible()&&(await state()).provider_calls===started.provider_calls+1);
 await page.locator('#research-start').click();await page.locator('#run-content').waitFor();
 const final=await state();check('연결 검사 뒤에도 같은 초안으로 연구 실행',final.researches===2&&final.provider_calls>started.provider_calls+1&&final.runs.every(r=>r.status!=='FAILED'&&r.status!=='STARTING'));
 check('실제 과금·외부 요청·브라우저 오류 없음',final.paid_calls===0&&errors.length===0);
 save(path.join(folder,'result.json'),{passed:true,execution:'REAL_CHROME_MOCK_PROVIDER',paid_calls:0,checks,errors,shots,provider_calls:final.provider_calls,mock_spent:final.spent,folder:path.relative(root,folder)});
 process.stdout.write(JSON.stringify({passed:true,checks:checks.length,paid_calls:0,provider_calls:final.provider_calls,folder:path.relative(root,folder)})+'\n');
}
main().catch(async error=>{const diagnostic=page?await page.evaluate(()=>({notice:document.querySelector('#notice')?.textContent,dialog:document.querySelector('#editor')?.innerText})).catch(()=>null):null;save(path.join(folder,'result.json'),{passed:false,checks,errors,error:error.message,diagnostic});process.stderr.write(error.stack+'\n');process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(child)child.kill();});
