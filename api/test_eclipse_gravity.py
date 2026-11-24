import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import omnifusion_service as service


class GravityResolverTests(unittest.TestCase):
    manifest_url = "https://gravity.example/account/manifest.json"

    def resolve_payload(self, payload, requested_fmt="EAC3_JOC", payload_sequence=None):
        requests = []
        responses = iter(payload_sequence or [payload])

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            return io.BytesIO(json.dumps(next(responses)).encode("utf-8"))

        with patch.object(
            service,
            "_ECLIPSE_AUTH_CACHE",
            {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}},
        ), patch.object(service, "_get_eclipse_addon_access_token", return_value=None), patch.object(
            service, "_eclipse_authenticated_urlopen", side_effect=fake_urlopen
        ), patch.object(service, "_probe_direct_audio_bitrate", return_value=(768_000, "test")), patch.object(
            service, "_probe_direct_media_transport", return_value="DIRECT"
        ):
            result = service.resolve_eclipse_addon(
                self.manifest_url,
                "USUM71806694",
                requested_fmt,
                explicit_track_id="gravity-natural",
                addon_id="app.eclipsemusic.gravity",
                addon_name="Gravity",
            )
        return result, requests

    def test_gravity_requests_declared_atmos_quality_and_accepts_explicit_joc(self):
        result, requests = self.resolve_payload(
            {
                "url": "https://cdn.example/natural.m3u8",
                "codec": "eac3_joc",
                "format": "hls",
                "quality": "Atmos",
                "sampleRate": 48000,
                "channels": 6,
                "encrypted": False,
            }
        )

        self.assertEqual(result["format"], "EAC3_JOC")
        self.assertEqual(result["codec"], "eac3_joc")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0].full_url).query)["quality"], ["Atmos"])

    def test_atmos_label_or_generic_codec_does_not_prove_joc(self):
        for codec in ("eac3", "ec-3", "ac4", "aac", None):
            with self.subTest(codec=codec):
                result, _ = self.resolve_payload(
                    {
                        "url": "https://cdn.example/stream",
                        "codec": codec,
                        "format": "Atmos",
                        "quality": "Atmos",
                    }
                )
                self.assertIsNone(result)

    def test_gravity_rejects_protected_stream_for_desktop_route(self):
        result, _ = self.resolve_payload(
            {
                "url": "https://cdn.example/protected.m3u8",
                "codec": "eac3_joc",
                "format": "hls",
                "quality": "Atmos",
                "encrypted": True,
                "drm": {"system": "widevine"},
            }
        )

        self.assertIsNone(result)

    def test_parent_level_drm_metadata_rejects_nested_streams(self):
        result, _ = self.resolve_payload(
            {
                "drm": {"system": "widevine"},
                "streams": [
                    {
                        "url": "https://cdn.example/protected.m3u8",
                        "codec": "eac3_joc",
                        "format": "hls",
                    }
                ],
            }
        )

        self.assertIsNone(result)

    def test_lossless_fallback_parent_drm_is_checked_after_retry(self):
        result, requests = self.resolve_payload(
            {},
            requested_fmt="LOSSLESS",
            payload_sequence=[
                {
                    "url": "https://cdn.example/max.flac",
                    "codec": "flac",
                    "sampleRate": 96000,
                    "bitDepth": 24,
                },
                {
                    "drm": {"system": "widevine"},
                    "streams": [
                        {
                            "url": "https://cdn.example/lossless.m3u8",
                            "codec": "flac",
                            "format": "hls",
                        }
                    ],
                },
            ],
        )

        self.assertIsNone(result)
        self.assertEqual(len(requests), 2)

    def test_candidate_level_drm_skips_protected_stream_and_uses_clear_candidate(self):
        result, _ = self.resolve_payload(
            {
                "streams": [
                    {
                        "url": "https://cdn.example/protected.m3u8",
                        "codec": "eac3_joc",
                        "format": "hls",
                        "drm": {"system": "widevine"},
                    },
                    {
                        "url": "https://cdn.example/clear.m3u8",
                        "codec": "eac3_joc",
                        "format": "hls",
                    },
                ]
            }
        )

        self.assertEqual(result["uri"], "https://cdn.example/clear.m3u8")

    def test_gravity_rejects_ac4_even_when_sample_rate_looks_hires(self):
        result, _ = self.resolve_payload(
            {
                "url": "https://cdn.example/audio.ac4",
                "codec": "ac4",
                "format": "ac4",
                "sampleRate": 96000,
                "bitDepth": 24,
            },
            requested_fmt="HI_RES_LOSSLESS",
        )

        self.assertIsNone(result)

    def test_ac4_ims_never_requests_an_eclipse_stream(self):
        with patch.object(service, "_eclipse_authenticated_urlopen") as urlopen:
            result = service.resolve_eclipse_addon(
                self.manifest_url,
                "USUM71806694",
                "AC4_IMS",
                explicit_track_id="gravity-natural",
                addon_id="app.eclipsemusic.gravity",
                addon_name="Gravity",
            )

        self.assertIsNone(result)
        urlopen.assert_not_called()

    def test_http_415_is_a_clear_protected_playback_failure(self):
        error = urllib.error.HTTPError(
            "https://gravity.example/stream/gravity-natural?quality=Atmos",
            415,
            "Unsupported Media Type",
            {},
            io.BytesIO(b"protected playback required"),
        )
        with patch.object(
            service,
            "_ECLIPSE_AUTH_CACHE",
            {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}},
        ), patch.object(service, "_get_eclipse_addon_access_token", return_value=None), patch.object(
            service, "_eclipse_authenticated_urlopen", side_effect=error
        ):
            result = service.resolve_eclipse_addon(
                self.manifest_url,
                "USUM71806694",
                "EAC3_JOC",
                explicit_track_id="gravity-natural",
                addon_id="app.eclipsemusic.gravity",
                addon_name="Gravity",
            )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
