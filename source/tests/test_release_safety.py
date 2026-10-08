import base64
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import Mock
import pytest
from exactcut import runtime, batch_control, publish, llm_intake
from exactcut.webserver import Handler


def test_external_clients_and_ai_are_blocked_before_network(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT','staging')
    monkeypatch.setenv('EXACTCUT_ALLOW_EXTERNAL_WRITES','1')
    request=Mock()
    monkeypatch.setattr(publish.requests,'post',request)
    for operation in [publish.FeishuClient,publish.CfAdsClient,lambda:llm_intake.interpret('{}')]:
        with pytest.raises(RuntimeError,match='隔离环境'):operation()
    request.assert_not_called()
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT','production')
    monkeypatch.setenv('EXACTCUT_ALLOW_EXTERNAL_WRITES','0')
    with pytest.raises(RuntimeError):runtime.require_external('upload')

def test_production_paths_and_secrets_cannot_be_used_in_development(tmp_path,monkeypatch):
    state=tmp_path/'.web-state';state.mkdir();(state/'production.json').write_text('{}')
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT','development')
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path))
    with pytest.raises(RuntimeError,match='生产数据'):runtime.data_root()
    with pytest.raises(RuntimeError,match='生产文件'):runtime.protect_path(tmp_path/'.env')
    with pytest.raises(RuntimeError,match='生产文件'):runtime.protect_path(tmp_path/'projects/101494')


def test_process_discovery_only_sees_its_data_instance(tmp_path,monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path/'production'))
    own=runtime.instance_marker()
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path/'staging'))
    foreign=runtime.instance_marker()
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path/'production'))
    def row(pid,batch,marker):
        return {'pid':pid,'stat':'S','command':f'/usr/bin/python -m exactcut.workflow produce {batch} {marker}'}
    snapshot={1:row(1,'a'*32,own),2:row(2,'b'*32,foreign),3:row(3,'c'*32,'')}
    assert list(batch_control.roots(snapshot))==['a'*32]


def test_maintenance_denies_writes_and_keeps_health_available(tmp_path,monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path))
    runtime.maintenance_file().parent.mkdir()
    runtime.maintenance_file().write_text('{}')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.password='testing-password-1234567890'
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        client=HTTPConnection(*server.server_address)
        auth='Basic '+base64.b64encode(('admin:'+server.password).encode()).decode()
        client.request('POST','/api/jobs','{}',{'Authorization':auth,'X-Exactcut-Request':'1'})
        response=client.getresponse();assert response.status==503;response.read()
        client.request('GET','/healthz');response=client.getresponse()
        assert response.status==200
        assert json.loads(response.read())['maintenance']
    finally:server.shutdown();server.server_close()


