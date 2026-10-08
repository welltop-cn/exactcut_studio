import threading
import time
import requests
from http.server import ThreadingHTTPServer
from exactcut import webserver as web


def test_health_distinguishes_live_http_from_stuck_runner(monkeypatch):
    server=ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
    class Runner:
        def is_alive(self):return True
    server.worker_thread=Runner()
    threading.Thread(target=server.serve_forever,daemon=True).start()
    client=requests.Session();client.trust_env=False
    url=f'http://127.0.0.1:{server.server_port}/healthz'
    try:
        monkeypatch.setattr(web,'WORKER_HEARTBEAT',time.monotonic())
        monkeypatch.setattr(web,'WORKER_FAILURES',0)
        assert client.get(url).status_code==200
        monkeypatch.setattr(web,'WORKER_HEARTBEAT',time.monotonic()-40)
        response=client.get(url)
        assert response.status_code==503 and response.json()['status']=='degraded'
        monkeypatch.setattr(web,'WORKER_HEARTBEAT',time.monotonic())
        monkeypatch.setattr(web,'WORKER_FAILURES',3)
        assert client.get(url).status_code==503
    finally:server.shutdown();server.server_close();client.close()
