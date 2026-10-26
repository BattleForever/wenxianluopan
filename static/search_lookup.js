/* Find within a declared local scope; theme ranking is an explicit action. */
(()=>{'use strict';
const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let mode='filter',previousSort='recommended';
const scope=$('scope'),scopeLabel=scope.closest('label');scopeLabel.removeChild(scope);scopeLabel.remove();scope.classList.add('lookup-scope');scope.setAttribute('aria-label','查找范围');
const names={all:'本机全部',task:'本次拓展',library:'文献库',reading:'阅读清单'};
for(const option of scope.options)option.textContent=names[option.value]||option.textContent;
document.querySelector('.search-input').prepend(scope);
$('apply-query').textContent='查找';$('query').setAttribute('aria-label','查找文献题名、摘要、作者或DOI');
$('ai-rank').textContent='按输入主题排序（保留候选）';
const feedback=document.createElement('div');feedback.id='lookup-feedback';feedback.className='lookup-feedback';feedback.hidden=true;feedback.setAttribute('role','status');document.querySelector('.search-row').after(feedback);
function hint(){const page=window.WenxianWorkbench?.page,fixed=['library','reading'].includes(page);scope.disabled=fixed;scope.classList.toggle('fixed-scope',fixed);const folder=fixed?window.WenxianOrganizer?.filter():null;const label=(folder?.folder_id||folder?.unfiled)?'当前分类':names[scope.value]||'当前范围';for(const option of scope.options)option.textContent=names[option.value]||option.textContent;scope.querySelector('[value="'+scope.value+'"]').textContent=label;$('query').placeholder='在'+label+'中查找题名、摘要、作者或 DOI';scope.title=fixed?'查找当前工作区或分类；跨范围查找请进入文献探索':'切换文献查找范围';scope.querySelector('[value="task"]').disabled=!new URLSearchParams(location.search).get('job')&&!localStorage.getItem('papernet_job_v2');}
function render(data){hint();const search=data.search||{},q=search.query||'';feedback.hidden=!q;if(!q){for(const id of ['graph-empty','list-empty'])$(id).textContent='当前范围暂无文献。';return;}const rank=search.mode==='rank';feedback.innerHTML='<span><strong>'+esc(rank?'主题排序':`找到 ${data.total} 篇`)+'</strong> · '+esc(q)+' <small>· '+esc(names[search.scope]||'当前范围')+(rank?' · 保留候选，不筛除':'')+'</small></span><div>'+(search.expansions?.length?'<details class="lookup-expansion"><summary>同义匹配</summary><div>'+search.expansions.map(e=>'<p>'+esc(e.term)+' → '+e.alternatives.map(esc).join(' / ')+'</p>').join('')+'<small>本机预置词汇规则，不是模型翻译。</small></div></details>':'')+(rank&&!search.keyword_matches?'<small>当前词语未匹配到本机文本</small>':'')+(!rank&&!data.total&&scope.value==='task'?'<button class="text-button" data-lookup-all>改查本机全部</button>':'')+'<button class="text-button" data-lookup-clear>清除</button></div>';
 if(!rank&&!data.total){for(const id of ['graph-empty','list-empty'])$(id).textContent='当前范围没有找到“'+q+'”。可换用关键词，或切换查找范围。';}
 else{for(const id of ['graph-empty','list-empty'])$(id).textContent='当前范围暂无文献。';}
}
function find(){if(mode==='rank'&&$('sort').value==='relevance')$('sort').value=previousSort;mode='filter';}
document.addEventListener('click',e=>{if(e.target.closest('[data-lookup-clear]')){find();$('query').value='';window.WenxianWorkbench.reload();}if(e.target.closest('[data-lookup-all]')){scope.value='all';scope.dispatchEvent(new Event('change',{bubbles:true}));}});
scope.addEventListener('change',hint);
window.WenxianLookup={get mode(){return mode},snapshot:()=>({mode,previousSort}),restore(s){mode=s?.mode==='rank'?'rank':'filter';if(s?.previousSort&&[...$('sort').options].some(o=>o.value===s.previousSort))previousSort=s.previousSort;},find,rank(){if(mode!=='rank')previousSort=$('sort').value;mode='rank'},render};hint();
})();
