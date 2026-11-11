"""Original downloads preserve byte identity and commit only complete files."""
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from exactcut import originals, workflow
from test_originals import app, call

BODY = b'original-video-bytes' * 2000


@pytest.fixture
def upstream():
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            start = int(self.headers.get('Range', 'bytes=0-')[6:-1])
            self.send_response(206 if start else 200)
            if self.path == '/':
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<!doctype html><title>Original download test</title>')
                return
            self.send_header('Content-Type', 'video/mp4')
            if self.path == '/gzip' or (self.path == '/negotiated' and self.headers.get('Accept-Encoding') != 'identity'):
                body = gzip.compress(BODY)
                self.send_header('Content-Encoding', 'gzip')
            elif self.path == '/negotiated':
                body = BODY
            else:
                body = b'first'  # EOF-delimited unless this case declares a length.
            total = len(body)
            body = body[start:]
            if start:
                self.send_header('Content-Range', f'bytes {start}-{total-1}/{total}')
            if self.path != '/short':
                self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_encoded_original_is_rejected_before_sending_success(app, upstream, monkeypatch):
    batch = workflow.create_blank()
    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': upstream + '/gzip'})
    status, _, body = call(app, 'GET', f"/api/batches/{batch['id']}/originals/EP1/file?drama_id=101606")
    assert status == 502
    assert '原片暂时无法下载' in json.loads(body)['error']


def test_identity_negotiation_preserves_complete_original(app, upstream, monkeypatch):
    batch = workflow.create_blank()
    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': upstream + '/negotiated'})
    status, headers, body = call(app, 'GET', f"/api/batches/{batch['id']}/originals/EP1/file?drama_id=101606")
    assert status == 200
    assert body == BODY
    assert int(dict(headers)['Content-Length']) == len(BODY)


def test_range_download_preserves_identity_bytes(upstream, monkeypatch):
    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': upstream + '/negotiated'})
    remote = originals.open_original('101606', 'EP1', start=5)
    try:
        originals.validate_resume(remote, 5, length=len(BODY))
        assert b''.join(remote.iter_bytes()) == BODY[5:]
    finally:
        remote.close()


def test_encoded_partial_response_is_rejected(upstream, monkeypatch):
    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': upstream + '/gzip'})
    with pytest.raises(originals.OriginalsUnavailable, match='原片暂时无法下载'):
        originals.open_original('101606', 'EP1', start=5)
