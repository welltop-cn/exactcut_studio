import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import base64
import pytest
from exactcut.webserver import Handler, job_command


def test_commands_reject_arbitrary_paths_and_actions(tmp_path):
    project = tmp_path / 'projects/101460'
    (project / 'requests').mkdir(parents=True)
    (project / 'project.json').write_text('{}')
    (project / 'requests/request.xlsx').write_bytes(b'PK')
    with patch('exactcut.webserver.ROOT', tmp_path):
        for data in [dict(project='../101460', action='prepare'), dict(project='101460', action='shell'),
                     dict(project='101460', action='execute', input='../request.xlsx')]:
            with pytest.raises(ValueError):
                job_command(data)
        cmd = job_command(dict(project='101460', action='publish', input='request.xlsx'))
        assert cmd[-2:] == ['--publish', '--wait-ready']
        assert '--force' not in cmd


def test_authentication_and_csrf(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.password = 'testing-password-1234567890'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection(*server.server_address)
        conn.request('GET', '/api/state')
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        conn.request('GET', '/healthz')
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        auth = 'Basic ' + base64.b64encode(('admin:' + server.password).encode()).decode()
        conn.request('POST', '/api/jobs', body='{}', headers={'Authorization': auth})
        response = conn.getresponse()
        assert response.status == 403
        response.read()
        conn.request('POST', '/api/jobs', body='{"project":"../../etc","action":"prepare"}',
                     headers={'Authorization': auth, 'X-Exactcut-Request': '1'})
        response = conn.getresponse()
        assert response.status == 400
        response.read()
    finally:
        server.shutdown()
        server.server_close()


def test_catalog_real_names_and_bounds():
    from exactcut.catalog import merge_catalog
    rows = merge_catalog([{'id': 101400, 'title': 'Real title'}, {'id': 101399, 'title': 'Old'},
                          {'id': 101405, 'title': ''}],
                         [{'id': '101400', 'title': 'Supply title'}, {'id': '101401', 'title': '（头） Next title'},
                          {'id': '../foo', 'title': 'Invalid'}])
    assert [(r['id'], r['title']) for r in rows] == [('101400', 'Real title'), ('101401', 'Next title')]


@pytest.mark.integration
def test_download_bridge_deduplicates_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr('exactcut.material_download.validate_downloaded_video', lambda video: None)
    from exactcut.material_download import mirror_videos
    source = tmp_path / 'hub'
    source.mkdir()
    (source / 'EP1.mp4').write_bytes(b'video')
    (source / 'EP2.mp4').write_bytes(b'')
    project = tmp_path / 'project'
    assert mirror_videos(project, source) == ['EP1.mp4']
    assert mirror_videos(project, source) == []
    (source / 'EP1.mp4').write_bytes(b'other')
    with pytest.raises(RuntimeError):
        mirror_videos(project, source)
    assert (project / 'source/videos/EP1.mp4').read_bytes() == b'video'
    (source / 'EP1.mp4').unlink()
    (source / 'EP1.mp4').write_bytes(b'changed size')
    with pytest.raises(RuntimeError):
        mirror_videos(project, source)
    assert (project / 'source/videos/EP1.mp4').read_bytes() == b'video'
