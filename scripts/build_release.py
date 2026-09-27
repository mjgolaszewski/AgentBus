#!/usr/bin/env python3
"""Build deterministic AgentBus source-release bytes from one Git commit."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import stat
import subprocess
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="exact commit or immutable ref to archive")
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts/release")
    args = parser.parse_args()

    commit = git("rev-parse", f"{args.ref}^{{commit}}").decode().strip()
    tree = git("rev-parse", f"{commit}^{{tree}}").decode().strip()
    version = tomllib.loads(git("show", f"{commit}:pyproject.toml").decode())["project"]["version"]
    prefix = f"AgentBus-{version}/"
    # Git's default tar.umask is 0002, which makes archive members group-writable.
    # Pin the release policy instead of inheriting machine or repository config.
    raw_tar = git("-c", "tar.umask=0022", "archive", "--format=tar", f"--prefix={prefix}", commit)

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"AgentBus-{version}.tar.gz"
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as stream:
        stream.write(raw_tar)
    archive.write_bytes(compressed.getvalue())

    # Verify the archive before writing its receipt.
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        root_name = prefix.rstrip("/")
        if not members or any(
            (member.name != root_name and not member.name.startswith(prefix))
            or member.issym()
            or member.islnk()
            or stat.S_IMODE(member.mode) not in ({0o755} if member.isdir() else {0o644, 0o755})
            for member in members
        ):
            raise SystemExit("release archive contains an invalid path, type, or mode")

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "product": "AgentBus",
        "version": version,
        "commit": commit,
        "tree": tree,
        "assets": [{"name": archive.name, "sha256": digest, "bytes": archive.stat().st_size}],
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
