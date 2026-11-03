"""Production children and cut/mosaic keep separate limits; neither waits on the other."""
from contextlib import ExitStack
from pathlib import Path
import os,signal,subprocess,sys,threading,time
import pytest
from exactcut import editor_resources,media
from exactcut.util import write_json_atomic
from test_source_frames import source  # noqa: F401  (fixture)
from test_web_base_path import app  # noqa: F401  (fixture)

FRAME=['ffmpeg','-v','error','-f','lavfi','-i','color=size=32x32:rate=1:duration=1','-frames:v','1','-y']


@pytest.fixture
def stage_root(tmp_path,monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path));monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test')
    cfg=tmp_path/'runtime.json';write_json_atomic(cfg,{'environment':'test'});monkeypatch.setenv('EXACTCUT_CONFIG_FILE',str(cfg))
    return tmp_path


def hold_all(stack):
    for kind in ('media','frame','transfer'):
        capacity=editor_resources.settings()[editor_resources._ROLES[kind][0]]
        for _ in range(capacity):stack.enter_context(editor_resources.acquire(kind))


@pytest.mark.integration
def test_workflow_child_media_and_transfer_run_while_editor_holds_every_slot(stage_root):
    out=stage_root/'production.png';(stage_root/'.env').write_text('')
    # Real `python -m exactcut.workflow` entry; only the stage body is a fixture.
    code=("import runpy,sys\nfrom exactcut import stage_results,material_workers,media,editor_resources\n"
          "stage_results.run_child=lambda function,*args:function(*args)\nout=sys.argv[1]\n"
          "def body(*_):\n media.run_checked("+repr(FRAME)+"+[out]);media.ffprobe(out)\n"
          " with editor_resources.stage('transfer'):pass\n return 0\n"
          "material_workers.retry_media_upload=body\n"
          "sys.argv=['workflow','media-upload','B','R']\nrunpy.run_module('exactcut.workflow',run_name='__main__')\n")
    with ExitStack() as held:
        hold_all(held)
        started=time.monotonic()
        result=subprocess.run([sys.executable,'-c',code,str(out)],capture_output=True,text=True,timeout=20,
            env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1])})
        assert result.returncode==0,result.stderr
        assert out.is_file() and time.monotonic()-started<15


@pytest.mark.integration
def test_editor_media_still_waits_for_its_own_slots(stage_root):
    out=stage_root/'editor.png'
    with ExitStack() as held:
        hold_all(held)
        worker=threading.Thread(target=media.run_checked,args=(FRAME+[str(out)],),daemon=True);worker.start()
        time.sleep(.6)
        assert worker.is_alive() and not out.exists(),'editor ffmpeg must wait while all media slots are held'
    worker.join(timeout=10)
    assert not worker.is_alive() and out.is_file()


def test_editor_transfer_stage_still_waits(stage_root):
    entered=threading.Event()
    def upload():
        with editor_resources.stage('transfer'):entered.set()
    with ExitStack() as held:
        hold_all(held)
        threading.Thread(target=upload,daemon=True).start()
        assert not entered.wait(.6)
    assert entered.wait(5)


def run_production_main(tmp_path,module,argv,setup,timeout=20,**popen):
    """Run a real production `python -m <module>` entry; `setup` only replaces its work body."""
    env_file=tmp_path/'child.env';env_file.write_text('')
    code=("import runpy,sys\nfrom exactcut import stage_results\n"
          "stage_results.run_child=lambda function,*args:function(*args)\n"+setup+
          "\nsys.argv=['main',*"+repr(argv)+"]\nrunpy.run_module("+repr(module)+",run_name='__main__')\n")
    env={**os.environ,'EXACTCUT_ENV_FILE':str(env_file),
         'PYTHONPATH':os.pathsep.join([str(Path(__file__).parents[1]),str(Path(__file__).parent)])}
    if popen:return subprocess.Popen([sys.executable,'-c',code],env=env,**popen)
    return subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=timeout,env=env)


@pytest.mark.integration
def test_material_download_validation_runs_while_editor_holds_every_slot(stage_root):
    (stage_root/'projects/101573').mkdir(parents=True);(stage_root/'projects/101573/project.json').write_text('{}')
    out=stage_root/'download.png'
    setup=("from exactcut import source_cache,media\nsource_cache.check_drama=lambda drama:None\n"
           "def ensure(drama,episodes):\n media.run_checked("+repr(FRAME+[str(out)])+");media.ffprobe("+repr(str(out))+");return ['EP1.mp4']\n"
           "source_cache.ensure=ensure")
    with ExitStack() as held:
        hold_all(held)
        result=run_production_main(stage_root,'exactcut.material_download',['101573','EP1'],setup)
        assert result.returncode==0,result.stderr
        assert out.is_file()


@pytest.mark.integration
def test_config_source_prefetch_frame_index_runs_while_editor_holds_every_slot(source,tmp_path):
    from exactcut import workflow
    _,item,_,_=source
    setup=("from exactcut import source_frames,workflow\nfrom test_source_prefetch import draft_with\n"
           # The module's own `run` needs a queued job record; only the indexing body runs here.
           "stage_results.run_child=lambda _run,batch,job:source_frames.prepare(workflow.folder(batch),draft_with('EP1'),'EP1') and 0")
    with ExitStack() as held:
        hold_all(held)
        result=run_production_main(tmp_path,'exactcut.source_prefetch',[item['id'],'J1'],setup)
        assert result.returncode==0,result.stderr
    assert next(workflow.folder(item['id']).glob('source-picker/*/index.json')).is_file()


@pytest.mark.integration
def test_pausing_a_workflow_process_group_freezes_its_media_child(stage_root):
    """Batch pause SIGSTOPs the workflow group; production media must stay inside it (8216c27)."""
    ticks=stage_root/'ticks';pids=stage_root/'pids'
    loop=("import os,time\nopen("+repr(str(pids))+",'w').write(f'{os.getpid()} {os.getpgrp()}')\n"
          "while True:\n open("+repr(str(ticks))+",'a').write('.');time.sleep(.05)")
    setup=("from exactcut import material_workers,media\n"
           "material_workers.retry_media_upload=lambda *a:media.run_checked([sys.executable,'-c',"+repr(loop)+"],timeout=60) and 0")
    process=run_production_main(stage_root,'exactcut.workflow',['media-upload','B','R'],setup,start_new_session=True)
    try:
        deadline=time.monotonic()+10
        while not (ticks.exists() and pids.exists() and pids.read_text()) and time.monotonic()<deadline:time.sleep(.05)
        child,group=map(int,pids.read_text().split())
        assert group==process.pid,'production media child must share the workflow process group'
        os.killpg(process.pid,signal.SIGSTOP);time.sleep(.3)
        frozen=ticks.stat().st_size;time.sleep(.6)
        assert ticks.stat().st_size==frozen,'paused production must freeze its media child'
        os.killpg(process.pid,signal.SIGCONT);time.sleep(.5)
        assert ticks.stat().st_size>frozen
    finally:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5)
