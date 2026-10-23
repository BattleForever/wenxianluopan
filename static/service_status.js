/* A closed browser does not stop the service; report disconnection without losing input. */
(()=>{'use strict';
 const banner=document.createElement('aside');banner.className='service-disconnected';banner.hidden=true;banner.setAttribute('role','status');
 banner.innerHTML='<span>后台服务未连接。请双击桌面“文献罗盘”重新打开。</span><button>重新连接</button>';document.body.append(banner);
 let checking=false,failures=0;
 async function check(manual=false){
  if(checking||window.WenxianServiceExiting)return;checking=true;
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),3000);
  try{const r=await fetch('/api/health',{cache:'no-store',signal:controller.signal});const h=await r.json();if(!r.ok||!h.ok)throw Error();failures=0;const disconnected=!banner.hidden;banner.hidden=true;if(manual&&disconnected)location.reload();}
  catch{if(++failures>=2||manual)banner.hidden=false;}
  finally{clearTimeout(timer);checking=false;}
 }
 banner.querySelector('button').onclick=()=>check(true);
 setInterval(()=>{if(!document.hidden)check();},15000);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)check();});
})();
