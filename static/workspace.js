'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state={page:'explore',seeds:new Map(),pending:new Map(),selected:new Set(),data:null,job:null,view:'graph',detail:null,detailTab:'overview',linkSort:'cited',cy:null,request:0};
let toastTimer, seedTimer, detailRequest=0, graphFrame;
const currentYear=new Date().getFullYear();
function toast(message,error=false){$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,6500);}
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok){let detail=data.detail||data.error||`HTTP ${response.status}`;if(Array.isArray(detail))detail=detail.map(d=>d.msg).join('；');throw new Error(detail);}return data;}
const guarded=fn=>async event=>{try{await fn(event);}catch(error){toast(error.message,true);}};
function num(id){const value=$(id).value.trim();return value===''?null:Number(value);}
function seedIds(){return [...state.seeds.keys()];}
function renderSeeds(){$('seed-chips').innerHTML=state.seeds.size?[...state.seeds.values()].map(p=>`<div class="seed-chip"><span title="${esc(p.title)}">${esc(p.title||p.title_zh||'未命名文献')}</span><button data-remove-seed="${p.id}" aria-label="移除此种子">×</button></div>`).join(''):'<span class="muted">选择一篇或多篇已知文献，开始探索。</span>';}
async function refreshCounts(){const [health,info]=await Promise.all([api('/api/health'),api('/api/research/state')]);$('paper-count').textContent=(health.library+health.candidates).toLocaleString();$('edge-count').textContent=health.edges.toLocaleString();if($('app-version'))$('app-version').textContent='文献罗盘 · v'+(health.version||'0.8.0');$('reading-count').textContent=info.reading_count;$('mode-badge').textContent=health.online?'联网模式 · 外部数据源':'离线模式 · 本地数据';}
function parameters(){return {query:$('query').value.trim(),seed_ids:seedIds(),scope:$('scope').value,sort:$('sort').value,job_id:state.job?.id||'',year_min:$('year-enabled').checked?num('year-min'):null,year_max:$('year-enabled').checked?num('year-max'):null,min_if:num('min-if'),cas_max:$('cas-max').value==='unknown'?null:num('cas-max'),cas_missing:$('cas-max').value==='unknown',zone_scheme:$('zone-scheme').value,limit:200};}
async function loadResults(){const version=++state.request;$('result-count').textContent='正在检索…';try{const data=await api('/api/research/search',parameters());if(version!==state.request)return;state.data=data;state.selected.clear();$('select-all').checked=false;renderResults();}catch(error){if(version===state.request)$('result-count').textContent='检索失败，请调整条件后重试';throw error;}}
function reason(p){const bits=[];if(p.is_seed)bits.push('<span class="tag seed">种子</span>');const d=p.discovery;if(d&&d.depth>0){bits.push(`第 ${d.depth} 轮 · ${d.relation==='references'?'参考文献':'施引文献'} · 经文献 #${d.parent}`);bits.push(typeof d.support_count==='number'?`发现依据 ${d.support_count} 条${d.support_truncated?'（至少）':''}`:'发现依据：未记录');}if(p.relevance!==undefined)bits.push(`文本相关度 ${(p.relevance*100).toFixed(1)}%${p.matched_terms?.length?' · 匹配 '+p.matched_terms.map(esc).join(' / '):''}`);if($('sort').value==='network')bits.push(`网络分数 ${p.network_score} · 共同参考 ${p.shared_refs} · 共同施引 ${p.shared_citers}`);return bits.join('<br>');}
function renderResults(){const data=state.data;renderMetricAvailability(data.facets);$('result-count').textContent=`${data.total} 篇文献${data.shown<data.total?` · 当前展示 ${data.shown} 篇`:''}`;$('result-caption').textContent=`${data.edges.length} 条本地已知引用 · 箭头由施引指向被引`;$('graph-empty').hidden=!!data.shown;$('list-empty').hidden=!!data.shown;
 const model=data.model;const modelVisible=$('sort').value==='relevance';$('model-note').hidden=!modelVisible;$('model-note').classList.toggle('warning',model.status!=='ok');$('model-note').textContent=modelVisible?`${model.method} ${model.components?`· ${model.components} 个潜在维度`:''}　${model.hint||''}　${model.status==='ok'?`在 ${model.corpus_size} 篇本地文献上建模；分数用于阅读排序。`:''}`:'';
 $('papers-body').innerHTML=data.papers.map(p=>{const v=p.venue_info||{};const hasIF=v.impact_factor!=null&&v.impact_factor!=='';const metricTip=`指标来源：${v.source||'未提供'} · 更新：${v.updated_at||'未知'} · 指标年份：${v.metric_year||'指标年份未知'}`;return `<tr><td><input type="checkbox" data-select="${p.id}" aria-label="选择 ${esc(p.title)}"></td><td><button class="paper-title" data-paper="${p.id}">${esc(p.title||p.title_zh||'未命名文献')}</button>${p.title_zh&&p.title_zh!==p.title?`<div class="paper-zh">${esc(p.title_zh)}</div>`:''}<div class="paper-meta">${esc((p.authors||[]).slice(0,2).join(' · '))}${p.venue?'　|　'+esc(p.venue):''}</div>${reason(p)?`<div class="reason">${reason(p)}</div>`:''}</td><td class="metric">${p.year==null?'—':p.year}</td><td class="metric">${(p.cited_by_count??0).toLocaleString()}</td><td class="metric" title="${esc(metricTip)}">${hasIF?'IF '+esc(v.impact_factor):'IF 未知'}<br>${v.cas_zone!=null&&v.cas_zone!==''?esc(v.cas_zone):'分区未知'}<br><small class="metric-year">${esc(v.metric_year?`指标年份 ${v.metric_year}`:'指标年份未知')}</small></td><td><button class="save-paper ${p.in_reading_list?'saved':''}" data-save="${p.id}" aria-label="${p.in_reading_list?'移出':'加入'}阅读清单">${p.in_reading_list?'▣':'▢'}</button></td></tr>`;}).join('');
 updateSelection();if(state.view==='graph')drawGraph();}
