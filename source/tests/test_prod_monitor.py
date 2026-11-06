"""线上监控的离线回归测试。"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location('prod_monitor', Path(__file__).parents[2] / 'monitoring/prod_monitor.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = dict(m.DEFAULTS, log_dir=str(tmp_path), cooldown_seconds=1800)
    state = {'scan_initialized': True}
    sender = Mock()
    monkeypatch.setattr(m, 'send_message', sender)
    monkeypatch.setattr(m, 'health_check', lambda c: (True, '正常', {'release': '版本一'}))
    monkeypatch.setattr(m, 'public_check', lambda c: True)
    monkeypatch.setattr(m, 'process_check', lambda c: True)
    monkeypatch.setattr(m, 'resources', lambda c: [])
    return config, state, sender


def event(**kwargs):
    return dict({'at': '2026-11-05T03:40:00Z', 'event': 'render.failed', 'level': 'error',
                 'batch_id': 'abcdef1234567890abcdef1234567890', 'error': {'type': 'ValueError', 'message': '任务 123 失败'}}, **kwargs)


def write(tmp_path, events, mode='w'):
    path = tmp_path / 'events-20261105-1.jsonl'
    with path.open(mode) as handle:
        for item in events:
            handle.write(json.dumps(item) + '\n')
    return path


@pytest.fixture
def http_failure():
    return event(
        at='2026-11-06T03:13:34.940123+00:00', event='http.failed',
        batch_id='dfdb472b0b354adb992d543196bb9e36', actor='producer06',
        route='/api/batches/dfdb472b0b354adb992d543196bb9e36/originals/EP25/file',
        trace_id='dda15e718d29478394f5937d402c0db7',
        client_request_id='f48b1dcb9ff7141f136bfc85e429b461',
        error={
            'type': 'ChunkedEncodingError',
            'message': "('Connection broken: IncompleteRead(33717782 bytes read, 79363511 more expected)', "
                       'IncompleteRead(33717782 bytes read, 79363511 more expected))',
            'frames': [{'file': 'webserver.py', 'line': 1001, 'function': 'handle_one_request'},
                       {'file': 'models.py', 'line': 939, 'function': 'generate'}],
        })


def test_http_failure_context(tmp_path, env, http_failure):
    config, state, sender = env
    path = write(tmp_path, [http_failure, http_failure])
    m.Monitor(config, state).run()
    text = sender.call_args.args[1]
    for expected in (
        config['public_url'].rstrip('/') + '/#' + http_failure['batch_id'],
        '项目 id：' + http_failure['batch_id'], '账号：producer06', '素材：EP25',
        http_failure['route'], http_failure['trace_id'], http_failure['client_request_id'],
        'ChunkedEncodingError', http_failure['error']['message'], '2 次',
        '11-06 11:13:34—11-06 11:13:34', '样例时间：11-06 11:13:34',
        '主机：线上', '日志：' + path.name, '栈：models.py:939 generate',
    ):
        assert expected in text
    for absent in ('method=', 'status=', 'duration_ms=', '项目名：'):
        assert absent not in text


def test_context_optional_fields_and_redaction(tmp_path, env, http_failure):
    config, state, sender = env
    hook = 'https://notify.example/custom/private-hook'
    http_failure.update(method='GET', status=0, duration_ms=123.5,
                        title='测试项目', request_id='request-full-id', job_id='job-full-id',
                        render_job_id='render-job-full-id', render_id='render-full-id')
    http_failure['route'] += '?token=route-private-value&other=private-query'
    http_failure['error']['frames'][-1]['file'] = '/private/runtime/models.py'
    http_failure['error']['message'] += (
        ' https://download.example/video/EP25.mp4?Signature=signed-private-value&AccessKeyId=access-private-value&Expires=expires-private-value'
        ' https://user:pass-private-value@download.example/another.mp4?token=url-private-value#fragment-private-value'
        ' token=plain-private-value AccessKeyId=key-private-value Signature=sig-private-value'
        ' Bearer bearer-private-value password=password-private-value'
        ' "token": "json-private-value" secret="quoted private-value"'
        ' /private/runtime/credentials.txt /private-file.txt '
        + hook + ' https://open.feishu.cn/open-apis/bot/v2/hook-private-value')
    write(tmp_path, [http_failure])
    m.Monitor(config, state, webhook=hook).run()
    text = sender.call_args.args[1]
    for value in ('GET', 'status=0', 'duration_ms=123.5', '测试项目',
                  'request-full-id', 'job-full-id', 'render-job-full-id', 'render-full-id',
                  'https://download.example/video/EP25.mp4', 'https://download.example/another.mp4',
                  '栈：models.py:939 generate'):
        assert value in text
    for value in ('private-value', 'private-query', 'Signature=', 'AccessKeyId=', 'Expires=',
                  '?token=', '/private/', '/private-file.txt', hook, 'open.feishu.cn', 'user:'):
        assert value not in text


def test_context_survives_cooldown(tmp_path, env, http_failure):
    config, state, sender = env
    path = write(tmp_path, [http_failure])
    m.Monitor(config, state, now=10000).run()
    write(tmp_path, [http_failure], 'a')
    m.Monitor(config, state, now=10100).run()
    # 模拟跨进程恢复状态，且到期时无新增事件。
    state = json.loads(json.dumps(state))
    sender.reset_mock()
    m.Monitor(config, state, now=11800).run()
    text = sender.call_args.args[1]
    assert '期间又出现 1 次' in text
    assert http_failure['route'] in text
    assert '日志：' + path.name in text
    assert http_failure['error']['message'] in text


def test_error_message_truncation(tmp_path, env, http_failure):
    config, state, sender = env
    original = http_failure['error']['message']
    http_failure['error']['message'] += ' extra-context' * 100
    write(tmp_path, [http_failure])
    m.Monitor(config, state).run()
    error_line = sender.call_args.args[1].split('错误：', 1)[1]
    assert original in error_line
    assert error_line.endswith('…')
    assert len(error_line) <= len('ChunkedEncodingError ') + 801


def test_increment_and_partial(tmp_path, env):
    config, state, _ = env
    path = write(tmp_path, [event()])
    with path.open('a') as handle:
        handle.write('{"level":')
    assert sum(g['count'] for g in m.scan_events(config, state).values()) == 1
    offset = state['cursors'][path.name]['offset']
    assert not m.scan_events(config, state)
    assert state['cursors'][path.name]['offset'] == offset
    with path.open('a') as handle:
        handle.write('"error","event":"scan.failed"}\n')
    assert len(m.scan_events(config, state)) == 1


def test_rotation_truncation_and_removal(tmp_path, env):
    config, state, _ = env
    path = write(tmp_path, [event(), event()])
    m.scan_events(config, state)
    write(tmp_path, [event()])
    assert len(m.scan_events(config, state)) == 1
    old = tmp_path / '旧日志'
    path.rename(old)
    write(tmp_path, [event()])
    assert len(m.scan_events(config, state)) == 1
    path.unlink()
    assert not m.scan_events(config, state)
    assert not state['cursors']


def test_merge_cooldown_and_counts(tmp_path, env):
    config, state, sender = env
    write(tmp_path, [event(), event(batch_id='0123456789abcdef0123456789abcdef')])
    m.Monitor(config, state, now=10000).run()
    assert sender.call_count == 1
    assert '2 次' in sender.call_args.args[1]
    assert '项目 id：abcdef1234567890abcdef1234567890' in sender.call_args.args[1]
    assert '同组其他项目 id：0123456789abcdef0123456789abcdef' in sender.call_args.args[1]
    assert config['public_url'].rstrip('/') + '/#abcdef1234567890abcdef1234567890' in sender.call_args.args[1]
    write(tmp_path, [event(), event(), event()], 'a')
    m.Monitor(config, state, now=10100).run()
    assert sender.call_count == 1
    m.Monitor(config, state, now=11800).run()
    assert sender.call_count == 2
    assert '期间又出现 3 次' in sender.call_args.args[1]


@pytest.mark.parametrize('batch_id', [
    'abcdef1234567890abcdef1234567890',
    'a', 'A0_-xyZ9', 'z' * 64, '', 'z' * 65, '项目一',
    'abc\n', 'abc/def', 'abc def', 'A' * 32,
    'https://secret.example/token', 12345678,
])
def test_batch_id_display(tmp_path, env, batch_id):
    config, state, sender = env
    config['public_url'] = 'https://public.example/custom/entry///'
    write(tmp_path, [event(batch_id=batch_id)])
    m.Monitor(config, state).run()
    text = sender.call_args.args[1]
    if batch_id == 'abcdef1234567890abcdef1234567890':
        assert f'项目 id：{batch_id}' in text
        assert f'项目：https://public.example/custom/entry/#{batch_id}' in text
    else:
        assert '项目 id：[无效]' in text
        assert '项目：https://' not in text
        if batch_id and len(str(batch_id)) > 1:
            assert str(batch_id) not in text


def test_ignore_and_warning(tmp_path, env):
    config, state, _ = env
    write(tmp_path, [event(event='http.failed', error={'type': 'BrokenPipeError'}),
                     event(event='http.handler_failed', error={'type': 'ConnectionResetError'}),
                     event(level='warning'), event(event='scan.slow', level='warning')])
    config['warning_events'] = ['scan.slow']
    groups = m.scan_events(config, state)
    assert len(groups) == 1
    assert next(iter(groups.values()))['sample']['event'] == 'scan.slow'


def test_permission_transition(env, monkeypatch):
    config, state, sender = env
    real = m.scan_events
    monkeypatch.setattr(m, 'scan_events', Mock(side_effect=PermissionError('敏感路径')))
    m.Monitor(config, state).run()
    m.Monitor(config, state).run()
    assert sender.call_count == 1
    assert sender.call_args.args[1].count('报错扫描不可用：无读权限') == 1
    # 不可用原因改变不重复提示，其他告警也不追加扫描状态。
    monkeypatch.setattr(m, 'scan_events', Mock(side_effect=FileNotFoundError()))
    monkeypatch.setattr(m, 'public_check', lambda c: False)
    config['failure_count'] = 1
    m.Monitor(config, state).run()
    assert sender.call_count == 2
    assert '公网首页' in sender.call_args.args[1]
    assert '报错扫描不可用' not in sender.call_args.args[1]
    m.Monitor(config, state).run()
    assert sender.call_count == 2
    monkeypatch.setattr(m, 'scan_events', real)
    m.Monitor(config, state).run()
    assert sender.call_count == 3
    assert '报错扫描已恢复' in sender.call_args.args[1]


def test_first_run_and_bootstrap(tmp_path, env):
    config, _, sender = env
    write(tmp_path, [event()])
    state = {}
    m.Monitor(config, state).run()
    assert not sender.called
    write(tmp_path, [event()], 'a')
    m.Monitor(config, state).run()
    assert sender.call_count == 1
    sender.reset_mock()
    write(tmp_path, [event()], 'a')
    m.Monitor(config, state, bootstrap=True).run()
    assert not sender.called
    m.Monitor(config, state).run()
    assert not sender.called


def test_bootstrap_partial_history(tmp_path, env):
    config, _, _ = env
    path = write(tmp_path, [])
    path.write_text('{"level":"error"')
    state = {}
    m.scan_events(config, state)
    with path.open('a') as handle:
        handle.write(',"event":"old"}\n' + json.dumps(event()) + '\n')
    groups = m.scan_events(config, state)
    assert len(groups) == 1
    assert next(iter(groups.values()))['sample']['event'] == 'render.failed'


def test_threshold_hysteresis_and_recovery(env):
    config, state, _ = env
    monitor = m.Monitor(config, state, now=1000)
    monitor.check('disk', '磁盘', True, False, 1, '使用率 90%')
    assert '告警' in monitor.messages[-1]
    monitor.check('disk', '磁盘', False, False, 1, '使用率 82%')
    assert state['checks']['disk']['active']
    monitor.now = 1600
    monitor.check('disk', '磁盘', False, True, 1, '使用率 79%')
    assert '已恢复，持续 10 分钟' in monitor.messages[-1]


def test_consecutive_failure(env, monkeypatch):
    config, state, sender = env
    monkeypatch.setattr(m, 'public_check', lambda c: False)
    m.Monitor(config, state, now=1000).run()
    assert not sender.called
    m.Monitor(config, state, now=1300).run()
    assert sender.call_count == 1
    monkeypatch.setattr(m, 'public_check', lambda c: True)
    m.Monitor(config, state, now=1600).run()
    assert '已恢复' in sender.call_args.args[1]


def test_resources_thresholds(env, monkeypatch):
    config, _, _ = env
    config['mounts'] = ['/srv/data', '/']
    monkeypatch.setattr(m.os, 'statvfs', lambda p: type('磁盘', (), dict(f_blocks=100, f_bfree=10, f_bavail=10))())
    monkeypatch.setattr(m.Path, 'read_text', lambda p: 'MemTotal: 100 kB\nMemAvailable: 9 kB\n')
    monkeypatch.setattr(m.os, 'getloadavg', lambda: (4, 0, 0))
    monkeypatch.setattr(m.os, 'cpu_count', lambda: 2)
    # 使用原始资源函数，夹具仅在其他测试中隔离系统读取。
    values = ORIGINAL_RESOURCES(config)
    assert [item[1] for item in values[:2]] == ['磁盘 /', '磁盘 /srv/data']
    assert all(item[2] for item in values)
    assert values[-1][4] == 3


ORIGINAL_RESOURCES = m.resources


def test_secret_never_in_output(tmp_path, env, monkeypatch, capsys):
    config, state, sender = env
    secret = 'https://secret.example/hook/very-secret?token=abcdef1234567890'
    sender.side_effect = RuntimeError(secret)
    write(tmp_path, [event(error={'type': '错误', 'message': secret})])
    for tick in range(3):
        m.Monitor(config, state, webhook=secret, now=10000 + tick).run()
    output = capsys.readouterr()
    assert secret not in output.err + output.out
    assert '监控脚本自身异常' in sender.call_args.args[1]
    sender.side_effect = None
    m.Monitor(config, state, webhook=secret, now=10004).run()
    assert '自身异常已恢复' in sender.call_args.args[1]
    sender.reset_mock()
    m.Monitor(config, {'scan_initialized': True}, webhook=secret, dry_run=True).run()
    assert secret not in capsys.readouterr().out
    assert not sender.called
    monkeypatch.setenv('FEISHU_ALERT_WEBHOOK', secret)
    assert m.main(['test-message', '--dry-run', '--state-dir', str(tmp_path / '状态'), '--text', secret]) == 0
    assert secret not in capsys.readouterr().out


def test_outbox_retry_and_permissions(tmp_path, env):
    config, state, sender = env
    write(tmp_path, [event()])
    sender.side_effect = OSError('发送失败')
    m.Monitor(config, state).run()
    assert len(state['outbox']) == 1
    sender.side_effect = None
    m.Monitor(config, state).run()
    assert not state['outbox']
    m.save_state(tmp_path, state)
    assert (tmp_path / 'state.json').stat().st_mode & 0o777 == 0o600


def test_outbox_limit(env, capsys):
    config, state, sender = env
    state['outbox'] = [f'旧消息{index}' for index in range(25)]
    sender.side_effect = OSError('发送失败')
    m.Monitor(config, state).run()
    assert state['outbox'] == [f'旧消息{index}' for index in range(5, 25)]
    assert sender.call_args.args[1] == '旧消息5'
    assert capsys.readouterr().err.count('待发消息超过 20 条，已丢弃最旧的 5 条') == 1
    # 脚本自身告警也受队列上限约束。
    state['self_failures'] = 2
    m.Monitor(config, state).run()
    assert len(state['outbox']) == 20
    assert state['outbox'][0] == '旧消息6'
    assert '监控脚本自身异常' in state['outbox'][-1]
    assert capsys.readouterr().err.count('待发消息超过 20 条，已丢弃最旧的 1 条') == 1
    sender.side_effect = None
    m.Monitor(config, state).run()
    assert not state['outbox']


def test_limit_lines_and_release(tmp_path, env, monkeypatch):
    config, state, sender = env
    write(tmp_path, [event(event=f'类别{index}.failed') for index in range(12)])
    m.Monitor(config, state).run()
    text = sender.call_args.args[1]
    assert '另有 2 类' in text
    assert text.count('项目 id：') == 10
    assert text.count('错误：') == 10
    assert text.count('日志：') == 10
    sender.reset_mock()
    monkeypatch.setattr(m, 'health_check', lambda c: (True, '维护中', {'maintenance': True, 'release': '版本二'}))
    m.Monitor(config, state).run()
    assert any('版本已更新：版本一→版本二' in call.args[1] for call in sender.call_args_list)
    assert any('维护中' in call.args[1] for call in sender.call_args_list)


def test_directory_permission_does_not_commit_cursor(tmp_path, env, monkeypatch):
    config, state, sender = env
    write(tmp_path, [event()])
    monkeypatch.setattr(m.os, 'scandir', Mock(side_effect=PermissionError()))
    m.Monitor(config, state).run()
    assert state['scan_status'] == '无读权限'
    assert not state.get('cursors')
    assert '无读权限' in sender.call_args.args[1]
    assert not state['self_failures']


def test_resource_failure_isolated(env, monkeypatch):
    config, state, _ = env
    monkeypatch.setattr(m.os, 'statvfs', Mock(side_effect=PermissionError()))
    monkeypatch.setattr(m.Path, 'read_text', lambda p: 'MemTotal: 100 kB\nMemAvailable: 9 kB\n')
    monkeypatch.setattr(m.os, 'getloadavg', lambda: (0, 0, 0))
    monkeypatch.setattr(m, 'resources', ORIGINAL_RESOURCES)
    monitor = m.Monitor(config, state)
    monitor.run()
    assert state['checks']['memory']['active']
    assert 'load' in state['checks']
    assert state['self_failures'] == 1


def test_load_failure_reset_and_recovery(env):
    config, state, _ = env
    monitor = m.Monitor(config, state, now=1000)
    for bad, recovered in [(True, False), (True, False), (False, False), (True, False), (True, False)]:
        monitor.check('load', '负载', bad, recovered, 3)
    assert not monitor.messages
    monitor.check('load', '负载', True, False, 3)
    assert len(monitor.messages) == 1
    monitor.check('load', '负载', False, True, 3)
    assert '已恢复' in monitor.messages[-1]


def test_frame_signature_and_normalized_ids():
    first = event(error={'type': '错误', 'message': '任务 123 失败'})
    second = event(error={'type': '错误', 'message': '任务 456 失败'})
    assert m.signature(first) == m.signature(second)
    first['error']['frames'] = [{'file': '模块.py', 'function': '执行', 'line': 1}]
    second['error']['frames'] = [{'file': '模块.py', 'function': '执行', 'line': 20}]
    assert m.signature(first) == m.signature(second)
    second['error']['frames'][0]['function'] = '另一执行'
    assert m.signature(first) != m.signature(second)


def test_webhook_file_and_response(tmp_path, monkeypatch):
    file = tmp_path / '通知配置'
    monkeypatch.setenv('FEISHU_ALERT_WEBHOOK', '环境值')
    assert m.webhook_value(file) == '环境值'
    file.write_text('FEISHU_ALERT_WEBHOOK=文件值\n')
    assert m.webhook_value(file) == '文件值'
    response = Mock(status=200)
    response.read.return_value = b'{"code": 0}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock(return_value=response)
    monkeypatch.setattr(m.request, 'urlopen', opener)
    # 单独调用原函数，夹具不参与此用例。
    m.send_message('https://example.invalid/notification', '中文测试')
    assert opener.call_args.kwargs['timeout'] == 10
    body = json.loads(opener.call_args.args[0].data)
    assert body == {'msg_type': 'text', 'content': {'text': '中文测试'}}
    response.read.return_value = b'{"code": 1}'
    with pytest.raises(ValueError):
        m.send_message('https://example.invalid/notification', '中文测试')
    assert m.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid') is None
