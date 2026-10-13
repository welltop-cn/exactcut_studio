/* Source frame selection is transient until the user explicitly applies it. */
window.SourcePicker=(()=>{
 let session=null,serial=0;
 const el=id=>document.getElementById(id);
 const sides=['Start','End'];
 const clock=ms=>`${String(Math.floor(ms/60000)).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`;
 const part=(clip,side)=>Number(clip[side+'_m']||0)*60000+Number(clip[side+'_s']||0)*1000+Number(clip[side+'_ms']||0);
 const parse=value=>{const m=String(value).match(/^(\d+):(\d{1,2})(?:[.:](\d{1,3}))?$/);return m?Number(m[1])*60000+Number(m[2])*1000+Number((m[3]||'').padEnd(3,'0')):0};
 function clipAt(i,n){const row=current?.draft.rows[i];return row?.composition?row.composition.plots[n]:row&&editorClips(row)?.[n]}
 function valid(s){const row=current?.draft.rows[s?.i];return !!s&&!!row&&(s.rowId?(row.request_id||row.client_id)===s.rowId:row===s.row)&&session===s&&s.id===current?.id&&s.id===selectedId&&s.navigation===openingBatchRequest&&s.drama===current.draft.drama_id&&clipTextEditable()&&JSON.stringify(clipAt(s.i,s.n))===s.before}
 function lowerBound(times,ms){let lo=0,hi=times.length;while(lo<hi){const mid=Math.floor((lo+hi)/2);if(times[mid]<ms)lo=mid+1;else hi=mid}return lo}
 function frameParts(meta,index){const ms=meta.times_ms[index],second=Math.floor(ms/1000);return {m:String(Math.floor(second/60)).padStart(2,'0'),s:String(second%60).padStart(2,'0'),f:String(index-lowerBound(meta.times_ms,second*1000)).padStart(3,'0')}}
 function frameClock(meta,index){const value=frameParts(meta,index);return `${value.m}:${value.s}:${value.f}`}
 function inputFrame(meta,clip,side){const fields=['m','s','f'].map(key=>String(clip[side+'_'+key]??'').trim());if(fields.some(value=>!/^\d{1,3}$/.test(value)))return null;const [m,sec,f]=fields.map(Number);if(sec>59)return null;const second=m*60+sec,index=lowerBound(meta.times_ms,second*1000)+f;return index<meta.times_ms.length&&Math.floor(meta.times_ms[index]/1000)===second?index:null}
 function ensureDialog(){
  if(el('sourcePickerDialog'))return;
  const dialog=document.createElement('dialog');dialog.id='sourcePickerDialog';dialog.setAttribute('aria-labelledby','pickerTitle');
  dialog.innerHTML=`<div class="row"><div><h2 id="pickerTitle">查看画面 / 精调</h2><p id="pickerSource" class="hint"></p></div><button id="pickerPlay" class="secondary" disabled>播放所选片段</button></div>
   <div class="picker-notice"><p id="pickerMessage" role="status">正在读取原片…</p><button id="pickerRetry" class="secondary" hidden>重试加载</button></div>
   <div class="picker-view">${sides.map(side=>`<section id="picker${side}Card" class="picker-card" aria-labelledby="picker${side}Title"><div class="row"><h3 id="picker${side}Title">${side==='Start'?'开始画面':'结束画面'}</h3><span class="hint">${side==='Start'?'保留的第一帧':'保留的最后一帧'}</span></div><figure><img id="picker${side}Frame" alt="${side==='Start'?'开始':'结束'}画面" hidden><span id="picker${side}State">等待画面</span></figure><div class="picker-card-controls"><button id="picker${side}Prev" class="secondary" aria-label="${side==='Start'?'开始':'结束'}画面上一帧" disabled>← 上一帧</button><strong id="picker${side}Time">—</strong><button id="picker${side}Next" class="secondary" aria-label="${side==='Start'?'开始':'结束'}画面下一帧" disabled>下一帧 →</button></div></section>`).join('')}</div>
   <div class="picker-timeline-label"><span>拖动开始 / 结束细线</span><strong id="pickerSelection">尚未选择</strong></div>
   <div id="pickerTimeline" class="picker-timeline"><div id="pickerTicks" class="picker-ticks"></div><div class="picker-track"><div id="pickerSelectedTrack"></div></div>${sides.map(side=>`<button id="picker${side}Handle" class="picker-playhead picker-${side.toLowerCase()}" role="slider" aria-label="${side==='Start'?'开始':'结束'}画面位置" disabled><span>${side==='Start'?'开始':'结束'}</span></button>`).join('')}</div>
   <p class="hint picker-help">拖动开始、结束细线，分别调整两张画面；选中细线后用方向键逐帧。结束画面会保留。</p>
   <div id="pickerPlayer" class="picker-player" hidden><button id="pickerHidePlayer" class="secondary">收起播放</button><video id="pickerVideo" controls playsinline preload="none" aria-label="所选片段播放预览"></video></div>
   <div class="actions picker-footer"><span class="hint">应用后回填时间，原输入在此之前保持不变。</span><button id="pickerCancel" class="secondary">取消精调</button><button id="pickerApply" disabled>应用到片段</button></div>`;
  document.body.append(dialog);
  dialog.addEventListener('cancel',event=>{event.preventDefault();close()});el('pickerCancel').onclick=close;
  el('pickerRetry').onclick=()=>{if(session)load(session)};el('pickerApply').onclick=apply;
  for(const side of sides){
   el('picker'+side+'Prev').onclick=()=>select(side,session[side].index-1);
   el('picker'+side+'Next').onclick=()=>select(side,session[side].index+1);
   const handle=el('picker'+side+'Handle');
   handle.onkeydown=event=>{if(!session?.meta||handle.disabled)return;const index=session[side].index,keys={ArrowLeft:index-1,ArrowDown:index-1,ArrowRight:index+1,ArrowUp:index+1,Home:0,End:session.meta.times_ms.length-1};if(event.key in keys){event.preventDefault();select(side,keys[event.key])}};
   const point=event=>{const s=session;if(!s?.meta||handle.disabled)return;const rect=el('pickerTimeline').getBoundingClientRect(),ms=Math.max(0,Math.min(s.meta.duration_ms,(event.clientX-rect.left)/rect.width*s.meta.duration_ms)),times=s.meta.times_ms;let index=Math.min(times.length-1,lowerBound(times,ms));if(index>0&&ms-times[index-1]<times[index]-ms)index--;select(side,index,true)};
   handle.onpointerdown=event=>{if(handle.disabled)return;event.preventDefault();handle.focus();handle.setPointerCapture(event.pointerId)};
   handle.onpointermove=event=>{if(handle.hasPointerCapture(event.pointerId))point(event)};
   handle.onpointerup=event=>{if(handle.hasPointerCapture(event.pointerId))handle.releasePointerCapture(event.pointerId)};
  }
  el('pickerHidePlayer').onclick=hidePlayer;
  el('pickerPlay').onclick=()=>{const s=session;if(!valid(s)||s.applying||s.end<=s.start)return;el('pickerPlayer').hidden=false;const v=el('pickerVideo');if(!v.getAttribute('src'))v.src=appUrl(s.base+'/video');v.currentTime=s.start/1000;v.play().catch(()=>{if(session===s)el('pickerMessage').textContent='视频暂不可播放，可继续用画面精调。'})};
  el('pickerVideo').addEventListener('timeupdate',()=>{if(session&&el('pickerVideo').currentTime>=session.end/1000)el('pickerVideo').pause()});
 }
 function hidePlayer(){el('pickerVideo')?.pause();if(el('pickerPlayer'))el('pickerPlayer').hidden=true}
 function close(){serial++;if(session)for(const side of sides)clearTimeout(session[side].timer);session=null;hidePlayer();const v=el('pickerVideo');if(v){v.removeAttribute('src');v.load()}el('sourcePickerDialog')?.close();for(const side of sides){const img=el('picker'+side+'Frame');if(img){img.hidden=true;img.removeAttribute('src')}}}
 function selection(){
  const s=session;if(!s)return;const ready=!!s.meta&&valid(s)&&!s.applying;
  for(const side of sides){const state=s[side],first=side==='Start'||!s.hasFrames?0:s.Start.index,last=side==='Start'&&s.hasFrames?s.End.index:(s.meta?.times_ms.length||1)-1;
   el('picker'+side+'Prev').disabled=!ready||state.index<=first;el('picker'+side+'Next').disabled=!ready||state.index>=last;
   const handle=el('picker'+side+'Handle');handle.disabled=!ready;handle.setAttribute('aria-valuemin',first);handle.setAttribute('aria-valuemax',last);handle.setAttribute('aria-valuenow',state.index);
   if(s.meta){el('picker'+side+'Time').textContent=s.frames?(state.inputValid?frameClock(s.meta,state.index):'帧序号待修正'):clock(s[side.toLowerCase()]);handle.style.left=s.meta.times_ms[state.index]/s.meta.duration_ms*100+'%';handle.setAttribute('aria-valuetext',s.frames?frameClock(s.meta,state.index):clock(s.meta.times_ms[state.index]))}
  }
  if(s.meta){const times=s.meta.times_ms,duration=s.meta.duration_ms;el('pickerSelectedTrack').style.left=times[s.Start.index]/duration*100+'%';el('pickerSelectedTrack').style.width=s.hasFrames?(times[s.End.index]-times[s.Start.index])/duration*100+'%':'0%'}
  el('pickerSelection').textContent=s.hasFrames?`已选 ${s.End.index-s.Start.index+1} 帧 · ${((s.end-s.start)/1000).toFixed(3)} 秒`:'尚无可保留画面';
  const ok=ready&&s.hasFrames&&s.end>s.start&&s.start>=0&&s.end<=s.meta.duration_ms&&s.Start.exact&&s.End.exact;
  el('pickerApply').disabled=!ok;el('pickerPlay').disabled=!ok;el('pickerRetry').disabled=!!s.applying;
 }
 async function load(s){
  if(s.applying)return;hidePlayer();const video=el('pickerVideo');video.removeAttribute('src');video.load();const request=++serial;for(const side of sides){clearTimeout(s[side].timer);s[side].generation++;s[side].exact=false;el('picker'+side+'Frame').hidden=true;el('picker'+side+'State').textContent='正在读取画面…'}
  s.meta=null;selection();el('pickerRetry').hidden=true;
  if(!valid(s)){el('pickerMessage').textContent='片段或项目已变化，请关闭后重新打开。';return}
  el('pickerMessage').textContent='正在读取原片，首次打开可能需要一些时间…';
  try{
   const meta=await api(`/api/batches/${s.id}/source-picker`,{draft:structuredClone(current.draft),episode:s.episode});
   if(request!==serial||!valid(s))return;
   s.meta=meta;s.base=`/api/batches/${s.id}/source-picker/${meta.key}`;
   el('pickerSource').textContent=`${meta.episode} · ${s.frames?'分:秒:帧 · 帧从 0 开始':'原时间格式'} · 拖动两端细线选帧`;
   el('pickerTicks').innerHTML=Array.from({length:5},(_,i)=>{const index=Math.min(meta.times_ms.length-1,lowerBound(meta.times_ms,meta.duration_ms*i/4)),label=s.frames?frameClock(meta,index):clock(meta.times_ms[index]);return `<span class="picker-tick" style="left:${i*25}%">${label}</span>`}).join('');
   if(s.frames){const clip=clipAt(s.i,s.n);for(const side of sides){if(s[side].edited)continue;const index=inputFrame(meta,clip,side.toLowerCase());s[side].inputValid=index!==null;s[side.toLowerCase()]=index===null?NaN:side==='Start'?meta.times_ms[index]:(meta.times_ms[index+1]??meta.duration_ms)}}
   rangeFrames(s);selection();
  }catch(error){if(request===serial&&session===s){el('pickerMessage').textContent=error.message+'；可重试加载或取消。';el('pickerRetry').hidden=false}}
 }
 function rangeFrames(s){
  const times=s.meta.times_ms,start=lowerBound(times,s.start),end=lowerBound(times,s.end)-1;
  s.hasFrames=Number.isFinite(s.start)&&Number.isFinite(s.end)&&start<=end&&start<times.length&&end>=0;
  s.Start.index=Math.min(times.length-1,start);s.End.index=Math.max(0,end);
  if(s.hasFrames){for(const side of sides)frame(s,side,false);return}
  for(const side of sides){s[side].exact=false;s[side].generation++;clearTimeout(s[side].timer);el('picker'+side+'Frame').hidden=true;el('picker'+side+'State').textContent='此范围没有可保留画面'}
  el('pickerMessage').textContent='当前时间范围没有可保留画面，请拖动两端或逐帧调整范围。';
 }
 function select(side,index,delayed=false){
  const s=session;if(!s?.meta||!valid(s)||s.applying)return;const state=s[side],wasEmpty=!s.hasFrames;
  index=Math.max(side==='Start'||wasEmpty?0:s.Start.index,Math.min(side==='Start'&&!wasEmpty?s.End.index:s.meta.times_ms.length-1,index));
  if(index===state.index&&!wasEmpty)return;
  state.index=index;state.edited=true;state.inputValid=true;s[side.toLowerCase()]=side==='Start'?s.meta.times_ms[index]:(s.meta.times_ms[index+1]??s.meta.duration_ms);
  hidePlayer();if(wasEmpty)rangeFrames(s);else frame(s,side,delayed);selection();
 }
 function frame(s,side,delayed){
  const state=s[side],token=++state.generation,metadata=serial;state.exact=false;clearTimeout(state.timer);el('picker'+side+'Frame').hidden=true;el('picker'+side+'State').textContent='正在确认画面…';el('pickerMessage').textContent='正在确认画面，确认完成后可应用。';
  const fresh=()=>metadata===serial&&token===state.generation&&valid(s);
  const fetchFrame=()=>{if(!fresh())return;const img=new Image();img.onload=()=>{if(!fresh())return;el('picker'+side+'Frame').src=img.src;el('picker'+side+'Frame').hidden=false;el('picker'+side+'State').textContent='';state.exact=true;selection();if(s.Start.exact&&s.End.exact)el('pickerMessage').textContent='两端画面已确认，可继续精调或应用。'};img.onerror=()=>{if(!fresh())return;el('picker'+side+'State').textContent='画面加载失败';el('pickerMessage').textContent='画面读取失败，请重试加载；尚未更改片段。';el('pickerRetry').hidden=false};img.src=appUrl(s.base+'/frame/'+state.index)};
  if(delayed)state.timer=setTimeout(fetchFrame,120);else fetchFrame();
 }
 async function apply(){
  const s=session;if(!s?.meta||!valid(s)){el('pickerMessage').textContent='片段或项目已变化，请关闭后重新打开。';return}
  if(s.applying||!s.hasFrames||!s.Start.exact||!s.End.exact||!(s.end>s.start&&s.start>=0&&s.end<=s.meta.duration_ms))return;
  const chosen={start:s.start,end:s.end,key:s.meta.key,generation:serial};s.applying=true;hidePlayer();selection();el('pickerMessage').textContent='正在确认原片未变化…';
  try{
   const latest=await api(`/api/batches/${s.id}/source-picker`,{draft:structuredClone(current.draft),episode:s.episode});
   if(!valid(s))throw Error('片段或项目已变化，请关闭后重新打开。');
   if(serial!==chosen.generation||latest.key!==chosen.key)throw Error('原片已变化，请关闭后重新选帧。');
   const row=current.draft.rows[s.i];
   for(const side of sides){if(!s[side].edited)continue;const name=side.toLowerCase(),ms=chosen[name];if(row.composition)row.composition.plots[s.n][name]=clock(ms);else{const c=ensureEditorClips(row)[s.n];if(s.frames){const parts=frameParts(s.meta,s[side].index);for(const key of ['m','s','f'])c[name+'_'+key]=parts[key];continue}c[name+'_m']=String(Math.floor(ms/60000)).padStart(2,'0');c[name+'_s']=String(Math.floor(ms/1000)%60).padStart(2,'0');c[name+'_ms']=String(ms%1000).padStart(3,'0')}}
   const edited=s.Start.edited||s.End.edited;if(edited)invalidateHook(s.i);close();if(edited){changed();renderEditor();toast('已回填选取时间，请保存并校验。')}
  }catch(error){if(session===s){s.applying=false;selection();el('pickerMessage').textContent=error.message}}
 }
 function open(i,n){
  if(!clipTextEditable())return;const c=clipAt(i,n);if(!c)return;
  const episode='EP'+String(c.episode||'').trim().replace(/^EP/i,'');if(!/^EP[1-9]\d{0,2}$/.test(episode)||Number(episode.slice(2))>500){toast('请先填写有效集号和时间段',true);return}
  ensureDialog();close();const composition=!!current.draft.rows[i].composition;
  session={frames:!composition&&c.time_format==='source_frames_v1',id:current.id,navigation:openingBatchRequest,drama:current.draft.drama_id,i,n,episode,rowId:current.draft.rows[i].request_id||current.draft.rows[i].client_id,row:current.draft.rows[i],before:JSON.stringify(c),start:composition?parse(c.start):part(c,'start'),end:composition?parse(c.end):part(c,'end'),Start:{index:0,generation:0,edited:false},End:{index:0,generation:0,edited:false}};
  el('sourcePickerDialog').showModal();load(session);
 }
 function render(){
  document.querySelectorAll('#requests .request').forEach((card,i)=>{
   card.querySelectorAll('.clip-row,.plot-row').forEach((line,n)=>{
    if(line.querySelector('[data-source-picker]'))return;
    const button=document.createElement('button');button.type='button';button.className='secondary';button.dataset.sourcePicker=`${i}:${n}`;button.textContent='查看画面 / 精调';button.onclick=()=>open(i,n);
    const status=document.createElement('span');status.className='hint picker-status';status.dataset.pickerStatus=`${i}:${n}`;line.append(button,status);
   });
  });update();
 }
 function update(){
  document.querySelectorAll('[data-source-picker]').forEach(button=>{const [i,n]=button.dataset.sourcePicker.split(':').map(Number),c=clipAt(i,n),episode='EP'+String(c?.episode||'').trim().replace(/^EP/i,'');button.disabled=!clipTextEditable()||!/^EP[1-9]\d{0,2}$/.test(episode)||Number(episode.slice(2))>500;const state=current?.source_prefetch?.episodes?.find(e=>e.episode===episode);const status=document.querySelector(`[data-picker-status="${i}:${n}"]`);if(status)status.textContent=state?({waiting:'原片待下载',queued:'原片下载排队中',downloading:'正在下载原片',ready:'原片已就绪',failed:'下载失败 · 可重试'}[state.status]||''):currentUser?.features.material_fetch?'填好后自动准备原片':'可查看本地已缓存原片';});
  if(session&&!valid(session)){selection();el('pickerApply').disabled=true;el('pickerMessage').textContent='片段或项目已变化，请关闭后重新打开。'}
 }
 return {render,update,close};
})();