def test_second_worker_for_same_data_is_rejected(tmp_path):
    state=tmp_path/'.web-state';state.mkdir();(state/'password').write_text('a'*32)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    env=dict(os.environ,EXACTCUT_DATA_ROOT=str(tmp_path),EXACTCUT_ENVIRONMENT='test',
             EXACTCUT_ALLOW_EXTERNAL_WRITES='0',EXACTCUT_PORT=str(port),EXACTCUT_WORKER_ENABLED='1')
    first=subprocess.Popen([sys.executable,'-m','exactcut.webserver'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(60):
            if (state/'accounts.sqlite3').exists():break
            time.sleep(.05)
        assert first.poll() is None
        env['EXACTCUT_PORT']=str(port+1)
        second=subprocess.run([sys.executable,'-m','exactcut.webserver'],env=env,capture_output=True,text=True,timeout=10)
        assert second.returncode!=0
        assert '已有任务执行器' in second.stderr
    finally:first.terminate();first.wait(timeout=10)


@pytest.fixture
def ops(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('release_ops',Path(__file__).resolve().parents[1]/'deploy/ops.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'OPS',tmp_path/'ops')
    root=tmp_path/'production';(root/'.web-state').mkdir(parents=True)
    plist=tmp_path/'web.plist';plist.write_bytes(plistlib.dumps({'Label':'example','ProgramArguments':['old-python'],'WorkingDirectory':'old'}))
    module.write(module.OPS/'settings.json',{'production_root':str(root),'plist':str(plist),'label':'example','port':8791})
    return module,root,plist


def engineering_fixture(module,monkeypatch):
    repo=module.OPS.parent/'repository';repo.mkdir()
    monkeypatch.setattr(module,'REPO',repo)
    module.run('git','init','-b','main',repo)
    (repo/'main.py').write_text('print("release")\n')
    module.run('git','add','main.py',cwd=repo)
    module.run('git','-c','user.name=Test','-c','user.email=test@example.invalid',
               'commit','-m','engineering fixture',cwd=repo)
    commit=module.run('git','rev-parse','HEAD',cwd=repo).strip()
    release=module.release_path('old');release.mkdir(parents=True)
    (release/'main.py').write_text('print("release")\n')
    (release/'requirements.lock.txt').write_text('dependency==1.0\n')
    (release/'.venv').mkdir();(release/'.venv/runtime.bin').write_bytes(b'runtime')
    module.write(release/'release.json',{'version':'old','commit':commit,
        'data_schema':{'version':1,'readable':[1]},'files':module.file_manifest(release)})
    module.write(module.OPS/'current.json',{'version':'old','commit':commit})
    return repo,release


def business_fixture(root):
    db=root/'.web-state/accounts.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE users (name TEXT)');connection.execute("INSERT INTO users VALUES ('original')")
    for name,body in [('feedback/item.json',b'{"value":1}'),
                      ('projects/123456/source/videos/1.mp4',b'original-video'),
                      ('projects/123456/output/material.mp4',b'produced-material'),
                      ('.web-state/batches/live/runtime.json',b'{"status":"producing"}')]:
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
    return [db,root/'feedback/item.json',root/'projects/123456/source/videos/1.mp4',
            root/'projects/123456/output/material.mp4',root/'.web-state/batches/live/runtime.json']


def fingerprints(module,files):
    return {str(p):(module.sha(p),p.stat().st_ino,p.stat().st_mtime_ns) for p in files}


def test_engineering_snapshot_excludes_business_data_and_restores_code_history(ops,monkeypatch):
    module,root,_=ops;repo,release=engineering_fixture(module,monkeypatch)
    files=business_fixture(root);before=fingerprints(module,files)
    (root/'.env').write_text('TEST_CONFIGURATION=fixture\n')
    (root/'.web-state/cloudflared.yml').write_text('tunnel: fixture\n')
    # A release's compatibility data link must never be traversed.
    (release/'projects').symlink_to(root/'projects',target_is_directory=True)
    original_rglob=Path.rglob
    def no_business_scan(path,*args,**kwargs):
        if path in [root,root/'projects',root/'.web-state',root/'feedback']:
            raise AssertionError('Engineering backup traversed a business directory')
        return original_rglob(path,*args,**kwargs)
    monkeypatch.setattr(Path,'rglob',no_business_scan)
    archive=module.snapshot(root,'test',True)
    record=module.read(archive/'manifest.json')
    assert record['kind']=='engineering-backup' and record['version']=='old'
    paths=[entry['path'] for entry in record['files']]
    assert not any(p.startswith(('data/','code/.venv/','code/projects/','code/.web-state/')) for p in paths)
    assert not any(p.endswith(('.mp4','.sqlite3','runtime.json','item.json')) for p in paths)
    assert (archive/'code/requirements.lock.txt').is_file()
    copy=archive/'configuration/.env'
    assert copy.stat().st_ino!=(root/'.env').stat().st_ino
    (root/'.env').write_text('TEST_CONFIGURATION=changed\n')
    assert copy.read_text()=='TEST_CONFIGURATION=fixture\n'
    assert fingerprints(module,files)==before
    restored=repo.parent/'restored'
    module.run('git','clone',archive/'history.bundle',restored)
    assert (restored/'main.py').read_text()==(archive/'code/main.py').read_text()
    assert module.verify(archive)['verified']>0
    copy.write_text('tampered')
    with pytest.raises(RuntimeError,match='verification failed'):module.verify(archive)


def test_engineering_backup_does_not_gate_or_wait_for_active_production(ops,monkeypatch):
    module,root,_=ops;engineering_fixture(module,monkeypatch);files=business_fixture(root)
    before=fingerprints(module,files)
    def forbidden(*args):raise AssertionError('Daily engineering backup interrupted business')
    for name in ['active_records','production_processes','write_barrier','await_idle']:
        monkeypatch.setattr(module,name,forbidden)
    with module.operation_lock():result=module.backup_engineering()
    assert result['scope']=='engineering'
    assert not (root/'.web-state/maintenance.json').exists()
    assert fingerprints(module,files)==before


def test_failed_engineering_archive_is_not_left_as_a_valid_backup(ops,monkeypatch):
    module,root,_=ops;engineering_fixture(module,monkeypatch)
    real_run=module.run
    def broken_bundle(*args,**kwargs):
        if args[:3]==('git','bundle','verify'):raise RuntimeError('bundle invalid')
        return real_run(*args,**kwargs)
    monkeypatch.setattr(module,'run',broken_bundle)
    with pytest.raises(RuntimeError,match='bundle invalid'):module.snapshot(root,'test',True)
    assert list((module.OPS/'backups').iterdir())==[]


def prepare_target(module):
    release=module.release_path('new');release.mkdir(parents=True)
    (release/'exactcut').mkdir();(release/'exactcut/runtime.py').write_text('')
    module.write(release/'release.json',{'version':'new','commit':'abc','data_schema':{'version':1,'readable':[1]}})
    module.write(module.OPS/'current.json',{'version':'old','previous':'older'})
    return release


def test_failed_health_check_restores_deployment_and_preserves_latest_data(ops,monkeypatch):
    module,root,plist=ops;prepare_target(module)
    original=plist.read_bytes();latest=root/'.web-state/latest.json';latest.write_text('{"new_order":true}')
    files=business_fixture(root);before=fingerprints(module,files)
    monkeypatch.setattr(module,'verify',lambda p:None)
    monkeypatch.setattr(module,'preflight',lambda p:{'status':'ok'})
    monkeypatch.setattr(module,'await_idle',lambda *a:None)
    monkeypatch.setattr(module,'snapshot',lambda *a:root/'backup')
    commands=[];monkeypatch.setattr(module,'run',lambda *a,**k:commands.append(a) or '')
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:None)
    checks=iter([RuntimeError('new release unhealthy'),{'status':'ok'}])
    def check(*args):
        value=next(checks)
        if isinstance(value,Exception):raise value
        return value
    monkeypatch.setattr(module,'wait_health',check)
    with pytest.raises(RuntimeError,match='unhealthy'):module.transition('new')
    assert plist.read_bytes()==original
    assert module.read(module.OPS/'current.json')['version']=='old'
    assert latest.read_text()=='{"new_order":true}'
    assert fingerprints(module,files)==before
    assert not (root/'.web-state/maintenance.json').exists()
    assert sum(a[1]=='bootstrap' for a in commands)==2


def test_successful_code_rollback_keeps_latest_database_original_and_output(ops,monkeypatch):
    module,root,plist=ops;release=prepare_target(module)
    files=business_fixture(root);before=fingerprints(module,files)
    monkeypatch.setattr(module,'verify',lambda p:None)
    monkeypatch.setattr(module,'preflight',lambda p:{'status':'ok'})
    monkeypatch.setattr(module,'await_idle',lambda *a:None)
    snapshot=Mock(return_value=module.OPS/'backups/engineering')
    monkeypatch.setattr(module,'snapshot',snapshot)
    monkeypatch.setattr(module,'run',lambda *a,**k:'')
    monkeypatch.setattr(module,'wait_health',lambda *a:{'status':'ok','release':'new'})
    module.transition('new',rollback=True)
    snapshot.assert_called_once_with(root,'pre-rollback',True)
    assert plistlib.loads(plist.read_bytes())['WorkingDirectory']==str(release)
    assert module.read(module.OPS/'current.json')['version']=='new'
    assert fingerprints(module,files)==before
    assert not (root/'.web-state/maintenance.json').exists()


def test_incompatible_data_and_busy_queue_prevent_switch(ops,monkeypatch):
    module,root,plist=ops;prepare_target(module);original=plist.read_bytes()
    monkeypatch.setattr(module,'verify',lambda p:None)
    calls=Mock();monkeypatch.setattr(module,'run',calls)
    module.write(root/'.web-state/data-schema.json',{'version':2})
    with pytest.raises(RuntimeError,match='compatibility'):module.transition('new')
    calls.assert_not_called();assert plist.read_bytes()==original
    module.write(root/'.web-state/data-schema.json',{'version':1})
    monkeypatch.setattr(module,'preflight',lambda p:{})
    def busy(*args):raise RuntimeError('Tasks have not drained')
    monkeypatch.setattr(module,'await_idle',busy)
    with pytest.raises(RuntimeError,match='drained'):module.transition('new')
    calls.assert_not_called();assert plist.read_bytes()==original
    assert not (root/'.web-state/maintenance.json').exists()
