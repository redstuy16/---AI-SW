// 설치한 Playwright를 우선 사용하고 로컬 앱의 번들 경로를 보조로 사용한다.
const path=require('path'),os=require('os');
let runtime;
if(process.env.PROBE_PLAYWRIGHT_PATH)runtime=require(process.env.PROBE_PLAYWRIGHT_PATH);
else{try{runtime=require('playwright');}catch(e){if(e.code!=='MODULE_NOT_FOUND')throw e;runtime=require(path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));}}
module.exports=runtime;
