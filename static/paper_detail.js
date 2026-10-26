/* Bilingual reading and source records are independent detail tabs. */
(()=>{'use strict';
const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dialog=$('detail-dialog'),drafts=new Map();
function sync(){delete dialog.dataset.detailView;}
function value(v){return v==null||v===''?'未取得':esc(v);}
function header(p){
 const authors=p.authors||[],short=authors.slice(0,2).join(' · ')+(authors.length>2?' 等':''),v=p.venue_info||{};
 const metrics=[p.cited_by_count!=null?'被引 '+Number(p.cited_by_count).toLocaleString():'',v.impact_factor!=null&&v.impact_factor!==''?'IF '+esc(v.impact_factor):'',v.cas_zone?'中科院 '+esc(v.cas_zone):''].filter(Boolean);
 return `<div class="paper-heading"><span class="paper-language-label">原文题名</span><h2 id="detail-title-original">${esc(p.title||'原文题名未取得')}</h2><div id="detail-title-zh"><span class="paper-language-label">中文题名</span>${p.title_zh?`<p class="paper-heading-zh">${esc(p.title_zh)}</p>`:'<p class="translation-missing">尚未补充</p>'}</div><p class="paper-heading-meta">${esc(p.year||'年份未取得')}${p.venue?' · '+esc(p.venue):''}</p>${short?`<p class="paper-heading-authors" title="${esc(authors.join(' · '))}">${esc(short)}</p>`:''}${metrics.length?`<p class="paper-heading-metrics">${metrics.join(' · ')}</p>`:''}${(p.title&&!p.title_zh)||(p.abstract&&!p.abstract_zh)?'<button type="button" data-translate-missing class="translation-entry">补充中文译文 ↗</button>':''}</div><div class="detail-actions"><button id="detail-ai" class="primary small">AI 速览</button><button id="detail-pdfguide" data-pdfguide="${Number(p.id)}" class="secondary small">全文解读</button><button id="detail-save" class="secondary small">${p.in_reading_list?'阅读清单 · 未分类':'加入阅读清单'} ⌄</button><button id="detail-enrich" class="secondary small">补全资料</button><button id="detail-organize" class="secondary small" title="保存到文献库，并选择是否放入文件夹">${p.status==='library'?'文献库 · 未分类':'保存到文献库'} ⌄</button><button id="detail-zflush" class="secondary small" title="选择 Zotero 集合，并保存书目、阅读内容和 PDF">保存到 Zotero</button><button id="detail-seed" class="secondary small">设为种子</button></div><div id="detail-action-result"></div>`;
}
function overview(p){
 const note=drafts.has(p.id)?drafts.get(p.id):(p.reading_note||'');
 return `<div id="detail-reading-summary"></div><section class="paper-abstract"><header><h3>摘要 · 中英对照</h3></header><div class="bilingual-abstract"><section id="detail-abstract-zh"><h4>中文译文</h4>${p.abstract_zh?`<p class="abstract">${esc(p.abstract_zh)}</p>`:`<p class="translation-missing">尚未补充中文摘要${p.abstract?' · 可翻译已有原文':''}</p>${p.abstract?'<button class="translation-entry" data-translate-missing>补充中文译文 ↗</button>':''}`}</section><section id="detail-abstract-original"><h4>原文</h4><p class="abstract">${esc(p.abstract||'尚未取得原文摘要，可点击“补全资料”尝试获取。')}</p></section></div></section><details class="paper-note" ${note?'open':''}><summary>阅读备注</summary><textarea id="reading-note" class="detail-note" rows="3" placeholder="选文理由、阅读要点或待核对的问题">${esc(note)}</textarea><button id="save-note" class="secondary small">保存备注并加入清单</button></details>`;
}
function records(p,path,supports){
 const v=p.venue_info||{},d=p.discovery,doi=p.doi?`<a href="https://doi.org/${encodeURIComponent(p.doi)}" target="_blank" rel="noopener">${esc(p.doi)} ↗</a>`:'未取得';
 return `<section class="paper-records"><h3>书目信息</h3><dl><dt>作者</dt><dd>${esc((p.authors||[]).join(' · ')||'未取得')}</dd><dt>年份</dt><dd>${value(p.year)}</dd><dt>期刊 / 会议</dt><dd>${value(p.venue)}</dd><dt>DOI</dt><dd>${doi}</dd><dt>被引数</dt><dd>${value(p.cited_by_count)}</dd><dt>影响因子</dt><dd>${value(v.impact_factor)}</dd><dt>中科院分区</dt><dd>${value(v.cas_zone)}</dd><dt>JCR 分区</dt><dd>${value(v.jcr_zone)}</dd><dt>指标年份</dt><dd>${value(v.metric_year)}</dd></dl></section><details class="paper-discovery"><summary>发现与指标来源</summary><div class="provenance">${path?'发现路径：'+esc(path)+'<br>':''}${d&&d.depth>0?`第 ${d.depth} 轮 · ${d.relation==='references'?'参考文献':'施引文献'}<br>`:''}书目来源：${esc(p.source||'未记录')}<br>已知引用关系：参考 ${p.edges_out||0} 篇 · 施引 ${p.edges_in||0} 篇<br>指标来源：${esc(v.source||'未记录')} · 更新 ${esc(v.updated_at||'未记录')}</div>${supports}</details><details class="paper-source-records"><summary>补全与同步记录</summary><div id="detail-existing-extra"></div></details>`;
}
document.addEventListener('click',e=>{
 if(e.target.closest('[data-translate-missing]')){const p=window.WenxianWorkbench?.detail;if(p)window.WenxianEnrichment?.open({ids:[p.id],purpose:'translate'});}
 const more=$('detail-more');if(more&&e.target.closest('#detail-more button'))more.open=false;
});
document.addEventListener('pointerdown',e=>{const more=$('detail-more');if(more&&!more.contains(e.target))more.open=false;});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){const more=$('detail-more');if(more?.open)more.open=false;}});
document.addEventListener('input',e=>{if(e.target.id==='reading-note'){const id=window.WenxianWorkbench?.detail?.id;if(id)drafts.set(id,e.target.value);}});
window.WenxianPaperDetail={header,overview,records,sync,saved:id=>drafts.delete(id)};sync();
})();
