#!/usr/bin/env python3
"""Build the versioned, credential-free TidaLuna API runtime archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import stat
import tarfile
from pathlib import Path


EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
FORBIDDEN_NAMES = {".env", "token.json", "tidal-session.json"}
REQUIRED_FILES = (
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


def included_files(api_root: Path) -> list[Path]:
    result: list[Path] = []
    for current, dirs, filenames in os.walk(api_root, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            name for name in dirs
            if name not in EXCLUDED_DIRS
            and not (current_path / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = current_path / filename
            if filename.endswith((".pyc", ".pyo")):
                continue
            lower_name = filename.casefold()
            if lower_name in FORBIDDEN_NAMES or lower_name.startswith(".env."):
                raise ValueError(f"credential-like file is present in the API bundle tree: {path}")
            if path.suffix.casefold() in {".json", ".yaml", ".yml", ".ini"} and any(
                token in path.stem.casefold() for token in ("session", "token", "credential")
            ):
                raise ValueError(f"credential-like file is present in the API bundle tree: {path}")
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"non-regular API bundle input is not supported: {path}")
            result.append(path)
    return sorted(result, key=lambda path: path.relative_to(api_root.parent).as_posix())


def write_archive(repo_root: Path, output: Path) -> str:
    api_root = repo_root / "api"
    if not api_root.is_dir():
        raise FileNotFoundError(f"API directory not found: {api_root}")
    for relative in REQUIRED_FILES:
        if not (repo_root / relative).is_file():
            raise FileNotFoundError(f"required runtime payload is missing: {relative}")

    files = included_files(api_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with output.open("wb") as raw_output:
        class DigestingWriter:
            def write(self, data: bytes) -> int:
                digest.update(data)
                return raw_output.write(data)

            def flush(self) -> None:
                raw_output.flush()

        with gzip.GzipFile(fileobj=DigestingWriter(), mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                api_info = tarfile.TarInfo("api/")
                api_info.type = tarfile.DIRTYPE
                api_info.mode = 0o755
                api_info.mtime = 0
                api_info.uid = api_info.gid = 0
                api_info.uname = api_info.gname = ""
                archive.addfile(api_info)
                for path in files:
                    arcname = path.relative_to(repo_root).as_posix()
                    info = tarfile.TarInfo(arcname)
                    info.size = path.stat().st_size
                    info.mode = stat.S_IMODE(path.stat().st_mode) or 0o644
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with path.open("rb") as source:
                        archive.addfile(info, source)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    output = args.output.resolve()
    digest = write_archive(repo_root, output)
    print(f"Archive: {output}")
    print(f"SHA-256: {digest}")
    print(f"Bytes: {output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
