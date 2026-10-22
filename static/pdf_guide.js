/* Optional PDF reading. All model text is rendered as text, never executable HTML. */
(()=>{'use strict';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let pdfMessage='',sourceOpen=false,pdfSource=null,pdfBusy=false,pid=null,current=null,tab='overview',timer=null,returnFocus=null,epoch=0,controller=new AbortController();
const dialog=document.createElement('dialog');dialog.id='pdf-guide-dialog';dialog.className='pg-dialog';dialog.setAttribute('aria-label','AI PDF 全文解读');document.body.append(dialog);
async function request(url,method='GET'){let r=await fetch(url,{method,signal:controller.signal});let data=await r.json();if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'请求失败，请稍后重试');return data;}
const url=()=>'/api/papers/'+pid+'/pdf-guide';
const ev=(e,figure=false)=>`<button class="pg-evidence" data-evidence="${e.page}" data-quote="${esc(e.quote)}" data-figure="${figure?'1':'0'}">原文依据 · PDF 第 ${e.page} 页 ↗</button>`;
function sourceChoice(){return `<div class="pg-source-choice"><button class="secondary" data-pdf-find ${pdfBusy?'disabled':''}>查找全文</button><button class="secondary" data-pdf-download ${pdfSource?.has_public_link?'':'hidden'} ${pdfBusy?'disabled':''}>下载公开 PDF</button><label class="primary pg-upload-label">${pdfSource?.ready?'更换 PDF':'上传 PDF'}<input type="file" accept="application/pdf,.pdf" data-pdf-upload ${pdfBusy?'disabled':''} hidden></label>${pdfSource?.doi_url?`<a href="${esc(pdfSource.doi_url)}" target="_blank" rel="noopener">出版商页面 ↗</a>`:''}</div><p class="pg-source-status" role="status">${esc(pdfMessage||pdfSource?.reading_issue||pdfSource?.message||(pdfSource?.ready?pdfSource.filename+' · '+pdfSource.pages+' 页':'仅在点击生成后使用当前模型'))}</p>`;}
function render(preserve=true){
 const scroll=preserve?(dialog.querySelector('.pg-main')?.scrollTop||0):0;
 const g=current?.guide,active=current?.status==='generating';
 dialog.innerHTML=`<header class="pg-header"><div><span class="pg-kicker">AI READING · 全文解读</span><h2>${esc(g?.title_zh||'论文精读')}</h2><span class="pg-meta">逐节解读 · 图表说明 · 原文核对</span></div><div class="pg-tools"><button class="secondary small" data-pg-source>${sourceOpen?'收起附件':'论文附件'}</button>${g?'<button class="secondary small" data-pg-export>导出解读</button>':''}<button class="primary small" data-pg-generate ${active||!pdfSource?.ready||pdfSource?.reading_ready===false?'disabled':''}>${active?'正在生成…':g?'重新生成':'生成全文解读'}</button><button class="icon-button" data-pg-close aria-label="关闭全文解读">×</button></div></header><div class="pg-body"><nav class="pg-nav" aria-label="全文解读目录">${[['overview','概览与中文解读'],['sections','逐节导读'],['figures','图表说明'],['conclusion','结论与局限']].map(([key,name])=>`<button data-pg-tab="${key}" class="${tab===key?'active':''}">${name}</button>`).join('')}<small>${g?`${current.pages} 页 PDF<br>${esc(current.model)}<br>${esc(current.created_at?.slice(0,10))}`:'不会自动生成<br>点击后才请求模型'}<br><br>AI 解读需要人工复核</small></nav><article class="pg-main"></article></div>`;
 const main=dialog.querySelector('.pg-main');
 if(!g){main.innerHTML=`<div class="pg-empty"><div class="pg-label">AI 全文解读</div><h3>${pdfSource?.ready?(pdfSource.reading_ready===false?'全文需要预处理':'论文已就绪，开始精读'):'先为这篇论文关联全文'}</h3><p>${pdfSource?.ready?'按论文结构阅读研究问题、方法、图表与结论。':'查找已有附件、下载公开全文，或上传本机 PDF。'}</p>${sourceChoice()}${active?'<div class="pg-loading"></div><p>正在解读，关闭窗口后可稍后回来查看。</p>':''}${current?.error?`<p class="pg-error">${esc(current.error.message)}</p>`:''}</div>`;return;}

 main.innerHTML=`${current.stale?`<div class="pg-notice">历史导读：${esc(current.stale_reasons.join('；'))}。请重新生成后再使用。</div>`:''}${active?'<div class="pg-loading"></div>':''}${current.error?`<div class="pg-notice pg-error">${esc(current.error.message)}</div>`:''}`;
 if(sourceOpen)main.innerHTML+=`<section class="pg-attachment-panel"><h4>论文附件</h4>${sourceChoice()}</section>`;
 if(tab==='overview')main.innerHTML+=`<div class="pg-label">${current.stale?'历史结果':'已保存的 AI 解读'} · 依据 PDF 正文与图注</div><h3>${esc(g.title_zh)}</h3><p>${esc(g.overview)}</p><div class="pg-card"><h4>作者单位</h4><p>${esc(g.institutions)}</p></div>${g.coverage?`<details class="pg-notice"><summary>章节与图表覆盖</summary>版面清单覆盖：${g.coverage.sections_covered}/${g.coverage.sections_detected} 个标题、${g.coverage.figures_covered}/${g.coverage.figures_detected} 张主图。${esc(g.coverage.note)}</details>`:''}<details class="pg-notice"><summary>阅读说明</summary>中文题名为翻译，正文为概括与解释。引用校验仅确认片段可定位，不等于概括的语义完全正确。图表解释依据正文和图注，仍需对照原图。${current.validity==='unverifiable'?' 当前模型配置无法比较，结果仍可作为历史资料阅读。':''}</details><details class="pg-meta"><summary>来源与生成信息</summary>原始题名：${esc(current.source_title)}<br>来源：${current.pages} 页真实 PDF · 未省略超限内容冒充全文<br>来源校验：${esc(current.source_hash.slice(0,16))}…</details>`;
 if(tab==='sections')main.innerHTML+='<h3>沿着论文结构理解论证</h3>'+g.sections.map((s,i)=>`<section class="pg-card"><div class="pg-label">SECTION ${String(i+1).padStart(2,'0')}</div><h4>${esc(s.heading)}</h4><p>${esc(s.explanation)}</p>${ev(s.evidence)}</section>`).join('');
 if(tab==='figures')main.innerHTML+='<h3>图表在论文中证明了什么</h3>'+g.figures.map(f=>`<section class="pg-card"><div class="pg-label">FIGURE ${f.number}</div><h4>${esc(f.caption.split("。")[0])}</h4><details class="pg-caption"><summary>查看中文图注概括</summary><p>${esc(f.caption)}</p></details>${f.panels.map(p=>`<div class="pg-panel-label">${esc(p.label)}</div><p>${esc(p.explanation)}</p>`).join('')}${ev(f.evidence,true)}</section>`).join('');
 if(tab==='conclusion')main.innerHTML+=`<h3>结论与局限</h3><div class="pg-card"><p>${esc(g.conclusion.text)}</p>${ev(g.conclusion.evidence)}</div><div class="pg-notice">区分论文报告、AI 解释和自己的研究判断。未在原文中验证的应用范围不能作为本文已经证明的结论。</div>`;
 main.scrollTop=scroll;
}
async function refresh(token=epoch){try{const data=await request(url());if(token!==epoch||!dialog.open)return;if(JSON.stringify(current)!==JSON.stringify(data)){current=data;render();}if(current.status==='generating')timer=setTimeout(()=>refresh(token),2000);}catch(e){if(e.name==='AbortError'||token!==epoch)return;current={...current,error:{message:e.message}};render();}}
async function open(id){clearTimeout(timer);controller.abort();controller=new AbortController();const token=++epoch;returnFocus=document.activeElement;pid=id;tab='overview';current=null;pdfSource=null;sourceOpen=false;pdfBusy=false;pdfMessage='';render(false);if(!dialog.open)dialog.showModal();try{const source=await request('/api/papers/'+id+'/pdf-access');if(token!==epoch||!dialog.open)return;pdfSource=source;}catch(e){}if(token!==epoch)return;await refresh(token);}
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
async function exportMd(){const token=epoch;const r=await fetch(url()+'/export?source_hash='+current.source_hash+'&guide_id='+current.guide_id,{signal:controller.signal});if(!r.ok){const e=await r.json();throw Error(e.detail||'导出失败');}const blob=await r.blob();if(token!==epoch)return;const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='文献罗盘_全文解读.zip';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
document.addEventListener('click',async e=>{let b=e.target.closest('[data-pdfguide]');if(b)await open(Number(b.dataset.pdfguide));});
dialog.addEventListener('close',()=>{if(dialog.open)return;clearTimeout(timer);++epoch;controller.abort();returnFocus?.focus();});
dialog.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;const token=epoch;try{
 if(b.hasAttribute('data-pg-close'))dialog.close();
 if(b.hasAttribute('data-pg-source')){sourceOpen=!sourceOpen;render();}
 if(b.dataset.pgTab){tab=b.dataset.pgTab;render(false);}
 if(b.hasAttribute('data-pg-generate')){b.disabled=true;let r=await request(url(),'POST');if(token!==epoch||!dialog.open)return;current={...current,...r,error:r.error||null};render();if(r.status==='generating'){clearTimeout(timer);timer=setTimeout(()=>refresh(token),1500);}}
 if(b.dataset.evidence)await source(Number(b.dataset.evidence),b.dataset.quote,b.dataset.figure==='1');
 if(b.hasAttribute('data-pg-source-close')){dialog.querySelector('.pg-source')?.remove();dialog.querySelector('.pg-body').classList.remove('with-source');}
 if(b.hasAttribute('data-pg-export'))await exportMd();
 }catch(err){if(err.name==='AbortError'||token!==epoch)return;current={...current,error:{message:err.message}};render();}});

async function access(action,file){
 if(pdfBusy)return;pdfBusy=true;const token=epoch,id=pid;pdfMessage=action==='upload'?'正在读取并校验PDF…':'正在查找或下载全文…';render();
 try{const r=await fetch('/api/papers/'+id+'/pdf-access/'+action,{method:'POST',signal:controller.signal,headers:file?{'Content-Type':'application/pdf'}:undefined,body:file||undefined});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'PDF获取未成功');if(token!==epoch||!dialog.open)return;pdfSource={...pdfSource,...d};pdfMessage='';await refresh(token);}
 catch(e){if(e.name!=='AbortError'&&token===epoch&&dialog.open)pdfMessage=e.message;}
 finally{if(token===epoch&&dialog.open){pdfBusy=false;render();}}
}

dialog.addEventListener('change',e=>{if(e.target.hasAttribute('data-pdf-upload')&&e.target.files?.[0])access('upload',e.target.files[0]);});
dialog.addEventListener('click',e=>{if(e.target.closest('[data-pdf-find]'))access('find');if(e.target.closest('[data-pdf-download]'))access('download');});

window.WenxianPDF={open};
})();
