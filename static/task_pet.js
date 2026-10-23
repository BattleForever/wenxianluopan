/* The task companion stays local. Dragging never starts a task or opens a dialog. */
(() => {
 'use strict';
 const dock=document.getElementById('background-task-dock'),orb=document.getElementById('task-orb');
 if(!dock||!orb)return;
 const KEY='wenxian_pet_position_v1',card=dock.querySelector('.task-dock-card');
 const image=document.createElement('img');image.src='/brand/book-spirit-v2.png';image.alt='';image.draggable=false;image.className='task-pet-image';image.setAttribute('aria-hidden','true');orb.prepend(image);
 dock.classList.add('task-pet');orb.title='小书灵 · 按住拖动，点击查看任务；方向键也可移动';
 const hint=document.createElement('span');hint.id='task-pet-hint';hint.className='pet-sr-only';hint.textContent='小书灵。按住可拖动，使用方向键移动，点击或按回车查看任务。';dock.append(hint);orb.setAttribute('aria-describedby',hint.id);
 let ratio={x:1,y:.84},drag=null,suppressUntil=0;
 try{const value=JSON.parse(localStorage.getItem(KEY)||'null');if(value&&Number.isFinite(value.x)&&Number.isFinite(value.y)&&value.x>=0&&value.x<=1&&value.y>=0&&value.y<=1)ratio=value;}catch(e){}
 const clamp=(value,min,max)=>Math.max(min,Math.min(value,Math.max(min,max)));
 function bounds(){return {w:dock.offsetWidth||100,h:dock.offsetHeight||104,vw:innerWidth,vh:innerHeight};}
 function place(x,y,save=false){
  const b=bounds(),left=clamp(x,8,b.vw-b.w-8),top=clamp(y,8,b.vh-b.h-8);
  dock.style.left=left+'px';dock.style.top=top+'px';dock.style.right='auto';dock.style.bottom='auto';
  if(save){ratio={x:(left-8)/Math.max(1,b.vw-b.w-16),y:(top-8)/Math.max(1,b.vh-b.h-16)};try{localStorage.setItem(KEY,JSON.stringify(ratio));}catch(e){}}
  const cw=card.offsetWidth||272,ch=card.offsetHeight||160;
  card.style.left=clamp(left+b.w-cw,8,b.vw-cw-8)+'px';
  card.style.top=clamp(top>=ch+16?top-ch-8:top+b.h+8,8,b.vh-ch-8)+'px';
 }
 function restore(){const b=bounds();place(8+ratio.x*Math.max(1,b.vw-b.w-16),8+ratio.y*Math.max(1,b.vh-b.h-16));}
 orb.addEventListener('pointerdown',e=>{if(e.button!==0||e.isPrimary===false)return;const r=dock.getBoundingClientRect();drag={id:e.pointerId,x:e.clientX,y:e.clientY,left:r.left,top:r.top,moved:false};orb.setPointerCapture(e.pointerId);});
 orb.addEventListener('pointermove',e=>{if(!drag||drag.id!==e.pointerId)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.hypot(dx,dy)>6)drag.moved=true;if(drag.moved){dock.classList.add('pet-dragging');place(drag.left+dx,drag.top+dy);}});
 function finish(e){if(!drag||drag.id!==e.pointerId)return;const moved=drag.moved;drag=null;dock.classList.remove('pet-dragging');if(orb.hasPointerCapture(e.pointerId))orb.releasePointerCapture(e.pointerId);if(moved){suppressUntil=performance.now()+450;const r=dock.getBoundingClientRect();place(r.left,r.top,true);orb.blur();}}
 orb.addEventListener('pointerup',finish);orb.addEventListener('pointercancel',finish);orb.addEventListener('lostpointercapture',finish);
 orb.addEventListener('click',e=>{if(performance.now()<suppressUntil){e.preventDefault();e.stopImmediatePropagation();}},true);
 orb.addEventListener('keydown',e=>{const delta={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[e.key];if(!delta)return;e.preventDefault();const r=dock.getBoundingClientRect(),step=e.shiftKey?32:12;place(r.left+delta[0]*step,r.top+delta[1]*step,true);});
 const value=dock.querySelector('.task-orb-value');
 function status(){dock.classList.toggle('pet-has-status',!['•','0%'].includes(value.textContent));if(!drag)restore();}
 new MutationObserver(status).observe(value,{childList:true,characterData:true,subtree:true});
 window.addEventListener('resize',restore);image.addEventListener('load',restore);restore();status();
})();
