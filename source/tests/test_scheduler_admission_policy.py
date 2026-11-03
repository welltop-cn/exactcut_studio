"""No admission-count gate; independent ordinary lanes retain bounded execution."""
import queue as stdqueue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import os
from pathlib import Path
from exactcut.util import read_json
from exactcut import concurrency,webserver as web,workflow
from exactcut.util import write_json_atomic
from test_concurrency import runner,queued,close_logs
from test_phase2_preview_api import fixture,serve,call
from test_cutting_api import ready
from test_source_prefetch import console,prefetch,draft_with,jobs
from test_web_base_path import app
from test_preview_subtitle_queue import descriptor
from exactcut import preview_subtitle_queue as subtitles_queue,preview_subtitles

REAL_POPEN=subprocess.Popen


def test_no_config_uses_independent_one_per_lane(monkeypatch):
    monkeypatch.setattr(concurrency,'load_config',lambda:{})
    limits=concurrency.settings()
    assert all(limits[lane]==1 for lane in concurrency.LANES)
    assert limits['max_active_children']==4


def test_four_real_lane_children_overlap_and_second_of_each_stays_queued(runner,tmp_path,monkeypatch):
    monkeypatch.setattr(concurrency,'load_config',lambda:{})
    monkeypatch.setattr(web.subprocess,'Popen',REAL_POPEN)
    release=tmp_path/'release';script=tmp_path/'child.py'
    script.write_text("from pathlib import Path\nimport sys,time\nPath(sys.argv[1]).write_text(str(time.time_ns()))\ndeadline=time.monotonic()+10\nwhile not Path(sys.argv[2]).exists() and time.monotonic()<deadline:time.sleep(.01)\n")
    owned={};pending=[]
    for index,lane in enumerate(concurrency.LANES):
        for replica in range(2):
            row=queued(chr(97+index*2+replica),lane)
            row['command']=[sys.executable,str(script),str(tmp_path/(row['id']+'.entered')),str(release)];web.save(row)
    try:
        web.worker_tick(pending,owned)
        deadline=time.monotonic()+3
        while len(list(tmp_path.glob('*.entered')))<4 and time.monotonic()<deadline:time.sleep(.02)
        assert len(list(tmp_path.glob('*.entered')))==4
        assert len(owned)==4 and len(pending)==4
        counts,_,_,_=web.resource_usage(owned);assert all(counts[lane]==1 for lane in concurrency.LANES)
        web.worker_tick(pending,owned);assert len(owned)==4 and len(pending)==4
        assert all(process.poll() is None for _,process,_ in owned.values())
    finally:
        release.touch()
        for _,process,_ in owned.values():process.wait(timeout=5)
        close_logs(owned)


def pad_jobs(root):
    web.JOBS.mkdir(parents=True,exist_ok=True)
    for number in range(12):web.save({'id':f'{number:032x}','action':'refresh_catalog','project':'catalog','status':'queued','created':number})


def test_admin_ordinary_api_persists_more_than_ten_submissions(fixture,tmp_path,monkeypatch):
    monkeypatch.setattr(web,'JOBS',tmp_path/'.web-state/jobs');monkeypatch.setattr(web,'WORK',stdqueue.Queue())
    web.JOBS.mkdir(parents=True,exist_ok=True);server=serve()
    try:
        for _ in range(12):
            code,row=call(server,'POST','/api/jobs',{'action':'refresh_catalog'});assert code==202,row
        assert web.queue_size()==12 and len(list(web.stored_jobs()))==12
    finally:server.shutdown();server.server_close()


def test_auto_source_fetch_is_admitted_when_ordinary_jobs_exceed_ten(console):
    pad_jobs(web.JOBS)
    code,row=prefetch(console,draft_with('EP1'))
    assert code==200 and row['episodes'][0]['status']=='queued'
    assert sum(job['action']=='prefetch' for job in jobs())==1
    prefetch(console,draft_with('EP1'))
    assert sum(job['action']=='prefetch' for job in jobs())==1


