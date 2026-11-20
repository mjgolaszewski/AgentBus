"""Identity previews bind verified context; decoding is never execution authority."""
import copy
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from src.agentbus_identity_intent_service import (
    decode_identity_intent, encode_identity_intent, intent_digest, intent_mapping, require_fresh_intent,
)


def preview(**changes):
    value = {'version': 1, 'request_id': str(uuid4()), 'actor': 'slack:UOP', 'channel': 'C111',
             'chat_id': str(uuid4()), 'revision': 7, 'action': 'retire', 'created_at': 100.0,
             'expires_at': 500.0, 'name': None, 'session_ids': [str(uuid4())], 'positions': []}
    value.update(changes)
    return value


def decode(value):
    return decode_identity_intent(value, actor='slack:UOP', channel='C111')


def test_identity_intent_is_immutable_and_round_trips_without_side_effects():
    value = preview(action='retire_with_history', positions=[8, 2], session_ids=[str(uuid4()), str(uuid4())])
    before = copy.deepcopy(value)
    intent = decode(value)
    assert value == before and intent.positions == (2, 8)
    import json
    assert decode(json.loads(encode_identity_intent(intent))) == intent
    assert intent_digest(decode({**value, 'positions': [2, 8]})) == intent_digest(intent)
    with pytest.raises(FrozenInstanceError):
        intent.actor = 'slack:OTHER'


@pytest.mark.parametrize('change', [
    {'version': True}, {'version': 2}, {'actor': 'slack:OTHER'}, {'channel': 'C222'},
    {'revision': True}, {'revision': -1}, {'revision': 2**63}, {'action': 'delete_anything'},
    {'created_at': float('nan')}, {'expires_at': float('inf')}, {'created_at': True},
    {'expires_at': 100.0}, {'expires_at': 700.01}, {'name': 'unexpected'},
    {'request_id': 'not-a-uuid'}, {'chat_id': 'foreign'}, {'positions': [1]},
    {'session_ids': [str(uuid4())]*257}, {'positions': list(range(1, 102)), 'action': 'retire_with_history'},
    {'positions': [True], 'action': 'retire_with_history'},
    {'positions': [2, 2], 'action': 'retire_with_history'},
    {'session_ids': ['aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa']*2},
    {'name': 'Upper Case', 'action': 'rename'}, {'name': None, 'action': 'rename'},
    {'grant': 'not-authority'},
])
def test_identity_intent_rejects_substitution_and_unbounded_shapes(change):
    with pytest.raises(ValueError, match='Identity operation unavailable'):
        decode(preview(**change))


@pytest.mark.parametrize('instant', [99.0, 500.0, True, float('nan'), float('inf')])
def test_expired_or_unknown_preview_never_becomes_fresh_admission(instant):
    value = preview()
    intent = decode(value)  # Historical decoding remains available to receipt readers.
    with pytest.raises(ValueError):
        require_fresh_intent(intent, instant)
    assert intent_mapping(intent)['request_id'] == value['request_id']


def test_identity_rename_shape_and_freshness_do_not_select_another_scope():
    intent = decode(preview(action='rename', name='new-name'))
    require_fresh_intent(intent, 100.0)
    assert intent.name == 'new-name' and intent.channel == 'C111'
    with pytest.raises(ValueError):
        decode_identity_intent(intent_mapping(intent), actor='slack:UOP', channel='C222')
