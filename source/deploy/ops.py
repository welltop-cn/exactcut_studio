#!/usr/bin/env python3
"""Local release manager. Secrets and business data are never Git artifacts."""
from __future__ import annotations
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

REPO = Path(os.environ.get('EXACTCUT_REPOSITORY', str(Path(__file__).resolve().parent.parent)))
OPS = Path(os.environ.get('EXACTCUT_OPS_ROOT', str(REPO.with_name('exact-cut-fbbm-ops'))))
ACTIVE = {'queued','downloading','indexing','producing','publishing','running'}

def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')

def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(temp, path)

def run(*args, cwd=None, env=None):
    return subprocess.run(list(map(str,args)), cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout

def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):
            value.update(block)
    return value.hexdigest()

def check_disk(path):
    if shutil.disk_usage(path).free < 10 * 1024**3:
        raise RuntimeError('Less than 10 GiB free; refusing backup/build until storage is reviewed')

def pinned(path):
    """Exact pins in a lock file; comments and -r includes are not pins."""
    return {line.strip().lower() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith(('#','-'))} if path.is_file() else set()

def check_dependencies(release):
    # Runtime pins must all be installed exactly. The only extras allowed are the exact
    # test-tool pins from requirements-dev.lock.txt (the release gate runs pytest).
    expected=pinned(release/'requirements.lock.txt')
    allowed=expected|pinned(release/'requirements-dev.lock.txt')
    actual={line.strip().lower() for line in run(release/'.venv/bin/python','-m','pip','freeze').splitlines()
            if line.strip() and not line.startswith(('#','-e '))}
    if not expected or not expected<=actual or not actual<=allowed:
        raise RuntimeError('Installed dependencies do not match committed requirements.lock.txt')

def external_dependencies(release):
    interpreter=Path(run(release/'.venv/bin/python','-c','import sys;print(sys._base_executable)').strip()).resolve()
    paths=[interpreter,Path(shutil.which('ffmpeg')).resolve(),Path(shutil.which('ffprobe')).resolve(),
           Path(__file__).resolve().parents[2]/'integrations/drama-material-hub/scripts/material_hub.py']
    return [{'path':str(p),'sha256':sha(p)} for p in paths if p.is_file()]

def configuration():
    result = read(OPS/'settings.json')
    if not result:
        raise RuntimeError('Missing private ops/settings.json')
    return result

@contextmanager
def operation_lock():
    OPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (OPS/'operations.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield

def release_path(version):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,80}',version):
        raise ValueError('Invalid release identifier')
    return OPS/'releases'/version

def excluded(path):
    return any(x in {'.git','.venv','__pycache__','.pytest_cache','.web-state','projects','feedback'} for x in path.parts)

def file_manifest(root, code_only=False):
    result = []
    for p in sorted(root.rglob('*')):
        relative = p.relative_to(root)
        if p.is_file() and not p.is_symlink() and (not code_only or not excluded(relative)):
            if relative.as_posix() in {'release.json','manifest.json'}:
                continue
            result.append({'path':relative.as_posix(),'size':p.stat().st_size,'sha256':sha(p)})
    return result

def verify(root):
    root = Path(root)
    record = read(root/'release.json') or read(root/'manifest.json')
    if not record or not record.get('files'):
        raise RuntimeError('Archive has no verification manifest')
    for entry in record['files']:
        relative = Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise RuntimeError('Unsafe manifest path')
        p = root/relative
        if not p.is_file() or p.stat().st_size != entry['size'] or sha(p) != entry['sha256']:
            raise RuntimeError(f'Archive verification failed: {relative}')
    for entry in record.get('external_dependencies',[]):
        if not Path(entry['path']).is_file() or sha(entry['path'])!=entry['sha256']:
            raise RuntimeError('External interpreter/tool changed; revalidate releases before deploying')
    return {'verified':len(record['files']),'path':str(root)}

