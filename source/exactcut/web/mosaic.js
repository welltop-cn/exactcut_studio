/* Independent source-frame region review. No deletion or rendering recipe. */
(() => {
  const dialog=$('mosaicDialog'), video=$('mosaicSource'), image=$('mosaicFrame'), overlay=$('mosaicOverlay');
  let context=null, epoch=0, token=0, wanted=0, ready=-1, candidates=[], selected=null, targets=[], editing=null;
  let motion='tracked';
  let loading=false, frameLoading=false, drawing=false, pointer=null, frameUrl=null, controller=null, timer;
  let batchJob=null,batchToken=0,batchLoading=false,batchError="";
  let trackToken=0,clipBounds=null;
  let deletions=[],savedDeletions=[],oldDraftRaw=null,mutationBusy=false,resultBefore=false,resultMode=false;
  let stopPending=false,previewBusy=false,previewClose=false,previewJob=null,previewEndpoint=null,previewReset="";
  const editLocked=()=>scanLoading||previewBusy||batchLoading||batchJob?.status==='running'||stopPending||mutationBusy;
  const defaultEffect={style:'pixel',strength:'medium',expansion:'small'};
  let effect={...defaultEffect},savedEffect={...defaultEffect},clipEffects={},savedClipEffects={};
  let removedClip=null,removeTimer;
  function clearRemovedClip(){removedClip=null;clearTimeout(removeTimer);$('mosaicRemoved').hidden=true;}
  let manualClips=[],savedManualClips=[],addingClip=false,manualAction='delete';
  let aiEvents=[],clipEdits={},savedAiEvents=[],savedClipEdits={};
  const allEvents=()=>[...(aiEvents.length?aiEvents:scanReport?.status==='running'?(scanReport.events||[]):[]),...manualClips];
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
    effect={...defaultEffect,...draft?.effect};savedEffect={...effect};
    clipEffects=structuredClone(draft?.clipEffects||{});savedClipEffects=structuredClone(clipEffects);
    manualClips=draft?.manualClips||[];
    if(!Array.isArray(manualClips)||manualClips.length>40||manualClips.some(e=>typeof e.id!=='string'||!e.id.startsWith('manual-')||![e.start,e.end].every(Number.isInteger)||e.start<0||e.start>=e.end||e.end>meta().frame_count||e.status!=='needs_review'||!Array.isArray(e.candidates)||e.candidates.length))throw Error('手动片段草稿格式无效');
    savedManualClips=structuredClone(manualClips);
    deletions=draft?.deletions||[];
    if(!Array.isArray(deletions)||deletions.length>40||deletions.some(t=>![t.start,t.end].every(Number.isInteger)||t.start<0||t.start>=t.end||t.end>meta().frame_count))throw Error('本机删除草稿格式无效');
    savedDeletions=structuredClone(deletions);
    if(!Array.isArray(data)||data.length>320||!data.every(validTarget)||data.filter(t=>t.adopted!==false).length>8)throw Error('本机草稿格式无效，未覆盖原数据。');
    targets=data.map(t=>({...t,uid:t.uid||regionId(),adopted:t.adopted!==false}));
    savedTargets=structuredClone(targets);
  }
  const status=(text,error=false)=>{$('mosaicStatus').textContent=text;$('mosaicStatus').classList.toggle('error',error);};
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
  function selectRegion(id){selected=id;const c=selectedBox();editing=c?.uid?targets.findIndex(t=>t.uid===c.uid):null;if(editing===-1)editing=null;motion=c?.motion||(editing!==null?targets[editing].motion||'tracked':motion);}
  function selectedBox(){return candidates.find(c=>c.id===selected);}
  function resetCandidate(){token++;loading=false;candidates=[];selected=null;editing=null;drawing=false;pointer=null;scanEvent=null;paint();}
  function paint(){
    if(!context)return;
    for(const el of dialog.querySelectorAll('[data-generation-lock]')){el.disabled=el.dataset.generationLock==='true';delete el.dataset.generationLock;}
    const c=selectedBox(),accurate=ready===wanted&&!image.hidden&&video.paused;
    $('mosaicPosition').textContent=`${clock(wanted)} / ${clock(meta().frame_count-1)}`;
    $('mosaicSeek').value=wanted;$('mosaicFrameNumber').value=clock(wanted);
    $('mosaicRecognize').disabled=loading||scanLoading||frameLoading||!accurate||!inRange()||!context.session.capabilities?.region_recognition;
    $('mosaicRecognize').textContent=loading?'识别中…':'识别当前帧';
    $('mosaicDraw').disabled=!accurate||loading||!inRange();$('mosaicDraw').textContent=drawing?'在画面拖出矩形':'手动框选';
    $('mosaicAddRegion').disabled=!accurate||loading||!inRange()||(!addingClip&&!scanEvent)||activeCount()+(addingClip?candidates.length:0)>=8;
    $('mosaicRemoveRegion').hidden=!c;
    $('mosaicRemoveRegion').disabled=loading||!accurate;
    $('mosaicConfirm').disabled=!accurate||!validBox(c)||!inRange()||loading;
    $('mosaicConfirm').textContent=editing===null?'保存区域':'保存调整';
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
    if(accurate)$('mosaicFrameHint').textContent=motion==='fixed'?'固定位置 · 所选时间内保持此位置':'准确帧 · 框仅对应当前参考帧，尚未跟踪';
    $('mosaicAdjust').classList.toggle('manual-delete',addingClip&&manualAction==='delete');
    $('mosaicPrevEvent').disabled=eventIndex<=0;
    $('mosaicNextEvent').disabled=eventIndex<0||eventIndex>=(allEvents().length||0)-1;
    $('mosaicReviewInfo').textContent=eventIndex>=0?`片段 ${eventIndex+1} / ${allEvents().length}`:'点击右侧片段查看';
    for(const axis of ['X','Y','W','H'])$('mosaicBox'+axis).value=c?c[axis.toLowerCase()]:'';
    $('mosaicCandidates').innerHTML=candidates.map(c=>`<button data-candidate="${esc(c.id)}" aria-pressed="${c.id===selected}"><strong>${esc(c.label)}</strong><small>${esc(c.reason)}</small></button>`).join('');
    const currentEvent=allEvents().find(e=>e.id===scanEvent);
    const savedRegions=currentEvent?targets.map((t,i)=>({t,i})).filter(({t})=>belongs(t,currentEvent)&&!t.removed):[];
    if(savedRegions.length>1)$('mosaicCandidates').innerHTML=savedRegions.map(({t,i})=>`<button data-saved-adjust="${i}" aria-pressed="${i===editing}">区域 ${savedRegions.findIndex(v=>v.i===i)+1} · ${esc(t.label)}</button>`).join('');
    $('mosaicTargetCount').textContent=String(allEvents().length||targets.length);
    dialog.classList.toggle('adjusting',$('mosaicAdjust').open);
    $('mosaicEmpty').hidden=!!targets.length;
    $('mosaicClear').disabled=!activeCount()&&!deletions.length;
    paintEffects();paintScan();paintBatch();
    drawOverlay();
  }
  const mutationSelector='[data-restore-clip],.mosaic-card-checks button,[data-remove-clip],#mosaicUndoRemove,#mosaicAddClip,#mosaicClear,#mosaicScan,#mosaicAdjustToggle,#mosaicAdjust button,#mosaicAdjust input,#mosaicAdjust select,#mosaicEffects select,#mosaicEffectSave,#mosaicEffectPreview,[data-effect-target]';
  for(const type of ['click','change','input'])dialog.addEventListener(type,e=>{
    if(editLocked()&&e.target.closest(mutationSelector)){e.preventDefault();e.stopImmediatePropagation();}
  },true);
  const clipKey=t=>t?(t.eventId||`${t.originStart??t.start}:${t.originEnd??t.end}`):null;
  const effectFor=t=>({...defaultEffect,...effect,...clipEffects[clipKey(t)]});
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
  const effectButton=index=>`<button data-effect-target="${index}" aria-label="设置本片段打码效果">效果</button>`;
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
    $('mosaicScan').textContent=scanLoading?'识别中…':scanError?'重试读取':scanReport?.status==='failed'?'重试识别':scanReport?.status==='completed'?'重新识别整段':'识别整段视频';
    let message='自动查找疑似时间段和目标区域，无需先手动找帧。';
    if(!enabled)message='本机未启用 Gemini 识别，可用下方手工补充。';
    else if(scanLoading)message=scanReport?.status==='running'?(scanReport.progress||'正在扫描整段视频…'):'正在读取整段识别任务…';
    else if(scanError)message=scanError;
    else if(scanReport?.status==='failed')message=scanReport.error||'整段识别失败，请重试。';
    else if(scanReport?.status==='completed'){
      const located=aiResults.filter(e=>e.status==='localized').length;
      message=aiResults.length?`发现 ${aiResults.length} 个片段${aiResults.length-located?` · ${aiResults.length-located} 段打码需补充区域`:''}`:'本次整段抽样未发现疑似时间段，仍请检查短闪帧。可用下方手工补充。';

    }
    if(scanLoading&&events.length)message='正在重新识别，已有片段已保留，完成后可继续编辑。';
    $('mosaicScanStatus').textContent=message;$('mosaicScanStatus').title=message;$('mosaicScanStatus').classList.toggle('error',!!scanError||scanReport?.status==='failed');
    const targetRow=(t,i)=>`<div class="mosaic-event-card"><button data-target-view="${i}"><strong>${clock(t.start)}–${clock(t.end-1)} · ${esc(t.label)}</strong></button><div class="mosaic-card-actions"><div class="mosaic-card-checks"><button data-target-remove="${i}" aria-pressed="${t.adopted!==false}" ${t.adopted===false&&activeCount()>=8?'disabled':''}>打码</button></div>${t.adopted!==false?effectButton(i):''}</div></div>`;
    const grouped=new Set();
    const html=events.map(e=>{
      const allRecords=targets.map((t,i)=>({t,i})).filter(({t})=>belongs(t,e));
      const records=allRecords.filter(({t})=>!t.removed);
      const removal=deletions.find(t=>belongs(t,e)),removed=!!removal,displayRange=eventRange(e);
      allRecords.forEach(({i})=>grouped.add(i));
      const suggestions=e.candidates.map((c,i)=>({c,i})).filter(({c})=>!allRecords.some(({t})=>t.proposal===c.id||(!t.proposal&&t.seed_frame===e.seed_frame&&t.x===c.x&&t.y===c.y&&t.w===c.w&&t.h===c.h)));
      const total=records.length+suggestions.length,multiple=total>1,adopted=records.filter(({t})=>t.adopted!==false).length;
      const toggle=multiple
        ?`<button data-event-adopt="${esc(e.id)}" aria-pressed="${adopted>0}">打码</button>`
        :records.length?`<button data-target-remove="${records[0].i}" aria-pressed="${adopted>0}" ${!adopted&&activeCount()>=8?'disabled':''}>打码</button>`
        :suggestions.length?`<button data-proposal="${esc(e.id)}:${suggestions[0].i}" aria-pressed="false" ${activeCount()>=8?'disabled':''}>打码</button>`
        :`<button data-event-draw="${esc(e.id)}" aria-pressed="false" title="先框选需要遮挡的区域">打码</button>`;
      return `<div class="mosaic-event-card ${scanEvent===e.id?'selected':''}" data-mosaic-card="${esc(e.id)}" ${e.id.startsWith('manual-')?'data-manual-card':''}>${e.id.startsWith('manual-')?`<button data-remove-clip="${esc(e.id)}" class="mosaic-remove-clip" aria-label="移除手动片段" title="移除片段">×</button>`:''}<button data-scan-event="${esc(e.id)}" ${records.length?`data-target-view="${records[0].i}"`:''} aria-pressed="${scanEvent===e.id}"><strong>${clock(displayRange.start)}–${clock(displayRange.end-1)}</strong><small>${esc(e.label)}${total?'':' · 打码需框选'}</small><small title="${esc(e.reason)}">${esc(e.reason)}</small></button><div class="mosaic-card-actions"><div class="mosaic-card-checks" role="group" aria-label="片段处理方式"><button data-event-delete="${esc(e.id)}" aria-pressed="${removed}">删除</button>${toggle}</div>${adopted&&!removed?effectButton(records.find(({t})=>t.adopted!==false).i):''}${!e.id.startsWith('manual-')?`<details class="mosaic-clip-menu"><summary aria-label="更多片段操作">⋯</summary><button data-restore-clip="${esc(e.id)}">恢复识别结果</button></details>`:''}</div></div>`;

    }).join('')+targets.map((t,i)=>t.removed||grouped.has(i)||(scanReport?.status==='completed'&&t.adopted===false&&Number.isInteger(t.originStart)&&!t.eventId)?'':targetRow(t,i,true)).join('');
    if($('mosaicScanEvents').innerHTML!==html)$('mosaicScanEvents').innerHTML=html;
    $('mosaicScanMarkers').innerHTML=events.map(original=>{const e=eventRange(original);return `<button data-scan-marker="${esc(e.id)}" class="${e.status==='needs_review'?'needs-review':''}" aria-label="查看 ${esc(e.label)} ${clock(e.start)}" title="${clock(e.start)}–${clock(e.end-1)} ${esc(e.label)}" style="left:${100*e.start/meta().frame_count}%;width:${100*(e.end-e.start)/meta().frame_count}%"></button>`;}).join('');
  }
  function checkedReport(report){
    if(!report)return null;
    if(!['running','completed','failed'].includes(report.status))throw Error('整段识别任务状态无效，请重新读取。');
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
    const version=epoch,request=++scanToken,endpoint=base(),valid=()=>active(version)&&request===scanToken;
    scanLoading=true;scanError='';paint();
    try{
      let state;
      if(start){
        const report=await api(endpoint+'/scan',{reset_at:context.session.reset_at||'',force:!!scanReport});
        state={enabled:true,report};
      }else state=await api(endpoint+'/scan');
      while(valid()){
        if(typeof state.enabled!=='boolean')throw Error('整段识别响应无效，请重试读取。');
        context.session.capabilities.region_recognition=state.enabled;
        const firstReport=!scanReport;scanReport=checkedReport(state.report);if(scanReport?.status==='completed')mergeDiscovery(scanReport.events);paint();if(firstReport&&scanReport)status('点击片段查看画面，选择删除或打码；不选则不处理。');
        if(scanReport?.status!=='running')break;
        await new Promise(r=>setTimeout(r,1000));if(!valid())return;
        state=await api(endpoint+'/scan');
      }
    }catch(e){if(valid())scanError=e.message;}
    finally{if(valid()){scanLoading=false;paint();}}
  }
  function viewScanEvent(id){
    addingClip=false;
    const original=allEvents().find(e=>e.id===id);if(!original)return;const event={...original,...eventRange(original)};motion=id.startsWith('manual-')?'fixed':'tracked';
    const deletion=deletions.find(t=>belongs(t,event));
    if(deletion){resetCandidate();scanEvent=id;$('mosaicStart').value=clock(deletion.start);$('mosaicEnd').value=clock(deletion.end-1);seek(deletion.start,true);return;}
    const savedIndex=targets.findIndex(t=>belongs(t,event)&&!t.removed);
    if(savedIndex>=0){viewTarget(savedIndex,id);return;}
    resetCandidate();scanEvent=id;
    $('mosaicStart').value=clock(event.start);$('mosaicEnd').value=clock(event.end-1);$('mosaicAdjust').open=event.status==='needs_review';
    candidates=event.candidates.filter(c=>!targets.some(t=>belongs(t,original)&&t.removed&&t.proposal===c.id)).map(c=>({...c}));selected=candidates[0]?.id??null;
    const seed=event.status==='localized'?Math.max(event.start,Math.min(event.end-1,event.seed_frame)):Math.floor((event.start+event.end-1)/2);
    seek(seed,true);
    $('mosaicScanEvents').querySelector('[aria-pressed="true"]')?.scrollIntoView({block:'nearest'});
    status(event.status==='localized'?'时间段和候选区域已自动定位。检查后打码，必要时再调整。':event.review_note||'该段需复核：尚未定位区域，可换一帧识别或手动框选。');
  }
  function geometry(){const rect=$('mosaicStage').getBoundingClientRect(),scale=Math.min(rect.width/meta().width,rect.height/meta().height);return {left:(rect.width-meta().width*scale)/2,top:(rect.height-meta().height*scale)/2,width:meta().width*scale,height:meta().height*scale,scale,rect};}
  function drawOverlay(){
    if(!context)return;
    overlay.hidden=ready!==wanted||image.hidden||!video.paused||(!candidates.length&&!drawing);
    if(overlay.hidden)return;
    const g=geometry();Object.assign(overlay.style,{left:g.left+'px',top:g.top+'px',width:g.width+'px',height:g.height+'px'});
    overlay.classList.toggle('drawing',drawing);
    overlay.innerHTML=candidates.map(c=>`<button class="mosaic-box ${c.id===selected?'selected':''}" data-box="${esc(c.id)}" aria-label="选择 ${esc(c.label)}" style="left:${c.x/meta().width*100}%;top:${c.y/meta().height*100}%;width:${c.w/meta().width*100}%;height:${c.h/meta().height*100}%"><span>${esc(c.label)}</span>${c.id===selected&&!editLocked()?['n','s','e','w','ne','nw','se','sw'].map(edge=>`<i data-resize="${edge}" class="mosaic-handle ${edge}"></i>`).join(''):''}</button>`).join('');
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
      $('mosaicFrameHint').textContent=motion==='fixed'?'固定位置 · 所选时间内保持此位置':'准确帧 · 框仅对应当前参考帧，尚未跟踪';
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
      candidates=job.candidates.map(c=>({...c}));selected=candidates[0]?.id??null;if(candidates.length)$('mosaicAdjust').open=true;
      status(candidates.length?'选中候选框，可调整位置和大小，确认后加入待跟踪目标。':'本帧未找到候选区域。可换一帧识别，或手动框选。');
    }catch(e){if(valid())status(e.message,true);}
    finally{if(valid()){loading=false;paint();}}
  }
  // Closing the dialog does not cancel server-side snapshot preparation.
  // Keep only in-flight requests so re-entry joins the same preparation, while
  // later opens still validate the current source and editing permissions.
  const preparing=new Map();
  function prepare(batch,asset){
    const id=JSON.stringify([currentUser?.username,batch,asset.request_id,asset.sha256]);
    if(!preparing.has(id)){
      const pending=api(`/api/batches/${batch}/mosaics/open`,{request_id:asset.request_id,source_sha256:asset.sha256})
        .finally(()=>preparing.delete(id));
      preparing.set(id,pending);
    }
    return preparing.get(id);
  }
  async function open(batch,asset){
    const version=++epoch;token++;controller?.abort();clearTimeout(timer);context=null;motion='tracked';wanted=0;ready=-1;candidates=[];selected=null;targets=[];editing=null;loading=false;frameLoading=false;drawing=false;
    batchToken++;batchJob=null;batchLoading=false;stopPending=false;batchError='';deletions=[];mutationBusy=false;resultBefore=false;resultMode=false;$('mosaicBatchDialog').hidden=true;
    clearRemovedClip();scanToken++;scanLoading=false;scanReport=null;scanError='';scanEvent=null;addingClip=false;manualClips=[];aiEvents=[];clipEdits={};$('mosaicAdjust').open=false;$('mosaicEffects').close();
    image.hidden=true;overlay.hidden=true;$('mosaicWorkspace').hidden=true;$('mosaicTitle').textContent='剪切打码 · '+asset.title;
    for(const v of $('assets').querySelectorAll('video'))v.pause();
    if(!dialog.open)dialog.showModal();status('正在准备原片，首次打开需要一些时间；关闭后重新进入会继续等待…');
    try{
      const session=await prepare(batch,asset);
      if(version!==epoch||!dialog.open)return;context={batch,asset,session};
      $('mosaicStart').value=clock(0);$('mosaicEnd').value=clock(meta().frame_count-1);$('mosaicSeek').max=meta().frame_count-1;
      loadDraft();
      $('mosaicWorkspace').hidden=false;video.src=appUrl(base()+'/source');video.load();
      status(session.capabilities?.region_recognition?'点击“识别整段视频”，自动查找需要打码的时间段和区域。':'本机未启用 Gemini 识别，可先手动框选目标。');
      paint();loadFrame();scanVideo();restoreBatch();
    }catch(e){if(version===epoch)status(e.message,true);}
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
  dialog.addEventListener('close',()=>{if(dialog.open)return;epoch++;token++;scanToken++;controller?.abort();clearTimeout(timer);context=null;clearRemovedClip();$('mosaicBatchVideo').pause();video.pause();video.removeAttribute('src');video.load();image.removeAttribute('src');if(frameUrl)URL.revokeObjectURL(frameUrl);frameUrl=null;});
  $('mosaicScan').onclick=()=>scanVideo(!scanError&&scanReport?.status!=='running');
  $('mosaicScanEvents').onclick=e=>{const b=e.target.closest('[data-scan-event]');if(b)viewScanEvent(b.dataset.scanEvent);};
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
  $('mosaicMotion').onchange=e=>{if(editLocked())return;motion=e.target.value;if(selectedBox())selectedBox().motion=motion;paint();status('位置模式已修改，保存区域后生效。');};
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
      for(const c of candidates.filter(validBox))targets.push({uid:regionId(),eventId:event.id,originStart:r.start,originEnd:r.end,proposal:c.id,adopted:true,motion:c.motion||motion,label:c.label,reason:c.reason,x:c.x,y:c.y,w:c.w,h:c.h,...r,seed_frame:Math.max(r.start,Math.min(r.end-1,c.seed_frame??wanted))});
      setEventRange(event,r);
      if(save()){addingClip=false;$('mosaicAdjust').open=false;viewScanEvent(event.id);paint();status('已添加打码片段。');}
    }
  });
  $('mosaicAdjustToggle').onclick=()=>{$('mosaicAdjust').open=!$('mosaicAdjust').open;paint();};
  $('mosaicAdjust').addEventListener('toggle',()=>{if(context)dialog.classList.toggle('adjusting',$('mosaicAdjust').open);});
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
  $('mosaicCandidates').onclick=e=>{const saved=e.target.closest('[data-saved-adjust]');if(saved){viewTarget(Number(saved.dataset.savedAdjust));$('mosaicAdjust').open=true;return;}const b=e.target.closest('[data-candidate]');if(b){selectRegion(b.dataset.candidate);paint();}};
  for(const axis of ['X','Y','W','H'])$('mosaicBox'+axis).onchange=guard(e=>{const c=selectedBox();if(!c)return;const next={...c,[axis.toLowerCase()]:Number(e.target.value)};if(!validBox(next)){paint();throw Error('矩形必须位于画面内，宽高至少 1 像素');}Object.assign(c,next);paint();});
  $('mosaicConfirm').onclick=guard(()=>{
    if($('mosaicConfirm').disabled)return;const c=selectedBox(),r=range();
    const currentEvent=allEvents().find(e=>e.id===scanEvent);
    if(editing===null&&currentEvent){const existing=targets.findIndex(t=>belongs(t,currentEvent)&&t.proposal===c.id);if(existing>=0)editing=existing;}
    if((editing===null||targets[editing]?.adopted===false)&&activeCount()>=8)throw Error('最多打码 8 个区域，请先取消一个。');
    if(editing===null&&targets.length>=320)throw Error('本机草稿区域已达上限。');
    const event=allEvents().find(e=>e.id===scanEvent);
    if(event)deletions=deletions.filter(t=>!belongs(t,event));
    const target={...(editing===null?{uid:regionId(),eventId:event?.id,originStart:event?.start,originEnd:event?.end,proposal:c.id}:targets[editing]),adopted:true,removed:false,motion,label:c.label,reason:c.reason,x:c.x,y:c.y,w:c.w,h:c.h,...r,seed_frame:wanted};
    setEventRange(event,r);
    const previous=editing===null?null:targets[editing];
    if(previous){
      for(const sibling of targets.filter(t=>event?belongs(t,event):t.uid===previous.uid)){
        Object.assign(sibling,r);
        sibling.seed_frame=Math.max(r.start,Math.min(r.end-1,sibling.seed_frame));
      }
    }
    if(editing===null){targets.push(target);editing=targets.length-1;}else targets[editing]=target;
    c.uid=target.uid;c.motion=motion;
    paint();if(save()){$('mosaicAdjust').open=false;paint();status('已保存区域。');return true;}
  });
  function viewTarget(i,eventId){
    addingClip=false;
    const t=targets[i];if(!t)return;
    resetCandidate();editing=i;motion=t.motion||'tracked';scanEvent=eventId??allEvents().find(e=>belongs(t,e))?.id??null;
    const event=allEvents().find(e=>e.id===scanEvent);
    candidates=targets.filter(v=>!v.removed&&(v.uid===t.uid||(event&&belongs(v,event)&&(v.motion==='fixed'||v.seed_frame===t.seed_frame)))).map(v=>({...v,id:'saved-'+v.uid}));
    selected='saved-'+t.uid;$('mosaicAdjust').open=false;
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
      if(save()){paint();status('处理选择已保存，关闭后可继续。');}return;
    }

    const draw=e.target.closest('[data-event-draw]');
    if(draw){viewScanEvent(draw.dataset.eventDraw);$('mosaicAdjust').open=true;drawing=true;paint();status('在画面框选区域，然后保存。');return;}
    const group=e.target.closest('[data-event-adopt]');
    if(group){
      const event=allEvents().find(v=>v.id===group.dataset.eventAdopt);
      const allRecords=targets.filter(t=>belongs(t,event)),records=allRecords.filter(t=>!t.removed);
      const missing=event.candidates.filter(c=>!allRecords.some(t=>t.proposal===c.id||(!t.proposal&&t.seed_frame===event.seed_frame&&t.x===c.x&&t.y===c.y&&t.w===c.w&&t.h===c.h)));
      const adopt=group.getAttribute('aria-pressed')!=='true';
      if(adopt&&(activeCount()+records.filter(t=>t.adopted===false).length+missing.length>8||targets.length+missing.length>320)){paint();status('最多打码 8 个区域，请先取消其他片段。',true);return;}
      if(adopt)deletions=deletions.filter(t=>!belongs(t,event));
      records.forEach(t=>t.adopted=adopt);
      if(adopt)missing.forEach(c=>targets.push({...c,uid:regionId(),adopted:true,proposal:c.id,eventId:event.id,originStart:event.start,originEnd:event.end,start:eventRange(event).start,end:eventRange(event).end,seed_frame:Math.max(eventRange(event).start,Math.min(eventRange(event).end-1,event.seed_frame))}));
      if(save()){paint();status(adopt?'已打码该片段的全部区域。':'已取消该片段；区域调整已保留。');}return;
    }
    const proposal=e.target.closest('[data-proposal]');
    if(proposal){
      const [id,index]=proposal.dataset.proposal.split(':'),event=allEvents().find(v=>v.id===id),c=event?.candidates[Number(index)];
      if(!c||activeCount()>=8||targets.length>=320){paint();return;}
      deletions=deletions.filter(t=>!belongs(t,event));
      targets.push({...c,uid:regionId(),adopted:true,proposal:c.id,eventId:event.id,originStart:event.start,originEnd:event.end,start:eventRange(event).start,end:eventRange(event).end,seed_frame:Math.max(eventRange(event).start,Math.min(eventRange(event).end-1,event.seed_frame))});
      if(save()){paint();status('已打码并保存；点击片段可继续检查。');}return;
    }
    const remove=e.target.closest('[data-target-remove]'),view=e.target.closest('[data-target-view]');
    if(remove){const t=targets[Number(remove.dataset.targetRemove)];if(t.adopted===false&&activeCount()>=8){paint();status('最多打码 8 个区域，请先取消一个。',true);return;}t.adopted=t.adopted===false;if(t.adopted){const event=allEvents().find(e=>belongs(t,e));if(event)deletions=deletions.filter(d=>!belongs(d,event));}if(save()){paint();status(t.adopted?'已恢复打码，保留原来的区域调整。':'已取消打码；框和时间已保留，可再次勾选。');}return;}
    if(view&&!view.hasAttribute('data-scan-event'))viewTarget(Number(view.dataset.targetView));
  };
  const deleteRanges=()=>deletions.map(t=>[t.start,t.end]).sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
  const sameBatch=()=>!!batchJob&&JSON.stringify({...defaultEffect,...batchJob.effect})===JSON.stringify(effect)&&targetSignature(batchJob.targets,batchJob.effect)===targetSignature(adoptedTargets())&&JSON.stringify(batchJob.ranges||[])===JSON.stringify(mergedRanges(deleteRanges()));
  function mergedRanges(input){const out=[];for(const [a,b] of input){if(out.length&&a<=out.at(-1)[1])out.at(-1)[1]=Math.max(b,out.at(-1)[1]);else out.push([a,b]);}return out;}
  const targetFields=['start','end','seed_frame','x','y','w','h'];
  const renderTarget=t=>({...Object.fromEntries(targetFields.map(k=>[k,t[k]])),...(t.motion==='fixed'?{motion:'fixed'}:{})});
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
    $('mosaicStop').hidden=batchJob?.status!=='running'||context.session.capabilities?.render_stop!==true;$('mosaicStop').disabled=stopPending||!!batchJob?.cancel_requested;
    $('mosaicStop').textContent=stopPending||batchJob?.cancel_requested?'正在停止…':'停止生成';
    $('mosaicDiscard').hidden=locked;
    $('mosaicProcessingHint').textContent='生成中暂不可编辑；停止生成后可修改。关闭后后台继续处理。';
    const adopted=adoptedTargets(),segments=new Set(adopted.map(t=>`${t.start}:${t.end}`)).size;
    const running=batchLoading||batchJob?.status==='running'||mutationBusy;
    const generating=batchJob?.status==='running'||(batchLoading&&!batchJob);
    const showOutput=resultMode&&batchJob?.status==='completed';
    $('mosaicWorkspace').hidden=showOutput;$('mosaicBatchDialog').hidden=!showOutput;
    $('mosaicProcessing').hidden=!generating;
    $('mosaicProcessingText').textContent=stopPending||batchJob?.cancel_requested?'正在停止生成…':batchJob?.progress||'正在准备处理视频…';
    $('mosaicStatus').hidden=generating||showOutput;
    dialog.classList.toggle('processing-result',showOutput);
    const clip=currentClip();
    $('mosaicBatchStart').disabled=(!context.session.capabilities?.tracking&&adopted.some(t=>t.motion!=='fixed'))||(!adopted.length&&!deletions.length)||running||scanLoading;
    $('mosaicDiscard').disabled=mutationBusy||scanReport?.status==='running';
    $('mosaicClose').disabled=mutationBusy;
    $('mosaicBatchStart').textContent=running?'正在生成视频…':'生成处理后视频';
    $('mosaicBatchHint').textContent=batchError||`删除 ${deletions.length} 段 · 打码 ${segments} 段`;
    $('mosaicBatchView').hidden=!batchJob&&!batchError;
    $('mosaicBatchView').textContent=batchJob?.status==='running'?'查看生成进度':batchJob?.status==='failed'?'查看失败原因':'查看生成结果';
    $('mosaicBatchClose').hidden=!showOutput;
    $('mosaicBatchStart').hidden=generating||(resultMode&&batchJob?.status==='completed');
    $('mosaicBatchView').hidden=generating||resultMode||batchJob?.status==='cancelled'||(!batchJob&&!batchError);
    $('mosaicBatchDownload').hidden=!resultMode; $('mosaicReplace').hidden=!resultMode;
    if(!resultMode)return;
    const done=batchJob?.status==='completed';
    $('mosaicReplace').hidden=!done;$('mosaicReplace').disabled=mutationBusy||!sameBatch();
    $('mosaicBatchBefore').hidden=!done;$('mosaicBatchAfter').hidden=!done;
    $('mosaicResultLabel').hidden=!done;$('mosaicResultLabel').textContent=resultBefore?'正在查看：处理前 · 原视频':'正在查看：处理后 · 生成结果';
    $('mosaicBatchBefore').setAttribute('aria-pressed',String(resultBefore));$('mosaicBatchAfter').setAttribute('aria-pressed',String(!resultBefore));
    $('mosaicBatchDownload').hidden=!done;$('mosaicBatchVideo').hidden=!done;
    $('mosaicBatchVersion').textContent=batchJob&&!sameBatch()?'处理方案已变化。此结果对应生成时的选择；要应用新选择，请重新生成。':'';
    if(done){
      $('mosaicBatchStatus').textContent=batchError||'完整视频已生成，可预览或下载。';
      const outputUrl=appUrl(base()+'/render/'+batchJob.id+'/video?v='+encodeURIComponent(batchJob.completed_at||''));
      const url=resultBefore?appUrl(base()+'/source'):outputUrl;
      if($('mosaicBatchVideo').getAttribute('src')!==url)$('mosaicBatchVideo').src=url;
      $('mosaicBatchDownload').href=outputUrl;$('mosaicBatchDownload').download=context.asset.title+'_处理后.mp4';
    }else $('mosaicBatchStatus').textContent=batchError||batchJob?.error||batchJob?.progress||'正在提交全部已打码区域…';
  }
  async function batchRequest(start=false){
    if(!context)return;
    const version=epoch,request=++batchToken,endpoint=base(),valid=()=>active(version)&&request===batchToken;
    batchLoading=true;batchError='';paintBatch();
    try{
      const result=await api(endpoint+'/render',start?{targets:adoptedTargets(),ranges:deleteRanges(),effect,reset_at:context.session.reset_at||''}:undefined);
      if(!valid())return;batchJob=start?result:result.job||null;paintBatch();
      while(valid()&&batchJob?.status==='running'){
        await new Promise(r=>setTimeout(r,1200));if(!valid())return;
        const job=await api(endpoint+'/render/'+batchJob.id);if(!valid())return;batchJob=job;paintBatch();
      }
    }catch(e){if(valid())batchError=e.message;}
    finally{if(valid()){batchLoading=false;paint();if(batchJob?.status==='cancelled')status('已停止生成，当前选择已保留，可以继续编辑。');}}
  }
  function restoreBatch(){batchRequest();}
  $('mosaicStop').onclick=async()=>{
    if(stopPending||batchJob?.status!=='running')return;
    const version=epoch,endpoint=base(),jid=batchJob.id;stopPending=true;paintBatch();
    try{
      const job=await api(endpoint+'/stop',{job_id:jid,reset_at:context.session.reset_at||''});
      if(!active(version)||batchJob?.id!==jid)return;
      // Resume polling the acknowledged job; do not unlock until the worker exits.
      batchToken++;batchJob=job;
      while(active(version)&&batchJob?.id===jid&&batchJob.status==='running'){
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
    batchJob=null;resultBefore=false;$('mosaicBatchVideo').pause();$('mosaicBatchVideo').removeAttribute('src');$('mosaicBatchVideo').load();
    showResult(true);batchRequest(true);
  };
  $('mosaicBatchView').onclick=()=>{showResult(true);paintBatch();};
  $('mosaicBatchClose').onclick=()=>{if(!mutationBusy)showResult(false);};


  function showResult(show){
    resultMode=show;
    if(show&&batchJob?.status==='completed'){video.pause();}else if(!show){$('mosaicBatchVideo').pause();loadFrame();}
    paintBatch();
  }
  function clearDraft(){for(const k of [draftKey(),oldDraftKey(),key()])localStorage.removeItem(k);}
  $('mosaicDiscard').onclick=async()=>{
    if(mutationBusy||!window.confirm('放弃本次处理？当前项目成片不会改变。'))return;
    const version=epoch;mutationBusy=true;paintBatch();
    try{await api(base()+'/discard',{reset_at:context.session.reset_at||''});if(!active(version))return;clearDraft();dialog.close();}
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
    }catch(e){if(active(version)){batchError=e.message;$('mosaicBatchStatus').textContent=e.message;}}
    finally{mutationBusy=false;if(active(version))paintBatch();}
  };
  function outputFrame(frame){return frame-(batchJob?.ranges||[]).reduce((n,[a,b])=>n+Math.max(0,Math.min(frame,b)-a),0);}
  function sourceFrame(frame){let start=0,remaining=frame;for(const [a,b] of batchJob?.ranges||[]){if(remaining<a-start)return start+remaining;remaining-=a-start;start=b;}return start+remaining;}
  function compareResult(before){const p=$('mosaicBatchVideo'),frame=p.currentTime*rate();const mapped=before?sourceFrame(frame):outputFrame(frame);resultBefore=before;paintBatch();p.onloadedmetadata=()=>{p.currentTime=mapped/rate();p.onloadedmetadata=null;};}
  $('mosaicBatchBefore').onclick=()=>{if(!resultBefore)compareResult(true);};
  $('mosaicBatchAfter').onclick=()=>{if(resultBefore)compareResult(false);};
  $('mosaicSaveTime').onclick=guard(()=>{
    if(editLocked())return;
    const event=allEvents().find(e=>e.id===scanEvent);if(!event)return;
    setEventRange(event,range());if(save()){paint();status('片段时间已保存。');}
  });
  $('mosaicSaveDelete').onclick=guard(()=>{const event=allEvents().find(e=>e.id===scanEvent);if(!event)return;const r=range();setEventRange(event,r);deletions=deletions.filter(t=>!belongs(t,event));deletions.push({...r,eventId:event.id,originStart:event.start,originEnd:event.end,label:event.label});targets.filter(t=>belongs(t,event)).forEach(t=>t.adopted=false);if(save())paint();});
  function currentClip(){const event=allEvents().find(e=>e.id===scanEvent);return event?eventRange(event):targets[editing]||null;}
  async function stopClipPreview(){
    previewClose=true;
    if(previewJob?.status==='running'){
      try{await api(previewEndpoint+'/stop',{job_id:previewJob.id,reset_at:previewReset});}
      catch(e){if(context)status('停止预览未成功，正在等待任务结束。',true);}
    }
  }
  $('mosaicEffectPreview').onclick=async()=>{
    if(editLocked()||!effectDraft||!effectEditKey)return;
    const selectedTargets=targets.filter(t=>t.adopted!==false&&clipKey(t)===effectEditKey)
      .map(t=>({...renderTarget(t),effect:{...effectDraft}}));
    if(!selectedTargets.length)return;
    const version=epoch,endpoint=base(),appearance={...effectDraft},reset=context.session.reset_at||'',previewStarted=Date.now();
    previewBusy=true;previewClose=false;previewJob=null;previewEndpoint=endpoint;previewReset=reset;
    clipBounds=null;trackToken++;paint();
    $('mosaicTrackTitle').textContent='本片段效果预览';$('mosaicTrackHint').textContent='';
    $('mosaicTrackStatus').textContent='正在生成本片段预览…';$('mosaicTrackVideo').hidden=true;
    $('mosaicTrackClose').textContent='返回设置';$('mosaicTrackDialog').classList.add('is-loading');$('mosaicTrackDialog').showModal();
    try{
      previewJob=await api(endpoint+'/clip-preview',{targets:selectedTargets,effect:appearance,reset_at:reset});
      if(previewClose)await stopClipPreview();
      while(previewJob.status==='running'){
        if(active(version)&&!previewClose){const seconds=Math.floor((Date.now()-previewStarted)/1000);$('mosaicTrackStatus').textContent=`${previewJob.stage==='composing'?'正在合成片段预览':selectedTargets.every(t=>t.motion==='fixed')?'正在准备固定位置打码':'正在跟踪片段'} · 已等待 ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;}
        await new Promise(r=>setTimeout(r,1000));
        previewJob=await api(endpoint+'/render/'+previewJob.id);
      }
      if(!active(version)||previewClose)return;
      if(previewJob.status!=='completed')throw Error(previewJob.error||'预览未完成，请重试。');
      $('mosaicTrackStatus').textContent='预览已生成';
      $('mosaicTrackVideo').src=appUrl(endpoint+'/render/'+previewJob.id+'/video?v='+encodeURIComponent(previewJob.completed_at||''));
      $('mosaicTrackVideo').hidden=false;
    }catch(e){if(active(version)&&!previewClose)$('mosaicTrackStatus').textContent=e.message;}
    finally{$('mosaicTrackDialog').classList.remove('is-loading');previewBusy=false;previewJob=null;if(context)paint();}
  };
  $('mosaicTrackVideo').addEventListener('loadedmetadata',()=>{if(clipBounds)$('mosaicTrackVideo').currentTime=clipBounds.start;});
  $('mosaicTrackVideo').addEventListener('play',()=>{const p=$('mosaicTrackVideo');if(clipBounds&&(p.currentTime<clipBounds.start||p.currentTime>=clipBounds.end))p.currentTime=clipBounds.start;});
  $('mosaicTrackVideo').addEventListener('timeupdate',()=>{const p=$('mosaicTrackVideo');if(clipBounds&&p.currentTime>=clipBounds.end){p.pause();p.currentTime=clipBounds.start;}});
  $('mosaicTrackClose').onclick=()=>$('mosaicTrackDialog').close();
  $('mosaicTrackDialog').addEventListener('close',()=>{if(previewBusy)stopClipPreview();$('mosaicTrackClose').textContent='返回检查';trackToken++;clipBounds=null;$('mosaicTrackVideo').pause();$('mosaicTrackVideo').removeAttribute('src');$('mosaicTrackVideo').load();});
  $('mosaicTrack').onclick=async()=>{
    clipBounds=null;$('mosaicTrackTitle').textContent='动态打码预览';$('mosaicTrackHint').textContent='仅预览当前区域、当前片段。';
    const saved=targets[editing],c=selectedBox();if(!saved||saved.adopted===false)return;
    try{const r=range();if(!c||['x','y','w','h'].some(k=>c[k]!==saved[k])||r.start!==saved.start||r.end!==saved.end)throw Error('请先保存区域调整，再开始跟踪。');}catch(e){status(e.message,true);return;}
    const request=++trackToken,version=epoch,endpoint=base(),valid=()=>active(version)&&request===trackToken&&$('mosaicTrackDialog').open;
    $('mosaicTrackVideo').hidden=true;$('mosaicTrackStatus').textContent='SAM 2 正在跟踪当前区域。首次加载模型可能需要稍等。';$('mosaicTrackDialog').classList.add('is-loading');$('mosaicTrackDialog').showModal();
    try{
      const target=Object.fromEntries(['start','end','seed_frame','x','y','w','h'].map(k=>[k,saved[k]]));
      let job=await api(endpoint+'/track',{target,effect:effectFor(saved),reset_at:context.session.reset_at||''});
      while(valid()&&job.status==='running'){
        $('mosaicTrackStatus').textContent=job.progress||'正在跟踪并生成预览…';
        await new Promise(r=>setTimeout(r,1200));if(!valid())return;
        job=await api(endpoint+'/track/'+job.id);
      }
      if(!valid())return;
      if(job.status!=='completed')throw Error(job.error||'跟踪未完成，请重试。');
      $('mosaicTrackStatus').textContent=`${clock(target.start)}–${clock(target.end-1)} · 预览已生成。`;
      $('mosaicTrackVideo').src=appUrl(endpoint+'/track/'+job.id+'/preview?v='+encodeURIComponent(job.completed_at||''));$('mosaicTrackVideo').hidden=false;
    }catch(e){if(valid())$('mosaicTrackStatus').textContent=e.message;}
    finally{if(valid())$('mosaicTrackDialog').classList.remove('is-loading');}
  };
  $('mosaicClear').onclick=()=>{if(window.confirm('取消全部处理选择？区域调整会保留。')){deletions=[];targets.forEach(t=>t.adopted=false);if(save())paint();}};
  $('mosaicAddRegion').onclick=()=>{
    if(editLocked()||$('mosaicAddRegion').disabled)return;
    if(!addingClip&&validBox(selectedBox())){if(!$('mosaicConfirm').onclick())return;}
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
      targets.push({...c,uid:regionId(),eventId:event.id,originStart:event.start,originEnd:event.end,proposal:c.id,adopted:false,removed:true,start:eventRange(event).start,end:eventRange(event).end,seed_frame:Math.max(eventRange(event).start,Math.min(eventRange(event).end-1,wanted))});
      if(!save())return;
    }
    candidates=candidates.filter(v=>v!==c);selectRegion(candidates[0]?.id??null);drawing=false;paint();status('已移除区域。');
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
  $('mosaicPlay').onclick=()=>{if(video.paused){controller?.abort();resetCandidate();ready=-1;image.hidden=true;overlay.hidden=true;video.play().catch(()=>status('暂时无法播放，请重试',true));}else{video.pause();wanted=Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));paint();loadFrame();}};
  video.onplay=()=>{$('mosaicPlay').textContent='暂停';image.hidden=true;overlay.hidden=true;paint();};
  video.onpause=()=>{$('mosaicPlay').textContent='播放';};
  video.ontimeupdate=()=>{if(context&&!video.paused){wanted=Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));paint();}};
  video.onended=()=>{if(context){wanted=Math.min(meta().frame_count-1,Math.floor(video.currentTime*rate()));paint();loadFrame();}};
  new ResizeObserver(drawOverlay).observe($('mosaicStage'));
  window.Mosaic={mount};mount();
})();
