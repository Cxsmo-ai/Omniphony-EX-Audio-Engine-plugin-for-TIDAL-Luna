
import io
import unittest
from unittest.mock import patch, MagicMock
import json
import os
import struct
import tempfile
import wave
from pathlib import Path
import omnifusion_service as service

class DownloaderTests(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(service.sanitize_filename_component('AC/DC: "Back in Black"'), 'AC_DC_ _Back in Black_')

    def test_pack_and_inject_flac_metadata(self):
        # 4-byte magic + 4-byte header + 34-byte STREAMINFO + audio
        streaminfo = b"\x00" * 34
        header = bytes([0x80 | 0, 0, 0, 34])
        audio = b"\xff\xf8" + b"\x00" * 100
        raw_flac = b"fLaC" + header + streaminfo + audio

        comments = {
            "TITLE": "Test Track",
            "ARTIST": "Test Artist",
            "SPATIAL_BINAURAL": "Dolby Atmos 5.1 SOFA"
        }
        tagged = service.inject_flac_metadata(raw_flac, comments, picture_bytes=b"dummy_jpeg_bytes")
        self.assertTrue(tagged.startswith(b"fLaC"))
        self.assertIn(b"TITLE=Test Track", tagged)
        self.assertIn(b"ARTIST=Test Artist", tagged)
        self.assertIn(b"SPATIAL_BINAURAL=Dolby Atmos 5.1 SOFA", tagged)
        self.assertIn(b"image/jpeg", tagged)

    @staticmethod
    def _flac(blocks, audio=b"\xff\xf8\x00\x00"):
        out = bytearray(b"fLaC")
        for index, (block_type, data) in enumerate(blocks):
            out.append((0x80 if index == len(blocks) - 1 else 0) | block_type)
            out.extend(len(data).to_bytes(3, "big"))
            out.extend(data)
        return bytes(out) + audio

    @staticmethod
    def _flac_blocks(data):
        blocks = []
        pos = 4
        while True:
            header = data[pos]
            size = int.from_bytes(data[pos + 1:pos + 4], "big")
            blocks.append((header & 0x7F, data[pos + 4:pos + 4 + size]))
            pos += 4 + size
            if header & 0x80:
                return blocks, data[pos:]

    def test_flac_metadata_keeps_non_comment_blocks_and_existing_art(self):
        old_comments = service.pack_flac_metadata({"ALBUM": "Original"})[0][1]
        picture = b"old-cover"
        blocks = [(0, bytes(34)), (2, b"application-data"), (3, b"seek-table"),
                  (4, old_comments), (6, picture)]
        raw = self._flac(blocks)

        tagged = service.inject_flac_metadata(raw, {"TITLE": "New title", "ALBUM": "Updated"})
        result_blocks, audio = self._flac_blocks(tagged)

        self.assertEqual(audio, b"\xff\xf8\x00\x00")
        self.assertIn((2, b"application-data"), result_blocks)
        self.assertIn((3, b"seek-table"), result_blocks)
        self.assertIn((6, picture), result_blocks)
        comment_data = next(data for kind, data in result_blocks if kind == 4)
        self.assertIn(b"TITLE=New title", comment_data)
        self.assertIn(b"ALBUM=Updated", comment_data)

    def test_flac_metadata_rejects_invalid_and_truncated_streams(self):
        with self.assertRaisesRegex(ValueError, "not a FLAC"):
            service.inject_flac_metadata(b"not-flac", {})
        with self.assertRaisesRegex(ValueError, "truncated FLAC metadata"):
            service.inject_flac_metadata(b"fLaC\x80\x00", {})
        with self.assertRaisesRegex(ValueError, "no audio frames"):
            service.inject_flac_metadata(self._flac([(0, bytes(34))], audio=b""), {})

    def test_download_path_rejects_escape_and_sanitizes_windows_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for pattern in ("../{title}", r"C:\\outside\\{title}", "{unknown}"):
                with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                    service.build_download_output_path(root, pattern, {"{title}": "Song"}, "flac")
            path = service.build_download_output_path(root, "{artist}/{title}",
                                                      {"{artist}": "CON", "{title}": "song."}, "flac")
            self.assertEqual(path, root / "_CON" / "song.flac")

    def test_publication_allocates_unique_name_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "track.flac"
            first.write_bytes(b"existing")
            staged = root / ".stage.tmp"
            staged.write_bytes(b"new")

            published = service.publish_file_without_overwrite(staged, first)

            self.assertEqual(published, root / "track (2).flac")
            self.assertEqual(first.read_bytes(), b"existing")
            self.assertEqual(published.read_bytes(), b"new")
            self.assertFalse(staged.exists())

    def test_wav_tags_preserve_pcm_and_embed_metadata_art_and_lyrics(self):
        fmt = struct.pack("<HHIIHH", 1, 2, 48000, 192000, 4, 16)
        pcm = b"\x01\x02\x03\x04"
        chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
        chunks += b"data" + struct.pack("<I", len(pcm)) + pcm
        raw = b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks
        jpeg = b"\xff\xd8\xffpayload\xff\xd9"
        tagged = service.tag_wav_bytes(raw, {
            "TITLE": "Tagged title", "ARTIST": "Artist", "ALBUM": "Album",
            "LYRICS": "plain lyric", "SYNCEDLYRICS": "[00:01.20]timed lyric",
        }, jpeg, "image/jpeg")

        self.assertEqual(tagged[:4], b"RIFF")
        self.assertEqual(struct.unpack_from("<I", tagged, 4)[0], len(tagged) - 8)
        self.assertIn(b"plain lyric", tagged)
        self.assertIn(b"timed lyric", tagged)
        self.assertIn(jpeg, tagged)
        self.assertIn(b"INAM", tagged)
        self.assertIn(pcm, tagged)
        with wave.open(io.BytesIO(tagged), "rb") as reader:
            self.assertEqual(reader.getframerate(), 48000)
            self.assertEqual(reader.getnchannels(), 2)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.readframes(reader.getnframes()), pcm)

    def test_picture_validation_rejects_html_and_detects_jpeg(self):
        self.assertEqual(service.validate_picture_bytes(b"<html>not art</html>"), (None, None))
        data = b"\xff\xd8\xffimage\xff\xd9"
        self.assertEqual(service.validate_picture_bytes(data), (data, "image/jpeg"))

    def test_flac_stream_inspection_reports_encoded_properties(self):
        packed = (96000 << 44) | (1 << 41) | (23 << 36) | 48000
        streaminfo = bytearray(34)
        streaminfo[10:18] = packed.to_bytes(8, "big")
        self.assertEqual(service.inspect_flac_stream(self._flac([(0, bytes(streaminfo))])), {
            "sampleRate": 96000, "channels": 2, "bitsPerSample": 24, "totalSamples": 48000,
        })

    def test_provider_metadata_is_bound_to_resolver_payload(self):
        state = {"title": "request title", "lyrics_text": ""}
        service.apply_provider_metadata({
            "title": "Amazon title", "artists": [{"name": "Artist A"}, {"name": "Artist B"}],
            "album": {"name": "Album"}, "releaseDate": "2025-03-01", "trackNumber": 4,
            "artworkUrl": "https://art.invalid/cover.jpg", "syncedLyrics": "[00:01.00]line",
        }, state)
        self.assertEqual(state["title"], "Amazon title")
        self.assertEqual(state["artist"], "Artist A; Artist B")
        self.assertEqual(state["album"], "Album")
        self.assertEqual(state["year"], "2025")
        self.assertEqual(state["track_num"], "4")
        self.assertEqual(state["art_url"], "https://art.invalid/cover.jpg")
        self.assertEqual(state["lyrics_synced"], "[00:01.00]line")

if __name__ == "__main__":
    unittest.main()