function renderMetricAvailability(f){
 if(!f)return;const scheme=$('zone-scheme').value,known=f[scheme+'_known']||0,total=f.total||0,zones=f[scheme+'_zones']||{},sel=$('cas-max'),selected=sel.value;
 const options=[['','不限分区 · '+total+' 篇'],...['1','2','3','4'].map((v,i)=>[v,(i===0?'1 区':'1–'+v+' 区')+' · '+['1','2','3','4'].slice(0,i+1).reduce((n,k)=>n+(zones[k]||0),0)+' 篇']),['unknown','分区缺失 · '+(total-known)+' 篇']];
 sel.innerHTML=options.map(([v,t])=>`<option value="${v}">${t}</option>`).join('');sel.value=selected;
 const box=$('metric-availability');box.hidden=total===0;box.innerHTML=`<span>IF 已知 ${f.if_known}/${total} · ${scheme==='cas'?'中科院':'JCR'}分区已知 ${known}/${total}${known===0?' · 当前缺少分区，可补全指标或查看缺失项':''}</span><button id="fill-metrics">补全期刊指标 →</button>`;
}
function updateSelection(){$('selection-count').textContent=state.selected.size?`已选择 ${state.selected.size} 篇文献`:`当前 ${state.data?.shown||0} 篇 · 可直接导出；勾选后仅导出所选`;$('add-selected').disabled=!state.selected.size;}
function sizeGraph(){const el=$('graph'),footer=document.querySelector('.result-footer');const height=Math.max(200,window.innerHeight-el.getBoundingClientRect().top-(footer?.getBoundingClientRect().height||56)-18);if(Math.abs(el.clientHeight-height)>1)el.style.height=height+'px';return height;}
function fitGraph(feedback=false){if(!state.cy||state.view!=='graph')return;sizeGraph();state.cy.resize();state.cy.elements().unselect();state.cy.fit(state.cy.elements(),38);if(feedback)toast('已适配 '+state.cy.nodes().length+' 篇文献');}
function scheduleGraph(){cancelAnimationFrame(graphFrame);graphFrame=requestAnimationFrame(()=>{if(state.cy&&state.view==='graph'&&!$('workspace-page').hidden)fitGraph();});}
function closeDetail(){++detailRequest;const d=$('detail-dialog');if(d.open)d.close();}
function spreadGraph(cy){
 const el=$('graph');sizeGraph();cy.resize();
 const ns=cy.nodes();if(ns.length<2){cy.fit(null,55);return;}
 const W=el.clientWidth,H=el.clientHeight,padX=70,padY=45;
 // Uniform area distribution prevents strongly connected nodes collapsing together.
 const seedIndex=Math.max(0,ns.toArray().findIndex(n=>n.hasClass('seed')));
 let rank=0;const cx=W/2,cy0=H/2,rx=(W-2*padX)/2,ry=(H-2*padY)/2;
 const pts=ns.map((n,i)=>{
  if(i===seedIndex)return {x:cx,y:cy0};
  rank++;const r=Math.sqrt((rank-.25)/(ns.length-.5)),angle=rank*2.399963229728653;
  return {x:cx+rx*r*Math.cos(angle),y:cy0+ry*r*Math.sin(angle)};
 });
 const gap=Math.max(32,Math.min(ns.length>150?42:ns.length>80?52:66,Math.sqrt((W-2*padX)*(H-2*padY)/ns.length)*.8));
 for(let pass=0;pass<100;pass++){
  let moved=false;
  for(let i=0;i<pts.length;i++)for(let j=i+1;j<pts.length;j++){
   let dx=pts[j].x-pts[i].x,dy=pts[j].y-pts[i].y,d=Math.hypot(dx,dy);
   if(d<gap){if(d<.01){dx=Math.cos(i*2.399);dy=Math.sin(i*2.399);d=1;}const push=(gap-d)*.51;pts[i].x-=dx/d*push;pts[i].y-=dy/d*push;pts[j].x+=dx/d*push;pts[j].y+=dy/d*push;moved=true;}
  }
  pts.forEach(p=>{p.x=Math.max(padX,Math.min(W-padX,p.x));p.y=Math.max(padY,Math.min(H-padY,p.y));});
  if(!moved)break;
 }
 cy.batch(()=>ns.forEach((n,i)=>n.position(pts[i])));cy.fit(null,30);
}
function graphMetric(p,mode){if(mode==='cited')return p.cited_by_count==null?null:Number.isFinite(Number(p.cited_by_count))?Number(p.cited_by_count):null;if(mode==='year')return p.year==null?null:Number(p.year);const v=p.venue_info?.impact_factor;if(v==null||v==='')return null;const n=Number(v);return Number.isFinite(n)?n:null;}
function colorScale(papers){const mode=$('graph-color')?.value||'cited',names={cited:'被引数',impact_factor:'期刊影响因子',year:'发表年份',cluster:'网络分组'};
const values=mode==='cluster'?[]:papers.map(p=>graphMetric(p,mode)).filter(v=>v!==null),lo=values.length?Math.min(...values):null,hi=values.length?Math.max(...values):null;
const legend=$('graph-color-legend');if(legend)legend.innerHTML='<span><i class="seed-dot"></i>种子文献</span>'+(mode==='cluster'?'<span>颜色表示引用网络分组</span>':'<span><b>'+names[mode]+'</b> '+(lo??'暂无数据')+' <i class="graph-gradient"></i> '+(hi??'暂无数据')+'</span><span><i class="graph-unknown"></i>指标缺失</span>')+'<span>大小=被引数 · 悬停查看题名 · 滚轮缩放</span>';
return {mode,color:p=>{const v=graphMetric(p,mode);if(v===null||lo===null)return '#68778a';let t=hi===lo?.65:mode==='cited'?(Math.log1p(v)-Math.log1p(lo))/(Math.log1p(hi)-Math.log1p(lo)):(v-lo)/(hi-lo);const a=t<.55?[40,76,131]:[83,166,204],b=t<.55?[83,166,204]:[160,240,219];t=t<.55?t/.55:(t-.55)/.45;return 'rgb('+a.map((x,i)=>Math.round(x+(b[i]-x)*t)).join(',')+')';},title:p=>names[mode]+': '+(graphMetric(p,mode)??'缺失')};}
function highlightGraph(node,hover=false){
 const cy=state.cy;if(!cy)return;cy.elements().removeClass('dim focus reference citing');
 if(!node)return;
 if(hover){cy.elements().addClass('dim');node.closedNeighborhood().removeClass('dim');node.addClass('focus');}
 node.connectedEdges().forEach(e=>{e.addClass(e.source().id()===node.id()?'reference':'citing');if(hover)e.addClass('focus');});
}
function graphTooltip(node){const box=$('graph-tooltip'),pos=node.renderedPosition();box.textContent=node.data('title');box.hidden=false;box.style.left=Math.max(12,Math.min(pos.x+20,$('graph').clientWidth-box.offsetWidth-12))+'px';box.style.top=Math.max(48,Math.min(pos.y+16,$('graph').clientHeight-box.offsetHeight-40))+'px';}
function drawGraph(){
 if(!state.data||$('workspace-page').hidden||state.view!=='graph')return;
 state.cy?.destroy();sizeGraph();
 const palette=['#549c99','#7798b7','#c6a36d','#8c8cb0','#9bac89','#b98f93'],papers=state.data.papers,colors=colorScale(papers);
 const elements=papers.map((p,i)=>({data:{id:String(p.id),title:(p.title||p.title_zh||'')+(colors.mode==='cluster'?'':'\n'+colors.title(p)),label:p.is_seed?(p.title||p.title_zh||'').slice(0,38)+((p.title||'').length>38?'…':''):'',color:colors.mode==='cluster'?palette[p.cluster%palette.length]:colors.color(p),size:Math.max(17,Math.min(30,17+Math.log10(1+(p.cited_by_count||0))*2.5))},classes:p.is_seed?'seed':'',position:{x:300+Math.cos(i*2.399)*Math.sqrt(i+1)*25,y:200+Math.sin(i*2.399)*Math.sqrt(i+1)*25}})).concat(state.data.edges.map(e=>({data:{id:`e${e.from}-${e.to}`,source:String(e.from),target:String(e.to)}})));
 state.cy=cytoscape({container:$('graph'),elements,style:[
  {selector:'node',style:{'background-color':'data(color)',width:'data(size)',height:'data(size)',label:'data(label)','font-size':11,color:'#c6d3e2','text-valign':'bottom','text-margin-y':9,'text-background-color':'#0b1320','text-background-opacity':.9,'text-background-padding':3,'border-width':2,'border-color':'#0b1320'}},
  {selector:'node.seed',style:{'border-color':'#7fd8c9','border-width':3,'font-weight':650,color:'#c9f2ea'}},
  {selector:'edge',style:{width:1.15,'line-color':'#63809d','target-arrow-color':'#8faecb','target-arrow-shape':'triangle','arrow-scale':1.15,'curve-style':'bezier','control-point-step-size':42,opacity:.3}},
  {selector:'edge.reference',style:{'line-color':'#54b8ff','target-arrow-color':'#83ccff',width:1.65,opacity:.88,'arrow-scale':1.35}},
  {selector:'edge.citing',style:{'line-color':'#ffb66e','target-arrow-color':'#ffd09c',width:1.65,opacity:.9,'arrow-scale':1.35}},
  {selector:'node:selected',style:{'border-width':4,'border-color':'#6cb2ef'}},
  {selector:'.dim',style:{opacity:.1}},
  {selector:'node.focus',style:{opacity:1}},
  {selector:'edge.focus',style:{opacity:1,width:2.3,'arrow-scale':1.6}}
 ],layout:{name:'preset',fit:false},minZoom:.08,maxZoom:3,wheelSensitivity:.25});
 spreadGraph(state.cy);const seed=()=>state.cy.nodes('.seed').first();highlightGraph(seed());
 state.cy.on('tap','node',guarded(async e=>openDetail(Number(e.target.id()))));
 state.cy.on('tap',e=>{if(e.target===state.cy)closeDetail();});
 state.cy.on('mouseover','node',e=>{highlightGraph(e.target,true);graphTooltip(e.target);});
 state.cy.on('mouseout','node',()=>{highlightGraph(seed());$('graph-tooltip').hidden=true;});
 state.cy.on('pan zoom',()=>$('graph-tooltip').hidden=true);
}
function setView(view){state.view=view;$('graph-wrap').hidden=view!=='graph';$('list-wrap').hidden=view!=='list';for(const item of ['graph','list']){$(`${item}-view`).classList.toggle('active',item===view);$(`${item}-view`).setAttribute('aria-pressed',item===view?'true':'false');}$('fit-graph').hidden=view!=='graph';if(view==='graph')requestAnimationFrame(drawGraph);}
async function navigate(page){closeDetail();clearTimeout(toastTimer);$('toast').hidden=true;document.querySelector('.advanced-box').open=false;state.page=page;document.querySelectorAll('[data-page]').forEach(button=>button.classList.toggle('active',button.dataset.page===page));$('workspace-page').hidden=['import','settings'].includes(page);$('import-page').hidden=page!=='import';$('settings-page').hidden=page!=='settings';if(page==='settings'){const settings=await api('/api/settings');$('offline-mode').checked=settings.offline_mode==='1';$('zotero-enabled').checked=settings.zotero_enabled==='1';$('mailto').value=settings.mailto||'';return;}if(page==='import')return;
 $('seed-panel').hidden=page!=='explore';const workflowEl=document.querySelector('.workflow');if(workflowEl)workflowEl.hidden=page!=='explore';$('page-title').textContent=page==='reading'?'把筛选判断留在阅读清单':page==='library'?'我的文献库':'从几篇论文，梳理研究脉络';$('page-subtitle').textContent=page==='reading'?'集中查看待精读文献，保留备注，并导出到文献管理器。':page==='library'?'已导入或从候选池保存的文献，可随时作为新的调研起点。':'沿参考文献与施引文献逐轮探索，再按研究相关性和文献指标筛选。';$('scope').value=page==='reading'?'reading':page==='library'?'library':state.job?'task':'all';resetFields();if(page!=='explore')setView('list');await loadResults();}
