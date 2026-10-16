/* Independent source-frame region review. No deletion or rendering recipe. */
(() => {
  const dialog=$('mosaicDialog'), video=$('mosaicSource'), image=$('mosaicFrame'), overlay=$('mosaicOverlay');
  let context=null, epoch=0, token=0, wanted=0, ready=-1, candidates=[], selected=null, targets=[], editing=null;
  let loading=false, frameLoading=false, drawing=false, pointer=null, frameUrl=null, controller=null, timer;
  const base=()=>`/api/batches/${context.batch}/mosaics/${context.session.id}`;
  const meta=()=>context.session.metadata;
  const rate=()=>meta().fps_num/meta().fps_den;
  const active=v=>context&&dialog.open&&epoch===v;
  const key=()=>`exactcut.mosaic.targets.v1.${currentUser?.username}.${context.batch}.${context.session.id}.${context.session.source_sha256}.${context.session.reset_at||''}`;
  const status=(text,error=false)=>{$('mosaicStatus').textContent=text;$('mosaicStatus').classList.toggle('error',error);};
  const guard=fn=>(...args)=>{try{fn(...args);}catch(e){status(e.message,true);}};
  function clock(frame){const {fps_num:n,fps_den:d}=meta(),s=Math.floor(frame*d/n);return [Math.floor(s/60),s%60,frame-Math.ceil(s*n/d)].map(v=>String(v).padStart(2,'0')).join(':');}
  function parse(value){const m=/^(\d+):([0-5]\d):(\d{2})$/.exec(value.trim());if(!m)throw Error('请填写 分:秒:帧，例如 00:01:05');const s=+m[1]*60+(+m[2]),f=+m[3],{fps_num:n,fps_den:d}=meta(),first=Math.ceil(s*n/d);if(f>=Math.ceil((s+1)*n/d)-first||first+f>=meta().frame_count)throw Error('时间超出原片范围');return first+f;}
  function range(){const start=parse($('mosaicStart').value),end=parse($('mosaicEnd').value)+1;if(start>=end)throw Error('开始帧不能晚于结束帧');return {start,end};}
  function inRange(){try{const r=range();return wanted>=r.start&&wanted<r.end;}catch{return false;}}
  function save(){try{localStorage.setItem(key(),JSON.stringify(targets));return true;}catch{status('浏览器无法保存草稿，关闭后可能丢失，请保持窗口打开。',true);return false;}}
  function validBox(c){return c&&[c.x,c.y,c.w,c.h].every(Number.isInteger)&&c.x>=0&&c.y>=0&&c.w>0&&c.h>0&&c.x+c.w<=meta().width&&c.y+c.h<=meta().height;}
  function selectedBox(){return candidates.find(c=>c.id===selected);}
  function resetCandidate(){token++;loading=false;candidates=[];selected=null;editing=null;drawing=false;pointer=null;paint();}
  function paint(){
    if(!context)return;
    const c=selectedBox(),accurate=ready===wanted&&!image.hidden&&video.paused;
    $('mosaicPosition').textContent=`${clock(wanted)} / ${clock(meta().frame_count-1)}`;
    $('mosaicSeek').value=wanted;$('mosaicFrameNumber').value=clock(wanted);
    $('mosaicRecognize').disabled=loading||frameLoading||!accurate||!inRange()||!context.session.capabilities?.region_recognition;
    $('mosaicRecognize').textContent=loading?'识别中…':'识别当前帧';
    $('mosaicDraw').disabled=!accurate||loading||!inRange();$('mosaicDraw').textContent=drawing?'在画面拖出矩形':'手动框选';
    $('mosaicConfirm').disabled=!accurate||!validBox(c)||!inRange()||loading;
    $('mosaicConfirm').textContent=editing===null?'确认目标':'保存调整';
    $('mosaicBoxFields').hidden=!c;
    for(const axis of ['X','Y','W','H'])$('mosaicBox'+axis).value=c?c[axis.toLowerCase()]:'';
    $('mosaicCandidates').innerHTML=candidates.map(c=>`<button data-candidate="${esc(c.id)}" aria-pressed="${c.id===selected}"><strong>${esc(c.label)}</strong><small>${esc(c.reason)}</small></button>`).join('');
    $('mosaicTargets').innerHTML=targets.map((t,i)=>`<li><button data-target-view="${i}"><strong>${esc(t.label)} <em>待跟踪</em></strong><small>${clock(t.start)}–${clock(t.end-1)} · 参考帧 ${clock(t.seed_frame)}</small></button><button data-target-remove="${i}" aria-label="移除目标 ${i+1}">×</button></li>`).join('');
    $('mosaicTargetCount').textContent=`${targets.length} / 8`;
    $('mosaicEmpty').hidden=!!targets.length;
    $('mosaicClear').disabled=!targets.length;
    drawOverlay();
  }
  function geometry(){const rect=$('mosaicStage').getBoundingClientRect(),scale=Math.min(rect.width/meta().width,rect.height/meta().height);return {left:(rect.width-meta().width*scale)/2,top:(rect.height-meta().height*scale)/2,width:meta().width*scale,height:meta().height*scale,scale,rect};}
  function drawOverlay(){
    if(!context)return;
    overlay.hidden=ready!==wanted||image.hidden||!video.paused||(!candidates.length&&!drawing);
    if(overlay.hidden)return;
    const g=geometry();Object.assign(overlay.style,{left:g.left+'px',top:g.top+'px',width:g.width+'px',height:g.height+'px'});
    overlay.classList.toggle('drawing',drawing);
    overlay.innerHTML=candidates.map(c=>`<button class="mosaic-box ${c.id===selected?'selected':''}" data-box="${esc(c.id)}" aria-label="选择 ${esc(c.label)}" style="left:${c.x/meta().width*100}%;top:${c.y/meta().height*100}%;width:${c.w/meta().width*100}%;height:${c.h/meta().height*100}%"><span>${esc(c.label)}</span></button>`).join('');
  }
  async function loadFrame(){
    if(!context||!dialog.open||!video.paused)return;
    const version=epoch,frame=wanted,url=base()+`/frame/${frame}`;
    controller?.abort();const request=new AbortController();controller=request;frameLoading=true;paint();
    $('mosaicFrameHint').textContent='正在读取准确帧…';$('mosaicFrameRetry').hidden=true;
    try{
      const response=await fetch(appUrl(url),{signal:request.signal});if(!response.ok)throw Error('准确帧读取失败，请重试');
      const blob=await response.blob();if(!active(version)||request!==controller)return;
      const next=URL.createObjectURL(blob);image.src=next;
      try{await image.decode();}catch{URL.revokeObjectURL(next);throw Error('准确帧读取失败，请重试');}
      if(!active(version)||request!==controller||frame!==wanted||!video.paused){URL.revokeObjectURL(next);return;}
      if(frameUrl)URL.revokeObjectURL(frameUrl);frameUrl=next;ready=frame;image.hidden=false;
      $('mosaicFrameHint').textContent='准确帧 · 框仅对应当前参考帧，尚未跟踪';
    }catch(e){if(active(version)&&request===controller&&e.name!=='AbortError'){$('mosaicFrameHint').textContent=e.message;$('mosaicFrameRetry').hidden=false;}}
    finally{if(active(version)&&request===controller){frameLoading=false;paint();}}
  }
  function seek(frame,keep=false){
    if(!context)return;clearTimeout(timer);controller?.abort();video.pause();
    wanted=Math.max(0,Math.min(meta().frame_count-1,Math.round(frame)));ready=-1;image.hidden=true;
    if(!keep)resetCandidate();
    if(Number.isFinite(video.duration))video.currentTime=(wanted+.05)/rate();
    paint();timer=setTimeout(loadFrame,100);
  }
  async function recognize(){
    if($('mosaicRecognize').disabled)return;
    const version=epoch,jobToken=++token,endpoint=base(),r=range(),frame=wanted;
    const valid=()=>active(version)&&jobToken===token;
    candidates=[];selected=null;editing=null;drawing=false;loading=true;paint();status('Gemini 正在识别参考帧中的候选遮挡区域…');
    try{
      let job=await api(endpoint+'/locate',{...r,seed_frame:frame,reset_at:context.session.reset_at||''});
      while(valid()&&job.status==='running'){
        status(job.progress||'正在识别参考帧…');await new Promise(r=>setTimeout(r,1000));if(!valid())return;
        job=await api(endpoint+'/locate/'+job.id);
      }
      if(!valid())return;
      if(job.status!=='completed')throw Error(job.error||'识别未完成，请重试');
      if(job.seed_frame!==frame||job.start!==r.start||job.end!==r.end||!Array.isArray(job.candidates)||job.candidates.length>8||job.candidates.some(c=>!validBox(c)))throw Error('识别结果与当前参考帧不匹配，请重试');
      candidates=job.candidates.map(c=>({...c}));selected=candidates[0]?.id??null;
      status(candidates.length?'选中候选框，可调整位置和大小，确认后加入待跟踪目标。':'本帧未找到候选区域。可换一帧识别，或手动框选。');
    }catch(e){if(valid())status(e.message,true);}
    finally{if(valid()){loading=false;paint();}}
  }
  async function open(batch,asset){
    const version=++epoch;token++;controller?.abort();clearTimeout(timer);context=null;wanted=0;ready=-1;candidates=[];selected=null;targets=[];editing=null;loading=false;frameLoading=false;drawing=false;
    image.hidden=true;overlay.hidden=true;$('mosaicWorkspace').hidden=true;$('mosaicTitle').textContent='马赛克 · '+asset.title;
    for(const v of $('assets').querySelectorAll('video'))v.pause();
    if(!dialog.open)dialog.showModal();status('正在准备原片…');
    try{
      const session=await api(`/api/batches/${batch}/mosaics/open`,{request_id:asset.request_id,source_sha256:asset.sha256});
      if(version!==epoch||!dialog.open)return;context={batch,asset,session};
      $('mosaicStart').value=clock(0);$('mosaicEnd').value=clock(meta().frame_count-1);$('mosaicSeek').max=meta().frame_count-1;
      try{const saved=JSON.parse(localStorage.getItem(key())||'[]');if(Array.isArray(saved)&&saved.length<=8&&saved.every(t=>validBox(t)&&[t.start,t.end,t.seed_frame].every(Number.isInteger)&&t.start>=0&&t.start<=t.seed_frame&&t.seed_frame<t.end&&t.end<=meta().frame_count&&typeof t.label==='string'&&typeof t.reason==='string'))targets=saved;}catch{}
      $('mosaicWorkspace').hidden=false;video.src=appUrl(base()+'/source');video.load();
      status(session.capabilities?.region_recognition?'选好范围和参考帧后，识别需要遮挡的区域。':'本机未启用 Gemini 识别，可先手动框选目标。');
      paint();loadFrame();
    }catch(e){if(version===epoch)status(e.message,true);}
  }
  function mount(){
    if(!currentUser?.features?.cutting||!current)return;
    for(const card of $('assets').querySelectorAll('[data-asset]')){
      const asset=current.assets[Number(card.dataset.asset)],batch=current.id;
      if(!asset?.preview||asset.can_edit_video!==true){card.querySelector('[data-open-mosaic]')?.remove();continue;}
      if(card.querySelector('[data-open-mosaic]'))continue;
      const button=document.createElement('button');button.type='button';button.className='secondary mosaic-entry';button.dataset.openMosaic='';button.textContent='马赛克';
      // A sibling prevents the existing cut-tools click handler from opening both dialogs.
      const tools=card.querySelector('.cut-tools');if(tools)tools.after(button);else card.append(button);
      button.onclick=()=>open(batch,asset);
    }
  }
  $('mosaicClose').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>{if(dialog.open)return;epoch++;token++;controller?.abort();clearTimeout(timer);context=null;video.pause();video.removeAttribute('src');video.load();image.removeAttribute('src');if(frameUrl)URL.revokeObjectURL(frameUrl);frameUrl=null;});
  $('mosaicRecognize').onclick=recognize;
  $('mosaicFrameRetry').onclick=loadFrame;
  $('mosaicPrevious').onclick=()=>seek(wanted-1);$('mosaicNext').onclick=()=>seek(wanted+1);
  $('mosaicSeek').oninput=e=>seek(Number(e.target.value));
  $('mosaicFrameNumber').onchange=guard(e=>seek(parse(e.target.value)));
  for(const id of ['mosaicStart','mosaicEnd'])$(id).onchange=guard(()=>{resetCandidate();range();status(inRange()?'范围已更新，请识别参考帧。':'请将参考帧移到所选时间范围内。');paint();});
  $('mosaicMarkStart').onclick=()=>{$('mosaicStart').value=clock(wanted);$('mosaicStart').dispatchEvent(new Event('change'));};
  $('mosaicMarkEnd').onclick=()=>{$('mosaicEnd').value=clock(wanted);$('mosaicEnd').dispatchEvent(new Event('change'));};
  $('mosaicCandidates').onclick=e=>{const b=e.target.closest('[data-candidate]');if(b){if(selected!==b.dataset.candidate)editing=null;selected=b.dataset.candidate;paint();}};
  for(const axis of ['X','Y','W','H'])$('mosaicBox'+axis).onchange=guard(e=>{const c=selectedBox();if(!c)return;const next={...c,[axis.toLowerCase()]:Number(e.target.value)};if(!validBox(next)){paint();throw Error('矩形必须位于画面内，宽高至少 1 像素');}Object.assign(c,next);paint();});
  $('mosaicConfirm').onclick=guard(()=>{
    if($('mosaicConfirm').disabled)return;const c=selectedBox(),r=range();
    if(editing===null&&targets.length>=8)throw Error('最多保存 8 个待跟踪目标');
    const target={label:c.label,reason:c.reason,x:c.x,y:c.y,w:c.w,h:c.h,...r,seed_frame:wanted};
    if(editing===null){targets.push(target);editing=targets.length-1;}else targets[editing]=target;
    paint();if(save())status('目标已保存为待跟踪；当前只确认参考帧，尚未生成马赛克。');
  });
  $('mosaicTargets').onclick=e=>{
    const remove=e.target.closest('[data-target-remove]'),view=e.target.closest('[data-target-view]');
    if(remove){targets.splice(Number(remove.dataset.targetRemove),1);resetCandidate();save();return;}
    if(view){const i=Number(view.dataset.targetView),t=targets[i];resetCandidate();editing=i;candidates=[{...t,id:'saved'}];selected='saved';$('mosaicStart').value=clock(t.start);$('mosaicEnd').value=clock(t.end-1);seek(t.seed_frame,true);status('正在查看已确认的参考帧，可调整后保存。');}
  };
  $('mosaicClear').onclick=()=>{if(window.confirm('清空本机保存的马赛克目标？原片不会改变。')){targets=[];resetCandidate();save();}};
  $('mosaicDraw').onclick=()=>{if($('mosaicDraw').disabled)return;drawing=!drawing;paint();if(drawing)status('在画面内拖出矩形；也可用像素输入框微调。');};
  function point(e){const r=overlay.getBoundingClientRect();return {x:Math.max(0,Math.min(meta().width,Math.round((e.clientX-r.left)/r.width*meta().width))),y:Math.max(0,Math.min(meta().height,Math.round((e.clientY-r.top)/r.height*meta().height)))};}
  overlay.onpointerdown=e=>{
    if(!drawing){const b=e.target.closest('[data-box]');if(b){if(selected!==b.dataset.box)editing=null;selected=b.dataset.box;paint();}return;}
    e.preventDefault();const p=point(e);pointer={...p,id:e.pointerId};overlay.setPointerCapture(e.pointerId);
    // Redrawing replaces the selected candidate, preserving any other candidates.
    selected=selected??'manual';const next={id:selected,label:'手动区域',reason:'人工框选',x:p.x,y:p.y,w:1,h:1};
    candidates=candidates.filter(c=>c.id!==selected);candidates.push(next);
  };
  overlay.onpointermove=e=>{if(!pointer)return;const p=point(e),c=selectedBox();Object.assign(c,{x:Math.min(p.x,pointer.x),y:Math.min(p.y,pointer.y),w:Math.abs(p.x-pointer.x),h:Math.abs(p.y-pointer.y)});drawOverlay();};
  overlay.onpointerup=()=>{if(!pointer)return;pointer=null;drawing=false;const c=selectedBox();if(!validBox(c)){candidates=candidates.filter(v=>v!==c);selected=candidates[0]?.id??null;}paint();};
  overlay.onpointercancel=()=>{pointer=null;drawing=false;candidates=candidates.filter(validBox);selected=candidates[0]?.id??null;paint();};
  $('mosaicPlay').onclick=()=>{if(video.paused){controller?.abort();resetCandidate();ready=-1;image.hidden=true;overlay.hidden=true;video.play().catch(()=>status('暂时无法播放，请重试',true));}else{video.pause();wanted=Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));paint();loadFrame();}};
  video.onplay=()=>{$('mosaicPlay').textContent='暂停';image.hidden=true;overlay.hidden=true;paint();};
  video.onpause=()=>{$('mosaicPlay').textContent='播放';};
  video.ontimeupdate=()=>{if(context&&!video.paused){wanted=Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));paint();}};
  video.onended=()=>{if(context){wanted=Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));paint();loadFrame();}};
  new ResizeObserver(drawOverlay).observe($('mosaicStage'));
  window.Mosaic={mount};mount();
})();
