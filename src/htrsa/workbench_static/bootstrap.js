'use strict';
// URL의 인증 값을 먼저 제거하고 기존 소유자 세션으로 전환한다.
window.showAuthFailure=function(){
 document.querySelector('.shell').hidden=true;
 const panel=document.querySelector('#auth-status');panel.hidden=false;
 panel.querySelector('h1').textContent='연결할 수 없습니다. H-TRSA를 다시 실행해 주세요.';
 document.body.dataset.authState='failed';
};
window.showAuthenticated=function(){
 document.querySelector('#auth-status').hidden=true;
 document.querySelector('.shell').hidden=false;
 document.body.dataset.authState='authenticated';
};
window.authenticateLocalBrowser=async()=>{
 let ticket=null;
 const bootstrap=location.hash.startsWith('#bootstrap=');
 if(bootstrap){
  ticket=location.hash.slice('#bootstrap='.length);
  history.replaceState(null,'',location.pathname);
  if(!/^[A-Za-z0-9_-]{43}$/.test(ticket)){ticket=null;return {authenticated:false};}
 }
 try{
  let pending;
  if(bootstrap){
   pending=fetch('/auth/bootstrap',{method:'POST',headers:{'Content-Type':'application/json','X-H-TRSA-Bootstrap':'1'},body:JSON.stringify({ticket}),credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer'});
   ticket=null;
  }else pending=fetch('/api/session',{credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer'});
  const response=await pending;
  if(response.ok){const metadata=await response.json();return {authenticated:true,csrf:metadata.csrf};}
  if(!bootstrap){
   const status=await fetch('/auth/status',{credentials:'same-origin',cache:'no-store',redirect:'error'});
   if(status.ok&&(await status.json()).manual_pairing)return {authenticated:false,manual_pairing:true};
  }
 }catch(_error){}finally{ticket=null;}
 return {authenticated:false};
};
window.htrsaAuthentication=window.authenticateLocalBrowser();
window.addEventListener('hashchange',()=>{
 if(!location.hash.startsWith('#bootstrap='))return;
 document.querySelector('.shell').hidden=true;
 document.querySelector('#auth-status').hidden=false;
 document.querySelector('#auth-status h1').textContent='H-TRSA에 연결하고 있습니다.';
 document.body.dataset.authState='connecting';
 window.dispatchEvent(new Event('htrsa-auth-start'));
 window.htrsaAuthentication=window.authenticateLocalBrowser();
 window.dispatchEvent(new CustomEvent('htrsa-auth-pending',{detail:window.htrsaAuthentication}));
});
