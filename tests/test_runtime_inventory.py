"""Omitted runtime files fail before starting command processing."""
from pathlib import Path

import pytest

from src.agentbus_runtime_inventory_service import RUNTIME_MODULES, verify_runtime_inventory


def test_declared_population_covers_every_source_file_and_deferred_import():
    root = Path(__file__).resolve().parents[1]/'src'
    assert set(RUNTIME_MODULES)=={p.stem for p in root.glob('*.py') if p.name!='__init__.py'}
    imported=[]
    verify_runtime_inventory(root,importer=imported.append)
    assert 'src.agentbus_slack_ui_commands_service' in imported
    assert len(imported)==len(set(imported))
    assert not any(name.endswith("_client") for name in imported)


def test_missing_deferred_module_refuses_before_any_import(tmp_path):
    missing='agentbus_slack_ui_commands_service'
    for name in RUNTIME_MODULES:
        if name!=missing:
            (tmp_path/(name+'.py')).write_text('')
    imported=[]
    with pytest.raises(RuntimeError,match=missing):
        verify_runtime_inventory(tmp_path,importer=imported.append)
    assert imported==[]


def test_packaged_launcher_rejects_wildcard_a2a_before_provider_startup(tmp_path):
    import os
    import subprocess
    import sys
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENTBUS_")}
    env.update({"AGENTBUS_A2A_ENABLED": "1", "AGENTBUS_HOST": "0.0.0.0", "AGENTBUS_PORT": "18766",
                "AGENTBUS_API_TOKEN": "fixture-api-token-with-at-least-32-characters",
                "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-fixture", "AGENTBUS_SLACK_APP_TOKEN": "xapp-fixture",
                "AGENTBUS_SLACK_CHANNEL": "C111", "AGENTBUS_DB_PATH": str(tmp_path / "must-not-exist.db")})
    completed = subprocess.run([sys.executable, "-m", "agentbus_service"],
                               cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=20)
    assert completed.returncode != 0
    assert "loopback-bound" in completed.stderr
    assert not (tmp_path / "must-not-exist.db").exists()
    assert "fixture-api-token-with-at-least-32-characters" not in completed.stderr
