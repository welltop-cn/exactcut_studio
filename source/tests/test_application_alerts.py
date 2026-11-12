"""Application events reach the notification boundary without scanning logs."""
import json

from exactcut import diagnostics


def test_application_error_is_delivered_with_parent_project(tmp_path, monkeypatch):
    from exactcut.alerts import AlertQueue
    from exactcut import util
    config = util.load_config()
    config['alerts'] = dict(config['alerts'], enabled=True)
    monkeypatch.setattr(util, 'load_config', lambda: config)
    diagnostics.configure(tmp_path)
    try:
        with diagnostics.context(batch_id='b' * 32, parent_batch_id='a' * 32,
                                 request_id='R001', actor='tester'):
            diagnostics.event('production.failed', level='error', error=ValueError('制作失败'))
        sent = []
        AlertQueue(tmp_path, config['alerts']).flush(sender=sent.append, now=100)
        assert len(sent) == 1
        assert '/#' + 'a' * 32 in sent[0]
        assert '内部批次：' + 'b' * 32 in sent[0]
        assert 'R001' in sent[0] and '制作失败' in sent[0]
    finally:
        diagnostics.disable()


def test_webhook_is_redacted_from_diagnostic_logs(tmp_path, monkeypatch):
    secret = 'https://open.feishu.cn/open-apis/bot/v2/hook/fixture-private-secret'
    monkeypatch.setenv('FEISHU_ALERT_WEBHOOK', secret)
    diagnostics.configure(tmp_path, {})
    try:
        diagnostics.event('production.failed', level='error', error=ValueError('failed ' + secret))
        text = ''.join(p.read_text() for p in (tmp_path / 'logs').glob('*.jsonl'))
        assert 'fixture-private-secret' not in text
    finally:
        diagnostics.disable()


def test_child_error_resolves_parent_from_existing_draft(tmp_path):
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    queue = AlertQueue(tmp_path, load_config()['alerts'])
    child = tmp_path / '.web-state/batches' / ('b' * 32)
    child.mkdir(parents=True)
    (child / 'draft.json').write_text(json.dumps({'media_internal': {'parent_batch_id': 'a' * 32}}))
    queue.enqueue({'at': '2026-11-12T00:00:00Z', 'event': 'render.failed', 'level': 'error',
                   'batch_id': child.name, 'error': {'type': 'ValueError', 'message': '失败'}})
    sent = []
    queue.flush(sender=sent.append)
    assert '/#' + 'a' * 32 in sent[0]


def test_cooldown_accumulates_and_flushes_without_new_events(tmp_path, monkeypatch):
    from exactcut import alerts
    from exactcut.util import load_config
    now = [10000]
    monkeypatch.setattr(alerts.time, 'time', lambda: now[0])
    settings = load_config()['alerts']
    queue = alerts.AlertQueue(tmp_path, settings)
    event = {'at': '2026-11-12T00:00:00Z', 'event': 'render.failed', 'level': 'error',
             'error': {'type': 'ValueError', 'message': '制作失败'}}
    sent = []
    queue.enqueue(event)
    queue.flush(sender=sent.append)
    now[0] += 10
    queue.enqueue(event)
    queue.enqueue(event)
    queue.flush(sender=sent.append)
    assert len(sent) == 1
    now[0] += 1800
    alerts.AlertQueue(tmp_path, settings).flush(sender=sent.append)
    assert len(sent) == 2 and '2 次' in sent[1]


def test_failed_notification_survives_restart(tmp_path):
    import pytest
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    settings = load_config()['alerts']
    queue = AlertQueue(tmp_path, settings)
    queue.enqueue({'at': '2026-11-12T00:00:00Z', 'event': 'render.failed', 'level': 'critical'})
    def unavailable(text):
        raise TimeoutError('fixture network unavailable')
    with pytest.raises(TimeoutError):
        queue.flush(sender=unavailable)
    sent = []
    AlertQueue(tmp_path, settings).flush(sender=sent.append)
    AlertQueue(tmp_path, settings).flush(sender=sent.append)
    assert len(sent) == 1 and 'render.failed' in sent[0]


def test_distinct_errors_are_combined_and_project_ids_are_preserved(tmp_path):
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    queue = AlertQueue(tmp_path, load_config()['alerts'])
    for name, batch in (('render.failed', 'a' * 32), ('render.failed', 'b' * 32), ('upload.failed', 'c' * 32)):
        queue.enqueue({'event': name, 'level': 'error', 'batch_id': batch})
    sent = []
    queue.flush(sender=sent.append)
    assert len(sent) == 1
    assert 'render.failed' in sent[0] and 'upload.failed' in sent[0]
    assert all(batch * 32 in sent[0] for batch in 'abc')


