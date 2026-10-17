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
