#!/usr/bin/env python3
"""Verify the runtime archive layout, licenses, imports, and optional exact digest."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


REQUIRED = (
    "api/omnifusion_service.py",
    "api/runtime/python.exe",
    "api/vendor/tidalapi/__init__.py",
    "api/oxideav/adapter-v0.1.71/ac4-to-wav.exe",
    "api/oxideav/adapter-v0.1.71/omnifusion_ac4_adapter.dll",
    "api/oxideav/adapter-v0.1.71/LICENSE",
    "api/harletty/v0.9.0/harletty_dolby_bridge.dll",
    "api/harletty/v0.9.0/harletty_dts_bridge.dll",
    "api/harletty/v0.9.0/harletty_iamf_bridge.dll",
    "api/harletty/v0.9.0/LICENSE",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--plugin-module", type=Path, required=True)
    parser.add_argument(
        "--expected-sha256",
        help="Optionally require an exact digest when checking a specific published/downloaded archive.",
    )
    args = parser.parse_args()
    archive_path = args.archive.resolve()
    try:
        archive_hash, pinned_hash = inspect_hashes(archive_path, args.plugin_module.resolve())
        validate_expected_sha256(args.expected_sha256, archive_hash)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    with tarfile.open(archive_path, "r:gz") as bundle:
        members = bundle.getmembers()
        names: set[str] = set()
        for member in members:
            name = member.name.replace("\\", "/")
            path = PurePosixPath(name)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                if name != "api/":
                    raise SystemExit(f"unsafe archive path: {member.name}")
            if not (name.rstrip("/") == "api" or name.startswith("api/")):
                raise SystemExit(f"unexpected archive root: {member.name}")
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise SystemExit(f"unsupported archive member type: {member.name}")
            names.add(name.rstrip("/"))
        missing = sorted(set(REQUIRED) - names)
        if missing:
            raise SystemExit("missing required archive payload: " + ", ".join(missing))
        for name in names:
            lowered = name.casefold()
            if lowered.endswith((".env", ".env.local")) or any(
                part in lowered for part in ("tidal-session", "token.json", "credentials.json")
            ):
                raise SystemExit(f"credential-like path found in runtime archive: {name}")

        with tempfile.TemporaryDirectory(prefix="omniphony-runtime-verify-") as temp:
            root = Path(temp)
            bundle.extractall(root, filter="data")
            api = root / "api"
            python = api / "runtime" / "python.exe"
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import tidalapi, requests, psutil, mpegdash; "
                "print('python-tidal and runtime dependencies import successfully')"
            )
            result = subprocess.run(
                [str(python), "-c", code, str(api / "vendor")],
                cwd=api,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode:
                raise SystemExit(
                    f"embedded Python import smoke failed ({result.returncode}): "
                    f"{result.stderr[-2000:]}"
                )
            print(result.stdout.strip())

    print(f"Runtime archive verified: {archive_path}")
    print(f"Built archive SHA-256: {archive_hash}")
    print(f"Pinned download SHA-256: {pinned_hash}")
    print(f"Members: {len(names)}")
    return 0


def inspect_hashes(archive_path: Path, module_path: Path) -> tuple[str, str]:
    """Inspect the build digest and the independent published-download pin."""
    archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest().upper()
    module = module_path.read_text(encoding="utf-8")
    assignments = re.findall(r"\bbundleSha256\s*=\s*([^;\r\n]+)", module)
    if len(assignments) != 1 or not re.fullmatch(
        r'"[A-Fa-f0-9]{64}"', assignments[0].strip()
    ):
        raise ValueError("plugin module must contain exactly one valid bundleSha256 pin")
    return archive_hash, assignments[0].strip()[1:-1].upper()


def validate_expected_sha256(expected_sha256: str | None, archive_hash: str) -> None:
    """Optionally enforce exact bytes when the caller is checking a specific asset."""
    if expected_sha256 is None:
        return
    expected_hash = expected_sha256.strip().upper()
    if not re.fullmatch(r"[A-F0-9]{64}", expected_hash):
        raise ValueError("expected SHA-256 must contain exactly 64 hexadecimal characters")
    if expected_hash != archive_hash:
        raise ValueError("runtime archive digest does not match the expected SHA-256")


if __name__ == "__main__":
    raise SystemExit(main())
