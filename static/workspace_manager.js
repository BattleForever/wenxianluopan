'use strict';
(() => {
 const dialog=document.createElement('dialog');dialog.id='workspace-manager';dialog.className='workspace-manager';
 dialog.innerHTML=`<header><div><span class="eyebrow">WORKSPACE</span><h2>工作空间</h2></div><button class="icon-button" data-wm-close aria-label="关闭工作空间">×</button></header>
 <div class="wm-current"><span>当前数据目录</span><code id="wm-directory">正在读取…</code><button class="secondary small" data-wm="open-folder">打开文件夹 ↗</button></div>
 <div class="wm-cards"><section><h3>打包备份</h3><p>保存文献、分类、阅读备注、AI 阅读成果和已关联原文，可带到另一台电脑继续使用。</p><button class="primary" data-wm="backup">下载工作空间备份</button><small>备份含私人阅读资料，请妥善保管。本机模型与数据源连接配置不打包。</small></section>
 <section><h3>迁移与导入</h3><p>选择新的空文件夹。切换后自动重启，原工作空间保留。</p><label>目标数据目录<div class="wm-destination"><input id="wm-target" placeholder="选择空文件夹，或填写完整路径" autocomplete="off"><button class="secondary small" data-wm="choose-folder">选择文件夹</button></div></label><div class="wm-transfer"><button class="secondary" data-wm="migrate">迁移当前工作空间</button><button class="secondary" data-wm="restore">从备份导入</button><input id="wm-file" type="file" accept=".zip" hidden></div><small>迁移保留本机连接配置；导入备份后可重新配置 AI。两种操作均不覆盖现有目录。</small></section></div>
 <p id="wm-status" role="status" aria-live="polite"></p><footer><span>关闭网页后后台仍运行；点击退出才会停止服务。</span><button class="wm-exit" data-wm="exit">退出软件</button></footer>`;
 document.body.append(dialog);
 let info=null,working=false;
 function status(text,error=false){const el=document.getElementById('wm-status');el.textContent=text;el.classList.toggle('error',error);}
 async function readState(){const r=await fetch('/api/workspace/state');if(!r.ok)throw Error('工作空间信息读取失败，请刷新页面重试。');info=await r.json();document.getElementById('wm-directory').textContent=info.directory;dialog.querySelector('[data-wm="exit"]').disabled=!info.can_exit;return info;}
 async function request(action,body){const r=await fetch('/api/workspace/'+action,{method:'POST',headers:{'X-Workspace-Token':info.token,...(body!==undefined?{'Content-Type':'application/json'}:{})},body:body===undefined?undefined:JSON.stringify(body)});if(!r.ok){let data=await r.json();throw Error(data.detail||'操作失败，请重试。');}return r;}
 async function show(){dialog.showModal();status('');try{await readState();}catch(e){status(e.message,true);}}
 function lock(value){working=value;dialog.querySelectorAll('button,input').forEach(el=>el.disabled=value);if(!value&&info&&!info.can_exit)dialog.querySelector('[data-wm="exit"]').disabled=true;dialog.setAttribute('aria-busy',String(value));}
 async function run(action){
  if(working)return;
  if(['migrate','restore'].includes(action)){
   const target=document.getElementById('wm-target').value.trim();if(!target){status('请先选择目标空文件夹。',true);return;}
   if(!info.can_exit){status('请使用启动器启动软件后再切换工作空间。',true);return;}
   if(action==='restore'){document.getElementById('wm-file').click();return;}
   if(!confirm('将当前工作空间复制到新目录并重启软件。原目录及原文仍保留。是否继续？'))return;
  }
  if(action==='exit'&&!confirm('退出文献罗盘并停止后台服务？再次双击桌面图标或 start.bat 即可打开。'))return;
  lock(true);
  try{
   status(({backup:'正在整理原文并生成备份，请稍候…',migrate:'正在复制并校验工作空间，请勿关闭…',exit:'正在停止后台服务…'})[action]||'正在处理…');
   if(action==='choose-folder'){const data=await (await request(action)).json();if(data.path)document.getElementById('wm-target').value=data.path;status('');}
   else if(action==='backup'){
    const r=await request(action),blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='文献罗盘备份_'+new Date().toISOString().slice(0,10)+'.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
    const missing=Number(r.headers.get('X-Missing-PDFs')||0);status('备份已生成。'+(missing?'有 '+missing+' 篇原文在本机已失效，书目与已有解读仍保留。':'已关联的可用原文已一并打包。'));
   }else if(action==='migrate'){
    const data=await (await request(action,{path:document.getElementById('wm-target').value.trim()})).json();status('迁移完成，正在重启。就绪后将自动打开新页面；若未打开，请再次双击桌面图标。'+(data.info.missing_pdf_ids?.length?'部分旧原文链接失效，可在新工作空间重新关联。':''));return;
   }else if(action==='exit'){await request(action);status('已退出软件，后台服务即将停止。可以关闭此网页。');document.title='文献罗盘 · 已退出';return;}
   else{await request(action);status('已打开当前数据文件夹。');}
  }catch(e){status(e.message,true);}
  lock(false);
 }
 dialog.querySelector('[data-wm-close]').onclick=()=>{if(!working)dialog.close();};dialog.addEventListener('cancel',e=>{if(working)e.preventDefault();});
 dialog.addEventListener('click',e=>{const b=e.target.closest('[data-wm]');if(b)run(b.dataset.wm);});
 document.getElementById('wm-file').onchange=async e=>{const file=e.target.files[0];if(!file)return;const target=document.getElementById('wm-target').value.trim();if(!confirm('将备份导入新目录并切换工作空间。当前数据保留，不合并、不覆盖。是否继续？')){e.target.value='';return;}lock(true);status('正在上传、校验并导入备份，请勿关闭…');try{const r=await fetch('/api/workspace/restore?path='+encodeURIComponent(target),{method:'POST',headers:{'X-Workspace-Token':info.token,'Content-Type':'application/zip'},body:file});const data=await r.json();if(!r.ok)throw Error(data.detail||'导入失败');status('已导入 '+data.info.papers+' 篇文献，正在重启。就绪后将自动打开新页面；若未打开，请再次双击桌面图标。');}catch(error){status(error.message,true);lock(false);}e.target.value='';};
 document.querySelectorAll('[data-workspace-manager]').forEach(el=>el.addEventListener('click',show));
})();
