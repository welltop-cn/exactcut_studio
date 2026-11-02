"""The new stage leases protect actual children and cannot deadlock nested SAM media."""
from contextlib import ExitStack
from pathlib import Path
import json,os,sys
import pytest
from exactcut import editor_resources,media,processing_cancel
from exactcut.util import write_json_atomic


@pytest.fixture
def stage_root(tmp_path,monkeypatch):
    monkeypatch.setenv('EXACTCUT_DATA_ROOT',str(tmp_path));monkeypatch.setenv('EXACTCUT_ENVIRONMENT','test')
    cfg=tmp_path/'runtime.json';write_json_atomic(cfg,{'environment':'test'});monkeypatch.setenv('EXACTCUT_CONFIG_FILE',str(cfg))
    return tmp_path,cfg


def test_final_media_can_advance_while_shared_media_is_full(stage_root):
    root,_=stage_root
    with ExitStack() as held:
        for _ in range(5):held.enter_context(editor_resources.acquire('media'))
        with editor_resources.final_group(),editor_resources.media_stage():
            with pytest.raises(cutting.ResourceBusy):editor_resources.acquire('final_media')
            media.run_checked(['ffmpeg','-v','error','-f','lavfi','-i','color=size=32x32:rate=1:duration=1','-frames:v','1','-y',str(root/'frame.png')])
        assert (root/'frame.png').is_file()


def test_inherited_media_lease_skips_second_acquisition_in_real_child(stage_root):
    root,cfg=stage_root;write_json_atomic(cfg,{'environment':'test','editor_resources':{'media_tasks':1}})
    # A unit fault fixture deliberately has one slot; full dev acceptance uses the production profile.
    code="from exactcut import editor_resources,media\nimport json,sys\nwith editor_resources.inherited_media(json.loads(sys.argv[1])):\n media.run_checked(['ffmpeg','-v','error','-f','lavfi','-i','color=size=32x32:rate=1:duration=1','-frames:v','1','-y',sys.argv[2]])\nprint('completed')\n"
    with editor_resources.stage('media'):
        bindings=editor_resources.export_media();assert bindings
        result=processing_cancel.run([sys.executable,'-c',code,json.dumps(bindings),str(root/'child.png')],
            env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1])},capture_output=True,text=True,timeout=5)
        assert result.returncode==0,result.stderr
        assert result.stdout.strip()=='completed' and (root/'child.png').is_file()


def test_inherited_lease_rejects_an_unrelated_reused_descriptor(stage_root):
    root,_=stage_root
    with editor_resources.stage('media'):
        bindings=editor_resources.export_media()
        with (root/'unrelated').open('w') as unrelated:
            bad=[{**bindings[0],'fd':unrelated.fileno()}]
            with pytest.raises(ValueError,match='句柄已变化'):
                with editor_resources.inherited_media(bad):pytest.fail('must not trust a reused descriptor')


from exactcut import cutting
