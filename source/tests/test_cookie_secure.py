from exactcut.webserver import Handler


def _handler(headers):
    handler = Handler.__new__(Handler)
    handler.headers = headers
    return handler


def test_secure_cookie_follows_forwarded_https_only():
    https = _handler({'X-Forwarded-Proto': 'https, http', 'Host': 'cut.oulagrowth.com'})
    https.cookie('token-value')
    assert https.response_cookie.startswith('exactcut_session=token-value; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800; Secure')
    assert 'Domain=' not in https.response_cookie

    direct = _handler({'Host': 'cut.oulagrowth.com'})
    direct.cookie('token-value')
    assert not direct.response_cookie.endswith('; Secure')
    assert 'oulagrowth' not in direct.response_cookie
    assert 'Domain=' not in direct.response_cookie
