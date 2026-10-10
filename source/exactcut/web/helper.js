(() => {
  const style = document.createElement('style');
  style.textContent = `
.helper-nav-btn{background:#164352;color:#a8e1d7;border:1px solid #286477;border-radius:20px;padding:6px 14px;font-size:13px;display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-weight:600;transition:all .15s}
.helper-nav-btn:hover{background:#1d5568;color:#fff;border-color:#38889f}
#productionHelp{position:fixed;top:0;right:0;width:min(440px,calc(100vw - 20px));height:100vh;height:100dvh;z-index:100;background:white;border-left:1px solid #d5e2e4;box-shadow:-12px 0 40px rgba(18,51,60,0.22);display:flex;flex-direction:column;color:#17333e;font-size:14px}
#productionHelp[hidden]{display:none}
#productionHelp *{box-sizing:border-box}
#productionHelp .help-head{padding:16px 20px;border-bottom:1px solid #e2eaec;display:flex;align-items:center;justify-content:space-between;gap:8px;background:#f8fbfb}
#productionHelp h2{margin:0;font-size:18px}
#productionHelp button{font-size:13px;padding:8px 12px;min-height:36px}
#helpContext{margin:6px 0 0;font-size:12px;color:#617a85;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#helpMessages{overflow-y:auto;flex:1;padding:16px 20px;min-height:80px}
#productionHelp .help-message{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;padding:11px 14px;margin-bottom:12px;border-radius:12px;background:#f1f6f6}
#productionHelp .help-user{margin-left:32px;background:#e2f2ee}
#productionHelp .help-source{font-size:11px;color:#617a85;margin-top:8px}
#helpSuggestions{display:flex;flex-wrap:wrap;gap:6px;padding:0 20px 12px}
#helpForm{padding:12px 20px 18px;border-top:1px solid #e2eaec;background:#fafcfc}
#helpQuestion{width:100%;min-height:70px;max-height:140px;margin:0;font-size:14px;resize:vertical;padding:10px}
#productionHelp .help-foot{display:flex;justify-content:space-between;align-items:center;margin-top:8px;gap:8px}
#productionHelp .help-foot small{font-size:11px;color:#617a85}
#helpPending{padding:0 20px 8px;color:#087f76;font-size:13px}
`;
  document.head.append(style);
  const root = document.createElement('div');
  root.innerHTML = `<section id="productionHelp" role="dialog" aria-label="制作小助手" hidden><div class="help-head"><div><h2>制作小助手</h2><p id="helpContext"></p></div><div><button id="helpClear" class="quiet">清空</button><button id="helpClose" class="quiet" aria-label="关闭制作小助手">✕</button></div></div><div id="helpMessages" role="log" aria-live="polite"></div><div id="helpPending" role="status" hidden>正在查阅制作说明与批次状态…</div><div id="helpSuggestions"></div><form id="helpForm"><label class="sr-only" for="helpQuestion">制作问题</label><textarea id="helpQuestion" maxlength="1600" placeholder="例如：本地已有正片，还需要下载吗？" required></textarea><div class="help-foot"><small>仅解答本项目制作 · 不代替你执行操作</small><button id="helpSend" type="submit">发送</button></div></form></section>`;
  document.body.append(root);
  const el = id => document.getElementById(id), chats = new Map();
  let activeKey = '';
  const batch = () => typeof current !== 'undefined' && current && !document.getElementById('workspace').hidden ? selectedId : null;
  function chat() { const key = batch() || ''; if (!chats.has(key)) chats.set(key, { id: randomId(), messages: [], pending: false, draft: '' }); activeKey = key; return chats.get(key) }
  function draw() {
    const c = chat(); el('helpContext').textContent = activeKey ? `当前剧：${current.draft.drama_id} · ${current.draft.drama_title || '制作批次'}` : '制作流程通用问答'; el('helpMessages').replaceChildren();
    const messages = c.messages.length ? c.messages : [{ answer: '你好，我可以解释 Excel 整理、时间线、正片准备、制作预览和 FBBM 上传。打开具体批次后，也可以问我当前进度。' }];
    for (const m of messages) { const row = document.createElement('div'); row.className = 'help-message' + (m.question ? ' help-user' : ''); row.textContent = m.question || m.answer; if (m.sources?.length) { const source = document.createElement('div'); source.className = 'help-source'; source.textContent = '依据：' + m.sources.map(s => s.title).join('、'); row.append(source) } el('helpMessages').append(row) }
    el('helpPending').hidden = !c.pending; el('helpSend').disabled = c.pending; el('helpQuestion').value = c.draft; el('helpSuggestions').hidden = c.messages.length > 0; el('helpMessages').scrollTop = el('helpMessages').scrollHeight;
  }
  function close() { el('productionHelp').hidden = true; }
  function toggle() {
    if (!el('productionHelp').hidden) return close();
    el('productionHelp').hidden = false;
    draw();
    el('helpQuestion').focus();
  }
  const launcher = el('helpNav') || el('helpLaunch');
  if (launcher) launcher.onclick = toggle;
  el('helpClose').onclick = close;
  el('helpClear').onclick = () => { chats.delete(batch() || ''); draw(); el('helpQuestion').focus() };
  el('productionHelp').addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); close() } });
  document.addEventListener('pointerdown', e => {
    if (!el('productionHelp').hidden && !el('productionHelp').contains(e.target) && (!launcher || !launcher.contains(e.target))) {
      close();
    }
  });
  el('helpQuestion').addEventListener('input', () => chat().draft = el('helpQuestion').value);
  for (const text of ['当前进行到哪一步？', '本地有正片还要下载吗？', '时间线怎么填写？', '如何上传 FBBM？']) {
    const button = document.createElement('button'); button.className = 'secondary'; button.textContent = text;
    button.onclick = () => { chat().draft = text; el('helpQuestion').value = text; el('helpForm').requestSubmit() };
    el('helpSuggestions').append(button);
  }
  el('helpForm').onsubmit = async e => {
    e.preventDefault(); const c = chat(), key = activeKey, question = el('helpQuestion').value.trim();
    if (!question || c.pending) return;
    c.messages.push({ question }); c.draft = ''; c.pending = true; draw();
    try {
      const result = await api('/api/help', { question, batch_id: key || null, conversation: c.id });
      c.messages.push(result);
    } catch (error) {
      c.messages.push({ answer: '未能取得回答：' + error.message + '。请稍后重试。' });
    } finally {
      c.pending = false; if (!el('productionHelp').hidden && activeKey === key) draw();
    }
  };
  setInterval(() => { if (!el('productionHelp').hidden && (batch() || '') !== activeKey) draw() }, 800);
})();
