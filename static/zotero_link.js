/* Explicit local authorization and collection destination. */
(()=>{'use strict';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let pid=null,epoch=0,model=null,authTimer=null;const syncing=new Set();
const dialog=document.createElement('dialog');dialog.className='organizer-dialog zotero-link-dialog';dialog.id='zotero-link-dialog';document.body.append(dialog);
async function api(path,method='GET',body){const r=await fetch('/api/zotero-link'+path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'连接未完成，请重试');return d;}
function note(text,error=false){const n=dialog.querySelector('[data-zotero-note]');if(n){n.textContent=text;n.classList.toggle('error',error);}}
async function open(id){const token=++epoch;pid=id;dialog.innerHTML='<header><h2>同步到 Zotero</h2><button class="icon-button" data-zotero-close aria-label="关闭同步窗口">×</button></header><p data-zotero-note>正在读取连接和同步位置…</p>';if(!dialog.open)dialog.showModal();try{
 const state=await api('/status/'+id);let rows=[];try{rows=(await api('/collections')).collections;}catch(e){}
 if(token!==epoch||!dialog.open)return;model=state;const record=state.record,dest=state.destination;
 dialog.innerHTML=`<header><div><span class="eyebrow">文献管理</span><h2>同步到 Zotero</h2></div><button class="icon-button" data-zotero-close aria-label="关闭同步窗口">×</button></header><div class="zotero-connection"><span class="zotero-led ${state.available?'online':''}"></span><div><strong>${state.available?state.authorized?'已连接并授权':'等待授权':'未连接 Zotero'}</strong><small>${state.available?'选择目标集合后同步书目与阅读内容':'请启动桌面端，并在设置中允许本机应用连接'}</small></div><button class="secondary small" data-zotero-authorize>${state.authorized?'重新授权':'连接 Zotero'}</button></div>${dest?`<div class="zotero-current"><small>当前同步位置</small><strong>${esc(dest.path)}</strong><span>${record?.zotero_state==='synced'?'已核验':'待重新核验'}</span></div>`:''}<label class="zotero-target">目标集合<select id="zotero-target"><option value="">我的文献库（根目录）</option>${rows.map(r=>`<option value="${esc(r.key)}">${esc(r.path)}</option>`).join('')}</select></label>${dest?'<label class="zotero-move"><input type="checkbox" id="zotero-move" checked>迁移本应用此前的同步位置<small>保留其他集合归属、原笔记和附件</small></label>':''}<p data-zotero-note role="status">${esc(record?.last_error?'上次同步未完成，可重新连接后重试。':'书目与阅读内容写入后会回读核对。')}</p><footer><button class="secondary" data-zotero-refresh>刷新集合</button><button class="primary" data-zotero-sync ${syncing.has(id)?'disabled':''}>${dest?'同步／迁移到此位置':'同步到所选位置'}</button></footer>`;
 dialog.querySelector('#zotero-target').value=dest?.collection_key||'';return true;
 }catch(e){if(token===epoch)note(e.message,true);}}
async function watch(jid,token){try{const j=await api('/authorize/'+jid);if(token!==epoch||!dialog.open)return;note(j.message);if(j.state==='waiting')authTimer=setTimeout(()=>watch(jid,token),1200);else if(j.state==='authorized')await open(pid);else dialog.querySelector('[data-zotero-authorize]').disabled=false;}catch(e){note(e.message,true);}}
dialog.addEventListener('close',()=>{if(dialog.open)return;++epoch;clearTimeout(authTimer);});
dialog.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{
 if(b.hasAttribute('data-zotero-close'))dialog.close();
 if(b.hasAttribute('data-zotero-refresh'))await open(pid);
 if(b.hasAttribute('data-zotero-authorize')){const token=epoch;b.disabled=true;const j=await api('/authorize','POST',{});if(token!==epoch||!dialog.open)return;note(j.message);watch(j.id,token);}
 if(b.hasAttribute('data-zotero-sync')){const id=pid,token=epoch;syncing.add(id);b.disabled=true;note('正在写入并核对，请稍候…');try{const r=await api('/sync','POST',{paper_id:id,collection_key:dialog.querySelector('#zotero-target').value||null,move:dialog.querySelector('#zotero-move')?.checked||false});window.dispatchEvent(new Event('wenxian-data-updated'));if(token!==epoch||!dialog.open)return;syncing.delete(id);if(await open(id))note(r.message,r.zotero_state!=='synced');}catch(err){if(token===epoch&&dialog.open)note(err.message,true);}finally{syncing.delete(id);if(pid===id&&dialog.open){const current=dialog.querySelector('[data-zotero-sync]');if(current)current.disabled=false;}}}

 }catch(err){note(err.message,true);b.disabled=false;}});
async function decorate(){const paper=window.WenxianWorkbench?.detail;if(!paper)return;try{const d=await api('/status/'+paper.id);if(window.WenxianWorkbench.detail?.id!==paper.id)return;const b=document.getElementById('detail-zflush');if(b&&d.destination){const label='Zotero · '+d.destination.path;if(b.textContent!==label)b.textContent=label;b.title='当前同步路径：'+d.destination.path+'；点击重新选择或迁移';}}catch(e){}}
new MutationObserver(decorate).observe(document.getElementById('detail-content'),{childList:true});
window.WenxianZotero={open};
})();
