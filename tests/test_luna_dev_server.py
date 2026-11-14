from __future__ import annotations

import importlib.util
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SERVER_PATH = Path(__file__).resolve().parents[1] / "tools" / "serve_luna_dev.py"
SPEC = importlib.util.spec_from_file_location("serve_luna_dev", SERVER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to import {SERVER_PATH}")
DEV_SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEV_SERVER)


class LunaDevServerCorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        dist = Path(cls.temp.name)
        (dist / "store.json").write_text('{"plugins":[]}', encoding="utf-8")
        cls.original_dist = DEV_SERVER.DIST
        cls.original_artifacts = DEV_SERVER.ARTIFACTS
        DEV_SERVER.DIST = dist
        DEV_SERVER.ARTIFACTS = {
            "/store.json": (dist / "store.json", "application/json; charset=utf-8"),
            "/unavailable.json": (dist / "missing.json", "application/json; charset=utf-8"),
        }
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DEV_SERVER.LunaDevHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()
        DEV_SERVER.DIST = cls.original_dist
        DEV_SERVER.ARTIFACTS = cls.original_artifacts

    def test_get_accepts_arbitrary_origin(self) -> None:
        request = Request(
            f"{self.base_url}/store.json",
            headers={"Origin": "https://arbitrary.example"},
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(response.read(), b'{"plugins":[]}')

    def test_preflight_accepts_private_network_request(self) -> None:
        request = Request(
            f"{self.base_url}/store.json",
            method="OPTIONS",
            headers={
                "Origin": "https://arbitrary.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(response.headers["Access-Control-Allow-Private-Network"], "true")

    def test_head_includes_cors_without_a_body(self) -> None:
        request = Request(
            f"{self.base_url}/store.json",
            method="HEAD",
            headers={"Origin": "https://arbitrary.example"},
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(response.read(), b"")

    def test_not_found_response_keeps_cors_headers(self) -> None:
        request = Request(
            f"{self.base_url}/not-allowlisted",
            headers={"Origin": "https://arbitrary.example"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)
        error = raised.exception
        try:
            self.assertEqual(error.code, 404)
            self.assertEqual(error.headers["Access-Control-Allow-Origin"], "*")
        finally:
            error.close()

    def test_missing_allowlisted_artifact_keeps_cors_headers(self) -> None:
        request = Request(
            f"{self.base_url}/unavailable.json",
            headers={"Origin": "https://arbitrary.example"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)
        error = raised.exception
        try:
            self.assertEqual(error.code, 404)
            self.assertEqual(error.headers["Access-Control-Allow-Origin"], "*")
        finally:
            error.close()


if __name__ == "__main__":
    unittest.main()
