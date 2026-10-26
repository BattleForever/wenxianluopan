/* Keep transport diagnostics out of product copy; preserve a local reference. */
(()=>{'use strict';
async function read(response,fallback='本次请求未完成，请稍后重试。'){
 let data=null;try{const text=await response.text();if(text.trim())data=JSON.parse(text);}catch(e){if(e.name==='AbortError')throw e;}
 if(!response.ok){let message=typeof data?.detail==='string'?data.detail:response.status===503?'服务暂时繁忙，请稍后重试。':response.status===401||response.status===403?'当前资源无法访问，请检查连接或访问权限。':fallback;if(data?.error_id&&/^[a-f0-9]{12}$/.test(data.error_id))message+=' 诊断编号：'+data.error_id;const error=Error(message);error.status=response.status;throw error;}
 if(data===null||typeof data!=='object')throw Error('服务返回的内容异常，请重试；若持续出现，请检查本地运行记录。');
 return data;
}
window.WenxianHTTP={read};
})();
