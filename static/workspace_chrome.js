/* Collapsible controls and a resumable background-task dock. */
(()=>{'use strict';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const filters=document.querySelector('.filter-row'),metrics=document.getElementById('metric-availability');
const panel=document.createElement('details');panel.id='screening-panel';panel.className='screening-panel';
panel.innerHTML='<summary><span>筛选与排序</span><small id="screening-summary">按年份、被引数与期刊指标筛选</small><span class="screening-chevron">⌄</span></summary>';
filters.before(panel);panel.append(filters,metrics);
try{panel.open=localStorage.getItem('wenxian_filters_open')==='1';}catch(e){}
panel.addEventListener('toggle',()=>{localStorage.setItem('wenxian_filters_open',panel.open?'1':'0');window.dispatchEvent(new Event('resize'));});
function summary(){const parts=[],year=document.getElementById('year-enabled');if(year.checked)parts.push(document.getElementById('year-min').value+'–'+document.getElementById('year-max').value);const impact=document.getElementById('min-if').value;if(impact)parts.push('IF ≥ '+impact);const zone=document.getElementById('cas-max');if(zone.value)parts.push(zone.options[zone.selectedIndex]?.text);const sort=document.getElementById('sort');if(sort.value!=='cited')parts.push(sort.options[sort.selectedIndex]?.text);document.getElementById('screening-summary').textContent=parts.join(' · ')||'按年份、被引数与期刊指标筛选';}
panel.addEventListener('change',summary);document.addEventListener('click',e=>{if(e.target.closest('#apply-filters,#reset-filters'))setTimeout(summary,0);});
const dock=document.createElement('aside');dock.id='background-task-dock';dock.hidden=true;dock.setAttribute('aria-label','后台任务');
dock.innerHTML='<button id="task-orb" aria-label="打开补全任务"><span class="task-orb-value">0%</span><i></i></button><div class="task-dock-card"><div><strong id="task-dock-title">资料补全</strong><button id="task-dock-hide" aria-label="隐藏任务提示">×</button></div><p id="task-dock-description"></p><div><button data-enrichment-show>查看任务</button><button id="task-dock-inline">展开结果栏</button></div></div>';
document.body.append(dock);let job=null,hiddenId=null;
function update(j){job=j;if(!j){dock.hidden=hiddenId==='idle';dock.classList.remove('task-active');dock.querySelector('.task-orb-value').textContent='•';document.getElementById('task-dock-title').textContent='小书灵 · 随时待命';document.getElementById('task-dock-description').textContent='按住我可以拖动；点击查看最近任务。';return;}const active=['running','queued','cancelling'].includes(j.status),pct=Math.round(100*(j.completed||0)/Math.max(1,j.total));
 dock.hidden=hiddenId===j.id;dock.classList.toggle('task-active',active);dock.style.setProperty('--task-progress',pct+'%');dock.querySelector('.task-orb-value').textContent=active?pct+'%':j.fields_filled?'✓':'•';
 document.getElementById('task-dock-title').textContent=active?'正在补全资料':'资料补全已结束';document.getElementById('task-dock-description').textContent=`${j.completed}/${j.total} 篇 · 新增 ${j.fields_filled} 个字段`;
 document.getElementById('task-orb').setAttribute('aria-label',`补全任务 ${pct}%：点击查看进度与结果`);
}
document.getElementById('task-orb').onclick=()=>window.WenxianEnrichment?.show();
document.getElementById('task-dock-hide').onclick=()=>{hiddenId=job?.id||'idle';dock.hidden=true;};
document.getElementById('task-dock-inline').onclick=()=>{const bar=document.getElementById('enrichment-progress');bar.dataset.dismissed='';bar.hidden=false;bar.dataset.view='inline';window.dispatchEvent(new Event('resize'));};
window.WenxianTaskDock={update,show:()=>{hiddenId=null;update(job);}};
const taskToggle=document.createElement('button');taskToggle.id='background-tasks-toggle';taskToggle.className='secondary small';taskToggle.textContent='后台任务';taskToggle.onclick=()=>{hiddenId=null;update(job);window.WenxianEnrichment?.show();};document.getElementById('detail-toggle').before(taskToggle);
update(null);
})();
