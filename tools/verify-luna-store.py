"""Check that the generated dist folder follows TidaLuna's store URL contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def verify_source_manifest(repo_root: Path) -> str:
    """Verify root metadata against the LF bytes served by GitHub."""
    repo_root = repo_root.resolve()
    try:
        manifest = json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))
        companion = json.loads((repo_root / "omniphony.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read source plugin manifests: {exc}") from exc

    main = manifest.get("main")
    if not isinstance(main, str) or not main:
        raise ValueError("source manifest has no main module")
    module_name = main.split("?", 1)[0]
    if Path(module_name).name != module_name or module_name in {"", ".", ".."}:
        raise ValueError(f"source manifest main must name a root module: {main!r}")
    try:
        payload = (repo_root / module_name).read_bytes().replace(b"\r\n", b"\n")
    except OSError as exc:
        raise ValueError(f"could not read source module {module_name!r}: {exc}") from exc

    actual_hash = hashlib.sha256(payload).hexdigest().upper()
    expected_hash = manifest.get("hash")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", expected_hash):
        raise ValueError("source manifest hash must be a 64-character SHA-256")
    if actual_hash != expected_hash.upper():
        raise ValueError(f"source manifest hash mismatch: expected {expected_hash.upper()}, got {actual_hash}")
    for key in ("main", "version", "hash"):
        if companion.get(key) != manifest.get(key):
            raise ValueError(f"omniphony.json does not match manifest.json for {key!r}")
    return actual_hash


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    args = parser.parse_args()
    dist = args.dist.resolve()
    repo_root = Path(__file__).resolve().parents[1]
    try:
        source_hash = verify_source_manifest(repo_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"GitHub source manifest verified: SHA-256 {source_hash}")
    store_path = dist / "store.json"
    if not store_path.is_file():
        raise SystemExit(f"missing Luna store catalog: {store_path}")

    store = json.loads(store_path.read_text(encoding="utf-8"))
    plugins = store.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise SystemExit("store.json must contain a non-empty plugins array")

    expected_name = "Omniphony EX Audio Engine"
    verified: list[str] = []
    for filename in plugins:
        if not isinstance(filename, str) or not filename.endswith(".mjs"):
            raise SystemExit(f"invalid plugin module entry: {filename!r}")
        basename = filename[:-4]
        module_path = dist / filename
        package_path = dist / f"{basename}.json"
        if not module_path.is_file() or not package_path.is_file():
            raise SystemExit(f"store entry {filename!r} is missing its .mjs/.json artifact pair")
        package = json.loads(package_path.read_text(encoding="utf-8"))
        if package.get("name") != expected_name:
            raise SystemExit(f"plugin metadata name mismatch: {package.get('name')!r}")
        if not isinstance(package.get("hash"), str) or not package["hash"].strip():
            raise SystemExit(f"plugin metadata for {filename!r} has no build hash")
        if not isinstance(package.get("version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", package["version"]):
            raise SystemExit(f"plugin metadata for {filename!r} has an invalid version")
        if not module_path.stat().st_size:
            raise SystemExit(f"plugin module is empty: {module_path}")
        verified.append(filename)

    print(f"TidaLuna store verified: {len(verified)} plugin(s), {', '.join(verified)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
