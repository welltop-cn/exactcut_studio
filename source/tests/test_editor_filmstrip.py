"""Real keyframe sprites, shared cache, failures and authenticated route dispatch."""
import shutil
import subprocess
import threading
import time
from contextlib import nullcontext

import pytest
from PIL import Image
from exactcut import cutting, editor_filmstrip as filmstrip, mosaic_http, mosaic_session, runtime
from exactcut.util import read_json

pytestmark=pytest.mark.integration


@pytest.fixture
def source(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,'data_root',lambda:tmp_path)
    monkeypatch.setattr(mosaic_session,'session_path',lambda bid,sid:tmp_path/sid)
    session={'source_sha256':'a'*64,'metadata':{'frame_count':61,'fps_num':30,'fps_den':1},'request_id':'R1'}
    monkeypatch.setattr(mosaic_session,'read_session',lambda bid,sid:session)
    directory=tmp_path/('b'*32);directory.mkdir()
    return directory/'source.mp4',session


def make_video(path,frames=61):
    if not shutil.which('ffmpeg'):pytest.skip('ffmpeg required')
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=s=160x284:r=30',
        '-frames:v',str(frames),'-c:v','libx264','-g','30','-pix_fmt','yuv420p',str(path)],check=True,timeout=30)


def wait_state():
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        value=filmstrip.state('batch','b'*32)
        if value['status']!='running':return value
        time.sleep(.02)
    pytest.fail('filmstrip did not finish')


def test_real_sprite_shared_between_sessions(source,monkeypatch):
    path,session=source;make_video(path)
    calls=[];original=cutting.run
    def run(*args,**kwargs):
        calls.append(args[0]);return original(*args,**kwargs)
    monkeypatch.setattr(cutting,'run',run)
    assert filmstrip.state('batch','b'*32)=={'status':'running'}
    value=wait_state();assert value['status']=='ready'
    assert value['count']==61
    assert value['frames']==list(range(61))
    assert value['schema_version']==1 and value['version']=='v1'
    assert Image.open(filmstrip.image_file('batch','b'*32)).size==(value['columns']*112,value['rows']*116)
    # The other session need not even have its own media available on a cache hit.
    assert filmstrip.state('other-batch','c'*32)['status']=='ready'
    assert len(calls)==1


@pytest.mark.parametrize('frames',[1,301])
def test_one_keyframe_and_tile_cap(source,frames):
    path,session=source;make_video(path,frames)
    session['metadata']['frame_count']=frames
    assert filmstrip.state('batch','b'*32)['status']=='running'
    value=wait_state();assert value['status']=='ready'
    count=min(frames,240)
    assert value['count']==count
    assert value['frames']==[(i*frames//count+(i+1)*frames//count)//2 for i in range(count)]
    image=Image.open(filmstrip.image_file('batch','b'*32))
    assert image.size==(value['columns']*112,value['rows']*116)
    # The final requested tile was filled, including a source with just one keyframe.
    assert image.crop((((count-1)%20)*112,((count-1)//20)*116,((count-1)%20+1)*112,((count-1)//20+1)*116)).getbbox()


def test_failure_is_private_and_retries_after_ten_minutes(source,monkeypatch):
    path,session=source;path.touch();calls=[]
    def fail(*a,**k):calls.append(1);raise RuntimeError('private details')
    monkeypatch.setattr(cutting,'run',fail)
    assert filmstrip.state('batch','b'*32)['status']=='running'
    assert wait_state()=={'status':'failed','error':filmstrip.ERROR}
    root=filmstrip.cache_path(session['source_sha256'])
    assert 'private details' not in str(read_json(root/'v1.failed.json'))
    assert wait_state()['status']=='failed' and len(calls)==1
    from exactcut.util import write_json_atomic
    write_json_atomic(root/'v1.failed.json',{'failed_at':time.time()-601})
    assert filmstrip.state('batch','b'*32)['status']=='running'
    assert wait_state()['status']=='failed' and len(calls)==2


@pytest.mark.parametrize('sha',['../escape','A'*64,'a'*63,None])
def test_invalid_sha_rejected(source,sha):
    source[1]['source_sha256']=sha
    with pytest.raises(cutting.CutError):filmstrip.state('batch','b'*32)


def test_concurrent_requests_generate_once(source,monkeypatch):
    path,_=source;path.touch();entered=threading.Event();release=threading.Event();calls=[]
    def fail(*a,**k):
        calls.append(1);entered.set();assert release.wait(5);raise RuntimeError('synthetic failure')
    monkeypatch.setattr(cutting,'run',fail)
    try:
        assert filmstrip.state('batch','b'*32)['status']=='running'
        assert entered.wait(5)
        assert filmstrip.state('other','c'*32)['status']=='running'
        assert len(calls)==1
    finally:release.set()
    assert wait_state()['status']=='failed'
    assert len(calls)==1


def test_routes_keep_readonly_authorization(source,monkeypatch):
    path,session=source;sid='b'*32;checks=[]
    monkeypatch.setattr(cutting,'enabled',lambda:True)
    monkeypatch.setattr(cutting,'batch_lease',lambda bid:nullcontext())
    monkeypatch.setattr(mosaic_http.workflow,'folder',lambda bid:path.parent)
    monkeypatch.setattr(mosaic_http.cutting_access,'require_editable',lambda *a,**k:checks.append(k))
    monkeypatch.setattr(filmstrip,'state',lambda *a:{'status':'running'})
    path.write_bytes(b'synthetic jpeg')
    monkeypatch.setattr(filmstrip,'image_file',lambda *a:path)
    class Handler:
        def authorized(self):return True
        def can_access(self,bid):return True
        def respond(self,*a,**k):return a,k
    h=Handler()
    assert mosaic_http.handle(h,'batch',sid+'/filmstrip','GET')==((200,{'status':'running'}),{})
    args,kwargs=mosaic_http.handle(h,'batch',sid+'/filmstrip/image','GET')
    assert args==(200,b'synthetic jpeg','image/jpeg')
    assert kwargs['cache']=='private, max-age=31536000, immutable'
    assert len(checks)==2 and all(c['readonly'] for c in checks)
