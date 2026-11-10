"""Chromium DOM acceptance of the production publish/operation/API/refresh flow.

Network replies are controlled in-page; no remote delivery or HTTP worker runs.
"""
from pathlib import Path

import pytest

from browser_support import shared_playwright


@pytest.mark.ui
def test_unknown_receipt_survives_reload_and_fresh_tab_has_separate_storage():
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    api = next(line for line in source.splitlines() if line.startswith('const randomId=')) + source[source.index('function isRevisionConflict('):source.index('function positionToast(')]
    setup = r'''
        const id='b'.repeat(32);
        window.current={id,media_managed:true,revision:Number(new URL(location.href).searchParams.get('revision')||7)};
        window.batchCache=[];window.openingBatchRequest=1;window.pending=[];
        window.appUrl=x=>x;
        window.fetch=(path,options)=>new Promise((resolve,reject)=>pending.push({path,options,resolve,reject}));
        window.EditorBusy={error:(r,b)=>Error(b.error)};
        window.submit=()=>{api('/api/batches/'+id+'/publish',{request_id:'r',version_id:'v',sha256:'sha'}).catch(()=>{})};
    '''
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            # Every request to this synthetic origin is fulfilled locally.
            context.route('http://publish.test/**', lambda route: route.fulfill(
                content_type='text/html', body='<button onclick="submit()">上传</button>'))
            context.add_init_script(setup+api+'\nwindow.api=api;')
            page = context.new_page()
            page.goto('http://publish.test/?revision=7')
            page.locator('button').click()
            first = page.evaluate('JSON.parse(pending[0].options.body)')
            page.evaluate("pending[0].reject(new TypeError('offline'))")
            page.wait_for_function('api.pendingWrites.size===0')
            page.goto('http://publish.test/?revision=8')
            page.locator('button').click()
            assert page.evaluate('JSON.parse(pending[0].options.body)') == first
            fresh = context.new_page()
            fresh.goto('http://publish.test/?revision=8')
            assert fresh.evaluate('sessionStorage.length') == 0
            fresh.locator('button').click()
            other = fresh.evaluate('JSON.parse(pending[0].options.body)')
            assert other['revision'] == 8
            assert other['submission_token'] != first['submission_token']
        finally:
            browser.close()


@pytest.mark.ui
@pytest.mark.parametrize('edit_while_waiting', [False, True])
def test_publish_receipt_queue_and_dirty_preservation_in_browser(edit_while_waiting):
    source = (Path(__file__).parents[1] / 'exactcut/web/app.js').read_text()
    code = source[source.index('function isRevisionConflict('):source.index('function positionToast(')]
    code += source[source.index('async function operation('):source.index('function hasDraftIdentity(')]
    code += next(line for line in source.splitlines() if line.startswith('function rememberSavedDraft('))
    code += source[source.index('async function refreshSelectedBatch('):source.index('function renderSourcePrefetch(')]
    code += source[source.index('const publishSubmissions='):source.index("$('assets').addEventListener('click'", source.index('const publishSubmissions='))]
    code += next(line for line in source.splitlines() if line.startswith("$('assets').addEventListener('click'"))
    with shared_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<input id="draft"><div id="assets"><button data-publish="0">上传一</button><button data-publish="1">上传二</button></div><p id="toast"></p>')
            page.evaluate(r'''() => {
                window.$=id=>document.getElementById(id);
                window.current={id:'b'.repeat(32),media_managed:true,media_delivery_enabled:true,revision:7,
                    draft:{text:'saved'},runtime:{status:'review',updated_at:'2026-11-10T10:00:07Z'},
                    assets:[{request_id:'r',version_id:'v',sha256:'sha',preview:'/v',status:'qc_passed'},
                            {request_id:'s',version_id:'w',sha256:'sha2',preview:'/w',status:'qc_passed'}]};
                window.savedDraftBaseline=null;window.selectedId=current.id;window.dirty=false;
                window.openingBatchId=null;window.openingBatchRequest=1;window.batchCache=[];
                window.busy=new Set();window.hookPreviews=new Map();window.validationDismissed=false;
                window.pending=[];window.memory=new Map();window.messages=[];
                Object.defineProperty(window,'sessionStorage',{value:{getItem:k=>memory.get(k),setItem:(k,v)=>memory.set(k,v),removeItem:k=>memory.delete(k)}});
                window.fetch=(path,options)=>new Promise(resolve=>pending.push({path,options,resolve}));
                window.appUrl=x=>x;window.randomId=()=>String(memory.size).padStart(32,'c');
                window.EditorBusy={error:(r,b)=>Error(b.error)};
                window.toast=(message,error)=>{messages.push({message,error});$('toast').textContent=message};
                window.controls=window.renderAssets=window.renderRuntime=window.renderEditor=window.refreshTiming=()=>{};
                window.focusValidationIssue=()=>{};window.pollInterval=()=>0;
                $('draft').addEventListener('input',()=>{current.draft.text=$('draft').value;dirty=true});
                window.reply=index=>{
                    const result=JSON.parse(JSON.stringify(current));result.draft={text:'saved'};
                    result.revision=8+index;result.runtime.updated_at=`2026-11-10T10:00:0${8+index}Z`;
                    result.assets[index].status='upload_queued';
                    pending[index].resolve({ok:true,status:202,json:async()=>result});
                };
            }''')
            page.add_script_tag(content=code+'\nrememberSavedDraft(current);')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.locator('[data-publish="0"]').click()
            page.locator('[data-publish="1"]').click()
            assert page.evaluate('pending.length') == 1
            if edit_while_waiting:
                page.locator('#draft').fill('typed during submission')
            page.evaluate('reply(0)')
            if edit_while_waiting:
                page.wait_for_function('messages.some(m=>m.error) && busy.size===0')
                assert page.evaluate('pending.length') == 1
                assert page.evaluate('current.draft.text') == 'typed during submission'
                assert page.evaluate('dirty') is True
                assert '先保存' in page.locator('#toast').inner_text()
            else:
                page.wait_for_function('pending.length===2')
                assert page.evaluate('JSON.parse(pending[1].options.body).revision') == 8
                page.evaluate('reply(1)')
                page.wait_for_function('busy.size===0')
                assert page.evaluate('current.revision') == 9
            assert not errors
        finally:
            browser.close()
