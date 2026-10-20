"""Historical provider proof must authenticate its bounded scope and exact receipts."""
import base64
import copy
import hashlib
import io
import json
import subprocess
import tarfile
from functools import lru_cache
from pathlib import Path

import pytest

from scripts.verify_provider_canary import load_evidence, verify_evidence

PATH = Path(__file__).resolve().parents[1] / 'audits/p11-provider-canary-07b9ecb.yml'


@lru_cache(maxsize=1)
def released_runtime_bytes():
    """Immutable historical inputs; never substitute them for candidate proof."""
    corpus = load_evidence(PATH.with_name('p11-provider-canary-00eecbe.yml'))
    source = '8f2dbf5ced0f591bec21d561f3a39308bc3f373b'
    archive = subprocess.check_output(
        ['git', 'archive', '--format=tar', source, '--', *corpus['runtime_files']],
        cwd=PATH.parents[1],
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        return {PATH.parents[1] / member.name: bundle.extractfile(member).read()
                for member in bundle.getmembers() if member.isfile()}


@pytest.fixture(autouse=True)
def historical_runtime(monkeypatch):
    original = Path.read_bytes
    released = released_runtime_bytes()

    def read_historical(path):
        return released[path] if path in released else original(path)

    monkeypatch.setattr(Path, 'read_bytes', read_historical)
    return original


def evidence():
    return load_evidence(PATH)


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
    return load_evidence(PATH.with_name('p11-provider-canary-00eecbe.yml'))


def test_released_wire3_canary_authenticates_replies_recovery_and_complete_withdrawal():
    assert verify_evidence(qualified_evidence()) == {
        'source': '00eecbe82e28fcfc12936f11d70ec3c41186bc31',
        'messages': 12, 'receipts': 12, 'reply_parents': 6,
        'withdrawals': 6, 'withdrawal_receipts': 6,
        'physical_observations': 43, 'scope': 'certified_wire3_canary_only',
        'runtime_files_match': 51, 'release_metadata_files_match': 3,
        'release_metadata_transition': '0.8.0->0.9.0',
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


@pytest.mark.parametrize('version', ['0.8.0', '0.9.0'])
def test_canary_accepts_only_exact_release_metadata_transition(monkeypatch, version):
    original = Path.read_bytes

    def metadata(path):
        raw = original(path)
        if path == PATH.parents[1] / 'uv.lock':
            raw = raw.replace(b'name = "superworkspace-agentbus"\nversion = "0.8.0"', b'name = "superworkspace-agentbus"\nversion = "0.9.0"')
            return raw.replace(b'name = "superworkspace-agentbus"\nversion = "0.9.0"', ('name = "superworkspace-agentbus"\nversion = "' + version + '"').encode())
        if path == PATH.parents[1] / 'pyproject.toml':
            raw = raw.replace(b'\nversion = "0.8.0"\n', b'\nversion = "0.9.0"\n')
            return raw.replace(b'\nversion = "0.9.0"\n', ('\nversion = "' + version + '"\n').encode())
        if path == PATH.parents[1] / 'src/agentbus_service.py':
            raw = raw.replace(b'version="0.8.0", lifespan=lifespan,', b'version="0.9.0", lifespan=lifespan,')
            return raw.replace(b'version="0.9.0", lifespan=lifespan,', ('version="' + version + '", lifespan=lifespan,').encode())
        return raw

    monkeypatch.setattr(Path, 'read_bytes', metadata)
    result = verify_evidence(qualified_evidence())
    assert result['source'] == '00eecbe82e28fcfc12936f11d70ec3c41186bc31'
    assert result['runtime_files_match'] == (54 if version == '0.8.0' else 51)
    assert result.get('release_metadata_files_match', 0) == (0 if version == '0.8.0' else 3)


@pytest.mark.parametrize('fault', ['unknown', 'mixed', 'duplicate', 'service_behavior', 'dependency', 'bootstrap', 'lock_dependency', 'lock_mixed'])
def test_release_metadata_applicability_rejects_other_runtime_changes(monkeypatch, fault):
    original = Path.read_bytes

    def changed(path):
        raw = original(path)
        if path == PATH.parents[1] / 'uv.lock':
            if fault == 'lock_dependency':
                raw = raw.replace(b'name = "cryptography"\nversion = "50.0.2"', b'name = "cryptography"\nversion = "50.0.1"')
            if fault == 'lock_mixed':
                raw = raw.replace(b'name = "superworkspace-agentbus"\nversion = "0.9.0"', b'name = "superworkspace-agentbus"\nversion = "0.8.0"')
        if path == PATH.parents[1] / 'pyproject.toml':
            raw = raw.replace(b'\nversion = "0.8.0"\n', b'\nversion = "0.9.0"\n')
            if fault == 'unknown':
                raw = raw.replace(b'\nversion = "0.9.0"\n', b'\nversion = "0.9.1"\n')
            if fault == 'duplicate':
                raw += b'\nversion = "0.9.0"\n'
            if fault == 'dependency':
                raw = raw.replace(b'cryptography==50.0.2', b'cryptography==50.0.1')
        elif path == PATH.parents[1] / 'src/agentbus_service.py':
            raw = raw.replace(b'version="0.8.0", lifespan=lifespan,', b'version="0.9.0", lifespan=lifespan,')
            if fault == 'mixed':
                raw = raw.replace(b'version="0.9.0", lifespan=lifespan,', b'version="0.8.0", lifespan=lifespan,')
            if fault == 'service_behavior':
                raw += b'\n# changed runtime\n'
        elif path == PATH.parents[1] / 'Dockerfile' and fault == 'bootstrap':
            raw += b'\n# changed bootstrap\n'
        return raw

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError):
        verify_evidence(qualified_evidence())


def test_current_channel_runtime_cannot_inherit_historical_live_qualification(historical_runtime, monkeypatch):
    # The prerelease is outside the historical 0.8 -> 0.9 metadata allowlist.
    # Keep rejection strict; never broaden historical live-proof applicability.
    monkeypatch.setattr(Path, 'read_bytes', historical_runtime)
    with pytest.raises(ValueError, match='mixed or unsupported release metadata'):
        verify_evidence(qualified_evidence())


@pytest.mark.parametrize('fault', ['digest', 'source', 'path'])
def test_public_evidence_reference_rejects_changed_custody(tmp_path, fault):
    import yaml

    data = yaml.safe_load(PATH.read_text())
    values = {'digest': ('sha256', '0' * 64), 'source': ('source', '0' * 40),
              'path': ('path', '../outside.yml')}
    field, value = values[fault]
    data['retained_evidence'][field] = value
    reference = tmp_path / 'reference.yml'
    reference.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_evidence(reference)


def test_public_documentation_omits_live_installation_and_coordination_details():
    import re

    root = PATH.parents[1]
    # Product docs and authored product logs; BCF machinery has another owner.
    files = [*root.glob('*.md'), root / 'MEMORY.yml', root / '.env.example',
             *root.glob('docs/**/*.md'), *root.glob('audits/**/*.yml'),
             *root.glob('plans/*.yml'), *root.glob('phases/*.yml'),
             *root.glob('contracts/**/*.yml')]
    forbidden = re.compile(
        r'\b[A-Z]0C[A-Z0-9]{7,}\b|/docker/|/home/vscode/|/tmp/agentbus-'
        r'|\bAgentBus\s*\d{4,}|\bPID\d{4,}'
    )
    for path in files:
        if path.name in {'AGENTS.md', 'CLAUDE.md'}:
            continue
        assert forbidden.search(path.read_text()) is None, str(path.relative_to(root))
