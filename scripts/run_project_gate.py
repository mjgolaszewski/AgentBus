#!/usr/bin/env python3
"""Run one AgentBus assurance producer through a stable direct-argv interface."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts"
TEST_GATES = {
    "architecture-test": ["tests/test_architecture.py::test_architecture_registry_covers_every_production_module"],
    "architecture-module-size": ["tests/test_architecture.py::test_production_modules_respect_loc_cap"],
    "architecture-layer-membership": ["tests/test_architecture.py::test_production_modules_map_to_exactly_one_layer"],
    "architecture-context-membership": ["tests/test_architecture.py::test_production_modules_map_to_exactly_one_bounded_context"],
    "architecture-import-boundaries": ["tests/test_architecture.py::test_context_import_boundaries"],
    "architecture-cqrs-side": ["tests/test_architecture.py::test_command_and_query_populations_are_closed"],
    "architecture-router-thinness": ["tests/test_architecture.py::test_http_routers_remain_thin_transport_adapters"],
    "architecture-duplication": ["tests/test_architecture.py::test_cross_context_duplication_stays_below_declared_block_size"],
    "test": ["tests/test_launcher.py", "tests/test_poll_client.py", "tests/test_presentation_client.py", "tests/test_operator_client.py", "tests/test_rename_client.py", "tests/test_codex_wake_contract.py", "tests/test_codex_wake_client.py", "tests/test_codex_rpc_client.py"],
    "contract-test": ["tests/test_service.py", "tests/test_participation_contract.py", "tests/test_participation_service.py", "tests/test_participation_store.py", "tests/test_control_store.py", "tests/test_rename_store.py", "tests/test_claim_recovery.py"],
    "python314-compatibility": [
        "tests/test_python_compatibility.py",
        "tests/test_launcher.py",
        "tests/test_poll_client.py",
        "tests/test_presentation_client.py",
        "tests/test_operator_client.py",
        "tests/test_rename_client.py",
        "tests/test_codex_wake_contract.py",
        "tests/test_codex_wake_client.py",
        "tests/test_codex_rpc_client.py",
        "tests/test_claim_recovery.py",
        "tests/test_service.py",
    ],
}
TOKEN_PATTERN = re.compile(
    rb"(?:xox[baprs]-[A-Za-z0-9-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def run(
    argv: list[str], *, capture: bool = False, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=capture,
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            **(environment or {}),
        },
    )


def require_success(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode:
        if result.stdout:
            print(result.stdout, end="", file=sys.stdout)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(result.returncode)


def test_gate(gate: str, junit: Path) -> None:
    junit.parent.mkdir(parents=True, exist_ok=True)
    pytest = ["python", "-m", "pytest"]
    if gate == "python314-compatibility":
        with tempfile.TemporaryDirectory(prefix="agentbus-python314-") as directory:
            command = [
                "uv", "run", "--python", "3.14", "--locked", "--extra", "dev",
                *pytest, "-q", *TEST_GATES[gate], f"--junitxml={junit}",
            ]
            require_success(
                run(
                    command,
                    environment={
                        "AGENTBUS_PYTHON314_COMPAT": "1",
                        "UV_PROJECT_ENVIRONMENT": directory,
                    },
                )
            )
        return
    pytest[0] = sys.executable
    require_success(run([*pytest, "-q", *TEST_GATES[gate], f"--junitxml={junit}"]))


def secret_scan() -> None:
    tracked = run(["git", "ls-files", "-z"], capture=True)
    require_success(tracked)
    findings: list[str] = []
    for name in tracked.stdout.split("\0"):
        if not name or name.startswith(("tests/", "scripts/_bcf_runtime/", "schemas/")):
            continue
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        for match in TOKEN_PATTERN.finditer(path.read_bytes()):
            findings.append(f"{name}:{match.start()}: credential-shaped bytes")
    if findings:
        raise SystemExit("\n".join(findings))
    print("secret-scan-ok")


def verify_declared_environment() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        *project["project"]["dependencies"],
        *project["project"]["optional-dependencies"]["dev"],
    ]
    failures: list[str] = []
    for raw in requirements:
        requirement = Requirement(raw)
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            failures.append(f"{requirement.name} is not installed")
            continue
        if installed not in requirement.specifier:
            failures.append(f"{requirement.name} {installed} violates {requirement.specifier}")
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {item["name"]: item["version"] for item in lock["package"] if "version" in item}
    for raw in requirements:
        requirement = Requirement(raw)
        normalized = requirement.name.lower().replace("_", "-")
        if normalized not in locked or locked[normalized] not in requirement.specifier:
            failures.append(f"uv.lock does not satisfy {raw}")
    if failures:
        raise SystemExit("\n".join(failures))
    require_success(run([sys.executable, "-m", "pip", "check"]))
    print("declared-environment-ok")


def runtime_smoke() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentbus-runtime-") as directory:
        iid = Path(directory) / "image-id"
        require_success(run(["docker", "build", "--iidfile", str(iid), "-t", "agentbus:bcf-runtime-smoke", "."]))
        image = iid.read_text(encoding="utf-8").strip()
        try:
            require_success(run([
                "docker", "run", "--rm", "--entrypoint", "/app/.venv/bin/python", image,
                "-c", (
                    "import os,sys,agentbus_service; "
                    "sys.exit('runtime-user-mismatch') if os.getuid()!=10001 else None; "
                    "assert agentbus_service.create_app"
                ),
            ]))
        finally:
            run(["docker", "image", "rm", "--force", image], capture=True)
    print("runtime-smoke-ok")


def release_build() -> None:
    with tempfile.TemporaryDirectory(prefix="agentbus-release-gate-") as directory:
        root = Path(directory)
        outputs = [root / "first", root / "second"]
        for output in outputs:
            require_success(run([
                sys.executable,
                "scripts/build_release.py",
                "--ref",
                "HEAD",
                "--output",
                str(output),
            ]))
        first_manifest = json.loads((outputs[0] / "release-manifest.json").read_text(encoding="utf-8"))
        second_manifest = json.loads((outputs[1] / "release-manifest.json").read_text(encoding="utf-8"))
        if first_manifest != second_manifest:
            raise SystemExit("release manifests are not reproducible")
        archive_name = first_manifest["assets"][0]["name"]
        first_archive = (outputs[0] / archive_name).read_bytes()
        second_archive = (outputs[1] / archive_name).read_bytes()
        if first_archive != second_archive:
            raise SystemExit("release archives are not reproducible")
        commit = run(["git", "rev-parse", "HEAD"], capture=True)
        tree = run(["git", "rev-parse", "HEAD^{tree}"], capture=True)
        require_success(commit)
        require_success(tree)
        if first_manifest["commit"] != commit.stdout.strip():
            raise SystemExit("release manifest commit mismatch")
        if first_manifest["tree"] != tree.stdout.strip():
            raise SystemExit("release manifest tree mismatch")
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        if first_manifest["version"] != version or archive_name != f"AgentBus-{version}.tar.gz":
            raise SystemExit("release manifest version mismatch")
        asset = first_manifest["assets"][0]
        if asset["sha256"] != hashlib.sha256(first_archive).hexdigest():
            raise SystemExit("release manifest digest mismatch")
        if asset["bytes"] != len(first_archive):
            raise SystemExit("release manifest size mismatch")
        if (outputs[0] / "SHA256SUMS").read_bytes() != (outputs[1] / "SHA256SUMS").read_bytes():
            raise SystemExit("release checksum files are not reproducible")
    print("release-build-ok")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=[
        *TEST_GATES,
        "lint",
        "typecheck",
        "security-secret-scan",
        "security-dependency-audit",
        "security-sbom",
        "security-vulnerability-scan",
        "runtime-smoke",
        "release-smoke",
    ])
    parser.add_argument("--junit", type=Path)
    args = parser.parse_args()

    if args.gate in TEST_GATES:
        if args.junit is None:
            parser.error("test gates require --junit")
        test_gate(args.gate, args.junit)
    elif args.gate == "lint":
        require_success(run([sys.executable, "scripts/check_source.py"]))
        require_success(run([
            sys.executable, "-m", "ruff", "check", "src", "agentbus", "agentbus_service.py",
            "scripts/check_source.py", "scripts/build_release.py", "scripts/run_project_gate.py", "tests",
        ]))
    elif args.gate == "typecheck":
        require_success(run([sys.executable, "-m", "mypy"]))
    elif args.gate == "security-secret-scan":
        secret_scan()
    elif args.gate == "security-dependency-audit":
        verify_declared_environment()
    elif args.gate == "security-sbom":
        output = ARTIFACTS / "security" / "agentbus.cdx.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        require_success(run([
            sys.executable, "-m", "cyclonedx_py", "environment", sys.executable,
            "--pyproject", "pyproject.toml", "--output-reproducible",
            "--output-format", "JSON", "--output-file", str(output),
        ]))
        try:
            json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit("SBOM output is not valid JSON") from exc
        print(output.relative_to(ROOT))
    elif args.gate == "security-vulnerability-scan":
        verify_declared_environment()
        output = ARTIFACTS / "security" / "pip-audit.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        require_success(run([
            sys.executable, "-m", "pip_audit", "--local", "--strict", "--skip-editable",
            "--progress-spinner", "off", "--format", "json", "--output", str(output),
        ]))
        json.loads(output.read_text(encoding="utf-8"))
        print(output.relative_to(ROOT))
    elif args.gate == "runtime-smoke":
        runtime_smoke()
    else:
        release_build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
