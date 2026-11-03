"""Stage leases extend live ownership; closed/reused descriptors must disappear."""
from exactcut import cutting,processing_cancel


def test_closed_stage_descriptor_is_not_inherited_when_os_reuses_it(tmp_path):
    worker=cutting.lock_file(tmp_path/'worker.lock');stage=cutting.lock_file(tmp_path/'stage.lock')
    try:
        with processing_cancel.scope(lambda:False,leases=[worker,stage]):
            previous=stage.fileno();stage.close()
            with (tmp_path/'unrelated').open('w') as unrelated:
                assert unrelated.fileno()==previous
                assert unrelated.fileno() not in processing_cancel.process_options().get('pass_fds',())
                assert worker.fileno() in processing_cancel.process_options()['pass_fds']
    finally:stage.close();worker.close()


def test_frame_stage_retains_its_lease_in_addition_to_worker(tmp_path):
    worker=cutting.lock_file(tmp_path/'worker.lock')
    try:
        with processing_cancel.scope(lambda:False,leases=[worker]):
            with cutting.frame_lease(tmp_path/'frame.lock'):
                descriptors=processing_cancel.process_options()['pass_fds']
                assert len(descriptors)==3  # worker + per-key producer lock + shared short-media slot
                assert worker.fileno() in descriptors and len(set(descriptors))==3
            assert processing_cancel.process_options()['pass_fds']==(worker.fileno(),)
    finally:worker.close()


def test_nested_stage_lease_retains_outer_job_and_removes_only_itself(tmp_path):
    worker=cutting.lock_file(tmp_path/'worker.lock');cache=cutting.lock_file(tmp_path/'cache.lock')
    try:
        with processing_cancel.scope(lambda:False,leases=[worker]):
            with processing_cancel.retain([cache]):
                assert set(processing_cancel.process_options()['pass_fds'])=={worker.fileno(),cache.fileno()}
            assert processing_cancel.process_options()['pass_fds']==(worker.fileno(),)
        assert processing_cancel.process_options()=={}
    finally:cache.close();worker.close()
