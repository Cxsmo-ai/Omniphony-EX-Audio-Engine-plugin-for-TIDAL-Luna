import threading
import unittest
from pathlib import Path

import omnifusion_service as service


class ServiceLifecycleTests(unittest.TestCase):
    def test_shutdown_is_scheduled_off_the_request_thread(self):
        completed = threading.Event()

        class FakeServer:
            def shutdown(self):
                completed.set()

        worker = service.schedule_server_shutdown(FakeServer())
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(completed.is_set())

    def test_root_launcher_checks_health_before_rebasing_config(self):
        root = Path(service.__file__).resolve().parent.parent
        launcher = root / "launch_service.bat"
        if not launcher.is_file():
            self.skipTest("standalone root launcher is not part of this repository layout")
        source = launcher.read_text(encoding="utf-8")
        self.assertLess(source.index("/health"), source.index("prepare_portable_omniphony_config.py"))
        self.assertIn('findstr /C:"2.1.26"', source)

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
        self.assertIn('findstr /C:"2.1.26"', source)
        self.assertIn("different OmniFusion service build owns port 8765", source)


if __name__ == "__main__":
    unittest.main()
