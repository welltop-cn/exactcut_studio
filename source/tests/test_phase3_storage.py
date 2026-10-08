import fcntl
import os
from pathlib import Path
import time
from types import SimpleNamespace
import pytest
from exactcut import storage
from test_phase2_preview_api import fixture,serve,call


def cache(root,key='a',age=0):
    p=root/f'projects/123456/cache/render-base/{key*64}.mp4'
    p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'rebuildable cache')
    p.with_suffix('.json').write_text('{"fixture":true}')
    os.utime(p,(time.time()-age,time.time()-age))
    return p


def protected_files(root):
    files=[]
    for name in ['source/videos/EP1.mp4','outputs/master.mp4','cache/hook-previews/approved.json','requests/user.xlsx']:
        p=root/'projects/123456'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'keep original')
        files.append(p)
    return {p:(p.read_bytes(),p.stat().st_ino,p.stat().st_mtime_ns) for p in files}


def test_cleanup_skips_live_cache_and_never_touches_original_output_or_approval(tmp_path):
    original=protected_files(tmp_path)
    expired=cache(tmp_path,age=9*86400);active=cache(tmp_path,key='b',age=9*86400)
    with active.with_suffix('.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        result=storage.cleanup(tmp_path)
    assert result['removed']==1 and result['skipped_busy_or_changed']==1
    assert not expired.exists() and active.exists()
    assert active.with_suffix('.lock').is_file()  # Never replace a lock's inode.
    assert {p:(p.read_bytes(),p.stat().st_ino,p.stat().st_mtime_ns) for p in original}==original


def test_quota_removes_oldest_and_manual_preview_rejects_changed_inventory(tmp_path,monkeypatch):
    old=cache(tmp_path,age=60);new=cache(tmp_path,key='b')
    monkeypatch.setattr(storage,'CACHE_LIMIT',old.stat().st_size+old.with_suffix('.json').stat().st_size+1)
    assert storage.cleanup(tmp_path)['removed']==1 and new.exists() and not old.exists()
    report=storage.report(tmp_path)
    new.write_bytes(b'changed cache')
    with pytest.raises(ValueError,match='清单已变化'):storage.cleanup(tmp_path,token=report['cleanup_token'])
    assert new.exists()
    assert storage.cleanup(tmp_path,token=storage.report(tmp_path)['cleanup_token'])['removed']==1


def test_symlinked_project_cache_or_metadata_is_excluded(tmp_path):
    outside=tmp_path/'outside';outside.mkdir();p=outside/('a'*64+'.mp4');p.write_bytes(b'keep')
    project=tmp_path/'projects/123456';project.mkdir(parents=True)
    (project/'cache').symlink_to(outside,target_is_directory=True)
    assert storage.report(tmp_path)['cleanup_count']==0
    (project/'cache').unlink()
    base=cache(tmp_path)
    base.with_suffix('.json').unlink();base.with_suffix('.json').symlink_to(p)
    assert storage.report(tmp_path)['cleanup_count']==0 and p.read_bytes()==b'keep'


def test_low_space_fails_before_render_or_ai_and_handles_distinct_filesystems(tmp_path,monkeypatch):
    monkeypatch.setattr(storage.shutil,'disk_usage',lambda p:SimpleNamespace(free=storage.RESERVE+100,total=100*storage.GIB))
    with pytest.raises(ValueError,match='磁盘空间不足'):storage.require_space(tmp_path,101)
    storage.require_space(tmp_path,100)
    checked=[]
    monkeypatch.setattr(storage,'require_space',lambda p,b:checked.append((p,b)))
    storage.prepare_render(tmp_path,tmp_path/'output/file.mp4',[{'type':'title_card','duration_ms':10000}],{'width':1080,'height':1920,'fps':30})
    assert len(checked)>=2 and all(b>10*1024**2 for p,b in checked)


def test_storage_api_requires_auth_admin_csrf_and_matching_cleanup_preview(fixture,tmp_path):
    p=cache(tmp_path);app=serve()
    try:
        assert call(app,'GET','/api/storage',auth=False)[0]==401
        assert call(app,'POST','/api/storage/cleanup',{'token':'fake'},csrf=False)[0]==403
        assert call(app,'POST','/api/storage/cleanup',{'token':'fake'})[0]==400
        status,report=call(app,'GET','/api/storage');assert status==200
        assert call(app,'POST','/api/storage/cleanup',{'token':report['cleanup_token']})[1]['result']['removed']==1
        assert not p.exists()
    finally:app.shutdown();app.server_close()


def test_storage_api_rejects_producer(fixture,monkeypatch):
    from exactcut.webserver import Handler
    def authorized(self):self.user={'role':'producer','username':'test'};return True
    monkeypatch.setattr(Handler,'authorized',authorized)
    app=serve()
    try:
        assert call(app,'GET','/api/storage')[0]==403
        assert call(app,'POST','/api/storage/cleanup',{'token':'any'})[0]==403
    finally:app.shutdown();app.server_close()
