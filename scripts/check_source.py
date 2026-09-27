#!/usr/bin/env python3
"""Deterministic AgentBus source checks for the earliest governance boundary."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PYTHON_SOURCES = {"agentbus"}
MARKDOWN_LINK = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    )
    return [ROOT / raw.decode() for raw in result.stdout.split(b"\0") if raw]


def check_python(path: Path, failures: list[str]) -> None:
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path.relative_to(ROOT)))
    except (OSError, UnicodeError, SyntaxError) as exc:
        failures.append(f"{path.relative_to(ROOT)}: invalid Python: {exc}")


def check_markdown(path: Path, failures: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for target in MARKDOWN_LINK.findall(text):
        target = target.strip().split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        candidate = (path.parent / target).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError:
            failures.append(f"{path.relative_to(ROOT)}: local link escapes repository: {target}")
            continue
        if not candidate.exists():
            failures.append(f"{path.relative_to(ROOT)}: missing local link: {target}")


def main() -> int:
    failures: list[str] = []
    files = tracked_files()
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if path.is_symlink() or not path.is_file():
            failures.append(f"{relative}: tracked source must be a regular file")
            continue
        data = path.read_bytes()
        if b"\0" not in data:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                failures.append(f"{relative}: tracked text is not UTF-8")
            else:
                if any(line.endswith((" ", "\t")) for line in text.splitlines()):
                    failures.append(f"{relative}: trailing whitespace")
        if path.suffix == ".py" or relative in PYTHON_SOURCES:
            check_python(path, failures)
        elif path.suffix == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                failures.append(f"{relative}: invalid JSON: {exc}")
        elif path.suffix == ".md":
            check_markdown(path, failures)

    try:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        failures.append(f"pyproject.toml: invalid TOML: {exc}")
        project = {}
    version = project.get("project", {}).get("version")
    service = (ROOT / "agentbus_service.py").read_text(encoding="utf-8")
    if not isinstance(version, str) or f'version="{version}"' not in service:
        failures.append("project version must match the FastAPI service version")
    if isinstance(version, str) and f"## [{version}]" not in (ROOT / "CHANGELOG.md").read_text():
        failures.append("project version must have a changelog release heading")
    if stat.S_IMODE((ROOT / "agentbus").stat().st_mode) != 0o755:
        failures.append("agentbus must be executable with mode 0755")

    for failure in failures:
        print(f"source-check: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"source-check: {len(files)} tracked files valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
