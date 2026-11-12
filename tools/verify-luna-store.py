"""Check that the generated dist folder follows TidaLuna's store URL contract."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    args = parser.parse_args()
    dist = args.dist.resolve()
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
