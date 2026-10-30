import threading
import time

import pytest

from exactcut import preview_subtitle_queue as queue, preview_subtitles as subtitles
from test_preview_subtitle_queue import descriptor, configured, dispatcher, ready, fixture
from test_preview_subtitles import wait, CUE
from exactcut import material_versions as media, workflow as w
from exactcut.util import write_json_atomic
from copy import deepcopy


def test_actual_execution_allows_two_materials_in_the_same_batch(configured, monkeypatch, dispatcher):
    bid,rid,sha,source=configured
    media.enroll(w.folder(bid))
    state=media.read(w.folder(bid));vid=state['assets'][rid]['current_version']
    other='Rsecond';other_vid='e'*32
    state['assets'][other]=deepcopy(state['assets'][rid])
    state['assets'][other].update(asset_id=other,current_version=other_vid)
    state['assets'][other]['row']['request_id']=other
    state['draft']['rows'].append(deepcopy(state['assets'][other]['row']))
    state['versions'][other_vid]=deepcopy(state['versions'][vid])
    state['versions'][other_vid]['asset_id']=other
    write_json_atomic(w.folder(bid)/'media-state.json',state)
    subtitles.settings()['max_active_tasks']=2
    entered=threading.Barrier(3);release=threading.Event();seen=[]
    def recognize(*args):
        seen.append(True);entered.wait(timeout=3);assert release.wait(5)
        return [CUE]
    monkeypatch.setattr(subtitles.model,'recognize',recognize)
    subtitles.start(bid,rid,sha,vid,'admin')
    subtitles.start(bid,other,sha,other_vid,'admin')
    dispatcher()
    try:
        entered.wait(timeout=3)
        assert len(seen)==2
    finally:release.set()
    assert wait(bid,rid,sha,vid)['status']=='completed'
    assert wait(bid,other,sha,other_vid)['status']=='completed'



def test_two_durable_slots_preserve_fifo_and_idempotency(configured, monkeypatch):
    for i in range(1,5): queue.enqueue(descriptor(i),20)
    assert queue.claim(2)['task_id']==descriptor(1)['task_id']
    assert queue.claim(2)['task_id']==descriptor(2)['task_id']
    assert queue.claim(2) is None
    assert queue.membership(descriptor(2)['task_id'])['active']
    assert queue.enqueue(descriptor(2),20)=={'status':'active','position':0}
    assert queue.membership(descriptor(3)['task_id'])['position']==1
    # Complete secondary first; the primary remains occupied.
    assert queue.finish(descriptor(2)['task_id'])
    assert queue.claim(2)['task_id']==descriptor(3)['task_id']
    recovered=[]
    monkeypatch.setattr(queue,'_interrupt',lambda item:recovered.append(item['task_id']))
    queue.recover()
    assert recovered==[descriptor(1)['task_id'],descriptor(3)['task_id']]
    assert queue.snapshot()['active'] is None
    assert queue.claim(2)['task_id']==descriptor(4)['task_id']


def test_two_real_workers_overlap_and_stop_keeps_lease_until_both_finish(configured, dispatcher):
    subtitles.settings()['max_active_tasks']=2
    entered=[threading.Event(),threading.Event(),threading.Event()]
    release=[threading.Event(),threading.Event(),threading.Event()]
    order=[]
    def run(item,stop):
        i=int(item['task_id'],16)-1;order.append(i);entered[i].set()
        assert release[i].wait(5)
    for i in range(1,4):queue.enqueue(descriptor(i),20)
    owner=dispatcher(run)
    try:
        assert entered[0].wait(2) and entered[1].wait(2)
        assert not entered[2].is_set()
        release[0].set()
        assert entered[2].wait(2)  # free slot advances while the other is still busy
        owner.stop(timeout=.01)
        assert owner.running
        assert queue.Dispatcher(run=run).start() is False
        release[1].set();release[2].set()
        owner.stop(timeout=2)
        assert not owner.running
        assert order==[0,1,2]
    finally:
        for event in release:event.set()


def test_partial_recovery_preserves_unrecovered_secondary(configured, monkeypatch):
    for i in range(1,3):queue.enqueue(descriptor(i),20);queue.claim(2)
    def interrupt(item):
        if item['task_id']==descriptor(2)['task_id']:raise OSError('disk')
    monkeypatch.setattr(queue,'_interrupt',interrupt)
    with pytest.raises(OSError):queue.recover()
    assert queue.membership(descriptor(2)['task_id'])['active']
