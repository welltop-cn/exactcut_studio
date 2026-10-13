"""Frame drafts and segment effects must not be handed to an old writer."""
import pytest
from test_linux_release import ops,initialize,release,Engine


@pytest.mark.parametrize('record', [
 {'draft': {'editor_time_format': 'source_frames_v1'}},
 {'draft': {'rows': [{'clips':[{'time_format':'source_frames_v1'}]}]}},
 {'versions': {'v': {'manifest': {'requests':[{'timeline':[{'segment_caption':{'text':'x','duration_ms':1500}}]}]}}}},
 {'requests':[{'timeline':[{'transition_in':'flash_white'}]}]},
])
def test_old_editor_blocked_before_stop(ops,tmp_path,record):
 initialize(ops,tmp_path);release(ops,tmp_path,'old')
 ops.write(tmp_path/'data/.web-state/batches/a/draft.json',record)
 engine=Engine()
 assert not ops.check_switch(tmp_path,'old',engine=engine)['allowed']
 with pytest.raises(RuntimeError,match='编辑'):
  ops.transition(tmp_path,'old',engine=engine,timeout=0)
 assert not engine.calls
 engine.editor_protocol=lambda image:True
 assert ops.check_switch(tmp_path,'old',engine=engine)['allowed']


def test_failed_health_cannot_restore_old_editor_after_new_data(ops,tmp_path):
 initialize(ops,tmp_path);release(ops,tmp_path,'old');new=release(ops,tmp_path,'new')
 new['image']='sha256:'+'c'*64;ops.write(tmp_path/'releases/new/release.json',new)
 ops.write(tmp_path/'current.json',{'version':'old'})
 engine=Engine();engine.editor_protocol=lambda image:image==new['image']
 def health(*args):
  ops.write(tmp_path/'data/.web-state/batches/a/draft.json',{'editor_time_format':'source_frames_v1'})
  raise RuntimeError('startup failed')
 engine.health=health
 with pytest.raises(RuntimeError,match='恢复失败'):
  ops.transition(tmp_path,'new',engine=engine,timeout=0)
 assert ('apply','old') not in engine.calls
 assert (tmp_path/'data/.web-state/maintenance.json').exists()


@pytest.mark.parametrize('content', ['{broken', '{"draft":{"editor_time_format":"source_frames_v1"}}'])
def test_archived_or_unreadable_editor_data_blocks_old_image(ops,tmp_path,content):
 initialize(ops,tmp_path);release(ops,tmp_path,'old')
 path=tmp_path/'data/projects/101573/requests/import-a/normalized.json'
 path.parent.mkdir(parents=True);path.write_text(content)
 assert not ops.check_switch(tmp_path,'old',engine=Engine())['allowed']
