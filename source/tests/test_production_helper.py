import json
import pytest
from unittest.mock import patch
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import threading
import base64
from exactcut import production_helper as h
from exactcut.webserver import Handler


@pytest.fixture(autouse=True)
def independent_helper_model_budget(monkeypatch):
    from exactcut import ai_rate
    monkeypatch.setattr(ai_rate, 'acquire', lambda: None)


def test_scope_and_history_are_enforced():
    h._HISTORY.clear()
    response={'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'in_scope':False,'answer':'unrelated answer','sources':[]})}]}]}
    with patch.object(h,'load_env_file'), patch.dict(h.os.environ,{'OPENAI_API_KEY':'test','EXACTCUT_ENVIRONMENT':'production','EXACTCUT_ALLOW_AI':'1'}), patch.object(h.httpx,'Client') as client:
        client.return_value.__enter__.return_value.post.return_value.json.return_value=response
        result=h.ask({'username':'scope-test'},{'question':'天气','conversation':'test'})
        assert result['answer']==h.REFUSAL
        assert ('scope-test','','test') in h._HISTORY
        assert ('other-user','','test') not in h._HISTORY


def test_ask_without_ai_flag_does_not_call_http():
    h._HISTORY.clear()
    with patch.dict(h.os.environ, {'EXACTCUT_ENVIRONMENT': 'production', 'EXACTCUT_ALLOW_EXTERNAL_WRITES': '1'}, clear=False), \
         patch.object(h.httpx, 'Client') as client:
        h.os.environ.pop('EXACTCUT_ALLOW_AI', None)
        with pytest.raises(RuntimeError, match='EXACTCUT_ALLOW_AI'):
            h.ask({'username': 'blocked'}, {'question': '进度', 'conversation': 'test'})
        client.assert_not_called()


def test_snapshot_excludes_private_fields():
    value=h.snapshot({'draft':{'drama_id':'101573','source':'/secret','author':'private'},'runtime':{'message':'https://example.com/?token=secret sk-abcdefghijk'},'assets':[]})
    assert 'private' not in json.dumps(value)
    assert 'https://' not in value['message']
    assert 'sk-' not in value['message']


def test_helper_rejects_unauthorized_batch_before_model():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.password='testing-password-1234567890'
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        auth='Basic '+base64.b64encode(('admin:'+server.password).encode()).decode()
        with patch.object(Handler,'can_access',return_value=False),patch.object(h,'ask') as ask:
            connection=HTTPConnection(*server.server_address)
            connection.request('POST','/api/help',json.dumps({'batch_id':'a'*32,'question':'进度','conversation':'test'}),{'Authorization':auth,'X-Exactcut-Request':'1'})
            response=connection.getresponse()
            assert response.status==404
            response.read()
            ask.assert_not_called()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('mode', ['', 'source_frames_v1'])
def test_snapshot_preserves_declared_editor_time_format(mode):
    detail={'draft':{'drama_id':'101573',**({'editor_time_format':mode} if mode else {})},'runtime':{},'assets':[]}
    assert h.snapshot(detail)['editor_time_format']==mode


def test_helper_knowledge_distinguishes_current_frame_and_legacy_ms_projects():
    knowledge={item['id']:item['text'] for item in json.loads(h.KNOWLEDGE.read_text())}
    assert 'source_frames_v1' in h.SYSTEM
    assert '帧从0开始' in knowledge['timeline']
    assert '结束帧保留' in knowledge['timeline']
    assert '旧项目' in knowledge['timeline'] and '毫秒' in knowledge['timeline']
    assert '手工填写' in knowledge['workflow']
    assert '点击新建项目后选择或拖入xlsx' not in knowledge['excel']
