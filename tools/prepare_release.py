#!/usr/bin/env python3
"""Prepare GitHub release assets with store entries matching uploaded names."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path, PurePosixPath


REQUIRED_ROOT_RELEASE_FILES = ("LICENSE", "THIRD-PARTY-NOTICES.md")
REQUIRED_DIST_RELEASE_FILES = ("OmniphonyEX-API-win-x64.tar.gz",)


def release_asset_name(plugin_name: str) -> str:
    path = PurePosixPath(plugin_name)
    if path.name != plugin_name or not plugin_name or plugin_name.lower().endswith((".mjs", ".json")):
        raise ValueError(f"invalid TidaLuna plugin base name: {plugin_name!r}")
    return plugin_name.replace(" ", ".")


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
    for plugin_name in plugins:
        if not isinstance(plugin_name, str):
            raise ValueError(f"invalid TidaLuna plugin base name: {plugin_name!r}")
        asset_basename = release_asset_name(plugin_name)
        module = dist / f"{plugin_name}.mjs"
        package = dist / f"{plugin_name}.json"
        if not module.is_file() or not package.is_file():
            raise FileNotFoundError(f"store entry {plugin_name!r} is missing its URL-resolved module/metadata pair")
        assets.extend(((f"{asset_basename}.mjs", module), (f"{asset_basename}.json", package)))
        release_plugins.append(asset_basename)

    names = [name for name, _ in assets]
    if len(names) != len(set(names)):
        raise ValueError("plugin filenames collide after GitHub release-name normalization")

    repo_root = dist.parent
    for filename in REQUIRED_ROOT_RELEASE_FILES:
        source = repo_root / filename
        if not source.is_file():
            raise FileNotFoundError(f"missing required release file: {source}")
        assets.append((filename, source))
    for filename in REQUIRED_DIST_RELEASE_FILES:
        source = dist / filename
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
    return [
        *(f"{plugin_name}.mjs" for plugin_name in release_plugins),
        *(f"{plugin_name}.json" for plugin_name in release_plugins),
        *REQUIRED_ROOT_RELEASE_FILES,
        *REQUIRED_DIST_RELEASE_FILES,
        "store.json",
    ]


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
