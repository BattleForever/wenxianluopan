/* Optional PDF reading. All model text is rendered as text, never executable HTML. */
(()=>{'use strict';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let pid=null,current=null,tab='overview',timer=null,returnFocus=null;
const dialog=document.createElement('dialog');dialog.id='pdf-guide-dialog';dialog.className='pg-dialog';dialog.setAttribute('aria-label','AI PDF 全文解读');document.body.append(dialog);
async function request(url,method='GET'){let r=await fetch(url,{method});let data=await r.json();if(!r.ok)throw Error(data.detail||'请求失败，请稍后重试');return data;}
const url=()=>'/api/papers/'+pid+'/pdf-guide';
const ev=(e,figure=false)=>`<button class="pg-evidence" data-evidence="${e.page}" data-quote="${esc(e.quote)}" data-figure="${figure?'1':'0'}">原文依据 · PDF 第 ${e.page} 页 ↗</button>`;
function render(){
 const g=current?.guide,active=current?.status==='generating';
 dialog.innerHTML=`<header class="pg-header"><div><span class="pg-kicker">AI READING · 全文解读</span><h2>${esc(g?.title_zh||'从 PDF 读懂一篇论文')}</h2><span class="pg-meta">按需使用 · Zotero 附件定位 → 正文与图注提取 → 逐节解读 → 原文核对</span></div><div class="pg-tools">${g?'<button class="secondary small" data-pg-export>导出解读</button>':''}<button class="primary small" data-pg-generate ${active?'disabled':''}>${active?'正在生成…':g?'重新生成':'生成全文解读'}</button><button class="icon-button" data-pg-close aria-label="关闭全文解读">×</button></div></header><div class="pg-body"><nav class="pg-nav" aria-label="全文解读目录">${[['overview','概览与中文解读'],['sections','逐节导读'],['figures','图表说明'],['conclusion','结论与局限']].map(([key,name])=>`<button data-pg-tab="${key}" class="${tab===key?'active':''}">${name}</button>`).join('')}<small>${g?`${current.pages} 页 PDF<br>${esc(current.model)}<br>${esc(current.created_at?.slice(0,10))}`:'不会自动生成<br>点击后才请求模型'}<br><br>AI 解读需要人工复核</small></nav><article class="pg-main"></article></div>`;
 const main=dialog.querySelector('.pg-main');
 if(!g){main.innerHTML=`<div class="pg-empty"><div class="pg-label">从摘要初筛，走向全文精读</div><h3>把论文的论证过程读清楚</h3><p>从当前论文的 PDF 附件出发，解释研究问题、每一节的方法与证据、图表面板及结论。生成后可回到原文页码核对。</p><div class="pg-flow"><span>定位真实 PDF</span><span>提取正文与图注</span><span>AI 中文解读</span><span>逐页核对依据</span></div><p class="pg-meta">优先使用已有 PDF；缺失时只读查找 Zotero 附件。扫描件暂不支持 OCR；读取失败会说明原因。不会修改 Zotero 原库。</p>${active?'<div class="pg-loading"></div><p>正在生成全文导读，请稍候。可以关闭窗口，稍后回来查看。</p>':''}${current?.error?`<p class="pg-error">${esc(current.error.message)}</p>`:''}</div>`;return;}
 main.innerHTML=`${current.stale?`<div class="pg-notice">历史导读：${esc(current.stale_reasons.join('；'))}。请重新生成后再使用。</div>`:''}${active?'<div class="pg-loading"></div>':''}${current.error?`<div class="pg-notice pg-error">${esc(current.error.message)}</div>`:''}`;
 if(tab==='overview')main.innerHTML+=`<div class="pg-label">${current.stale?'历史结果':'已保存的 AI 解读'} · 依据 PDF 正文与图注</div><h3>${esc(g.title_zh)}</h3><p>${esc(g.overview)}</p><div class="pg-card"><h4>作者单位</h4><p>${esc(g.institutions)}</p></div><div class="pg-notice">中文题名为翻译，正文为概括与解释。引用校验仅确认片段可定位，不等于概括的语义完全正确。图表解释依据正文和图注，仍需对照原图。</div><p class="pg-meta">原始题名：${esc(current.source_title)}<br>来源：${current.pages} 页真实 PDF · 未省略超限内容冒充全文<br>来源校验：${esc(current.source_hash.slice(0,16))}…</p>`;
 if(tab==='sections')main.innerHTML+='<h3>沿着论文结构理解论证</h3>'+g.sections.map((s,i)=>`<section class="pg-card"><div class="pg-label">SECTION ${String(i+1).padStart(2,'0')}</div><h4>${esc(s.heading)}</h4><p>${esc(s.explanation)}</p>${ev(s.evidence)}</section>`).join('');
 if(tab==='figures')main.innerHTML+='<h3>图表在论文中证明了什么</h3>'+g.figures.map(f=>`<section class="pg-card"><div class="pg-label">FIGURE ${f.number}</div><h4>${esc(f.caption)}</h4>${f.panels.map(p=>`<div class="pg-panel-label">${esc(p.label)}</div><p>${esc(p.explanation)}</p>`).join('')}${ev(f.evidence,true)}</section>`).join('');
 if(tab==='conclusion')main.innerHTML+=`<h3>结论与局限</h3><div class="pg-card"><p>${esc(g.conclusion.text)}</p>${ev(g.conclusion.evidence)}</div><div class="pg-notice">区分论文报告、AI 解释和自己的研究判断。未在原文中验证的应用范围不能作为本文已经证明的结论。</div>`;
}
async function refresh(){try{current=await request(url());render();if(current.status==='generating')timer=setTimeout(refresh,2000);}catch(e){current={error:{message:e.message}};render();}}
async function open(id){clearTimeout(timer);returnFocus=document.activeElement;pid=id;tab='overview';current=null;render();if(!dialog.open)dialog.showModal();await refresh();}
async function source(page,quote,figure){
 const data=await request(url()+'/page/'+page);const body=dialog.querySelector('.pg-body');body.classList.add('with-source');body.querySelector('.pg-source')?.remove();
 let pane=document.createElement('aside');pane.className='pg-source';pane.setAttribute('aria-label','PDF 原文依据');body.append(pane);
 const pos=data.text.indexOf(quote);const text=pos<0?esc(data.text):esc(data.text.slice(0,pos))+'<mark>'+esc(quote)+'</mark>'+esc(data.text.slice(pos+quote.length));
 pane.innerHTML=`<button data-pg-source-close>收起原文</button><h4>PDF 第 ${page} 页</h4><small>下方为可提取的原文文本；高亮表示引用逐字命中。</small>${figure?`<img alt="PDF 第 ${page} 页原图与正文" src="${url()}/page/${page}?image=true">`:''}<p>${text}</p>`;
 if(!figure)pane.querySelector('mark')?.scrollIntoView({block:'center',behavior:'smooth'});
}
function exportMd(){const g=current.guide;const cite=e=>`\n\n> PDF 第 ${e.page} 页：${e.quote}\n`;let md=`# ${g.title_zh}\n\n来源：${current.source_title}\n\n模型：${current.model}；${current.pages} 页 PDF；AI 生成，需核对。\n\n## 概括\n${g.overview}\n\n## 作者单位\n${g.institutions}\n`;
 for(const s of g.sections)md+=`\n## ${s.heading}\n${s.explanation}${cite(s.evidence)}`;
 for(const f of g.figures)md+=`\n## Fig. ${f.number} ${f.caption}\n`+f.panels.map(p=>`\n### ${p.label}\n${p.explanation}\n`).join('')+cite(f.evidence)+`\n![PDF 第 ${f.evidence.page} 页原图](${location.origin+url()+'/page/'+f.evidence.page+'?image=true'})\n`;
 md+=`\n## 结论与局限\n${g.conclusion.text}${cite(g.conclusion.evidence)}`;let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([md],{type:'text/markdown;charset=utf-8'}));a.download='文献罗盘_全文解读.md';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
document.addEventListener('click',async e=>{let b=e.target.closest('[data-pdfguide]');if(b)await open(Number(b.dataset.pdfguide));});
dialog.addEventListener('close',()=>{clearTimeout(timer);returnFocus?.focus();});
dialog.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{
 if(b.hasAttribute('data-pg-close'))dialog.close();
 if(b.dataset.pgTab){tab=b.dataset.pgTab;render();}
 if(b.hasAttribute('data-pg-generate')){b.disabled=true;let r=await request(url(),'POST');current={...current,...r};render();if(r.status==='generating'){clearTimeout(timer);timer=setTimeout(refresh,1500);}}
 if(b.dataset.evidence)await source(Number(b.dataset.evidence),b.dataset.quote,b.dataset.figure==='1');
 if(b.hasAttribute('data-pg-source-close')){dialog.querySelector('.pg-source')?.remove();dialog.querySelector('.pg-body').classList.remove('with-source');}
 if(b.hasAttribute('data-pg-export'))exportMd();
 }catch(err){current={...current,error:{message:err.message}};render();}});
window.WenxianPDF={open};
})();
