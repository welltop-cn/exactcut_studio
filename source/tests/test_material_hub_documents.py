import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

import pytest


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENV_FILE', str(tmp_path / 'empty.env'))
    script = Path(__file__).resolve().parents[2] / 'integrations/drama-material-hub/scripts/material_hub.py'
    spec = importlib.util.spec_from_file_location('portable_hub_documents', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_reader_uses_antiword_utf8_without_sidecar(hub, tmp_path, monkeypatch):
    source = tmp_path / '素材建议.doc'
    source.write_bytes(b'fixture')
    def convert(command, **kwargs):
        assert command == ['antiword', '-m', 'UTF-8.txt', str(source)]
        assert kwargs['encoding'] == 'utf-8'
        return subprocess.CompletedProcess(command, 0, '中文建议\nEarlier\n', '')
    monkeypatch.setattr(hub.subprocess, 'run', convert)
    assert hub.parse_text_file(source) == '中文建议\nEarlier\n'
    assert list(tmp_path.iterdir()) == [source]


def test_missing_doc_converter_reports_optional_dependency(hub, tmp_path, monkeypatch):
    source = tmp_path / 'suggestions.doc'
    source.write_bytes(b'fixture')
    def missing(*args, **kwargs):
        raise FileNotFoundError('antiword')
    monkeypatch.setattr(hub.subprocess, 'run', missing)
    with pytest.raises(hub.HubError, match='antiword'):
        hub.parse_text_file(source)


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired('antiword', 60),
                                 subprocess.CompletedProcess('antiword', 1, '', 'corrupt document')])
def test_doc_conversion_failure_is_a_hub_error(hub, tmp_path, monkeypatch, error):
    source = tmp_path / 'suggestions.doc'
    source.write_bytes(b'fixture')
    def fail(*args, **kwargs):
        if isinstance(error, Exception):
            raise error
        return error
    monkeypatch.setattr(hub.subprocess, 'run', fail)
    with pytest.raises(hub.HubError, match='DOC'):
        hub.parse_text_file(source)


def test_online_document_becomes_docx_without_external_converter(hub, tmp_path, monkeypatch):
    content = '中文正文 & <tag>\n\n  Leading spaces\nEmoji: 😀'
    client = hub.FeishuClient.__new__(hub.FeishuClient)
    client.headers = {}
    client._online_document_token = lambda url: 'fixture-token'
    client.session = Mock()
    client.session.get.return_value.json.return_value = {'code': 0, 'data': {'content': content}}
    def forbidden(*args, **kwargs):
        raise AssertionError('online DOCX generation must not spawn a system converter')
    monkeypatch.setattr(hub.subprocess, 'run', forbidden)
    path = tmp_path / 'nested' / 'script.docx'
    client.download_online_document('https://example.invalid/fixture', path)
    assert hub.parse_docx(path) == content
    with ZipFile(path) as archive:
        assert {'[Content_Types].xml', '_rels/.rels', 'word/document.xml'} <= set(archive.namelist())
    assert not path.with_suffix('.txt').exists()
