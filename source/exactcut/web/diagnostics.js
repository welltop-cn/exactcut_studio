/* Bounded metadata-only reports. Never send form values, bodies, URLs or cookies. */
(() => {
  const originalFetch = window.fetch.bind(window);
  const base = new URL(document.baseURI);
  let sent = 0;
  const report = data => {
    if (sent >= 20) return;
    sent++;
    originalFetch(new URL('api/client-events', base), {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Exactcut-Request': '1'},
      body: JSON.stringify(data), keepalive: true
    }).catch(() => {});
  };
  const name = value => /^[a-zA-Z0-9_.-]{1,100}$/.test(value || '') ? value : 'unknown';
  window.addEventListener('error', event => {
    let source = 'unknown';
    try { source = name(new URL(event.filename).pathname.split('/').pop()); } catch (_) {}
    report({event: 'page_error', source, line: event.lineno || 0, column: event.colno || 0,
            error_type: name(event.error?.name)});
  });
  window.addEventListener('unhandledrejection', event => {
    report({event: 'unhandled_rejection', error_type: name(event.reason?.name)});
  });
  document.addEventListener('click', event => {
    const id = event.target.closest('button')?.id;
    if (['startNow', 'newBatch', 'saveDraft', 'uploadButton', 'confirmPublish'].includes(id)) {
      report({event: 'action', action: id});
    }
  }, true);
  window.fetch = async (...args) => {
    let observed = false;
    try {
      const url = new URL(typeof args[0] === 'string' ? args[0] : args[0].url || args[0], document.baseURI);
      observed = url.origin === base.origin && url.pathname.startsWith(new URL('api/', base).pathname);
    } catch (_) {}
    const start = performance.now();
    try {
      const response = await originalFetch(...args);
      const duration = Math.round(performance.now() - start);
      if (observed && (!response.ok || duration >= 5000)) {
        report({event: 'slow_request', status: response.status, duration_ms: duration,
                http_trace_id: name(response.headers.get('X-Request-ID'))});
      }
      return response;
    } catch (error) {
      if (observed) report({event: 'network_error', duration_ms: Math.round(performance.now()-start), error_type: name(error?.name)});
      throw error;
    }
  };
})();
