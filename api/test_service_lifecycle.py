import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import omnifusion_service as service


class ServiceLifecycleTests(unittest.TestCase):
    def test_eclipse_manifest_urls_allow_https_and_local_development_only(self):
        self.assertEqual(
            service.validate_eclipse_addon_urls([
                " https://example.com/manifest.json ",
                "http://localhost:3000/manifest.json",
                "http://127.0.0.1:3000/manifest.json",
                "http://[::1]:3000/manifest.json",
            ]),
            [
                "https://example.com/manifest.json",
                "http://localhost:3000/manifest.json",
                "http://127.0.0.1:3000/manifest.json",
                "http://[::1]:3000/manifest.json",
            ],
        )

    def test_eclipse_manifest_urls_reject_unsafe_schemes_and_credentials(self):
        for value in (["javascript:alert(1)"], ["http://example.com/manifest.json"],
                      ["https://user:pass@example.com/manifest.json"], ["https://example.com/" + "a" * 2050]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.validate_eclipse_addon_urls(value)

    def test_eclipse_manifest_urls_require_a_list_of_strings(self):
        for value in ("https://example.com/manifest.json", [None], ["https://example.com/" for _ in range(17)]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.validate_eclipse_addon_urls(value)

    def test_invalid_saved_eclipse_manifests_are_ignored(self):
        with TemporaryDirectory() as temp:
            settings_file = Path(temp) / "settings.json"
            settings_file.write_text(
                json.dumps({"eclipse_addons": ["javascript:alert(1)"]}),
                encoding="utf-8",
            )
            with patch.object(service, "SETTINGS_FILE", settings_file):
                settings = service.load_settings()
        self.assertEqual(settings["eclipse_addons"], [])

    def test_audio_device_default_is_portable_and_saved_device_is_preserved(self):
        self.assertEqual(service.DEFAULT_SETTINGS["audio_device"], "auto")
        with TemporaryDirectory() as temp:
            settings_file = Path(temp) / "settings.json"
            settings_file.write_text(
                json.dumps({"audio_device": "wasapi/test-device"}),
                encoding="utf-8",
            )
            with patch.object(service, "SETTINGS_FILE", settings_file):
                settings = service.load_settings()
        self.assertEqual(settings["audio_device"], "wasapi/test-device")

    def test_shutdown_is_scheduled_off_the_request_thread(self):
        completed = threading.Event()

        class FakeServer:
            def shutdown(self):
                completed.set()

        worker = service.schedule_server_shutdown(FakeServer())
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(completed.is_set())

    def test_unexpected_post_handler_error_returns_json_instead_of_dropping_connection(self):
        class FakeRequest:
            path = "/seek?diagnostic=ignored"
            close_connection = False

            def _do_POST(self):
                raise RuntimeError("do not expose this exception message")

            def send_json(self, payload, status=200):
                self.response = (payload, status)

        request = FakeRequest()
        with patch("builtins.print") as log:
            service.RequestHandler.do_POST(request)

        self.assertEqual(request.response, ({
            "error": "request failed unexpectedly",
            "code": "INTERNAL_ERROR",
            "errorType": "RuntimeError",
        }, 500))
        log.assert_called_once_with("[-] POST /seek handler failed unexpectedly (RuntimeError)")

    def test_root_launcher_checks_health_before_rebasing_config(self):
        root = Path(service.__file__).resolve().parent.parent
        launcher = root / "launch_service.bat"
        if not launcher.is_file():
            self.skipTest("standalone root launcher is not part of this repository layout")
        source = launcher.read_text(encoding="utf-8")
        self.assertLess(source.index("/health"), source.index("prepare_portable_omniphony_config.py"))
        self.assertIn(f'findstr /C:"{service.SERVICE_VERSION}"', source)

    def test_packaged_launcher_waits_for_health_and_rejects_other_build(self):
        root = Path(service.__file__).resolve().parent.parent
        service_dir = Path(service.__file__).resolve().parent
        candidates = [service_dir / "launch_service.bat", root / "api" / "launch_service.bat",
                      root / "plugin-dev" / "api" / "launch_service.bat"]
        launcher = next((candidate for candidate in candidates if candidate.is_file()), None)
        if launcher is None:
            self.skipTest("packaged API launcher is not present")
        source = launcher.read_text(encoding="utf-8")
        self.assertIn("for /l %%I in (1,1,20)", source)
        self.assertIn(f'findstr /C:"{service.SERVICE_VERSION}"', source)
        self.assertIn("different OmniFusion service build owns port 8765", source)


if __name__ == "__main__":
    unittest.main()
