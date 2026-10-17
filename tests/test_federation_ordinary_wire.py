"""Explicit ordinary successor preserves local ancestry authority boundaries."""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agentbus_federation_ordinary_service import decode_ordinary, encode_ordinary
from src.agentbus_federation_wire_service import Binding, Identity, decode_frame, encode_frame
from src.agentbus_service import SendMessage


def root():
    return SendMessage(sender='home:agentbus:a', recipient='remote:agentbus:b', text='root')


def test_ordinary_wrapper_requires_canonical_explicit_parent_and_no_foreign_ancestry():
    payload = encode_ordinary(root(), None)
    decoded = decode_ordinary(payload)
    assert decoded.parent is None and decoded.message['text'] == 'root'
    raw = json.loads(payload)
    for changed in [dict(raw, version=True), {'version': 1, 'message': raw['message']},
                    dict(raw, unexpected=1), dict(raw, message=dict(raw['message'], reply_to_cursor=1)),
                    dict(raw, message=dict(raw['message'], thread_ts='1.000001'))]:
        with pytest.raises(ValueError):
            decode_ordinary(json.dumps(changed, sort_keys=True, separators=(',', ':')))
    with pytest.raises(ValueError):
        decode_ordinary(json.dumps(raw, indent=2))


@pytest.mark.parametrize('parent', [
    {}, {'origin_instance_id': 'home', 'origin_message_id': 'x', 'destination_instance_id': 'remote', 'cursor': 1},
    {'origin_instance_id': '../home', 'origin_message_id': 'x', 'destination_instance_id': 'remote'},
    {'origin_instance_id': 'home', 'origin_message_id': '', 'destination_instance_id': 'remote'},
    {'origin_instance_id': 'home', 'origin_message_id': 'x' * 129, 'destination_instance_id': 'remote'},
])
def test_ordinary_parent_is_bounded_opaque_identity_not_local_cursor(parent):
    with pytest.raises(ValueError):
        encode_ordinary(root(), parent)


def test_wire_three_requires_explicit_peer_opt_in_and_keeps_legacy_versions():
    key = Ed25519PrivateKey.generate()
    identity = Identity('home', 'home', 'home-publisher', key)
    legacy = Binding('home', 'home', 'home-publisher', key.public_key())
    approved = Binding('home', 'home', 'home-publisher', key.public_key(), ordinary_version=3)
    payload = encode_ordinary(root(), None)
    text = encode_frame(identity, 'message', 'm1', 'remote', payload, version=3)
    with pytest.raises(ValueError):
        decode_frame(text, 'home-publisher', 'remote', {'home': legacy})
    assert decode_frame(text, 'home-publisher', 'remote', {'home': approved})['agentbus_federation'] == 3
    assert json.loads(encode_frame(identity, 'message', 'legacy', 'remote', '{}'))['agentbus_federation'] == 1
    assert json.loads(encode_frame(identity, 'receipt', 'receipt', 'remote', '{}'))['agentbus_federation'] == 1
    with pytest.raises(ValueError):
        encode_frame(identity, 'receipt', 'bad', 'remote', '{}', version=3)


@pytest.mark.parametrize('version', [True, 1, 2, 4, '3'])
def test_ordinary_binding_never_coerces_or_infers_version(version):
    with pytest.raises(ValueError):
        Binding('home', 'home', 'publisher', Ed25519PrivateKey.generate().public_key(), ordinary_version=version)
