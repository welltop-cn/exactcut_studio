(() => {
  const style=document.createElement('style');
  style.textContent=`#helpLaunch{position:fixed;right:24px;bottom:24px;z-index:35;box-shadow:0 4px 20px #12333c25;border-radius:28px;padding:13px 20px;font-size:15px}#productionHelp{position:fixed;right:24px;bottom:88px;width:min(430px,calc(100vw - 32px));height:min(620px,calc(100dvh - 112px));z-index:36;background:white;border:1px solid #d5e2e4;border-radius:18px;box-shadow:0 12px 48px #12333c30;display:flex;flex-direction:column;overflow:hidden;color:#17333e;font-size:14px}#productionHelp[hidden]{display:none}#productionHelp *{box-sizing:border-box}#productionHelp .help-head{padding:16px;border-bottom:1px solid #e2eaec;display:flex;align-items:center;justify-content:space-between;gap:8px}#productionHelp h2{margin:0;font-size:18px}#productionHelp button{font-size:13px;padding:8px 12px;min-height:36px}#helpContext{margin:6px 0 0;font-size:12px;color:#617a85;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#helpMessages{overflow-y:auto;flex:1;padding:16px;min-height:80px}#productionHelp .help-message{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;padding:11px 13px;margin-bottom:12px;border-radius:12px;background:#f1f6f6}#productionHelp .help-user{margin-left:32px;background:#e2f2ee}#productionHelp .help-source{font-size:11px;color:#617a85;margin-top:8px}#helpSuggestions{display:flex;flex-wrap:wrap;gap:6px;padding:0 16px 12px}#helpForm{padding:12px 16px 16px;border-top:1px solid #e2eaec}#helpQuestion{width:100%;min-height:70px;max-height:140px;margin:0;font-size:14px;resize:vertical;padding:10px}#productionHelp .help-foot{display:flex;justify-content:space-between;align-items:center;margin-top:8px;gap:8px}#productionHelp .help-foot small{font-size:11px;color:#617a85}#helpPending{padding:0 16px 8px;color:#087f76;font-size:13px}@media(max-width:600px){#helpLaunch{right:16px;bottom:16px}#productionHelp{right:16px;bottom:76px;height:calc(100dvh - 100px)}}`;
  style.textContent+=`#helpLaunch{width:100px;height:132px;padding:0;border:0;border-radius:20px;background:transparent;color:#17665f;box-shadow:none;cursor:grab;touch-action:none;user-select:none;display:flex;flex-direction:column;align-items:center;gap:0}#helpLaunch svg{width:92px;height:103px;filter:drop-shadow(0 4px 5px #12333c15);pointer-events:none}#helpLaunch span{font-size:12px;white-space:nowrap;padding:4px 9px;border-radius:14px;background:#fff;border:1px solid #cde5df;box-shadow:0 2px 8px #12333c10;pointer-events:none}#helpLaunch:hover svg{filter:drop-shadow(0 5px 7px #087f7633)}#helpLaunch:focus-visible{outline:3px solid #168c81;outline-offset:4px}#helpLaunch.dragging{cursor:grabbing}#helpLaunch.dragging svg{transform:rotate(-6deg)}`;
  document.head.append(style);
  const root=document.createElement('div');
  root.innerHTML=`<button id="helpLaunch" aria-expanded="false" aria-controls="productionHelp"><svg viewBox="0 0 100 108" aria-hidden="true"><defs><linearGradient id="robotShell" x2="0" y2="1"><stop stop-color="#ffffff"/><stop offset="1" stop-color="#ccece7"/></linearGradient></defs><ellipse cx="50" cy="101" rx="28" ry="5" fill="#12333c" opacity=".13"/><path d="M50 20V11" stroke="#168c81" stroke-width="4"/><circle cx="50" cy="8" r="6" fill="#ffc65c"/><rect x="8" y="36" width="12" height="23" rx="6" fill="#13978b"/><rect x="80" y="36" width="12" height="23" rx="6" fill="#13978b"/><rect x="17" y="19" width="66" height="53" rx="22" fill="url(#robotShell)" stroke="#168c81" stroke-width="2"/><rect x="25" y="30" width="50" height="30" rx="13" fill="#173e47"/><path d="M34 43q4-7 8 0m16 0q4-7 8 0" fill="none" stroke="#9bf3da" stroke-width="3.5" stroke-linecap="round"/><path d="M45 50q5 6 10 0" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/><ellipse cx="30" cy="53" rx="4" ry="2" fill="#f0b1ab"/><ellipse cx="70" cy="53" rx="4" ry="2" fill="#f0b1ab"/><rect x="32" y="72" width="36" height="22" rx="10" fill="url(#robotShell)" stroke="#168c81" stroke-width="2"/><path d="M44 80h12M50 77v6" stroke="#168c81" stroke-width="3" stroke-linecap="round"/><path d="M31 77l-9 7m47-7 9 7M41 94v4m18-4v4" stroke="#168c81" stroke-width="6" stroke-linecap="round"/></svg><span>制作小助手</span></button><section id="productionHelp" role="dialog" aria-label="制作小助手" hidden><div class="help-head"><div><h2>制作小助手</h2><p id="helpContext"></p></div><div><button id="helpClear" class="quiet">清空</button><button id="helpClose" class="quiet" aria-label="关闭制作小助手">关闭</button></div></div><div id="helpMessages" role="log" aria-live="polite"></div><div id="helpPending" role="status" hidden>正在查阅制作说明与批次状态…</div><div id="helpSuggestions"></div><form id="helpForm"><label class="sr-only" for="helpQuestion">制作问题</label><textarea id="helpQuestion" maxlength="1600" placeholder="例如：本地已有正片，还需要下载吗？" required></textarea><div class="help-foot"><small>仅解答本项目制作 · 不代替你执行操作</small><button id="helpSend" type="submit">发送</button></div></form></section>`;
  document.body.append(root);
  const el=id=>document.getElementById(id), chats=new Map();
  let activeKey='';
  const batch=()=>typeof current!=='undefined'&&current&&!document.getElementById('workspace').hidden?selectedId:null;
  function chat(){const key=batch()||'';if(!chats.has(key))chats.set(key,{id:randomId(),messages:[],pending:false,draft:''});activeKey=key;return chats.get(key)}
  function draw(){const c=chat();el('helpContext').textContent=activeKey?`当前剧：${current.draft.drama_id} · ${current.draft.drama_title||'制作批次'}`:'制作流程通用问答';el('helpMessages').replaceChildren();
    const messages=c.messages.length?c.messages:[{answer:'你好，我可以解释 Excel 整理、时间线、正片准备、制作预览和 FBBM 上传。打开具体批次后，也可以问我当前进度。'}];
    for(const m of messages){const row=document.createElement('div');row.className='help-message'+(m.question?' help-user':'');row.textContent=m.question||m.answer;if(m.sources?.length){const source=document.createElement('div');source.className='help-source';source.textContent='依据：'+m.sources.map(s=>s.title).join('、');row.append(source)}el('helpMessages').append(row)}
    el('helpPending').hidden=!c.pending;el('helpSend').disabled=c.pending;el('helpQuestion').value=c.draft;el('helpSuggestions').hidden=c.messages.length>0;el('helpMessages').scrollTop=el('helpMessages').scrollHeight;
  }
  const launcher=el('helpLaunch'), panel=el('productionHelp');
  let drag=null, suppressClick=false;
  function place(x,y){
    const margin=8, w=launcher.offsetWidth,h=launcher.offsetHeight;
    x=Math.max(margin,Math.min(x,innerWidth-w-margin));
    y=Math.max(margin,Math.min(y,innerHeight-h-margin));
    Object.assign(launcher.style,{left:x+'px',top:y+'px',right:'auto',bottom:'auto'});
    positionPanel();return {x,y};
  }
  function positionPanel(){
    if(panel.hidden)return;
    const r=launcher.getBoundingClientRect(), w=panel.offsetWidth,h=panel.offsetHeight;
    const x=r.left>w+16?r.left-w-12:r.right+12+w<innerWidth?r.right+12:Math.max(8,innerWidth-w-8);
    const y=Math.max(8,Math.min(r.bottom-h,innerHeight-h-8));
    Object.assign(panel.style,{left:x+'px',top:y+'px',right:'auto',bottom:'auto'});
  }
  try{const pos=JSON.parse(localStorage.getItem('exactcut-helper-position'));if(pos&&Number.isFinite(pos.x)&&Number.isFinite(pos.y))place(pos.x,pos.y)}catch{}
  launcher.title='点击打开制作小助手 · 拖动可移动位置';
  launcher.setAttribute('aria-label','制作小助手（可拖动）');
  launcher.addEventListener('pointerdown',e=>{
    if(e.button!==0)return;
    suppressClick=false;const r=launcher.getBoundingClientRect();
    drag={id:e.pointerId,x:e.clientX,y:e.clientY,left:r.left,top:r.top,moved:false};
    launcher.setPointerCapture(e.pointerId);
  });
  launcher.addEventListener('pointermove',e=>{
    if(!drag||drag.id!==e.pointerId)return;
    const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
    if(Math.hypot(dx,dy)>5)drag.moved=true;
    if(drag.moved){launcher.classList.add('dragging');place(drag.left+dx,drag.top+dy)}
  });
  function endDrag(e){
    if(!drag||drag.id!==e.pointerId)return;
    suppressClick=drag.moved;
    if(drag.moved){const r=launcher.getBoundingClientRect();try{localStorage.setItem('exactcut-helper-position',JSON.stringify({x:r.left,y:r.top}))}catch{}}
    drag=null;launcher.classList.remove('dragging');
    if(launcher.hasPointerCapture(e.pointerId))launcher.releasePointerCapture(e.pointerId);
  }
  launcher.addEventListener('pointerup',endDrag);
  launcher.addEventListener('pointercancel',endDrag);
  window.addEventListener('resize',()=>{const r=launcher.getBoundingClientRect();place(r.left,r.top)});
  function close(){el('productionHelp').hidden=true;el('helpLaunch').setAttribute('aria-expanded','false');el('helpLaunch').focus()}
  el('helpLaunch').onclick=(event)=>{if(suppressClick&&event.detail!==0){suppressClick=false;return} if(!el('productionHelp').hidden)return close();el('productionHelp').hidden=false;positionPanel();el('helpLaunch').setAttribute('aria-expanded','true');draw();el('helpQuestion').focus()};
  el('helpClose').onclick=close;el('helpClear').onclick=()=>{chats.delete(batch()||'');draw();el('helpQuestion').focus()};
  el('productionHelp').addEventListener('keydown',e=>{if(e.key==='Escape'){e.stopPropagation();close()}});
  el('helpQuestion').addEventListener('input',()=>chat().draft=el('helpQuestion').value);
  for(const text of ['当前进行到哪一步？','本地有正片还要下载吗？','时间线怎么填写？','如何上传 FBBM？']){const button=document.createElement('button');button.className='secondary';button.textContent=text;button.onclick=()=>{chat().draft=text;el('helpQuestion').value=text;el('helpForm').requestSubmit()};el('helpSuggestions').append(button)}
  el('helpForm').onsubmit=async e=>{e.preventDefault();const c=chat(),key=activeKey,question=el('helpQuestion').value.trim();if(!question||c.pending)return;c.messages.push({question});c.draft='';c.pending=true;draw();try{const result=await api('/api/help',{question,batch_id:key||null,conversation:c.id});c.messages.push(result)}catch(error){c.messages.push({answer:'未能取得回答：'+error.message+'。请稍后重试。'})}finally{c.pending=false;if(!el('productionHelp').hidden&&activeKey===key)draw()}};
  setInterval(()=>{if(!el('productionHelp').hidden&&(batch()||'')!==activeKey)draw()},800);
})();
