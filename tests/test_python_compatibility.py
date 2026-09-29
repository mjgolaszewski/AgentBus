"""Runtime contract for the separately provisioned compatibility lane."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]


def test_declared_project_range_includes_selected_runtime() -> None:
    if sys.version_info[:2] != (3, 14):
        pytest.skip("Python 3.14 compatibility is proven by its dedicated producer")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    selected = ".".join(str(value) for value in sys.version_info[:3])
    assert sys.version_info[:2] == (3, 14)
    assert SpecifierSet(project["requires-python"]).contains(selected)
