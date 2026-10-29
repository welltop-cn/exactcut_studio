"""Resource retry scheduler, tested with browser fake timers and synthetic responses."""
from pathlib import Path
import pytest


@pytest.fixture
def page():
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.clock.install()
        page.clock.pause_at(page.evaluate("Date.now()"))
        page.add_script_tag(content=(Path(__file__).parents[1]/'exactcut/web/editor-busy.js').read_text())
        yield page
        browser.close()


def test_bounded_retry_and_header(page):
    page.evaluate('''() => {Math.random=()=>0; window.calls=0; window.done=false;
      EditorBusy.run(async()=>{calls++;throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',resource:'compute',retryable:true,retryAfterMs:1000})}).catch(()=>done=true);
    }''')
    for delay, count in [(999,1),(1,2),(2000,3),(4000,4)]:
        page.clock.run_for(delay)
        assert page.evaluate('calls') == count
    assert page.evaluate('done')


def test_non_busy_and_cancellation(page):
    page.evaluate('''() => {window.calls=0;window.cancel=new AbortController();
      EditorBusy.run(async()=>{calls++;throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',resource:'compute',retryable:true})},{signal:cancel.signal}).catch(()=>{});
    }''')
    page.evaluate('cancel.abort()')
    page.clock.run_for(20000)
    assert page.evaluate('calls') == 1
    for status in [409, 503, 429]:
        assert page.evaluate('''async status=>{let calls=0;try{await EditorBusy.run(async()=>{calls++;throw Object.assign(Error('no'),{status})})}catch{}return calls}''',status)==1


def test_scheduling_budget_does_not_abort_accepted_open(page):
    page.evaluate('''() => {Math.random=()=>0;window.calls=0;window.result=null;
      EditorBusy.run(async()=>{if(++calls===1)throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',resource:'compute',retryable:true});return new Promise(r=>setTimeout(()=>r('ready'),20000))}).then(v=>result=v);
    }''')
    page.clock.run_for(16000)
    assert page.evaluate('calls') == 2
    page.clock.run_for(5000)
    assert page.evaluate('result') == 'ready'


def test_retry_after_budget_frame_limit_and_unknown_resource(page):
    page.evaluate('''() => {window.calls=0;window.done=false;
      EditorBusy.run(async()=>{calls++;throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',retryable:true,resource:'frame',retryAfterMs:15000})}).catch(()=>done=true);
    }''')
    assert page.evaluate('done && calls===1')
    assert page.evaluate('''async()=>{let calls=0;try{await EditorBusy.run(async()=>{calls++;throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',retryable:true,resource:'asset'})})}catch{}return calls}''') == 1
    page.evaluate('''() => {Math.random=()=>0;calls=0;done=false;
      EditorBusy.run(async()=>{calls++;throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',retryable:true,resource:'frame',retryAfterMs:3000})},{maxRetries:1}).catch(()=>done=true);
    }''')
    page.clock.run_for(2999)
    assert page.evaluate('calls') == 1
    page.clock.run_for(1)
    assert page.evaluate('done && calls===2')


def test_error_parser_retains_legacy_message_and_contract(page):
    assert page.evaluate('''()=>{const e=EditorBusy.error(new Response('',{status:429,headers:{'Retry-After':'2'}}),{error:'legacy',code:'editor_resource_busy',retryable:true,resource:'scan',retry_after_ms:1000});return [e.message,e.status,e.code,e.retryable,e.resource,e.retryAfterMs]}''') == ['legacy',429,'editor_resource_busy',True,'scan',2000]


def test_api_preserves_busy_fields_and_external_abort(page):
    app=(Path(__file__).parents[1]/'exactcut/web/app.js').read_text().split('function positionToast()')[0]
    page.add_script_tag(content=app)
    assert page.evaluate('''async()=>{window.fetch=async()=>new Response(JSON.stringify({error:'busy',code:'editor_resource_busy',resource:'compute',retryable:true,retry_after_ms:1000}),{status:429,headers:{'Retry-After':'1'}});try{await api('https://fixture.test/api')}catch(e){return [e.message,e.status,e.code,e.retryable,e.retryAfterMs]}}''') == ['busy',429,'editor_resource_busy',True,1000]
    assert page.evaluate('''async()=>{const controller=new AbortController();window.fetch=async(url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('cancel','AbortError'))));const pending=api('https://fixture.test/api',undefined,{signal:controller.signal});controller.abort();try{await pending}catch(e){return e.name}}''') == 'AbortError'


def test_wait_callback_abort_and_diagnostic_failure_are_isolated(page):
    assert page.evaluate('''async()=>{const controller=new AbortController();window.reportEditorRetry=()=>{throw Error('diagnostics')};let calls=0;try{await EditorBusy.run(async()=>{calls++;throw Object.assign(Error('busy'),{status:429,code:'editor_resource_busy',retryable:true,resource:'scan'})},{signal:controller.signal,onWait:()=>controller.abort()})}catch(e){return [calls,e.name]}}''') == [1,'AbortError']


def test_api_body_abort_preserves_external_cancellation(page):
    app=(Path(__file__).parents[1]/'exactcut/web/app.js').read_text().split('function positionToast()')[0]
    page.add_script_tag(content=app)
    assert page.evaluate('''async()=>{const controller=new AbortController();window.fetch=async(url,{signal})=>new Response(new ReadableStream({start(stream){signal.addEventListener('abort',()=>stream.error(new DOMException('cancel','AbortError')))}}),{status:202});const pending=api('https://fixture.test/scan',{}, {signal:controller.signal});await Promise.resolve();controller.abort();try{await pending}catch(e){return [e.name,!!e.outcomeUnknown]}}''') == ['AbortError',False]
