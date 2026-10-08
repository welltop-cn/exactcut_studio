from concurrent.futures import ThreadPoolExecutor
from test_phase1_recovery import workspace
from exactcut import workflow as w


def test_multiple_copies_reserve_unique_names_even_before_execution(workspace):
    source=workspace
    with ThreadPoolExecutor(max_workers=4) as pool:
        clones=list(pool.map(lambda _:w.clone_batch(source['id']),range(4)))
    names=[source['assets'][0]['filename']]+[c['assets'][0]['filename'] for c in clones]
    assert len(set(names))==5
    assert sorted(int(c['draft']['rows'][0]['seq']) for c in clones)==[2,3,4,5]
    assert all(c['draft']['rows'][0]['original_seq']=='1' for c in clones)
    assert all(w.runtime(w.folder(c['id']))['status']=='draft' for c in clones)


def test_edited_author_reserves_names_in_final_scope(workspace):
    clone=w.clone_batch(workspace['id'])
    clone['draft']['rows'][0]['seq']='1'
    saved=w.save_draft(clone['id'], {'revision':clone['revision'],'draft':clone['draft']})
    assert saved['assets'][0]['filename']!=workspace['assets'][0]['filename']
    assert saved['draft']['rows'][0]['seq']=='2'
