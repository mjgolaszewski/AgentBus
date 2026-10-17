#!/usr/bin/env python3
"""Authenticate bounded retained provider corpora; never authorize deployment or semantic ACK."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
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
QUALIFIED_SOURCE = '00eecbe82e28fcfc12936f11d70ec3c41186bc31'
QUALIFIED_ROOTS = {
    '4e06348d-407c-4463-94a8-f6945e5dc4b9': ('home', 'moranwm'),
    '8917cdd3-08b3-48b6-b303-410e3a4df253': ('home', 'home-latejoin'),
    '62ea104b-c628-44f5-ab33-e4129b81d5d0': ('home-latejoin', 'home'),
    'e14198ce-a495-49d1-9cf2-16cc6775fd56': ('home-latejoin', 'moranwm'),
    '50a43085-e0b9-4727-9be9-ff05566a4e70': ('moranwm', 'home'),
    '55569fb8-4788-4b71-ad2e-a0264763d101': ('moranwm', 'home-latejoin'),
}
QUALIFIED_PARENTS = {
    '9ec3eedc-f747-4a9a-b4c3-6da13c93f5f0': '50a43085-e0b9-4727-9be9-ff05566a4e70',
    '85bd3880-7869-4685-a3d7-36c5d19b2ac2': '62ea104b-c628-44f5-ab33-e4129b81d5d0',
    'e65fb195-b894-45d4-9e82-0249cc342c37': '4e06348d-407c-4463-94a8-f6945e5dc4b9',
    '52e4a6bd-82d0-46ee-8a11-3b9ea72c4d91': 'e14198ce-a495-49d1-9cf2-16cc6775fd56',
    'f0f8f802-3ca8-421e-b2e1-f043db2e138c': '8917cdd3-08b3-48b6-b303-410e3a4df253',
    'b53315d7-d6c8-4e0e-9255-e943544f28d0': '55569fb8-4788-4b71-ad2e-a0264763d101',
}


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _bindings(rows: list, *, ordinary: bool = False) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from src.agentbus_federation_wire_service import Binding, key_generation

    _require(isinstance(rows, list) and len(rows) == 3, 'exact operator origins required')
    result = {}
    for row in rows:
        fields = {'instance_id', 'route_prefix', 'publisher_id', 'key_generation', 'ed25519_public_bytes_hex'}
        _require(set(row) == fields | ({'directory_version', 'ordinary_version'} if ordinary else set()),
                 'unexpected operator binding fields')
        origin = row['instance_id']
        _require(origin in PINS and origin not in result, 'unknown or duplicate origin')
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(row['ed25519_public_bytes_hex']))
        _require((row['publisher_id'], row['key_generation']) == PINS[origin]
                 and key_generation(public) == row['key_generation'] and row['route_prefix'] == origin,
                 'operator publisher or generation changed')
        if ordinary:
            _require(type(row['directory_version']) is int and row['directory_version'] == 2
                     and type(row['ordinary_version']) is int and row['ordinary_version'] == 3,
                     'explicit operator wire versions required')
        result[origin] = Binding(origin, origin, row['publisher_id'], public,
                                 directory_version=2 if ordinary else None,
                                 ordinary_version=3 if ordinary else None)
    return result


def _decode_observation(row: dict, bindings: dict, start: str, end: str) -> dict:
    from src.agentbus_federation_wire_service import decode_frame

    wire = row['wire']
    _require(isinstance(wire, str), 'carrier must be original text')
    _require(hashlib.sha256(wire.encode()).hexdigest() == row['wire_sha256'], 'carrier hash changed')
    reference = row['actual_provider_ref']
    _require(isinstance(reference, str) and re.fullmatch(r'[1-9][0-9]{0,15}\.[0-9]{1,9}', reference) is not None,
             'provider reference must retain its actual timestamp shape')
    stamp = Decimal(reference)
    _require(stamp.is_finite() and Decimal(start) <= stamp < Decimal(end),
             'provider reference is outside the observed window')
    frame = decode_frame(wire, row['actual_publisher_id'], row['destination_instance_id'], bindings)
    _require(frame['origin_instance_id'] == row['origin_instance_id']
             and frame['origin_message_id'] == row['origin_message_id'], 'observation identity changed')
    return frame


def _frame(row: dict, bindings: dict) -> dict:
    frame = _decode_observation(row, bindings, '1792195680', '1792196280')
    _require(frame['agentbus_federation'] == 1, 'historical scope requires wire1')
    common = {'origin_instance_id', 'origin_message_id', 'destination_instance_id', 'actual_provider_ref',
              'actual_publisher_id', 'wire_sha256', 'wire', 'verified_signature'}
    expected_fields = common | ({'wire_bytes', 'matches_local_retained_frame'} if frame['kind'] == 'message' else {'receipt'})
    _require(set(row) == expected_fields, 'unexpected observation fields')
    return frame


def _verify_historical(data: dict) -> dict:
    """Authenticate retained origins/receipts, preserving their explicit historical scope."""
    from src.agentbus_federation_wire_service import canonical

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
    bindings = _bindings(data['operator_bindings'])
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


def _moment(value: str) -> Decimal:
    _require(isinstance(value, str), 'observation time must be an actual ISO timestamp')
    instant = datetime.fromisoformat(value)
    _require(instant.tzinfo is not None, 'observation time requires its timezone')
    instant = instant.astimezone(timezone.utc)
    return Decimal(int(instant.timestamp())) + Decimal(instant.microsecond) / Decimal(1_000_000)


def _qualified_observations(observed: dict) -> None:
    _require(Decimal('1792205100') <= _moment(observed['root_gate_pass_at']) <= Decimal('1792205340'),
             'root receipt deadline missed')
    _require(_moment(observed['root_gate_pass_at']) <= _moment(observed['all_origins_gate_pass_at']) < Decimal('1792205760'),
             'reply receipt deadline missed')
    _require(Decimal('1792205760') <= _moment(observed['withdrawal_gate_pass_at']) < Decimal('1792206300'),
             'withdrawal must converge inside the window')
    _require(observed['offline_at'] == '2026-10-17T02:49:16.099396+00:00'
             and observed['restart_at'] == '2026-10-17T02:52:03.291848+00:00', 'offline interval changed')
    _require(Decimal(observed['checkpoint_after_restart']) > Decimal(observed['checkpoint_before_offline'])
             and observed['checkpoint_before_offline'] == '1792205315.327409', 'recovery checkpoint regressed')
    _require(observed['recovered_reply_parent_cursors'] == {'home_reply': 110, 'moranwm_reply': 111},
             'recovered replies require the actual local parent mapping')
    _require(observed['stopped_replay_statuses'] == {origin: 423 for origin in PINS},
             'all stopped sessions must reject exact replay')
    rollback = observed['rollback']
    _require(rollback['prior_core_commit'] == '1be62d305db1f03a20543ae732ee00412755528f'
             and rollback['federation_disabled'] is True and rollback['late_service_stopped'] is True
             and rollback['latest_primary_preserved'] is True and rollback['checkpoint_reset'] is False
             and rollback['backup_restored_over_new_custody'] is False, 'rollback lost custody or authority boundaries')
    times = {key: _moment(rollback[key]) for key in ['requested_at', 'late_stopped_at', 'home_stopped_at',
             'home_prior_core_healthy_at', 'moranwm_prior_core_healthy_at']}
    _require(times['requested_at'] == Decimal('1792206285')
             and all(times['requested_at'] <= value < Decimal('1792206300') for value in times.values())
             and times['home_stopped_at'] < times['home_prior_core_healthy_at'], 'rollback missed fixed end')


def _runtime_applicability(runtime_files: dict) -> dict:
    """Permit only the reviewed 0.8.0 -> 0.9.0 release-version literals."""
    root = Path(__file__).resolve().parents[1]
    contents = {}
    for name in runtime_files:
        path = root / name
        _require(path.resolve().is_relative_to(root) and not path.is_symlink() and path.is_file(),
                 'qualified runtime path changed')
        contents[name] = path.read_bytes()
    declarations = {
        'uv.lock': (b'\nname = "superworkspace-agentbus"\nversion = "0.8.0"\nsource = { virtual = "." }\n',
                    b'\nname = "superworkspace-agentbus"\nversion = "0.9.0"\nsource = { virtual = "." }\n'),
        'pyproject.toml': (b'\nversion = "0.8.0"\n', b'\nversion = "0.9.0"\n'),
        'src/agentbus_service.py': (b'FastAPI(title="AgentBus", version="0.8.0", lifespan=lifespan,',
                                    b'FastAPI(title="AgentBus", version="0.9.0", lifespan=lifespan,'),
    }
    baseline = all(contents[name].count(old) == 1 and contents[name].count(new) == 0
                   for name, (old, new) in declarations.items())
    release = all(contents[name].count(new) == 1 and contents[name].count(old) == 0
                  for name, (old, new) in declarations.items())
    _require(baseline or release, 'mixed or unsupported release metadata')
    if release:
        for name, (old, new) in declarations.items():
            contents[name] = contents[name].replace(new, old, 1)
    for name, digest in runtime_files.items():
        raw = contents[name]
        _require(hashlib.sha256(raw).hexdigest() == digest,
                 'retained provider evidence does not qualify changed runtime bytes')
    result: dict = {'runtime_files_match': 51 if release else 54}
    if release:
        result.update(release_metadata_files_match=3, release_metadata_transition='0.8.0->0.9.0')
    return result


def _verify_qualified(data: dict) -> dict:
    from src.agentbus_federation_ordinary_service import decode_ordinary
    from src.agentbus_federation_wire_service import canonical

    _require(set(data) == {'document', 'scope', 'operator_bindings', 'carrier_observations', 'observations', 'limitations', 'runtime_files'},
             'unsupported qualified evidence shape')
    runtime_files = data['runtime_files']
    _require(isinstance(runtime_files, dict) and len(runtime_files) == 54
             and hashlib.sha256(canonical(runtime_files).encode()).hexdigest()
             == '50174ce1438696da8016ea810b5a3c325ecdd6d05180c189b499088a5a98a493',
             'qualified archive runtime manifest changed')
    runtime_applicability = _runtime_applicability(runtime_files)
    _require(data['document'] == {'kind': 'released_runtime_provider_canary_evidence',
             'id': 'p11-provider-canary-00eecbe', 'version': 2, 'path': 'audits/p11-provider-canary-00eecbe.yml'},
             'qualified document identity changed')
    scope = data['scope']
    _require(scope['source_commit'] == QUALIFIED_SOURCE
             and scope['source_tree'] == 'b95264e19f2db0b40a73b1883dea77d44c1ca19f'
             and scope['released_archive_sha256'] == '8c99f9258c852e8574e1800dd65e3480b7fe9bdf4d63e6c4062d91ead35f0d20',
             'qualified runtime subject changed')
    _require(scope['semantic_acknowledgement'] is False and scope['computed_phase_closure'] is False
             and scope['deployment_authority'] is False, 'provider proof cannot confer new authority')
    _require(scope['window_start'] == '1792205100.000000' and scope['window_end'] == '1792206300.000000'
             and scope['correlation_id'] == 'p11-v3-r2-00eecbe' and scope['provider_channel'] == 'C0C4GDC7Q4D'
             and scope['provider_audience'] == 'public', 'qualified arrangement changed')
    _require(all(type(scope[key]) is int and scope[key] == version for key, version in
                 [('message_wire_version', 3), ('receipt_wire_version', 1), ('directory_wire_version', 2)]),
             'qualified wire versions changed')
    bindings = _bindings(data['operator_bindings'], ordinary=True)
    expected = QUALIFIED_ROOTS | {mid: tuple(reversed(QUALIFIED_ROOTS[parent]))
                                  for mid, parent in QUALIFIED_PARENTS.items()}
    rows = data['carrier_observations']
    _require(isinstance(rows, list) and 36 <= len(rows) <= 128, 'bounded complete corpus required')
    payloads: dict[tuple[str, str, str], str] = {}
    wires: dict[tuple[str, str, str], str] = {}
    messages: set[str] = set()
    receipts: dict[tuple[str, str, str], dict] = {}
    receipt_references: dict[tuple[str, str, str], set[Decimal]] = {}
    directories: set[tuple[str, str]] = set()
    references: dict[str, set[str]] = {}
    replies: list[tuple[str, str, str | None, Decimal]] = []
    directory_payload = canonical({'directory': 1, 'operation': 'snapshot', 'revision': 7, 'entries': []})
    directory_id = 'directory-' + hashlib.sha256(directory_payload.encode()).hexdigest()
    fields = {'origin_instance_id', 'origin_message_id', 'destination_instance_id', 'kind', 'actual_provider_ref',
              'actual_publisher_id', 'provider_subtype', 'provider_thread_ref', 'wire_sha256', 'wire'}
    for row in rows:
        _require(isinstance(row, dict) and set(row) == fields, 'unexpected qualified observation fields')
        frame = _decode_observation(row, bindings, scope['window_start'], scope['window_end'])
        _require(frame['kind'] == row['kind'], 'observation kind changed')
        origin, mid, destination = frame['origin_instance_id'], frame['origin_message_id'], frame['destination_instance_id']
        key = (origin, mid, destination)
        if frame['kind'] == 'receipt':
            _require(frame['agentbus_federation'] == 1, 'receipt compatibility version changed')
            body = json.loads(frame['payload'])
            _require(set(body) == {'origin', 'message', 'destination', 'payload_hash'}, 'receipt fields changed')
            acknowledged = (body['origin'], body['message'], body['destination'])
            _require((origin, destination) == (body['destination'], body['origin'])
                     and mid == 'receipt-' + hashlib.sha256(canonical(list(acknowledged)).encode()).hexdigest(),
                     'receipt identity changed')
            receipts[acknowledged] = body
            receipt_references.setdefault(acknowledged, set()).add(Decimal(row['actual_provider_ref']))
            continue
        _require(wires.get(key, row['wire']) == row['wire'], 'conflicting physical replay')
        wires[key], payloads[key] = row['wire'], frame['payload']
        if frame['kind'] == 'directory':
            _require(frame['agentbus_federation'] == 2 and mid == directory_id
                     and frame['payload'] == directory_payload and origin != destination,
                     'directory withdrawal changed')
            directories.add((origin, destination))
            continue
        _require(frame['agentbus_federation'] == 3 and mid in expected
                 and (origin, destination) == expected[mid], 'unknown or redirected ordinary origin')
        message = decode_ordinary(frame['payload'])
        _require(message.message['sender'] == origin + ':agentbus:qualification-v3-r2'
                 and message.message['recipient'] == destination + ':agentbus:qualification-v3-r2'
                 and message.message['correlation_id'] == scope['correlation_id'], 'ordinary routing changed')
        messages.add(mid)
        references.setdefault(mid, set()).add(row['actual_provider_ref'])
        if mid in QUALIFIED_ROOTS:
            _require(message.parent is None and message.message['kind'] == 'status', 'root ancestry changed')
        else:
            parent_id = QUALIFIED_PARENTS[mid]
            parent_origin, parent_destination = QUALIFIED_ROOTS[parent_id]
            parent = message.parent.model_dump() if message.parent is not None else None
            _require(parent == {'origin_instance_id': parent_origin, 'origin_message_id': parent_id,
                                'destination_instance_id': parent_destination}
                     and message.message['kind'] == 'reply' and row['provider_subtype'] == 'thread_broadcast',
                     'signed reply parent or broadcast changed')
            replies.append((mid, parent_id, row['provider_thread_ref'], Decimal(row['actual_provider_ref'])))
    _require(messages == set(expected) and directories == {(a, b) for a in PINS for b in PINS if a != b},
             'incomplete roots replies or withdrawal fanout')
    _require(set(receipts) == set(payloads), 'missing signed destination custody')
    for key, body in receipts.items():
        _require(body['payload_hash'] == hashlib.sha256(payloads[key].encode()).hexdigest(), 'receipt digest changed')
        gate = ('root_gate_pass_at' if key[1] in QUALIFIED_ROOTS else
                'all_origins_gate_pass_at' if key[1] in QUALIFIED_PARENTS else 'withdrawal_gate_pass_at')
        _require(min(receipt_references[key]) <= _moment(data['observations'][gate]),
                 'actual signed receipt was published after its claimed gate')
    for mid, parent_id, thread, stamp in replies:
        _require(thread in references[parent_id], 'physical reply thread differs from signed logical parent')
        if expected[mid][1] == 'home-latejoin':
            _require(_moment(data['observations']['offline_at']) < stamp < _moment(data['observations']['restart_at']),
                     'offline reply was not published during the actual outage')
    _qualified_observations(data['observations'])
    return {'source': QUALIFIED_SOURCE, 'messages': 12, 'receipts': 12, 'reply_parents': 6,
            'withdrawals': 6, 'withdrawal_receipts': 6, 'physical_observations': len(rows),
            **runtime_applicability,
            'scope': 'certified_wire3_canary_only'}


def verify_evidence(data: dict) -> dict:
    _require(isinstance(data, dict) and isinstance(data.get('document'), dict), 'evidence document required')
    if type(data['document'].get('version')) is int and data['document']['version'] == 2:
        return _verify_qualified(data)
    return _verify_historical(data)


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
