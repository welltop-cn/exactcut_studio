/* A session owns one source decision; each video owns its playback intent. */
(() => {
  function create({endpoint,local,enabled=false,timeout=3000}) {
    let choice=null,pending=null,serial=0,closed=false,localOnly=!enabled,denied=false;
    let resigned=false,fellBack=false;
    const bindings=new Set();
    const localChoice=()=>({source:'local',url:appUrl(local),expiresAt:Infinity});
    function fallback(){
      const switched=choice?.source==='oss'||choice?.switched===true;
      serial++;pending=null;localOnly=true;fellBack=true;choice={...localChoice(),switched};return choice;
    }
    async function resolve(renew=false){
      if(closed)return null;
      if(denied)throw Error('播放访问已失效，请关闭后重新打开');
      if(localOnly)return choice||(choice=localChoice());
      if(pending)return pending;
      if(choice&&!renew)return choice;
      const request=++serial,started=Date.now(),controller=new AbortController();
      let timer;
      const work=(async()=>{
        try{
          const data=await Promise.race([
            (async()=>{
              const response=await fetch(appUrl(endpoint),{signal:controller.signal,cache:'no-store'});
              if(!response.ok){
                if([401,403,404,409].includes(response.status)){
                  if(request===serial&&!closed)denied=true;
                  throw Error('播放访问已失效，请关闭后重新打开');
                }
                throw Error('source unavailable');
              }
              return response.json();
            })(),
            new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('timeout'));},timeout);})
          ]);
          if(!['oss','local'].includes(data.source)||typeof data.url!=='string'||!Number.isFinite(data.expires_in))throw Error('invalid source');
          if(closed||request!==serial)return choice;
          if(data.source==='local')return fallback();
          choice={source:'oss',url:data.url,expiresAt:started+data.expires_in*1000};return choice;
        }catch(error){
          if(closed||request!==serial)return choice;
          if(denied)throw error;
          return fallback();
        }finally{clearTimeout(timer);if(request===serial)pending=null;}
      })();
      pending=work;
      return work;
    }
    function attach(video,options={}){
      let generation=0,disposed=false,intent=false,loading=false,recovering=false,failed=false,nativePlayDuringLoad=false;
      let target=null,restore=null,lastTime=video.currentTime,lastProgress=Date.now(),seekingAt=0;
      const valid=()=>!closed&&!disposed&&(options.valid?.()??true);
      const panel=document.createElement('div'),message=document.createElement('span');
      const switchButton=document.createElement('button'),retry=document.createElement('button');
      switchButton.type=retry.type='button';switchButton.textContent='切换服务器';retry.textContent='重试播放';
      panel.className='editor-playback-status';panel.hidden=true;
      panel.append(message,switchButton,retry);options.anchor?.after(panel);
      function show(text,{switchable=false,retryable=false}={}){
        message.textContent=text;switchButton.hidden=!switchable;retry.hidden=!retryable;panel.hidden=!text;
      }
      function cancel(){generation++;restore=loading?{generation,time:target??video.currentTime,play:false}:null;recovering=false;seekingAt=0;lastProgress=Date.now();}
      function failure(error){
        if(!valid())return;
        failed=true;intent=false;video.pause();show(error.message,{retryable:!denied});options.failed?.(error);
      }
      function playRaw(){
        if(!valid())return;
        const attempt=generation;
        // Editors retain their existing playAttempt guards and failure UI.
        const result=options.play?options.play():video.play();
        result?.catch(error=>{
          if(attempt!==generation||!valid())return;
          intent=false;
          show(error.name==='NotAllowedError'?'点击播放以继续':'暂时无法播放，请重试',{retryable:error.name!=='NotAllowedError'});
        });
      }
      function finish(){
        if(!restore||!valid())return;
        const saved=restore;restore=null;
        if(saved.generation!==generation)return;
        if(Number.isFinite(saved.time)&&Number.isFinite(video.duration))video.currentTime=Math.max(0,Math.min(saved.time,video.duration));
        loading=false;target=null;
        if(saved.play&&intent)playRaw();
      }
      function apply(next,time,play,force=false){
        if(!next||!valid())return;
        target=time;
        show(next.source==='local'&&next.switched?'已切换服务器':'');
        if(!force&&video.getAttribute('src')===next.url){
          if(video.readyState<1){loading=true;restore={generation,time,play};return;}
          loading=false;
          if(Number.isFinite(time)&&Number.isFinite(video.duration))video.currentTime=time;
          target=null;if(play&&intent)playRaw();return;
        }
        options.beforeChange?.();loading=true;nativePlayDuringLoad=false;
        // load() resets playback; preserve only this video's latest user intent.
        restore={generation,time,play};
        const controls={volume:video.volume,muted:video.muted,playbackRate:video.playbackRate};
        video.src=next.url;video.load();
        Object.assign(video,controls);lastProgress=Date.now();
      }
      function use({play=false,time=target??video.currentTime,renew=false,force=false}={}){
        if(failed&&!force){failure(Error(denied?'播放访问已失效，请关闭后重新打开':'暂时无法播放，请重试'));return Promise.resolve();}
        const operation=++generation;restore=null;intent=play;target=time;lastProgress=Date.now();
        const fresh=choice&&(!renew||choice.source==='local')&&!denied;
        if(fresh){apply(choice,time,play,force);return Promise.resolve();}
        return resolve(renew).then(next=>{if(operation===generation&&valid())apply(next,time,play,force);})
          .catch(error=>{if(operation===generation)failure(error);});
      }
      function needsRenew(){return choice?.source==='oss'&&choice.expiresAt-Date.now()<60000;}
      async function recover(){
        if(!valid()||recovering||denied||failed)return;
        const operation=++generation,time=target??video.currentTime,wantsPlay=intent;
        restore=null;recovering=true;
        try{
          let next;
          if(choice?.source==='oss'&&!localOnly&&!resigned){resigned=true;next=await resolve(true);}
          else if(choice?.source==='oss'&&!fellBack)next=fallback();
          else {failure(Error('暂时无法播放，请重试'));return;}
          if(operation!==generation||!valid())return;
          apply(next,time,wantsPlay,true);
          if(next?.source==='local'){
            for(const peer of bindings)if(peer!==binding)peer.sync();
          }
        }catch(error){if(operation===generation)failure(error);}
        finally{if(operation===generation)recovering=false;}
      }
      const onPause=()=>{if(video.paused&&(!loading||nativePlayDuringLoad)){intent=false;cancel();}};
      const onPlay=()=>{
        if(video.paused)return;
        intent=true;nativePlayDuringLoad=loading;lastProgress=Date.now();
        // Native controls have already started playback. Prime a fresh signature,
        // but do not interrupt a healthy stream; use it on the next seek/error.
        if(!loading&&needsRenew()){
          const operation=generation;
          resolve(true).catch(error=>{if(operation===generation&&valid())failure(error);});
        }
      };
      const onPlaying=()=>{failed=false;show('');lastProgress=Date.now();};
      const onSeeking=()=>{
        // Native controls can seek while an asynchronous error recovery is pending.
        // Keep its new position and invalidate the old restore even without renewal.
        const resumeRecovery=recovering||!!pending;
        if(!loading){cancel();target=video.currentTime;}
        else if(video.readyState>=1&&target!==null&&Math.abs(video.currentTime-target)>.001){
          cancel();target=video.currentTime;restore={generation,time:target,play:intent};
        }
        seekingAt=Date.now();
        if(!loading&&(resumeRecovery||needsRenew()||choice?.url!==video.getAttribute('src')))
          use({time:video.currentTime,play:intent,renew:resumeRecovery||needsRenew(),force:resumeRecovery});
      };
      const onSeeked=()=>{seekingAt=0;if(!loading)target=null;lastProgress=Date.now();};
      const listeners={error:recover,loadedmetadata:finish,pause:onPause,play:onPlay,playing:onPlaying,seeking:onSeeking,seeked:onSeeked};
      for(const [name,fn] of Object.entries(listeners))video.addEventListener(name,fn);
      const clock=setInterval(()=>{
        if(!valid()||document.visibilityState!=='visible'){lastProgress=Date.now();if(seekingAt)seekingAt=Date.now();panel.hidden=true;return;}
        const now=Date.now();
        if(video.currentTime!==lastTime){lastTime=video.currentTime;lastProgress=now;if(!failed)show('');}
        if(choice?.source!=='oss'||denied)return;
        if(video.seeking||seekingAt){
          if(seekingAt&&now-seekingAt>=10000)show('定位中，可切换服务器继续',{switchable:true});
        }else if(intent&&now-lastProgress>=10000)show('视频加载较慢，可切换服务器继续',{switchable:true});
      },1000);
      switchButton.onclick=()=>{
        if(!valid()||choice?.source!=='oss'||denied)return;
        const time=target??video.currentTime,play=intent;cancel();choice=fallback();show('已切换服务器');
        apply(choice,time,play);for(const peer of bindings)if(peer!==binding)peer.sync();
      };
      retry.onclick=()=>{
        if(!valid()||denied)return;
        serial++;pending=null;choice=null;localOnly=!enabled;resigned=false;fellBack=false;failed=false;show('');
        use({play:true,force:true});
      };
      const binding={
        open:()=>use({time:target??video.currentTime}),
        play:()=>use({play:true,renew:needsRenew()}),
        pause(){intent=false;cancel();},
        rejected(){intent=false;},
        seek(time){const resumeRecovery=recovering||!!pending;intent=false;cancel();seekingAt=Date.now();return use({time,renew:resumeRecovery||needsRenew(),force:resumeRecovery});},
        sync(){if(valid())use({time:target??video.currentTime});},
        get changing(){return loading;},
        get wantsPlay(){return intent;},
        get position(){return target??video.currentTime;},
        dispose(){disposed=true;cancel();clearInterval(clock);panel.remove();for(const [name,fn] of Object.entries(listeners))video.removeEventListener(name,fn);bindings.delete(binding);}
      };
      bindings.add(binding);return binding;
    }
    return {attach,dispose(){closed=true;serial++;for(const binding of [...bindings])binding.dispose();}};
  }
  window.EditorSource={create};
})();
