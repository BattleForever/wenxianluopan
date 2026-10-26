'use strict';
(() => {
 let job=null,timer=null,context=null;
 const seen=new Set(),box=document.createElement('div');
 box.id='metric-progress';box.className='metric-availability';box.hidden=true;
 document.getElementById('metric-availability').after(box);
 const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 async function request(path,body){const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'期刊指标暂时不可用');return d;}
 function active(){return job&&['queued','running'].includes(job.status);}
 function render(){window.WenxianTasks?.update('metrics',job);box.hidden=!job||(!active()&&!job.total&&!job.fields_filled); if(!job)return;box.innerHTML=`<span>${active()?'期刊指标 '+job.completed+'/'+job.total+' · 后台读取中':'期刊指标 · '+esc(job.message)}${job.fields_filled?' · 新增 '+job.fields_filled+' 个字段':''}</span><span>${active()?'<button data-metrics-stop>停止</button>':'<button data-metrics-retry>重新查询</button>'}<button data-metrics-hide aria-label="隐藏期刊指标进度">×</button></span>`;}
 async function refresh(){window.dispatchEvent(new Event('wenxian-data-updated'));}
 async function poll(){clearTimeout(timer);if(!job)return;try{const old=job.fields_filled;job=await request('/api/research/metrics/'+job.id);render();if(!active()&&job.fields_filled>0)await refresh();if(active())timer=setTimeout(poll,1800);}catch(e){box.hidden=false;box.textContent='指标读取暂时中断；已保存的数据仍可使用。';}}
 async function start(retry=false){if(active()){render();return;}try{job=await request('/api/research/metrics',{ids:context?.ids??null,allow_network:true,retry});render();if(active())poll();else if(job.fields_filled)await refresh();}catch(e){if(retry){box.hidden=false;box.textContent=e.message;}}}
 window.addEventListener('wenxian-results-rendered',e=>{context=e.detail;const key=context.key;if(!seen.has(key)){seen.add(key);start();}});
 document.addEventListener('click',async e=>{if(e.target.closest('[data-metrics-hide]'))box.hidden=true;if(e.target.closest('[data-metrics-retry]'))start(true);if(e.target.closest('[data-metrics-stop]')&&job){await request('/api/research/metrics/'+job.id+'/cancel',{});}});
 window.WenxianMetrics={start:()=>start(true)};
})();
