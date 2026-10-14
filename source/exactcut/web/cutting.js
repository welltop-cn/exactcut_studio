/* Frame-addressed deletion editor. All edits refer to the source snapshot. */
(() => {
  const dialog = $('cutDialog'), video = $('cutSource'), image = $('cutFrame'), output = $('cutOutput');
  let context = null, epoch = 0, ranges = [], undo = [], selectedJob = null, busy = false;
  let wanted = 0, selection = null, frameReady = -1, imageUrl = null, frameBusy = false;
  let seekTimer, toastTimer, quick = false, dragging = null, scanToken = 0, scanReport = null;
  let frameController = null, resultMode = false, scanLoading = false;
  let clips = [], editingId = null, clipSerial = 0;
  const frames = new Map();
  const path = () => `/api/batches/${context.batch}/cuts/${context.session.id}`;
  const count = () => context.session.metadata.frame_count;
  const fps = () => context.session.metadata.fps_num / context.session.metadata.fps_den;
  const duration = n => (n/fps()).toFixed(2)+' 秒';
  // Frame-within-second follows the source's rational CFR rate, from frame zero.
  function timecode(frame) {
    const {fps_num:num,fps_den:den}=context.session.metadata;
    const seconds=Math.floor(frame*den/num), ordinal=frame-Math.ceil(seconds*num/den);
    return [Math.floor(seconds/60),seconds%60,ordinal].map(v=>String(v).padStart(2,'0')).join(':');
  }
  function parseTimecode(value) {
    const match=/^(\d+):([0-5]\d):(\d{2})$/.exec(value.trim());
    if(!match)throw Error('请按 分:秒:帧 填写，例如 00:20:14');
    const seconds=Number(match[1])*60+Number(match[2]),ordinal=Number(match[3]);
    const {fps_num:num,fps_den:den}=context.session.metadata;
    const first=Math.ceil(seconds*num/den),next=Math.ceil((seconds+1)*num/den);
    if(ordinal>=next-first)throw Error(`这一秒的帧应为 00–${String(next-first-1).padStart(2,'0')}`);
    const frame=first+ordinal;
    if(!Number.isSafeInteger(frame)||frame<0||frame>=count())throw Error(`位置应在 00:00:00–${timecode(count()-1)} 之间`);
    return frame;
  }
  const plan = () => ({ranges, mosaics: []});
  const samePlan = job => job && !(job.mosaics || []).length && JSON.stringify(job.ranges) === JSON.stringify(ranges);
  const draftKey = () => `exactcut.cuts.delete.v1.${currentUser?.username}.${context.batch}.${context.session.id}${context.session.reset_at?'.'+context.session.reset_at:''}`;
  const active = version => version === epoch && context && dialog.open;
  function status(message, error = false) { $('cutStatus').textContent = message; $('cutStatus').className = error ? 'error' : ''; }
  function notify(message) { clearTimeout(toastTimer); $('cutToast').textContent = message; $('cutToast').hidden = false; toastTimer = setTimeout(() => $('cutToast').hidden = true, 4000); }
  function guarded(fn) { return (...args) => { try { fn(...args); } catch (e) { status(e.message, true); } }; }
  function merge(items) {
    const result = [];
    for (const [a,b] of items.map(r=>r.slice()).sort((x,y)=>x[0]-y[0])) {
      if (![a,b].every(Number.isInteger) || a < 0 || b > count() || a >= b) throw Error('请填写视频范围内的开始帧和结束帧，开始不能晚于结束');
      const last = result.at(-1);
      if (last && a <= last[1]) last[1] = Math.max(b,last[1]); else result.push([a,b]);
    }
    if (result.length > 40) throw Error('最多添加 40 段删除片段');
    if (result.reduce((n,[a,b])=>n+b-a,0) >= count()) throw Error('请至少保留一帧');
    return result;
  }
  const copyClips = () => clips.map(c=>({...c}));
  const clipRanges = items => merge(items.filter(c=>c.checked).map(c=>[c.start,c.end]));
  function remember() { try { localStorage.setItem(draftKey(), JSON.stringify(ranges)); localStorage.setItem(draftKey()+'.clips',JSON.stringify(clips)); } catch { /* Storage is optional. */ } }
  function commitClips(next, checkpoint = true) {
    const nextRanges=clipRanges(next);
    if(checkpoint)undo.push(copyClips());
    const changed=JSON.stringify(ranges)!==JSON.stringify(nextRanges);
    clips=next;ranges=nextRanges;if(changed)selectedJob=null;
    remember();paintSelection();
  }
  function editClip(id) {
    if(busy)return;
    const clip=clips.find(c=>c.id===id);if(!clip)return;
    editingId=id;selection=[clip.start,clip.end];paintSelection();seek(clip.start,true);
  }
  function manual() {
    if(busy)return;editingId=null;selection=[wanted,wanted+1];paintSelection();seek(wanted,true);$('cutStart').focus();
  }
  function saveSelection() {
    if(editingId&&selection)commitClips(clips.map(c=>c.id===editingId?{...c,start:selection[0],end:selection[1]}:{...c}));
    else paintSelection();
  }
  function ingestSuggestions(events) {
    const keyed=events.map(e=>({...e,key:JSON.stringify([e.start_frame,e.end_frame,e.category])}));
    const keys=new Set(keyed.map(e=>e.key));
    clips=clips.filter(c=>c.origin!=='ai'||c.checked||c.id===editingId||keys.has(c.aiKey));
    for(const e of keyed){
      if(clips.some(c=>c.aiKey===e.key))continue;
      const restored=clips.find(c=>c.origin==='restored'&&c.start===e.start_frame&&c.end===e.end_frame);
      if(restored){Object.assign(restored,{origin:'ai',title:e.category,aiKey:e.key});continue;}
      clips.push({id:'c'+(++clipSerial),origin:'ai',aiKey:e.key,title:e.category,start:e.start_frame,end:e.end_frame,checked:false});
    }
    remember();paint();
  }
  function resultPresentation() {
    dialog.classList.toggle('cut-result-mode',resultMode);
    if(!context)return;
    const removed=ranges.reduce((n,[a,b])=>n+b-a,0),checked=clips.filter(c=>c.checked).length;
    $('cutSourceInfo').textContent=`${context.session.metadata.width} × ${context.session.metadata.height} · ${fps().toFixed(2)} fps · ${resultMode?'剪后 ':''}${duration(count()-(resultMode?removed:0))}`;
    $('cutSummary').textContent=resultMode?`已删除 ${duration(removed)} · 剪后 ${duration(count()-removed)}`:`将删除 ${checked} 段，共 ${duration(removed)} · 编辑后 ${duration(count()-removed)}`;
    if(resultMode)$('cutOutputLabel').textContent='确认替换才更新项目；关闭可稍后继续，放弃则清空本次编辑。';
  }
  function setResult(show) {
    const previous=resultMode;
    resultMode = show;
    output.hidden = !show; video.hidden = show;
    if (show) { clearTimeout(seekTimer);frameController?.abort();video.pause();image.hidden=true; }
    else { output.pause(); image.hidden = frameReady !== wanted; }
    $('cutStageLabel').textContent = show ? '编辑后视频' : '原片';
    $('cutContinue').hidden = !show;
    $('cutDiscard').hidden = !show;
    resultPresentation();
    if(previous!==show&&!busy)status(show?'视频已生成，请播放检查，再决定是否替换。':'正在调整原片的删除方案，修改后需重新生成。');
  }
  function syncRiskBulk() {
    $('cutRiskAddAll').disabled=busy||!clips.length;
    $('cutRiskAddAll').textContent=clips.length&&clips.every(c=>c.checked)?'取消全选':'全选';
  }
  function paint() {
    if (!context) return;
    syncRiskBulk();
    const removed=ranges.reduce((n,[a,b])=>n+b-a,0), checked=clips.filter(c=>c.checked).length;
    $('cutSummary').textContent=`将删除 ${checked} 段，共 ${duration(removed)} · 编辑后 ${duration(count()-removed)}`;
    $('cutRangeCount').textContent=`${checked} / ${clips.length}`;
    $('cutRanges').innerHTML=clips.map((c,i)=>`<li class="cut-clip ${c.id===editingId?'active':''}"><input type="checkbox" data-clip-check="${c.id}" aria-label="删除片段 ${i+1}" ${c.checked?'checked':''} ${busy?'disabled':''}><button class="cut-clip-view" data-clip-edit="${c.id}" ${busy?'disabled':''}><strong>${esc(c.title)}<span class="cut-clip-state">${c.checked?'待删除':'未勾选'}</span></strong><small>${timecode(c.start)}–${timecode(c.end-1)} · ${duration(c.end-c.start)}</small><small>${c.origin==='ai'?'AI 建议':'手动片段'} · 点击查看 / 微调</small></button><button class="cut-clip-remove" data-cut-remove="${c.id}" aria-label="移除片段 ${i+1}" ${busy?'disabled':''}>×</button></li>`).join('');
    $('cutEmpty').hidden=!!clips.length;
    $('cutMarkers').innerHTML=ranges.map(([a,b])=>`<span title="待删除 ${timecode(a)}–${timecode(b-1)} · 生成后生效" style="left:${100*a/count()}%;width:${100*(b-a)/count()}%">删除</span>`).join('');
    paintRiskMarkers();
    $('cutUndo').disabled=busy||!undo.length;$('cutManual').disabled=busy;
    $('cutRender').disabled=busy||!ranges.length||(!!selection&&!editingId);
    $('cutRender').textContent=busy?'处理中…':'生成编辑后视频';
    const ready=samePlan(selectedJob)&&['ready','saved'].includes(selectedJob.status);
    $('cutDownload').hidden=!ready;$('cutReplace').hidden=!ready;$('cutReplace').disabled=busy;$('cutDiscard').disabled=busy;$('cutContinue').disabled=busy;
    $('cutAdd').disabled=busy||!selection;$('cutAdd').hidden=!!editingId;
    $('cutStart').disabled=busy;$('cutEnd').disabled=busy;
    if(!ready){setResult(false);output.removeAttribute('src');output.load();}
    $('cutOutputLabel').textContent=selection&&!editingId?'手动片段尚未添加，请添加或取消。':ready?'检查播放效果后，可下载或替换项目成片。':'只删除勾选的片段；重叠部分只计算一次。';
    resultPresentation();
  }
  function paintRiskMarkers() {
    const pending=clips.filter(c=>c.origin==='ai'&&!c.checked&&!ranges.some(([a,b])=>a<=c.start&&b>=c.end));
    $('cutRiskMarkers').innerHTML=pending.length?pending.map(c=>`<button data-clip-edit="${c.id}" aria-label="查看 AI 建议 ${esc(c.title)}" title="${esc(c.title)} ${timecode(c.start)}–${timecode(c.end-1)} · 点击查看" style="left:${100*c.start/count()}%;width:${100*(c.end-c.start)/count()}%">建议</button>`).join(''):`<span class="cut-track-empty">${clips.some(c=>c.origin==='ai')?'建议已勾选或被删除范围覆盖':scanReport?.status==='running'?'识别中…':'暂无建议'}</span>`;
  }
  function paintSelection() {
    $('cutRangeEditor').hidden=!selection;
    $('cutRangeEditorTitle').textContent=editingId?'微调片段 · 自动保存':'手动添加片段';
    $('cutResetSelection').textContent=editingId?'完成':'取消';
    $('cutStart').value = selection ? timecode(selection[0]) : '';
    $('cutEnd').value = selection ? timecode(selection[1]-1) : '';
    $('cutSelectionSummary').textContent = selection ? `${selection[1]-selection[0]} 帧 · ${duration(selection[1]-selection[0])}（含开始和结束帧）` : '分:秒:帧 · 从 00:00:00 开始，结束帧包含在内';
    $('cutSelection').hidden = !selection;
    if (selection) {
      $('cutSelection').style.left = 100*selection[0]/count()+'%';
      $('cutSelection').style.width = 100*(selection[1]-selection[0])/count()+'%';
      for (const [id,value] of [['cutHandleStart',selection[0]+1],['cutHandleEnd',selection[1]]]) {
        $(id).setAttribute('aria-valuetext',timecode(value-1)); $(id).setAttribute('aria-valuenow',value); $(id).setAttribute('aria-valuemin',1); $(id).setAttribute('aria-valuemax',count());
      }
    }
    paint();
  }
  function endpoint(which, value, preview = true) {
    if (!context || busy) return;
    if (!Number.isInteger(value) || value < 1 || value > count()) throw Error(`帧号应在 1–${count()} 之间`);
    const next = selection ? selection.slice() : [value-1,value];
    if (which === 'start') next[0] = value-1; else next[1] = value;
    if (next[0] >= next[1] && !editingId) { if(which==='start')next[1]=value;else next[0]=value-1; }
    if (next[0] >= next[1]) throw Error('开始帧不能晚于结束帧');
    const previous=selection;selection=next;
    try{saveSelection();}catch(e){selection=previous;paintSelection();throw e;}
    if (preview) seek(value-1, true);
  }
  function counter() { if (!context) return; $('cutPosition').textContent = `${timecode(wanted)} / ${timecode(count())}`; $('cutSeek').value = wanted; $('cutFrameNumber').value = timecode(wanted); }
  async function loadFrame() {
    if (!context || !dialog.open || quick || frameBusy || !video.paused || resultMode) return;
    const version = epoch; frameBusy = true;
    try {
      while (active(version) && !quick && video.paused && !resultMode && frameReady !== wanted) {
        const target = wanted;
        $('cutFrameHint').textContent = '正在读取准确帧…'; $('cutFrameRetry').hidden = true;
        let blob = frames.get(target);
        if (!blob) {
          frameController = new AbortController();
          const response = await fetch(appUrl(path()+`/frame/${target}`), {signal:frameController.signal});
          if (!response.ok) throw Error('准确帧读取失败，请重试');
          blob = await response.blob();
          if (!active(version)) return;
          frames.set(target,blob); if (frames.size > 40) frames.delete(frames.keys().next().value);
        }
        if (target !== wanted || quick || !video.paused || resultMode) continue;
        const url = URL.createObjectURL(blob); image.src = url;
        try { await image.decode(); } catch { URL.revokeObjectURL(url); throw Error('画面读取失败，请重试'); }
        if (!active(version) || target !== wanted || quick || !video.paused || resultMode) { URL.revokeObjectURL(url); continue; }
        if (imageUrl) URL.revokeObjectURL(imageUrl);
        imageUrl = url; frameReady = target; image.hidden = false;
        $('cutFrameHint').textContent = '准确帧 · 可用两端的 ‹ › 逐帧微调';
      }
    } catch(e) { if (active(version) && e.name !== 'AbortError') { $('cutFrameHint').textContent = e.message; $('cutFrameRetry').hidden = false; } }
    finally { frameBusy = false; if (version !== epoch && context && dialog.open) loadFrame(); }
  }
  function seek(frame, fast = false) {
    if (!context || !Number.isFinite(frame)) return;
    setResult(false); clearTimeout(seekTimer); quick = fast;
    video.pause(); wanted = Math.max(0,Math.min(count()-1,Math.round(frame))); frameReady = -1; image.hidden = true;
    if (Number.isFinite(video.duration)) video.currentTime = (wanted+.05)/fps();
    counter(); $('cutFrameHint').textContent = '快速预览 · 停下后显示准确帧';
    if (fast) seekTimer = setTimeout(()=>{quick=false;loadFrame();},220); else loadFrame();
  }
  function showJob(job) {
    selectedJob = job; paint();
    if (!samePlan(job) || !['ready','saved'].includes(job.status)) return;
    const url = appUrl(path()+`/output/${job.id}`);
    if (output.getAttribute('src') !== url) { output.src = url; output.load(); }
    $('cutDownload').href = url; $('cutDownload').download = context.session.source_filename.replace(/\.mp4$/i,'')+'_edited.mp4';
    setResult(true);
  }
  async function watch(jobId,version) {
    while (active(version)) {
      const state = await api(path()); if (!active(version)) return;
      context.session = state; const job = state.jobs.find(j=>j.id===jobId);
      if (!job || job.status==='failed') throw Error(job?.error || '生成失败，请重试');
      if (['ready','saved'].includes(job.status)) { showJob(job); status('视频已生成。播放检查后，再决定是否替换项目成片。'); return; }
      status('正在生成并校验视频，可关闭窗口，稍后回来查看。');
      await new Promise(r=>setTimeout(r,1000));
    }
  }
  async function scan(retry = false) {
    if (!context) return;
    const version=epoch, token=++scanToken, base=path();
    const valid=()=>active(version)&&token===scanToken;
    scanLoading=true;syncRiskBulk();
    $('cutScan').disabled=true; $('cutScanStatus').textContent='正在读取 AI 建议…';
    try {
      let state=await api(base+'/scan/standard'); if (!valid()) return;
      if (state.enabled && (retry || !state.report)) {
        await api(base+'/scan',{policy:'standard',force:retry}); if(!valid())return;
        if(retry){
          if(clips.some(c=>c.id===editingId&&c.origin==='ai')){editingId=null;selection=null;}
          undo=[];scanReport=null;
          commitClips(clips.filter(c=>c.origin!=='ai'),false);
        }
        state=await api(base+'/scan/standard');
      }
      while(valid()) {
        scanReport=state.report;
        const events=scanReport?.status==='completed' ? scanReport.risk_events : [];
        if(scanReport?.status==='completed')ingestSuggestions(events);else paintRiskMarkers();
        $('cutScanStatus').textContent=!state.enabled?'本机未启用识别，可手动选片段。':!scanReport?'暂无识别记录':scanReport.status==='running'?(scanReport.progress||'正在识别…'):scanReport.status==='failed'?scanReport.error:events.length?'勾选要删除的片段；AI 建议仅供参考。':'本次抽样未发现疑似内容，仍请检查视频。';
        if (scanReport?.status!=='running') { $('cutScan').disabled=!state.enabled; return; }
        await new Promise(r=>setTimeout(r,1500)); if(!valid())return; state=await api(base+'/scan/standard');
      }
    } catch(e) { if(valid()) { $('cutScanStatus').textContent=e.message; $('cutScan').disabled=false; } }
    finally { if(valid()) { scanLoading=false;syncRiskBulk(); } }
  }
  async function open(batch, asset) {
    const version=++epoch; scanToken++; frameController?.abort(); frames.clear(); clearTimeout(seekTimer);
    context=null; clips=[];editingId=null;clipSerial=0;ranges=[];undo=[];selection=null;selectedJob=null;busy=false;wanted=0;frameReady=-1;quick=false;resultMode=false;scanReport=null;scanLoading=false;syncRiskBulk();
    $('cutEditor').hidden=true; $('cutReplaceConfirm').hidden=true; $('cutToast').hidden=true; $('cutDownload').hidden=true; $('cutReplace').hidden=true; $('cutContinue').hidden=true; $('cutRender').disabled=true;
    $('cutRanges').innerHTML='';$('cutRiskMarkers').innerHTML='';$('cutRangeEditor').hidden=true;
    $('cutTitle').textContent='剪切与打码 · '+asset.title;
    for(const v of $('assets').querySelectorAll('video'))v.pause();
    if(!dialog.open)dialog.showModal(); status('正在准备视频…');
    try {
      const session=await api(`/api/batches/${batch}/cuts/open`,{request_id:asset.request_id,source_sha256:asset.sha256});
      if(version!==epoch||!dialog.open)return;
      context={batch,asset,session};
      const chosen=session.jobs.filter(j=>!j.discarded&&!(j.mosaics||[]).length).at(-1);
      ranges=chosen?.ranges?.map(r=>r.slice())||[];
      try { const saved=JSON.parse(localStorage.getItem(draftKey())||'null'); if(Array.isArray(saved)) ranges=merge(saved); } catch { /* Keep server recipe. */ }
      clips=ranges.map(([start,end])=>({id:'c'+(++clipSerial),origin:'restored',title:'已选片段',start,end,checked:true}));
      try {
        const saved=JSON.parse(localStorage.getItem(draftKey()+'.clips')||'null');
        if(Array.isArray(saved)&&saved.every(c=>Number.isInteger(c.start)&&Number.isInteger(c.end)&&c.start>=0&&c.end<=count()&&c.start<c.end&&typeof c.checked==='boolean'&&typeof c.title==='string')){
          const restored=saved.map(c=>({...c,id:'c'+(++clipSerial)}));
          if(JSON.stringify(clipRanges(restored))===JSON.stringify(ranges))clips=restored;
        }
      }catch{}
      $('cutSourceInfo').textContent=`${session.metadata.width} × ${session.metadata.height} · ${fps().toFixed(2)} fps · ${duration(count())}`;
      $('cutSeek').max=count()-1;$('cutTimelineEnd').textContent=timecode(count());
      $('cutEditor').hidden=false;video.src=appUrl(path()+'/source');video.load();image.hidden=true;
      paintSelection();counter();loadFrame();scan();status('勾选要删除的片段，生成后预览，再决定是否替换。');
      if(chosen&&samePlan(chosen)) {
        if(chosen.status==='running') {busy=true;paint();try{await watch(chosen.id,version);}finally{if(active(version)){busy=false;paint();}}}
        else if(['ready','saved'].includes(chosen.status))showJob(chosen);
      }
    }catch(e){if(version===epoch)status(e.message,true);}
  }
  function mount() {
    if(!currentUser?.features?.cutting||!current)return;
    for(const card of $('assets').querySelectorAll('[data-asset]')) {
      const asset=current.assets[Number(card.dataset.asset)],batch=current.id;
      if(!asset?.preview || asset.can_edit_video !== true){card.querySelector('.cut-tools')?.remove();continue;}
      if(card.querySelector('.cut-tools'))continue;
      const box=document.createElement('div');box.className='cut-tools';box.innerHTML='<button type="button" class="secondary" data-open-cut>✂ 剪切与打码</button>';
      const download=card.querySelector('a[download]')?.closest('p');
      if(download){
        let row=download.closest('.cut-media-row');
        if(!row){row=document.createElement('div');row.className='cut-media-row';download.before(row);row.append(download);}
        row.append(box);
      }else{const anchor=card.querySelector('.approve');if(anchor)anchor.before(box);else card.append(box);}
      box.onclick=()=>open(batch,asset);
    }
  }
  $('cutClose').onclick=()=>{if(!$('cutReplaceConfirm').hidden&&busy)return;dialog.close();};
  dialog.addEventListener('close',()=>{epoch++;scanToken++;clearTimeout(seekTimer);frameController?.abort();context=null;video.pause();output.pause();for(const v of [video,output]){v.removeAttribute('src');v.load();}if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=null;image.removeAttribute('src');frames.clear();});
  $('cutMosaic').onclick=()=>notify('打码功能暂未开放，先使用删除片段。');
  for(const which of ['start','end']) {
    const input=$(which==='start'?'cutStart':'cutEnd');
    input.onchange=guarded(()=>endpoint(which,parseTimecode(input.value)+1));
    input.onkeydown=guarded(event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();endpoint(which,parseTimecode(input.value)+1+(event.key==='ArrowLeft'?-1:1));}});
  }
  for(const button of dialog.querySelectorAll('[data-nudge]'))button.onclick=guarded(()=>{const [which,step]=button.dataset.nudge.split(':');endpoint(which,($(which==='start'?'cutStart':'cutEnd').value ? parseTimecode($(which==='start'?'cutStart':'cutEnd').value)+1 : wanted+1)+Number(step));});
  $('cutMarkStart').onclick=guarded(()=>endpoint('start',wanted+1));$('cutMarkEnd').onclick=guarded(()=>endpoint('end',wanted+1));
  $('cutResetSelection').onclick=()=>{if(busy)return;selection=null;editingId=null;paintSelection();};
  $('cutManual').onclick=manual;
  $('cutAdd').onclick=guarded(()=>{if(!selection||busy)return;const [start,end]=selection;const next=[...copyClips(),{id:'c'+(++clipSerial),origin:'manual',title:'手动片段',start,end,checked:true}];clipRanges(next);selection=null;editingId=null;commitClips(next);});
  $('cutRanges').onchange=guarded(e=>{const id=e.target.dataset.clipCheck;if(!id||busy)return;try{commitClips(clips.map(c=>c.id===id?{...c,checked:e.target.checked}:{...c}));}catch(error){paint();throw error;}});
  $('cutRanges').onclick=guarded(e=>{const edit=e.target.closest('[data-clip-edit]');if(edit){editClip(edit.dataset.clipEdit);return;}const b=e.target.closest('[data-cut-remove]');if(!b||busy)return;if(editingId===b.dataset.cutRemove){editingId=null;selection=null;}commitClips(clips.filter(c=>c.id!==b.dataset.cutRemove));});
  $('cutUndo').onclick=()=>{if(busy||!undo.length)return;selection=null;editingId=null;commitClips(undo.pop(),false);};
  $('cutPrevious').onclick=()=>seek(wanted-1,true);$('cutNext').onclick=()=>seek(wanted+1,true);
  $('cutFrameNumber').onchange=guarded(e=>seek(parseTimecode(e.target.value),true));$('cutSeek').oninput=e=>seek(Number(e.target.value),true);
  $('cutFrameRetry').onclick=()=>loadFrame();$('cutContinue').onclick=()=>{if(busy)return;setResult(false);paint();loadFrame();};
  $('cutPlay').onclick=()=>{if(video.paused){setResult(false);clearTimeout(seekTimer);quick=false;image.hidden=true;video.play().catch(()=>status('暂时无法播放，请重试',true));}else video.pause();};
  video.onplay=()=>{$('cutPlay').textContent='暂停';image.hidden=true;};
  video.onpause=()=>{$('cutPlay').textContent='播放';if(context&&!quick&&!resultMode){wanted=Math.min(count()-1,Math.floor(video.currentTime*fps()));frameReady=-1;counter();clearTimeout(seekTimer);seekTimer=setTimeout(loadFrame,220);}};
  video.ontimeupdate=()=>{if(context&&!video.paused){wanted=Math.min(count()-1,Math.floor(video.currentTime*fps()));counter();}};
  const track=$('cutTrack');
  track.onpointerdown=e=>{if(!context||busy)return;const h=e.target.closest('#cutHandleStart,#cutHandleEnd');dragging={which:h?(h.id==='cutHandleStart'?'start':'end'):null,anchor:Math.min(count()-1,Math.max(0,Math.floor((e.clientX-track.getBoundingClientRect().left)/track.clientWidth*count())))};track.setPointerCapture(e.pointerId);if(!h){editingId=null;selection=[dragging.anchor,dragging.anchor+1];paintSelection();seek(dragging.anchor,true);}e.preventDefault();};
  track.onpointermove=e=>{if(!dragging)return;const frame=Math.max(0,Math.min(count()-1,Math.floor((e.clientX-track.getBoundingClientRect().left)/track.clientWidth*count())));if(dragging.which==='start')selection[0]=Math.min(frame,selection[1]-1);else if(dragging.which==='end')selection[1]=Math.max(frame+1,selection[0]+1);else selection=[Math.min(frame,dragging.anchor),Math.max(frame,dragging.anchor)+1];paintSelection();seek(dragging.which==='start'?selection[0]:selection[1]-1,true);};
  track.onpointerup=guarded(()=>{dragging=null;try{saveSelection();}catch(e){if(editingId){const c=clips.find(c=>c.id===editingId);selection=[c.start,c.end];paintSelection();}throw e;}});
  track.onpointercancel=()=>{dragging=null;if(editingId){const c=clips.find(c=>c.id===editingId);selection=[c.start,c.end];paintSelection();}};
  for(const which of ['start','end'])$(which==='start'?'cutHandleStart':'cutHandleEnd').onkeydown=guarded(e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)&&selection){e.preventDefault();endpoint(which,(which==='start'?selection[0]+1:selection[1])+(e.key==='ArrowLeft'?-1:1));}});
  $('cutRiskAddAll').onclick=guarded(()=>{if(busy||!clips.length)return;const checked=!clips.every(c=>c.checked);commitClips(clips.map(c=>({...c,checked})));});
  $('cutRiskMarkers').onclick=e=>{const b=e.target.closest('[data-clip-edit]');if(b)editClip(b.dataset.clipEdit);};
  $('cutScan').onclick=()=>{if(busy||scanLoading)return;if(window.confirm('重新识别将重置 AI 建议及其勾选、微调，手动添加的片段保留。是否继续？'))scan(true);};
  $('cutRender').onclick=async()=>{if(busy||(selection&&!editingId)||!ranges.length)return;const version=epoch;busy=true;video.pause();paint();status('正在提交删除片段…');try{const job=await api(path()+'/render',plan());if(active(version))await watch(job.id,version);}catch(e){if(active(version))status(e.message,true);}finally{if(active(version)){busy=false;paint();}}};
  $('cutDiscard').onclick=async()=>{
    if(busy)return;
    const version=epoch,key=draftKey(),base=path();busy=true;paint();status('正在放弃本次剪切…');
    try{
      await api(base+'/discard',{});
      try{localStorage.removeItem(key);localStorage.removeItem(key+'.clips');}catch{}
      if(active(version)){dialog.close();toast('已放弃本次剪切，项目原片未改变。下次打开会重新识别。');}
    }catch(e){if(active(version))status(e.message,true);}
    finally{if(active(version)){busy=false;paint();}}
  };
  $('cutReplace').onclick=()=>{$('cutReplaceConfirm').hidden=false;$('cutReplaceCancel').focus();};
  $('cutReplaceCancel').onclick=()=>{$('cutReplaceConfirm').hidden=true;};
  $('cutReplaceNow').onclick=async()=>{
    if(busy||!samePlan(selectedJob))return;
    const version=epoch,batch=context.batch;busy=true;paint();$('cutReplaceNow').disabled=true;$('cutReplaceCancel').disabled=true;
    status('正在替换项目成片，上传成功后才切换视频…');
    try {
      let job=await api(path()+'/replace',{job_id:selectedJob.id});
      while(active(version)&&job.replacement?.status==='running') {await new Promise(r=>setTimeout(r,1000));if(!active(version))return;const state=await api(path());job=state.jobs.find(j=>j.id===selectedJob.id);}
      if(!active(version))return;
      if(job.replacement?.status!=='completed')throw Error(job.replacement?.error||'替换未完成，请重试');
      try{localStorage.removeItem(draftKey());localStorage.removeItem(draftKey()+'.clips');}catch{}
      const updated=await api(`/api/batches/${batch}`);if(!active(version))return;
      if(current?.id===batch){current=updated;renderRuntime();}
      dialog.close();toast('已替换项目成片，后续上传将使用新视频。');
    }catch(e){if(active(version))status(e.message,true);}
    finally{if(active(version)){busy=false;paint();}$('cutReplaceNow').disabled=false;$('cutReplaceCancel').disabled=false;$('cutReplaceConfirm').hidden=true;}
  };
  window.Cutting={mount};mount();
})();
