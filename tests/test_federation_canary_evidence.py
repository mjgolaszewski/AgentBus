"""Historical provider proof must authenticate its bounded scope and exact receipts."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from scripts.verify_provider_canary import verify_evidence

PATH = Path(__file__).resolve().parents[1] / 'audits/p11-provider-canary-07b9ecb.yml'


def evidence():
    return yaml.safe_load(PATH.read_text())


def test_historical_real_provider_carriers_and_receipts_authenticate_all_six_directions():
    result = verify_evidence(evidence())
    assert result == {'source': '07b9ecb5e478ce0728b3a788071d50469294c327',
                      'carriers': 7, 'receipts': 7, 'directions': 6, 'scope': 'historical_wire1_only'}


@pytest.mark.parametrize('fault', ['wire', 'signature', 'publisher', 'key', 'receipt_digest', 'missing', 'duplicate',
                                   'subject', 'semantic_ack', 'timestamp_type', 'extra_field'])
def test_historical_provider_proof_rejects_forgery_omission_and_scope_promotion(fault):
    data = copy.deepcopy(evidence())
    if fault in {'wire', 'signature'}:
        row = data['carriers'][0]
        frame = json.loads(row['wire'])
        if fault == 'wire':
            body = json.loads(frame['payload'])
            body['text'] = 'forged'
            frame['payload'] = json.dumps(body, sort_keys=True, separators=(',', ':'))
        else:
            frame['signature'] = base64.b64encode(bytes(64)).decode()
        row['wire'] = json.dumps(frame, sort_keys=True, separators=(',', ':'))
        row['wire_sha256'] = hashlib.sha256(row['wire'].encode()).hexdigest()
        row['wire_bytes'] = len(row['wire'].encode())
    elif fault == 'publisher':
        data['carriers'][0]['actual_publisher_id'] = 'unapproved/publisher/channel'
    elif fault == 'key':
        data['operator_bindings'][0]['ed25519_public_bytes_hex'] = '00' * 32
    elif fault == 'receipt_digest':
        data['receipts'][0]['receipt']['payload_hash'] = '00' * 32
    elif fault == 'missing':
        data['receipts'].pop()
    elif fault == 'duplicate':
        data['receipts'][-1] = data['receipts'][0]
    elif fault == 'subject':
        data['scope']['source_commit'] = '5e05c04d87c036a33dd6d8adfe10a6f3b1bf2291'
    elif fault == 'semantic_ack':
        data['scope']['semantic_acknowledgement'] = True
    elif fault == 'timestamp_type':
        data['carriers'][0]['actual_provider_ref'] = float(data['carriers'][0]['actual_provider_ref'])
    else:
        data['carriers'][0]['extra'] = 'not admitted'
    with pytest.raises(ValueError):
        verify_evidence(data)


def qualified_evidence():
    return yaml.safe_load(PATH.with_name('p11-provider-canary-00eecbe.yml').read_text())


def test_released_wire3_canary_authenticates_replies_recovery_and_complete_withdrawal():
    assert verify_evidence(qualified_evidence()) == {
        'source': '00eecbe82e28fcfc12936f11d70ec3c41186bc31',
        'messages': 12, 'receipts': 12, 'reply_parents': 6,
        'withdrawals': 6, 'withdrawal_receipts': 6,
        'physical_observations': 43, 'scope': 'certified_wire3_canary_only',
        'runtime_files_match': 54,
    }


@pytest.mark.parametrize('fault', [
    'signature', 'publisher', 'parent_thread', 'missing_receipt', 'missing_withdrawal',
    'subject', 'semantic_ack', 'deployment_authority', 'phase_closure', 'late_rollback',
    'restore_old_primary', 'checkpoint_reset', 'checkpoint_regression', 'local_parent',
    'timestamp_type', 'receipt_late', 'binding_extra', 'runtime_manifest', 'extra_field',
])
def test_released_canary_rejects_forgery_missing_custody_and_authority_promotion(fault):
    data = copy.deepcopy(qualified_evidence())
    rows = data['carrier_observations']
    if fault == 'signature':
        frame = json.loads(rows[0]['wire'])
        frame['signature'] = base64.b64encode(bytes(64)).decode()
        rows[0]['wire'] = json.dumps(frame, sort_keys=True, separators=(',', ':'))
        rows[0]['wire_sha256'] = hashlib.sha256(rows[0]['wire'].encode()).hexdigest()
    elif fault == 'publisher':
        rows[0]['actual_publisher_id'] = 'unapproved/publisher/channel'
    elif fault == 'parent_thread':
        row = next(r for r in rows if r['provider_subtype'] == 'thread_broadcast')
        row['provider_thread_ref'] = '1792205101.000001'
    elif fault in {'missing_receipt', 'missing_withdrawal'}:
        kind = 'receipt' if fault == 'missing_receipt' else 'directory'
        index = next(i for i, row in enumerate(rows) if row['kind'] == kind)
        other = next(row for row in rows if row['kind'] == kind and row['origin_message_id'] != rows[index]['origin_message_id']) if kind == 'receipt' else next(row for row in rows if row['kind'] == kind and row['destination_instance_id'] != rows[index]['destination_instance_id'])
        rows[index] = copy.deepcopy(other)
    elif fault == 'subject':
        data['scope']['source_commit'] = '87d7c74517dff22eb5d64d54f2ba9457dfa78b32'
    elif fault in {'semantic_ack', 'deployment_authority', 'phase_closure'}:
        key = {'semantic_ack': 'semantic_acknowledgement', 'phase_closure': 'computed_phase_closure'}.get(fault, fault)
        data['scope'][key] = True
    elif fault == 'late_rollback':
        data['observations']['rollback']['moranwm_prior_core_healthy_at'] = '2026-10-17T03:05:01+00:00'
    elif fault in {'restore_old_primary', 'checkpoint_reset'}:
        key = 'backup_restored_over_new_custody' if fault == 'restore_old_primary' else fault
        data['observations']['rollback'][key] = True
    elif fault == 'checkpoint_regression':
        data['observations']['checkpoint_after_restart'] = '1792205000.000000'
    elif fault == 'local_parent':
        data['observations']['recovered_reply_parent_cursors']['home_reply'] = 119
    elif fault == 'timestamp_type':
        rows[0]['actual_provider_ref'] = float(rows[0]['actual_provider_ref'])
    elif fault == 'receipt_late':
        next(row for row in rows if row['kind'] == 'receipt')['actual_provider_ref'] = '1792205341.000000'
    elif fault == 'binding_extra':
        data['operator_bindings'][0]['extra'] = 'not admitted'
    elif fault == 'runtime_manifest':
        data['runtime_files']['src/agentbus_service.py'] = '00' * 32
    else:
        rows[0]['extra'] = 'not admitted'
    with pytest.raises(ValueError):
        verify_evidence(data)


def test_retained_provider_evidence_cannot_qualify_changed_runtime_bytes(monkeypatch):
    data = qualified_evidence()
    original = Path.read_bytes

    def changed(path):
        raw = original(path)
        if path == PATH.parents[1] / 'src/agentbus_service.py':
            return raw + b'\n# simulated successor runtime change\n'
        return raw

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError):
        verify_evidence(data)