function resetFields(){for(const id of ['query','min-if','cas-max'])$(id).value='';$('year-min').value=2020;$('year-max').value=currentYear;$('year-enabled').checked=false;$('sort').value='cited';$('graph-color').value='cited';}
async function seedSearch(){const result=await api('/api/papers?limit=80&query='+encodeURIComponent($('seed-search').value.trim()));$('seed-results').innerHTML=result.papers.map(p=>`<label class="seed-option"><input type="checkbox" data-seed-pick="${p.id}" ${state.pending.has(p.id)?'checked':''}><span>${esc(p.title||p.title_zh)}<small>${esc(p.title_zh||'')}${p.year?' · '+p.year:''} · 被引 ${(p.cited_by_count||0).toLocaleString()}</small></span></label>`).join('')||'<div class="empty">未找到文献。请先导入，再选择种子。</div>';state.seedSearchRows=new Map(result.papers.map(p=>[p.id,p]));$('seed-picked').textContent=`已选 ${state.pending.size} / 10 篇`;}
async function startExploration() {if(!state.seeds.size)throw new Error('请先选择至少一篇种子文献。');$('run-explore').disabled=true;try{const job=await api('/api/research/explore',{seed_ids:seedIds(),direction:$('direction').value,depth:num('depth'),per_direction:num('per-direction'),max_nodes:num('max-nodes'),online:$('fetch-mode').value==='online',max_fetch:num('max-fetch')??20});state.job=job;localStorage.setItem('papernet_job_v2',job.id);$('scope').value='task';await pollJob();}catch(error){$('run-explore').disabled=false;throw error;}}
const jobNames={queued:'排队中',running:'扩展进行中',completed:'扩展完成',completed_with_errors:'扩展结束 · 存在失败请求',cancelled:'任务已停止',interrupted:'任务中断',failed:'任务失败'};
function supportsSummary(results){let known=false,total=0,truncated=false;for(const r of results||[]){if(r&&typeof r.support_count==='number'){known=true;total+=r.support_count;if(r.support_truncated)truncated=true;}}return known?{total,truncated}:null;}
async function pollJob(){if(!state.job)return;try{state.job=await api('/api/research/jobs/'+state.job.id);const j=state.job;const active=['queued','running','cancelling'].includes(j.status);$('run-explore').disabled=active;$('job-progress').hidden=false;const sup=supportsSummary(j.results);const supText=sup?`本任务已记录的发现依据 ${sup.total} 条${sup.truncated?'（至少；部分节点依据超过 50 条，已截断）':''}`:'本任务未保存可展示的发现依据';const fetchCapped=j.options?.online&&typeof j.options.max_fetch==='number'&&j.fetches>=j.options.max_fetch;const truncText=j.truncated?(fetchCapped?`联网抓取已达上限 ${j.options.max_fetch} 篇节点，其余邻居本轮不再扩展`:'已触及采样或数量上限，结果为有界样本'):j.options?.online?'按请求范围补充数据':'沿本地已有引用关系探索';$('job-progress').innerHTML=`<strong>${esc(jobNames[j.status]||j.status)}</strong><span>发现 ${j.results.length} 篇 · 处理 ${j.processed} 篇节点${j.fetches?` · 联网取数 ${j.fetches} 篇节点（非 HTTP 次数）`:''}</span><small>${truncText}${j.errors.length?` · ${j.errors.length} 个请求失败（悬停查看）`:''}<br>${supText}</small>${active?'<button id="cancel-job" class="text-button">停止任务</button>':''}`;if(j.errors.length)$('job-progress').title=j.errors.map(e=>`#${e.paper_id}: ${e.error}`).join('\n');if(!active){await loadResults();await refreshCounts();if(j.status==='failed')toast(j.message,true);}else setTimeout(()=>pollJob(),700);}catch(error){$('run-explore').disabled=false;toast('进度读取失败：'+error.message,true);}}
async function saveReading(ids,action='add',note=null){await api('/api/research/reading',{ids,action,note});await refreshCounts();if(state.data)await loadResults();toast(action==='add'?'已保存到阅读清单':'已移出阅读清单');}
async function openDetail(id){const token=++detailRequest,page=state.page;const result=await api('/api/papers/'+id);if(token!==detailRequest||page!==state.page)return;const current=state.data?.papers.find(p=>p.id===id)||{};const path=state.job?.results.find(p=>p.id===id);const venueInfo={...(result.venue_info||{})};if(venueInfo.metric_year===undefined&&current.venue_info)venueInfo.metric_year=current.venue_info.metric_year;state.detail={...current,...result,venue_info:venueInfo,discovery:path||current.discovery,in_reading_list:current.in_reading_list||false,reading_note:current.reading_note||''};if(!('in_reading_list' in current)){const all=await api('/api/research/search',{scope:'reading',limit:500});const saved=all.papers.find(p=>p.id===id);if(saved){state.detail.in_reading_list=true;state.detail.reading_note=saved.reading_note;}}if(token!==detailRequest||page!==state.page)return;renderDetail();showDetailPanel();await detailTab('overview');}
/* A 线 v06：详情改为桌面右侧边面板（非模态 show，保留图谱/列表上下文）。
 * 环境只实现 showModal（如 Node DOM stub / 旧内核）时回退 showModal，行为不退化。 */
