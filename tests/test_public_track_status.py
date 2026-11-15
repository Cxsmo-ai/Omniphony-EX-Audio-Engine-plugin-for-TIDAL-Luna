from __future__ import annotations

import sys
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


API_ROOT = Path(__file__).resolve().parents[1] / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import omnifusion_service as service


STALE_LABEL = "Omniphony renderer disconnected"
HEALTHY_JOC_LABEL = "Dolby Atmos (E-AC-3 JOC)"


def metadata(**overrides):
    value = {
        "trackId": 227401768,
        "generation": 42,
        "format": "EAC3_JOC",
        "codec": "eac3",
        "label": STALE_LABEL,
        "lastFailure": STALE_LABEL,
        "omniphonyRoute": {"ready": True},
        "title": "Natural",
    }
    value.update(overrides)
    return value


class PublicTrackStatusTests(unittest.TestCase):
    def test_verified_paused_joc_normalizes_exact_stale_renderer_message(self):
        value = metadata()
        original = value.copy()

        result = service.public_track_status(value, "paused")

        self.assertEqual(result["label"], HEALTHY_JOC_LABEL)
        self.assertIsNone(result["lastFailure"])
        self.assertEqual(value, original)

    def test_verified_joc_does_not_reinfer_failure_from_stale_label(self):
        result = service.public_track_status(
            metadata(lastFailure=None), "paused"
        )

        self.assertEqual(result["label"], HEALTHY_JOC_LABEL)
        self.assertIsNone(result["lastFailure"])

    def test_unrelated_active_failure_is_preserved_and_not_hidden_by_label(self):
        result = service.public_track_status(
            metadata(lastFailure="MPV audio output failed"), "paused"
        )

        self.assertEqual(result["label"], STALE_LABEL)
        self.assertEqual(result["lastFailure"], "MPV audio output failed")

    def test_unready_route_keeps_renderer_failure(self):
        result = service.public_track_status(
            metadata(omniphonyRoute={"ready": False}), "paused"
        )

        self.assertEqual(result["label"], STALE_LABEL)
        self.assertEqual(result["lastFailure"], STALE_LABEL)

    def test_missing_route_keeps_renderer_failure(self):
        value = metadata()
        value.pop("omniphonyRoute")

        result = service.public_track_status(value, "paused")

        self.assertEqual(result["label"], STALE_LABEL)
        self.assertEqual(result["lastFailure"], STALE_LABEL)

    def test_failed_state_is_never_normalized(self):
        result = service.public_track_status(metadata(), "failed")

        self.assertEqual(result["label"], STALE_LABEL)
        self.assertEqual(result["lastFailure"], STALE_LABEL)

    def test_loading_state_is_not_treated_as_verified_active_playback(self):
        result = service.public_track_status(metadata(), "loading")

        self.assertEqual(result["label"], STALE_LABEL)
        self.assertEqual(result["lastFailure"], STALE_LABEL)

    def test_stalled_state_is_not_treated_as_verified_active_playback(self):
        result = service.public_track_status(metadata(), "stalled")

        self.assertEqual(result["label"], STALE_LABEL)
        self.assertEqual(result["lastFailure"], STALE_LABEL)

    def test_prepared_joc_with_verified_route_is_normalized(self):
        result = service.public_track_status(metadata(), "prepared")

        self.assertEqual(result["label"], HEALTHY_JOC_LABEL)
        self.assertIsNone(result["lastFailure"])

    def test_other_codec_is_never_normalized(self):
        result = service.public_track_status(
            metadata(format="AC4_IMS"), "paused"
        )

        self.assertEqual(result["label"], STALE_LABEL)
        self.assertEqual(result["lastFailure"], STALE_LABEL)

    def test_only_exact_renderer_label_is_normalized(self):
        other_label = "Another renderer disconnected"
        result = service.public_track_status(
            metadata(label=other_label, lastFailure=None), "paused"
        )

        self.assertEqual(result["label"], other_label)
        self.assertEqual(result["lastFailure"], other_label)


class StatusEndpointIntegrationTests(unittest.TestCase):
    class FakeEngine:
        def __init__(self, failure):
            self.playback_generation_lock = threading.RLock()
            self.playback_lock = threading.RLock()
            self.last_seek_diagnostic = None
            self.mpv_instance_id = "mpv-test-instance"
            self.proc = SimpleNamespace(pid=123)
            self.pending_generation = 1
            self.active_generation = 1
            self.prepared_generation = 0
            self.current_meta = metadata(
                generation=1,
                requestId="joc-request",
                playbackState="paused",
                playTarget="fixture-path",
                lastFailure=failure,
            )
            self.stream_proxy = None
            self.command_calls = []

        def is_mpv_media_loading(self):
            return False

        def is_playback_preparing(self):
            return False

        def clock_cmd(self, args):
            values = {
                "time-pos": 20.0,
                "duration": 189.0,
                "pause": True,
                "audio-device": "fixture-device",
                "audio-codec-name": "eac3",
                "audio-params": {"samplerate": 48000, "channel-count": 2},
                "audio-out-params": {"samplerate": 96000, "channel-count": 8},
                "audio-bitrate": 768000,
                "current-tracks/audio/decoder": "orender",
                "path": "fixture-path",
            }
            prop = args[-1].removeprefix("current-tracks/audio/")
            if prop == "decoder":
                prop = "current-tracks/audio/decoder"
            return {"error": "success", "data": values[prop], "request_id": 7}

        def update_playback_state(self, *_args):
            self.current_meta["playbackState"] = "paused"
            return "paused"

        def cmd(self, args):
            self.command_calls.append(args)
            if args[:2] == ["set_property", "pause"]:
                return {"error": "success", "data": True}
            return {"error": "success", "data": None}

    def fetch_status(self, server):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/status", timeout=3
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_ready_route_clears_only_the_exact_stale_failure(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        engine = self.FakeEngine("Omniphony renderer disconnected")
        route = {"ready": True, "checks": {}}

        try:
            thread.start()
            with patch.object(service, "engine", engine), \
                    patch.object(service, "mpv_path_matches", return_value=True), \
                    patch.object(service, "get_omniphony_route_status", return_value=route), \
                    patch.object(service, "is_active_orender_decoder", return_value=True), \
                    patch.object(service, "joc_route_is_verified", return_value=True), \
                    patch.object(service, "refresh_invalidated_joc_route_telemetry"):
                status = self.fetch_status(server)

            self.assertTrue(status["omniphonyRoute"]["ready"])
            self.assertEqual(status["label"], HEALTHY_JOC_LABEL)
            self.assertIsNone(status["lastFailure"])
            self.assertEqual(status["playbackState"], "paused")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_ready_route_preserves_unrelated_failure(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        engine = self.FakeEngine("MPV audio output failed")
        route = {"ready": True, "checks": {}}

        try:
            thread.start()
            with patch.object(service, "engine", engine), \
                    patch.object(service, "mpv_path_matches", return_value=True), \
                    patch.object(service, "get_omniphony_route_status", return_value=route), \
                    patch.object(service, "is_active_orender_decoder", return_value=True), \
                    patch.object(service, "joc_route_is_verified", return_value=True), \
                    patch.object(service, "refresh_invalidated_joc_route_telemetry"):
                status = self.fetch_status(server)

            self.assertTrue(status["omniphonyRoute"]["ready"])
            self.assertEqual(status["lastFailure"], "MPV audio output failed")
            self.assertEqual(status["label"], STALE_LABEL)
            self.assertEqual(status["playbackState"], "paused")
            self.assertEqual(engine.command_calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
