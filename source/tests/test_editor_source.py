"""Only an existing receipt for the session's current master may be signed."""
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import Mock

import pytest
from exactcut import cutting, workflow, preview_oss
from test_cutting_api import ready, fixture, call, serve, prepared


@pytest.fixture
def source_case(tmp_path, monkeypatch):
    video = tmp_path/'master.mp4'
    video.write_bytes(b'master bytes')
    sha = 'a'*64
    session = {'request_id': 'R /1', 'source_sha256': sha}
    bucket = SimpleNamespace(bucket_name='test-bucket', endpoint='https://oss.test', sign_url=Mock(return_value='https://oss.test/signed'))
    receipt = dict(key=f'{preview_oss.object_prefix()}/batch/{quote(session["request_id"], safe="")}/{sha}.mp4',
                   sha256=sha, bytes=video.stat().st_size, identity=preview_oss._identity(video), bucket=bucket.bucket_name, endpoint=bucket.endpoint)
    record = {'output_sha256': sha, 'original_oss': receipt}
    monkeypatch.setattr(cutting, 'settings', lambda: {'oss_playback_enabled': True})
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    monkeypatch.setattr(preview_oss, '_connect', lambda: bucket)
    monkeypatch.setattr(preview_oss, '_ensure_uploaded', Mock(side_effect=AssertionError('must not upload/head')))
    monkeypatch.setattr(workflow, 'folder', lambda bid: tmp_path)
    monkeypatch.setattr(workflow, 'preview_file', lambda *a, **kw: (video, record))
    from exactcut import cutting_access
    check = Mock()
    monkeypatch.setattr(cutting_access, 'require_editable', check)
    return session, record, bucket, check


def resolve(case):
    from exactcut.editor_source import resolve
    return resolve('batch', case[0], '/api/batches/batch/cuts/session/source')


def test_sign_current_master_without_upload(source_case):
    result = resolve(source_case)
    assert result == {'source': 'oss', 'url': 'https://oss.test/signed', 'expires_in': 900, 'reason': 'verified'}
    source_case[2].sign_url.assert_called_once_with('GET', source_case[1]['original_oss']['key'], 900)
    assert source_case[3].call_count == 2


@pytest.mark.parametrize('field,value', [('sha256','b'*64),('key','wrong.mp4'),('bytes',99),('identity',[0]*5),('bucket','other'),('endpoint','https://other')])
def test_bad_receipt_stays_local(source_case, field, value):
    source_case[1]['original_oss'][field] = value
    assert resolve(source_case)['source'] == 'local'
    source_case[2].sign_url.assert_not_called()


def test_disabled_missing_and_sign_failure(source_case, monkeypatch):
    monkeypatch.setattr(cutting,'settings',lambda: {})
    assert resolve(source_case)['reason'] == 'disabled'
    monkeypatch.setattr(cutting,'settings',lambda: {'oss_playback_enabled':True})
    source_case[2].sign_url.side_effect = RuntimeError('credentials secret')
    assert resolve(source_case)['reason'] == 'unavailable'
    source_case[1].pop('original_oss')
    assert resolve(source_case)['source'] == 'local'


def test_version_failure_not_downgraded(source_case):
    source_case[3].side_effect = [None, cutting.CutError('changed',409)]
    with pytest.raises(cutting.CutError) as exc:
        resolve(source_case)
    assert exc.value.status == 409


@pytest.mark.parametrize('kind',['cuts','mosaics'])
def test_http_local_source_and_access(ready,kind):
    bid,rid,sha,_ = ready
    app=serve()
    try:
        base=f'/api/batches/{bid}/{kind}'
        status,session=call(app,'POST',base+'/open',{'request_id':rid,'source_sha256':sha})
        assert status in {200,202}
        session=prepared(app,base,session)
        assert session['capabilities']['oss_playback'] is False
        path=base+'/'+session['id']
        assert call(app,'GET',path+'/source-url',auth=False)[0]==401
        assert call(app,'GET',path+'/source-url')==(200,{'source':'local','url':path+'/source','expires_in':0,'reason':'disabled'})
        other=workflow.create_blank()
        assert call(app,'GET',f'/api/batches/{other["id"]}/{kind}/{session["id"]}/source-url')[0]==404
    finally:
        app.shutdown();app.server_close()


def test_current_record_version_failure_before_sign(source_case):
    source_case[1]['output_sha256']='b'*64
    with pytest.raises(cutting.CutError) as exc:
        resolve(source_case)
    assert exc.value.status==409
    source_case[2].sign_url.assert_not_called()


def test_oss_disabled_does_not_connect(source_case,monkeypatch):
    monkeypatch.setattr(preview_oss,'enabled',lambda:False)
    monkeypatch.setattr(preview_oss,'_connect',Mock(side_effect=AssertionError('disabled connection')))
    assert resolve(source_case)['reason']=='disabled'


@pytest.mark.parametrize('kind',['cuts','mosaics'])
def test_http_signing_cache_control_and_version_fence(ready,kind,monkeypatch):
    from http.client import HTTPConnection
    import base64
    from exactcut.util import read_json, write_json_atomic
    bid,rid,sha,source=ready
    monkeypatch.setattr(cutting,'settings',lambda:{'enabled':True,'oss_playback_enabled':True})
    monkeypatch.setattr(preview_oss,'enabled',lambda:True)
    bucket=SimpleNamespace(bucket_name='test',endpoint='https://oss.test',sign_url=Mock(return_value='https://oss.test/signed'))
    monkeypatch.setattr(preview_oss,'_connect',lambda:bucket)
    results=workflow.folder(bid)/'production/results.json'
    data=read_json(results)
    data['requests'][rid]['original_oss']={'key':f'{preview_oss.object_prefix()}/{bid}/{quote(rid,safe="")}/{sha}.mp4','sha256':sha,'bytes':source.stat().st_size,'identity':preview_oss._identity(source),'bucket':bucket.bucket_name,'endpoint':bucket.endpoint}
    write_json_atomic(results,data)
    app=serve()
    try:
        base=f'/api/batches/{bid}/{kind}'
        status,session=call(app,'POST',base+'/open',{'request_id':rid,'source_sha256':sha})
        assert status in {200,202}
        session=prepared(app,base,session)
        assert session['capabilities']['oss_playback'] is True
        url=base+'/'+session['id']+'/source-url'
        conn=HTTPConnection(*app.server_address,timeout=10)
        try:
            conn.request('GET',url,headers={'Authorization':'Basic '+base64.b64encode(('admin:'+app.password).encode()).decode()})
            response=conn.getresponse()
            assert response.status==200
            assert response.getheader('Cache-Control')=='no-store'
            assert b'https://oss.test/signed' in response.read()
        finally:conn.close()
        data['requests'][rid]['output_sha256']='b'*64;write_json_atomic(results,data)
        assert call(app,'GET',url)[0]==409
        assert bucket.sign_url.call_count==1
    finally:app.shutdown();app.server_close()

from interactive_test_support import interactive_dispatcher