@pytest.mark.integration
@pytest.mark.parametrize('action',['start','preview','publish'])
def test_legacy_batch_submission_and_delivery_ignore_global_ten(ready,tmp_path,monkeypatch,action):
    bid,rid,sha,_=ready
    if action=='start':bid=workflow.clone_batch(bid,purpose='copy')['id']
    monkeypatch.setattr(web,'JOBS',tmp_path/'.web-state/jobs');monkeypatch.setattr(web,'WORK',stdqueue.Queue());monkeypatch.setattr(web,'require_publish',lambda:None)
    pad_jobs(web.JOBS);server=serve()
    payload={'request_id':rid,'sha256':sha} if action=='publish' else {'revision':workflow.detail(bid)['revision']}
    try:
        code,row=call(server,'POST',f'/api/batches/{bid}/{action}',payload)
        assert code==202,row
        assert web.queue_size()==13
        repeat_code,repeat=call(server,'POST',f'/api/batches/{bid}/{action}',payload)
        assert repeat_code==200 and repeat['submission']['id']==row['submission']['id']
        assert web.queue_size()==13
    finally:server.shutdown();server.server_close()


@pytest.mark.integration
def test_production_profile_runs_three_real_media_tasks_with_other_lanes(runner,tmp_path,monkeypatch):
    profile=read_json(Path(concurrency.__file__).parents[1]/'config/production.json')
    monkeypatch.setattr(concurrency,'load_config',lambda:profile);monkeypatch.setattr(web.subprocess,'Popen',REAL_POPEN)
    limits=concurrency.settings();assert [limits[k] for k in concurrency.LANES]==[2,3,2,4]
    assert limits['max_active_children']==11
    release=tmp_path/'release';script=tmp_path/'media.py'
    script.write_text("from pathlib import Path\nimport subprocess,sys,time\np=Path(sys.argv[1]);p.write_text(str(time.time_ns()))\nchild=None\nif sys.argv[3]=='render':\n child=subprocess.Popen(['ffmpeg','-v','error','-y','-re','-f','lavfi','-i','testsrc2=size=96x64:rate=30:duration=2','-c:v','libx264',str(p.with_suffix('.mp4'))]);p.with_suffix('.ffmpeg').write_text(str(child.pid))\ndeadline=time.monotonic()+10\nwhile not Path(sys.argv[2]).exists() and time.monotonic()<deadline:time.sleep(.01)\nif child:assert child.wait(timeout=5)==0\n")
    owned={};pending=[]
    for key,lane in zip('abcdefg',['render']*4+['fetch','upload','poll']):
        row=queued(key,lane);row['command']=[sys.executable,str(script),str(tmp_path/(row['id']+'.entered')),str(release),lane];web.save(row)
    try:
        web.worker_tick(pending,owned);deadline=time.monotonic()+3
        while (len(list(tmp_path.glob('*.entered')))<6 or len(list(tmp_path.glob('*.ffmpeg')))<3) and time.monotonic()<deadline:time.sleep(.02)
        assert len(owned)==6 and len(pending)==1
        assert len(list(tmp_path.glob('*.entered')))==6 and len(list(tmp_path.glob('*.ffmpeg')))==3
        for pid in tmp_path.glob('*.ffmpeg'):os.kill(int(pid.read_text()),0)
        counts,_,_,_=web.resource_usage(owned);assert counts=={'fetch':1,'render':3,'upload':1,'poll':1}
        assert all(process.poll() is None for _,process,_ in owned.values())
    finally:
        release.touch()
        for _,process,_ in owned.values():assert process.wait(timeout=6)==0
        close_logs(owned)
    assert len(list(tmp_path.glob('*.mp4')))==3


def test_subtitle_two_active_thirty_waiting_exact_boundary_and_fifo(fixture):
    cfg=preview_subtitles.settings();assert cfg['max_active_tasks']==2 and cfg['max_queued_tasks']==30
    for number in (1,2):subtitles_queue.enqueue(descriptor(number),30);subtitles_queue.claim(2)
    for number in range(3,33):subtitles_queue.enqueue(descriptor(number),30)
    state=subtitles_queue.snapshot();assert len(state['pending'])==30 and len(state['active_tasks'])==2
    import pytest
    with pytest.raises(subtitles_queue.QueueError) as rejected:subtitles_queue.enqueue(descriptor(33),30)
    assert rejected.value.status==429
    assert subtitles_queue.enqueue(descriptor(3),30)['position']==1
    subtitles_queue.finish(descriptor(1)['task_id'])
    assert subtitles_queue.claim(2)['task_id']==descriptor(3)['task_id']
