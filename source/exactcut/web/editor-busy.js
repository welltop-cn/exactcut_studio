/* Opt-in retries only for explicit, pre-admission editor resource conflicts. */
window.EditorBusy=(()=>{
  const cancelled=()=>new DOMException('已取消等待','AbortError');
  function error(response,body={}){
    if(!body||typeof body!=='object')body={};
    const info=body.error_info;
    const result=Error(info?`${info.title}。${info.action}\n${info.detail}`:body.error||`请求失败 (${response.status})`);
    result.status=response.status;
    if(typeof body.code==='string')result.code=body.code;
    result.retryable=body.retryable===true;
    if(['frame','compute','scan'].includes(body.resource))result.resource=body.resource;
    const header=response.headers.get('Retry-After');
    const seconds=header!==null&&/^\d+(\.\d+)?$/.test(header)?Number(header)*1000:0;
    result.retryAfterMs=Math.max(seconds,Number.isFinite(body.retry_after_ms)&&body.retry_after_ms>=0?body.retry_after_ms:0);
    return result;
  }
  async function frame(url,signal){
    const response=await fetch(url,{signal});
    if(!response.ok){let body={};try{body=await response.json()}catch{}throw error(response,body)}
    return response;
  }
  async function run(send,{signal,valid=()=>true,maxRetries=3,onWait=()=>{},onSend=()=>{},diagnostics={}}={}){
    let deadline=null,last;
    const report=action=>{try{window.reportEditorRetry?.({batch_id:diagnostics.batch,request_id:diagnostics.request,session_id:diagnostics.session,action,source:last?.resource,status:last?.status,duration_ms:deadline===null?0:Date.now()-(deadline-15000)});}catch{}};
    const live=()=>!signal?.aborted&&valid()&&!document.hidden;
    const check=()=>{if(!live())throw cancelled()};
    for(let attempt=0;;attempt++){
      check();if(deadline!==null&&Date.now()>=deadline)throw last;
      onSend(attempt);
      try{const result=await send();if(deadline!==null)report('retry_recovered');return result}catch(e){
        last=e;
        if(e.status!==429||e.code!=='editor_resource_busy'||e.retryable!==true||!['frame','compute','scan'].includes(e.resource))throw e;
        check();deadline??=Date.now()+15000;
        const delay=Math.max(1000*2**attempt+Math.floor(Math.random()*251),e.retryAfterMs||0);
        if(attempt>=maxRetries||Date.now()+delay>=deadline){report('retry_exhausted');throw e;}
        onWait(attempt+1,maxRetries);
        report('retry_scheduled');
        await new Promise((resolve,reject)=>{
          const finish=err=>{clearTimeout(timer);signal?.removeEventListener('abort',abort);document.removeEventListener('visibilitychange',hidden);err?reject(err):resolve()};
          const abort=()=>finish(cancelled()),hidden=()=>{if(document.hidden)abort()};
          const timer=setTimeout(()=>finish(),delay);
          signal?.addEventListener('abort',abort,{once:true});document.addEventListener('visibilitychange',hidden);if(!live())abort();
        });
      }
    }
  }
  return {run,error,frame};
})();