function showDetailPanel(){const dlg=$('detail-dialog');if(dlg.open)return;if(typeof dlg.show==='function')dlg.show();else dlg.showModal();}
function renderDetail(){const p=state.detail;const v=p.venue_info||{};const doi=p.doi?`<a href="https://doi.org/${encodeURIComponent(p.doi)}" target="_blank" rel="noopener">DOI ↗</a>`:'';$('detail-content').innerHTML=`<h2>${esc(p.title||p.title_zh)}</h2>${p.title_zh&&p.title!==p.title_zh?`<h3><span class="detail-label">中文题名</span>${esc(p.title_zh)}</h3>`:''}<p class="detail-meta">${esc((p.authors||[]).join(' · '))}<br>${p.year==null?'年份未知':p.year}　${esc(p.venue||'刊物未知')}　|　被引 ${(p.cited_by_count??0).toLocaleString()}<br>IF ${v.impact_factor!=null&&v.impact_factor!==''?esc(v.impact_factor):'未知'}　中科院分区 ${v.cas_zone!=null&&v.cas_zone!==''?esc(v.cas_zone):'未知'}　JCR ${v.jcr_zone!=null&&v.jcr_zone!==''?esc(v.jcr_zone):'未知'}</p><div class="detail-actions"><button id="detail-save" class="primary small">${p.in_reading_list?'移出阅读清单':'加入阅读清单'}</button><button id="detail-ai" class="secondary small">AI 速览</button><button id="detail-pdfguide" data-pdfguide="${p.id}" class="secondary small">AI 全文解读</button><button id="detail-seed" class="secondary small">设为扩展种子</button><button id="detail-promote" class="secondary small" ${p.status==='library'?'disabled':''}>${p.status==='library'?'已在文献库':'保存到文献库'}</button><button id="detail-enrich" class="secondary small" title="按字段补全缺失信息：本地已有 → Zotero → 有来源网络书目；已有内容不覆盖，可选择 AI 翻译与概括">补全缺失字段</button><button id="detail-zflush" class="secondary small" title="把本篇的同步待办写入 Zotero（写回通道）；失败可重试">同步到 Zotero / 重试</button>${doi}</div><div id="detail-action-result"></div>`;}
/* V4-07 修复：详情页同步状态按当前 paper_id 过滤请求（不再取无参默认前 500 条）。
   响应回来时先校验仍是当前详情 id 与页签再渲染，快速切换详情不串页；
   服务端无该端点时整块优雅隐藏。 */
