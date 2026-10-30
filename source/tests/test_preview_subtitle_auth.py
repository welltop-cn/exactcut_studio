"""Queued actors must remain verifiable before a paid recognition call."""
import sqlite3

import pytest

from exactcut import accounts, preview_subtitles as subtitles


@pytest.mark.parametrize('actor', ['admin', 'producer'])
def test_missing_account_database_never_grants_a_queued_actor_access(tmp_path, monkeypatch, actor):
    database = tmp_path/'missing.sqlite3'
    monkeypatch.setattr(subtitles.queue, 'accounts_path', lambda: database)
    assert subtitles._actor_user(actor) is None
    assert not database.exists()
    assert subtitles._invalid({'batch_id':'a'*32, 'request_id':'R1', 'actor':actor})


def test_unreadable_account_database_fails_closed(tmp_path, monkeypatch):
    database = tmp_path/'accounts.sqlite3'
    database.write_text('not a sqlite database')
    monkeypatch.setattr(subtitles.queue, 'accounts_path', lambda: database)
    assert subtitles._actor_user('admin') is None


def test_queued_actor_uses_current_account_enabled_state(tmp_path, monkeypatch):
    database = tmp_path/'accounts.sqlite3'
    accounts.initialize(database, 'independent-test-password')
    monkeypatch.setattr(subtitles.queue, 'accounts_path', lambda: database)
    assert subtitles._actor_user('admin')['enabled'] == 1
    with sqlite3.connect(database) as db:
        db.execute('UPDATE users SET enabled=0 WHERE username=?', ('admin',))
    assert subtitles._actor_user('admin')['enabled'] == 0
    assert subtitles._invalid({'batch_id':'a'*32, 'request_id':'R1', 'actor':'admin'})
    assert subtitles._actor_user('missing') is None
