from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1] / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import omnifusion_service as service


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSeekEngine:
    def __init__(
        self, seeking_values, *, expected_pause_revision=7, user_pauses=False,
        internal_pause_on_seeking=False,
    ):
        self.playback_generation_lock = threading.RLock()
        self.pause_intent_revision = expected_pause_revision
        self.active_generation = 1
        self.current_meta = {
            "trackId": 227401769,
            "generation": 1,
            "requestId": "seek-test",
            "format": "AC4_IMS",
            "streamTransport": service.STREAM_TRANSPORT_OXIDEAV_RAW_S16LE,
        }
        self.seek_sequence = 1
        self.paused = False
        self.time_pos = 37.0
        self.seeking_values = list(seeking_values)
        self.user_pauses = user_pauses
        self.internal_pause_on_seeking = internal_pause_on_seeking
        self.resume_calls = 0
        self.resume_at = None

    def control_matches(self, track_id, generation, request_id=None, *, allow_prepared=False):
        return (
            str(track_id) == str(self.current_meta["trackId"])
            and generation == self.active_generation
            and request_id == self.current_meta["requestId"]
        )

    def seek_is_current(self, sequence, track_id, generation, request_id=None):
        return sequence == self.seek_sequence and self.control_matches(
            track_id, generation, request_id, allow_prepared=True
        )

    def cmd(self, command):
        if command[0] == "get_property":
            prop = command[1]
            if prop == "pause":
                return {"error": "success", "data": self.paused}
            if prop == "time-pos":
                if self.resume_at is not None and not self.paused:
                    self.time_pos = 37.0 + (service.time.monotonic() - self.resume_at)
                return {"error": "success", "data": self.time_pos}
            if prop == "seeking":
                value = self.seeking_values.pop(0) if self.seeking_values else False
                if value is True and self.internal_pause_on_seeking:
                    self.paused = True
                if value is False and self.resume_at is None:
                    self.paused = True
                    if self.user_pauses:
                        self.pause_intent_revision += 1
                return {"error": "success", "data": value}
        if command[:2] == ["set_property", "pause"] and command[2] is False:
            self.paused = False
            self.resume_calls += 1
            self.resume_at = service.time.monotonic()
            return {"error": "success"}
        return {"error": "success"}


class SeekPauseRecoveryTests(unittest.TestCase):
    def run_seek(self, engine, *, expected_paused, revision=7):
        clock = FakeClock()
        samples = []
        with patch.object(service.time, "monotonic", clock.monotonic), \
                patch.object(service.time, "sleep", clock.sleep), \
                patch.object(service, "SEEK_SETTLE_CONFIRMATION_SAMPLES", 2), \
                patch.object(service, "SEEK_SETTLE_STABILITY_SECONDS", 0.05):
            return service.HeadlessAudioEngine.wait_for_seek_position(
                engine,
                37.0,
                1,
                227401769,
                1,
                "seek-test",
                timeout=2.0,
                expected_paused=expected_paused,
                expected_pause_intent_revision=revision,
                recover_raw_ac4_pause=(
                    engine.current_meta.get("format") == "AC4_IMS"
                    and engine.current_meta.get("streamTransport")
                    == service.STREAM_TRANSPORT_OXIDEAV_RAW_S16LE
                ),
                diagnostic_samples=samples,
            )

    def test_playing_raw_pcm_seek_restores_internal_pause_and_settles(self):
        engine = FakeSeekEngine([True, True, False, False, False, False])

        settled, position, error = self.run_seek(engine, expected_paused=False)

        self.assertTrue(settled)
        self.assertAlmostEqual(position, 37.2, places=5)
        self.assertIsNone(error)
        self.assertEqual(engine.resume_calls, 1)
        self.assertFalse(engine.paused)

    def test_user_pause_during_seek_is_not_overridden(self):
        engine = FakeSeekEngine(
            [True, True, False, False],
            user_pauses=True,
        )

        settled, position, error = self.run_seek(engine, expected_paused=False)

        self.assertFalse(settled)
        self.assertAlmostEqual(position, 37.0)
        self.assertEqual(error, "pause-changed")
        self.assertEqual(engine.resume_calls, 0)
        self.assertTrue(engine.paused)

    def test_internal_pause_on_first_seeking_sample_is_recovered(self):
        engine = FakeSeekEngine(
            [True, False, False, False],
            internal_pause_on_seeking=True,
        )

        settled, position, error = self.run_seek(engine, expected_paused=False)

        self.assertTrue(settled)
        self.assertAlmostEqual(position, 37.2, places=5)
        self.assertIsNone(error)
        self.assertEqual(engine.resume_calls, 1)
        self.assertFalse(engine.paused)

    def test_non_ac4_pause_is_not_reconciled(self):
        engine = FakeSeekEngine([True, False])
        engine.current_meta["format"] = "EAC3_JOC"

        settled, position, error = self.run_seek(engine, expected_paused=False)

        self.assertFalse(settled)
        self.assertEqual(position, 37.0)
        self.assertEqual(error, "pause-changed")
        self.assertEqual(engine.resume_calls, 0)
        self.assertTrue(engine.paused)

    def test_paused_seek_stays_paused_without_resume(self):
        engine = FakeSeekEngine([False, False, False, False])
        engine.paused = True

        settled, position, error = self.run_seek(engine, expected_paused=True)

        self.assertTrue(settled)
        self.assertAlmostEqual(position, 37.0)
        self.assertIsNone(error)
        self.assertEqual(engine.resume_calls, 0)
        self.assertTrue(engine.paused)


if __name__ == "__main__":
    unittest.main()