def test_warning_and_disconnected_clients_do_not_alert(tmp_path):
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    queue = AlertQueue(tmp_path, load_config()['alerts'])
    for event in (
        {'event': 'publish.conflict', 'level': 'warning', 'status': 409},
        {'event': 'http.failed', 'level': 'error', 'error': {'type': 'BrokenPipeError'}},
        {'event': 'http.handler_failed', 'level': 'error', 'error': {'type': 'ConnectionResetError'}},
        {'event': 'http.completed', 'level': 'info'},
    ):
        queue.enqueue(event)
    sent = []
    queue.flush(sender=sent.append)
    assert sent == []


def test_error_text_cannot_spoof_redaction_placeholders(tmp_path):
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    queue = AlertQueue(tmp_path, load_config()['alerts'])
    queue.enqueue({'event': 'render.failed', 'level': 'error',
                   'error': {'message': 'failed \x009999\x00 https://user:private-pass@media.example/file?token=secret'}})
    sent = []
    queue.flush(sender=sent.append)
    assert len(sent) == 1 and 'private-pass' not in sent[0] and '?token=' not in sent[0]


def test_notification_redacts_credentials_and_paths(tmp_path):
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    queue = AlertQueue(tmp_path, load_config()['alerts'])
    queue.enqueue({'at': '2026-11-12T00:00:00Z', 'event': 'render.failed', 'level': 'error',
                   'error': {'message': 'https://open.feishu.cn/open-apis/bot/v2/hook/private-hook '
                             'https://user:private-pass@media.example/file?Signature=private-signature '
                             'token=private-token /srv/private/file.txt'},
                   'payload': 'private-payload'})
    sent = []
    queue.flush(sender=sent.append)
    assert 'https://media.example/file' in sent[0]
    assert 'private-' not in sent[0]
    assert b'private-' not in queue.database.read_bytes()


def test_background_send_does_not_hold_queue_transaction(tmp_path):
    import threading
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    queue = AlertQueue(tmp_path, load_config()['alerts'])
    queue.enqueue({'event': 'first.failed', 'level': 'error'})
    entered, release = threading.Event(), threading.Event()
    sent = []
    def blocked(text):
        entered.set()
        assert release.wait(3)
        sent.append(text)
    thread = threading.Thread(target=lambda: queue.flush(sender=blocked))
    thread.start()
    try:
        assert entered.wait(3)
        queue.enqueue({'event': 'second.failed', 'level': 'error'})
        # Another sender cannot double-send while the first sender is blocked.
        queue.flush(sender=sent.append)
        assert sent == []
    finally:
        release.set()
        thread.join(3)
    queue.flush(sender=sent.append)
    assert len(sent) == 2


def test_application_alert_survives_log_write_failure(tmp_path, monkeypatch):
    from exactcut import util
    from exactcut.alerts import AlertQueue
    config = util.load_config()
    config['alerts']['enabled'] = True
    monkeypatch.setattr(util, 'load_config', lambda: config)
    diagnostics.configure(tmp_path)
    (tmp_path / 'logs').write_text('fixture blocks log directory')
    try:
        diagnostics.event('production.failed', level='error', message='制作失败')
        sent = []
        AlertQueue(tmp_path, config['alerts']).flush(sender=sent.append)
        assert len(sent) == 1
    finally:
        diagnostics.disable()


def test_worker_processes_share_a_durable_queue(tmp_path):
    from pathlib import Path
    import subprocess
    import sys
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    settings = load_config()['alerts']
    script = """
import json,sys
from exactcut.alerts import AlertQueue
queue=AlertQueue(sys.argv[1],json.loads(sys.argv[2]))
for index in range(5):
    queue.enqueue({'event':'worker.failed','level':'error','at':'2026-11-12T00:00:00Z'})
"""
    children = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path), json.dumps(settings)],
                                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(2)]
    for child in children:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr.decode()
    sent = []
    AlertQueue(tmp_path, settings).flush(sender=sent.append)
    assert len(sent) == 1 and '10 次' in sent[0]


