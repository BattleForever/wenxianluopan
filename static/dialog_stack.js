/* In-dialog navigation keeps DOM state and closes one layer at a time. */
(()=>{'use strict';const frames=new WeakMap();
function focusSelector(el){if(!el)return null;if(el.id)return '#'+CSS.escape(el.id);const attr=[...el.attributes].find(a=>a.name.startsWith('data-'));return attr?'['+attr.name+'="'+CSS.escape(attr.value)+'"]':null;}
function push(dialog,restore){const list=frames.get(dialog)||[];list.push({nodes:[...dialog.childNodes].filter(n=>n.id!=='background-task-dock'),scroll:dialog.scrollTop,focus:dialog.contains(document.activeElement)?document.activeElement:null,focusSelector:focusSelector(document.activeElement),restore});frames.set(dialog,list);}
function back(dialog){const list=frames.get(dialog),frame=list?.pop();if(!frame)return false;const pet=dialog.querySelector('#background-task-dock');if(pet)document.body.append(pet);dialog.replaceChildren(...frame.nodes);if(!dialog.open)dialog.showModal();frame.restore?.();dialog.scrollTop=frame.scroll;const target=frame.focus?.isConnected?frame.focus:frame.focusSelector?dialog.querySelector(frame.focusSelector):null;target?.focus({preventScroll:true});return true;}
function close(dialog){if(!back(dialog))dialog.close();}
function reset(dialog){frames.delete(dialog);}
window.WenxianDialogs={push,back,close,reset,depth:dialog=>frames.get(dialog)?.length||0};
})();
