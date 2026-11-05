/* Timeline for the isolated experiment. All ranges are [start, end). */
class LiveMosaicTimeline {
  constructor({video,getDuration,isSynthetic,onDraw,onSeek}) {
    this.video=video;this.getDuration=getDuration;this.isSynthetic=isSynthetic;this.onDraw=onDraw;this.onSeek=onSeek;
    this.fps=30;this.frame=0;this.pendingFrame=null;this.seekVersion=0;this.sourceEpoch=0;this.thumbnailCache=new Map();this.drag=null;
    this.$=id=>document.getElementById(id);
    this.scroll=this.$('timelineScroll');this.content=this.$('timelineContent');
    this.reader=document.createElement('video');this.reader.muted=true;this.reader.preload='auto';
    this.$('prevFrame').onclick=()=>this.seek(this.frame-1,true);
    this.$('nextFrame').onclick=()=>this.seek(this.frame+1,true);
    this.$('frameNumber').onchange=()=>this.seek(Number(this.$('frameNumber').value),true);
    this.$('fps').onchange=()=>{this.setFps(Number(this.$('fps').value));this.refresh();};
    this.$('zoom').oninput=()=>{
      const anchor=(this.scroll.scrollLeft+this.scroll.clientWidth/2)/this.pixels;
      this.pixels=Number(this.$('zoom').value);this.refresh();
      this.scroll.scrollLeft=anchor*this.pixels-this.scroll.clientWidth/2;this.scheduleThumbnails();
    };
    for(const id of ['start','end'])this.$(id).addEventListener('change',()=>{this.normalizeRange();this.onDraw();});
    this.scroll.addEventListener('scroll',()=>{this.ruler();this.scheduleThumbnails();});
    this.content.onpointerdown=event=>this.pointerDown(event);
    this.content.onpointermove=event=>this.pointerMove(event);
    this.content.onpointerup=()=>this.pointerEnd();
    this.content.onpointercancel=()=>{this.drag=null;};
    document.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight'].includes(event.key)||event.altKey||event.ctrlKey||event.metaKey)return;
      if(event.target.closest('input,select,textarea,[contenteditable=true]'))return;
      const step=event.key==='ArrowLeft'?-1:1;
      if(['rangeStart','rangeEnd'].includes(event.target.id)){
        const [start,end]=this.range(),edge=event.target.id==='rangeStart';
        this.setRange(edge?Math.min(end-1,start+step):start,edge?end:Math.max(start+1,end+step));this.onDraw();
      }else this.seek(this.frame+step,true);
      event.preventDefault();
    });
    this.pixels=Number(this.$('zoom').value);
    new ResizeObserver(()=>this.refresh()).observe(this.scroll);
  }
  get count(){return Math.max(1,Math.ceil(this.getDuration()*this.fps-1e-6));}
  setFps(fps){if(!Number.isFinite(fps)||fps<=0)return;if(fps!==this.fps)this.thumbnailCache.clear();this.fps=fps;const select=this.$('fps');let option=Array.from(select.options).find(o=>Number(o.value)===fps);if(!option){option=new Option(fps.toFixed(3),String(fps));select.add(option);}select.value=String(fps);this.$('frameNumber').max=this.count-1;}
  timecode(frame){const seconds=Math.floor(frame/this.fps+1e-7),within=frame-Math.ceil(seconds*this.fps-1e-7);return [Math.floor(seconds/60),seconds%60,within].map(n=>String(n).padStart(2,'0')).join(':');}
  range(){return [Math.round(Number(this.$('start').value)*this.fps),Math.round(Number(this.$('end').value)*this.fps)];}
  setRange(start,end){start=Math.max(0,Math.min(this.count-1,Math.round(start)));end=Math.max(start+1,Math.min(this.count,Math.round(end)));this.$('start').value=(start/this.fps).toFixed(6);this.$('end').value=(end/this.fps).toFixed(6);this.paintRange();}
  normalizeRange(){this.setRange(...this.range());}
  paintRange(){const [start,end]=this.range(),block=this.$('mosaicRange');block.style.left=start*this.pixels+'px';block.style.width=Math.max(2,(end-start)*this.pixels)+'px';block.title=`${start}–${end-1} 帧（结束 ${end} 不含）`;for(const [id,value] of [['rangeStart',start],['rangeEnd',end]]){const el=this.$(id);el.setAttribute('aria-valuemin','0');el.setAttribute('aria-valuemax',String(this.count));el.setAttribute('aria-valuenow',String(value));}}
  refresh(){this.content.style.width=Math.max(this.scroll.clientWidth,this.count*this.pixels)+'px';this.$('frameNumber').max=this.count-1;this.ruler();this.paintRange();this.update(this.isSynthetic()?Number(this.$('seek').value):this.video.currentTime);this.scheduleThumbnails();}
  update(time){if(this.pendingFrame===null)this.frame=Math.max(0,Math.min(this.count-1,Math.floor(time*this.fps+1e-4)));this.$('frameNumber').value=this.frame;this.$('timecode').textContent=this.timecode(this.frame);this.$('playhead').style.left=this.frame*this.pixels+'px';this.paintRange();}
  seek(frame,accurate){const version=++this.seekVersion;this.frame=Math.max(0,Math.min(this.count-1,Math.round(frame)||0));this.pendingFrame=this.frame;this.update(this.frame/this.fps);const result=this.onSeek(this.frame,accurate);Promise.resolve(result).finally(()=>{if(version===this.seekVersion)this.pendingFrame=null;});}
  frameAt(event){const rect=this.content.getBoundingClientRect();return Math.max(0,Math.min(this.count,Math.round((event.clientX-rect.left)/this.pixels)));}
  pointerDown(event){if(event.button!==0)return;event.preventDefault();this.content.setPointerCapture(event.pointerId);const [start,end]=this.range();const kind=event.target.id==='rangeStart'?'start':event.target.id==='rangeEnd'?'end':event.target.closest('#mosaicRange')?'move':'seek';this.drag={kind,frame:this.frameAt(event),start,end};if(kind==='seek')this.seek(this.drag.frame,false);}
  pointerMove(event){if(!this.drag)return;const frame=this.frameAt(event),d=this.drag,delta=frame-d.frame;if(d.kind==='seek'){this.seek(frame,false);return;}if(d.kind==='start')this.setRange(Math.min(d.end-1,d.start+delta),d.end);else if(d.kind==='end')this.setRange(d.start,Math.max(d.start+1,d.end+delta));else{const offset=Math.max(-d.start,Math.min(this.count-d.end,delta));this.setRange(d.start+offset,d.end+offset);}this.onDraw();}
  pointerEnd(){const d=this.drag;this.drag=null;if(d?.kind==='seek')this.seek(this.frame,true);}
  ruler(){const first=this.scroll.scrollLeft,last=first+this.scroll.clientWidth,p=this.pixels;const steps=[1,2,5,10,15,30,60,150,300,600,1800,3600];const step=steps.find(n=>n*p>=64)||Math.ceil(64/p);this.$('ruler').replaceChildren();for(let f=Math.floor(first/p/step)*step;f<=Math.min(this.count,last/p+step);f+=step){const tick=document.createElement('span');tick.style.left=f*p+'px';tick.textContent=p>=8?`${this.timecode(f)} · ${f}f`:this.timecode(f);this.$('ruler').append(tick);}}
  setSource(url){this.sourceEpoch++;this.thumbnailCache.clear();this.$('thumbs').replaceChildren();this.reader.pause();this.reader.removeAttribute('src');if(url){this.reader.src=url;this.reader.load();}this.scheduleThumbnails();}
  scheduleThumbnails(){clearTimeout(this.thumbTimer);this.thumbTimer=setTimeout(()=>this.thumbnails(),120);}
  async readyReader(epoch){if(this.reader.readyState>=2)return;await new Promise((resolve,reject)=>{const timer=setTimeout(()=>finish(Error('缩略图读取超时')),5000);const finish=error=>{clearTimeout(timer);this.reader.removeEventListener('loadeddata',ready);this.reader.removeEventListener('error',fail);error?reject(error):resolve();};const ready=()=>finish(),fail=()=>finish(Error('缩略图不可用'));this.reader.addEventListener('loadeddata',ready,{once:true});this.reader.addEventListener('error',fail,{once:true});});if(epoch!==this.sourceEpoch)throw Error('source changed');}
  async thumbnails(){
    if(this.thumbBusy){this.thumbAgain=true;return;}
    const epoch=this.sourceEpoch;this.thumbBusy=true;
    try{
      const strip=this.$('thumbs'),left=Math.floor(this.scroll.scrollLeft/100)*100,right=Math.min(this.content.offsetWidth,left+this.scroll.clientWidth+200),nodes=[];
      for(let x=left;x<right;x+=100){const c=document.createElement('canvas');c.width=160;c.height=100;c.style.left=x+'px';c.dataset.frame=String(Math.min(this.count-1,Math.floor(x/this.pixels)));nodes.push(c);}strip.replaceChildren(...nodes);
      if(!this.isSynthetic()){if(!this.reader.getAttribute('src'))return;await this.readyReader(epoch);}
      for(const c of nodes){if(epoch!==this.sourceEpoch)return;const frame=Number(c.dataset.frame),ctx=c.getContext('2d'),cached=this.thumbnailCache.get(frame);
        if(cached){ctx.drawImage(cached,0,0);continue;}
        if(this.isSynthetic()){ctx.fillStyle=['#367eff','#2dd857','#b747dc'][Math.floor(frame/30)%3];ctx.fillRect(0,0,160,100);ctx.fillStyle='#fff';ctx.font='18px system-ui';ctx.fillText(this.timecode(frame),8,58);}
        else{const time=Math.min(this.getDuration()-.001,(frame+.2)/this.fps);if(Math.abs(this.reader.currentTime-time)>.00001){await new Promise((resolve,reject)=>{const timer=setTimeout(()=>finish(Error('seek timeout')),5000);const finish=error=>{clearTimeout(timer);this.reader.removeEventListener('seeked',ready);this.reader.removeEventListener('error',fail);error?reject(error):resolve();};const ready=()=>finish(),fail=()=>finish(Error('seek error'));this.reader.addEventListener('seeked',ready,{once:true});this.reader.addEventListener('error',fail,{once:true});this.reader.currentTime=time;});}if(epoch!==this.sourceEpoch)return;ctx.drawImage(this.reader,0,0,160,100);}
        const copy=document.createElement('canvas');copy.width=160;copy.height=100;copy.getContext('2d').drawImage(c,0,0);this.thumbnailCache.set(frame,copy);if(this.thumbnailCache.size>120)this.thumbnailCache.delete(this.thumbnailCache.keys().next().value);
      }
    }catch(error){/* Thumbnail availability does not block editing or accurate-frame requests. */}
    finally{this.thumbBusy=false;if(this.thumbAgain){this.thumbAgain=false;this.scheduleThumbnails();}}
  }
}
