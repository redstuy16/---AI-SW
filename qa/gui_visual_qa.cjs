// 기존 번들 Playwright와 Chrome으로 실제 화면을 검사한다.
const fs=require('fs'),path=require('path'),{spawn}=require('child_process');
const {chromium}=require('./browser_runtime.cjs');
const root=path.resolve(__dirname,'..'),fixture=JSON.parse(fs.readFileSync(path.join(root,'build/gui_visual_fixture.json'),'utf8'));
const output=path.join(root,'output/playwright');fs.mkdirSync(output,{recursive:true});
const server=spawn(path.join(root,'.venv/Scripts/python.exe'),['-m','probe.workbench',fixture.database,fixture.workspace,'--mode','DEMO','--no-browser','--port','8876'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
let browser;
async function main(){
 const handoff=await new Promise((resolve,reject)=>{let text='';server.stdout.on('data',b=>{text+=b.toString();const m=text.match(/(http:\/\/127\.0\.0\.1:\d+\/#bootstrap=[A-Za-z0-9_-]{43})/);if(m)resolve(m[1]);});server.on('exit',c=>reject(Error('Server exit '+c)));setTimeout(()=>reject(Error('Server startup timeout')),15000).unref();});
 browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
 const errors=[],egress=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith('http://127.0.0.1:8876'))egress.push(r.url());});
 await page.goto(handoff);await page.locator('#research-table').waitFor();
 const shots=[],checks=[];
 async function capture(name){await page.screenshot({path:path.join(output,name+'.png'),fullPage:!(await page.locator('dialog[open]').count())});shots.push(name+'.png');const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2);checks.push({name,viewport:page.viewportSize(),primary_overflow:overflow});if(overflow)throw Error('Primary overflow '+name);}
 async function navigate(view){await page.locator(`[data-view="${view}"]`).click();await page.waitForFunction(()=>!document.querySelector('#content').textContent.includes('불러오는 중'));if(view==='settings'){await page.locator('#settings-tab-environment').waitFor();await page.locator('#settings-tab-environment').click();}}
 async function runTab(tab){await page.goto(`http://127.0.0.1:8876/#research/${fixture.research_id}/${tab}`);await page.locator('[data-tab="'+tab+'"][aria-current="page"]').waitFor();await page.locator('#run-content').waitFor();await page.waitForFunction(()=>document.querySelector('#run-content').textContent.length>10);}
 for(const viewport of [{width:1440,height:900},{width:390,height:844}]){
  await page.setViewportSize(viewport);const suffix=viewport.width===390?'mobile':'desktop';
  await navigate('research');await page.locator('#research-table').waitFor();await capture('01-research-'+suffix);
  await runTab('overview');await capture('02-overview-'+suffix);
  await runTab('timeline');await page.getByText('저장된 의사결정·도구·검증 기록입니다.',{exact:false}).waitFor();await capture('03-activity-'+suffix);
  await runTab('verification');await page.getByRole('button',{name:'복구 상세'}).waitFor();await capture('04-verification-'+suffix);
  await page.getByRole('button',{name:'복구 상세'}).first().click();await page.locator('#inspector[open]').waitFor();await capture('05-f3p-detail-'+suffix);await page.getByRole('button',{name:'상세 닫기'}).click();
  await navigate('settings');await page.locator('#settings-5').waitFor();await capture('06-settings-'+suffix);
  await navigate('usage');await page.locator('#usage-table').waitFor();await capture('07-usage-'+suffix);
  await navigate('library');await page.locator('#library-table').waitFor();await capture('08-library-'+suffix);
  await page.locator('[data-artifact-preview]').first().click();await page.locator('#artifact-preview').waitFor();await page.waitForFunction(()=>{const p=document.querySelector('#artifact-preview');return p?.complete&&p.naturalWidth>0;});await capture('09-artifact-'+suffix);await page.getByRole('button',{name:'상세 닫기'}).click();
 }
 for(const viewport of [{width:1280,height:720},{width:1024,height:768}]){await page.setViewportSize(viewport);await navigate('research');await page.locator('#research-table').waitFor();await capture('layout-'+viewport.width);}
 await page.setViewportSize({width:640,height:450});await navigate('settings');await page.locator('#settings-5').waitFor();await capture('layout-200-percent-equivalent');
 await page.keyboard.press('Tab');const focus=await page.evaluate(()=>({tag:document.activeElement.tagName,outline:getComputedStyle(document.activeElement).outlineStyle,visible:document.activeElement.getBoundingClientRect().width>0}));checks.push({name:'keyboard-focus',...focus});if(!focus.visible||focus.outline==='none')throw Error('Missing visible keyboard focus');
 await navigate('research');await page.getByRole('button',{name:'새 연구',exact:true}).first().click();await page.locator('#research-form').waitFor();const defaults=await page.locator('#research-form input[name="verified_analysis_skills"],#research-form input[name="verification_repair"],#research-form input[name="ridge_arithmetic_check"]').evaluateAll(nodes=>nodes.map(n=>n.checked));if(defaults.some(Boolean))throw Error('Experimental default enabled');if(!await page.locator('[name="adaptive_budget"]').isChecked())throw Error('Adaptive budget default missing');await page.keyboard.press('Escape');if(await page.locator('#editor').evaluate(e=>e.open))throw Error('Escape did not close dialog');
 // 공격 문자열을 텍스트로 표시하고 불필요한 외부 요청이 없는지 확인한다.
 const hostileTitle='<img src="https://evil.example/canary" onerror="window.__xss=1"> '+Date.now();
 const rendering=await page.evaluate(async title=>{const s=await(await fetch('/api/session')).json();const response=await fetch('/api/control/research',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf},body:JSON.stringify({title,question:'<script>window.__xss=1</script>',source_relative:'data.csv'})});return response.ok;},hostileTitle);
 if(!rendering)throw Error('Hostile rendering fixture create failed');await navigate('research');await page.getByRole('button',{name:hostileTitle,exact:true}).waitFor();if(await page.evaluate(()=>window.__xss))throw Error('Unsafe rendering');
 if(errors.length||egress.length)throw Error('Browser errors or egress '+JSON.stringify({errors,egress}));
 const luminance=hex=>{const rgb=hex.replace('#','').match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4);return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;};
 const contrast=Object.entries({body:['#232523','#f7f7f5'],muted:['#60645f','#f0f1ed'],primary:['#ffffff','#303c34'],good:['#285439','#edf3ed'],warning:['#795012','#faf4e8'],error:['#8e3232','#f8efef']}).map(([name,[fg,bg]])=>({name,ratio:(Math.max(luminance(fg),luminance(bg))+.05)/(Math.min(luminance(fg),luminance(bg))+.05)}));if(contrast.some(v=>v.ratio<4.5))throw Error('Text contrast below 4.5');
 const report={mode:'DEMO',live_validation:'NOT_VALIDATED',screenshots:shots.map(p=>path.join(output,p)),checks,contrast,errors,unexpected_network:egress,experimental_defaults_off:true,escape_close:true,unsafe_rendering_blocked:true,note:'200% reflow checked at half CSS viewport (640x450 for 1280x900); OS browser zoom is not claimed.'};
 const text=JSON.stringify(report,null,2)+'\n';if(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw Error('Invalid UTF-8 surrogate');fs.writeFileSync(path.join(root,'qa/results/gui_visual_results.json'),Buffer.from(text,'utf8'));console.log(JSON.stringify({screenshots:shots.length,checks:checks.length,errors:errors.length,unexpected_network:egress.length}));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill();});
