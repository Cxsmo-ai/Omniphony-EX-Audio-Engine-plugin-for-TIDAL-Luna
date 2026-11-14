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


class StreamTransportStatusTests(unittest.TestCase):
    def test_no_proxy_preserves_null_transport(self):
        self.assertIsNone(service.stream_transport_label(None))

    def test_dash_proxy_reports_dash_transport(self):
        proxy = service.TidalDashProxy.__new__(service.TidalDashProxy)
        self.assertEqual(service.stream_transport_label(proxy), "tidal-dash-proxy")

    def test_ac4_proxy_reports_oxideav_raw_pcm_transport(self):
        proxy = service.Ac4RawPcmProxy.__new__(service.Ac4RawPcmProxy)
        self.assertEqual(service.stream_transport_label(proxy), "oxideav-raw-s16le")

    def test_unrecognized_proxy_is_not_mislabeled_as_dash(self):
        class UnknownProxy:
            pass

        self.assertIsNone(service.stream_transport_label(UnknownProxy()))

    def test_active_track_status_uses_transport_from_its_metadata(self):
        metadata = {
            "trackId": 97874144,
            "format": "AC4_IMS",
            "streamTransport": "oxideav-raw-s16le",
        }
        status = service.public_track_status(metadata, "playing")
        self.assertEqual(status["streamTransport"], "oxideav-raw-s16le")

    def test_codec_switch_replaces_transport_with_the_new_generation(self):
        joc = service.public_track_status({
            "trackId": 97874148,
            "format": "EAC3_JOC",
            "streamTransport": "tidal-dash-proxy",
        }, "playing")
        ac4 = service.public_track_status({
            "trackId": 97874144,
            "format": "AC4_IMS",
            "streamTransport": "oxideav-raw-s16le",
        }, "playing")
        self.assertEqual(joc["streamTransport"], "tidal-dash-proxy")
        self.assertEqual(ac4["streamTransport"], "oxideav-raw-s16le")

    def test_idle_status_keeps_null_transport(self):
        status = service.public_track_status({}, "idle")
        self.assertIsNone(status["streamTransport"])

    def test_status_endpoint_reports_transport_and_proxy_counters_for_committed_proxy(self):
        class FakeEngine:
            def __init__(self):
                self.playback_generation_lock = threading.RLock()
                self.playback_lock = threading.RLock()
                self.last_seek_diagnostic = None
                self.mpv_instance_id = "mpv-test-instance"
                self.proc = SimpleNamespace(pid=123)
                self.pending_generation = 1
                self.active_generation = 1
                self.prepared_generation = 0
                self.current_meta = {}
                self.stream_proxy = None

            def is_mpv_media_loading(self):
                return False

            def is_playback_preparing(self):
                return False

            def clock_cmd(self, args):
                prop = args[-1]
                values = {
                    "time-pos": 12.5,
                    "duration": 180.0,
                    "pause": False,
                    "audio-device": "fixture-device",
                    "audio-codec-name": "pcm_s16le",
                    "audio-params": {"samplerate": 48000, "channel-count": 2},
                    "audio-out-params": {"samplerate": 48000, "channel-count": 2},
                    "audio-bitrate": 256000,
                    "current-tracks/audio/decoder": "lavc",
                    "path": "fixture-path",
                }
                return {"error": "success", "data": values[prop], "request_id": 7}

            def update_playback_state(self, *_args):
                return "playing"

        engine = FakeEngine()
        dash_proxy = service.TidalDashProxy.__new__(service.TidalDashProxy)
        dash_proxy.segment_requests = 11
        dash_proxy.bytes_served = 2048
        dash_proxy.failed_segments = 2
        ac4_proxy = service.Ac4RawPcmProxy.__new__(service.Ac4RawPcmProxy)
        ac4_proxy.segment_requests = 5
        ac4_proxy.bytes_served = 96000
        ac4_proxy.failed_segments = 1

        server = ThreadingHTTPServer(("127.0.0.1", 0), service.RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)

        def fetch_status():
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/status", timeout=3
            ) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            thread.start()
            with patch.object(service, "engine", engine), \
                    patch.object(service, "load_settings", return_value={"audio_device": "fixture-device"}), \
                    patch.object(service, "refresh_invalidated_joc_route_telemetry"), \
                    patch.object(service, "get_omniphony_route_status", return_value={"ready": True, "checks": {}}), \
                    patch.object(service, "is_active_orender_decoder", return_value=True), \
                    patch.object(service, "joc_route_is_verified", return_value=True):
                cases = (
                    ({"trackId": 97874144, "format": "AC4_IMS", "generation": 1}, ac4_proxy,
                     "oxideav-raw-s16le", 5, 96000, 1),
                    ({"trackId": 97874148, "format": "EAC3_JOC", "generation": 1,
                      "requestId": "joc-request"}, dash_proxy,
                     "tidal-dash-proxy", 11, 2048, 2),
                    ({"trackId": 17, "format": "HI_RES_LOSSLESS", "generation": 1},
                     SimpleNamespace(segment_requests=99, bytes_served=99, failed_segments=99),
                     None, 0, 0, 0),
                    ({"trackId": None, "format": None, "generation": None}, None,
                     None, 0, 0, 0),
                )
                for metadata, proxy, label, requests, byte_count, failures in cases:
                    with self.subTest(format=metadata.get("format")):
                        with engine.playback_generation_lock:
                            engine.current_meta = {
                                **metadata,
                                "streamTransport": service.stream_transport_label(proxy),
                            }
                            engine.stream_proxy = proxy
                            engine.pending_generation = metadata.get("generation") or 0
                            engine.active_generation = metadata.get("generation") or 0
                        status = fetch_status()
                        self.assertEqual(status["streamTransport"], label)
                        self.assertEqual(status["streamProxyRequests"], requests)
                        self.assertEqual(status["streamProxyBytes"], byte_count)
                        self.assertEqual(status["streamProxyFailures"], failures)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
