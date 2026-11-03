"""Kernel-held execution bounds; same-account work is admitted through the queue."""
import os
from pathlib import Path
import pytest
from exactcut import cutting,processing_cancel
from exactcut.util import read_json,write_json_atomic
from test_cutting_api import ready
from test_phase2_preview_api import fixture

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('owners',[('alpha','beta'),('alpha','alpha')])
def test_two_preview_execution_slots_are_independent_of_final_and_have_no_account_quota(ready,owners):
    from exactcut import editor_resources as resources
    with cutting.lock_file(cutting.lock_root()/'compute.lock'):
        with resources.acquire('preview',owners[0]) as a,resources.acquire('preview',owners[1]) as b:
            with pytest.raises(cutting.ResourceBusy):resources.acquire('preview','gamma')
            with processing_cancel.scope(lambda:False,leases=[a,b]):
                assert len(processing_cancel.process_options()['pass_fds'])==2
        with resources.acquire('preview','gamma'):pass


def test_roles_are_independent_and_configurable_without_changing_model_config(ready):
    from exactcut import editor_resources as resources
    path=Path(os.environ['EXACTCUT_CONFIG_FILE']);cfg=read_json(path)
    cfg['editor_resources']={'preview_tasks':3,'preview_per_user':2};write_json_atomic(path,cfg)
    with resources.acquire('preview','alpha'),resources.acquire('preview','alpha'),resources.acquire('preview','beta'):
        with resources.acquire('scan','alpha'),resources.acquire('preprocess','alpha'):pass
        with pytest.raises(cutting.ResourceBusy):resources.acquire('preview','gamma')
    assert read_json(path)['cutting']==cfg['cutting']


@pytest.mark.parametrize('value',[0,True,1.5,'2'])
def test_invalid_capacity_is_explicit_configuration_failure(ready,value):
    from exactcut import editor_resources as resources
    path=Path(os.environ['EXACTCUT_CONFIG_FILE']);cfg=read_json(path)
    cfg['editor_resources']={'preview_tasks':value};write_json_atomic(path,cfg)
    with pytest.raises(cutting.CutError,match='配置') as error:resources.acquire('preview','alpha')
    assert error.value.status==503


def test_global_execution_slots_are_shared_between_processes_without_account_quota(ready):
    import subprocess,sys,selectors
    from exactcut import editor_resources as resources
    code="from exactcut import editor_resources as r; import sys;\nwith r.acquire('preview','alpha'):\n print('owned',flush=True);sys.stdin.read(1)"
    env=os.environ.copy();env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
    child=subprocess.Popen([sys.executable,'-c',code],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout,selectors.EVENT_READ)
            assert selector.select(3),'resource owner did not start'
            assert child.stdout.readline().strip()=='owned'
        with resources.acquire('preview','alpha'):
            with pytest.raises(cutting.ResourceBusy):resources.acquire('preview','beta')
            with pytest.raises(cutting.ResourceBusy):resources.acquire('preview','gamma')
    finally:
        child.communicate('x',timeout=3)
    with resources.acquire('preview','alpha'):pass
