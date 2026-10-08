import importlib.util
from pathlib import Path
import plistlib
import subprocess
from unittest.mock import Mock
import pytest

@pytest.fixture
def tunnel(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('tunnel_recovery',Path(__file__).resolve().parents[1]/'deploy/tunnel_recovery.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    monkeypatch.setattr(m,'OPS',tmp_path)
    exe=tmp_path/'cloudflared';exe.write_bytes(b'installed binary')
    config=tmp_path/'config.yml';config.write_text('tunnel: original-id')
    plist=tmp_path/'tunnel.plist';plist.write_bytes(plistlib.dumps({'Label':m.LABEL,'ProgramArguments':[str(exe),'tunnel','--config',str(config),'run','original-id']}))
    monkeypatch.setattr(m,'PLIST',plist)
    return m,plist

def test_tunnel_override_retains_id_and_saves_reversible_configuration(tunnel,monkeypatch):
    m,plist=tunnel;original=plist.read_bytes()
    commands=[];monkeypatch.setattr(m,'run',lambda *args:commands.append(args) or '')
    monkeypatch.setattr(m,'wait_ready',lambda:{'readyConnections':2})
    monkeypatch.setattr(m,'fetch',lambda url:{'status':'ok'})
    receipt=m.apply(['198.41.192.27','198.41.200.43'])
    args=plistlib.loads(plist.read_bytes())['ProgramArguments']
    assert args[-2:]==['run','original-id']
    assert '--no-tls-verify' not in args
    assert Path(receipt['original']).read_bytes()==original
    m.rollback()
    assert plist.read_bytes()==original
    assert all(m.LABEL in str(args) or args[1]=='bootstrap' for args in commands)

def test_unready_connector_restores_original_deployment(tunnel,monkeypatch):
    m,plist=tunnel;original=plist.read_bytes()
    monkeypatch.setattr(m,'run',lambda *args:'')
    def fail():raise RuntimeError('connector not ready')
    monkeypatch.setattr(m,'wait_ready',fail)
    monkeypatch.setattr(m.subprocess,'run',lambda *args,**kwargs:None)
    with pytest.raises(RuntimeError,match='not ready'):m.apply(['198.41.192.27','198.41.200.43'])
    assert plist.read_bytes()==original
    assert not (m.OPS/'tunnel-current.json').exists()

def test_invalid_edge_addresses_never_change_service(tunnel,monkeypatch):
    m,plist=tunnel;original=plist.read_bytes();run=Mock();monkeypatch.setattr(m,'run',run)
    with pytest.raises(ValueError):m.apply(['198.18.0.52','198.41.200.43'])
    run.assert_not_called();assert plist.read_bytes()==original

def test_bootstrap_retries_launchd_grace_period_only(tunnel,monkeypatch):
    m,_=tunnel;calls=iter([False,False,True])
    def run(*args):
        if not next(calls):raise subprocess.CalledProcessError(5,args)
        return 'started'
    monkeypatch.setattr(m,'run',run);monkeypatch.setattr(m.time,'sleep',lambda _:None)
    assert m.bootstrap('gui/test')=='started'

def test_public_health_waits_for_connection_propagation(tunnel,monkeypatch):
    m,_=tunnel;results=iter([RuntimeError('temporary edge response'),{'status':'ok'}])
    def fetch(url):
        value=next(results)
        if isinstance(value,Exception):raise value
        return value
    monkeypatch.setattr(m,'fetch',fetch);monkeypatch.setattr(m.time,'sleep',lambda _:None)
    assert m.wait_public()=={'status':'ok'}
