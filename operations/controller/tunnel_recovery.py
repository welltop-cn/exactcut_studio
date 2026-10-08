#!/usr/bin/env python3
"""Recorded, reversible app-only tunnel repair; system/VPN DNS is untouched."""
import argparse
from datetime import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time
from urllib.request import build_opener, ProxyHandler, Request
from zoneinfo import ZoneInfo

OPS=Path(os.environ.get('EXACTCUT_OPS_ROOT','/Users/admin/Desktop/codex/exact-cut-fbbm-ops'))
LABEL='com.oulagrowth.exactcut.tunnel'
PLIST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
HTTP=build_opener(ProxyHandler({}))

def run(*args):
    return subprocess.run(list(map(str,args)),check=True,capture_output=True,text=True).stdout

def bootstrap(domain):
    # launchd can retain the terminating connector briefly during its grace period.
    for attempt in range(70):
        try:return run('launchctl','bootstrap',domain,PLIST)
        except subprocess.CalledProcessError as exc:
            if exc.returncode!=5 or attempt==69:raise
            time.sleep(.5)
                          
def fetch(url):
    request=Request(url,headers={'User-Agent':'ExactCut-deployment-check'})
    with HTTP.open(request,timeout=10) as reply:return json.load(reply)

def wait_public():
    for attempt in range(8):
        try:
            value=fetch('https://cut.oulagrowth.com/healthz')
            if value.get('status')=='ok':return value
        except Exception:
            if attempt==7:raise
        time.sleep(.5)
    raise RuntimeError('Public health check failed')

def wait_ready():
    for _ in range(40):
        try:
            result=fetch('http://127.0.0.1:28790/ready')
            if result.get('readyConnections',0)>=2:return result
        except Exception:pass
        time.sleep(.5)
    raise RuntimeError('New tunnel connector did not become ready')

def apply(addresses):
    for address in addresses:
        ip=ipaddress.ip_address(address)
        if not any(ip in ipaddress.ip_network(prefix) for prefix in ['198.41.192.0/24','198.41.200.0/24']):
            raise ValueError('Only verified Cloudflare tunnel edge addresses are accepted')
    if len(set(addresses))<2:raise ValueError('Use at least two independently resolved edge addresses')
    at=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    archive=OPS/'tunnel-history'/at;archive.mkdir(parents=True,mode=0o700)
    original=PLIST.read_bytes();(archive/'original.plist').write_bytes(original)
    payload=plistlib.loads(original)
    old_args=payload['ProgramArguments']
    # Retain the original tunnel ID and config; alter only this connector's transport/discovery.
    index=old_args.index('run')
    if '--edge' in old_args or '--protocol' in old_args:
        raise RuntimeError('Tunnel already has an override; review its stored configuration before changing')
    extra=['--protocol','http2','--ha-connections','2','--metrics','127.0.0.1:28790']
    for address in addresses:extra.extend(['--edge',address+':7844'])
    payload['ProgramArguments']=old_args[:index]+extra+old_args[index:]
    revised=plistlib.dumps(payload);(archive/'candidate.plist').write_bytes(revised)
    config=Path(old_args[old_args.index('--config')+1]);shutil.copy2(config,archive/'cloudflared.yml')
    domain=f'gui/{os.getuid()}'
    run('launchctl','bootout',domain+'/'+LABEL)
    try:
        PLIST.write_bytes(revised)
        bootstrap(domain)
        ready=wait_ready()
        public=wait_public()
        if public.get('status')!='ok':raise RuntimeError('Public health check failed')
        receipt={'at':at,'addresses':addresses,'protocol':'http2','ready_connections':ready['readyConnections'],
            'public_health':public,'original':str(archive/'original.plist'),
            'cloudflared_sha256':hashlib.sha256(Path(old_args[0]).read_bytes()).hexdigest(),
            'warning':'Uses cloudflared edge override supported by the installed binary; revalidate on upgrades. Fix VPN fake DNS before removing it.'}
        (archive/'receipt.json').write_text(json.dumps(receipt,indent=2))
        (OPS/'tunnel-current.json').write_text(json.dumps(receipt,indent=2))
        return receipt
    except Exception:
        subprocess.run(['launchctl','bootout',domain+'/'+LABEL],capture_output=True)
        PLIST.write_bytes(original);bootstrap(domain)
        raise

def rollback():
    current=json.loads((OPS/'tunnel-current.json').read_text())
    original=Path(current['original']).read_bytes()
    domain=f'gui/{os.getuid()}'
    run('launchctl','bootout',domain+'/'+LABEL)
    revised=PLIST.read_bytes()
    try:
        PLIST.write_bytes(original);bootstrap(domain)
    except Exception:
        PLIST.write_bytes(revised);bootstrap(domain);raise
    (OPS/'tunnel-current.json').unlink()
    return {'restored':current['original'],'note':'Original DNS discovery is restored; existing VPN DNS issues may return.'}

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    a=sub.add_parser('apply');a.add_argument('addresses',nargs='+')
    sub.add_parser('rollback');sub.add_parser('status')
    args=parser.parse_args()
    with (OPS/'operations.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
        result=apply(args.addresses) if args.command=='apply' else rollback() if args.command=='rollback' else fetch('https://cut.oulagrowth.com/healthz')
        print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