function detailSyncStatusUrl(paperId){return '/api/library/sync/status?paper_id='+encodeURIComponent(paperId);}
async function refreshDetailSyncStatus(){
  const p=state.detail;if(!p)return;const myId=p.id;
  const extra=$('detail-existing-extra');if(!extra)return;
  let box=document.getElementById('detail-sync-box');
  try{const r=await fetch(detailSyncStatusUrl(myId));
    if(!r.ok||state.detail?.id!==myId)return;
    const data=await r.json();if(state.detail?.id!==myId)return;
    const X=window.WenxianExisting;const rec=X?X.extractSyncRecord(data,p):null;
    if(!rec){if(box)box.remove();return;}
    const label=rec.zotero_state?X.syncStateText(rec.zotero_state):null;
    const errLine=rec.last_error?`<div class="sync-err">上次错误：${esc(rec.last_error)}</div>`:'';
    const html=`<div class="sync-status" id="detail-sync-box"><strong>Zotero 同步状态</strong>${label?`<span class="sync-badge sync-${esc(rec.zotero_state)}">${esc(label)}</span>`:''}${rec.local_saved&&rec.zotero_state!=='synced'?'<span class="muted">已保存本地库</span>':''}${errLine}</div>`;
    if(box)box.outerHTML=html;else extra.insertAdjacentHTML('beforeend',html);
  }catch(e){/* 端点不存在：同步状态区块保持隐藏 */}
}
/* V4-07/08 产品按钮结果渲染：字段补全（契约 3.1）与 Zotero 同步（契约 3.3）。 */
function renderEnrichResult(result){
  const box=$('detail-action-result');if(!box)return;
  const X=window.WenxianExisting;
  const fields=result&&result.fields?Object.keys(result.fields):[];
  const lines=fields.map(f=>{const line=X?X.enrichFieldLine(f,result.fields[f]):null;return line?`<li>${esc(line)}</li>`:'';}).join('');
  const errors=(Array.isArray(result&&result.errors)?result.errors:[]).map(e=>{const text=X?(X.enrichErrorText(e.code)||e.message||e.code):String(e.message||e.code);return `<li class="sync-err">${esc(X&&X.fieldLabel?X.fieldLabel(e.field):e.field)}：${esc(text)}</li>`;}).join('');
  const summary=(X&&X.enrichSummaryLine?X.enrichSummaryLine(result):null)||'';
  box.innerHTML=`<div class="enrich-result"><strong>补全缺失字段结果</strong><p class="muted">${esc(summary)}</p>${lines?`<ul class="enrich-fields">${lines}</ul>`:''}${errors?`<ul class="enrich-errors">${errors}</ul>`:''}${result&&result.queued?'<p class="muted">已登记 Zotero 同步待办，可点「同步到 Zotero / 重试」执行写回。</p>':''}</div>`;
}
/* R2-02：同步结果渲染——同时支持单篇入口 /api/library/sync/paper 响应
   （queued/queue_count/flushed/zotero_state/reason/last_error/result）与旧
   /api/library/sync/flush 批量响应（flushed/biblio/notes/verified/reason/errors），
   复用现有 enrich-result / sync-err / sync-badge 样式，不新造样式体系。 */
