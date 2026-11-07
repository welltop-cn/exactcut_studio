"""A broken OSS stream resumes with a Range request instead of failing the download."""
import pytest
import requests
from exactcut import originals, webserver


class Remote:
    status = 200

    def __init__(self, chunks, length, headers=None):
        self.chunks, self.headers, self.closed = chunks, {'Content-Length': str(length), **(headers or {})}, False

    def iter_bytes(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class Handler(webserver.Handler):
    def __init__(self):
        from io import BytesIO
        self.wfile = BytesIO()
        self.sent_headers = []
        self.diagnostic_status = None

    def send_response(self, code): self.diagnostic_status = code
    def send_header(self, key, value): self.sent_headers.append((key, value))
    def end_headers(self): pass


def broken(after=b'first'):
    return Remote([after, requests.exceptions.ChunkedEncodingError('IncompleteRead 33/113')], 3 * len(after))


def test_interrupted_stream_resumes_from_the_bytes_already_sent():
    handler, first = Handler(), broken()
    calls = []

    def resume(sent):
        calls.append(sent)
        return Remote([b'second', b'third'], 3 * len(b'first'), {'Content-Range': f'bytes {sent}-'})

    handler._send_original(first, resume=resume)
    assert handler.wfile.getvalue() == b'firstsecondthird'
    assert calls == [len(b'first')]
    assert handler.sent_headers.count(('Content-Length', str(3 * len(b'first')))) == 1
    assert first.closed and handler.diagnostic_status in (None, 200)


def test_resume_gives_up_after_bounded_attempts_and_surfaces_the_error():
    handler = Handler()
    attempts = []

    def resume(sent):
        attempts.append(sent)
        return broken()

    with pytest.raises(requests.exceptions.RequestException):
        handler._send_original(broken(), resume=resume)
    assert len(attempts) <= 3
    assert handler.wfile.getvalue().startswith(b'first')


def test_open_original_range_requires_a_partial_response(monkeypatch):
    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': 'https://oss.example/EP25'})

    def getter(url, headers=None):
        assert headers == {'Range': 'bytes=100-'}
        return Remote([b'x'], 1000, {})

    monkeypatch.setattr(originals, '_get_stream', getter)
    with pytest.raises(originals.OriginalsUnavailable):
        originals.open_original('101606', 'EP25', start=100)
