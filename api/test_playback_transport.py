import os
import time
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import omnifusion_service as service


class ProbeResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = BytesIO(body)
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        self.read_sizes.append(size)
        return self.body.read(size)


class PlaybackTransportTests(unittest.TestCase):
    def test_mp4_transport_selects_mov_demuxer(self):
        calls = []

        def command(args):
            calls.append(args)
            return {"error": "success"}

        result = service.prepare_mpv_demuxer_format("EAC3_JOC", command, "MP4")

        self.assertEqual(result, "mov")
        self.assertEqual(calls, [
            ["set_property", "options/demuxer", "lavf"],
            ["set_property", "demuxer-lavf-format", "mov"],
        ])

    def test_dash_transport_still_selects_dash_demuxer(self):
        calls = []

        def command(args):
            calls.append(args)
            return {"error": "success"}

        result = service.prepare_mpv_demuxer_format("EAC3_JOC", command, "DASH")

        self.assertEqual(result, "dash")
        self.assertEqual(calls, [
            ["set_property", "options/demuxer", "lavf"],
            ["set_property", "demuxer-lavf-format", "dash"],
        ])

    def test_direct_track_resets_mp4_demuxer_override(self):
        calls = []

        def command(args):
            calls.append(args)
            return {"error": "success"}

        self.assertEqual(service.prepare_mpv_demuxer_format("EAC3_JOC", command, "MP4"), "mov")
        self.assertEqual(service.prepare_mpv_demuxer_format("LOSSLESS", command, "DIRECT"), "")
        self.assertEqual(
            [call[-1] for call in calls],
            ["lavf", "mov", "auto", ""],
            "a following direct stream restores automatic demuxer selection and probing",
        )

    def test_extensionless_audio_mp4_is_detected_from_mime_and_ftyp(self):
        class Response:
            status = 206
            headers = {"Content-Type": "audio/mp4", "Content-Range": "bytes 0-23/1000"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size):
                self.read_size = size
                return bytes.fromhex("000000206674797069736f6d0000020069736f6d64627931")

        with patch.object(service.urllib.request, "urlopen", return_value=Response()) as open_url:
            result = service._probe_direct_media_transport("https://signed.example/opaque", {"Accept": "*/*"})

        self.assertEqual(result, "MP4")
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Range"), "bytes=0-63")

    def test_ftyp_must_be_the_first_plausible_box(self):
        class Response:
            status = 206
            headers = {"Content-Type": "audio/mp4", "Content-Range": "bytes 0-63/1000"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b"\x00\x00\x00\x18junkftypisom\x00\x00\x00\x00"

        with patch.object(service.urllib.request, "urlopen", return_value=Response()):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})

        self.assertEqual(result, "DIRECT")

    def test_partial_response_must_begin_at_byte_zero(self):
        class Response:
            status = 206
            headers = {"Content-Type": "audio/mp4", "Content-Range": "bytes 8-31/1000"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                raise AssertionError("invalid range response must be rejected before reading")

        with patch.object(service.urllib.request, "urlopen", return_value=Response()):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})

        self.assertEqual(result, "DIRECT")

    def test_partial_response_must_stay_within_requested_range(self):
        response = ProbeResponse(
            206,
            {"Content-Type": "audio/mp4", "Content-Range": "bytes 0-64/1000"},
            bytes.fromhex("000000206674797069736f6d0000020069736f6d64627931"),
        )
        with patch.object(service.urllib.request, "urlopen", return_value=response):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})

        self.assertEqual(result, "DIRECT")
        self.assertEqual(response.read_sizes, [])

    def test_partial_response_body_must_match_advertised_range(self):
        response = ProbeResponse(
            206,
            {
                "Content-Type": "audio/mp4",
                "Content-Range": "bytes 0-63/1000",
                "Content-Length": "64",
            },
            bytes.fromhex("000000206674797069736f6d0000020069736f6d64627931"),
        )
        with patch.object(service.urllib.request, "urlopen", return_value=response):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})

        self.assertEqual(result, "DIRECT")
        self.assertEqual(response.read_sizes, [64])

    def test_partial_response_requires_valid_total_and_range(self):
        for content_range in ("", "bytes 0-23/23", "bytes 0-63/1000, bytes 0-1/2"):
            with self.subTest(content_range=content_range):
                response = ProbeResponse(
                    206,
                    {"Content-Type": "audio/mp4", "Content-Range": content_range},
                    b"",
                )
                with patch.object(service.urllib.request, "urlopen", return_value=response):
                    result = service._probe_direct_media_transport("https://signed.example/opaque", {})
                self.assertEqual(result, "DIRECT")
                self.assertEqual(response.read_sizes, [])

    def test_multipart_partial_response_is_not_treated_as_mp4(self):
        response = ProbeResponse(
            206,
            {
                "Content-Type": "multipart/byteranges; boundary=example",
                "Content-Range": "bytes 0-23/1000",
            },
            bytes.fromhex("000000206674797069736f6d0000020069736f6d64627931"),
        )
        with patch.object(service.urllib.request, "urlopen", return_value=response):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})

        self.assertEqual(result, "DIRECT")

    def test_mp4_probe_streams_only_a_bounded_prefix_when_range_is_ignored(self):
        body = bytes.fromhex("000000206674797069736f6d0000020069736f6d64627931") + b"x" * 256
        response = ProbeResponse(
            200,
            {"Content-Type": "audio/mp4", "Content-Length": str(len(body))},
            body,
        )
        with patch.object(service.urllib.request, "urlopen", return_value=response):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})

        self.assertEqual(result, "MP4")
        self.assertEqual(response.read_sizes, [64])

    def test_extended_size_ftyp_box_is_supported_and_invalid_size_rejected(self):
        extended = bytes.fromhex("0000000166747970000000000000002069736f6d00000200")
        response = ProbeResponse(
            206,
            {"Content-Type": "audio/mp4", "Content-Range": "bytes 0-23/1000"},
            extended,
        )
        with patch.object(service.urllib.request, "urlopen", return_value=response):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})
        self.assertEqual(result, "MP4")

        invalid = ProbeResponse(
            200,
            {"Content-Type": "audio/mp4"},
            bytes.fromhex("000000086674797069736f6d00000200"),
        )
        with patch.object(service.urllib.request, "urlopen", return_value=invalid):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})
        self.assertEqual(result, "DIRECT")

    def test_mime_without_iso_bmff_signature_keeps_direct_transport(self):
        class Response:
            headers = {"Content-Type": "audio/mp4"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b"not an MP4 response"

        with patch.object(service.urllib.request, "urlopen", return_value=Response()):
            result = service._probe_direct_media_transport("https://signed.example/opaque", {})

        self.assertEqual(result, "DIRECT")

    def test_ac4_cache_pruning_preserves_active_source_and_unrelated_files(self):
        with TemporaryDirectory() as temp:
            cache = Path(temp)
            stale = cache / f"ac4_1_{'a' * 20}.mp4"
            active = cache / f"ac4_2_{'b' * 20}.mp4"
            unrelated = cache / "downloaded-track.wav"
            stale.write_bytes(b"x" * 8)
            active.write_bytes(b"y" * 5)
            unrelated.write_bytes(b"z" * 3)
            old_time = time.time() - 60
            os.utime(stale, (old_time, old_time))
            os.utime(active, (old_time, old_time))

            result = service.prune_ac4_playback_cache(
                cache,
                protected_paths=(active,),
                max_bytes=6,
                min_age_seconds=0,
            )

            self.assertFalse(stale.exists())
            self.assertTrue(active.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(result["removedFiles"], 1)
            self.assertEqual(result["removedBytes"], 8)
            self.assertEqual(result["remainingBytes"], 5)

    def test_ac4_cache_pruning_removes_only_old_partial_files(self):
        with TemporaryDirectory() as temp:
            cache = Path(temp)
            stale = cache / f".ac4_3_{'c' * 32}.download"
            fresh = cache / f".ac4_4_{'d' * 32}.download"
            stale.write_bytes(b"old")
            fresh.write_bytes(b"new")
            old_time = time.time() - 200
            os.utime(stale, (old_time, old_time))

            result = service.prune_ac4_playback_cache(
                cache,
                max_bytes=1024,
                partial_max_age_seconds=100,
            )

            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertEqual(result["removedFiles"], 1)
            self.assertEqual(result["removedBytes"], 3)


if __name__ == "__main__":
    unittest.main()
