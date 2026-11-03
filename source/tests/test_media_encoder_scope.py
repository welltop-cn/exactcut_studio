"""Interactive encoder budgets are scoped, nested and never raise operator limits."""
from concurrent.futures import ThreadPoolExecutor
import threading
from exactcut import media


def test_encoder_scope_restores_limits_and_keeps_other_threads_independent(monkeypatch):
    monkeypatch.setattr(media,'load_config',lambda:{'media':{'encoder_threads':4}})
    assert media.encoder_limit()==4
    with media.encoder_scope(2):
        assert media.encoder_limit()==2
        with media.encoder_scope(1):assert media.encoder_limit()==1
        assert media.encoder_limit()==2
    assert media.encoder_limit()==4
    barrier=threading.Barrier(2)
    def limited():
        with media.encoder_scope(2):barrier.wait();return media.encoder_limit()
    def ordinary():barrier.wait();return media.encoder_limit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(limited);b=pool.submit(ordinary)
        assert [a.result(),b.result()]==[2,4]
    monkeypatch.setattr(media,'load_config',lambda:{'media':{'encoder_threads':1}})
    with media.encoder_scope(2):assert media.encoder_limit()==1
