from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_runtime_package", ROOT / "tools" / "verify-runtime-package.py"
)
assert SPEC and SPEC.loader
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)

PINNED_V2146 = "6F3563978A4276DB743E12B64B8E50169E99049318B2CDEF6AE11F2FF8CA220B"


class RuntimeDigestContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.archive = root / "runtime.tar.gz"
        self.module = root / "plugin.mjs"
        self.archive.write_bytes(b"different current build bytes")
        self.module.write_text(f'const bundleSha256 = "{PINNED_V2146}";\n', encoding="utf-8")

    def test_build_digest_can_differ_from_published_download_pin(self) -> None:
        archive_hash, pinned_hash = VERIFY.inspect_hashes(self.archive, self.module)
        self.assertEqual(archive_hash, hashlib.sha256(self.archive.read_bytes()).hexdigest().upper())
        self.assertNotEqual(archive_hash, pinned_hash)
        self.assertEqual(pinned_hash, PINNED_V2146)

    def test_missing_malformed_and_duplicate_pins_are_rejected(self) -> None:
        for source in (
            "const other = 'no pin';\n",
            'const bundleSha256 = "not-a-digest";\n',
            "const bundleSha256 = getPinnedDigest();\n",
            f'const bundleSha256 = "{PINNED_V2146}";\nconst bundleSha256 = "{PINNED_V2146}";\n',
            f'const bundleSha256 = "{PINNED_V2146}";\nconst bundleSha256 = getPinnedDigest();\n',
        ):
            with self.subTest(source=source):
                self.module.write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    VERIFY.inspect_hashes(self.archive, self.module)

    def test_optional_exact_digest_check_accepts_match_and_rejects_mismatch(self) -> None:
        archive_hash = hashlib.sha256(self.archive.read_bytes()).hexdigest().upper()
        VERIFY.validate_expected_sha256(archive_hash.lower(), archive_hash)
        with self.assertRaisesRegex(ValueError, "does not match"):
            VERIFY.validate_expected_sha256("0" * 64, archive_hash)

    def test_optional_exact_digest_check_rejects_malformed_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
            VERIFY.validate_expected_sha256("xyz", "0" * 64)


if __name__ == "__main__":
    unittest.main()
