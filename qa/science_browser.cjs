// 실제 Chrome과 PDF.js에서 고교 과학의 모델 루프·도식·보고서를 확인한다.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),folder=path.join(root,'build/science/browser',crypto.randomUUID());
const out=path.join(root,'output/playwright/science');fs.mkdirSync(folder,{recursive:true});fs.mkdirSync(out,{recursive:true});
const checks=[],errors=[],external=[],violations=[];let browser,child,stderr='';
function save(file,value){const text=JSON.stringify(value,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('UTF-8');fs.writeFileSync(file,Buffer.from(text,'utf8'));}
function check(name,value){checks.push({name,passed:!!value});save(path.join(folder,'progress.json'),{checks,errors});if(!value)throw Error(name);}
async function renderedCanvas(page){await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));await page.waitForFunction(()=>{const c=document.querySelector('#pdf-canvas');if(document.querySelector('#pdf-viewer')?.dataset.ready!=='true'||!c?.width||!c.height)return false;const values=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=0;i<values.length;i+=32)if(values[i+3]>0&&values[i]+values[i+1]+values[i+2]<600)return true;return false;});}
(async()=>{try{
 const env={...process.env};for(const key of Object.keys(env))if(key.endsWith('API_KEY'))delete env[key];
 child=spawn(path.join(root,'.venv/Scripts/python.exe'),['-B','-X','utf8','qa/science_browser_fixture.py',folder],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
 child.stderr.on('data',v=>stderr+=v);
 const url=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',v=>{text+=v;const m=text.match(/http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43}/);if(m)resolve(m[0]);});child.on('exit',()=>reject(Error(stderr.slice(-3000))));setTimeout(()=>reject(Error('과학 검사 서버 시간 초과')),45000).unref();});
 const origin=new URL(url).origin,ids=JSON.parse(fs.readFileSync(path.join(folder,'fixture.json'),'utf8'));
 browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage({viewport:{width:1280,height:900}});page.setDefaultTimeout(20000);
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith(origin+'/')&&!r.url().startsWith('blob:')&&!r.url().startsWith('data:'))external.push(r.url());});
 await page.addInitScript(()=>document.addEventListener('securitypolicyviolation',e=>(window.scienceViolations??=[]).push(e.effectiveDirective)));
 await page.goto(url);await page.locator('#research-table').waitFor();const before=await page.evaluate(()=>api('/qa/observed'));
 check('원리 설명은 모델 한 번',before.calls[ids.principle]===1);
 check('설계 보완 후 완료는 모델 두 번',before.calls[ids.refine]===2);
 check('CSV 분석 후 완료는 모델 두 번',before.calls[ids.analysis]===2);
 check('모델의 완료 초안을 다시 작성하지 않음',Object.values(before.types).flat().every(v=>v==='ScienceDecision'));
 check('검증된 자료에서 실제 분석과 그림 도구 실행',before.tool_calls.some(v=>v.research_id===ids.analysis&&v.tool_name==='stats.run')&&before.tool_calls.some(v=>v.research_id===ids.analysis&&v.tool_name==='visualization.render'));
 const allText={};
 for(const scenario of ['principle','refine','analysis']){
  const rid=ids[scenario];await page.goto(origin+'/#research/'+rid+'/flow');await page.locator('#stage-map').waitFor();
  const view=await page.evaluate(r=>api('/api/control/research/'+r+'/report-view'),rid);
  const summary=await page.evaluate(r=>api('/api/control/research/'+r+'/execution-summary'),rid);
  check(scenario+' 완료 상태와 불필요 오류 없음',view.complete&&summary.blocker===null);
  await page.locator('[data-flow-category=experiment]').click();await page.locator('.science-visual').last().waitFor();
  check(scenario+' 의미 있는 두 도식',await page.locator('.science-visual').count()===2&&view.visual_specs.length===2);
  const visible=(await page.locator('#run-visuals').innerText()).replace(/\s/g,'');
  for(const spec of view.visual_specs){check(scenario+' '+spec.title+' 화면 캡션',visible.includes(spec.caption.replace(/\s/g,'')));for(const node of spec.nodes||[])check(scenario+' 화면 변인 '+node.role,visible.includes(node.name.replace(/\s/g,''))&&(!node.unit||visible.includes(node.unit.replace(/\s/g,''))));for(const edge of spec.edges||[])check(scenario+' 화면 관계 '+edge.label,visible.includes(edge.label.replace(/\s/g,'')));}
  if(scenario==='analysis'){check('모델 그림 참조 누락에도 검증된 그림 표시',view.images.length>0&&await page.locator('#run-visuals .result-figure').count()===view.images.length);for(const image of view.images)check('화면 검증 그림 캡션',visible.includes(image.caption.replace(/\s/g,'')));}
  await page.locator('[data-primary-tab=report]').click();await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');
  check(scenario+' PDF.js 실제 캔버스 렌더',await page.locator('#pdf-canvas').evaluate(e=>e.width>200&&e.height>200));
  const count=Number(await page.locator('#pdf-page').getAttribute('max'));let text='';
  for(let index=1;index<=count;index++){await page.locator('#pdf-page').fill(String(index));await page.locator('#pdf-page').dispatchEvent('change');await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');text+=' '+await page.locator('#pdf-text').textContent();}
  allText[scenario]=text;const normalized=text.replace(/\s/g,'');
  for(const spec of view.visual_specs){check(scenario+' '+spec.title+' PDF 제목·캡션',normalized.includes(spec.title.replace(/\s/g,''))&&normalized.includes(spec.caption.replace(/\s/g,'')));for(const node of spec.nodes||[])check(scenario+' PDF 변인 '+node.role,normalized.includes(node.name.replace(/\s/g,''))&&(!node.unit||normalized.includes(node.unit.replace(/\s/g,''))));for(const step of spec.steps||[])check(scenario+' PDF 절차',normalized.includes(step.replace(/\s/g,'')));for(const edge of spec.edges||[])check(scenario+' PDF 관계',normalized.includes(edge.label.replace(/\s/g,'')));}
  for(const image of view.images)check(scenario+' PDF 검증 그림 캡션',normalized.includes(image.caption.replace(/\s/g,'')));
  await page.locator('#pdf-page').fill('1');await page.locator('#pdf-page').dispatchEvent('change');await page.waitForFunction(()=>document.querySelector('#pdf-viewer')?.dataset.ready==='true');
  await page.screenshot({path:path.join(out,scenario+'-pdf.png'),fullPage:true});
 }
 save(path.join(folder,'pdf-text.json'),allText);
 for(const [name,width,zoom] of [['desktop',1280,1],['mobile390',390,1],['zoom200',1280,2]]){await page.setViewportSize({width,height:900});await page.evaluate(z=>document.documentElement.style.zoom=String(z),zoom);await page.locator('#pdf-fit').click();await renderedCanvas(page);check(name+' PDF 픽셀 실제 표시',true);check(name+' PDF 가로 넘침 없음',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});}
 await page.evaluate(()=>document.documentElement.style.zoom='1');await page.setViewportSize({width:1280,height:900});
 await page.locator('#run-back').click();await page.locator('#list-new').click();await page.locator('#research-form').waitFor();
 check('불필요한 대상·분야 선택 제거',await page.locator('[name=audience],[name=science_field]').count()===0&&await page.locator('#research-form').getAttribute('data-page')==='0');
 check('첫 장에서 주제·질문 입력 가능',await page.locator('[name=title]').isVisible()&&await page.locator('[name=question]').isVisible());
 check('무료 검색 엔진 설정 제거',await page.locator('[name=free_search_enabled],[name=searxng_url],[name=free_search_order]').count()===0);
 await page.locator('[name=question]').fill('빛의 세기와 광합성 원리를 설명해 주세요.');await page.locator('#research-next').click();
 await page.locator('#more-models').click();await page.locator('input[name=catalog-model][value=m]').check();await page.locator('#research-next').click();await page.locator('#research-start').click();await page.locator('#research-workspace').waitFor();
 const after=await page.evaluate(()=>api('/qa/observed')),newIds=Object.keys(after.calls).filter(r=>!Object.hasOwn(before.calls,r));
 check('새 과학 양식도 실행기를 통해 모델 한 번 완료',newIds.length===1&&after.calls[newIds[0]]===1&&after.runs.find(v=>v.research_id===newIds[0]).status==='COMPLETED');
 check('보고서 조회·페이지 조작은 추가 모델 요청 없음',Object.entries(before.calls).every(([rid,count])=>after.calls[rid]===count));
 check('유료 호출·검색·외부 HTTP 없음',after.paid_calls===0&&after.searches===0&&after.external_attempts.length===0);
 violations.push(...await page.evaluate(()=>window.scienceViolations||[]));check('브라우저 실행 오류 없음',errors.length===0);check('CSP 위반 없음',violations.length===0);check('브라우저 외부 요청 없음',external.length===0);
 }catch(error){errors.push(error.stack||error.message);process.exitCode=1;if(browser){const pages=browser.contexts().flatMap(v=>v.pages());if(pages.length)await pages[0].screenshot({path:path.join(folder,'failure.png'),fullPage:true}).catch(()=>{});}}
 finally{if(browser)await browser.close();if(child)child.kill();save(path.join(folder,'browser.json'),{checks,errors,violations,external,fixture_stderr:stderr,paid_calls:0,execution:'CHROME_OFFLINE_FAKE_SCIENCE_DECISION'});console.log(JSON.stringify({folder,checks:checks.length,passed:checks.every(v=>v.passed)&&errors.length===0,errors,fixture_stderr:stderr.slice(-3000)},null,2));}
})();
