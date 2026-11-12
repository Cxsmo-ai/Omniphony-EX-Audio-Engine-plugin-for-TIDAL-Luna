import importlib.util
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location(
    "package_runtime", REPOSITORY_ROOT / "tools" / "package-runtime.py"
)
PACKAGE_RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(PACKAGE_RUNTIME)


class RuntimeArchiveDeterminismTests(unittest.TestCase):
    def test_windows_crlf_checkout_normalizes_to_lf(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.txt"
            path.write_bytes(bytes([97, 13, 10, 98, 10]))
            self.assertEqual(PACKAGE_RUNTIME.stable_file_bytes(path), bytes([97, 10, 98, 10]))

    def test_binary_payload_is_preserved_byte_for_byte(self):
        payload = bytes([0, 13, 10, 255])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.bin"
            path.write_bytes(payload)
            self.assertEqual(PACKAGE_RUNTIME.stable_file_bytes(path), payload)


if __name__ == "__main__":
    unittest.main()
