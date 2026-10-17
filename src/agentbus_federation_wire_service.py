"""Bounded signed federation frames and operator-owned publisher bindings."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

MAX_FRAME_BYTES = 39_000
INSTANCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
FIELDS = {'agentbus_federation', 'kind', 'origin_instance_id', 'origin_message_id',
          'destination_instance_id', 'key_generation', 'payload', 'signature'}


class SignedFrame(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    agentbus_federation: Literal[1]
    kind: Literal['message', 'receipt']
    origin_instance_id: str
    origin_message_id: str
    destination_instance_id: str
    key_generation: str = Field(pattern=r'^[0-9a-f]{64}$')
    payload: str
    signature: str


class DirectoryFrame(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    agentbus_federation: Literal[2]
    kind: Literal['directory']
    origin_instance_id: str
    origin_message_id: str
    destination_instance_id: str
    key_generation: str = Field(pattern=r'^[0-9a-f]{64}$')
    payload: str
    signature: str


class OrdinaryFrame(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    agentbus_federation: Literal[3]
    kind: Literal['message']
    origin_instance_id: str
    origin_message_id: str
    destination_instance_id: str
    key_generation: str = Field(pattern=r'^[0-9a-f]{64}$')
    payload: str
    signature: str


@dataclass(frozen=True)
class Binding:
    instance_id: str
    route_prefix: str
    publisher_id: str
    public_key: Ed25519PublicKey = field(repr=False)
    directory_version: int | None = None
    ordinary_version: int | None = None

    def __post_init__(self):
        _validate_identity(self.instance_id, self.route_prefix, self.publisher_id)
        if self.directory_version is not None and (type(self.directory_version) is not int or self.directory_version != 2):
            raise ValueError("unsupported directory peer version")
        if self.ordinary_version is not None and (type(self.ordinary_version) is not int or self.ordinary_version != 3):
            raise ValueError('unsupported ordinary peer version')


@dataclass(frozen=True)
class Identity:
    instance_id: str
    route_prefix: str
    publisher_id: str
    private_key: Ed25519PrivateKey = field(repr=False)

    def __post_init__(self):
        _validate_identity(self.instance_id, self.route_prefix, self.publisher_id)


def _validate_identity(instance: str, prefix: str, publisher: str) -> None:
    if (not isinstance(instance, str) or not INSTANCE.fullmatch(instance)
            or not isinstance(prefix, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', prefix)
            or not isinstance(publisher, str) or not 1 <= len(publisher) <= 256):
        raise ValueError('invalid operator federation identity')


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True, allow_nan=False)


def key_generation(public_key: Ed25519PublicKey) -> str:
    return hashlib.sha256(public_key.public_bytes_raw()).hexdigest()


def encode_frame(identity: Identity, kind: str, message_id: str,
                 destination: str, payload: str, *, version: int | None = None) -> str:
    wire_version = (2 if kind == 'directory' else 1) if version is None else version
    frame = {'agentbus_federation': wire_version, 'kind': kind, 'origin_instance_id': identity.instance_id,
             'origin_message_id': message_id, 'destination_instance_id': destination,
             'key_generation': key_generation(identity.private_key.public_key()), 'payload': payload}
    _validate(dict(frame, signature='pending'))
    frame['signature'] = base64.b64encode(identity.private_key.sign(canonical(frame).encode())).decode()
    text = canonical(frame)
    for literal, escape in (('<', r'\u003c'), ('>', r'\u003e'), ('&', r'\u0026'), ('/', r'\/')):
        text = text.replace(literal, escape)
    if len(text.encode()) > MAX_FRAME_BYTES:
        raise ValueError('federation frame exceeds limit')
    return text


def _validate(frame: dict) -> None:
    if frame.get('agentbus_federation') == 3:
        OrdinaryFrame(**frame)
    elif frame.get("agentbus_federation") == 2:
        DirectoryFrame(**frame)
    else:
        SignedFrame(**frame)
    if set(frame) != FIELDS or type(frame['agentbus_federation']) is not int or frame['agentbus_federation'] not in {1, 2, 3}:
        raise ValueError('unsupported federation frame')
    if frame['kind'] not in {'message', 'receipt', 'directory'}:
        raise ValueError('unsupported federation frame kind')
    for key in ('origin_instance_id', 'destination_instance_id'):
        if not isinstance(frame[key], str) or not INSTANCE.fullmatch(frame[key]):
            raise ValueError('invalid federation instance')
    if not isinstance(frame['origin_message_id'], str) or not 1 <= len(frame['origin_message_id']) <= 128:
        raise ValueError('invalid federation message identity')
    if not isinstance(frame['payload'], str) or not frame['payload']:
        raise ValueError('invalid federation payload')
    if not isinstance(frame['signature'], str) or len(frame['signature']) > 128:
        raise ValueError('invalid federation signature')


def decode_frame(text: str, publisher: str, destination: str, bindings: dict[str, Binding]) -> dict:
    if not isinstance(text, str) or len(text.encode()) > MAX_FRAME_BYTES:
        raise ValueError('federation frame exceeds limit')
    try:
        frame = json.loads(text)
        if not isinstance(frame, dict):
            raise ValueError('invalid federation frame')
        _validate(frame)
        binding = bindings.get(frame['origin_instance_id'])
        if binding is None or binding.publisher_id != publisher or frame['destination_instance_id'] != destination:
            raise ValueError('untrusted federation publisher or destination')
        if frame['agentbus_federation'] == 2 and binding.directory_version != 2:
            raise ValueError('directory peer version is not operator-approved')
        if frame['agentbus_federation'] == 3 and binding.ordinary_version != 3:
            raise ValueError('ordinary peer version is not operator-approved')
        if frame['key_generation'] != key_generation(binding.public_key):
            raise ValueError('untrusted federation key generation')
        signature = base64.b64decode(frame['signature'], validate=True)
        unsigned = {key: value for key, value in frame.items() if key != 'signature'}
        binding.public_key.verify(signature, canonical(unsigned).encode())
    except (InvalidSignature, TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError('invalid federation signature or frame') from exc
    return frame
