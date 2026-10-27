/* Shared color scale for graph nodes, the legend and display preferences. */
((root)=>{'use strict';
const DEFAULT='#53a6cc',KEY='wenxian_graph_base_v1';
const PRESETS=[['海蓝','#53a6cc'],['青绿','#42b8a4'],['紫罗兰','#9a83e8'],['玫瑰','#df829f'],['琥珀','#d8a552'],['珊瑚','#e28565']];
const CATEGORIES=['#549c99','#7798b7','#c6a36d','#8c8cb0','#9bac89','#b98f93'];
const valid=value=>typeof value==='string'&&/^#[0-9a-f]{6}$/i.test(value);
function stops(hex=DEFAULT){
 if(!valid(hex))hex=DEFAULT;
 const rgb=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255),max=Math.max(...rgb),min=Math.min(...rgb),d=max-min,l=(max+min)/2;
 let h=0,s=d===0?0:d/(1-Math.abs(2*l-1));
 if(d){h=max===rgb[0]?((rgb[1]-rgb[2])/d)%6:max===rgb[1]?(rgb[2]-rgb[0])/d+2:(rgb[0]-rgb[1])/d+4;h=((h*60)%360+360)%360;}
 function at(light){const c=(1-Math.abs(2*light-1))*s,x=c*(1-Math.abs((h/60)%2-1)),m=light-c/2;
  const a=h<60?[c,x,0]:h<120?[x,c,0]:h<180?[0,c,x]:h<240?[0,x,c]:h<300?[x,0,c]:[c,0,x];return a.map(v=>Math.round((v+m)*255));}
 return [at(.28),at(.54),at(.82)];
}
const css=rgb=>'rgb('+rgb.join(',')+')';
const gradient=hex=>'linear-gradient(90deg,'+stops(hex).map(css).join(',')+')';
let base=DEFAULT,lastScale=null,panel=null;
try{const saved=root.localStorage?.getItem(KEY);if(valid(saved))base=saved.toLowerCase();}catch(e){}
function createScale(values,mode,hex=base){
 const known=values.filter(v=>typeof v==='number'&&Number.isFinite(v)&&(mode!=='cited'||v>=0));
 const lo=known.length?Math.min(...known):null,hi=known.length?Math.max(...known):null,colors=stops(hex);
 return {mode,lo,hi,gradient:gradient(hex),color(value){
  if(typeof value!=='number'||!Number.isFinite(value)||lo===null||(mode==='cited'&&value<0))return '#68778a';
  let t=hi===lo ? .65 : mode==='cited'?(Math.log1p(value)-Math.log1p(lo))/(Math.log1p(hi)-Math.log1p(lo)):(value-lo)/(hi-lo);
  t=Math.max(0,Math.min(1,t));const i=t<.5?0:1,f=t<.5?t*2:(t-.5)*2;return css(colors[i].map((v,j)=>Math.round(v+(colors[i+1][j]-v)*f)));
 }};
}
function setBase(value){
 if(!valid(value))return false;base=value.toLowerCase();try{root.localStorage?.setItem(KEY,base);}catch(e){}
 sync();root.dispatchEvent?.(new Event('wenxian-graph-palette-changed'));return true;
}
function sync(){
 const doc=root.document;if(!doc)return;doc.documentElement.style.setProperty('--graph-gradient',gradient(base));if(!panel)return;
 const mode=doc.getElementById('graph-color')?.value||'cited',cluster=mode==='cluster';panel.hidden=doc.getElementById('graph-wrap')?.hidden===true;
 const bar=panel.querySelector('.node-color-preview');bar.style.background=cluster?'linear-gradient(90deg,'+CATEGORIES.flatMap((c,i)=>[c+' '+i/6*100+'%',c+' '+(i+1)/6*100+'%']).join(',')+')':gradient(base);
 bar.setAttribute('aria-label',cluster?'网络分组分类色':'节点颜色由深到浅，表示数值由低到高');
 const bounds=panel.querySelector('.node-color-bounds'),known=lastScale?.mode===mode&&lastScale.lo!==null;
 const format=n=>Number.isInteger(n)?String(n):String(Math.round(n*100)/100);
 bounds.replaceChildren();const left=doc.createElement('span'),right=doc.createElement('span');
 left.textContent=cluster?'不同颜色代表不同分组':known?format(lastScale.lo):mode==='year'?'较早':'较低';
 right.textContent=cluster?'':known?format(lastScale.hi):mode==='year'?'较近':'较高';bounds.append(left,right);
 panel.querySelector('.node-color-picker').hidden=cluster;
 for(const b of panel.querySelectorAll('[data-node-base]'))b.setAttribute('aria-pressed',String(b.dataset.nodeBase===base));
 const input=panel.querySelector('input[type=color]');input.value=base;input.title='自定义基础色：'+base;
}
function mount(){
 const doc=root.document,field=doc.querySelector('.graph-color-control');if(!field||panel)return;
 panel=doc.createElement('section');panel.id='node-color-palette';panel.className='node-color-palette';panel.setAttribute('aria-label','节点配色');
 panel.innerHTML='<div class="node-color-preview" role="img"></div><div class="node-color-bounds"></div><div class="node-color-picker"><div class="node-color-picker-heading"><span>基础色</span><label class="node-custom-color" title="打开色谱选择颜色"><input id="node-base-custom" type="color" aria-label="自定义节点基础色"><span>自定义</span></label></div><div class="node-color-swatches" role="group" aria-label="常用节点颜色"></div></div>';
 const swatches=panel.querySelector('.node-color-swatches');
 for(const [name,color] of PRESETS){const b=doc.createElement('button');b.type='button';b.dataset.nodeBase=color;b.style.setProperty('--swatch-color',color);b.setAttribute('aria-label',name);b.title=name;b.addEventListener('click',()=>setBase(color));swatches.append(b);}
 const input=panel.querySelector('input');input.addEventListener('input',()=>setBase(input.value));field.after(panel);
 for(const event of ['wenxian-view-changed','wenxian-results-rendered','wenxian-display-state'])root.addEventListener(event,sync);
 doc.getElementById('graph-color').addEventListener('change',sync);doc.getElementById('view-options').addEventListener('toggle',sync);sync();
}
const api={DEFAULT,PRESETS,CATEGORIES,stops,gradient,createScale,setBase,getBase:()=>base,updateScale:scale=>{lastScale=scale;sync();}};
root.WenxianGraphColors=api;if(typeof module!=='undefined'&&module.exports)module.exports=api;
if(root.document){root.document.addEventListener('DOMContentLoaded',mount,{once:true});}
})(typeof window==='undefined'?globalThis:window);
