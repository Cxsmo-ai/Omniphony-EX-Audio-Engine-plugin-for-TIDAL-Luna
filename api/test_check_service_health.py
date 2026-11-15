import unittest
from unittest.mock import patch
from urllib.error import URLError

import check_service_health as checker


class ServiceHealthIdentityTests(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "expected_version": "2.1.36",
            "expected_owner_pid": 1234,
            "expected_plugin_root": r"C:\Users\picky\AppData\Roaming\TIDAL\plugins\omniphony-tidal-luna-plugin",
        }
        self.health = {
            "status": "ok",
            "version": "2.1.36",
            "tidalOwnerPid": 1234,
            "tidalOwnerAlive": True,
            "pluginRoot": r"c:\users\picky\appdata\roaming\tidal\plugins\omniphony-tidal-luna-plugin",
        }

    def test_accepts_exact_live_service_identity(self):
        self.assertTrue(checker.check_health(self.health, **self.expected))

    def test_rejects_substring_or_similarly_numbered_version(self):
        for version in ("2.1.360", "2.1.36-old", "unrelated 2.1.36"):
            with self.subTest(version=version):
                health = {**self.health, "version": version}
                self.assertFalse(checker.check_health(health, **self.expected))

    def test_rejects_other_live_tidal_owner(self):
        health = {**self.health, "tidalOwnerPid": 5678}
        self.assertFalse(checker.check_health(health, **self.expected))

    def test_rejects_dead_owner_or_different_plugin_root(self):
        self.assertFalse(checker.check_health(
            {**self.health, "tidalOwnerAlive": False}, **self.expected
        ))
        self.assertFalse(checker.check_health(
            {**self.health, "pluginRoot": r"C:\Other\Plugin"}, **self.expected
        ))

    def test_read_health_only_marks_connection_refused_as_unreachable(self):
        with patch.object(checker, "urlopen", side_effect=URLError(ConnectionRefusedError(10061, "refused"))):
            self.assertEqual(checker.read_health(), (2, None))
        with patch.object(checker, "urlopen", side_effect=URLError(TimeoutError("timed out"))):
            self.assertEqual(checker.read_health(), (1, None))

    def test_other_health_failures_are_not_permission_to_start(self):
        with patch.object(checker, "urlopen", side_effect=URLError(OSError("ambiguous network error"))):
            self.assertEqual(checker.read_health(), (1, None))


if __name__ == "__main__":
    unittest.main()
