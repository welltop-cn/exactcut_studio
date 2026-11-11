"""A broken OSS stream resumes with a Range request instead of failing the download."""
import pytest
import requests
from exactcut import originals, webserver


class Remote:
    status = 200

    def __init__(self, chunks, length, headers=None):
        self.chunks, self.headers, self.closed = chunks, {'Content-Length': str(length), **(headers or {})}, False

    def iter_bytes(self):
        assert not self.closed, 'stream used after close()'
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
    return Remote([after, requests.exceptions.ChunkedEncodingError('IncompleteRead 33/113')], 16)


@pytest.mark.parametrize('etag', [None, '"same"'])
def test_interrupted_stream_resumes_from_the_bytes_already_sent(etag):
    handler, first = Handler(), broken()
    if etag:
        first.headers['ETag'] = etag
    calls = []

    def resume(sent):
        calls.append(sent)
        remote = Remote([b'second', b'third'], 11, {'Content-Range': f'bytes {sent}-15/16', 'ETag': etag})
        remote.status = 206
        return remote

    handler._send_original(first, resume=resume)
    assert handler.wfile.getvalue() == b'firstsecondthird'
    assert calls == [len(b'first')]
    assert handler.sent_headers.count(('Content-Length', '16')) == 1
    assert first.closed and handler.diagnostic_status in (None, 200)


def test_retry_before_first_byte_accepts_matching_full_response():
    handler = Handler()
    first = broken(after=b'')
    first.headers['ETag'] = '"same"'
    remote = Remote([b'firstsecondthird'], 16, {'ETag': '"same"'})
    offsets = []

    def resume(sent):
        offsets.append(sent)
        return remote

    handler._send_original(first, resume=resume)
    assert handler.wfile.getvalue() == b'firstsecondthird'
    assert offsets == [0]
    assert first.closed and remote.closed


@pytest.mark.parametrize('changed', [{'Content-Length': '17'}, {'ETag': '"changed"'}])
def test_retry_before_first_byte_rejects_changed_object(changed):
    handler, first = Handler(), broken(after=b'')
    first.headers['ETag'] = '"same"'
    remote = Remote([b'firstsecondthird'], 16, {'ETag': '"same"', **changed})
    with pytest.raises(requests.exceptions.RequestException):
        handler._send_original(first, resume=lambda sent: remote)
    assert handler.wfile.getvalue() == b''
    assert remote.closed


def test_weak_etag_cannot_prove_identical_resume_bytes():
    handler, first = Handler(), broken()
    first.headers['ETag'] = 'W/"same"'
    remote = Remote([b'secondthird'], 11, {'Content-Range': 'bytes 5-15/16', 'ETag': 'W/"same"'})
    remote.status = 206
    with pytest.raises(requests.exceptions.RequestException):
        handler._send_original(first, resume=lambda sent: remote)
    assert handler.wfile.getvalue() == b'first'
    assert remote.closed


def test_unknown_initial_length_allows_consistent_partial_response():
    handler, first = Handler(), broken()
    first.headers.pop('Content-Length')
    remote = Remote([b'secondthird'], 11, {'Content-Range': 'bytes 5-15/*'})
    remote.status = 206
    handler._send_original(first, resume=lambda sent: remote)
    assert handler.wfile.getvalue() == b'firstsecondthird'
    assert first.closed and remote.closed


def test_resume_gives_up_after_bounded_attempts_and_surfaces_the_error():
    handler = Handler()
    attempts = []
    first = broken()
    first.headers['Content-Length'] = '100'

    def resume(sent):
        attempts.append(sent)
        remote = broken()
        remote.status = 206
        remote.headers.update({'Content-Length': str(100 - sent), 'Content-Range': f'bytes {sent}-99/100'})
        return remote

    with pytest.raises(requests.exceptions.RequestException):
        handler._send_original(first, resume=resume)
    assert len(attempts) == 3
    assert handler.wfile.getvalue().startswith(b'first')


