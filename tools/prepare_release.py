#!/usr/bin/env python3
"""Prepare GitHub release assets with store entries matching uploaded names."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path, PurePosixPath


REQUIRED_RELEASE_FILES = ("LICENSE", "THIRD-PARTY-NOTICES.md")


def release_asset_name(filename: str) -> str:
    path = PurePosixPath(filename)
    if path.name != filename or path.suffix != ".mjs" or not filename:
        raise ValueError(f"invalid plugin module filename: {filename!r}")
    return filename.replace(" ", ".")


def prepare_release(dist: Path, output: Path) -> list[str]:
    dist = dist.resolve()
    output = output.resolve()
    store_path = dist / "store.json"
    if not store_path.is_file():
        raise FileNotFoundError(f"missing Luna store catalog: {store_path}")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"release output directory must be empty: {output}")

    store = json.loads(store_path.read_text(encoding="utf-8"))
    plugins = store.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise ValueError("store.json must contain a non-empty plugins array")

    assets: list[tuple[str, Path]] = []
    release_plugins: list[str] = []
    for filename in plugins:
        if not isinstance(filename, str):
            raise ValueError(f"invalid plugin module entry: {filename!r}")
        asset = release_asset_name(filename)
        basename = filename[:-4]
        asset_basename = asset[:-4]
        module = dist / filename
        package = dist / f"{basename}.json"
        if not module.is_file() or not package.is_file():
            raise FileNotFoundError(f"store entry {filename!r} is missing its module/metadata pair")
        assets.extend(((asset, module), (f"{asset_basename}.json", package)))
        release_plugins.append(asset)

    names = [name for name, _ in assets]
    if len(names) != len(set(names)):
        raise ValueError("plugin filenames collide after GitHub release-name normalization")

    repo_root = dist.parent
    for filename in REQUIRED_RELEASE_FILES:
        source = repo_root / filename
        if not source.is_file():
            raise FileNotFoundError(f"missing required release file: {source}")
        assets.append((filename, source))

    output.mkdir(parents=True, exist_ok=True)
    for name, source in assets:
        shutil.copy2(source, output / name)

    store["plugins"] = release_plugins
    (output / "store.json").write_text(
        json.dumps(store, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return [*release_plugins, *(filename[:-4] + ".json" for filename in release_plugins), *REQUIRED_RELEASE_FILES, "store.json"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    dist = args.dist.resolve()
    output = args.output.resolve() if args.output else dist / "release"
    files = prepare_release(dist, output)
    print(f"Prepared {len(files)} release assets in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
