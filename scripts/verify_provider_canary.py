#!/usr/bin/env python3
"""Verify the bounded historical canary corpus; never authorize deployment or semantic ACK."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

SOURCE = '07b9ecb5e478ce0728b3a788071d50469294c327'
TREE = 'd262f84243282741292bcc3befd7f29ece43f17f'
ARCHIVE = '1d6f42543c85d408d480d923e2fc04445766a99075d0d66a99943218c3d466b8'
PINS = {
    'home': ('A0C4FA6MK7V/B0C4GE61DV3/C0C4GDC7Q4D',
             'e58bbcb53d763c85f6ad3c42c73c0b2038773b4d845544900664a36b02fe2cc6'),
    'moranwm': ('A0CAGUPHCV6/B0C9QG8AJV7/C0C4GDC7Q4D',
                '13d2ef7ecf2b4101ef94f08755935977fb2268c5c26417153d8bdc8e993423a7'),
    'home-latejoin': ('A0C9V935APQ/B0C9Z5GLLAD/C0C4GDC7Q4D',
                      'a3eeee3e930cd51c125254d8bc133b789b07088b988512e8cb91ac2f211f3d0b'),
}
ROOTS = {
    'ee58f140-10a9-4294-a808-716ef52368f0': ('home', 'moranwm'),
    '975dc50a-3f2e-4d71-8b3e-09238f4facf8': ('home', 'home-latejoin'),
    'dce002c6-8d25-4be1-849d-d7499f477a3c': ('moranwm', 'home'),
    'c6086950-be18-4d45-a60a-44af7b174a71': ('moranwm', 'home-latejoin'),
    '0b97ec1d-e036-4ba7-9e74-25551aa542f2': ('home-latejoin', 'home'),
    '0a8eedb1-364f-4689-8b0a-4b02405fd081': ('home-latejoin', 'moranwm'),
    'd2d94f21-c292-4ebe-9977-0d0d69208c25': ('home', 'moranwm'),
}


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _frame(row: dict, bindings: dict) -> dict:
    from src.agentbus_federation_wire_service import decode_frame

    wire = row['wire']
    _require(isinstance(wire, str), 'carrier must be original text')
    _require(hashlib.sha256(wire.encode()).hexdigest() == row['wire_sha256'], 'carrier hash changed')
    reference = row['actual_provider_ref']
    _require(isinstance(reference, str) and re.fullmatch(r'[1-9][0-9]{0,15}\.[0-9]{1,9}', reference) is not None,
             'provider reference must retain its actual timestamp shape')
    stamp = Decimal(reference)
    _require(stamp.is_finite() and Decimal('1792195680') <= stamp < Decimal('1792196280'),
             'provider reference is outside the observed window')
    frame = decode_frame(wire, row['actual_publisher_id'], row['destination_instance_id'], bindings)
    _require(frame['agentbus_federation'] == 1, 'historical scope requires wire1')
    common = {'origin_instance_id', 'origin_message_id', 'destination_instance_id', 'actual_provider_ref',
              'actual_publisher_id', 'wire_sha256', 'wire', 'verified_signature'}
    expected_fields = common | ({'wire_bytes', 'matches_local_retained_frame'} if frame['kind'] == 'message' else {'receipt'})
    _require(set(row) == expected_fields, 'unexpected observation fields')
    _require(frame['origin_instance_id'] == row['origin_instance_id']
             and frame['origin_message_id'] == row['origin_message_id'], 'observation identity changed')
    return frame


def verify_evidence(data: dict) -> dict:
    """Authenticate retained origins/receipts, preserving their explicit historical scope."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from src.agentbus_federation_wire_service import Binding, canonical, key_generation

    _require(isinstance(data, dict) and set(data) == {
        'document', 'scope', 'operator_bindings', 'carriers', 'receipts', 'observations', 'limitations'},
        'unsupported evidence shape')
    _require(type(data['document']['version']) is int and data['document']['version'] == 1,
             'unsupported evidence version')
    scope = data['scope']
    _require(scope['source_commit'] == SOURCE and scope['source_tree'] == TREE
             and scope['released_archive_sha256'] == ARCHIVE, 'historical subject changed')
    _require(type(scope['message_wire_version']) is int and scope['message_wire_version'] == 1
             and scope['semantic_acknowledgement'] is False
             and scope['final_wire3_qualification'] is False, 'historical proof cannot confer new authority')
    _require(scope['window_start'] == '1792195680.000000' and scope['window_end'] == '1792196280.000000',
             'historical window changed')
    _require(scope['correlation_id'] == 'p11-q-20261016-07b9ecb'
             and scope['provider_channel'] == 'C0C4GDC7Q4D', 'historical arrangement changed')
    bindings = {}
    _require(isinstance(data['operator_bindings'], list) and len(data['operator_bindings']) == 3,
             'exact operator origins required')
    for row in data['operator_bindings']:
        origin = row['instance_id']
        _require(origin in PINS and origin not in bindings, 'unknown or duplicate origin')
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(row['ed25519_public_bytes_hex']))
        _require((row['publisher_id'], row['key_generation']) == PINS[origin]
                 and key_generation(public) == row['key_generation'] and row['route_prefix'] == origin,
                 'operator publisher or generation changed')
        bindings[origin] = Binding(origin, origin, row['publisher_id'], public)
    _require(isinstance(data['carriers'], list) and len(data['carriers']) == 7, 'seven original carriers required')
    roots = {}
    directions = set()
    for row in data['carriers']:
        frame = _frame(row, bindings)
        message_id = frame['origin_message_id']
        pair = (frame['origin_instance_id'], frame['destination_instance_id'])
        _require(frame['kind'] == 'message' and message_id in ROOTS and ROOTS[message_id] == pair
                 and message_id not in roots, 'unknown, duplicate or redirected canary origin')
        body = json.loads(frame['payload'])
        _require(body['correlation_id'] == scope['correlation_id'] and body['audience'] == 'direct'
                 and body['sender'] == pair[0] + ':agentbus:qualification'
                 and body['recipient'] == pair[1] + ':agentbus:qualification', 'canary route/content changed')
        _require(type(row['wire_bytes']) is int and len(row['wire'].encode()) == row['wire_bytes'],
                 'carrier size changed')
        roots[message_id] = frame
        directions.add(pair)
    _require(set(roots) == set(ROOTS) and len(directions) == 6, 'fan-out evidence is incomplete')
    _require(isinstance(data['receipts'], list) and len(data['receipts']) == 7, 'seven original receipts required')
    acknowledged = set()
    for row in data['receipts']:
        frame = _frame(row, bindings)
        _require(frame['kind'] == 'receipt', 'destination receipt required')
        receipt = json.loads(frame['payload'])
        _require(receipt == row['receipt'], 'claimed receipt attribution changed')
        message_id = receipt['message']
        _require(message_id in roots and message_id not in acknowledged, 'unknown or duplicate receipt')
        root = roots[message_id]
        expected = {'origin': root['origin_instance_id'], 'message': message_id,
                    'destination': root['destination_instance_id'],
                    'payload_hash': hashlib.sha256(root['payload'].encode()).hexdigest()}
        _require(receipt == expected and frame['origin_instance_id'] == expected['destination']
                 and frame['destination_instance_id'] == expected['origin'], 'receipt does not bind exact destination custody')
        expected_id = 'receipt-' + hashlib.sha256(canonical([
            expected['origin'], message_id, expected['destination']]).encode()).hexdigest()
        _require(frame['origin_message_id'] == expected_id, 'receipt identity changed')
        acknowledged.add(message_id)
    _require(acknowledged == set(roots), 'missing destination custody')
    return {'source': SOURCE, 'carriers': 7, 'receipts': 7, 'directions': 6, 'scope': 'historical_wire1_only'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=Path('audits/p11-provider-canary-07b9ecb.yml'))
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        if args.path.stat().st_size > 65536:
            raise ValueError('evidence exceeds byte budget')
        result = verify_evidence(yaml.safe_load(args.path.read_text()))
    except (ValueError, OSError, KeyError, TypeError, InvalidOperation, yaml.YAMLError):
        print('provider-canary-evidence-invalid', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
