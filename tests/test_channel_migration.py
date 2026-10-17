"""Offline channel migration preserves legacy identities, ancestry and custody."""
import sqlite3

import pytest


def legacy_database(path):
    from src.agentbus_service import MessageStore, SendMessage
    store = MessageStore(path)
    chat, session, revision = store.participation.enroll(repo='agentbus',route='home:agentbus:old',
        display_name='Old',session_secret='legacy-secret-'.ljust(40,'x'))
    store.participation.acknowledge_policy(session, 'legacy-secret-'.ljust(40,'x'), revision)
    parent = store.append('C111','100.1',SendMessage(sender='home:agentbus:old',text='parent'))
    store.append('C111','100.2',SendMessage(sender='home:agentbus:old',text='reply',
        recipient='home:agentbus:old',audience='direct',reply_to_cursor=parent.cursor,thread_ts='100.1'))
    identity = store.info('C111').inbox_id
    store.close()
    return chat,session,identity


def test_offline_migration_is_atomic_dry_run_preserves_bytes_and_repeatable(tmp_path):
    from src.agentbus_channel_migration_service import migrate_channels
    path = tmp_path/'legacy.sqlite3'
    chat,session,identity = legacy_database(path)
    before = path.read_bytes()
    report = migrate_channels(path, legacy_channel='C111', channels=('C111','C222'), apply=False)
    assert report['legacy_channel'] == 'C111'
    assert path.read_bytes() == before
    migrate_channels(path, legacy_channel='C111', channels=('C111','C222'), apply=True)
    from src.agentbus_service import MessageStore
    store = MessageStore(path)
    assert store.channels.channel_for_chat(chat) == 'C111'
    assert store.channels.inbox_id('C111') == identity
    assert store.participation.session_state(session)['state'] == 'active'
    assert [store.channels.public_cursor('C111', m.cursor) for m in store.read('C111').messages] == [1,2]
    assert store.read('C111').messages[1].reply_to_cursor == 1
    store.close()
    migrate_channels(path, legacy_channel='C111', channels=('C111','C222'), apply=True)
    db = sqlite3.connect(path)
    assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
    db.close()


def test_migration_refuses_unexplained_other_channel_data(tmp_path):
    from src.agentbus_channel_migration_service import migrate_channels

    from src.agentbus_service import MessageStore, SendMessage
    path = tmp_path/'ambiguous.sqlite3'
    legacy_database(path)
    store=MessageStore(path)
    store.append('C999','100.1',SendMessage(sender='unknown',text='unexplained'))
    store.close()
    before=path.read_bytes()
    with pytest.raises(ValueError, match='channel|ambiguous'):
        migrate_channels(path,legacy_channel='C111',channels=('C111','C222'),apply=True)
    assert path.read_bytes() == before