function renderFlushResult(resp){
  const box=$('detail-action-result');if(!box)return;
  if(!resp||typeof resp!=='object'){box.innerHTML='';return;}
  const X=window.WenxianExisting;
  const badge=(typeof resp.zotero_state==='string'&&resp.zotero_state&&X&&X.syncStateText(resp.zotero_state))?`<span class="sync-badge sync-${esc(resp.zotero_state)}">${esc(X.syncStateText(resp.zotero_state))}</span>`:'';
  const head=`<strong>同步到 Zotero</strong>${badge}`;
  // 未入队（如论文不存在）：如实显示原因，不得显示为同步成功
  if(resp.queued===false){box.innerHTML=`<div class="enrich-result">${head}<p class="sync-err">${esc(resp.reason||'未入队，未同步。')}</p></div>`;return;}
  const r=resp.result||{};
  const bits=[];if(typeof r.synced==='number')bits.push(`写入 ${r.synced} 条`);if(typeof r.updated_existing==='number')bits.push(`补全已有条目 ${r.updated_existing} 条`);if(typeof r.deduped==='number')bits.push(`已存在无需重复 ${r.deduped} 条`);if(typeof r.conflict==='number'&&r.conflict)bits.push(`冲突 ${r.conflict} 条`);if(typeof r.stayed_pending==='number'&&r.stayed_pending)bits.push(`保持待重试 ${r.stayed_pending} 条`);
  if(resp.flushed===false){
    // 队列已登记但本轮未写入（无凭据/服务不可达/失败待重试）：pending 语义如实展示
    const qBits=[];if(typeof resp.queue_count==='number')qBits.push(`队列已登记 ${resp.queue_count} 条`);if(resp.zotero_state==='pending')qBits.push('状态：等待 Zotero（未写入）');
    const errHtml=(Array.isArray(resp.errors)&&resp.errors.length)?`<ul class="enrich-errors">${resp.errors.map(e=>`<li class="sync-err">${esc(`#${e.paper_id??'?'} ${e.last_error||e.message||e.state||''}`.trim())}</li>`).join('')}</ul>`:'';
    box.innerHTML=`<div class="enrich-result">${head}<p>${esc(qBits.join(' · ')||'本轮未写入。')}</p>${resp.reason?`<p class="sync-err">${esc(resp.reason)}</p>`:''}${errHtml}</div>`;return;}
  box.innerHTML=`<div class="enrich-result">${head}<p>${esc(bits.length?bits.join(' · '):'队列处理完成。')}</p>${resp.last_error?`<p class="sync-err">上次错误：${esc(resp.last_error)}</p>`:''}</div>`;
}
async function detailTab(tab){state.detailTab=tab;document.querySelectorAll('[data-detail-tab]').forEach(button=>button.classList.toggle('active',button.dataset.detailTab===tab));const p=state.detail;$('detail-tab-content').innerHTML='<p class="muted">正在读取…</p>';if(tab==='overview'){const v=p.venue_info||{},d=p.discovery;let path='';if(d){let cur=d;const parts=[];let safety=0;const jobResults=state.job?.results||[];while(cur&&safety++<6){parts.unshift('#'+cur.id);cur=cur.parent?jobResults.find(x=>x.id===cur.parent):null;}path=parts.join(' → ');}
 let supportsBlock='';if(d&&Array.isArray(d.supports)&&d.supports.length){supportsBlock='<details class="supports-block"><summary>发现依据明细（前 '+Math.min(d.supports.length,10)+' 条）</summary><ul class="supports-list">'+d.supports.slice(0,10).map(s=>{const seed=s.seed==null?'未记录':'#'+s.seed,parent=s.parent==null?'未记录':'#'+s.parent;const dir=s.relation==='references'?'参考文献（父→本篇被引用）':s.relation==='citing'?'施引文献（本篇→父被引用）':'方向 '+(s.relation??'未记录');return `<li>依据文献 #${s.id??'未记录'} · 父节点 ${parent} · 种子 ${seed} · ${esc(dir)} · 边来源 ${esc(s.edge_source||'未记录')}</li>`;}).join('')+'</ul>'+(d.support_truncated?'<p class="muted">依据超过 50 条，仅保留最近记录；计数为实际存储的去重数。</p>':'')+'</details>';}else if(d&&d.depth>0){supportsBlock='<p class="muted supports-missing">本节点尚未保存可展示的发现依据。</p>';}
 $('detail-tab-content').innerHTML=`<h2>原文摘要</h2><p class="abstract">${esc(p.abstract||'本地暂无原文摘要。请核对 DOI 对应的原文，或导入摘要元数据。')}</p><h2 class="detail-sub">中文摘要</h2><div id="detail-abstract-zh"><p class="abstract">${p.abstract_zh?esc(p.abstract_zh):'本地暂无中文摘要。中文题名/摘要与总结可能来自人工录入或已有数据，来源标注见下方（若可用）。'}</p></div><div id="detail-existing-extra"></div><div class="provenance"><strong>发现与数据来源</strong><br>${path?'发现路径：'+esc(path)+'<br>':''}${d&&d.depth>0?`第 ${d.depth} 轮 · ${d.relation==='references'?'参考文献':'施引文献'} · 边来源 ${esc(d.edge_source||'未知')}<br>`:''}${typeof d?.support_count==='number'?`本节点已记录发现依据 ${d.support_count} 条${d.support_truncated?'（至少）':''}<br>`:''}书目来源：${esc(p.source||'未知')}<br>本地已知关系：参考 ${p.edges_out||0} 篇 · 施引 ${p.edges_in||0} 篇<br>指标来源：${esc(v.source||'未提供')} · 缓存更新 ${esc(v.updated_at||'未知')}<br>指标所属年份：${v.metric_year?esc(String(v.metric_year)):'指标年份未知'}。中文内容的生成或导入来源见对应标注。</div>${supportsBlock}<label style="margin-top:20px">阅读备注<textarea id="reading-note" class="detail-note" rows="3" placeholder="记录为什么值得阅读，以及需要核对的问题">${esc(p.reading_note||'')}</textarea></label><button id="save-note" class="secondary small">保存备注并加入清单</button>`;
 attachExistingBlocks(p);return;}
 const similar=tab==='similar';const data=await api(`/api/papers/${p.id}/${similar?'similar':'linked?direction='+tab+'&sort='+state.linkSort+'&limit=300'}`);if(state.detail?.id!==p.id||state.detailTab!==tab)return;const items=similar?data.results:data.papers;$('detail-tab-content').innerHTML=`<div class="linked-toolbar"><span>${similar?'网络相似候选':'本地已知文献'} ${data.total??items.length} 篇${!similar&&data.count<data.total?' · 当前最多展示 '+data.count+' 篇':''}</span>${similar?'':`<select id="detail-sort" aria-label="引用文献排序"><option value="cited">被引数由高到低</option><option value="impact_factor">影响因子由高到低</option><option value="cas_zone">中科院分区优先</option><option value="year_desc">发表时间由新到旧</option><option value="year_asc">发表时间由旧到新</option></select>`}</div>${items.map(item=>{const v=item.venue_info||{};return `<article class="linked-paper"><button class="paper-title" data-paper="${item.id}">${esc(item.title||item.title_zh)}</button><p class="paper-meta">${item.year||'年份未知'} · 被引 ${(item.cited_by_count||0).toLocaleString()} · ${esc(item.venue||'刊物未知')}</p><p class="reason">${similar?`共同参考 ${item.shared_refs} 篇 · 共同施引 ${item.shared_citers} 篇 · 分数 ${item.score}`:`IF ${esc(v.impact_factor||'未知')} · 中科院 ${esc(v.cas_zone||'未知')} · 指标来源 ${esc(v.source||'未提供')}`}</p></article>`;}).join('')||'<div class="empty">本地暂无记录；可回到工作台选择联网补充。</div>'}<p class="muted" style="margin-top:15px">${similar?'网络重叠是推荐线索，请结合摘要判断。':'已在本地取回的文献内排序；缺失指标排在最后。'}</p>`;if(!similar)$('detail-sort').value=state.linkSort;}
function originBadgeHtml(origin){const X=window.WenxianExisting;if(!X)return '';const t=X.originLabel(origin);return t?`<span class="origin-badge">${esc(t)}</span>`:'';}
/* C 线 parallel_v04：已有内容（§3.1）与 Zotero 同步状态（§3.3，按 paper_id 过滤）分区展示。
   端点失败时对应区块整体隐藏，绝不报错打断详情；产品按钮（补全缺失字段 / 同步 Zotero）
   的结果渲染见 renderEnrichResult / renderFlushResult。 */
