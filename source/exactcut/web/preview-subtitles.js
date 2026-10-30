/* Preview-only native captions. Never changes the media URL or delivery data. */
(() => {
  const dialog = $('subtitleTimeline');
  const entries = new Map(), panels = new Map(), tracks = new WeakMap();
  let dialogPanel = null, focusTarget = null;
  const binding = (batch,asset) => ({batch_id:batch,request_id:asset.request_id,source_sha256:asset.sha256,version_id:asset.version_id||null});
  const keyOf = value => JSON.stringify([value.batch_id,value.request_id,value.source_sha256,value.version_id||null]);
  const clock = ms => `${Math.floor(ms/60000).toString().padStart(2,'0')}:${(ms/1000%60).toFixed(2).padStart(5,'0')}`;
  const active = panel => !panel.disposed && panel.video.isConnected && activeTab==='review' && !$('workspace').hidden &&
    current?.id===panel.binding.batch_id && selectedId===panel.binding.batch_id &&
    current.assets.some(a=>a.preview && keyOf(binding(current.id,a))===panel.key);
  const endpoint = panel => `/api/batches/${panel.binding.batch_id}/preview-subtitles/${encodeURIComponent(panel.binding.request_id)}`;
  const getPath = panel => `${endpoint(panel)}?source_sha256=${panel.binding.source_sha256}&version_id=${panel.binding.version_id||''}`;
  function entryFor(key) {
    if(!entries.has(key))entries.set(key,{enabled:false,report:{status:'idle',cues:[]},autoOpen:false});
    return entries.get(key);
  }
  function clearTrack(video) {
    const saved=tracks.get(video);if(!saved)return;
    // Disabled native tracks expose cues=null in Chromium; use hidden while clearing.
    saved.track.mode='hidden';
    for(const cue of Array.from(saved.track.cues||[]))saved.track.removeCue(cue);
    saved.track.mode='disabled';saved.signature='';
  }
  function nativeTrack(panel) {
    const entry=panel.entry,report=entry.report;
    if(report.status!=='completed'){const saved=tracks.get(panel.video);if(saved)saved.track.mode='disabled';return;}
    if(!window.VTTCue){panel.status.textContent='当前浏览器不支持原生字幕，可查看字幕时间轴';return;}
    let saved=tracks.get(panel.video);
    if(!saved){saved={track:panel.video.addTextTrack('subtitles','中文字幕 (ExactCut)','zh'),signature:''};tracks.set(panel.video,saved);}
    const signature=report.id+JSON.stringify(report.cues);
    if(saved.signature!==signature){
      clearTrack(panel.video);
      for(const cue of report.cues){
        // VTTCue has its own markup; escape model text to preserve literal characters.
        const text=cue.translation_zh.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const native=new VTTCue(cue.start_ms/1000,cue.end_ms/1000,text);native.line=2;
        saved.track.addCue(native);
      }
      saved.signature=signature;
    }
    saved.track.mode=entry.enabled?'showing':'disabled';
  }
  function queuedText(report) {
    const ahead=(report.queue_position||1)-1;
    return ahead>0?`排队中 · 前面还有 ${ahead} 条`:'排队中 · 即将开始识别';
  }
  function paint(panel) {
    if(!active(panel))return;
    const {entry}=panel,report=entry.report,done=report.status==='completed';
    const working=report.status==='running'||report.status==='queued';
    panel.toggle.checked=entry.enabled;panel.toggle.disabled=false;
    panel.label.textContent=report.status==='idle'?'生成中文字幕':'中文字幕';
    panel.timeline.disabled=!done;panel.retry.hidden=report.status!=='failed';
    panel.status.className='subtitle-status'+(working?' loading':done&&entry.enabled?' ready':report.status==='failed'?' failed':'');
    panel.status.textContent=report.status==='running'?`${report.progress||'正在识别'}${entry.enabled?'':' · 已关闭，完成后不弹窗'}`:
      report.status==='queued'?`${queuedText(report)}${entry.enabled?'':' · 已关闭，完成后不弹窗'}`:
      done?(report.cues.length?(entry.enabled?`已就绪 · 共 ${report.cues.length} 句 · 已缓存`:'字幕已隐藏 · 缓存保留'): '未识别到可见对白字幕'):
      report.status==='failed'?(report.error||'字幕识别失败，请点击重试'):'未生成 · 点击开启识别';
    nativeTrack(panel);sync(panel);
  }
  function sync(panel) {
    if(dialogPanel!==panel||!dialog.open||!active(panel))return;
    const time=panel.video.currentTime*1000,cues=panel.entry.report.cues||[];
    const index=cues.findIndex(c=>c.start_ms<=time&&time<c.end_ms);
    if(panel.activeIndex===index)return;
    panel.activeIndex=index;
    for(const item of $('subtitleCues').querySelectorAll('[data-subtitle-cue]')){
      const selected=Number(item.dataset.subtitleCue)===index;item.classList.toggle('active',selected);
      if(selected)item.scrollIntoView({block:'nearest'});
    }
  }
  function closeTimeline(){if(dialog.open)dialog.close();}
  function openTimeline(panel) {
    if(!active(panel)||panel.entry.report.status!=='completed')return;
    if(dialog.open&&dialogPanel!==panel)return;
    dialogPanel=panel;focusTarget=panel.timeline;panel.activeIndex=-2;
    const report=panel.entry.report;
    $('subtitleTitle').textContent='📑 双语字幕时间轴';
    $('subtitleInfo').textContent=`素材 ${panel.binding.request_id} · 共 ${report.cues.length} 条对白 · ${report.elapsed_seconds??'—'} 秒`;
    $('subtitleModel').textContent=`模型：${report.model||'Gemini'} · 已保存于本项目`;
    $('subtitleCues').replaceChildren();
    if(!report.cues.length){const empty=document.createElement('p');empty.textContent='未识别到可见对白字幕';$('subtitleCues').append(empty);}
    report.cues.forEach((cue,index)=>{
      const item=document.createElement('button');item.type='button';item.className='subtitle-cue';item.dataset.subtitleCue=index;
      const time=document.createElement('span');time.className='subtitle-time';time.textContent=`${clock(cue.start_ms)} → ${clock(cue.end_ms)}`;
      const original=document.createElement('span');original.className='subtitle-original';original.textContent=cue.original_text;
      const translation=document.createElement('strong');translation.textContent=cue.translation_zh;
      item.append(time,original,translation);
      item.onclick=()=>{
        if(!active(panel))return;
        panel.video.currentTime=cue.start_ms/1000;
        panel.video.play().catch(()=>{if(active(panel))toast('已定位字幕，请点击视频播放');});
        sync(panel);if($('subtitleAutoClose').checked)closeTimeline();
        toast(`已跳转至 ${clock(cue.start_ms)} · ${cue.translation_zh}`);
      };
      $('subtitleCues').append(item);
    });
    if(!dialog.open)dialog.showModal();
    sync(panel);$('subtitleClose').focus();
  }
  function apply(panel,response) {
    if(!active(panel))return false;
    const value=response.binding;
    if(!value||keyOf(value)!==panel.key){
      panel.entry.autoOpen=false;panel.entry.report={status:'failed',cues:[],error:'成片版本已变化，请刷新后重试'};paint(panel);return false;
    }
    panel.entry.report=response.report;paint(panel);
    if(response.report.status==='completed'){
      const shouldOpen=panel.entry.autoOpen&&panel.entry.enabled&&!document.hidden&&!document.querySelector('dialog[open]');
      panel.entry.autoOpen=false;if(shouldOpen)openTimeline(panel);
    }
    return true;
  }
  function failed(panel,error) {
    if(!active(panel))return;
    panel.entry.autoOpen=false;
    panel.entry.report={status:'failed',cues:[],error:error.message||'字幕识别失败，请点击重试'};paint(panel);
  }
  const isPending = status => status==='running'||status==='queued';
  function watch(panel) {
    clearTimeout(panel.timer);
    const status=panel.entry.report.status;
    if(!active(panel)||!isPending(status))return;
    // Status reads never enqueue; the backend dispatcher is what advances the queue.
    panel.timer=setTimeout(async()=>{
      if(!active(panel))return;
      try{if(apply(panel,await api(getPath(panel))))watch(panel);}catch(error){failed(panel,error);}
    },status==='queued'?1500:1000);
  }
  async function turnOn(panel,retry=false) {
    if(!active(panel))return;
    panel.entry.enabled=true;paint(panel);
    if(panel.pending||panel.entry.report.status==='completed')return;
    if(panel.entry.report.status==='failed'&&!retry)return;
    panel.pending=true;
    try{
      // GET first reconnects to a prior tab/task/cache without charging again.
      if(!apply(panel,await api(getPath(panel))))return;
      if(panel.entry.report.status==='idle'||(retry&&panel.entry.report.status==='failed')){
        panel.entry.autoOpen=panel.entry.enabled&&!panel.suppressed;
        if(!apply(panel,await api(endpoint(panel),{source_sha256:panel.binding.source_sha256,version_id:panel.binding.version_id})))return;
      }
      watch(panel);
    }catch(error){failed(panel,error);}finally{panel.pending=false;}
  }
  function remove(panel) {
    panel.disposed=true;panel.entry.autoOpen=false;clearTimeout(panel.timer);panel.listeners.abort();
    clearTrack(panel.video);if(dialogPanel===panel)closeTimeline();panel.row.remove();panels.delete(panel.video);
  }
  function dispose(){for(const panel of Array.from(panels.values()))remove(panel);}
  function mount() {
    if(!currentUser?.features?.preview_subtitles||!current||activeTab!=='review'||$('workspace').hidden){dispose();return;}
    for(const panel of Array.from(panels.values()))if(!active(panel))remove(panel);
    for(const card of $('assets').querySelectorAll('[data-asset]')){
      const asset=current.assets[Number(card.dataset.asset)],video=card.querySelector('video');
      if(!asset?.preview||!video||!asset.sha256||panels.has(video))continue;
      const value=binding(current.id,asset),key=keyOf(value),entry=entryFor(key);
      const row=document.createElement('div');row.className='subtitle-row';
      row.innerHTML='<label class="subtitle-toggle-wrap"><span class="subtitle-switch"><input type="checkbox" data-subtitle-toggle aria-label="生成中文字幕"><span></span></span><span data-subtitle-label>生成中文字幕</span></label><span class="subtitle-status" role="status"></span><button type="button" class="secondary" data-subtitle-timeline disabled>📑 查看字幕时间轴</button><button type="button" class="secondary" data-subtitle-retry hidden>重试识别</button><small>中文字幕仅供预览，不影响下载或上传的成片。</small>';
      card.querySelector('.videoarea').after(row);
      const panel={video,row,key,binding:value,entry,toggle:row.querySelector('[data-subtitle-toggle]'),label:row.querySelector('[data-subtitle-label]'),status:row.querySelector('.subtitle-status'),timeline:row.querySelector('[data-subtitle-timeline]'),retry:row.querySelector('[data-subtitle-retry]'),listeners:new AbortController(),pending:false,suppressed:false,disposed:false};
      panels.set(video,panel);
      panel.toggle.onchange=()=>{
        entry.enabled=panel.toggle.checked;
        if(entry.enabled)turnOn(panel);else{entry.autoOpen=false;panel.suppressed=true;paint(panel);}
      };
      panel.timeline.onclick=()=>openTimeline(panel);
      panel.retry.onclick=()=>{panel.suppressed=false;turnOn(panel,true);};
      for(const event of ['timeupdate','seeking','seeked','ended'])video.addEventListener(event,()=>sync(panel),{signal:panel.listeners.signal});
      video.addEventListener('loadedmetadata',()=>{if(active(panel)){nativeTrack(panel);sync(panel);}},{signal:panel.listeners.signal});
      paint(panel);
      if(isPending(entry.report.status)){
        entry.autoOpen=false;api(getPath(panel)).then(response=>{if(apply(panel,response))watch(panel);},error=>failed(panel,error));
      }
    }
    // Keep completed/hidden state for normal project revisits without unbounded growth.
    if(entries.size>100)for(const [key,entry] of entries){if(!Array.from(panels.values()).some(p=>p.key===key)&&entry.report.status!=='running')entries.delete(key);if(entries.size<=100)break;}
  }
  $('subtitleClose').onclick=closeTimeline;$('subtitleFooterClose').onclick=closeTimeline;
  dialog.addEventListener('close',()=>{dialogPanel=null;const target=focusTarget;focusTarget=null;if(target?.isConnected)target.focus();});
  dialog.addEventListener('click',event=>{const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeTimeline();});
  window.PreviewSubtitles={mount,dispose};mount();
})();
