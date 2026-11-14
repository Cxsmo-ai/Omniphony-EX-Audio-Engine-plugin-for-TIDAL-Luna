from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.prepare_release import prepare_release, release_asset_name


class PrepareReleaseTests(unittest.TestCase):
    def test_release_store_uses_github_normalized_asset_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            release = dist / "release"
            dist.mkdir()
            (dist / "store.json").write_text(
                json.dumps({"name": "store", "plugins": ["Omniphony EX Audio Engine.mjs"]}),
                encoding="utf-8",
            )
            (dist / "Omniphony EX Audio Engine.mjs").write_text("module", encoding="utf-8")
            (dist / "Omniphony EX Audio Engine.json").write_text(
                json.dumps({"name": "Omniphony EX Audio Engine", "version": "2.1.44", "hash": "x"}),
                encoding="utf-8",
            )
            (dist / "OmniphonyEX-API-win-x64.tar.gz").write_bytes(b"verified-runtime")
            (root / "LICENSE").write_text("license", encoding="utf-8")
            (root / "THIRD-PARTY-NOTICES.md").write_text("notices", encoding="utf-8")

            names = prepare_release(dist, release)

            self.assertEqual(
                names,
                [
                    "Omniphony.EX.Audio.Engine.mjs",
                    "Omniphony.EX.Audio.Engine.json",
                    "LICENSE",
                    "THIRD-PARTY-NOTICES.md",
                    "OmniphonyEX-API-win-x64.tar.gz",
                    "store.json",
                ],
            )
            self.assertEqual(
                json.loads((release / "store.json").read_text(encoding="utf-8"))["plugins"],
                ["Omniphony.EX.Audio.Engine.mjs"],
            )
            self.assertTrue((release / "Omniphony.EX.Audio.Engine.mjs").is_file())
            self.assertTrue((release / "Omniphony.EX.Audio.Engine.json").is_file())
            self.assertEqual(
                (release / "OmniphonyEX-API-win-x64.tar.gz").read_bytes(),
                b"verified-runtime",
            )
            self.assertEqual(
                json.loads((dist / "store.json").read_text(encoding="utf-8"))["plugins"],
                ["Omniphony EX Audio Engine.mjs"],
            )

    def test_release_module_names_reject_paths(self):
        self.assertEqual(
            release_asset_name("Omniphony EX Audio Engine.mjs"),
            "Omniphony.EX.Audio.Engine.mjs",
        )
        with self.assertRaises(ValueError):
            release_asset_name("../outside.mjs")

    def test_release_rejects_names_that_collide_after_normalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            dist.mkdir()
            modules = ["Foo Bar.mjs", "Foo.Bar.mjs"]
            (dist / "store.json").write_text(json.dumps({"plugins": modules}), encoding="utf-8")
            for module in modules:
                (dist / module).write_text("module", encoding="utf-8")
                (dist / f"{module[:-4]}.json").write_text(
                    json.dumps({"name": "Omniphony EX Audio Engine", "version": "1.0.0", "hash": "x"}),
                    encoding="utf-8",
                )
            (root / "LICENSE").write_text("license", encoding="utf-8")
            (root / "THIRD-PARTY-NOTICES.md").write_text("notices", encoding="utf-8")
            (dist / "OmniphonyEX-API-win-x64.tar.gz").write_bytes(b"runtime")

            with self.assertRaisesRegex(ValueError, "collide"):
                prepare_release(dist, dist / "release")


if __name__ == "__main__":
    unittest.main()