async function attachExistingBlocks(p){
  const extra=$('detail-existing-extra');if(!extra)return;const myId=p.id;
  // 1) 已有内容来源标注（GET /api/papers/{id}/existing，B 线 seam）
  try{const r=await fetch('/api/papers/'+myId+'/existing');
    if(!r.ok||state.detail?.id!==myId||state.detailTab!=='overview')return;
    const data=await r.json();if(state.detail?.id!==myId||state.detailTab!=='overview')return;
    const X=window.WenxianExisting;
    if(X&&data&&typeof data==='object'){
      if(data.abstract_zh&&data.abstract_zh.text!=null){const badge=originBadgeHtml(data.abstract_zh.origin);if(badge){const target=document.getElementById('detail-abstract-zh');if(target)target.insertAdjacentHTML('afterbegin',badge);}}
      const sum=data.summary;
      if(sum&&sum.text)extra.insertAdjacentHTML('afterbegin',`<div class="existing-block"><h3>已有总结</h3>${originBadgeHtml(sum.origin)}<p class="abstract">${esc(sum.text)}</p></div>`);
    }
  }catch(e){/* 端点不存在：已有总结区块保持隐藏 */}
  try{const r=await fetch('/api/research/enrichment/sources/'+myId);if(r.ok&&state.detail?.id===myId&&state.detailTab==='overview'){const data=await r.json();if(data.sources?.length){const names={title:'题名',abstract:'原文摘要',authors:'作者',year:'年份',venue:'期刊',impact_factor:'IF',cas_zone:'中科院分区',jcr_zone:'JCR 分区',title_zh:'中文题名',abstract_zh:'中文摘要译文',summary:'概括'};extra.insertAdjacentHTML('beforeend','<details class="supports-block"><summary>补全信息的来源记录</summary>'+data.sources.map(x=>{let proof={};try{proof=JSON.parse(x.evidence||'{}');}catch(e){}const link=typeof x.url==='string'&&x.url.startsWith('https://')?` · <a href="${esc(x.url)}" target="_blank" rel="noopener">查看来源 ↗</a>`:'';return `<p class="paper-meta">${esc(names[x.field]||x.field)} · ${esc(x.source)} · ${esc(x.recorded_at?.slice(0,10))}${link}</p>${proof.quote?`<blockquote class="source-proof">${esc(proof.quote)}</blockquote>`:''}`;}).join('')+'</details>');}}}catch(e){}
  // 2) Zotero 同步状态（GET /api/library/sync/status?paper_id=<当前 id>，V4-07 修复：
  //    不再请求无过滤的默认前 500 条；响应按 id 校验归属，快速切换不串页）
  try{const r=await fetch(detailSyncStatusUrl(myId));
    if(!r.ok||state.detail?.id!==myId||state.detailTab!=='overview')return;
    const data=await r.json();if(state.detail?.id!==myId||state.detailTab!=='overview')return;
    const X=window.WenxianExisting;const rec=X?X.extractSyncRecord(data,p):null;
    if(rec){
      const label=rec.zotero_state?X.syncStateText(rec.zotero_state):null;
      const errLine=rec.last_error?`<div class="sync-err">上次错误：${esc(rec.last_error)}</div>`:'';
      extra.insertAdjacentHTML('beforeend',`<div class="sync-status" id="detail-sync-box"><strong>Zotero 同步状态</strong>${label?`<span class="sync-badge sync-${esc(rec.zotero_state)}">${esc(label)}</span>`:''}${rec.local_saved&&rec.zotero_state!=='synced'?'<span class="muted">已保存本地库</span>':''}${errLine}</div>`);
    }
  }catch(e){/* 端点不存在：同步状态区块保持隐藏 */}
}
async function exportSelection(){let ids=[...state.selected];if(!ids.length)ids=state.data.papers.map(p=>p.id);if(!ids.length)throw new Error('请先在列表勾选文献；阅读清单中可直接导出当前结果。');const format=$('export-format').value;const response=await fetch('/api/research/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,format,job_id:state.job?.id||''})});if(!response.ok)throw new Error('导出失败，请重试。');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=`文献罗盘_阅读清单.${format}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);toast(`已导出 ${ids.length} 篇文献`);}

document.addEventListener('click',guarded(async event=>{const target=event.target.closest('button');if(!target)return;if(target.dataset.close){$(target.dataset.close).close();return;}if(target.dataset.page){await navigate(target.dataset.page);return;}if(target.dataset.removeSeed){state.seeds.delete(Number(target.dataset.removeSeed));renderSeeds();return;}if(target.dataset.paper){await openDetail(Number(target.dataset.paper));return;}if(target.dataset.save){const id=Number(target.dataset.save),p=state.data.papers.find(x=>x.id===id);await saveReading([id],p.in_reading_list?'remove':'add');return;}if(target.dataset.detailTab){await detailTab(target.dataset.detailTab);return;}
 switch(target.id){case 'help-btn':$('help-dialog').showModal();break;case 'choose-seeds':state.pending=new Map(state.seeds);$('seed-search').value='';await seedSearch();$('seed-dialog').showModal();break;case 'confirm-seeds':state.seeds=new Map(state.pending);renderSeeds();$('seed-dialog').close();await loadResults();break;case 'run-explore':await startExploration();break;case 'cancel-job':await api('/api/research/jobs/'+state.job.id+'/cancel',{});toast('已请求停止，将在当前请求结束后生效。');break;case 'apply-query':case 'apply-filters':await loadResults();break;case 'reset-filters':resetFields();await loadResults();break;case 'ai-rank':$('sort').value='relevance';setView('list');await loadResults();break;case 'graph-view':setView('graph');break;case 'enrich-results':case 'fill-metrics':window.WenxianEnrichment?.open({ids:[...state.selected].length?[...state.selected]:state.data.papers.map(p=>p.id)});break;case 'list-view':setView('list');break;case 'fit-graph':case 'zoom-fit':fitGraph(true);break;case 'zoom-in':if(state.cy)state.cy.zoom(Math.min(state.cy.zoom()*1.25,3));break;case 'zoom-out':if(state.cy)state.cy.zoom(Math.max(state.cy.zoom()/1.25,.15));break;case 'add-selected':await saveReading([...state.selected]);break;case 'export-btn':await exportSelection();break;
 case 'detail-save':await saveReading([state.detail.id],state.detail.in_reading_list?'remove':'add');state.detail.in_reading_list=!state.detail.in_reading_list;renderDetail();break;case 'detail-ai':{const open=window.WenxianAI&&typeof window.WenxianAI.open==='function'?window.WenxianAI.open:null;if(!open){toast('AI 模块尚未集成：AI 速览为接口预留，待 B 线合入后可用。',true);break;}
  // F2 修复：原生 <dialog> 详情位于浏览器模态顶层，普通 z-index 的 AI 面板无法盖过它。
  // 方案：先关闭详情弹窗再打开 AI 面板，并把详情弹窗与 AI 入口按钮交给 AI 模块，
  // 由 AI 面板关闭时负责重新打开详情并还原焦点（返回详情流程合理、焦点不丢）。
  const aiEntry=target;const detailDialog=$('detail-dialog');
  try{if(detailDialog.open)detailDialog.close();open(state.detail.id,{returnTo:detailDialog,returnFocus:aiEntry});}
  catch(error){toast('AI 速览调用失败：'+error.message,true);if(!detailDialog.open)detailDialog.showModal();}
  break;}case 'detail-seed':if(state.seeds.size>=10&&!state.seeds.has(state.detail.id))throw new Error('最多选择 10 篇种子。');state.seeds.set(state.detail.id,state.detail);renderSeeds();toast('已加入扩展种子');break;case 'detail-promote':await api('/api/papers/promote',{ids:[state.detail.id]});state.detail.status='library';renderDetail();await refreshCounts();toast('已保存到文献库');break;case 'save-note':await saveReading([state.detail.id],'add',$('reading-note').value);state.detail.in_reading_list=true;state.detail.reading_note=$('reading-note').value;renderDetail();break;
 case 'detail-enrich':window.WenxianEnrichment?.open({ids:[state.detail.id],single:true});break;
 case 'detail-zflush':{target.disabled=true;try{// V4R1-06/R2-02：单篇同步入口 /api/library/sync/paper——普通未入队论文从本按钮发起同步（enqueue → 仅该篇 flush）；旧 /api/library/sync/flush 保留批量/重试语义，不再由本按钮调用
   const resp=await api('/api/library/sync/paper',{paper_id:state.detail.id});
   renderFlushResult(resp);await refreshDetailSyncStatus();}finally{target.disabled=false;}break;}
 case 'save-settings':await api('/api/settings',{values:{offline_mode:$('offline-mode').checked?'1':'0',zotero_enabled:$('zotero-enabled').checked?'1':'0',mailto:$('mailto').value.trim()}});await refreshCounts();toast('设置已保存');break;
 case 'import-btn':{const kind=$('import-format').value;const text=$('import-content').value.trim();if(!text)throw new Error('请先填写导入内容。');target.disabled=true;try{const body=kind==='standard'?{papers_text:text,citations_text:$('import-citations').value,venues_text:$('import-venues').value}:kind==='pdf-folder'?{path:text}:{text};const result=await api('/api/import/'+kind,body);$('import-result').hidden=false;$('import-result').textContent=JSON.stringify(result,null,2);await refreshCounts();toast('导入已处理，请核对结果中的失败或未解析条目。');}finally{target.disabled=false;}break;}
 case 'zotero-sync':target.disabled=true;try{const result=await api('/api/zotero/sync',{});toast(`Zotero 同步完成：新增 ${result.added} 条，失败 ${result.errors.length} 条`);await refreshCounts();}finally{target.disabled=false;}break;
 }}));
document.addEventListener('change',guarded(async event=>{const t=event.target;if(t.dataset.select){const id=Number(t.dataset.select);if(t.checked)state.selected.add(id);else state.selected.delete(id);updateSelection();}if(t.id==='select-all'){state.selected=new Set(t.checked?state.data.papers.map(p=>p.id):[]);document.querySelectorAll('[data-select]').forEach(box=>box.checked=t.checked);updateSelection();}if(t.dataset.seedPick){const id=Number(t.dataset.seedPick);if(t.checked){if(state.pending.size>=10){t.checked=false;throw new Error('最多选择 10 篇种子。');}state.pending.set(id,state.seedSearchRows.get(id));}else state.pending.delete(id);$('seed-picked').textContent=`已选 ${state.pending.size} / 10 篇`;}if(t.id==='zone-scheme')await loadResults();if(t.id==='graph-color'){if(t.value!=='cluster')$('sort').value=t.value==='year'?'year_desc':t.value;await loadResults();}if(t.id==='sort'){const mode=t.value.startsWith('year')?'year':t.value;if(['year','cited','impact_factor'].includes(mode))$('graph-color').value=mode;}if(['sort','scope'].includes(t.id))await loadResults();if(t.id==='detail-sort'){state.linkSort=t.value;await detailTab(state.detailTab);}}));
$('seed-search').addEventListener('input',()=>{clearTimeout(seedTimer);seedTimer=setTimeout(guarded(seedSearch),220);});$('query').addEventListener('keydown',guarded(async event=>{if(event.key==='Enter')await loadResults();}));
window.addEventListener('resize',scheduleGraph);document.querySelector('.advanced-box').addEventListener('toggle',scheduleGraph);new ResizeObserver(scheduleGraph).observe(document.querySelector('.result-toolbar'));
$('close-advanced').onclick=()=>document.querySelector('.advanced-box').open=false;
document.addEventListener('pointerdown',e=>{if(!e.target.closest('.advanced-box'))document.querySelector('.advanced-box').open=false;});
document.addEventListener('pointerdown',e=>{if($('detail-dialog').open&&!e.target.closest('#detail-dialog,.pg-dialog,.wnai-overlay,dialog[open]:not(#detail-dialog)'))closeDetail();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if($('detail-dialog').open)closeDetail();document.querySelector('.advanced-box').open=false;}});
for(const id of ['year-min','year-max'])$(id).addEventListener('input',()=>{$('year-enabled').checked=true;});$('year-max').value=currentYear;
window.addEventListener('wenxian-data-updated',guarded(async()=>{const id=$('detail-dialog').open?state.detail?.id:null;await refreshCounts();await loadResults();if(id)await openDetail(id);}));
(async()=>{try{const params=new URLSearchParams(location.search);await refreshCounts();const result=await api('/api/papers?limit=100');const defaultPaper=result.papers.find(p=>p.local_pdf_path)||result.papers[0];if(defaultPaper)state.seeds.set(defaultPaper.id,defaultPaper);renderSeeds();const jobParam=params.get('job')||localStorage.getItem('papernet_job_v2');if(jobParam){try{state.job=await api('/api/research/jobs/'+jobParam);$('scope').value=params.get('scope')||'task';}catch{localStorage.removeItem('papernet_job_v2');}}const scope=params.get('scope');if(scope&&!jobParam)$('scope').value=scope;if(params.get('sort'))$('sort').value=params.get('sort');await loadResults();if(params.get('view')==='list')setView('list');if(state.job)await pollJob();}catch(error){$('result-count').textContent='连接失败';toast('本地服务读取失败：'+error.message,true);}})();
