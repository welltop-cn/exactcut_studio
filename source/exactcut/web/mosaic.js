/* Independent source-frame region review. No deletion or rendering recipe. */
(() => {
  const dialog=$('mosaicDialog'), video=$('mosaicSource'), image=$('mosaicFrame'), overlay=$('mosaicOverlay');
  let context=null, epoch=0, token=0, wanted=0, ready=-1, candidates=[], selected=null, targets=[], editing=null;
  let motion='fixed',regionDrafts=[],scanReadPending=false,scanSubmitting=false,legacyDraft=false;
  const legacyDraftHint='旧草稿已按当前规则改为固定位置、标准强度、不扩大范围；请播放检查后再生成。';
  const trackingUnavailableHint='跟随目标当前不可用；请启用目标跟踪，或在编辑中明确改为固定位置并保存。';
  let loading=false, frameLoading=false, drawing=false, pointer=null, frameUrl=null, controller=null, timer;
  let batchJob=null,batchToken=0,batchLoading=false,batchError="";
  let trackToken=0,clipBounds=null;
  let deletions=[],savedDeletions=[],oldDraftRaw=null,mutationBusy=false,resultBefore=false,resultMode=false;
  let stopPending=false,previewOperation=null;
  let playAttempt=0,playbackError=false,statusMessage='',statusError=false;
  let sourceManager=null,sourceBinding=null,beforeBinding=null;
  const pendingJob=(job=batchJob)=>['queued','running','cancelling'].includes(job?.status);
  const awaitingDecision=(job=batchJob)=>job?.status==='completed'&&!job?.discarded_at&&job?.replacement?.status!=='completed';
  const editLocked=()=>batchLoading||pendingJob()||awaitingDecision()||stopPending||mutationBusy;
  const defaultEffect={style:'pixel',strength:'medium',expansion:'none'};
  let effect={...defaultEffect},savedEffect={...defaultEffect},clipEffects={},savedClipEffects={};
  let removedClip=null,removeTimer;
  function clearRemovedClip(){removedClip=null;clearTimeout(removeTimer);$('mosaicRemoved').hidden=true;}
  let manualClips=[],savedManualClips=[],addingClip=false,manualAction='delete';
  let aiEvents=[],clipEdits={},savedAiEvents=[],savedClipEdits={};
  const allEvents=()=>[...(aiEvents.length?aiEvents:scanReport?.status==='running'?(scanReport.events||[]):[]),...manualClips];
  let scanController=null, releasePreparation=null, pendingFrame=-1;
  let scanToken=0, scanLoading=false, scanReport=null, scanError='', scanEvent=null;
  const base=()=>`/api/batches/${context.batch}/mosaics/${context.session.id}`;
  const meta=()=>context.session.metadata;
  const rate=()=>meta().fps_num/meta().fps_den;
  const active=v=>context&&dialog.open&&epoch===v;
  const key=()=>`exactcut.mosaic.targets.v1.${currentUser?.username}.${context.batch}.${context.session.id}.${context.session.source_sha256}.${context.session.reset_at||''}`;
  const regionId=()=>Array.from(crypto.getRandomValues(new Uint32Array(4)),n=>n.toString(16).padStart(8,'0')).join('');
  let savedRaw=null,legacyRaw=null,savedTargets=[];
  const oldDraftKey=()=>key().replace('targets.v1.','workbench.v2.');
  const draftKey=()=>key().replace('targets.v1.','workbench.v3.');
  const activeCount=()=>targets.filter(t=>t.adopted!==false).length;
  const belongs=(t,e)=>t.eventId||e.id.startsWith('manual-')?t.eventId===e.id:(t.originStart??t.start)===e.start&&(t.originEnd??t.end)===e.end;
  const validTarget=t=>validBox(t)&&(t.motion===undefined||['fixed','tracked'].includes(t.motion))&&[t.start,t.end,t.seed_frame].every(Number.isInteger)&&t.start>=0&&t.start<=t.seed_frame&&t.seed_frame<t.end&&t.end<=meta().frame_count&&typeof t.label==='string'&&typeof t.reason==='string'&&(t.adopted===undefined||typeof t.adopted==='boolean');
  function loadDraft(){
    savedRaw=localStorage.getItem(draftKey());legacyRaw=localStorage.getItem(key());oldDraftRaw=localStorage.getItem(oldDraftKey());
    const draft=savedRaw===null?null:JSON.parse(savedRaw);
    const data=draft?draft.targets:JSON.parse(oldDraftRaw||legacyRaw||'[]');
    aiEvents=structuredClone(draft?.aiEvents||[]);clipEdits=structuredClone(draft?.clipEdits||{});
    if(!clipEdits||Array.isArray(clipEdits)||typeof clipEdits!=='object'||!Array.isArray(aiEvents)||aiEvents.length>80)throw Error('片段草稿格式无效');
    if(aiEvents.length)checkedReport({status:'completed',source_sha256:context.session.source_sha256,reset_at:context.session.reset_at||'',events:aiEvents});
    for(const r of Object.values(clipEdits))if(!r||![r.start,r.end].every(Number.isInteger)||r.start<0||r.start>=r.end||r.end>meta().frame_count)throw Error('片段时间草稿无效');
    savedAiEvents=structuredClone(aiEvents);savedClipEdits=structuredClone(clipEdits);
    effect=simplified(draft?.effect);savedEffect={...effect};
    clipEffects=Object.fromEntries(Object.entries(draft?.clipEffects||{}).map(([k,e])=>[k,simplified(e)]));savedClipEffects=structuredClone(clipEffects);
    manualClips=draft?.manualClips||[];
    if(!Array.isArray(manualClips)||manualClips.length>40||manualClips.some(e=>typeof e.id!=='string'||!e.id.startsWith('manual-')||![e.start,e.end].every(Number.isInteger)||e.start<0||e.start>=e.end||e.end>meta().frame_count||e.status!=='needs_review'||!Array.isArray(e.candidates)||e.candidates.length))throw Error('手动片段草稿格式无效');
    savedManualClips=structuredClone(manualClips);
    deletions=draft?.deletions||[];
    if(!Array.isArray(deletions)||deletions.length>40||deletions.some(t=>![t.start,t.end].every(Number.isInteger)||t.start<0||t.start>=t.end||t.end>meta().frame_count))throw Error('本机删除草稿格式无效');
    savedDeletions=structuredClone(deletions);
    if(!Array.isArray(data)||data.length>320||!data.every(validTarget)||data.filter(t=>t.adopted!==false).length>8)throw Error('本机草稿格式无效，未覆盖原数据。');
    // Fixed-only mode: older drafts may hold tracked regions or extra effect options.
    // Convert them in memory and say so; a result rendered from the old plan stays unreplaceable (see samePlan).
    legacyDraft=data.some(t=>t.adopted!==false&&t.motion!=='fixed')||[draft?.effect,...Object.values(draft?.clipEffects||{})].some(e=>e&&!simpleEffect(e));
    targets=data.map(t=>({...t,motion:'fixed',uid:t.uid||regionId(),adopted:t.adopted!==false}));
    savedTargets=structuredClone(targets);
  }
  function pausePlayback(){sourceBinding?.pause();playAttempt++;video.pause();$('mosaicPlay').textContent='播放';}
  function playSource(){if(sourceBinding)sourceBinding.play();else playDirect();}
  function playDirect(){
    if(!context||!dialog.open)return;
    const attempt=++playAttempt,version=epoch,playback=context;
    return video.play().catch(error=>{
      window.reportMediaPlayFailure?.(video,error,playback);
      if(attempt!==playAttempt||!active(version)||context!==playback)return;
      sourceBinding?.rejected();playbackError=error.name||true;paintStatus();
    });
  }
  video.addEventListener('playing',()=>{
    if(context&&dialog.open&&!video.paused&&playbackError){playbackError=false;paintStatus();}
  });
  function paintStatus(){
    // Playback failures and workflow messages have independent lifetimes.
    $('mosaicStatus').textContent=[statusMessage,playbackError==='NotAllowedError'?'点击播放以继续':playbackError?'暂时无法播放，请重试':''].filter(Boolean).join('\n');
    $('mosaicStatus').classList.toggle('error',statusError||playbackError);
  }
  function status(message,error=false){statusMessage=error?EditorBusy.message(message,'视频处理未完成，请重试；原成片保留'):message;statusError=error;paintStatus();}
  const guard=fn=>(...args)=>{try{return fn(...args);}catch(e){status(e.message,true);}};
  function clock(frame){const {fps_num:n,fps_den:d}=meta(),s=Math.floor(frame*d/n);return [Math.floor(s/60),s%60,frame-Math.ceil(s*n/d)].map(v=>String(v).padStart(2,'0')).join(':');}
  function parse(value){const m=/^(\d+):([0-5]\d):(\d{2})$/.exec(value.trim());if(!m)throw Error('请填写 分:秒:帧，例如 00:01:05');const s=+m[1]*60+(+m[2]),f=+m[3],{fps_num:n,fps_den:d}=meta(),first=Math.ceil(s*n/d);if(f>=Math.ceil((s+1)*n/d)-first||first+f>=meta().frame_count)throw Error('时间超出原片范围');return first+f;}
  function range(){const start=parse($('mosaicStart').value),end=parse($('mosaicEnd').value)+1;if(start>=end)throw Error('开始帧不能晚于结束帧');return {start,end};}
  function inRange(){try{const r=range();return wanted>=r.start&&wanted<r.end;}catch{return false;}}
  function save(){
    clearRemovedClip();
    try{
      if(localStorage.getItem(draftKey())!==savedRaw||localStorage.getItem(key())!==legacyRaw||localStorage.getItem(oldDraftKey())!==oldDraftRaw)throw Error('草稿已在其他页面更新，请关闭后重新进入，避免覆盖。');
      const value=JSON.stringify({schema_version:1,targets,deletions,effect,clipEffects,manualClips,aiEvents,clipEdits});localStorage.setItem(draftKey(),value);
      savedRaw=value;savedAiEvents=structuredClone(aiEvents);savedClipEdits=structuredClone(clipEdits);savedManualClips=structuredClone(manualClips);savedClipEffects=structuredClone(clipEffects);savedEffect={...effect};savedTargets=structuredClone(targets);savedDeletions=structuredClone(deletions);return true;
    }catch(e){aiEvents=structuredClone(savedAiEvents);clipEdits=structuredClone(savedClipEdits);manualClips=structuredClone(savedManualClips);clipEffects=structuredClone(savedClipEffects);effect={...savedEffect};targets=structuredClone(savedTargets);deletions=structuredClone(savedDeletions);editing=null;paint();status(e.message||'无法保存草稿，请保持窗口打开。',true);return false;}
  }
  function validBox(c){return c&&[c.x,c.y,c.w,c.h].every(Number.isInteger)&&c.x>=0&&c.y>=0&&c.w>0&&c.h>0&&c.x+c.w<=meta().width&&c.y+c.h<=meta().height;}
  function selectRegion(id){selected=id;const c=selectedBox();editing=c?.uid?targets.findIndex(t=>t.uid===c.uid):null;if(editing===-1)editing=null;motion='fixed';}
  function selectedBox(){return candidates.find(c=>c.id===selected);}
  let choosingMosaic=false;
  function resetCandidate(){choosingMosaic=false;regionDrafts=[];token++;loading=false;candidates=[];selected=null;editing=null;drawing=false;pointer=null;scanEvent=null;paint();}
  function motionHint(){return '固定位置 · 所选时间内保持此位置';}
  function paint(){
    if(!context)return;
    for(const el of dialog.querySelectorAll('[data-generation-lock]')){el.disabled=el.dataset.generationLock==='true';delete el.dataset.generationLock;}
    const c=selectedBox(),accurate=ready===wanted&&!image.hidden&&video.paused;
    $('mosaicPosition').textContent=`${clock(wanted)} / ${clock(meta().frame_count-1)}`;
    $('mosaicSeek').value=wanted;$('mosaicFrameNumber').value=clock(wanted);
    $('mosaicRecognize').disabled=loading||scanLoading||frameLoading||!accurate||!inRange()||!context.session.capabilities?.region_recognition;
    $('mosaicRecognize').textContent=loading?'识别中…':'识别当前帧';
    $('mosaicDraw').disabled=!accurate||loading||!inRange();$('mosaicDraw').textContent=drawing?'在画面拖出矩形':'手动框选';
    $('mosaicAddRegion').disabled=!accurate||loading||!inRange()||activeCount()+(addingClip?candidates.length:0)>=8;
    $('mosaicRemoveRegion').hidden=!c;
    $('mosaicRemoveRegion').disabled=loading||!accurate;
    $('mosaicConfirm').disabled=loading||(!scanEvent&&(!accurate||!validBox(c)||!inRange()));
    $('mosaicConfirm').textContent='保存修改';
    $('mosaicTrack').hidden=motion==='fixed'||!context.session.capabilities?.tracking||editing===null||targets[editing]?.adopted===false;
    $('mosaicBoxFields').hidden=!c;
    $('mosaicReview').hidden=false;
    $('mosaicConfirmNext').disabled=$('mosaicConfirm').disabled||!scanEvent;
    const eventIndex=allEvents().findIndex(e=>e.id===scanEvent);
    $('mosaicSaveDelete').hidden=addingClip||eventIndex<0;
    $('mosaicSaveTime').hidden=addingClip||eventIndex<0;
    $('mosaicManualOptions').hidden=!addingClip;
    $('mosaicAddSubmit').hidden=!addingClip;$('mosaicConfirm').hidden=addingClip;
    $('mosaicAddSubmit').disabled=editLocked()||(manualAction==='mosaic'&&$('mosaicConfirm').disabled);
    $('mosaicManualAction').value=manualAction;$('mosaicMotion').value=motion;
    if(accurate)$('mosaicFrameHint').textContent=motionHint();
    const mosaicChosen=addingClip?manualAction==='mosaic':choosingMosaic||!!selectedLiveTarget()||(!scanEvent&&editing===null&&$('mosaicAdjust').open);
    $('mosaicAdjust').classList.toggle('manual-delete',!mosaicChosen);
    $('mosaicPrevEvent').disabled=eventIndex<=0;
    $('mosaicNextEvent').disabled=eventIndex<0||eventIndex>=(allEvents().length||0)-1;
    $('mosaicReviewInfo').textContent=eventIndex>=0?`片段 ${eventIndex+1} / ${allEvents().length}`:'点击左侧片段查看';
    for(const axis of ['X','Y','W','H'])$('mosaicBox'+axis).value=c?c[axis.toLowerCase()]:'';
    $('mosaicCandidates').innerHTML=candidates.map((c,i)=>`<button data-candidate="${esc(c.id)}" aria-pressed="${c.id===selected}">区域 ${i+1}</button>`).join('');
    const currentEvent=allEvents().find(e=>e.id===scanEvent);
    const savedRegions=currentEvent?targets.map((t,i)=>({t,i})).filter(({t})=>belongs(t,currentEvent)&&!t.removed):[];
    if(savedRegions.length>1&&savedRegions.some(({t})=>!candidates.some(c=>c.uid===t.uid)))$('mosaicCandidates').innerHTML=savedRegions.map(({t,i})=>`<button data-saved-adjust="${i}" aria-pressed="${i===editing}">区域 ${savedRegions.findIndex(v=>v.i===i)+1}</button>`).join('');
    $('mosaicTargetCount').textContent=String(allEvents().length||targets.length);
    dialog.classList.toggle('adjusting',$('mosaicAdjust').open);
    $('mosaicEmpty').hidden=!!targets.length;
    $('mosaicClear').disabled=!activeCount()&&!deletions.length;
    paintEffects();paintScan();paintBatch();
    drawOverlay();paintTimeline();paintLiveControls();
  }
  const mutationSelector='[data-restore-clip],.mosaic-card-checks button,[data-remove-clip],#mosaicUndoRemove,#mosaicAddClip,#mosaicClear,#mosaicScan,#mosaicAdjustToggle,#mosaicAdjust button,#mosaicAdjust input,#mosaicAdjust select,#mosaicEffects select,#mosaicEffectSave,#mosaicEffectPreview,[data-effect-target]';
  for(const type of ['click','change','input'])dialog.addEventListener(type,e=>{
    if(editLocked()&&e.target.closest(mutationSelector)){e.preventDefault();e.stopImmediatePropagation();}
  },true);
  const clipKey=t=>t?(t.eventId||`${t.originStart??t.start}:${t.originEnd??t.end}`):null;
  const simplified=e=>({...defaultEffect,style:['blur','soft_blur'].includes(e?.style)?'blur':'pixel'});
  const simpleEffect=e=>['pixel','blur'].includes(e.style??defaultEffect.style)&&(e.strength??defaultEffect.strength)===defaultEffect.strength&&(e.expansion??defaultEffect.expansion)===defaultEffect.expansion;
  const legacyJob=job=>(job?.targets||[]).some(t=>t.motion!=='fixed'||!simpleEffect({...job.effect,...t.effect}));
  const effectFor=t=>simplified({...effect,...clipEffects[clipKey(t)]});
  let effectEditKey=null,effectDraft=null;
  function paintEffects(){
    $('mosaicEffectPreview').disabled=editLocked()||(!context?.session.capabilities?.tracking&&targets.some(t=>clipKey(t)===effectEditKey&&t.adopted!==false&&t.motion!=='fixed'));
    if(!effectDraft)return;
    for(const k of ['style','strength','expansion'])$('mosaicEffect'+k[0].toUpperCase()+k.slice(1)).value=effectDraft[k];
    $('mosaicStrengthRow').hidden=effectDraft.style==='solid';
  }
  function openEffects(index){
    const target=targets[index];if(editLocked()||!target||target.adopted===false)return;
    effectEditKey=clipKey(target);effectDraft=effectFor(target);
    $('mosaicEffectTitle').textContent=`本片段效果 · ${clock(target.start)}–${clock(target.end-1)}`;
    paintEffects();$('mosaicEffects').showModal();
  }
  for(const k of ['style','strength','expansion'])$('mosaicEffect'+k[0].toUpperCase()+k.slice(1)).onchange=()=>{
    if(editLocked()||!effectDraft)return;
    effectDraft[k]=$('mosaicEffect'+k[0].toUpperCase()+k.slice(1)).value;paintEffects();
  };
  $('mosaicEffectCancel').onclick=()=>$('mosaicEffects').close();
  $('mosaicEffects').addEventListener('close',()=>{effectEditKey=null;effectDraft=null;});
  $('mosaicEffectSave').onclick=()=>{
    if(editLocked()||!effectEditKey||!effectDraft)return;
    clipEffects[effectEditKey]={...effectDraft};
    if(save()){$('mosaicEffects').close();paint();status('本片段效果已保存，重新生成后生效。');}
  };
  const effectButton=index=>`<button data-effect-target="${index}" aria-label="动态预览本片段打码效果" ${editLocked()?'disabled':''}>动态预览</button>`;
  function eventRange(event){
    if(clipEdits[event.id])return {...event,...clipEdits[event.id]};
    const deletion=deletions.find(t=>belongs(t,event));if(deletion)return deletion;
    const saved=targets.filter(t=>belongs(t,event)&&!t.removed),adopted=saved.filter(t=>t.adopted!==false);
    const regions=adopted.length?adopted:saved;
    return regions.length?{...event,start:Math.min(...regions.map(t=>t.start)),end:Math.max(...regions.map(t=>t.end))}:event;
  }
  function setEventRange(event,r){
    if(!event)return;
    clipEdits[event.id]={...r};
    for(const t of [...targets,...deletions].filter(t=>belongs(t,event))){
      t.eventId=event.id;Object.assign(t,r);
      if(Number.isInteger(t.seed_frame))t.seed_frame=Math.max(r.start,Math.min(r.end-1,t.seed_frame));
    }
  }
  function mergeDiscovery(events){
    const protectedEvents=aiEvents.filter(e=>clipEdits[e.id]||targets.some(t=>belongs(t,e))||deletions.some(t=>belongs(t,e)));
    const next=structuredClone(protectedEvents);
    for(const incoming of events){
      const overlaps=e=>Math.max(0,Math.min(e.end,incoming.end)-Math.max(e.start,incoming.start))>=Math.min(e.end-e.start,incoming.end-incoming.start)*.5;
      if(protectedEvents.some(overlaps))continue;
      const previous=aiEvents.find(e=>!protectedEvents.includes(e)&&overlaps(e));
      const id=previous?.id||incoming.id;
      next.push({...structuredClone(incoming),id:next.some(e=>e.id===id)?'ai-'+regionId():id});
    }
    aiEvents=next.sort((a,b)=>a.start-b.start);
    // Bind legacy records once, using their original bounds rather than mutable times.
    for(const event of aiEvents){
      const effective=eventRange(event);
      for(const t of [...targets,...deletions].filter(t=>belongs(t,event))){const oldKey=clipKey(t);if(clipEffects[oldKey]&&!clipEffects[event.id])clipEffects[event.id]=structuredClone(clipEffects[oldKey]);t.eventId=event.id;}
      if(effective.start!==event.start||effective.end!==event.end)clipEdits[event.id]={start:effective.start,end:effective.end};
    }
    save();
  }
  function paintScan(){
    const events=allEvents(),aiResults=aiEvents,enabled=context.session.capabilities?.region_recognition;
    $('mosaicScan').disabled=scanLoading||loading||!enabled;
    $('mosaicScan').textContent=pendingJob(scanReport)?(scanReport.status==='queued'?'识别已排队…':'识别中…'):scanSubmitting?'正在提交…':scanLoading?'读取记录…':scanError?'重试读取':scanReport?.status==='failed'?'重试识别':scanReport?.status==='completed'?'重新识别整段':'识别整段视频';
    let message='未识别。可点「识别整段视频」获取 AI 建议（需 1–3 分钟），也可直接添加片段手动处理。';
    if(!enabled)message='本机未启用 Gemini 识别，可用下方手工补充。';
    else if(scanLoading)message=pendingJob(scanReport)?(scanReport.progress||'识别已接收，等待后台处理…'):scanSubmitting?'正在提交整段识别…':'正在读取已有识别记录…';
    else if(scanError)message=scanError;
    else if(scanReport?.status==='failed')message=scanReport.error||'整段识别失败，请重试。';
    else if(scanReport?.status==='completed'){
      const located=aiResults.filter(e=>e.status==='localized').length;
      message=aiResults.length?`发现 ${aiResults.length} 个片段${aiResults.length-located?` · ${aiResults.length-located} 段打码需补充区域`:''}`:'本次整段抽样未发现疑似时间段，仍请检查短闪帧。可用下方手工补充。';

    }
    if(scanLoading&&events.length)message='识别在后台继续，已有片段可编辑，结果完成后可人工复核。';
    $('mosaicScanStatus').textContent=EditorBusy.message(message);$('mosaicScanStatus').title=EditorBusy.message(message);$('mosaicScanStatus').classList.toggle('error',!!scanError||scanReport?.status==='failed');
    const targetRow=(t,i)=>`<div class="mosaic-event-card"><button data-target-view="${i}"><strong>${clock(t.start)}–${clock(t.end-1)} · ${esc(t.label)}</strong></button><div class="mosaic-card-actions"><div class="mosaic-card-checks"><button data-target-remove="${i}" aria-label="打码" role="checkbox" aria-checked="${t.adopted!==false}" aria-pressed="${t.adopted!==false}" ${t.adopted===false&&activeCount()>=8?'disabled':''}>打码</button></div></div></div>`;
    const grouped=new Set();
    const html=events.map(e=>{
      const allRecords=targets.map((t,i)=>({t,i})).filter(({t})=>belongs(t,e));
      const records=allRecords.filter(({t})=>!t.removed);
      const removal=deletions.find(t=>belongs(t,e)),removed=!!removal,displayRange=eventRange(e);
      allRecords.forEach(({i})=>grouped.add(i));
      const suggestions=e.candidates.map((c,i)=>({c,i})).filter(({c})=>!allRecords.some(({t})=>t.proposal===c.id||(!t.proposal&&t.seed_frame===e.seed_frame&&t.x===c.x&&t.y===c.y&&t.w===c.w&&t.h===c.h)));
      const total=records.length+suggestions.length,multiple=total>1,adopted=records.filter(({t})=>t.adopted!==false).length;
      const toggle=multiple
        ?`<button data-event-adopt="${esc(e.id)}" aria-label="打码" role="checkbox" aria-checked="${adopted>0}" aria-pressed="${adopted>0}">打码</button>`
        :records.length?`<button data-target-remove="${records[0].i}" aria-label="打码" role="checkbox" aria-checked="${adopted>0}" aria-pressed="${adopted>0}" ${!adopted&&activeCount()>=8?'disabled':''}>打码</button>`
        :suggestions.length?`<button data-proposal="${esc(e.id)}:${suggestions[0].i}" aria-label="打码" role="checkbox" aria-checked="false" aria-pressed="false" ${activeCount()>=8?'disabled':''}>打码</button>`
        :`<button data-event-draw="${esc(e.id)}" aria-label="打码" role="checkbox" aria-checked="false" aria-pressed="false" title="先框选需要遮挡的区域">打码</button>`;
      return `<div class="mosaic-event-card ${scanEvent===e.id?'selected':''}" data-mosaic-card="${esc(e.id)}" ${e.id.startsWith('manual-')?'data-manual-card':''}>${e.id.startsWith('manual-')?`<button data-remove-clip="${esc(e.id)}" class="mosaic-remove-clip" aria-label="移除手动片段" title="移除片段">×</button>`:''}<button data-scan-event="${esc(e.id)}" ${records.length?`data-target-view="${records[0].i}"`:''} aria-pressed="${scanEvent===e.id}"><strong>${clock(displayRange.start)}–${clock(displayRange.end-1)}</strong><small>${esc(e.label)}${total?'':' · 打码需框选'}</small><small title="${esc(e.reason)}">${esc(e.reason)}</small></button><div class="mosaic-card-actions"><div class="mosaic-card-checks" role="group" aria-label="片段处理方式"><button data-event-delete="${esc(e.id)}" aria-label="删除" role="checkbox" aria-checked="${removed}" aria-pressed="${removed}">删除</button>${toggle}</div>${!e.id.startsWith('manual-')?`<details class="mosaic-clip-menu"><summary aria-label="更多片段操作">⋯</summary><button data-restore-clip="${esc(e.id)}">恢复识别结果</button></details>`:''}</div></div>`;

    }).join('')+targets.map((t,i)=>t.removed||grouped.has(i)||(scanReport?.status==='completed'&&t.adopted===false&&Number.isInteger(t.originStart)&&!t.eventId)?'':targetRow(t,i,true)).join('');
    if($('mosaicScanEvents').innerHTML!==html)$('mosaicScanEvents').innerHTML=html;
    $('mosaicScanMarkers').innerHTML=events.map(original=>{const e=eventRange(original);return `<button data-scan-marker="${esc(e.id)}" class="${e.status==='needs_review'?'needs-review':''}" aria-label="查看 ${esc(e.label)} ${clock(e.start)}" title="${clock(e.start)}–${clock(e.end-1)} ${esc(e.label)}" style="left:${100*e.start/meta().frame_count}%;width:${100*(e.end-e.start)/meta().frame_count}%"></button>`;}).join('');
  }
  function checkedReport(report){
    if(!report)return null;
    if(!['queued','running','cancelling','completed','failed','cancelled','superseded'].includes(report.status))throw Error('整段识别任务状态无效，请重新读取。');
    if(report.status!=='completed'&&!report.events?.length)return report;
    if(report.source_sha256!==context.session.source_sha256||report.reset_at!==(context.session.reset_at||'')||!Array.isArray(report.events)||report.events.length>80)throw Error('整段识别结果已过期，请重新打开。');
    const ids=new Set();
    for(const e of report.events){
      if(typeof e.id!=='string'||ids.has(e.id)||![e.start,e.end].every(Number.isInteger)||e.start<0||e.start>=e.end||e.end>meta().frame_count||typeof e.label!=='string'||typeof e.reason!=='string'||!['localized','needs_review'].includes(e.status)||!Array.isArray(e.candidates)||e.candidates.length>8||e.candidates.some(c=>!validBox(c)))throw Error('识别结果包含无效区间或区域，请重试。');
      if(e.status==='localized'&&(!e.candidates.length||!Number.isInteger(e.seed_frame)||e.seed_frame<e.start||e.seed_frame>=e.end))throw Error('识别结果缺少有效参考帧，请重试。');
      ids.add(e.id);
    }
    return report;
  }
  async function scanVideo(start=false){
    if(!context||scanLoading||loading)return;
    scanReadPending=false;
    const version=epoch,request=++scanToken,endpoint=base(),identity=JSON.stringify([context.session.source_sha256,context.session.reset_at]),valid=()=>active(version)&&request===scanToken&&identity===JSON.stringify([context.session.source_sha256,context.session.reset_at]);
    scanController?.abort();const scanAbort=new AbortController();scanController=scanAbort;
    const submit=async force=>{
      const body={reset_at:context.session.reset_at||'',force},previous=scanReport?.id;
      const send=()=>api(endpoint+'/scan',body,{signal:scanAbort.signal});
      try{return await (force?send():EditorBusy.run(send,{signal:scanAbort.signal,valid,diagnostics:{batch:context.batch,request:context.asset.request_id,session:context.session.id},onWait:(n,max)=>status(`处理资源暂忙，正在重试（${n}/${max}）`)}));}
      catch(e){
        if(!valid()||scanAbort.signal.aborted||e.status||!(e instanceof TypeError||e.outcomeUnknown))throw e;
        status('正在核对任务状态…');
        try{
          const observed=await api(endpoint+'/scan',undefined,{signal:scanAbort.signal});
          if(!valid()||scanAbort.signal.aborted)throw new DOMException('已取消等待','AbortError');
          const report=checkedReport(observed.report);
          if(observed.enabled===true&&report?.id&&report.source_sha256===context.session.source_sha256&&report.reset_at===(context.session.reset_at||'')&&report.id!==previous&&['queued','running','completed'].includes(report.status)){
            status('已读取当前识别任务状态；提交回执未确认。');return report;
          }
        }catch(checkError){if(checkError.name==='AbortError')throw checkError;}
        throw Error('识别提交结果待核实，未重复提交。请稍后重新读取状态；再次手动识别可能产生新任务。');
      }
    };
    scanLoading=true;scanError='';paint();
    try{
      let state;
      if(start){
        scanSubmitting=true;paint();
        try{state={enabled:true,report:await submit(!!scanReport)};}finally{scanSubmitting=false;}
      }else state=await api(endpoint+'/scan'); // Opening only reads; Gemini runs after the user clicks.
      while(valid()){
        if(typeof state.enabled!=='boolean')throw Error('整段识别响应无效，请重试读取。');
        context.session.capabilities.region_recognition=state.enabled;
        const firstReport=!scanReport;scanReport=checkedReport(state.report);if(scanReport?.status==='completed'&&!pendingJob()&&!awaitingDecision()&&!batchLoading)mergeDiscovery(scanReport.events);paint();if(firstReport&&scanReport)status('点击片段查看画面，选择删除或打码；不选则不处理。');
        if(!pendingJob(scanReport))break;
        await new Promise(r=>setTimeout(r,1000));if(!valid())return;
        state=await api(endpoint+'/scan');
      }
    }catch(e){if(valid())scanError=e.message;}
    finally{if(valid()){scanLoading=false;paint();}}
  }
  function viewScanEvent(id){
    addingClip=false;
    const original=allEvents().find(e=>e.id===id);if(!original)return;const event={...original,...eventRange(original)};motion='fixed';
    const deletion=deletions.find(t=>belongs(t,event));
    if(deletion){resetCandidate();scanEvent=id;$('mosaicAdjust').open=true;$('mosaicStart').value=clock(deletion.start);$('mosaicEnd').value=clock(deletion.end-1);seek(deletion.start,true);return;}
    const savedIndex=targets.findIndex(t=>belongs(t,event)&&!t.removed);
    if(savedIndex>=0){viewTarget(savedIndex,id);return;}
    resetCandidate();scanEvent=id;
    $('mosaicStart').value=clock(event.start);$('mosaicEnd').value=clock(event.end-1);$('mosaicAdjust').open=true;
    candidates=event.candidates.filter(c=>!targets.some(t=>belongs(t,original)&&t.removed&&t.proposal===c.id)).map(c=>({...c,motion:'fixed'}));selected=candidates[0]?.id??null;
    const seed=event.status==='localized'?Math.max(event.start,Math.min(event.end-1,event.seed_frame)):Math.floor((event.start+event.end-1)/2);
    seek(seed,true);
    $('mosaicScanEvents').querySelector('[aria-pressed="true"]')?.scrollIntoView({block:'nearest'});
    status(event.status==='localized'?'时间段和候选区域已自动定位。检查后打码，必要时再调整。':event.review_note||'该段需复核：尚未定位区域，可换一帧识别或手动框选。');
  }
  function geometry(){const rect=$('mosaicStage').getBoundingClientRect(),scale=Math.min(rect.width/meta().width,rect.height/meta().height);return {left:(rect.width-meta().width*scale)/2,top:(rect.height-meta().height*scale)/2,width:meta().width*scale,height:meta().height*scale,scale,rect};}
  function drawOverlay(){
    if(!context)return;
    drawLivePreview();
    overlay.hidden=ready!==wanted||image.hidden||!video.paused||(!candidates.length&&!drawing);
    if(overlay.hidden)return;
    const g=geometry();Object.assign(overlay.style,{left:g.left+'px',top:g.top+'px',width:g.width+'px',height:g.height+'px'});
    overlay.classList.toggle('drawing',drawing);
    overlay.innerHTML=candidates.map(c=>`<button class="mosaic-box ${c.id===selected?'selected':''}" data-box="${esc(c.id)}" aria-label="选择 ${esc(c.label)}" style="left:${c.x/meta().width*100}%;top:${c.y/meta().height*100}%;width:${c.w/meta().width*100}%;height:${c.h/meta().height*100}%"><span>${esc(c.label)}</span>${c.id===selected&&!editLocked()?['n','s','e','w','ne','nw','se','sw'].map(edge=>`<i data-resize="${edge}" class="mosaic-handle ${edge}"></i>`).join(''):''}</button>`).join('');
  }
  async function loadFrame(){
    if(document.hidden||!context||!dialog.open||!video.paused||resultMode)return;
    if(frameLoading&&pendingFrame===wanted&&!controller?.signal.aborted)return;
    const version=epoch,frame=wanted,url=base()+`/frame/${frame}`;
    controller?.abort();const request=new AbortController();controller=request;pendingFrame=frame;frameLoading=true;paint();
    $('mosaicFrameHint').textContent='正在读取准确帧…';$('mosaicFrameRetry').hidden=true;
    try{
      const response=await EditorBusy.run(()=>EditorBusy.frame(appUrl(url),request.signal,job=>{if(active(version)&&request===controller)$('mosaicFrameHint').textContent=job.progress||'准确帧已排队，等待后台读取…';}),{signal:request.signal,maxRetries:1,diagnostics:{batch:context.batch,request:context.asset.request_id,session:context.session.id},valid:()=>active(version)&&request===controller&&wanted===frame,onWait:(n,max)=>{$('mosaicFrameHint').textContent=`处理资源暂忙，正在重试（${n}/${max}）`;}});
      const blob=await response.blob();if(!active(version)||request!==controller||request.signal.aborted||document.hidden)return;
      const next=URL.createObjectURL(blob);image.src=next;
      try{await image.decode();}catch{URL.revokeObjectURL(next);throw Error('准确帧读取失败，请重试');}
      if(!active(version)||request!==controller||request.signal.aborted||document.hidden||frame!==wanted||!video.paused){URL.revokeObjectURL(next);return;}
      if(frameUrl)URL.revokeObjectURL(frameUrl);frameUrl=next;ready=frame;image.hidden=false;
      $('mosaicFrameHint').textContent=motionHint();
    }catch(e){if(active(version)&&request===controller&&e.name!=='AbortError'){$('mosaicFrameHint').textContent=EditorBusy.message(e.message,'画面读取失败，请点击重试');$('mosaicFrameRetry').hidden=false;}}
    finally{if(active(version)&&request===controller){frameLoading=false;paint();if(scanReadPending&&ready===wanted&&!image.hidden)scanVideo(false);}}
  }
  function seek(frame,keep=false){
    if(!context)return;clearTimeout(timer);controller?.abort();pausePlayback();
    wanted=Math.max(0,Math.min(meta().frame_count-1,Math.round(frame)));ready=-1;image.hidden=true;
    if(!keep)resetCandidate();
    if(sourceBinding)sourceBinding.seek((wanted+.05)/rate());
    else if(Number.isFinite(video.duration))video.currentTime=(wanted+.05)/rate();
    paint();timer=setTimeout(loadFrame,100);
  }
  async function recognize(){
    if($('mosaicRecognize').disabled)return;
    const version=epoch,jobToken=++token,endpoint=base(),r=range(),frame=wanted;
    const valid=()=>active(version)&&jobToken===token;
    candidates=[];selected=null;editing=null;drawing=false;loading=true;paint();status('Gemini 正在识别参考帧中的候选遮挡区域…');
    try{
      let job=await api(endpoint+'/locate',{...r,seed_frame:frame,reset_at:context.session.reset_at||''});
      while(valid()&&pendingJob(job)){
        status(job.progress||'正在识别参考帧…');await new Promise(r=>setTimeout(r,1000));if(!valid())return;
        job=await api(endpoint+'/locate/'+job.id);
      }
      if(!valid())return;
      if(wanted!==frame||JSON.stringify(range())!==JSON.stringify(r)||pendingJob()||awaitingDecision())return;
      if(job.status!=='completed')throw Error(job.error||'识别未完成，请重试');
      if(job.seed_frame!==frame||job.start!==r.start||job.end!==r.end||!Array.isArray(job.candidates)||job.candidates.length>8||job.candidates.some(c=>!validBox(c)))throw Error('识别结果与当前参考帧不匹配，请重试');
      candidates=job.candidates.map(c=>({...c,motion:'fixed'}));selected=candidates[0]?.id??null;if(candidates.length)$('mosaicAdjust').open=true;
      status(candidates.length?'选中候选框，可调整位置和大小，确认后加入待跟踪目标。':'本帧未找到候选区域。可换一帧识别，或手动框选。');
    }catch(e){if(valid())status(e.message,true);}
    finally{if(valid()){loading=false;paint();}}
  }
  // Closing the dialog does not cancel server-side snapshot preparation.
  // Keep only in-flight requests so re-entry joins the same preparation, while
  // later opens still validate the current source and editing permissions.
  const preparing=new Map();
  function prepare(batch,asset,valid){
    const id=JSON.stringify([currentUser?.username,batch,asset.request_id,asset.sha256]);
    let entry=preparing.get(id);
    if(!entry){
      entry={consumers:new Set(),abort:new AbortController()};
      const body={request_id:asset.request_id,source_sha256:asset.sha256};
      const live=()=>[...entry.consumers].some(v=>v());
      // The initial microtask lets the first consumer register before sending.
      entry.pending=Promise.resolve().then(()=>EditorBusy.run(()=>api(`/api/batches/${batch}/mosaics/open`,body),{
        signal:entry.abort.signal,valid:live,diagnostics:{batch,request:asset.request_id},
        onWait:(n,max)=>{entry.waiting=true;if(live())status(`处理资源暂忙，正在重试（${n}/${max}）`);},
        onSend:()=>{entry.waiting=false;if(live())status('正在准备原片，首次打开需要一些时间；关闭后重新进入会继续等待…');}
      })).then(async value=>{
        while(['queued','running','cancelling'].includes(value?.status)){
          if(!live())throw new DOMException('已离开等待，后台继续准备','AbortError');
          if(live())status(value.progress||'原片准备已排队，后台轮到后自动处理…');
          await new Promise(resolve=>setTimeout(resolve,500));
          value=await api(`/api/batches/${batch}/mosaics/preparation/${value.id}`,undefined,{signal:entry.abort.signal});
        }
        if(value?.status==='completed')return value.result.session;
        if(value?.status)throw Error(value.error||value.progress||'原片准备未完成，请重新打开');
        return value;
      }).finally(()=>{if(preparing.get(id)===entry)preparing.delete(id);});
      preparing.set(id,entry);
    }
    entry.consumers.add(valid);
    releasePreparation=()=>{entry.consumers.delete(valid);if(!entry.consumers.size){entry.abort.abort();if(entry.waiting&&preparing.get(id)===entry)preparing.delete(id);}};
    return entry.pending;
  }
  async function open(batch,asset){
    releasePreparation?.();scanController?.abort();
    sourceManager?.dispose();sourceManager=sourceBinding=beforeBinding=null;
    detachClipPreview();stopWorkspacePreview();previewOperation=null;const version=++epoch;token++;controller?.abort();clearTimeout(timer);context=null;playbackError=false;pausePlayback();motion='fixed';wanted=0;ready=-1;candidates=[];selected=null;targets=[];editing=null;loading=false;frameLoading=false;drawing=false;
    batchToken++;batchJob=null;batchLoading=false;stopPending=false;batchError='';deletions=[];mutationBusy=false;resultBefore=false;resultMode=false;$('mosaicBatchDialog').hidden=true;
    scanReadPending=false;scanSubmitting=false;clearRemovedClip();scanToken++;scanLoading=false;scanReport=null;scanError='';scanEvent=null;addingClip=false;manualClips=[];aiEvents=[];clipEdits={};$('mosaicAdjust').open=false;$('mosaicEffects').close();
    image.hidden=true;overlay.hidden=true;$('mosaicWorkspace').hidden=true;$('mosaicTitle').textContent='剪切打码 · '+asset.title;
    for(const v of $('assets').querySelectorAll('video'))v.pause();
    $('mosaicPreparing').hidden=false;dialog.setAttribute('aria-busy','true');dialog.querySelector('.mosaic-batch-footer').hidden=true;
    if(!dialog.open)dialog.showModal();status('正在准备原片，首次打开需要一些时间；关闭后重新进入会继续等待…');
    try{
      const session=await prepare(batch,asset,()=>version===epoch&&dialog.open&&!document.hidden);
      if(version!==epoch||!dialog.open)return;context={batch,asset,session};
      for(const id of ['mosaicSource', 'mosaicBatchVideo', 'mosaicTrackVideo']){Object.assign($(id).dataset,{previewBatch:batch,previewRequest:asset.request_id,previewSession:session.id});}
      $('mosaicStart').value=clock(0);$('mosaicEnd').value=clock(meta().frame_count-1);$('mosaicSeek').max=meta().frame_count-1;
      batchLoading=true;
      const restored=await api(base()+'/render');
      if(!active(version))return;
      batchJob=restored.job||null;batchLoading=false;
      savedRaw=localStorage.getItem(draftKey());legacyRaw=localStorage.getItem(key());oldDraftRaw=localStorage.getItem(oldDraftKey());
      if(batchJob?.queued_task&&(pendingJob()||awaitingDecision()||(savedRaw===null&&legacyRaw===null&&oldDraftRaw===null))){
        restoreParameters(batchJob);
      }else loadDraft();
      resultMode=awaitingDecision();
      $('mosaicPreparing').hidden=true;dialog.setAttribute('aria-busy','false');dialog.querySelector('.mosaic-batch-footer').hidden=false;
      $('mosaicWorkspace').hidden=false;
      if(session.capabilities?.oss_playback){
        sourceManager=EditorSource.create({endpoint:base()+'/source-url',local:base()+'/source',enabled:true});
        sourceBinding=sourceManager.attach(video,{valid:()=>active(version)&&!resultMode,beforeChange:()=>{playAttempt++;},play:playDirect,failed:()=>{playbackError=true;paintStatus();},anchor:$('mosaicStatus')});
        sourceBinding.open();
      }else{video.src=appUrl(base()+'/source');video.load();}
      status(session.capabilities?.region_recognition?'可直接播放和添加片段；需要 AI 建议时点「识别整段视频」。':'本机未启用 Gemini 识别，可先手动框选目标。');
      scanReadPending=!batchJob;paint();loadFrame();restoreBatch();restorePreviewNotice(version);
    }catch(e){if(version===epoch){$('mosaicPreparing').hidden=true;dialog.setAttribute('aria-busy','false');status(e.message,true);}}
  }
  function mount(){
    if(!currentUser?.features?.cutting||!current)return;
    for(const card of $('assets').querySelectorAll('[data-asset]')){
      const asset=current.assets[Number(card.dataset.asset)],batch=current.id;
      if(!asset?.preview||asset.can_edit_video!==true){card.querySelector('[data-open-mosaic]')?.remove();continue;}
      if(card.querySelector('[data-open-mosaic]'))continue;
      const button=document.createElement('button');button.type='button';button.className='secondary mosaic-entry';button.dataset.openMosaic='';button.textContent='剪切打码';
      // A sibling prevents the existing cut-tools click handler from opening both dialogs.
      const tools=card.querySelector('.cut-tools');if(tools){tools.hidden=true;tools.after(button);}else card.append(button);
      button.onclick=()=>open(batch,asset);
    }
  }
  $('mosaicClose').onclick=()=>{if(!mutationBusy){$('mosaicEffects').close();dialog.close();}};
  dialog.addEventListener('cancel',e=>{if(mutationBusy)e.preventDefault();});
  dialog.addEventListener('close',()=>{if(dialog.open)return;stopWorkspacePreview();detachClipPreview();previewOperation=null;$('mosaicTrackDialog').close();releasePreparation?.();releasePreparation=null;scanController?.abort();sourceManager?.dispose();sourceManager=sourceBinding=beforeBinding=null;epoch++;token++;scanToken++;controller?.abort();clearTimeout(timer);context=null;clearRemovedClip();$('mosaicBatchVideo').pause();pausePlayback();video.removeAttribute('src');video.load();image.removeAttribute('src');if(frameUrl)URL.revokeObjectURL(frameUrl);frameUrl=null;});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){releasePreparation?.();scanController?.abort();controller?.abort();clearTimeout(timer);}else if(context&&dialog.open&&video.paused&&!resultMode&&ready!==wanted){loadFrame();}});
  $('mosaicScan').onclick=()=>scanVideo(!scanError&&scanReport?.status!=='running');
  $('mosaicScanEvents').onclick=e=>{const b=e.target.closest('[data-scan-event]'),card=e.target.closest('[data-mosaic-card]');if(b)viewScanEvent(b.dataset.scanEvent);else if(card&&!e.target.closest('button,summary,details'))viewScanEvent(card.dataset.mosaicCard);};
  $('mosaicScanMarkers').onclick=e=>{const b=e.target.closest('[data-scan-marker]');if(b)viewScanEvent(b.dataset.scanMarker);};
  function nextEvent(offset){
    const events=allEvents(),index=events.findIndex(e=>e.id===scanEvent),next=events[index+offset];
    if(index>=0&&next)viewScanEvent(next.id);
  }
  $('mosaicPrevEvent').onclick=()=>nextEvent(-1);
  $('mosaicNextEvent').onclick=()=>nextEvent(1);
  $('mosaicConfirmNext').onclick=()=>{if($('mosaicConfirmNext').disabled)return;if($('mosaicConfirm').onclick())nextEvent(1);};
  $('mosaicAddClip').onclick=()=>{
    if(editLocked())return;
    resetCandidate();addingClip=true;manualAction='delete';motion='fixed';
    $('mosaicStart').value=clock(wanted);$('mosaicEnd').value=clock(Math.min(meta().frame_count-1,wanted+Math.round(rate()*5)-1));
    $('mosaicAdjust').open=true;paint();status('设置时间，选择删除或打码后添加。');
  };
  $('mosaicMotion').onchange=()=>{$('mosaicMotion').value=motion='fixed';};
  $('mosaicManualAction').onchange=e=>{manualAction=e.target.value;paint();};
  $('mosaicAddSubmit').onclick=guard(()=>{
    if(editLocked())return;
    const r=range();
    if(manualAction==='delete'&&deletions.length>=40)throw Error('最多选择 40 个删除片段。');
    if(manualClips.length>=40)throw Error('最多添加 40 个手动片段。');
    if(manualAction==='mosaic'&&(!validBox(selectedBox())||!inRange()||ready!==wanted))throw Error('请先在片段内框选打码区域。');
    if(manualAction==='mosaic'&&targets.length+candidates.length>320)throw Error('本机草稿区域已达上限。');
    if(manualAction==='mosaic'&&activeCount()+candidates.length>8)throw Error('最多打码 8 个区域，请先取消一个。');
    const event={id:'manual-'+regionId(),...r,label:'手动片段',reason:'',status:'needs_review',candidates:[]};
    manualClips.push(event);scanEvent=event.id;
    if(manualAction==='delete'){
      deletions.push({...r,eventId:event.id,originStart:r.start,originEnd:r.end,label:event.label});
      if(save()){addingClip=false;$('mosaicAdjust').open=false;paint();status('已添加删除片段。');}
    }else{
      for(const c of candidates.filter(validBox))targets.push({uid:regionId(),eventId:event.id,originStart:r.start,originEnd:r.end,proposal:c.id,adopted:true,motion:'fixed',label:c.label,reason:c.reason,x:c.x,y:c.y,w:c.w,h:c.h,...r,seed_frame:Math.max(r.start,Math.min(r.end-1,c.seed_frame??wanted))});
      setEventRange(event,r);
      if(save()){addingClip=false;$('mosaicAdjust').open=false;viewScanEvent(event.id);paint();status('已添加打码片段。');}
    }
  });
  $('mosaicAdjustToggle').onclick=()=>{$('mosaicAdjust').open=true;paint();};
  $('mosaicAdjust').addEventListener('toggle',()=>{if(context){dialog.classList.toggle('adjusting',$('mosaicAdjust').open);if(!scanEvent&&editing===null&&!addingClip)$('mosaicAdjust').classList.toggle('manual-delete',!$('mosaicAdjust').open);}});
  $('mosaicRecognize').onclick=recognize;
  $('mosaicFrameRetry').onclick=loadFrame;
  $('mosaicPrevious').onclick=()=>seek(wanted-1);$('mosaicNext').onclick=()=>seek(wanted+1);
  $('mosaicSeek').oninput=e=>seek(Number(e.target.value));
  $('mosaicFrameNumber').onchange=guard(e=>seek(parse(e.target.value)));
  for(const id of ['mosaicStart','mosaicEnd'])$(id).onchange=guard(()=>{
    const r=range();
    if(!inRange()){
      seek(Math.max(r.start,Math.min(r.end-1,wanted)),true);
      status(selectedBox()?'已跳到新时间范围，请检查区域位置后保存调整。':'已跳到新时间范围，请框选区域。');
    }else status('时间范围已修改，点击保存后生效。');
    paint();
  });
  $('mosaicMarkStart').onclick=()=>{$('mosaicStart').value=clock(wanted);$('mosaicStart').dispatchEvent(new Event('change'));};
  $('mosaicMarkEnd').onclick=()=>{$('mosaicEnd').value=clock(wanted);$('mosaicEnd').dispatchEvent(new Event('change'));};
  $('mosaicCandidates').onclick=e=>{const saved=e.target.closest('[data-saved-adjust]');if(saved){const drafts=[...regionDrafts.filter(d=>!candidates.some(c=>c.id===d.id)),...candidates.map(c=>({...c}))],start=$('mosaicStart').value,end=$('mosaicEnd').value;viewTarget(Number(saved.dataset.savedAdjust));regionDrafts=drafts;candidates=candidates.map(c=>drafts.find(d=>d.id===c.id)||c);$('mosaicStart').value=start;$('mosaicEnd').value=end;$('mosaicAdjust').open=true;paint();return;}const b=e.target.closest('[data-candidate]');if(b){selectRegion(b.dataset.candidate);paint();}};
  for(const axis of ['X','Y','W','H'])$('mosaicBox'+axis).onchange=guard(e=>{const c=selectedBox();if(!c)return;const next={...c,[axis.toLowerCase()]:Number(e.target.value)};if(!validBox(next)){paint();throw Error('矩形必须位于画面内，宽高至少 1 像素');}Object.assign(c,next);drawOverlay();});
  $('mosaicConfirm').onclick=guard(()=>{
    if($('mosaicConfirm').disabled)return;
    const r=range(),event=allEvents().find(e=>e.id===scanEvent);
    const oldRange=event?eventRange(event):null;
    if(event&&!choosingMosaic&&!selectedLiveTarget()){
      setEventRange(event,r);if(save()){paint();status('片段时间已保存，处理选择保持不变。');return true;}return;
    }
    const timeChanged=oldRange&&(oldRange.start!==r.start||oldRange.end!==r.end);
    const boxes=[...regionDrafts.filter(d=>!candidates.some(c=>c.id===d.id)),...candidates].filter(c=>validBox(c)&&!(timeChanged&&!c.uid&&event?.candidates?.some(v=>v.id===c.id&&['x','y','w','h'].every(k=>v[k]===c[k]))));
    const changes=boxes.map(c=>{
      const index=targets.findIndex(t=>c.uid?t.uid===c.uid:event&&belongs(t,event)&&t.proposal===c.id);
      return {c,index};
    });
    if(activeCount()+changes.filter(v=>v.index<0).length>8)throw Error('最多打码 8 个区域，请先取消一个。');
    if(targets.length+changes.filter(v=>v.index<0).length>320)throw Error('本机草稿区域已达上限。');
    setEventRange(event,r);
    if(event&&changes.some(({index})=>index<0||targets[index].adopted!==false))deletions=deletions.filter(t=>!belongs(t,event));
    for(const {c,index} of changes){
      const target={...(index<0?{uid:regionId(),eventId:event?.id,originStart:event?.start,originEnd:event?.end,proposal:c.id}:targets[index]),adopted:index<0||targets[index].adopted!==false,removed:false,motion:'fixed',label:c.label,reason:c.reason,x:c.x,y:c.y,w:c.w,h:c.h,...r,seed_frame:Math.max(r.start,Math.min(r.end-1,c.seed_frame??wanted))};
      if(index<0)targets.push(target);else targets[index]=target;
      c.uid=target.uid;
    }
    selectRegion(selected);
    paint();if(save()){$('mosaicAdjust').open=true;paint();status('已保存修改。');return true;}
  });
  function viewTarget(i,eventId){
    addingClip=false;
    const t=targets[i];if(!t)return;
    resetCandidate();editing=i;motion='fixed';scanEvent=eventId??allEvents().find(e=>belongs(t,e))?.id??null;
    const event=allEvents().find(e=>e.id===scanEvent);
    candidates=targets.filter(v=>!v.removed&&(v.uid===t.uid||(event&&belongs(v,event)&&(v.motion==='fixed'||v.seed_frame===t.seed_frame)))).map(v=>({...v,motion:'fixed',id:'saved-'+v.uid}));
    selected='saved-'+t.uid;$('mosaicAdjust').open=true;
    $('mosaicStart').value=clock(t.start);$('mosaicEnd').value=clock(t.end-1);seek(t.seed_frame,true);
    status('正在查看保存的区域；取消勾选不会丢失调整。');
  }
  $('mosaicUndoRemove').onclick=()=>{
    if(editLocked()||!removedClip)return;
    ({manualClips,targets,deletions,clipEffects,clipEdits}=structuredClone(removedClip));
    if(save()){resetCandidate();paint();status('已恢复片段。');}
  };
  $('mosaicTargets').onclick=e=>{
    const restore=e.target.closest('[data-restore-clip]');
    if(restore){
      if(editLocked())return;
      const event=aiEvents.find(e=>e.id===restore.dataset.restoreClip);if(!event)return;
      const keys=targets.filter(t=>belongs(t,event)).map(clipKey);
      targets=targets.filter(t=>!belongs(t,event));deletions=deletions.filter(t=>!belongs(t,event));delete clipEdits[event.id];
      for(const key of keys)delete clipEffects[key];
      if(save()){viewScanEvent(event.id);paint();status('已恢复该片段的识别结果，处理选择已取消。');}return;
    }

    const removeClip=e.target.closest('[data-remove-clip]');
    if(removeClip){
      if(editLocked())return;
      const event=manualClips.find(c=>c.id===removeClip.dataset.removeClip);if(!event)return;
      const previous=structuredClone({manualClips,targets,deletions,clipEffects,clipEdits});
      const keys=targets.filter(t=>belongs(t,event)).map(clipKey);
      manualClips=manualClips.filter(c=>c.id!==event.id);delete clipEdits[event.id];
      targets=targets.filter(t=>!belongs(t,event));deletions=deletions.filter(t=>!belongs(t,event));
      for(const key of keys)if(!targets.some(t=>clipKey(t)===key))delete clipEffects[key];
      if(save()){
        resetCandidate();$('mosaicAdjust').open=false;paint();
        removedClip=previous;$('mosaicRemoved').hidden=false;
        removeTimer=setTimeout(clearRemovedClip,8000);
      }
      return;
    }

    const effectButton=e.target.closest('[data-effect-target]');if(effectButton){openEffects(Number(effectButton.dataset.effectTarget));return;}
    const removeEvent=e.target.closest('[data-event-delete]');
    if(removeEvent){
      const event=allEvents().find(v=>v.id===removeEvent.dataset.eventDelete);
      const found=deletions.findIndex(t=>belongs(t,event));
      if(found>=0)deletions.splice(found,1);
      else {const effective=eventRange(event);deletions.push({start:effective.start,end:effective.end,eventId:event.id,originStart:event.start,originEnd:event.end,label:event.label});targets.filter(t=>belongs(t,event)).forEach(t=>t.adopted=false);}
      if(save()){viewScanEvent(event.id);paint();status('处理选择已保存，关闭后可继续。');}return;
    }

    const draw=e.target.closest('[data-event-draw]');
    if(draw){viewScanEvent(draw.dataset.eventDraw);choosingMosaic=true;$('mosaicAdjust').open=true;drawing=true;paint();status('在画面框选区域，然后保存。');return;}
    const group=e.target.closest('[data-event-adopt]');
    if(group){
      const event=allEvents().find(v=>v.id===group.dataset.eventAdopt);
      const allRecords=targets.filter(t=>belongs(t,event)),records=allRecords.filter(t=>!t.removed);
      const missing=event.candidates.filter(c=>!allRecords.some(t=>t.proposal===c.id||(!t.proposal&&t.seed_frame===event.seed_frame&&t.x===c.x&&t.y===c.y&&t.w===c.w&&t.h===c.h)));
      const adopt=group.getAttribute('aria-pressed')!=='true';
      if(adopt&&(activeCount()+records.filter(t=>t.adopted===false).length+missing.length>8||targets.length+missing.length>320)){paint();status('最多打码 8 个区域，请先取消其他片段。',true);return;}
      if(adopt)deletions=deletions.filter(t=>!belongs(t,event));
      records.forEach(t=>t.adopted=adopt);
      if(adopt)missing.forEach(c=>targets.push({...c,motion:'fixed',uid:regionId(),adopted:true,proposal:c.id,eventId:event.id,originStart:event.start,originEnd:event.end,start:eventRange(event).start,end:eventRange(event).end,seed_frame:Math.max(eventRange(event).start,Math.min(eventRange(event).end-1,event.seed_frame))}));
      if(save()){viewScanEvent(event.id);paint();status(adopt?'已打码该片段的全部区域。':'已取消该片段；区域调整已保留。');}return;
    }
    const proposal=e.target.closest('[data-proposal]');
    if(proposal){
      const [id,index]=proposal.dataset.proposal.split(':'),event=allEvents().find(v=>v.id===id),c=event?.candidates[Number(index)];
      if(!c||activeCount()>=8||targets.length>=320){paint();return;}
      deletions=deletions.filter(t=>!belongs(t,event));
      targets.push({...c,motion:'fixed',uid:regionId(),adopted:true,proposal:c.id,eventId:event.id,originStart:event.start,originEnd:event.end,start:eventRange(event).start,end:eventRange(event).end,seed_frame:Math.max(eventRange(event).start,Math.min(eventRange(event).end-1,event.seed_frame))});
      if(save()){viewScanEvent(event.id);paint();status('已打码并保存；点击片段可继续检查。');}return;
    }
    const remove=e.target.closest('[data-target-remove]'),view=e.target.closest('[data-target-view]');
    if(remove){const t=targets[Number(remove.dataset.targetRemove)];if(t.adopted===false&&activeCount()>=8){paint();status('最多打码 8 个区域，请先取消一个。',true);return;}t.adopted=t.adopted===false;if(t.adopted){const event=allEvents().find(e=>belongs(t,e));if(event)deletions=deletions.filter(d=>!belongs(d,event));}if(save()){viewTarget(Number(remove.dataset.targetRemove));paint();status(t.adopted?'已恢复打码，保留原来的区域调整。':'已取消打码；框和时间已保留，可再次勾选。');}return;}
    if(view&&!view.hasAttribute('data-scan-event'))viewTarget(Number(view.dataset.targetView));
  };
  const deleteRanges=()=>deletions.map(t=>[t.start,t.end]).sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
  const samePlan=(job,plan)=>!!job&&JSON.stringify({...defaultEffect,...job.effect})===JSON.stringify({...defaultEffect,...plan.effect})&&targetSignature(job.targets||[],job.effect)===targetSignature(plan.targets||[],plan.effect)&&JSON.stringify(job.ranges||[])===JSON.stringify(mergedRanges(plan.ranges||[]));
  const sameBatch=()=>samePlan(batchJob,{effect,targets:adoptedTargets(),ranges:deleteRanges()});
  function mergedRanges(input){const out=[];for(const [a,b] of input){if(out.length&&a<=out.at(-1)[1])out.at(-1)[1]=Math.max(b,out.at(-1)[1]);else out.push([a,b]);}return out;}
  const targetFields=['start','end','seed_frame','x','y','w','h'];
  const renderTarget=t=>({...Object.fromEntries(targetFields.map(k=>[k,t[k]])),motion:'fixed'});
  const adoptedTargets=()=>targets.filter(t=>t.adopted!==false).map(t=>({...renderTarget(t),effect:effectFor(t)}));
  const targetSignature=(list,fallback=effect)=>JSON.stringify([...new Set(list.map(t=>JSON.stringify([...targetFields.map(k=>t[k]),t.motion||'tracked',{...defaultEffect,...fallback,...t.effect}])))] .sort());
  function paintBatch(){
    if(!context)return;
    for(const el of dialog.querySelectorAll('[data-generation-lock]')){el.disabled=el.dataset.generationLock==='true';delete el.dataset.generationLock;}
    const locked=editLocked();
    if(locked){
      for(const el of dialog.querySelectorAll(mutationSelector)){el.dataset.generationLock=String(el.disabled);el.disabled=true;}
      drawing=false;pointer=null;
    }
    $('mosaicStop').hidden=!pendingJob()||context.session.capabilities?.render_stop!==true;$('mosaicStop').disabled=stopPending||!!batchJob?.cancel_requested;
    $('mosaicStop').textContent=stopPending||batchJob?.cancel_requested?'正在停止…':batchJob?.status==='queued'?'取消排队':'停止生成';
    $('mosaicDiscard').textContent=awaitingDecision()?'放弃结果':'放弃本次处理';
    $('mosaicBatchClose').textContent=awaitingDecision()?'查看原片与参数':'返回调整';
    $('mosaicDiscard').hidden=locked&&!awaitingDecision();
    $('mosaicProcessingHint').textContent=batchJob?.status==='queued'?'任务已提交，关闭后继续排队。其他素材可继续编辑和提交。':'处理期间暂不可编辑；停止后可修改。关闭后后台继续处理。';
    const adopted=adoptedTargets(),segments=new Set(adopted.map(t=>`${t.start}:${t.end}`)).size;
    const running=batchLoading||pendingJob()||mutationBusy;
    const generating=pendingJob()||(batchLoading&&!batchJob);
    const showOutput=resultMode&&batchJob?.status==='completed';
    if(showOutput&&!$('mosaicWorkspace').hidden)pausePlayback();
    $('mosaicWorkspace').hidden=showOutput;$('mosaicBatchDialog').hidden=!showOutput;
    $('mosaicProcessing').hidden=!generating;
    $('mosaicProcessingText').textContent=batchError||(stopPending||batchJob?.cancel_requested?'正在停止生成…':batchJob?.progress||'正在准备处理视频…');
    $('mosaicStatus').hidden=generating||showOutput;
    dialog.classList.toggle('processing-result',showOutput);
    const clip=currentClip();
    const trackingUnavailable=!context.session.capabilities?.tracking&&adopted.some(t=>t.motion!=='fixed');
    $('mosaicBatchStart').disabled=trackingUnavailable||(!adopted.length&&!deletions.length)||running;
    $('mosaicDiscard').disabled=mutationBusy||scanReport?.status==='running';
    $('mosaicClose').disabled=mutationBusy;
    $('mosaicBatchStart').textContent=running?'正在生成视频…':'生成处理后视频';
    $('mosaicBatchHint').textContent=[`删除 ${deletions.length} 段 · 打码 ${segments} 段`,trackingUnavailable&&!running&&!awaitingDecision()?trackingUnavailableHint:'',legacyDraft&&!running&&!awaitingDecision()?legacyDraftHint:'',batchError].filter(Boolean).join('\n');
    $('mosaicBatchView').hidden=!batchJob&&!batchError;
    $('mosaicBatchView').textContent=batchJob?.status==='running'?'查看生成进度':batchJob?.status==='failed'?'查看失败原因':'查看生成结果';
    $('mosaicBatchClose').hidden=!showOutput;
    $('mosaicBatchStart').hidden=generating||awaitingDecision();
    $('mosaicBatchView').hidden=generating||resultMode||!!batchJob?.discarded_at||batchJob?.status==='cancelled'||(!batchJob&&!batchError);
    $('mosaicBatchDownload').hidden=!resultMode; $('mosaicReplace').hidden=!resultMode;
    if(!resultMode){beforeBinding?.dispose();beforeBinding=null;return;}
    const done=batchJob?.status==='completed';
    $('mosaicReplace').hidden=!done;$('mosaicReplace').disabled=mutationBusy||!sameBatch();
    $('mosaicBatchBefore').hidden=!done;$('mosaicBatchAfter').hidden=!done;
    $('mosaicResultLabel').hidden=!done;$('mosaicResultLabel').textContent=resultBefore?'正在查看：处理前 · 原视频':'正在查看：处理后 · 生成结果';
    $('mosaicBatchBefore').setAttribute('aria-pressed',String(resultBefore));$('mosaicBatchAfter').setAttribute('aria-pressed',String(!resultBefore));
    $('mosaicBatchDownload').hidden=!done;$('mosaicBatchVideo').hidden=!done;
    $('mosaicBatchVersion').textContent=batchJob&&!sameBatch()?(legacyJob(batchJob)?'此结果由旧版处理方式（跟随目标或旧效果选项）生成，与当前固定位置方案不同，不能直接替换；请放弃结果后重新生成。':'处理方案已变化。此结果对应生成时的选择；要应用新选择，请重新生成。'):'';
    if(done){
      $('mosaicBatchStatus').textContent=batchError||'处理完成，请预览；确认替换或放弃结果后可继续编辑。';
      const outputUrl=appUrl(base()+'/render/'+batchJob.id+'/video?v='+encodeURIComponent(batchJob.completed_at||''));
      if(resultBefore&&sourceManager){
        if(!beforeBinding){
          const version=epoch;
          beforeBinding=sourceManager.attach($('mosaicBatchVideo'),{valid:()=>active(version)&&resultMode&&resultBefore,anchor:$('mosaicBatchStatus')});
          beforeBinding.open();
        }
      }else{
        beforeBinding?.dispose();beforeBinding=null;
        const url=resultBefore?appUrl(base()+'/source'):outputUrl;
        if($('mosaicBatchVideo').getAttribute('src')!==url)$('mosaicBatchVideo').src=url;
      }
      $('mosaicBatchDownload').href=outputUrl;$('mosaicBatchDownload').download=context.asset.title+'_处理后.mp4';
    }else $('mosaicBatchStatus').textContent=EditorBusy.message(batchError||batchJob?.error||batchJob?.progress||'正在提交全部已打码区域…');
  }
  function restoreParameters(job){
    legacyDraft=legacyJob(job);effect=simplified(job.effect);clipEffects={};aiEvents=[];clipEdits={};manualClips=[];
    targets=(job.targets||[]).map((t,i)=>{
      const eventId='manual-recovered-region-'+i;
      manualClips.push({id:eventId,start:t.start,end:t.end,status:'needs_review',candidates:[]});
      clipEffects[eventId]=simplified({...job.effect,...t.effect});
      return {...t,motion:'fixed',eventId,uid:regionId(),adopted:true,label:'打码区域',reason:''};
    });
    deletions=(job.ranges||[]).map(([start,end],i)=>{
      const eventId='manual-recovered-delete-'+i;
      manualClips.push({id:eventId,start,end,status:'needs_review',candidates:[]});
      return {start,end,eventId};
    });
    savedTargets=structuredClone(targets);savedDeletions=structuredClone(deletions);
    savedManualClips=structuredClone(manualClips);savedAiEvents=[];savedClipEdits={};
    savedEffect={...effect};savedClipEffects=structuredClone(clipEffects);
  }
  async function batchRequest(start=false,previousJob=null){
    if(!context)return;
    const version=epoch,request=++batchToken,endpoint=base(),valid=()=>active(version)&&request===batchToken;
    const sourceSha=context.session.source_sha256;
    const submitted=start?structuredClone({targets:adoptedTargets(),ranges:deleteRanges(),effect,reset_at:context.session.reset_at||'',submission_id:regionId()}):null;
    batchLoading=true;batchError='';paintBatch();
    try{
      const result=await api(endpoint+'/render',submitted||undefined);
      if(!valid())return;batchJob=start?result:result.job||null;
      if(awaitingDecision())resultMode=true;
      paintBatch();
      while(valid()&&pendingJob()){
        await new Promise(r=>setTimeout(r,1200));if(!valid())return;
        try{
          const job=await api(endpoint+'/render/'+batchJob.id);if(!valid())return;batchJob=job;batchError='';
          if(awaitingDecision())resultMode=true;
        }catch(e){if(!valid())return;batchError='连接暂时异常，正在恢复查询…';}
        paintBatch();
      }
    }catch(e){
      if(valid()){
        batchError=e.message;
        const unknown=e.outcomeUnknown===true||e instanceof TypeError||e.name==='AbortError'||(e.status>=500&&e.status!==503);
        if(start&&!unknown){
          resultMode=false;status('提交失败：'+e.message,true);return;
        }
        // A lost response can precede admission. Never resubmit automatically or
        // mistake an older terminal report for acknowledgement of this attempt.
        if(start&&unknown){
          batchError='尚未确认是否提交成功，正在恢复查询…';paintBatch();
          while(valid()){
            await new Promise(r=>setTimeout(r,1200));if(!valid())return;
            try{
              const restored=await api(endpoint+'/render');if(!valid())return;
              const candidate=restored.job||null;
              const frozen=pendingJob(candidate)||awaitingDecision(candidate);
              const accepted=candidate?.queued_task&&candidate.submission_id===submitted.submission_id&&candidate.id!==previousJob?.id&&candidate.source_sha256===sourceSha&&(candidate.reset_at||'')===submitted.reset_at&&samePlan(candidate,submitted);
              if(!frozen&&!accepted){
                batchError='尚未确认是否提交成功，正在恢复查询…';paintBatch();continue;
              }
              batchJob=candidate;
              if(frozen)restoreParameters(candidate);
              batchError=accepted?(candidate.error||''):'提交回执未确认，已恢复素材现有任务。';
              resultMode=awaitingDecision();
              if(pendingJob()){batchLoading=false;batchRequest();return;}
              return;
            }catch{batchError='连接暂时异常，正在恢复查询…';paintBatch();}
          }
        }
      }
    }
    finally{if(valid()){batchLoading=false;paint();if(batchJob?.status==='cancelled')status('已停止生成，当前选择已保留，可以继续编辑。');}}
  }
  function restoreBatch(){batchRequest();}
  $('mosaicStop').onclick=async()=>{
    if(stopPending||!pendingJob())return;
    const version=epoch,endpoint=base(),jid=batchJob.id;stopPending=true;paintBatch();
    try{
      const job=await api(endpoint+'/stop',{job_id:jid,reset_at:context.session.reset_at||''});
      if(!active(version)||batchJob?.id!==jid)return;
      // Resume polling the acknowledged job; do not unlock until the worker exits.
      batchToken++;batchJob=job;
      while(active(version)&&batchJob?.id===jid&&pendingJob()){
        paintBatch();await new Promise(r=>setTimeout(r,500));
        if(!active(version))return;
        const next=await api(endpoint+'/render/'+jid);
        if(!active(version)||batchJob?.id!==jid)return;
        batchJob=next;
      }
      if(active(version)){batchLoading=false;resultMode=false;status(batchJob?.status==='cancelled'?'已停止生成，当前选择已保留，可以继续编辑。':'视频已生成，可查看结果。');}
    }catch(e){if(active(version)){status(e.message,true);batchRequest();}}
    finally{if(active(version)){stopPending=false;paint();}}
  };
  $('mosaicBatchStart').onclick=()=>{
    if($('mosaicBatchStart').disabled)return;
    const saved=targets[editing],c=selectedBox();
    if(saved&&saved.adopted!==false){try{const r=range();if(!c||['x','y','w','h'].some(k=>c[k]!==saved[k])||r.start!==saved.start||r.end!==saved.end)throw Error('请先保存当前区域调整，再生成完整视频。');}catch(e){status(e.message,true);return;}}
    const previousJob=batchJob;
    batchJob=null;resultBefore=false;$('mosaicBatchVideo').pause();$('mosaicBatchVideo').removeAttribute('src');$('mosaicBatchVideo').load();
    showResult(true);batchRequest(true,previousJob);
  };
  $('mosaicBatchView').onclick=()=>{showResult(true);paintBatch();};
  $('mosaicBatchClose').onclick=()=>{if(!mutationBusy)showResult(false);};


  function showResult(show){
    resultMode=show;
    if(show){pausePlayback();}else if(!show){$('mosaicBatchVideo').pause();sourceBinding?.open();loadFrame();}
    paintBatch();
  }
  function clearDraft(){for(const k of [draftKey(),oldDraftKey(),key()])localStorage.removeItem(k);}
  $('mosaicDiscard').onclick=async()=>{
    if(mutationBusy||!window.confirm('放弃本次处理？当前项目成片不会改变。'))return;
    const version=epoch;mutationBusy=true;paintBatch();
    try{
      if(awaitingDecision()){
        const job=await api(base()+'/discard-result',{job_id:batchJob.id,reset_at:context.session.reset_at||''});
        if(!active(version))return;batchJob=job;save();resultMode=false;paint();
      }else{
        await api(base()+'/discard',{reset_at:context.session.reset_at||''});if(!active(version))return;clearDraft();dialog.close();
      }
    }
    catch(e){if(active(version))status(e.message,true);}
    finally{mutationBusy=false;if(active(version))paintBatch();}
  };
  $('mosaicReplace').onclick=async()=>{
    if(mutationBusy||!sameBatch()||!window.confirm('用处理后的视频替换项目成片？后续预览和上传将使用新视频。'))return;
    const version=epoch,batch=context.batch;mutationBusy=true;paintBatch();
    try{
      let job=await api(base()+'/replace',{job_id:batchJob.id,reset_at:context.session.reset_at||''});
      while(active(version)&&job.replacement?.status==='running'){
        $('mosaicBatchStatus').textContent='正在替换项目成片…';await new Promise(r=>setTimeout(r,1000));
        if(!active(version))return;job=await api(base()+'/render/'+job.id);
      }
      if(!active(version))return;
      if(job.replacement?.status!=='completed')throw Error(job.replacement?.error||'替换未完成，请重试');
      clearDraft();const updated=await api(`/api/batches/${batch}`);
      if(current?.id===batch){current=updated;renderRuntime();}
      showResult(false);dialog.close();toast('已替换项目成片。');
    }catch(e){if(active(version)){batchError=e.message;$('mosaicBatchStatus').textContent=EditorBusy.message(e.message,'视频处理未完成，请重试');}}
    finally{mutationBusy=false;if(active(version))paintBatch();}
  };
  function outputFrame(frame){return frame-(batchJob?.ranges||[]).reduce((n,[a,b])=>n+Math.max(0,Math.min(frame,b)-a),0);}
  function sourceFrame(frame){let start=0,remaining=frame;for(const [a,b] of batchJob?.ranges||[]){if(remaining<a-start)return start+remaining;remaining-=a-start;start=b;}return start+remaining;}
  function compareResult(before){
    const p=$('mosaicBatchVideo'),frame=p.currentTime*rate(),version=epoch;
    const mapped=before?sourceFrame(frame):outputFrame(frame);
    p.onloadedmetadata=null;resultBefore=before;paintBatch();
    if(beforeBinding){beforeBinding.seek(mapped/rate());return;}
    const url=p.getAttribute('src');
    p.onloadedmetadata=()=>{if(active(version)&&resultBefore===before&&p.getAttribute('src')===url)p.currentTime=mapped/rate();p.onloadedmetadata=null;};
  }
  $('mosaicBatchBefore').onclick=()=>{if(!resultBefore)compareResult(true);};
  $('mosaicBatchAfter').onclick=()=>{if(resultBefore)compareResult(false);};
  $('mosaicSaveTime').onclick=guard(()=>{
    if(editLocked())return;
    const event=allEvents().find(e=>e.id===scanEvent);if(!event)return;
    setEventRange(event,range());if(save()){paint();status('片段时间已保存。');}
  });
  $('mosaicSaveDelete').onclick=guard(()=>{const event=allEvents().find(e=>e.id===scanEvent);if(!event)return;const r=range();setEventRange(event,r);deletions=deletions.filter(t=>!belongs(t,event));deletions.push({...r,eventId:event.id,originStart:event.start,originEnd:event.end,label:event.label});targets.filter(t=>belongs(t,event)).forEach(t=>t.adopted=false);if(save())paint();});
  function currentClip(){const event=allEvents().find(e=>e.id===scanEvent);return event?eventRange(event):targets[editing]||null;}
  async function restorePreviewNotice(version){
    const storageKey=key().replace('targets.v1.','preview-job.v1.'),endpoint=base();
    $('mosaicResumePreview').hidden=true;
    try{
      const saved=JSON.parse(localStorage.getItem(storageKey)||'null');
      if(!saved||!['track','clip'].includes(saved.kind)||!/^([a-f0-9]{32})$/.test(saved.id))return;
      const job=await api(endpoint+(saved.kind==='track'?'/track/':'/render/')+saved.id);
      if(!active(version)||pendingJob()||awaitingDecision()||!['queued','running','cancelling','completed'].includes(job.status))return;
      $('mosaicResumePreview').hidden=false;
      $('mosaicResumePreview').textContent=pendingJob(job)?'片段预览在后台等待/处理中 · 查看':'查看上次片段预览（不改变当前设置）';
      $('mosaicResumePreview').onclick=()=>runPreview(saved.kind,saved.body,job);
    }catch(error){/* A stale preview receipt never blocks this material's editor. */}
  }
  function detachClipPreview(){if(previewOperation)previewOperation.closed=true;}
  const previewCurrent=op=>previewOperation===op&&active(op.version)&&!op.closed&&$('mosaicTrackDialog').open&&!pendingJob()&&!batchLoading&&!awaitingDecision();
  async function stopClipPreview(op=previewOperation){
    if(!op)return;
    op.closed=true;
    if(pendingJob(op.job)&&!op.stopping&&!op.stopSent){
      op.stopping=true;
      try{
        const reply=await api(op.kind==='track'?op.endpoint+'/track/'+op.job.id+'/stop':op.endpoint+'/stop',
          op.kind==='track'?{reset_at:op.reset}:{job_id:op.job.id,reset_at:op.reset});
        op.stopSent=true;
        if(reply?.id===op.job.id&&reply.status)op.job=reply;
      }catch(e){if(previewOperation===op&&active(op.version)&&!pendingJob()&&!batchLoading&&!awaitingDecision())status('停止预览未成功，正在等待任务结束。',true);}
      finally{op.stopping=false;}
    }
  }
  async function runPreview(kind,body,acknowledged=null){
    const storageKey=key().replace('targets.v1.','preview-job.v1.');
    const op={kind,body,endpoint:base(),reset:context.session.reset_at||'',version:epoch,job:null,busy:true,closed:false,started:Date.now(),stopping:false,stopSent:false};
    previewOperation=op;clipBounds=null;trackToken++;const receiptToken=regionId();paint();
    $('mosaicTrackTitle').textContent=kind==='track'?'动态打码预览':'本片段效果预览';
    $('mosaicTrackHint').textContent=kind==='track'?'仅预览当前区域、当前片段。':'';
    $('mosaicTrackStatus').textContent='正在提交本片段预览…';$('mosaicTrackVideo').hidden=true;$('mosaicTrackStop').disabled=true;
    $('mosaicTrackClose').textContent=kind==='track'?'返回检查':'返回设置';$('mosaicTrackDialog').classList.add('is-loading');$('mosaicTrackDialog').showModal();
    try{
      localStorage.setItem(storageKey+'.request',receiptToken);
      op.job=acknowledged||await api(op.endpoint+(kind==='track'?'/track':'/clip-preview'),body);
      if(localStorage.getItem(storageKey+'.request')===receiptToken)localStorage.setItem(storageKey,JSON.stringify({kind,body,id:op.job.id}));
      if(previewOperation===op)$('mosaicTrackStop').disabled=!pendingJob(op.job);
      if(op.closed||!active(op.version)||previewOperation!==op)return;
      while(pendingJob(op.job)&&!op.closed&&active(op.version)&&previewOperation===op){
        if(!pendingJob(op.job))break;
        if(previewCurrent(op)){
          const seconds=Math.floor((Date.now()-op.started)/1000),fixed=(body.targets||[body.target]).every(t=>t.motion==='fixed');
          $('mosaicTrackStatus').textContent=(op.job.status==='queued'?'预览已排队，可返回继续编辑；后台轮到后自动处理。':kind==='track'?op.job.progress:null)||
            (op.job.stage==='composing'?'正在合成片段预览':fixed?'正在准备固定位置打码':'正在跟踪片段')+' · 已等待 '+Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0');
        }
        await new Promise(r=>setTimeout(r,1000));
        op.job=await api(op.endpoint+(kind==='track'?'/track/':'/render/')+op.job.id);
      }
      if(!previewCurrent(op))return;
      if(op.job.status!=='completed')throw Error(op.job.error||'预览未完成，请重试。');
      $('mosaicTrackStatus').textContent=kind==='track'?clock(body.target.start)+'–'+clock(body.target.end-1)+' · 预览已生成。':'预览已生成';
      $('mosaicTrackVideo').src=appUrl(op.endpoint+(kind==='track'?'/track/':'/render/')+op.job.id+(kind==='track'?'/preview':'/video')+'?v='+encodeURIComponent(op.job.completed_at||''));
      $('mosaicTrackVideo').hidden=false;
    }catch(e){if(previewCurrent(op))$('mosaicTrackStatus').textContent=EditorBusy.message(e.message,'预览未完成，请重试');}
    finally{
      op.busy=false;
      if(previewOperation===op){$('mosaicTrackDialog').classList.remove('is-loading');if(active(op.version))paint();}
    }
  }
  $('mosaicEffectPreview').onclick=()=>{
    if(editLocked()||!effectDraft||!effectEditKey)return;
    const selectedTargets=targets.filter(t=>t.adopted!==false&&clipKey(t)===effectEditKey).map(t=>({...renderTarget(t),effect:{...effectDraft}}));
    if(selectedTargets.length)return runPreview('clip',{targets:selectedTargets,effect:{...effectDraft},reset_at:context.session.reset_at||''});
  };
  $('mosaicTrackVideo').addEventListener('loadedmetadata',()=>{if(clipBounds)$('mosaicTrackVideo').currentTime=clipBounds.start;});
  $('mosaicTrackVideo').addEventListener('play',()=>{const p=$('mosaicTrackVideo');if(clipBounds&&(p.currentTime<clipBounds.start||p.currentTime>=clipBounds.end))p.currentTime=clipBounds.start;});
  $('mosaicTrackVideo').addEventListener('timeupdate',()=>{const p=$('mosaicTrackVideo');if(clipBounds&&p.currentTime>=clipBounds.end){p.pause();p.currentTime=clipBounds.start;}});
  $('mosaicTrackClose').onclick=()=>{$('mosaicTrackDialog').close();};
  $('mosaicTrackStop').onclick=()=>{stopClipPreview();$('mosaicTrackDialog').close();};
  $('mosaicTrackDialog').addEventListener('close',()=>{
    if($('mosaicTrackDialog').open)return;
    detachClipPreview();if(context){paint();restorePreviewNotice(epoch);}$('mosaicTrackClose').textContent='返回检查';trackToken++;clipBounds=null;
    $('mosaicTrackVideo').pause();$('mosaicTrackVideo').removeAttribute('src');$('mosaicTrackVideo').load();
  });
  $('mosaicTrack').onclick=()=>{
    if(editLocked())return;
    const saved=targets[editing],c=selectedBox();if(!saved||saved.adopted===false)return;
    try{const r=range();if(!c||['x','y','w','h'].some(k=>c[k]!==saved[k])||r.start!==saved.start||r.end!==saved.end)throw Error('请先保存区域调整，再开始跟踪。');}catch(e){status(e.message,true);return;}
    const target=Object.fromEntries(['start','end','seed_frame','x','y','w','h'].map(k=>[k,saved[k]]));
    return runPreview('track',{target,effect:effectFor(saved),reset_at:context.session.reset_at||''});
  };
  $('mosaicClear').onclick=()=>{if(window.confirm('取消全部处理选择？区域调整会保留。')){deletions=[];targets.forEach(t=>t.adopted=false);if(save())paint();}};
  $('mosaicAddRegion').onclick=()=>{
    if(editLocked()||$('mosaicAddRegion').disabled)return;

    selected=null;editing=null;drawing=true;$('mosaicAdjust').open=true;paint();status('在画面拖出新区域，原有区域会保留。');
  };
  $('mosaicRemoveRegion').onclick=()=>{
    if(editLocked())return;
    const c=selectedBox();if(!c)return;
    const i=c.uid?targets.findIndex(t=>t.uid===c.uid):-1;
    const event=allEvents().find(e=>e.id===scanEvent);
    if(i>=0){
      if(event&&!event.id.startsWith('manual-')){targets[i].removed=true;targets[i].adopted=false;}
      else targets.splice(i,1);
      if(!save())return;
    }else if(event&&!event.id.startsWith('manual-')&&event.candidates.some(v=>v.id===c.id)){
      if(targets.length>=320){status('本机草稿区域已达上限。',true);return;}
      targets.push({...c,motion:'fixed',uid:regionId(),eventId:event.id,originStart:event.start,originEnd:event.end,proposal:c.id,adopted:false,removed:true,start:eventRange(event).start,end:eventRange(event).end,seed_frame:Math.max(eventRange(event).start,Math.min(eventRange(event).end-1,wanted))});
      if(!save())return;
    }
    regionDrafts=regionDrafts.filter(v=>v.id!==c.id);candidates=candidates.filter(v=>v!==c);selectRegion(candidates[0]?.id??null);drawing=false;paint();status('已移除区域。');
  };
  $('mosaicDraw').onclick=()=>{if($('mosaicDraw').disabled)return;drawing=!drawing;paint();if(drawing)status('在画面内拖出矩形；也可用像素输入框微调。');};
  function point(e){const r=overlay.getBoundingClientRect();return {x:Math.max(0,Math.min(meta().width,Math.round((e.clientX-r.left)/r.width*meta().width))),y:Math.max(0,Math.min(meta().height,Math.round((e.clientY-r.top)/r.height*meta().height)))};}
  overlay.onpointerdown=e=>{
    if(editLocked()||e.button!==0||ready!==wanted||image.hidden||!video.paused)return;
    const b=e.target.closest('[data-box]'),p=point(e);
    if(b&&!drawing){
      selectRegion(b.dataset.box);
      const bounds=b.getBoundingClientRect(),edge=(Math.abs(e.clientY-bounds.top)<=6?'n':Math.abs(e.clientY-bounds.bottom)<=6?'s':'')+(Math.abs(e.clientX-bounds.left)<=6?'w':Math.abs(e.clientX-bounds.right)<=6?'e':'');
      pointer={...p,id:e.pointerId,mode:e.target.closest('[data-resize]')?.dataset.resize||edge||'move',original:{...selectedBox()}};
      drawing=false;
    }else{
      if(!drawing)return;
      pointer={...p,id:e.pointerId,mode:'draw'};
      selected=selected??'manual-'+regionId();const next={id:selected,motion,seed_frame:wanted,label:'手动区域',reason:'人工框选',x:p.x,y:p.y,w:1,h:1};
      candidates=candidates.filter(c=>c.id!==selected);candidates.push(next);
    }
    e.preventDefault();overlay.setPointerCapture(e.pointerId);drawOverlay();
  };
  overlay.onpointermove=e=>{
    if(!pointer||e.pointerId!==pointer.id||editLocked())return;
    const p=point(e),c=selectedBox(),o=pointer.original,dx=p.x-pointer.x,dy=p.y-pointer.y;
    if(!c)return;
    if(pointer.mode==='draw')Object.assign(c,{x:Math.min(p.x,pointer.x),y:Math.min(p.y,pointer.y),w:Math.abs(dx),h:Math.abs(dy)});
    else if(pointer.mode==='move')Object.assign(c,{x:Math.max(0,Math.min(meta().width-o.w,o.x+dx)),y:Math.max(0,Math.min(meta().height-o.h,o.y+dy))});
    else{
      let left=o.x,right=o.x+o.w,top=o.y,bottom=o.y+o.h;
      if(pointer.mode.includes('w'))left=Math.max(0,Math.min(right-1,o.x+dx));
      if(pointer.mode.includes('e'))right=Math.min(meta().width,Math.max(left+1,o.x+o.w+dx));
      if(pointer.mode.includes('n'))top=Math.max(0,Math.min(bottom-1,o.y+dy));
      if(pointer.mode.includes('s'))bottom=Math.min(meta().height,Math.max(top+1,o.y+o.h+dy));
      Object.assign(c,{x:left,y:top,w:right-left,h:bottom-top});
    }
    drawOverlay();
  };
  overlay.onpointerup=e=>{
    if(!pointer||e.pointerId!==pointer.id)return;
    pointer=null;drawing=false;
    if(overlay.hasPointerCapture(e.pointerId))overlay.releasePointerCapture(e.pointerId);
    const c=selectedBox();if(!validBox(c)){candidates=candidates.filter(v=>v!==c);selected=candidates[0]?.id??null;}
    $('mosaicAdjust').open=true;paint();status('拖动框内可移动，拖动边框可缩放；调整后保存区域。');
  };
  overlay.onpointercancel=()=>{
    if(pointer?.original&&selectedBox())Object.assign(selectedBox(),pointer.original);
    pointer=null;drawing=false;candidates=candidates.filter(validBox);paint();
  };
  function skipDeletedPlayback(){
    if(!context||resultMode||compareOriginal||video.paused||sourceBinding?.changing)return false;
    const frame=Math.floor(video.currentTime*rate()+.0001),range=mergedRanges(deleteRanges()).find(([start,end])=>frame>=start&&frame<end);
    if(!range)return false;
    if(range[1]>=meta().frame_count){
      pausePlayback();wanted=Math.max(0,range[0]-1);video.currentTime=(wanted+.05)/rate();
      paint();if(range[0]>0)loadFrame();
      status(range[0]>0?'已到保留内容末尾。':'整段视频已标记删除，没有可播放的保留内容。');
    }else{
      video.currentTime=(range[1]+.05)/rate();
      if(video.currentTime<range[1]/rate()){
        pausePlayback();status('删除区间暂时无法跳过，请等待视频加载后重试。',true);
      }else wanted=range[1];
    }
    return true;
  }
  $('mosaicPlay').onclick=()=>{if(video.paused&&!sourceBinding?.wantsPlay){controller?.abort();resetCandidate();ready=-1;image.hidden=true;overlay.hidden=true;playSource();}else{const time=sourceBinding?.position??video.currentTime;pausePlayback();wanted=Math.min(meta().frame_count-1,Math.floor(time*rate()));paint();loadFrame();}};
  video.onplay=()=>{skipDeletedPlayback();if(video.paused)return;$('mosaicPlay').textContent='暂停';image.hidden=true;overlay.hidden=true;paint();};
  video.onpause=()=>{if(sourceBinding?.changing||!video.paused)return;$('mosaicPlay').textContent='播放';};
  video.ontimeupdate=()=>{if(skipDeletedPlayback())return;if(context&&!sourceBinding?.changing&&!video.paused){wanted=Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));paint();}};
  video.onended=()=>{if(context){wanted=Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));paint();loadFrame();}};
  new ResizeObserver(drawOverlay).observe($('mosaicStage'));
  // Keep the existing editor state and persistence contract as the single source of truth.
  const library=document.createElement('aside');library.className='mosaic-library';library.setAttribute('aria-label','片段列表');
  library.append(dialog.querySelector('.mosaic-scan-section'));$('mosaicWorkspace').prepend(library);
  dialog.querySelector('.mosaic-header').insertBefore(dialog.querySelector('.mosaic-batch-footer'),$('mosaicClose'));
  const liveCanvas=$('mosaicLiveCanvas'),liveContext=liveCanvas.getContext('2d');
  const liveEffect=document.createElement('canvas'),liveMask=document.createElement('canvas'),liveSmall=document.createElement('canvas');
  const timeline=$('mosaicTimelineTracks'),thumbReader=document.createElement('video');
  thumbReader.muted=true;thumbReader.preload='metadata';
  let timelineDrag=null,timelineSignature='',thumbnailSource='',thumbnailEpoch=0,liveAnimation=0,compareOriginal=false;
  function timelineItems(){
    const events=allEvents(),items=events.map(e=>{
      const r=eventRange(e),records=targets.filter(t=>belongs(t,e)&&!t.removed&&t.adopted!==false);
      return {id:e.id,event:e,...r,kind:deletions.some(t=>belongs(t,e))?'deletion':records.length?'mosaic':'suggestion'};
    });
    targets.forEach((t,index)=>{if(!t.removed&&!events.some(e=>belongs(t,e)))items.push({...t,id:'target:'+t.uid,index,kind:t.adopted!==false?'mosaic':'suggestion'});});
    return items.filter(item=>item.kind!=='suggestion');
  }
  function paintTimeline(){
    if(!context)return;
    $('mosaicTimelineHead').style.left=100*wanted/meta().frame_count+'%';
    if(timelineDrag)return;
    const items=timelineItems(),signature=JSON.stringify([meta().frame_count,items,scanEvent,editing,editLocked()]);
    if(signature!==timelineSignature){
      timelineSignature=signature;const ends=[];
      const blocks=[],deletionOverlays=[];
      for(const item of items.sort((a,b)=>a.start-b.start)){
        let row=0;
        if(item.kind!=='deletion'){row=ends.findIndex(end=>end<=item.start);if(row<0)row=ends.length;ends[row]=item.end;}
        const selected=item.event?scanEvent===item.id:editing===item.index;
        const label=item.kind==='deletion'?'删除':'打码';
        const html=`<button type="button" class="mosaic-timeline-block ${item.kind} ${selected?'selected':''}" data-timeline-event="${esc(item.id)}" data-start="${item.start}" data-end="${item.end}" aria-label="${label} ${esc(item.label)} ${clock(item.start)}–${clock(item.end-1)}" aria-pressed="${selected}" title="${label} · ${clock(item.start)}–${clock(item.end-1)}" style="left:${100*item.start/meta().frame_count}%;width:${100*(item.end-item.start)/meta().frame_count}%;top:${item.kind==='deletion'?0:row*32+3}px"><i data-edge="start"></i>${item.kind==='deletion'?'<span class="mosaic-deletion-label">删除</span>':label+' · '+esc(item.label)}<i data-edge="end"></i></button>`;
        (item.kind==='deletion'?deletionOverlays:blocks).push(html);
      }
      $('mosaicTimelineBlocks').innerHTML=blocks.join('');
      $('mosaicTimelineDeletions').innerHTML=deletionOverlays.join('');
      const blockHeight=ends.length?ends.length*32+6:0;
      $('mosaicTimelineBlocks').style.height=blockHeight+'px';
      $('mosaicWorkspace').style.setProperty('--mosaic-timeline-height',Math.min(219,25+blockHeight+58+2)+'px');
      $('mosaicTimelineRuler').innerHTML=Array.from({length:8},(_,i)=>{const f=Math.floor(i*meta().frame_count/8);return `<span style="left:${i*12.5}%">${clock(f)}</span>`;}).join('');
    }
    scheduleThumbnails();
  }
  function timelineFrame(event){return Math.max(0,Math.min(meta().frame_count,Math.round((event.clientX-timeline.getBoundingClientRect().left)/timeline.clientWidth*meta().frame_count)));}
  timeline.onpointerdown=event=>{
    if(!context||event.button!==0)return;
    const button=event.target.closest('[data-timeline-event]'),item=button&&timelineItems().find(i=>i.id===button.dataset.timelineEvent);
    if(item&&editLocked())return;
    event.preventDefault();timeline.setPointerCapture(event.pointerId);
    timelineDrag={pointer:event.pointerId,item,button,start:timelineFrame(event),edge:event.target.dataset.edge,moved:false,version:epoch};
    if(!item)seek(Math.min(meta().frame_count-1,timelineFrame(event)),true);
  };
  timeline.onpointermove=event=>{
    const drag=timelineDrag;if(!drag||drag.pointer!==event.pointerId||drag.version!==epoch)return;
    const frame=timelineFrame(event);if(!drag.item){seek(Math.min(meta().frame_count-1,frame),true);return;}
    if(editLocked())return;
    const delta=frame-drag.start,item=drag.item;drag.moved=drag.moved||delta!==0;
    let start=item.start,end=item.end;
    if(drag.edge==='start')start=Math.max(0,Math.min(end-1,start+delta));
    else if(drag.edge==='end')end=Math.min(meta().frame_count,Math.max(start+1,end+delta));
    else{const offset=Math.max(-start,Math.min(meta().frame_count-end,delta));start+=offset;end+=offset;}
    drag.range={start,end};Object.assign(drag.button.style,{left:100*start/meta().frame_count+'%',width:100*(end-start)/meta().frame_count+'%'});
    drag.button.title=`${clock(start)}–${clock(end-1)}`;
  };
  timeline.onpointerup=event=>{
    const drag=timelineDrag;if(!drag||drag.pointer!==event.pointerId)return;
    timelineDrag=null;if(timeline.hasPointerCapture(event.pointerId))timeline.releasePointerCapture(event.pointerId);
    if(!context||drag.version!==epoch)return;
    const item=drag.item;
    if(item&&!editLocked()){
      if(drag.moved&&drag.range){
        if(item.event)setEventRange(item.event,drag.range);
        else{const t=targets[item.index];Object.assign(t,drag.range);t.seed_frame=Math.max(t.start,Math.min(t.end-1,t.seed_frame));}
        if(!save()){timelineSignature='';paint();return;}
      }
      if(item.event)viewScanEvent(item.id);else viewTarget(item.index);
      if(drag.moved)status('时间已保存，播放即可预览。');
    }
    timelineSignature='';paint();
  };
  timeline.onpointercancel=()=>{timelineDrag=null;timelineSignature='';if(context)paint();};
  let thumbnailCancel=null,thumbnailCacheKey='';
  const thumbnailCache=new Map();
  new ResizeObserver(()=>{if(context&&dialog.open)scheduleThumbnails();}).observe(timeline);
  function scheduleThumbnails(){
    if(!context||!dialog.open||!timeline.clientWidth)return;
    const rawSource=video.currentSrc||video.getAttribute('src');if(!rawSource)return;
    const source=new URL(rawSource,location.href).href,total=meta().frame_count,fps=rate();
    const count=Math.max(1,Math.ceil(timeline.clientWidth/56));
    const cacheKey=JSON.stringify([source,context.session.source_sha256,total,fps]);
    const signature=cacheKey+':'+count;if(signature===thumbnailSource)return;
    thumbnailSource=signature;const version=++thumbnailEpoch;
    thumbnailCancel?.();thumbnailCancel=null;
    if(cacheKey!==thumbnailCacheKey){thumbnailCache.clear();thumbnailCacheKey=cacheKey;}
    const strip=$('mosaicTimelineThumbs');strip.replaceChildren();strip.removeAttribute('title');
    const pending=[];
    for(let i=0;i<count;i++){
      const start=Math.floor(i*total/count),end=Math.max(start+1,Math.floor((i+1)*total/count)),middle=Math.floor((start+end)/2);
      const cached=[...thumbnailCache.keys()].filter(f=>f>=start&&f<end).sort((a,b)=>Math.abs(a-middle)-Math.abs(b-middle))[0];
      const frame=Math.min(total-1,cached??middle),c=document.createElement('canvas');c.width=112;c.height=116;
      c.setAttribute('aria-label','原片缩略图 '+clock(frame));strip.append(c);
      if(cached!==undefined)c.getContext('2d').drawImage(thumbnailCache.get(cached),0,0);
      else pending.push({index:i,frame,canvas:c});
    }
    if(!pending.length)return;
    // First cover the whole strip sparsely, then fill the gaps.
    pending.sort((a,b)=>(a.index%4!==0)-(b.index%4!==0)||a.index-b.index);
    const wait=type=>new Promise((resolve,reject)=>{
      const finish=error=>{clearTimeout(timer);thumbReader.removeEventListener(type,done);thumbReader.removeEventListener('error',fail);if(thumbnailCancel===cancel)thumbnailCancel=null;error?reject(error):resolve();};
      const done=()=>finish(),fail=()=>finish(Error('缩略图不可用')),cancel=()=>finish(Error('缩略图读取已取消')),timer=setTimeout(()=>finish(Error('缩略图读取超时')),5000);
      thumbnailCancel=cancel;thumbReader.addEventListener(type,done,{once:true});thumbReader.addEventListener('error',fail,{once:true});
    });
    (async()=>{
      try{
        if(thumbReader.src!==source){thumbReader.src=source;thumbReader.load();}
        if(thumbReader.readyState<2)await wait('loadeddata');
        for(const {frame,canvas:c} of pending){
          if(version!==thumbnailEpoch||!dialog.open)return;
          const time=(frame+.05)/fps;
          if(Math.abs(thumbReader.currentTime-time)>.0001){const seeked=wait('seeked');thumbReader.currentTime=time;await seeked;}
          if(version!==thumbnailEpoch||!dialog.open)return;
          const ctx=c.getContext('2d'),scale=Math.max(c.width/thumbReader.videoWidth,c.height/thumbReader.videoHeight);
          ctx.drawImage(thumbReader,(c.width-thumbReader.videoWidth*scale)/2,(c.height-thumbReader.videoHeight*scale)/2,thumbReader.videoWidth*scale,thumbReader.videoHeight*scale);
          thumbnailCache.set(frame,c);
          if(thumbnailCache.size>160)thumbnailCache.delete(thumbnailCache.keys().next().value);
        }
      }catch{if(version===thumbnailEpoch)strip.title='缩略图暂不可用，可继续定位与编辑。';}
    })();
  }
  function selectedLiveTarget(){const event=allEvents().find(e=>e.id===scanEvent);return targets.find(t=>t.adopted!==false&&!t.removed&&(event?belongs(t,event):t===targets[editing]))||null;}
  function paintLiveControls(){
    if(!context)return;
    const target=selectedLiveTarget(),appearance=effectFor(target);
    for(const k of ['style','strength','expansion']){const el=$('mosaicLive'+k[0].toUpperCase()+k.slice(1));el.value=appearance[k];el.disabled=editLocked()||!target||target.adopted===false;}
    $('mosaicLiveControls').hidden=!target;
    $('mosaicLivePreviewAction').innerHTML='';
    $('mosaicInspectorHint').hidden=!!target||choosingMosaic||addingClip;
    $('mosaicInspectorHint').textContent='选择左侧片段的“打码”，即可编辑区域与效果。';
  }
  for(const k of ['style','strength','expansion'])$('mosaicLive'+k[0].toUpperCase()+k.slice(1)).onchange=()=>{
    const target=selectedLiveTarget();if(!target||editLocked()||target.adopted===false)return;
    clipEffects[clipKey(target)]={...effectFor(target),[k]:$('mosaicLive'+k[0].toUpperCase()+k.slice(1)).value};
    if(save()){paint();status('效果已保存，播放即可预览。');}
  };
  $('mosaicLivePreviewAction').onclick=e=>{const button=e.target.closest('[data-effect-target]');if(button)openEffects(Number(button.dataset.effectTarget));};
  $('mosaicCompare').onclick=()=>{compareOriginal=!compareOriginal;$('mosaicCompare').setAttribute('aria-pressed',String(compareOriginal));$('mosaicCompare').textContent=compareOriginal?'返回效果':'对比原画';drawLivePreview();};
  function drawLivePreview(){
    liveCanvas.hidden=true;
    if(!context||!dialog.open||resultMode||document.hidden||compareOriginal)return;
    const still=!image.hidden&&ready===wanted,source=still?image:video;
    if(still?!image.complete:video.readyState<2)return;
    const scale=Math.min(1,960/Math.max(meta().width,meta().height)),w=Math.round(meta().width*scale),h=Math.round(meta().height*scale);
    if(liveCanvas.width!==w||liveCanvas.height!==h){for(const c of [liveCanvas,liveEffect,liveMask]){c.width=w;c.height=h;}}
    const frame=still?wanted:Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));
    let regions=targets.filter(t=>t.adopted!==false&&!t.removed&&frame>=t.start&&frame<t.end);
    if(still&&inRange()&&candidates.length){const ids=new Set(candidates.map(c=>c.uid).filter(Boolean));regions=regions.filter(t=>!ids.has(t.uid));regions.push(...candidates.filter(validBox).map(c=>({...c,motion:c.motion||motion,...range()})));}
    const tracked=regions.some(t=>t.motion!=='fixed');
    $('mosaicLiveHint').textContent=tracked?'当前含跟随目标区域：此处仅显示固定区域效果，跟踪效果请用动态预览检查。':'固定区域实时预览 · 最终效果请在生成后复核。';
    regions=regions.filter(t=>t.motion==='fixed');if(!regions.length)return;
    liveContext.clearRect(0,0,w,h);
    try{
      liveContext.drawImage(source,0,0,w,h);
      for(const target of regions){
        const appearance=effectDraft&&clipKey(target)===effectEditKey?effectDraft:effectFor(target.uid?targets.find(t=>t.uid===target.uid)||target:selectedLiveTarget());
        const ec=liveEffect.getContext('2d'),mc=liveMask.getContext('2d');ec.clearRect(0,0,w,h);ec.filter='none';ec.globalCompositeOperation='source-over';
        const ratio=Math.max(w,h)/640;
        if(appearance.style==='solid'){ec.fillStyle='#000';ec.fillRect(0,0,w,h);}
        else if(appearance.style==='pixel'){
          const block=Math.max(1,Math.round({light:16,medium:30,heavy:50}[appearance.strength]*ratio));liveSmall.width=Math.max(1,Math.floor(w/block));liveSmall.height=Math.max(1,Math.floor(h/block));
          liveSmall.getContext('2d').drawImage(source,0,0,liveSmall.width,liveSmall.height);ec.imageSmoothingEnabled=false;ec.drawImage(liveSmall,0,0,w,h);ec.imageSmoothingEnabled=true;
        }else{ec.filter=`blur(${{light:8,medium:16,heavy:28}[appearance.strength]*ratio}px)`;ec.drawImage(source,0,0,w,h);ec.filter='none';}
        const expansion=Math.round(Math.min(w,h)*{none:0,small:.008,medium:.016,large:.03}[appearance.expansion]);
        const x=target.x*scale-expansion,y=target.y*scale-expansion,rw=target.w*scale+2*expansion,rh=target.h*scale+2*expansion;
        mc.clearRect(0,0,w,h);mc.fillStyle='#fff';mc.filter='none';
        if(appearance.style==='soft_blur'){const radius=Math.max(1,Math.round(Math.min(w,h)*.015));mc.filter=`blur(${radius}px)`;mc.fillRect(x-radius,y-radius,rw+radius*2,rh+radius*2);mc.filter='none';}
        mc.fillRect(x,y,rw,rh);ec.globalCompositeOperation='destination-in';ec.drawImage(liveMask,0,0);ec.globalCompositeOperation='source-over';liveContext.drawImage(liveEffect,0,0);
      }
      liveCanvas.hidden=false;
    }catch{liveCanvas.hidden=true;$('mosaicLiveHint').textContent='实时预览暂不可用，可使用动态预览检查效果。';}
  }
  function animateLivePreview(){cancelAnimationFrame(liveAnimation);if(!context||!dialog.open||video.paused||document.hidden)return;if(!skipDeletedPlayback())drawLivePreview();if(!video.paused)liveAnimation=requestAnimationFrame(animateLivePreview);}
  video.addEventListener('playing',animateLivePreview);video.addEventListener('seeked',drawLivePreview);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)cancelAnimationFrame(liveAnimation);else if(context&&dialog.open){if(video.paused)drawLivePreview();else animateLivePreview();}});
  function stopWorkspacePreview(){
    cancelAnimationFrame(liveAnimation);thumbnailEpoch++;thumbnailCancel?.();thumbnailCancel=null;thumbnailSource='';timelineSignature='';timelineDrag=null;thumbReader.pause();thumbReader.removeAttribute('src');thumbReader.load();liveCanvas.hidden=true;compareOriginal=false;$('mosaicCompare').setAttribute('aria-pressed','false');$('mosaicCompare').textContent='对比原画';
  }
  window.Mosaic={mount};mount();
})();