@pytest.mark.parametrize('start', [0, 100])
def test_default_adapter_sends_byte_offset_as_http_range(monkeypatch, start):
    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': 'https://oss.example/EP25'})
    calls = []
    from io import BytesIO
    response = requests.Response()
    response.raw = BytesIO(b'')
    response.status_code = 206 if start else 200

    def get(session, url, **kwargs):
        calls.append((url, kwargs['headers'], session.trust_env))
        return response

    monkeypatch.setattr(requests.Session, 'get', get)
    remote = originals.open_original('101606', 'EP25', start=start)
    remote.close()
    expected = {'Accept-Encoding': 'identity', 'Range': 'bytes=100-'} if start else {'Accept-Encoding': 'identity'}
    assert calls == [('https://oss.example/EP25', expected, False)]


def test_default_adapter_resumes_interrupted_body_without_losing_bytes(monkeypatch):
    from urllib3.exceptions import ProtocolError
    payload = bytes(range(256)) * 3000
    offset = 256 * 1024

    class Body:
        def __init__(self, chunks):
            self.chunks, self.closed, self.released = chunks, False, False

        def stream(self, amount, decode_content=True):
            for chunk in self.chunks:
                assert not self.closed and not self.released, 'response closed before its body was read'
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    responses = []
    for status, chunks, headers in [
        (200, [payload[:offset], ProtocolError('upstream disconnected')],
         {'Content-Length': str(len(payload))}),
        (206, [payload[offset:]], {'Content-Length': str(len(payload) - offset),
                                 'Content-Range': f'bytes {offset}-{len(payload)-1}/{len(payload)}'}),
    ]:
        response = requests.Response()
        response.status_code, response.raw = status, Body(chunks)
        response.headers.update(headers)
        responses.append(response)
    calls = []

    def get(session, url, **kwargs):
        index = len(calls)
        calls.append(kwargs['headers'])
        assert kwargs['headers'] == ({'Accept-Encoding': 'identity'} if index == 0 else
                                     {'Accept-Encoding': 'identity', 'Range': f'bytes={offset}-'})
        if index:
            assert responses[0].raw.closed
        return responses[index]

    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': 'https://oss.example/EP25'})
    monkeypatch.setattr(requests.Session, 'get', get)
    handler = Handler()
    handler._send_original(originals.open_original('101606', 'EP25'),
                           resume=lambda sent: originals.open_original('101606', 'EP25', start=sent))
    assert handler.wfile.getvalue() == payload
    assert handler.sent_headers.count(('Content-Length', str(len(payload)))) == 1
    assert len(calls) == 2
    assert all(response.raw.released for response in responses)


@pytest.mark.parametrize('changed', [
    {'Content-Range': 'bytes 0-10/16'},
    {'Content-Range': 'bytes 5-15/17'},
    {'Content-Range': None},
    {'Content-Range': 'bytes 5-4/16'},
    {'Content-Range': 'bytes 5-15/*'},
    {'Content-Length': 'garbage'},
    {'Content-Length': '10'},
    {'ETag': '"different-object"'},
    {'ETag': None},
])
def test_resume_rejects_inconsistent_range_or_object_before_writing(changed):
    handler, first = Handler(), broken()
    first.headers['ETag'] = '"original-object"'
    headers = {'Content-Range': 'bytes 5-15/16', 'ETag': '"original-object"', **changed}
    remote = Remote([b'wrongtail!!'], 11, headers)
    remote.status = 206
    with pytest.raises(requests.exceptions.RequestException):
        handler._send_original(first, resume=lambda sent: remote)
    assert handler.wfile.getvalue() == b'first'
    assert first.closed and remote.closed


def test_open_original_range_requires_a_partial_response(monkeypatch):
    monkeypatch.setattr(originals, 'sign_original', lambda *a, **k: {'url': 'https://oss.example/EP25'})

    from io import BytesIO
    response = requests.Response()
    response.status_code = 200
    response.raw = BytesIO(b'x')

    def get(session, url, **kwargs):
        assert kwargs['headers'] == {'Accept-Encoding': 'identity', 'Range': 'bytes=100-'}
        return response

    monkeypatch.setattr(requests.Session, 'get', get)
    with pytest.raises(originals.OriginalsUnavailable):
        originals.open_original('101606', 'EP25', start=100)
    assert response.raw.closed