def test_actual_web_main_starts_and_stops_background_alert_sender(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time
    state = tmp_path / '.web-state'
    state.mkdir()
    (state / 'password').write_text('synthetic-alert-main-password-only')
    env_file = tmp_path / 'empty.env'
    env_file.write_text('')
    config = tmp_path / 'runtime.json'
    config.write_text(json.dumps({'alerts': {'enabled': True, 'poll_seconds': 1}}))
    sent = tmp_path / 'sent.txt'
    script = """
import os
from pathlib import Path
from exactcut import alerts,diagnostics,webserver
from http.server import ThreadingHTTPServer
alerts.send_message=lambda text:Path(os.environ['TEST_ALERT_SENT']).write_text(text)
class Server(ThreadingHTTPServer):
    def serve_forever(self,*args,**kwargs):
        diagnostics.event('main.fixture_failed',level='error',message='synthetic failure')
        return super().serve_forever(*args,**kwargs)
webserver.ThreadingHTTPServer=Server
webserver.main()
"""
    env = {**os.environ, 'EXACTCUT_DATA_ROOT': str(tmp_path), 'EXACTCUT_ENVIRONMENT': 'test',
           'EXACTCUT_ENV_FILE': str(env_file), 'EXACTCUT_CONFIG_FILE': str(config),
           'EXACTCUT_WORKER_ENABLED': '0', 'EXACTCUT_BIND': '127.0.0.1', 'EXACTCUT_PORT': '0',
           'EXACTCUT_ALLOW_EXTERNAL_WRITES': '0', 'TEST_ALERT_SENT': str(sent)}
    process = subprocess.Popen([sys.executable, '-c', script], env=env,
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not sent.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert sent.exists(), 'actual web main did not deliver its event'
        assert 'main.fixture_failed' in sent.read_text()
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr.decode()


def test_disabling_log_files_does_not_disable_alerts(tmp_path, monkeypatch):
    from exactcut import util
    from exactcut.alerts import AlertQueue
    config = util.load_config()
    config['logging']['enabled'] = False
    config['alerts']['enabled'] = True
    monkeypatch.setattr(util, 'load_config', lambda: config)
    diagnostics.configure(tmp_path)
    try:
        diagnostics.event('production.failed', level='error')
        sent = []
        AlertQueue(tmp_path, config['alerts']).flush(sender=sent.append)
        assert len(sent) == 1 and not (tmp_path / 'logs').exists()
    finally:
        diagnostics.disable()


def test_queue_failure_does_not_prevent_diagnostic_logging(tmp_path, monkeypatch):
    from exactcut import util
    config = util.load_config()
    config['alerts']['enabled'] = True
    monkeypatch.setattr(util, 'load_config', lambda: config)
    diagnostics.configure(tmp_path)
    database = tmp_path / '.web-state/alerts/queue.sqlite3'
    database.unlink()
    database.mkdir()
    try:
        diagnostics.event('production.failed', level='error')
        rows = [json.loads(line) for path in (tmp_path / 'logs').glob('*.jsonl') for line in path.read_text().splitlines()]
        assert [row['event'] for row in rows] == ['production.failed']
    finally:
        diagnostics.disable()


def test_more_than_ten_error_types_remain_pending_for_the_next_round(tmp_path):
    from exactcut.alerts import AlertQueue
    from exactcut.util import load_config
    queue = AlertQueue(tmp_path, load_config()['alerts'])
    for index in range(12):
        queue.enqueue({'event': f'failure_{index}', 'level': 'error'})
    sent = []
    queue.flush(sender=sent.append)
    assert len(sent) == 1 and 'failure_10' not in sent[0]
    queue.flush(sender=sent.append)
    assert len(sent) == 2 and 'failure_10' in sent[1] and 'failure_11' in sent[1]
    assert all(len(text.encode()) <= 12000 for text in sent)


def test_isolated_environment_refuses_real_notification(monkeypatch):
    import pytest
    from exactcut.alerts import send_message
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    monkeypatch.setenv('EXACTCUT_ALLOW_EXTERNAL_WRITES', '1')
    monkeypatch.setenv('FEISHU_ALERT_WEBHOOK', 'https://open.feishu.cn/open-apis/bot/v2/hook/fixture')
    with pytest.raises(RuntimeError, match='隔离环境'):
        send_message('fixture')


def test_webhook_transport_validates_response_and_refuses_redirects(monkeypatch):
    import pytest
    from unittest.mock import Mock
    from exactcut import alerts
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'production')
    monkeypatch.setenv('EXACTCUT_ALLOW_EXTERNAL_WRITES', '1')
    monkeypatch.setenv('FEISHU_ALERT_WEBHOOK', 'https://open.feishu.cn/open-apis/bot/v2/hook/fixture')
    response = Mock(status=200)
    response.read.return_value = b'{"code":0}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(alerts.request, 'build_opener', lambda handler: opener)
    alerts.send_message('中文告警')
    assert json.loads(opener.open.call_args.args[0].data) == {'msg_type': 'text', 'content': {'text': '中文告警'}}
    assert opener.open.call_args.kwargs['timeout'] == 10
    for result in (b'{"code":1}', b'{}', b'not-json'):
        response.read.return_value = result
        with pytest.raises(ValueError):
            alerts.send_message('中文告警')
    assert alerts.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.example') is None
    for url in ('http://open.feishu.cn/open-apis/bot/v2/hook/x',
                'https://attacker.example/open-apis/bot/v2/hook/x',
                'https://open.feishu.cn/open-apis/bot/v2/hook/x?token=secret'):
        monkeypatch.setenv('FEISHU_ALERT_WEBHOOK', url)
        with pytest.raises(ValueError, match='invalid alert webhook'):
            alerts.send_message('中文告警')
