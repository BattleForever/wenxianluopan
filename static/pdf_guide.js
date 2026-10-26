/* Optional PDF reading. All model text is rendered as text, never executable HTML. */
(()=>{'use strict';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let pdfMessage='',fileToolsOpen=false,pdfSource=null,pdfBusy=false,pid=null,current=null,tab='overview',timer=null,returnFocus=null,epoch=0,controller=new AbortController();
const dialog=document.createElement('dialog');dialog.id='pdf-guide-dialog';dialog.className='pg-dialog';dialog.setAttribute('aria-label','AI 全文解读');document.body.append(dialog);
async function request(url,method='GET'){let r=await fetch(url,{method,signal:controller.signal});let data=await window.WenxianHTTP.read(r,'本次操作未完成，请重试。');if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'请求失败，请稍后重试');return data;}
const url=()=>'/api/papers/'+pid+'/pdf-guide';
const ev=(e,figure=false)=>`<button class="pg-evidence" data-evidence="${e.page}" data-quote="${esc(e.quote)}" data-figure="${figure?'1':'0'}">原文依据 · PDF 第 ${e.page} 页 ↗</button>`;
function sourceChoice(){
 const ready=!!pdfSource?.ready,pending=ready&&pdfSource.saved_to==='cache'&&pdfSource.storage_mode==='zotero',checking=!pdfSource;
 const place=pending?'本机暂存 · 待保存到 Zotero':pdfSource?.saved_to==='zotero'?'已保存到 Zotero':ready?'已关联本机原文':'';
 const state=checking?'正在检查':ready?(pdfSource.reading_ready===false?'需处理原文':'原文已就绪'):'尚未关联原文';
 const upload=`<label class="secondary pg-upload-label">${ready?'更换 PDF':'上传 PDF'}<input type="file" accept="application/pdf,.pdf" data-pdf-upload ${pdfBusy?'disabled':''} hidden></label>`;
 return `<section class="pg-file-card" aria-label="论文原文"><div class="pg-file-heading"><div><h4>论文原文 <span class="pg-file-badge ${ready?'ready':''}">${state}</span></h4>${ready?`<span class="pg-file-name" title="${esc(pdfSource.filename)}">${esc(pdfSource.filename||'已关联的论文原文')}</span><span class="pg-file-meta">${Number(pdfSource.pages)||'—'} 页${place?' · '+place:''}</span>`:`<span class="pg-file-meta">${checking?'正在核对已有关联':pdfSource?.has_public_link?'已发现公开全文链接，可直接获取':'查找已有附件或公开全文，也可上传本机文件'}</span>`}</div><div class="pg-file-quick">${pending?`<button class="secondary" data-pdf-save ${pdfBusy?'disabled':''}>保存到 Zotero</button>`:''}${ready&&pdfSource.can_open_folder?'<button class="secondary" data-pdf-folder>打开文件夹 ↗</button>':''}</div></div>${!ready&&!checking?`<div class="pg-file-actions"><button class="primary" data-pdf-find ${pdfBusy?'disabled':''}>${pdfBusy?'正在获取…':'获取论文原文'}</button>${upload}</div>`:''}${pdfSource?.reading_issue?`<p class="pg-file-warning">${esc(pdfSource.reading_issue)}</p>`:''}${pdfMessage?`<p class="pg-source-status" role="status">${esc(pdfMessage)}</p>`:''}<details class="pg-file-tools" data-pg-files ${fileToolsOpen?'open':''}><summary>${ready?'管理原文':'其他获取方式'} <span aria-hidden="true">⌄</span></summary><div class="pg-file-tools-body"><div class="pg-file-actions">${ready?`${upload}<button class="secondary" data-pdf-find ${pdfBusy?'disabled':''}>重新查找</button>`:''}<button class="secondary" data-pdf-browser ${pdfBusy?'disabled':''}>浏览器获取</button>${pdfSource?.doi_url?`<a href="${esc(pdfSource.doi_url)}" target="_blank" rel="noopener">出版商页面 ↗</a>`:''}</div>${pdfSource?.saved_directory?`<div class="pg-file-location"><span>文件位置</span><code>${esc(pdfSource.saved_directory)}</code></div>`:''}</div></details></section>`;
}
function generateControl(g,active){return `<div class="pg-generate-actions"><button class="${g?'secondary':'primary'} pg-generate" data-pg-generate ${active||!pdfSource?.ready||pdfSource?.reading_ready===false?'disabled':''}>${active?'正在生成全文解读…':g?'重新生成解读':'生成全文解读'}${active?'':' <span aria-hidden="true">→</span>'}</button>${!g&&!active?'<span>逐节导读 · 图表说明 · 原文依据</span>':''}</div>`;}

function render(preserve=true){
 const scroll=preserve?(dialog.querySelector('.pg-main')?.scrollTop||0):0;
 const g=current?.guide,active=current?.status==='generating';
 dialog.innerHTML=`<header class="pg-header"><div><span class="pg-kicker">AI 全文解读</span><h2>${esc(g?.title_zh||'论文精读')}</h2></div><div class="pg-tools">${g?'<button class="secondary small" data-pg-export>导出解读</button>':''}<button class="icon-button" data-pg-close aria-label="关闭全文解读">×</button></div></header><div class="pg-body"><nav class="pg-nav" aria-label="全文解读目录">${[['overview','概览与中文解读'],['sections','逐节导读'],['figures','图表说明'],['conclusion','结论与局限']].map(([key,name])=>`<button data-pg-tab="${key}" class="${tab===key?'active':''}">${name}</button>`).join('')}<small>${g?`${current.pages} 页原文<br>${esc(current.created_at?.slice(0,10))}`:'先关联原文<br>再生成全文解读'}<br><br>AI 解读请结合原文核对</small></nav><article class="pg-main"></article></div>`;
 const main=dialog.querySelector('.pg-main');
 if(!g){main.innerHTML=`<div class="pg-start"><div class="pg-label">${active?'AI 正在阅读原文':pdfSource?.ready?'原文已关联':'准备论文原文'}</div><h3>${active?'正在生成全文解读':pdfSource?.ready?(pdfSource.reading_ready===false?'原文需要处理':'开始全文解读'):pdfSource?'添加论文原文':'正在检查原文'}</h3><p class="pg-start-description">${pdfSource?.ready?'按章节理解研究思路、方法与结论，结合图表核对原文。':'关联原文后，即可生成逐节导读与图表说明。'}</p>${pdfSource?.ready||active?generateControl(g,active):''}${active?'<div class="pg-loading"></div><p class="pg-generation-status" role="status">正在处理，关闭窗口后可稍后回来查看。</p>':''}${current?.error?`<p class="pg-error" role="alert">${esc(current.error.message)}</p>`:''}${sourceChoice()}</div>`;return;}

 main.innerHTML=`<div class="pg-reading-actions"><span>${current.stale?'原文或配置已变化':'解读已保存'}</span>${generateControl(g,active)}</div>${current.stale?`<div class="pg-notice">历史导读：${esc(current.stale_reasons.join('；'))}。请重新生成后再使用。</div>`:''}${active?'<div class="pg-loading"></div>':''}${current.error?`<div class="pg-notice pg-error" role="alert">${esc(current.error.message)}</div>`:''}${sourceChoice()}`;
 if(tab==='overview')main.innerHTML+=`<div class="pg-label">${current.stale?'历史结果':'已保存的 AI 解读'} · 依据 PDF 正文与图注</div><h3>${esc(g.title_zh)}</h3><p>${esc(g.overview)}</p><div class="pg-card"><h4>作者单位</h4><p>${esc(g.institutions)}</p></div>${g.coverage?`<details class="pg-notice"><summary>章节与图表覆盖</summary>版面清单覆盖：${g.coverage.sections_covered}/${g.coverage.sections_detected} 个标题、${g.coverage.figures_covered}/${g.coverage.figures_detected} 张主图。${esc(g.coverage.note)}</details>`:''}<details class="pg-notice"><summary>阅读说明</summary>中文题名为翻译，正文为概括与解释。引用校验仅确认片段可定位，不等于概括的语义完全正确。图表解释依据正文和图注，仍需对照原图。${current.validity==='unverifiable'?' 当前模型配置无法比较，结果仍可作为历史资料阅读。':''}</details><details class="pg-meta"><summary>来源与生成信息</summary>原始题名：${esc(current.source_title)}<br>来源：${current.pages} 页真实 PDF · 未省略超限内容冒充全文<br>来源校验：${esc(current.source_hash.slice(0,16))}…</details>`;
 if(tab==='sections')main.innerHTML+='<h3>沿着论文结构理解论证</h3>'+g.sections.map((s,i)=>`<section class="pg-card"><div class="pg-label">SECTION ${String(i+1).padStart(2,'0')}</div><h4>${esc(s.heading)}</h4><p>${esc(s.explanation)}</p>${ev(s.evidence)}</section>`).join('');
 if(tab==='figures')main.innerHTML+='<h3>图表在论文中证明了什么</h3>'+g.figures.map(f=>`<section class="pg-card"><div class="pg-label">FIGURE ${f.number}</div><h4>${esc(f.caption.split("。")[0])}</h4><details class="pg-caption"><summary>查看中文图注概括</summary><p>${esc(f.caption)}</p></details>${f.panels.map(p=>`<div class="pg-panel-label">${esc(p.label)}</div><p>${esc(p.explanation)}</p>`).join('')}${ev(f.evidence,true)}</section>`).join('');
 if(tab==='conclusion')main.innerHTML+=`<h3>结论与局限</h3><div class="pg-card"><p>${esc(g.conclusion.text)}</p>${ev(g.conclusion.evidence)}</div><div class="pg-notice">区分论文报告、AI 解释和自己的研究判断。未在原文中验证的应用范围不能作为本文已经证明的结论。</div>`;
 main.scrollTop=scroll;
}
async function refresh(token=epoch){try{const data=await request(url());if(token!==epoch||!dialog.open)return;if(JSON.stringify(current)!==JSON.stringify(data)){current=data;render();}if(current.status==='generating')timer=setTimeout(()=>refresh(token),2000);}catch(e){if(e.name==='AbortError'||token!==epoch)return;current={...current,error:{message:e.message}};render();}}
async function open(id){clearTimeout(timer);controller.abort();controller=new AbortController();const token=++epoch;returnFocus=document.activeElement;pid=id;tab='overview';current=null;pdfSource=null;fileToolsOpen=false;pdfBusy=false;pdfMessage='';render(false);if(!dialog.open)dialog.showModal();try{const source=await request('/api/papers/'+id+'/pdf-access');if(token!==epoch||!dialog.open)return;pdfSource=source;}catch(e){}if(token!==epoch)return;await refresh(token);}
async function source(page,quote,figure){
 const token=epoch,hash=current.source_hash,base=url(),id=current.guide_id;
 const data=await request(base+'/page/'+page+'?source_hash='+hash+'&guide_id='+id);if(token!==epoch||!dialog.open)return;
 if(data.source_hash!==hash||!data.text.includes(quote))throw Error('原文依据与导读快照不一致，请重新生成。');
 const body=dialog.querySelector('.pg-body');body.classList.add('with-source');body.querySelector('.pg-source')?.remove();
 let pane=document.createElement('aside');pane.className='pg-source';pane.setAttribute('aria-label','PDF 原文依据');body.append(pane);
 const pos=data.text.indexOf(quote);const text=pos<0?esc(data.text):esc(data.text.slice(0,pos))+'<mark>'+esc(quote)+'</mark>'+esc(data.text.slice(pos+quote.length));
 pane.innerHTML=`<button data-pg-source-close>收起原文</button><h4>PDF 第 ${page} 页</h4><small>生成时保存的 PDF 快照；高亮表示引用逐字命中。</small>${figure?`<img alt="PDF 第 ${page} 页原图与正文" src="${base}/page/${page}?image=true&source_hash=${hash}&guide_id=${id}">`:''}<p>${text}</p>`;
 if(!figure)pane.querySelector('mark')?.scrollIntoView({block:'center',behavior:'smooth'});
}
async function exportMd(){const token=epoch;const r=await fetch(url()+'/export?source_hash='+current.source_hash+'&guide_id='+current.guide_id,{signal:controller.signal});if(!r.ok){const e=await window.WenxianHTTP.read(r,'本次操作未完成，请重试。');throw Error(e.detail||'导出失败');}const blob=await r.blob();if(token!==epoch)return;const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='文献罗盘_全文解读.zip';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
document.addEventListener('click',async e=>{let b=e.target.closest('[data-pdfguide]');if(b)await open(Number(b.dataset.pdfguide));});
dialog.addEventListener('close',()=>{if(dialog.open)return;clearTimeout(timer);++epoch;controller.abort();returnFocus?.focus();});
dialog.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;const token=epoch;try{
 if(b.hasAttribute('data-pg-close'))dialog.close();
 if(b.dataset.pgTab){tab=b.dataset.pgTab;render(false);}
 if(b.hasAttribute('data-pg-generate')){b.disabled=true;let r=await request(url(),'POST');if(token!==epoch||!dialog.open)return;current={...current,...r,error:r.error||null};render();if(r.status==='generating'){clearTimeout(timer);timer=setTimeout(()=>refresh(token),1500);}}
 if(b.dataset.evidence)await source(Number(b.dataset.evidence),b.dataset.quote,b.dataset.figure==='1');
 if(b.hasAttribute('data-pg-source-close')){dialog.querySelector('.pg-source')?.remove();dialog.querySelector('.pg-body').classList.remove('with-source');}
 if(b.hasAttribute('data-pg-export'))await exportMd();
 }catch(err){if(err.name==='AbortError'||token!==epoch)return;current={...current,error:{message:err.message}};render();}});

