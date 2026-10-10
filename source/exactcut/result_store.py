"""Version-1 request results; legacy batch results remain readable."""
from contextlib import contextmanager
import fcntl
from .util import read_json, write_json_atomic

EXECUTION_FIELDS = ('executor_running', 'heartbeat_at', 'execution_started_at', 'execution_ended_at')


def file_for(directory, request_id):
    if (not isinstance(request_id, str) or not request_id or request_id in {'.', '..'}
            or '/' in request_id or '\\' in request_id or '\x00' in request_id):
        raise ValueError('无效素材 ID')
    return directory / 'results' / (request_id + '.json')


def read(directory):
    legacy = directory / 'results.json'
    result = read_json(legacy) if legacy.exists() else {'schema_version': 1, 'requests': {}}
    result.setdefault('requests', {})
    for file in (directory / 'results').glob('*.json'):
        result['requests'][file.stem] = read_json(file)
    return result


@contextmanager
def locked(directory, request_id):
    file = file_for(directory, request_id)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.with_suffix('.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield file


def patch(directory, request_id, **fields):
    with locked(directory, request_id) as file:
        previous = read_json(file) if file.exists() else read(directory)['requests'].get(request_id, {})
        previous.update(fields)
        write_json_atomic(file, previous)
        return previous


def put(directory, request_id, record):
    # Render updates must not overwrite a heartbeat from the execution thread.
    with locked(directory, request_id) as file:
        previous = read_json(file) if file.exists() else {}
        active = {key: previous[key] for key in EXECUTION_FIELDS if key in previous} if previous.get('executor_running') else {}
        record = {**record, **active}
        write_json_atomic(file, record)
        return record
