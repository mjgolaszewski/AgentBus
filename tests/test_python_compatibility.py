"""Runtime contract for the separately provisioned compatibility lane."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]


def test_declared_project_range_includes_selected_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    selected = "3.14.0"
    if os.environ.get("AGENTBUS_PYTHON314_COMPAT") == "1":
        assert sys.version_info[:2] == (3, 14)
        selected = ".".join(str(value) for value in sys.version_info[:3])
    assert SpecifierSet(project["requires-python"]).contains(selected)