async function access(action,file){
 if(pdfBusy)return;pdfBusy=true;const token=epoch,id=pid;let destination=null;pdfMessage=action==='upload'?'正在读取并校验PDF…':'正在查找或下载全文…';render();
 try{if(['download','upload','save'].includes(action))destination=await window.WenxianPDFStorage?.prepare();const suffix=action==='upload'&&destination?'?destination='+encodeURIComponent(destination):'';const r=await fetch('/api/papers/'+id+'/pdf-access/'+action+suffix,{method:'POST',signal:controller.signal,headers:file?{'Content-Type':'application/pdf'}:{'Content-Type':'application/json'},body:file||JSON.stringify({destination})});const d=await window.WenxianHTTP.read(r,'本次操作未完成，请重试。');if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'PDF获取未成功');if(token!==epoch||!dialog.open)return;pdfSource={...pdfSource,...d};pdfMessage=d.message||'';if(action==='find'&&!d.ready){if(d.has_public_link){pdfBusy=false;await access('download');return;}await window.WenxianPDFCapture?.open(id);return;}await refresh(token);}
 catch(e){if(e.name!=='AbortError'&&token===epoch&&dialog.open){pdfMessage=e.message;if(action==='download'&&!/取消|选择.*目录/.test(e.message)){await window.WenxianPDFCapture?.open(id,{prepared:true,destination});}}}
 finally{if(token===epoch&&dialog.open){pdfBusy=false;render();}}
}

dialog.addEventListener('toggle',e=>{if(e.target.matches('[data-pg-files]')&&e.target.isConnected)fileToolsOpen=e.target.open;},true);
dialog.addEventListener('change',e=>{if(e.target.hasAttribute('data-pdf-upload')&&e.target.files?.[0])access('upload',e.target.files[0]);});
dialog.addEventListener('click',e=>{if(e.target.closest('[data-pdf-find]'))access('find');if(e.target.closest('[data-pdf-download]'))access('download');if(e.target.closest('[data-pdf-save]'))access('save');if(e.target.closest('[data-pdf-folder]'))window.WenxianPDFStorage?.openFolder(pid).catch(e=>{pdfMessage=e.message;render();});if(e.target.closest('[data-pdf-browser]'))window.WenxianPDFCapture?.open(pid);});

window.WenxianPDF={open,refreshSource:async id=>{if(id!==pid||!dialog.open)return;pdfSource=await request('/api/papers/'+id+'/pdf-access');await refresh();render();}};
})();