def clone_file(source,destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    # APFS copy-on-write has separate inodes; never hard-link writable business data.
    if sys.platform == 'darwin':
        result = subprocess.run(['/bin/cp','-c','-p',str(source),str(destination)],capture_output=True)
        if result.returncode == 0:
            return
    shutil.copy2(source,destination)

def active_records(root):
    active = []
    for folder in ['.web-state/batches','.web-state/jobs']:
        paths = (root/folder).glob('*/runtime.json') if folder.endswith('batches') else (root/folder).glob('*.json')
        for p in paths:
            item=read(p)
            if item.get('status') in ACTIVE:
                active.append(p.relative_to(root).as_posix())
    return active

def production_processes():
    # All workflow jobs must drain at release time, including legacy unmarked jobs.
    rows = run('ps','-axo','pid=,command=').splitlines()
    return [r for r in rows if re.search(r'\s-m\s+exactcut\.(workflow|material_download|catalog)(?:\s|$)',r)]

@contextmanager
def write_barrier(root, reason, timeout):
    gate=root/'.web-state/maintenance.json'
    write(gate,{'reason':reason,'at':stamp()})
    try:
        with (root/'.web-state/mutation.lock').open('a') as stream:
            deadline=time.monotonic()+timeout
            while True:
                try:
                    fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise RuntimeError('In-flight request did not finish; aborting')
                    time.sleep(.1)
            yield
    finally:
        gate.unlink(missing_ok=True)

def await_idle(root,timeout):
    deadline=time.monotonic()+timeout
    while True:
        if not active_records(root) and not production_processes():
            return
        if time.monotonic()>=deadline:
            raise RuntimeError('Tasks have not drained; deployment aborted without terminating jobs')
        time.sleep(1)

def snapshot(root,reason,consistent):
    """Archive engineering files only; never traverse or restore live business data."""
    check_disk(OPS)
    current=read(OPS/'current.json',{})
    if not current.get('version'):
        raise RuntimeError('No current engineering release recorded')
    release=release_path(current['version'])
    verify(release)
    metadata=read(release/'release.json')
    settings=configuration()
    folder=OPS/'backups';folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    target=Path(tempfile.mkdtemp(prefix=f'{reason}-{stamp()}-',dir=folder))
    try:
        # Copy only verified, committed source entries. Runtime stays in immutable releases;
        # requirements.lock.txt and version metadata describe rebuilding it after migration.
        for entry in metadata['files']:
            rel=Path(entry['path'])
            if excluded(rel):continue
            if any((release/Path(*rel.parts[:i])).is_symlink() for i in range(1,len(rel.parts)+1)):
                raise RuntimeError('Engineering source contains a symlink')
            clone_file(release/rel,target/'code'/rel)
        write(target/'deployment/release-metadata.json',
              {k:v for k,v in metadata.items() if k!='files'})
        write(target/'deployment/current.json',current)
        clone_file(OPS/'settings.json',target/'deployment/settings.json')
        clone_file(Path(settings['plist']),target/'deployment/web.plist')
        for p in (Path.home()/'Library/LaunchAgents').glob('com.oulagrowth.exactcut*.plist'):
            clone_file(p,target/'deployment/launch-agents'/p.name)
        for name in ['ops.py','tunnel_recovery.py']:
            p=OPS/'controller'/name
            if p.is_file():clone_file(p,target/'deployment/controller'/name)
        for name in ['ops','tunnel']:
            p=OPS/'bin'/name
            if p.is_file():clone_file(p,target/'deployment/bin'/name)
        # Explicit engineering configuration exceptions, never the containing data folders.
        for name in ['.env','.web-state/cloudflared.yml']:
            p=root/name
            if p.exists():
                if p.is_symlink():raise RuntimeError('Engineering configuration contains a symlink')
                destination=target/'configuration'/name
                clone_file(p,destination);destination.chmod(0o600)
        bundle=target/'history.bundle'
        run('git','bundle','create',bundle,'--all',cwd=REPO)
        run('git','bundle','verify',bundle,cwd=REPO)
        write(target/'manifest.json',{'created_at':stamp(),'consistent':consistent,
              'kind':'engineering-backup','version':current['version'],'commit':metadata['commit'],
              'excluded':['original-videos','produced-materials','projects','.web-state business state','feedback'],
              'rollback_policy':'switch immutable code/runtime; retain latest business data',
              'files':file_manifest(target)})
        verify(target)
    except Exception:
        shutil.rmtree(target)
        raise
    return target

def code_environment(release,root,version,production=False,worker=False,port=8793):
    result=dict(os.environ)
    # A staging process never inherits credentials or production control switches.
    if not production:
        for key in list(result):
            if key.startswith(('ALIYUN_','FEISHU_','CF_ADS_','OPENAI_','OPENCLAW_','EXACTCUT_')):
                result.pop(key)
    result.update(EXACTCUT_DATA_ROOT=str(root),EXACTCUT_RELEASE_ID=version,
                  EXACTCUT_ENVIRONMENT='production' if production else 'staging',
                  EXACTCUT_ALLOW_EXTERNAL_WRITES='1' if production else '0',
                  EXACTCUT_WORKER_ENABLED='1' if worker else '0',EXACTCUT_PORT=str(port))
    return result

def copy_runtime(destination):
    run(REPO/'.venv/bin/python','-m','venv','--without-pip',destination)
    source=REPO/'.venv/lib/python3.11/site-packages'
    target=destination/'lib/python3.11/site-packages'
    for p in source.iterdir():
        if p.name.startswith(('__editable__','exact_cut_fbbm-')) or p.name=='__pycache__':continue
        if p.is_dir():shutil.copytree(p,target/p.name,symlinks=True,ignore=shutil.ignore_patterns('__pycache__'))
        else:clone_file(p,target/p.name)

def build(version,ref):
    check_disk(OPS)
    release=release_path(version)
    if release.exists():raise RuntimeError('Release is immutable and already exists')
    if run('git','status','--porcelain',cwd=REPO).strip():raise RuntimeError('Commit all changes before building')
    commit=run('git','rev-parse',ref,cwd=REPO).strip()
    archive=subprocess.check_output(['git','archive',commit],cwd=REPO)
    release.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(release,filter='data')
    copy_runtime(release/'.venv')
    check_dependencies(release)
    isolated=OPS/'build-data'/version
    isolated.mkdir(parents=True)
    env=code_environment(release,isolated,version)
    # Tests create their own mocked HTTP servers and explicitly exercise task submission.
    # No webserver main is launched here; the worker-lock test uses its own temp data.
    env['EXACTCUT_WORKER_ENABLED']='1'
    try:
        # Release gate runs the full suite (all layers), same as linux_ops.
        result=run(release/'.venv/bin/python','-m','pytest','-o','addopts=','-q',cwd=release,env=env)
    except subprocess.CalledProcessError as exc:
        write(OPS/'test-results'/f'{version}-failed.json',{'commit':commit,'stdout':exc.stdout,'stderr':exc.stderr,'at':stamp()})
        raise RuntimeError('Candidate tests failed; details saved privately in ops/test-results') from exc
    write(OPS/'test-results'/f'{version}.json',{'commit':commit,'result':result,'at':stamp()})
    policy=read(release/'deploy/data-schema.json',{'version':1,'readable':[1]})
    runtime_files=[{'path':p.relative_to(release).as_posix(),'size':p.stat().st_size,'sha256':sha(p)}
                   for p in (release/'.venv').rglob('*') if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts]
    write(release/'release.json',{'version':version,'commit':commit,'created_at':stamp(),'data_schema':policy,
        'tests':result.strip().splitlines()[-1], 'files':file_manifest(release,True)+runtime_files,
        'python':run(release/'.venv/bin/python','--version').strip(),
        'ffmpeg':run('ffmpeg','-version').splitlines()[0],
        'external_dependencies':external_dependencies(release)})
    # Baseline code expects durable data relative to its code root. Links also preserve old CLI behavior.
    root=Path(configuration()['production_root'])
    for name in ['.web-state','projects','feedback','.env']:
        if (root/name).exists():(release/name).symlink_to(root/name,target_is_directory=(root/name).is_dir())
    for p in release.rglob('*'):
        if p.is_file() and not p.is_symlink():
            p.chmod(p.stat().st_mode & ~0o222)
    verify(release)
    return {'release':version,'commit':commit,'tests':result.strip().splitlines()[-1]}

def health(port):
    with urlopen(f'http://127.0.0.1:{port}/healthz',timeout=3) as response:
        return json.load(response)

def wait_health(port,version=None):
    for _ in range(30):
        try:
            value=health(port)
            if value.get('status')=='ok' and (version is None or value.get('release')==version):return value
        except Exception:pass
        time.sleep(.5)
    raise RuntimeError('Health check failed')

def preflight(release):
    # Do not run baseline code against copied or production records as a second worker.
    if not (release/'exactcut/runtime.py').exists():return {'baseline':'verified-test-suite'}
    root=OPS/'staging-data'
    (root/'.web-state').mkdir(parents=True,exist_ok=True)
    if not (root/'.web-state/password').exists():
        import secrets
        (root/'.web-state/password').write_text(secrets.token_urlsafe(32))
    settings=configuration()
    port=settings.get('staging_port',8793)
    version=read(release/'release.json')['version']
    env=code_environment(release,root,version,port=port)
    with (OPS/'staging.log').open('ab') as log:
        process=subprocess.Popen([str(release/'.venv/bin/python'),'-m','exactcut.webserver'],cwd=release,
                                  env=env,stdout=log,stderr=log,start_new_session=True)
        try:
            response=wait_health(port,version)
            if response['environment']!='staging':raise RuntimeError('Staging boundary failed')
            password=(root/'.web-state/password').read_text().strip()
            headers={'Authorization':'Basic '+base64.b64encode(('admin:'+password).encode()).decode()}
            def get(path,extra=None):
                request=Request(f'http://127.0.0.1:{port}'+path,headers={**headers,**(extra or {})})
                with urlopen(request,timeout=10) as reply:
                    return reply.status,reply.read(),dict(reply.headers)
            assert get('/')[0]==200
            assert get('/template.xlsx')[1].startswith(b'PK')
            batches=json.loads(get('/api/batches')[1])['batches']
            previews=0
            for batch in batches:
                detail=json.loads(get('/api/batches/'+batch['id'])[1])
                for asset in detail.get('assets',[]):
                    if asset.get('preview'):
                        status,body,meta=get(asset['preview'],{'Range':'bytes=0-1023'})
                        if status!=206 or not body or not meta.get('Content-Range'):
                            raise RuntimeError('Staging video Range check failed')
                        previews+=1
                        break
                if previews:break
            response.update(batches_checked=len(batches),preview_range_checked=previews)
            return response
        finally:
            process.terminate()
            process.wait(timeout=10)

def transition(version,rollback=False,timeout=300):
    root=Path(configuration()['production_root'])
    with write_barrier(root,'rollback' if rollback else 'release',timeout):
        return transition_locked(version,rollback,timeout)

def transition_locked(version,rollback=False,timeout=300):
    release=release_path(version)
    verify(release)
    target=read(release/'release.json')
    settings=configuration()
    root=Path(settings['production_root'])
    schema=read(root/'.web-state/data-schema.json',{'version':1})['version']
    if schema not in target['data_schema']['readable'] or target['data_schema']['version']!=schema:
        raise RuntimeError('Data compatibility gate failed; automatic migration/restore is prohibited')
    staging=preflight(release)
    current=read(OPS/'current.json',{})
    if current.get('version')==version:raise RuntimeError('Already running this release')
    label=settings['label'];domain=f'gui/{os.getuid()}'
    plist=Path(settings['plist']);original=plist.read_bytes()
    stopped=False
    switched=False
    backup=None
    try:
        await_idle(root,timeout)
        run('launchctl','bootout',domain+'/'+label)
        stopped=True
        # Recheck after closing the acceptor. An old version might not understand the gate.
        await_idle(root,0)
        backup=snapshot(root,'pre-rollback' if rollback else 'pre-release',True)
        payload=plistlib.loads(original)
        payload['ProgramArguments']=[str(release/'.venv/bin/python'),'-m','exactcut.webserver']
        payload['WorkingDirectory']=str(release)
        environment=payload.setdefault('EnvironmentVariables',{})
        environment.update(EXACTCUT_DATA_ROOT=str(root),EXACTCUT_ENVIRONMENT='production',
            EXACTCUT_ALLOW_EXTERNAL_WRITES='1',EXACTCUT_WORKER_ENABLED='1',EXACTCUT_RELEASE_ID=version,
            EXACTCUT_PORT=str(settings['port']))
        plist.write_bytes(plistlib.dumps(payload));switched=True
        run('launchctl','bootstrap',domain,plist)
        baseline=not (release/'exactcut/runtime.py').exists()
        check=wait_health(settings['port'],None if baseline else version)
        write(OPS/'current.json',{'version':version,'previous':current.get('version'),
                                'release':str(release),'commit':target['commit'],'at':stamp()})
        write(root/'.web-state/data-schema.json',{'version':schema})
        write(root/'.web-state/production.json',{'environment':'production'})
        receipt={'at':stamp(),'action':'rollback' if rollback else 'publish','from':current.get('version'),
                 'to':version,'backup':str(backup),'backup_scope':'engineering','health':check,'preflight':staging}
        write(OPS/'history'/f'{stamp()}-{version}.json',receipt)
        return receipt
    except Exception as exc:
        if stopped:
            if switched:subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
            plist.write_bytes(original)
            run('launchctl','bootstrap',domain,plist)
            wait_health(settings['port'])
        write(OPS/'current.json',current)
        write(OPS/'history'/f'{stamp()}-failed-{version}.json',{'at':stamp(),'to':version,
            'restored':current.get('version'),'error':type(exc).__name__})
        raise

def backup_engineering():
    root=Path(configuration()['production_root'])
    # operation_lock serializes code/config switches; business writes can continue normally.
    return {'backup':str(snapshot(root,'daily',True)),'scope':'engineering'}

def main():
    if sys.platform != 'darwin':
        print('此入口仅适用于 macOS LaunchAgent；Linux 使用独立的容器部署。', file=sys.stderr)
        raise SystemExit(2)
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status');sub.add_parser('backup')
    b=sub.add_parser('build');b.add_argument('version');b.add_argument('--ref',default='HEAD')
    p=sub.add_parser('publish');p.add_argument('version');p.add_argument('--drain-timeout',type=int,default=300)
    r=sub.add_parser('rollback');r.add_argument('--to');r.add_argument('--drain-timeout',type=int,default=300)
    v=sub.add_parser('verify');v.add_argument('path',type=Path)
    sub.add_parser('bundle')
    args=parser.parse_args()
    with operation_lock():
        if args.command=='status':
            s=configuration();result={'current':read(OPS/'current.json',{}),'active':active_records(Path(s['production_root'])),'health':health(s['port'])}
        elif args.command=='build':result=build(args.version,args.ref)
        elif args.command=='publish':result=transition(args.version,timeout=args.drain_timeout)
        elif args.command=='rollback':
            target=args.to or read(OPS/'current.json',{}).get('previous')
            if not target:raise RuntimeError('No previous release recorded')
            result=transition(target,True,args.drain_timeout)
        elif args.command=='verify':result=verify(args.path)
        elif args.command=='backup':result=backup_engineering()
        else:
            destination=OPS/'git-bundles'/f'code-{stamp()}.bundle';destination.parent.mkdir(parents=True,exist_ok=True)
            run('git','bundle','create',destination,'--all',cwd=REPO);result={'bundle':str(destination)}
        print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
