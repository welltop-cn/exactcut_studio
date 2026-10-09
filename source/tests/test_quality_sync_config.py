import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'integrations/drama-material-hub/scripts/quality_sync.py'


def load_quality_sync(monkeypatch, tmp_path, name):
    env_file = tmp_path / 'empty.env'
    env_file.write_text('')
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(env_file))
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_project_env_file_respects_override_and_defaults_to_repo_env(monkeypatch, tmp_path):
    module = load_quality_sync(monkeypatch, tmp_path, 'quality_sync_project_env')
    override = tmp_path / 'custom.env'
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(override))
    assert module.project_env_file() == override
    monkeypatch.delenv('EXACTCUT_ENV_FILE', raising=False)
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'development')
    assert module.project_env_file() == REPO / 'source/.env.development'


def test_load_project_env_parses_export_quotes_comments_and_keeps_existing(monkeypatch, tmp_path):
    module = load_quality_sync(monkeypatch, tmp_path, 'quality_sync_load_env')
    env_file = tmp_path / 'values.env'
    env_file.write_text(
        '# a comment\n'
        'export EXPORTED=exported-value\n'
        'QUOTED="hello world"\n'
        "SINGLE='single value'\n"
        'EXISTING=from-file\n'
        'NO_EQUALS_LINE\n'
        'EMPTY=\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(env_file))
    for key in ('EXPORTED', 'QUOTED', 'SINGLE', 'EMPTY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('EXISTING', 'from-environment')
    module.load_project_env()
    assert os.environ['EXPORTED'] == 'exported-value'
    assert os.environ['QUOTED'] == 'hello world'
    assert os.environ['SINGLE'] == 'single value'
    assert os.environ['EMPTY'] == ''
    assert os.environ['EXISTING'] == 'from-environment'


def test_material_local_root_uses_override_and_repo_default(monkeypatch, tmp_path):
    module = load_quality_sync(monkeypatch, tmp_path, 'quality_sync_local_root')
    monkeypatch.delenv('MATERIAL_HUB_LOCAL_ROOT', raising=False)
    assert module.material_local_root() == REPO / 'source/material-local'
    monkeypatch.setenv('MATERIAL_HUB_LOCAL_ROOT', str(tmp_path / 'local'))
    assert module.material_local_root() == tmp_path / 'local'


def test_cf_login_requires_credentials_and_uses_username_fallback(monkeypatch, tmp_path):
    module = load_quality_sync(monkeypatch, tmp_path, 'quality_sync_cf_login')
    with pytest.raises(module.QualitySyncError):
        module.cf_login({'CF_ADS_PASSWORD': 'only-password'})

    posts = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {'data': {'token': 'token-value'}}

    class Session:
        def __init__(self):
            self.trust_env = True

        def post(self, url, json=None, timeout=None):
            posts.append((url, json, timeout))
            return Response()

    monkeypatch.setattr(module.requests, 'Session', Session)
    session, token = module.cf_login({'CF_ADS_USERNAME': 'fallback-user', 'CF_ADS_PASSWORD': 'pw'})
    assert token == 'token-value'
    assert session.trust_env is False
    assert posts[0][1] == {'username': 'fallback-user', 'password': 'pw'}

    posts.clear()
    module.cf_login({'CF_ADS_USER': 'primary-user', 'CF_ADS_USERNAME': 'fallback-user', 'CF_ADS_PASSWORD': 'pw'})
    assert posts[0][1]['username'] == 'primary-user'
