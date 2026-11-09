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
