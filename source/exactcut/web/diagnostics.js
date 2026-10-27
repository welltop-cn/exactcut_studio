/* Bounded metadata-only reports. Never send form values, bodies, URLs or cookies. */
(() => {
  const originalFetch = window.fetch.bind(window), base = new URL(document.baseURI);
  const name = value => /^[a-zA-Z0-9_.-]{1,100}$/.test(value || '') ? value : 'unknown';
  const identifier = () => {
    if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
      return Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
    }
    return Array.from({length: 32}, () => Math.floor(Math.random() * 16).toString(16)).join('');
  };
  const page = identifier();
  let build = 'unknown';
  try { build = name(document.currentScript.dataset?.appVersion || new URL(document.currentScript.src).searchParams.get('v')); } catch (_) {}
  let budgetAt = Date.now(), routine = 0, failures = 0;
  const report = data => {
    try {
      if (Date.now() - budgetAt >= 60000) { budgetAt = Date.now(); routine = 0; failures = 0; }
      const failure = /error|rejection/.test(data.event) || data.status >= 400;
      if (failure ? failures >= 40 : routine >= 40) return;
      if (failure) failures++; else routine++;
      originalFetch(new URL('api/client-events', base), {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Exactcut-Request': '1'},
        body: JSON.stringify({page_id: page, frontend_build: build, ...data}), keepalive: true
      }).catch(() => {});
    } catch (_) { /* Diagnostics must not change page behavior. */ }
  };
  const selectedBatch = () => {
    const value = document.querySelector?.('[data-batch].selected')?.dataset.batch;
    return /^[a-f0-9]{32}$/.test(value || '') ? {batch_id: value} : {};
  };
  const listen = (target, event, handler, capture = false) => target.addEventListener(event, value => {
    try { handler(value); } catch (_) {}
  }, capture);
  listen(window, 'error', event => {
    let source = 'unknown';
    try { source = name(new URL(event.filename).pathname.split('/').pop()); } catch (_) {}
    report({event: 'page_error', ...selectedBatch(), source, line: event.lineno || 0,
            column: event.colno || 0, error_type: name(event.error?.name)});
  });
  listen(window, 'unhandledrejection', event => report({event: 'unhandled_rejection',
    ...selectedBatch(), error_type: name(event.reason?.name)}));
  const buttons = new Set(['newBatch', 'saveButton', 'confirmButton', 'startNow', 'cloneButton', 'pauseButton',
    'resumeButton', 'archiveButton', 'unarchiveButton', 'retryButton', 'previewButton', 'reworkButton', 'cleanupCache']);
  listen(document, 'click', event => {
    const button = event.target.closest?.('button');
    if (!button) return;
    const data = button.dataset || {};
    let action = buttons.has(button.id) ? button.id : null;
    for (const [key, label] of Object.entries({hookPreview: 'hook_preview', publish: 'publish',
      sendFeedback: 'feedback', batch: 'open_project', batchAction: 'project_action', tab: 'switch_tab'})) {
      if (data[key] !== undefined) { action = label; break; }
    }
    if (!action) return;
    const fields = {event: 'action', action, ...selectedBatch(), disabled: Number(!!button.disabled)};
    const batch = data.batch || data.id;
    if (/^[a-f0-9]{32}$/.test(batch || '')) fields.batch_id = batch;
    if (/^\d{1,4}$/.test(data.row || '')) fields.row_index = Number(data.row);
    report(fields);
  }, true);
  const mediaFields = video => ({
    source: name(video.id), ready_state: video.readyState, network_state: video.networkState,
    media_error: video.error?.code || 0, paused: Number(!!video.paused),
    seeking: Number(!!video.seeking), ended: Number(!!video.ended),
    current_ms: Number.isFinite(video.currentTime) ? Math.max(0, Math.round(video.currentTime * 1000)) : 0,
    visibility: name(document.visibilityState)
  });
  // Explicit context belongs to the play attempt, even if the editor was reopened.
  window.reportMediaPlayFailure = (video, error, context) => {
    try {
      if (!video || !context) return;
      report({event: 'media_play_rejection', ...mediaFields(video), error_type: name(error?.name),
        batch_id: name(context.batch), request_id: name(context.asset.request_id),
        session_id: name(context.session.id)});
    } catch (_) { /* Diagnostics must not change playback behavior. */ }
  };
  for (const type of ['error', 'stalled', 'waiting', 'playing']) {
    listen(document, type, event => {
      const video = event.target;
      if (video?.tagName !== 'VIDEO' || !video.dataset?.previewRequest) return;
      report({event: 'media_' + type, ...selectedBatch(), ...mediaFields(video),
        request_id: name(video.dataset.previewRequest),
        ...(video.dataset.previewBatch ? {batch_id: name(video.dataset.previewBatch)} : {}),
        ...(video.dataset.previewSession ? {session_id: name(video.dataset.previewSession)} : {})});
    }, true);
  }
  listen(document, 'visibilitychange', () => report({event: 'visibility', ...selectedBatch(), visibility: name(document.visibilityState)}));
  window.fetch = async (...args) => {
    let context = null;
    try {
      const input = args[0], options = args[1] || {};
      const url = new URL(typeof input === 'string' ? input : input.url || input, document.baseURI);
      const prefix = new URL('api/', base).pathname;
      if (url.origin === base.origin && url.pathname.startsWith(prefix) && url.pathname !== prefix + 'client-events') {
        const path = url.pathname.slice(prefix.length), batch = path.match(/^batches\/([a-f0-9]{32})(?:\/|$)/);
        const parts = path.split('/');
        const route = batch ? 'batches.' + (parts[2] || 'detail') : parts[0];
        context = {route: name(route), method: name(String(options.method || input.method || 'GET').toUpperCase()),
          ...(batch ? {batch_id: batch[1]} : {}), client_request_id: identifier()};
        if (typeof Headers !== 'undefined') {
          const headers = new Headers(options.headers === undefined ? input.headers : options.headers);
          headers.set('X-Client-Request-ID', context.client_request_id);
          args = [input, {...options, headers}];
        }
      }
    } catch (_) { context = null; }
    const start = performance.now();
    if (context && context.method !== 'GET') report({event: 'request_started', ...context});
    try {
      const response = await originalFetch(...args), duration = Math.round(performance.now() - start);
      try {
        if (context && (!response.ok || duration >= 5000 || context.method !== 'GET')) {
          report({event: response.ok && duration < 5000 ? 'request_completed' : 'slow_request', ...context,
            status: response.status, duration_ms: duration, http_trace_id: name(response.headers.get('X-Request-ID'))});
        }
      } catch (_) {}
      return response;
    } catch (error) {
      if (context) report({event: 'network_error', ...context,
        duration_ms: Math.round(performance.now() - start), error_type: name(error?.name)});
      throw error;
    }
  };
  report({event: 'page_ready', visibility: name(document.visibilityState)});
})();
