import importlib.util
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = REPOSITORY_ROOT / "tools" / "verify-luna-store.py"
SPEC = importlib.util.spec_from_file_location("verify_luna_store", VERIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFY_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY_MODULE)


class SourceManifestTests(unittest.TestCase):
    def _write_manifests(self, root: Path, module: bytes, digest: str) -> None:
        (root / "omniphony.mjs").write_bytes(module)
        manifest = {
            "main": "omniphony.mjs?version=2.1.51&service=2.1.34",
            "version": "2.1.51",
            "hash": digest,
        }
        for filename in ("manifest.json", "omniphony.json"):
            (root / filename).write_text(json.dumps(manifest), encoding="utf-8")

    def test_source_manifest_hash_uses_github_lf_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = b"export const plugin = true;\r\n"
            digest = sha256(module.replace(b"\r\n", b"\n")).hexdigest().upper()
            self._write_manifests(root, module, digest)

            self.assertEqual(VERIFY_MODULE.verify_source_manifest(root), digest)

    def test_source_manifest_rejects_a_windows_line_ending_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = b"export const plugin = true;\r\n"
            digest = sha256(module).hexdigest().upper()
            self._write_manifests(root, module, digest)

            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                VERIFY_MODULE.verify_source_manifest(root)

    def test_companion_manifest_must_match_source_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = b"export const plugin = true;\n"
            digest = sha256(module).hexdigest().upper()
            self._write_manifests(root, module, digest)
            companion = json.loads((root / "omniphony.json").read_text(encoding="utf-8"))
            companion["version"] = "2.1.49"
            (root / "omniphony.json").write_text(json.dumps(companion), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "version"):
                VERIFY_MODULE.verify_source_manifest(root)


if __name__ == "__main__":
    unittest.main()
