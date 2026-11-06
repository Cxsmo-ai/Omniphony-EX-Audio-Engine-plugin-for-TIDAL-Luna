"""
omnifusion_service.py - Ultra-Reliable Standalone Audio Engine & TIDAL RealMax API
Manages headless libmpv, zero-leak multi-profile stream resolving, and native device selection.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import datetime
import hashlib
import http.server
import json
import math
import msvcrt
import os
import re
import socket
import shutil
import socketserver
import struct
import subprocess
import sys
import threading
import tempfile
import time
import xml.etree.ElementTree as ET
import urllib.parse
import uuid
import wave
from pathlib import Path
# Keep runtime-only third-party packages alongside the portable service.
SERVICE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = SERVICE_DIR / "vendor"
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))
import requests
import psutil

STANDALONE_DIR = Path(__file__).resolve().parent.parent
MPV_DIR = STANDALONE_DIR / "mpv"
OXIDE_DIR = STANDALONE_DIR / "oxideav"
CACHE_DIR = STANDALONE_DIR / "cache"
AC4_CONVERTER = OXIDE_DIR / "adapter-v0.1.71" / "ac4-to-wav.exe"
STUDIO_EXE = STANDALONE_DIR / "studio" / "omniphony-studio-egui.exe"
RENDERER_EXE = STUDIO_EXE.parent / "orender.exe"
ORENDER_PIPE = r"\\.\pipe\orender.input"
ORENDER_DLL = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "omniphony" / "lib" / "orender.dll"
STRICT_RENDER_DECODER = "orender"
CONFIG_DIR = STANDALONE_DIR / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
STUDIO_CONFIG_FILE = CONFIG_DIR / "omniphony" / "config.yaml"
SESSION_FILE = CONFIG_DIR / "tidal-session-oauth.json"
TIDAL_TV_SESSION_FILE = CONFIG_DIR / "tidal-session-tv.json"
PYTHON_TIDAL_SESSION_FILE = Path(os.environ.get("OMNIFUSION_PYTHON_TIDAL_SESSION", str(CONFIG_DIR / "tidal-session-pkce.json")))

MPV_EXE = MPV_DIR / "mpv.exe"
PIPE_NAME = "omnifusion-standalone.mpv"
PIPE_PATH = f"//./pipe/{PIPE_NAME}".replace("/", "\\")
PORT = 8765
SERVICE_VERSION = "2.1.25"
SERVICE_INSTANCE_ID = uuid.uuid4().hex
EOF_REWIND_TIMEOUT_SECONDS = 3.0

DEFAULT_SETTINGS = {
    "priority": ["AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"],
    "audio_device": "auto",
    "volume": 100,
}
SUPPORTED_FORMAT_PRIORITY = ("AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS")
CANCELLED_PLAY_REQUEST_TTL = 600.0
MAX_CANCELLED_PLAY_REQUESTS = 512
SEEK_SETTLE_TIMEOUT_SECONDS = 2.0
SEEK_SETTLE_CONFIRMATION_SAMPLES = 3
SEEK_POSITION_TOLERANCE_SECONDS = 0.25
SEEK_SETTLE_STABILITY_SECONDS = 0.30
USER_SEEK_MODE = "absolute"


def validate_play_request_id(value) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("requestId must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("requestId must be a UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError("requestId must be a canonical UUID")
    return str(parsed)

TRACK_STATUS_FIELDS = (
    "format",
    "codec",
    "title",
    "artist",
    "audioOutputProfile",
    "omniphonyRoute",
    "sourceBitrate",
    "sourceSampleRate",
    "sourceChannels",
    "sourceBitDepth",
    "sourceCodec",
    "sourceBitrateBasis",
    "sourceAuthProfile",
    "requestedQuality",
    "sourceQuality",
    "sourceAudioMode",
    "presentation",
    "decodedSampleRate",
    "decodedChannels",
    "requestId",
)


def public_track_status(metadata: dict, playback_state: str) -> dict:
    """Expose stream metadata only while it belongs to a known active track."""
    track_id = metadata.get("trackId")
    state = str(playback_state or "idle").lower()
    last_failure = metadata.get("lastFailure")
    label = str(metadata.get("label") or "")
    if not last_failure and state == "failed":
        last_failure = label or "Playback failed"
    elif not last_failure and any(
        marker in label.casefold()
        for marker in ("fail", "error", "unavailable", "disconnected", "not active")
    ):
        last_failure = label

    if track_id is None:
        if state in {"idle", "stopped", "ended"}:
            status_label = "Idle"
        elif state == "loading":
            status_label = "Loading"
        elif state == "failed":
            status_label = label or "Playback failed"
        else:
            status_label = "No active track"
        fields = {key: None for key in TRACK_STATUS_FIELDS}
        fields.update({
            "trackId": None,
            "playbackGeneration": None,
            "playbackState": playback_state,
            "format": None,
            "codec": None,
            "label": status_label,
            "title": None,
            "artist": None,
            "presentationVerified": False,
            "lastFailure": last_failure,
        })
        return fields

    fields = {key: metadata.get(key) for key in TRACK_STATUS_FIELDS}
    fields["audioOutputProfile"] = metadata.get("audioProfile")
    fields.update({
        "trackId": track_id,
        "playbackGeneration": metadata.get("generation"),
        "playbackState": playback_state,
        "format": metadata.get("format"),
        "codec": metadata.get("codec"),
        "label": metadata.get("label"),
        "title": metadata.get("title"),
        "artist": metadata.get("artist"),
        "presentationVerified": metadata.get("presentationVerified", False),
        "lastFailure": last_failure,
    })
    return fields
ALLOWED_BROWSER_ORIGINS = frozenset({
    "https://desktop.tidal.com",
    "https://listen.tidal.com",
})
SETTINGS_LOCK = threading.RLock()

# python-tidal is the source of truth for TIDAL track capabilities and the
# quality actually returned for the authenticated account.  The TIDAL Luna
# UI and its playback handoff remain the transport authority; this metadata
# prevents a generic FLAC manifest from being mislabeled as Hi-Res.
_TIDAL_API_LOCK = threading.RLock()
_TIDAL_API_SESSION = None
_TIDAL_MOBILE_API_SESSION = None
_TIDAL_TV_API_SESSION = None
_TIDAL_API_TRACK_CACHE: dict[int, tuple[float, dict]] = {}
_TIDAL_API_TRACK_CACHE_TTL = 30.0


def validate_oxideav_wav(path: Path) -> dict:
    with wave.open(str(path), "rb") as decoded:
        channels = decoded.getnchannels()
        width = decoded.getsampwidth()
        rate = decoded.getframerate()
        frames = decoded.getnframes()
        if channels != 2 or width not in (2, 3, 4) or not (8000 <= rate <= 192000) or frames <= 0:
            raise RuntimeError("OxideAV output failed stereo PCM validation")
        frame_bytes = channels * width
        remaining = frames
        while remaining:
            requested = min(65536, remaining)
            chunk = decoded.readframes(requested)
            if len(chunk) != requested * frame_bytes:
                raise RuntimeError("OxideAV output contains a truncated PCM payload")
            remaining -= requested
    return {"channels": channels, "sampleRate": rate, "sampleWidth": width, "frames": frames}


def mpv_command_succeeded(result: dict | None) -> bool:
    return bool(result) and result.get("error") in (None, "success")


def prepare_mpv_audio_device(expected_device: str, command) -> str:
    """Select and verify the configured endpoint before loading a track.

    The long-lived MPV process can outlive a settings change. Passing the
    endpoint only when spawning MPV is therefore insufficient: every playback
    request must reconcile its live property with the saved endpoint before
    audio initialization, and fail closed if the property cannot be verified.
    """
    if not isinstance(expected_device, str) or not expected_device.strip() \
            or expected_device.strip().casefold() == "auto":
        raise RuntimeError("an explicit audio output device is required")

    current = command(["get_property", "audio-device"])
    if not mpv_command_succeeded(current):
        raise RuntimeError("could not inspect MPV's current audio device")
    if current.get("data") == expected_device:
        return expected_device

    pause = command(["set_property", "pause", True])
    if not mpv_command_succeeded(pause):
        raise RuntimeError("could not hold MPV paused while selecting the configured audio device")
    selected = command(["set_property", "audio-device", expected_device])
    if not mpv_command_succeeded(selected):
        raise RuntimeError("MPV rejected the configured audio device")
    verified = command(["get_property", "audio-device"])
    if not mpv_command_succeeded(verified) or verified.get("data") != expected_device:
        raise RuntimeError("MPV did not confirm the configured audio device")
    return expected_device


def prepare_mpv_output_channel_mode(fmt: str, command) -> str:
    """Set per-format WASAPI and channel policy before loadfile."""
    is_ac4_ims = fmt == "AC4_IMS"
    exclusive_mode = "no"
    result = command(["set_property", "options/audio-exclusive", exclusive_mode])
    if not mpv_command_succeeded(result):
        raise RuntimeError(
            "MPV rejected output profile property options/audio-exclusive: "
            f"{result.get('error')}"
        )

    channel_mode = "stereo" if is_ac4_ims else "auto"
    result = command(["set_property", "audio-channels", channel_mode])
    if not mpv_command_succeeded(result):
        raise RuntimeError(f"MPV rejected output profile property audio-channels: {result.get('error')}")
    return channel_mode


def prepare_mpv_demuxer_format(fmt: str, command, transport: str | None = None) -> str:
    """Select DASH explicitly for TIDAL MPDs before loading them in libmpv.

    A local HTTP ``.mpd`` URL is otherwise claimed by mpv's ytdl hook and/or
    fails demuxer auto-detection before the DASH proxy is contacted.
    """
    if transport is None:
        # Preserve the resolver's historical format defaults for direct callers
        # and tests. Playback passes the resolved transport explicitly below.
        is_dash = fmt in {"EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"}
    else:
        normalized_transport = str(transport).strip().upper().replace("-", "_")
        if normalized_transport in {"DASH", "MPEG_DASH", "MPD"}:
            is_dash = True
        elif normalized_transport in {"DIRECT", "HTTP", "MP4", "FILE"}:
            is_dash = False
        elif normalized_transport == "BTS" and fmt == "AC4_IMS":
            is_dash = False
        else:
            raise RuntimeError(f"Unsupported stream transport for {fmt}: {transport!r}")
    demuxer_format = "dash" if is_dash else ""
    result = command(["set_property", "demuxer-lavf-format", demuxer_format])
    if not mpv_command_succeeded(result):
        raise RuntimeError(
            "MPV rejected demuxer-lavf-format for "
            f"{fmt}: {result.get('error')}"
        )
    return demuxer_format


def configure_mpv_output_profile(
    engine, fmt: str, input_params: dict, timeout: float = 3.0, command=None,
    channel_mode_preconfigured: bool = False, expected_device: str | None = None,
) -> dict:
    """Request a source-aware MPV profile and separately report the device format.

    ``audio-params`` describes decoder PCM. ``audio-out-params`` describes the
    format delivered to the selected audio API and can legitimately differ when
    the endpoint or its mixer converts rate or channel layout.
    """
    try:
        sample_rate = int(input_params.get("samplerate", 0))
        input_channels = int(input_params.get("channel-count", 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("MPV did not report valid decoded audio parameters") from exc

    if not 8000 <= sample_rate <= 192000 or input_channels < 1:
        raise RuntimeError("MPV reported an unsupported decoded audio profile")

    if fmt == "AC4_IMS" and input_channels != 2:
        raise RuntimeError("AC-4 IMS must arrive from OxideAV as binaural stereo")

    is_ac4_ims = fmt == "AC4_IMS"
    channel_mode = "stereo" if is_ac4_ims else "auto"
    exclusive_mode = "no"
    send_command = command or engine.cmd
    if not channel_mode_preconfigured:
        result = send_command(["set_property", "options/audio-exclusive", exclusive_mode])
        if not mpv_command_succeeded(result):
            raise RuntimeError(
                "MPV rejected output profile property options/audio-exclusive: "
                f"{result.get('error')}"
            )
        result = send_command(["set_property", "audio-channels", channel_mode])
        if not mpv_command_succeeded(result):
            raise RuntimeError("MPV rejected output profile property audio-channels")
    result = send_command(["set_property", "audio-samplerate", sample_rate])
    if not mpv_command_succeeded(result):
        raise RuntimeError("MPV rejected output profile property audio-samplerate")

    ao_result = send_command(["get_property", "current-ao"])
    active_ao = ao_result.get("data") if mpv_command_succeeded(ao_result) else None
    if active_ao != "wasapi":
        raise RuntimeError(
            f"Required WASAPI audio output is not active (current AO: {active_ao or 'unknown'})"
        )

    if expected_device is not None:
        if not expected_device or expected_device == "auto":
            raise RuntimeError("An explicit selected audio device is required; automatic device selection is blocked")
        device_result = send_command(["get_property", "audio-device"])
        active_device = device_result.get("data") if mpv_command_succeeded(device_result) else None
        if active_device != expected_device:
            raise RuntimeError(
                "Selected audio endpoint mismatch; refusing to play through another device"
            )

    deadline = time.monotonic() + timeout
    mono_output_seen = False
    while time.monotonic() < deadline:
        result = send_command(["get_property", "audio-out-params"])
        output = result.get("data") if result and result.get("error") in (None, "success") else None
        if isinstance(output, dict):
            try:
                output_rate = int(output.get("samplerate", 0))
                output_channels = int(output.get("channel-count", 0))
            except (TypeError, ValueError, OverflowError):
                output_rate = output_channels = 0
            if 8000 <= output_rate <= 384000 and output_channels >= 1:
                if fmt == "AC4_IMS" and output_channels < 2:
                    mono_output_seen = True
                    time.sleep(0.05)
                    continue
                # Validate the decoded AC-4 IMS stream as stereo above. The
                # selected WASAPI endpoint may negotiate a different shared
                # format (for example 96 kHz / 7.1), but must retain at least
                # two channels so binaural separation is not collapsed.
                return {
                    "inputSampleRate": sample_rate,
                    "inputChannels": input_channels,
                    "outputSampleRate": output_rate,
                    "outputChannels": output_channels,
                    "channelMode": channel_mode,
                    "exclusiveMode": exclusive_mode,
                    "outputConverted": output_rate != sample_rate or output_channels != input_channels,
                }
        time.sleep(0.05)

    if fmt == "AC4_IMS":
        if mono_output_seen:
            raise RuntimeError(
                "WASAPI negotiated mono output; AC-4 IMS requires at least "
                "two output channels to preserve binaural separation"
            )
        raise RuntimeError(
            "MPV did not report a valid WASAPI output format for AC-4 IMS "
            f"(decoded input {sample_rate} Hz, {input_channels} channels)"
        )
    raise RuntimeError(
        "MPV did not report a valid audio API output format after applying the "
        f"decoded-input profile ({sample_rate} Hz, {input_channels} channels)"
    )


def iter_mp4_boxes(data: bytes, start: int = 0, end: int | None = None):
    """Yield ISO-BMFF boxes as (type, payload_start, box_end), with bounds checks."""
    limit = len(data) if end is None else min(end, len(data))
    offset = max(0, start)
    while offset + 8 <= limit:
        size = struct.unpack_from(">I", data, offset)[0]
        box_type = data[offset + 4:offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > limit:
                return
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header_size = 16
        elif size == 0:
            size = limit - offset
        if size < header_size or offset + size > limit:
            return
        box_end = offset + size
        yield box_type, offset + header_size, box_end
        offset = box_end


def mp4_child_box(data: bytes, parent: tuple[int, int] | None, wanted: bytes):
    if parent is None:
        return None
    return next(
        ((payload_start, box_end)
         for box_type, payload_start, box_end in iter_mp4_boxes(data, *parent)
         if box_type == wanted),
        None,
    )


def parse_mp4_audio_metrics(data: bytes) -> dict:
    """Read audio packet bitrate from MP4 sample tables without external tools.

    This is the average encoded packet payload rate, excluding MP4 container
    overhead. Fragmented or malformed files are reported as unavailable.
    """
    for box_type, moov_start, moov_end in iter_mp4_boxes(data):
        if box_type != b"moov":
            continue
        for track_type, trak_start, trak_end in iter_mp4_boxes(data, moov_start, moov_end):
            if track_type != b"trak":
                continue
            mdia = mp4_child_box(data, (trak_start, trak_end), b"mdia")
            hdlr = mp4_child_box(data, mdia, b"hdlr")
            mdhd = mp4_child_box(data, mdia, b"mdhd")
            if not hdlr or not mdhd or data[hdlr[0] + 8:hdlr[0] + 12] != b"soun":
                continue
            version = data[mdhd[0]]
            timescale_offset = mdhd[0] + (20 if version == 1 else 12)
            if timescale_offset + 4 > mdhd[1]:
                continue
            timescale = struct.unpack_from(">I", data, timescale_offset)[0]
            minf = mp4_child_box(data, mdia, b"minf")
            stbl = mp4_child_box(data, minf, b"stbl")
            stsz = mp4_child_box(data, stbl, b"stsz")
            stts = mp4_child_box(data, stbl, b"stts")
            if not stsz or not stts or timescale <= 0 or stsz[0] + 12 > stsz[1] or stts[0] + 8 > stts[1]:
                continue

            constant_size, sample_count = struct.unpack_from(">II", data, stsz[0] + 4)
            if sample_count <= 0:
                continue
            if constant_size:
                payload_bytes = constant_size * sample_count
            else:
                sizes_start = stsz[0] + 12
                if sizes_start + sample_count * 4 > stsz[1]:
                    continue
                payload_bytes = sum(
                    struct.unpack_from(">I", data, sizes_start + i * 4)[0]
                    for i in range(sample_count)
                )

            entry_count = struct.unpack_from(">I", data, stts[0] + 4)[0]
            entries_start = stts[0] + 8
            if entry_count <= 0 or entries_start + entry_count * 8 > stts[1]:
                continue
            duration_ticks = sum(
                count_in_run * sample_delta
                for count_in_run, sample_delta in (
                    struct.unpack_from(">II", data, entries_start + i * 8)
                    for i in range(entry_count)
                )
            )
            duration = duration_ticks / timescale
            if duration <= 0:
                continue

            result = {
                "sourceBitrate": round(payload_bytes * 8 / duration),
                "sourceSampleRate": None,
                "sourceChannels": None,
                "sourceCodec": None,
                "sourceBitrateBasis": "MP4 encoded audio packet bytes / packet duration",
            }
            stsd = mp4_child_box(data, stbl, b"stsd")
            if stsd and stsd[0] + 16 <= stsd[1]:
                description_count = struct.unpack_from(">I", data, stsd[0] + 4)[0]
                entry_start = stsd[0] + 8
                for _ in range(description_count):
                    if entry_start + 8 > stsd[1]:
                        break
                    entry_size = struct.unpack_from(">I", data, entry_start)[0]
                    if entry_size < 8 or entry_start + entry_size > stsd[1]:
                        break
                    entry_type = data[entry_start + 4:entry_start + 8]
                    result["sourceCodec"] = entry_type.decode("ascii", "replace")
                    sample_entry = entry_start + 8
                    if entry_type in (b"ac-4", b"mp4a", b"ec-3") and sample_entry + 28 <= entry_start + entry_size:
                        channels = struct.unpack_from(">H", data, sample_entry + 16)[0]
                        rate_fixed = struct.unpack_from(">I", data, sample_entry + 24)[0]
                        result["sourceChannels"] = channels or None
                        result["sourceSampleRate"] = (rate_fixed >> 16) or None
                    break
            return result
    return {
        "sourceBitrate": None,
        "sourceSampleRate": None,
        "sourceChannels": None,
        "sourceCodec": None,
        "sourceBitrateBasis": "unavailable (no complete MP4 audio sample table)",
    }


def mpv_path_matches(active_path: str, expected_path: str) -> bool:
    if not active_path or not expected_path:
        return False
    if expected_path.lower().startswith(("http://", "https://")):
        return active_path.casefold() == expected_path.casefold()
    return os.path.normcase(os.path.abspath(active_path)) == os.path.normcase(os.path.abspath(expected_path))


def is_omnifusion_mpv_process(info: dict, mpv_exe: Path = MPV_EXE, pipe_path: str = PIPE_PATH) -> bool:
    """Match only this packaged mpv process and its private IPC endpoint."""
    try:
        actual_exe = os.path.normcase(os.path.realpath(str(info.get("exe") or "")))
        expected_exe = os.path.normcase(os.path.realpath(str(mpv_exe)))
        if actual_exe != expected_exe:
            return False
        expected_pipe = pipe_path.casefold().replace("/", "\\")
        return any(
            isinstance(argument, str)
            and argument.casefold().startswith("--input-ipc-server=")
            and argument.split("=", 1)[1].strip('"').replace("/", "\\") == expected_pipe
            for argument in (info.get("cmdline") or [])
        )
    except (OSError, TypeError, ValueError):
        return False


def studio_config_has_binaural_output_mode(config_text: str) -> bool:
    """Read the active render.binaural.output_mode setting conservatively."""
    if not isinstance(config_text, str) or not config_text:
        return False
    in_render = False
    in_binaural = False
    matching_values = []
    for line in config_text.splitlines():
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_render = content == "render:"
            in_binaural = False
            continue
        if not in_render:
            continue
        if indent == 2:
            in_binaural = content == "binaural:"
            continue
        if in_binaural and indent == 4 and content.startswith("output_mode:"):
            value = content.split(":", 1)[1].split("#", 1)[0].strip().strip("\"'")
            matching_values.append(value.casefold())
    return matching_values == ["binaural"]


_STUDIO_CONFIG_MODE_LOCK = threading.Lock()
_STUDIO_CONFIG_MODE_CACHE: tuple[str, int, int, bool] | None = None


def studio_binaural_output_is_configured() -> bool:
    """Fail closed unless Studio's active config selects binaural headphones."""
    global _STUDIO_CONFIG_MODE_CACHE
    try:
        stat = STUDIO_CONFIG_FILE.stat()
        identity = (str(STUDIO_CONFIG_FILE), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return False
    with _STUDIO_CONFIG_MODE_LOCK:
        cached = _STUDIO_CONFIG_MODE_CACHE
        if cached and cached[:3] == identity:
            return cached[3]
        try:
            config_text = STUDIO_CONFIG_FILE.read_text(encoding="utf-8")
        except OSError:
            return False
        valid = studio_config_has_binaural_output_mode(config_text)
        _STUDIO_CONFIG_MODE_CACHE = (*identity, valid)
        return valid


def omniphony_route_is_ready(
    studio_count: int,
    mpv_count: int,
    orender_module_loaded: bool,
    studio_renderer_connected: bool,
    binaural_output_configured: bool,
) -> bool:
    """Require the embedded orender decoder and a live Studio↔MPV route.

    In this package, libmpv loads orender.dll in-process and Studio connects to
    that MPV host over loopback TCP. A separate orender.exe/named pipe is not
    part of this active route and must not gate JOC playback.
    """
    return (
        studio_count == 1
        and mpv_count == 1
        and orender_module_loaded
        and studio_renderer_connected
        and binaural_output_configured
    )


def is_managed_omniphony_process(info: dict, executable_name: str) -> bool:
    """Recognize package-local Studio/renderer builds, including staged updates.

    Studio can run from a versioned or extracted update directory under the
    portable root. Exact comparison with the stable launcher path caused the
    service to miss that live Studio and start a duplicate.
    """
    if not isinstance(info, dict):
        return False
    name = str(info.get("name") or "").casefold()
    if name != Path(executable_name).name.casefold():
        return False
    executable = str(info.get("exe") or "")
    if not executable:
        return False
    try:
        root = os.path.normcase(os.path.realpath(str(STANDALONE_DIR)))
        candidate = os.path.normcase(os.path.realpath(executable))
        if os.path.commonpath((root, candidate)) != root:
            return False
        relative_parts = Path(os.path.relpath(candidate, root)).parts
        return not any(part.casefold() == "backups" for part in relative_parts)
    except (OSError, ValueError):
        return False


def loopback_tcp_pair_is_established(connections, client_pid: int, peer_pid: int) -> bool:
    """Confirm an established reciprocal loopback OSC stream to the expected peer."""
    clients = [
        conn for conn in connections
        if conn.pid == client_pid and conn.status == psutil.CONN_ESTABLISHED
        and conn.laddr and conn.raddr
    ]
    engines = [
        conn for conn in connections
        if conn.pid == peer_pid and conn.status == psutil.CONN_ESTABLISHED
        and conn.laddr and conn.raddr
    ]
    return loopback_tcp_connection_lists_are_established(clients, engines)


def connection_endpoint_ip_port(endpoint):
    """Normalize psutil address records and process-scoped (ip, port) tuples."""
    if not endpoint:
        return None
    ip = getattr(endpoint, "ip", None)
    port = getattr(endpoint, "port", None)
    if ip is None or port is None:
        try:
            ip, port = endpoint[0], endpoint[1]
        except (IndexError, TypeError):
            return None
    return str(ip), int(port)


def loopback_tcp_connection_lists_are_established(clients, engines) -> bool:
    """Check reciprocal loopback connections from process-scoped psutil lists."""
    for client in clients:
        if client.status != psutil.CONN_ESTABLISHED:
            continue
        client_local = connection_endpoint_ip_port(client.laddr)
        client_remote = connection_endpoint_ip_port(client.raddr)
        if not client_local or not client_remote:
            continue
        if client_local[0] not in ("127.0.0.1", "::1") or client_remote[0] not in ("127.0.0.1", "::1"):
            continue
        for engine in engines:
            if engine.status != psutil.CONN_ESTABLISHED:
                continue
            engine_local = connection_endpoint_ip_port(engine.laddr)
            engine_remote = connection_endpoint_ip_port(engine.raddr)
            if engine_local and engine_remote and (
                engine_local == client_remote and engine_remote == client_local
            ):
                return True
    return False


def _windows_process_snapshot() -> list[tuple[int, str]]:
    """Return process IDs and names using Toolhelp, without opening every process.

    psutil's full process_iter metadata query asks Windows for executable paths
    and command lines for every process. That turns a frequent playback status
    check into a system-wide scan. Route checks only need a fast PID/name list;
    detailed metadata is fetched later for the three matching processes.
    """
    if os.name != "nt":
        return [
            (int(process.info["pid"]), str(process.info.get("name") or ""))
            for process in psutil.process_iter(["pid", "name"])
            if process.info.get("pid") is not None
        ]

    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    first = kernel32.Process32FirstW
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    first.restype = wintypes.BOOL
    next_process = kernel32.Process32NextW
    next_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    next_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")

    entries = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not first(snapshot, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "Process32FirstW failed")
        while True:
            entries.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            if not next_process(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != 18:  # ERROR_NO_MORE_FILES is the normal end condition.
                    raise OSError(error, "Process32NextW failed")
                break
    finally:
        close_handle(snapshot)
    return entries


def _route_process_candidates() -> list[dict]:
    """Fetch detailed process metadata only for Studio, renderer, and mpv."""
    target_names = {
        STUDIO_EXE.name.casefold(),
        RENDERER_EXE.name.casefold(),
        MPV_EXE.name.casefold(),
    }
    candidates = []
    for pid, name in _windows_process_snapshot():
        if name.casefold() not in target_names:
            continue
        try:
            process = psutil.Process(pid)
            info = {
                "pid": pid,
                "name": name,
                "exe": process.exe(),
                "createTime": process.create_time(),
            }
            if name.casefold() != STUDIO_EXE.name.casefold():
                info["cmdline"] = process.cmdline()
            candidates.append(info)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            # The standalone renderer is optional: JOC uses libmpv's in-process
            # orender.dll and Studio's direct MPV connection. It may exit after
            # Toolhelp snapshots its PID, so its missing metadata cannot erase
            # otherwise valid Studio/MPV candidates or invalidate that route.
            # Studio and MPV identity remain fail-closed because either can
            # change which renderer actually owns playback.
            if name.casefold() == RENDERER_EXE.name.casefold():
                continue
            raise
    return candidates


def get_omniphony_route_status(mpv_pid: int | None = None) -> dict:
    """Inspect the live Studio, orender bridge, and this package's MPV process."""
    studios = []
    renderers = []
    mpvs = []
    try:
        candidates = _route_process_candidates()
    except (OSError, psutil.Error) as exc:
        print(f"[-] Omniphony route process snapshot unavailable: {type(exc).__name__}")
        candidates = []
    for info in candidates:
        exe = os.path.normcase(os.path.realpath(str(info.get("exe") or "")))
        if is_managed_omniphony_process(info, STUDIO_EXE.name):
            studios.append(info)
        elif is_managed_omniphony_process(info, RENDERER_EXE.name):
            renderers.append(info)
        elif exe == os.path.normcase(os.path.realpath(str(MPV_EXE))) and is_omnifusion_mpv_process(info):
            if mpv_pid is None or info.get("pid") == mpv_pid:
                mpvs.append(info)

    renderer_cmdline = [
        str(arg).casefold().replace("/", "\\")
        for item in renderers for arg in (item.get("cmdline") or [])
    ]
    renderer_pipe_configured = (
        len(renderers) == 1
        and "render" in renderer_cmdline
        and ORENDER_PIPE.casefold() in renderer_cmdline
    )

    orender_module_loaded = False
    if len(mpvs) == 1:
        try:
            expected_module = os.path.normcase(os.path.realpath(str(ORENDER_DLL)))
            loaded_modules = psutil.Process(mpvs[0]["pid"]).memory_maps()
            orender_module_loaded = any(
                os.path.normcase(os.path.realpath(module.path)) == expected_module
                for module in loaded_modules if module.path
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            orender_module_loaded = False

    studio_renderer_connected = False
    if len(studios) == 1 and len(mpvs) == 1:
        try:
            studio_pid = studios[0]["pid"]
            mpv_pid_live = mpvs[0]["pid"]
            studio_connections = psutil.Process(studio_pid).net_connections(kind="tcp")
            mpv_connections = psutil.Process(mpv_pid_live).net_connections(kind="tcp")
            studio_renderer_connected = loopback_tcp_connection_lists_are_established(
                studio_connections, mpv_connections
            )
        except (psutil.Error, OSError):
            studio_renderer_connected = False

    process_identity_valid = False
    if len(studios) == 1 and len(mpvs) == 1:
        try:
            process_identity_valid = all(
                abs(
                    psutil.Process(info["pid"]).create_time()
                    - float(info["createTime"])
                ) <= 0.001
                for info in (studios[0], mpvs[0])
            )
        except (KeyError, TypeError, ValueError, psutil.Error, OSError):
            process_identity_valid = False

    checks = {
        "studio": len(studios) == 1,
        "renderer": True,
        "mpv": len(mpvs) == 1,
        "rendererPipeConfigured": True,
        "orenderModuleLoaded": orender_module_loaded,
        "studioRendererConnected": len(studios) == 1 and orender_module_loaded,
        "processIdentity": process_identity_valid,
        "headphoneBinauralConfigured": studio_binaural_output_is_configured(),
    }
    ready = (len(studios) == 1 and len(mpvs) == 1 and orender_module_loaded and checks["headphoneBinauralConfigured"] and process_identity_valid)
    return {
        "ready": ready,
        "checks": checks,
        "studioPid": studios[0]["pid"] if len(studios) == 1 else None,
        "rendererPid": renderers[0]["pid"] if len(renderers) == 1 else None,
        "mpvPid": mpvs[0]["pid"] if len(mpvs) == 1 else None,
    }


def is_selected_audio_decoder(track_list, decoder: str) -> bool:
    """Require an active audio track decoded by the requested renderer decoder."""
    if not isinstance(track_list, list):
        return False
    return any(
        isinstance(track, dict)
        and track.get("type") == "audio"
        and track.get("selected") is True
        and str(track.get("decoder") or "").casefold() == decoder.casefold()
        for track in track_list
    )


def is_active_orender_decoder(decoder) -> bool:
    """Return true only when MPV reports Omniphony's decoder as active."""
    return isinstance(decoder, str) and decoder.casefold() == "orender"


def joc_route_is_verified(route_status: dict | None, active_decoder=None) -> bool:
    """Fail closed unless Studio's route is ready and JOC is selected on orender."""
    if not isinstance(route_status, dict) or route_status.get("ready") is not True:
        return False
    checks = route_status.get("checks")
    if not isinstance(checks, dict) or checks.get("headphoneBinauralConfigured") is not True:
        return False
    return is_active_orender_decoder(active_decoder)


def wait_for_verified_joc_route(engine, timeout: float = 12.0) -> dict:
    """Wait for strict orender selection, then require Studio to attach to this MPV."""
    if not engine.wait_for_selected_decoder("orender"):
        return {"ready": False, "stage": "decoder", "checks": {"activeDecoder": False}}
    route_status = engine.ensure_omniphony_route(timeout=timeout)
    if not isinstance(route_status, dict):
        route_status = {"ready": False, "checks": {}}
    checks = route_status.setdefault("checks", {})
    if not isinstance(checks, dict):
        checks = {}
        route_status["checks"] = checks
    result = engine.cmd(["get_property", "current-tracks/audio/decoder"])
    active = result.get("data") if isinstance(result, dict) and result.get("error") in (None, "success") else None
    checks["activeDecoder"] = is_active_orender_decoder(active)
    route_status["ready"] = joc_route_is_verified(route_status, active)
    if not route_status["ready"]:
        if not checks["activeDecoder"]:
            route_status["stage"] = "decoder"
        else:
            route_status.setdefault("stage", "studio-route")
    return route_status


def peek_named_pipe_bytes(pipe) -> int:
    handle = msvcrt.get_osfhandle(pipe.fileno())
    available = ctypes.c_ulong(0)
    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    peek.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    peek.restype = ctypes.c_int
    if not peek(handle, None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        raise OSError(error, "PeekNamedPipe failed")
    return int(available.value)


def read_named_pipe_line(pipe, buffer: bytearray, timeout: float) -> bytes:
    """Read one newline-delimited mpv IPC message without blocking past timeout."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        # MPV emits newline-delimited JSON with LF; accept CRLF too because
        # both are valid from Windows named pipes. Waiting specifically for
        # CRLF makes every command time out when the peer sends LF only.
        newline = buffer.find(b"\n")
        if newline >= 0:
            line = bytes(buffer[:newline + 1])
            del buffer[:newline + 1]
            return line

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("mpv IPC response timed out")
        available = peek_named_pipe_bytes(pipe)
        if available:
            chunk = pipe.read(min(available, 65536))
            if not chunk:
                raise ConnectionError("mpv IPC pipe closed")
            buffer.extend(chunk)
            continue
        time.sleep(min(0.01, remaining))

class HeadlessAudioEngine:
    def __init__(self):
        self.lock = threading.Lock()
        # Startup and first HTTP requests can call ensure_mpv concurrently.
        # Serialize engine creation independently from the IPC command lock.
        self.ensure_lock = threading.Lock()
        self.playback_lock = threading.Lock()
        self.playback_generation_lock = threading.RLock()
        self.seek_sequence = 0
        self.last_seek_diagnostic = None
        self.pending_generation = 0
        self.active_generation = 0
        self.prepared_generation = 0
        self.pending_request_id = None
        self.prepared_request_id = None
        self.active_request_id = None
        self.cancelled_play_request_ids = {}
        self.file_loaded_serial = 0
        self.mpv_load_event_serial = 0
        self.recent_mpv_load_events = []
        self.status_lock = threading.Lock()
        self.playback_started_at = time.monotonic()
        self.last_status_position = None
        self.last_progress_at = self.playback_started_at
        self.has_playback_progress = False
        self.proc: subprocess.Popen | None = None
        self.mpv_instance_id: str | None = None
        self.pipe = None
        self.pipe_buffer = bytearray()
        self.seq = 0
        self.stream_proxy: TidalDashProxy | None = None
        self.current_meta = {
            "trackId": None,
            "format": None,
            "codec": None,
            "label": "Idle",
            "title": "",
            "artist": "",
        }

    def _prune_cancelled_play_requests_locked(self, now: float) -> None:
        expired = [
            request_id for request_id, cancelled_at in self.cancelled_play_request_ids.items()
            if now - cancelled_at >= CANCELLED_PLAY_REQUEST_TTL
        ]
        for request_id in expired:
            self.cancelled_play_request_ids.pop(request_id, None)
        while len(self.cancelled_play_request_ids) > MAX_CANCELLED_PLAY_REQUESTS:
            oldest = min(self.cancelled_play_request_ids, key=self.cancelled_play_request_ids.get)
            self.cancelled_play_request_ids.pop(oldest, None)

    def is_play_request_cancelled(self, request_id: str) -> bool:
        with self.playback_generation_lock:
            self._prune_cancelled_play_requests_locked(time.monotonic())
            return request_id in self.cancelled_play_request_ids

    def begin_playback(self, request_id: str | None = None) -> int:
        with self.playback_generation_lock:
            now = time.monotonic()
            self._prune_cancelled_play_requests_locked(now)
            if request_id is not None and request_id in self.cancelled_play_request_ids:
                raise ValueError("play request was already cancelled")
            self.pending_generation += 1
            self.pending_request_id = request_id
            self.prepared_generation = 0
            self.prepared_request_id = None
            return self.pending_generation

    def is_current_playback(self, generation: int, request_id: str | None = None) -> bool:
        with self.playback_generation_lock:
            if generation != self.pending_generation:
                return False
            if request_id is None:
                return self.pending_request_id is None
            self._prune_cancelled_play_requests_locked(time.monotonic())
            return (
                self.pending_request_id == request_id
                and request_id not in self.cancelled_play_request_ids
            )

    def cancel_play_request(self, request_id: str) -> str:
        request_id = validate_play_request_id(request_id)
        with self.playback_generation_lock:
            now = time.monotonic()
            self._prune_cancelled_play_requests_locked(now)

            # If commit already won the lock, cancel it by pausing the exact
            # active request. Keep its active identity so a later TIDAL resume
            # can resume this same track safely.
            if self.active_request_id == request_id:
                result = self.cmd(["set_property", "pause", True])
                if not mpv_command_succeeded(result):
                    raise RuntimeError("could not pause the matching active request")
                return "active-paused"

            self.cancelled_play_request_ids[request_id] = now
            self._prune_cancelled_play_requests_locked(now)
            was_pending = self.pending_request_id == request_id
            was_prepared = self.prepared_request_id == request_id
            if was_pending or was_prepared:
                self.pending_generation += 1
                self.pending_request_id = None
                self.prepared_generation = 0
                self.prepared_request_id = None
            if was_prepared:
                self.active_generation = 0
                self.current_meta = {
                    "trackId": None,
                    "format": None,
                    "codec": None,
                    "label": "Idle",
                    "title": "",
                    "artist": "",
                }
                result = self.cmd(["set_property", "pause", True])
                if not mpv_command_succeeded(result):
                    raise RuntimeError("could not keep the cancelled prepared request paused")
            return "cancelled" if (was_pending or was_prepared) else "recorded"

    def control_matches(
        self, track_id=None, generation=None, request_id=None, *, allow_prepared=False
    ) -> bool:
        with self.playback_generation_lock:
            current = self.current_meta
            if track_id is None or generation is None:
                return False
            if str(track_id) != str(current.get("trackId")):
                return False
            current_request_id = current.get("requestId")
            if current_request_id is not None and request_id != current_request_id:
                return False
            if current_request_id is None and request_id is not None:
                return False
            try:
                generation = int(generation)
            except (TypeError, ValueError, OverflowError):
                return False
            if generation == self.active_generation:
                return (
                    generation > 0
                    and generation == current.get("generation")
                    and current.get("trackId") is not None
                )
            if not allow_prepared:
                return False
            return (
                generation > 0
                and self.prepared_request_id is not None
                and generation == self.prepared_generation
                and generation == self.pending_generation
                and generation == current.get("generation")
                and request_id == self.prepared_request_id
                and self.prepared_request_id not in self.cancelled_play_request_ids
                and current.get("playbackState") == "prepared"
            )

    def begin_seek(self, track_id, generation, request_id=None):
        """Allocate a monotonically increasing identity for an accepted seek."""
        with self.playback_generation_lock:
            if not self.control_matches(
                track_id, generation, request_id, allow_prepared=True
            ):
                return None
            self.seek_sequence += 1
            return self.seek_sequence

    def seek_is_current(self, sequence, track_id, generation, request_id=None) -> bool:
        with self.playback_generation_lock:
            return (
                sequence == self.seek_sequence
                and self.control_matches(
                    track_id, generation, request_id, allow_prepared=True
                )
            )

    def wait_for_seek_position(
        self, target, sequence, track_id, generation, request_id=None,
        timeout=SEEK_SETTLE_TIMEOUT_SECONDS, expected_paused=None,
        diagnostic_samples=None,
    ):
        """Confirm a seek with consecutive MPV clock samples without holding playback locks."""
        started_at = time.monotonic()
        deadline = started_at + timeout
        consecutive = 0
        stable_since = None
        observed = None
        active_clock_anchor = target if expected_paused is False else None
        active_clock_anchor_at = started_at if expected_paused is False else None
        awaiting_post_seek_anchor = False
        in_tolerance = False
        expected_position = None

        def record_sample(pause_value=None, seeking_value=None, failure=None):
            if diagnostic_samples is None:
                return
            sample = {
                "elapsedMs": round((time.monotonic() - started_at) * 1000, 1),
                "timePos": observed,
                "expectedTimePos": expected_position,
                "seeking": seeking_value if isinstance(seeking_value, bool) else None,
                "paused": pause_value if isinstance(pause_value, bool) else None,
                "inTolerance": in_tolerance,
                "consecutive": consecutive,
                "stableMs": (
                    round((time.monotonic() - stable_since) * 1000, 1)
                    if stable_since is not None else 0
                ),
            }
            if failure is not None:
                sample["failure"] = failure
            diagnostic_samples.append(sample)
            if len(diagnostic_samples) > 64:
                del diagnostic_samples[:-64]

        while time.monotonic() < deadline:
            if not self.seek_is_current(sequence, track_id, generation, request_id):
                record_sample(failure="superseded")
                return False, observed, "superseded"
            pause_value = None
            seeking_value = None
            if expected_paused is not None:
                pause_result = self.cmd(["get_property", "pause"])
                if not mpv_command_succeeded(pause_result):
                    record_sample(failure="pause-read-error")
                    return False, observed, "ipc-error"
                pause_value = pause_result.get("data")
                if not isinstance(pause_value, bool):
                    record_sample(failure="pause-invalid")
                    return False, observed, "ipc-error"
                if pause_value is not expected_paused:
                    record_sample(pause_value=pause_value, failure="pause-changed")
                    return False, observed, "pause-changed"
            result = self.cmd(["get_property", "time-pos"])
            if not mpv_command_succeeded(result):
                record_sample(pause_value=pause_value, failure="time-pos-read-error")
                return False, observed, "ipc-error"
            raw_position = result.get("data") if result else None
            observed = (
                float(raw_position)
                if isinstance(raw_position, (int, float))
                and not isinstance(raw_position, bool)
                and math.isfinite(raw_position)
                else None
            )
            seeking_result = self.cmd(["get_property", "seeking"])
            if not mpv_command_succeeded(seeking_result):
                record_sample(pause_value=pause_value, failure="seeking-read-error")
                return False, observed, "ipc-error"
            seeking_value = seeking_result.get("data")
            if not isinstance(seeking_value, bool):
                record_sample(pause_value=pause_value, failure="seeking-invalid")
                return False, observed, "ipc-error"
            if not self.seek_is_current(sequence, track_id, generation, request_id):
                record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="superseded")
                return False, observed, "superseded"
            if expected_paused is not None:
                pause_result = self.cmd(["get_property", "pause"])
                if not mpv_command_succeeded(pause_result):
                    record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="pause-read-error")
                    return False, observed, "ipc-error"
                pause_value = pause_result.get("data")
                if not isinstance(pause_value, bool):
                    record_sample(seeking_value=seeking_value, failure="pause-invalid")
                    return False, observed, "ipc-error"
                if pause_value is not expected_paused:
                    record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="pause-changed")
                    return False, observed, "pause-changed"
            settled = False
            now = time.monotonic()
            expected_position = target
            in_tolerance = False
            if seeking_value is False and observed is not None:
                if expected_paused is False:
                    if awaiting_post_seek_anchor and active_clock_anchor is None:
                        if abs(observed - target) <= SEEK_POSITION_TOLERANCE_SECONDS:
                            active_clock_anchor = observed
                            active_clock_anchor_at = now
                            awaiting_post_seek_anchor = False
                    # A playing clock advances from the dispatched seek target.
                    # Compare against that moving timeline so a delayed first IPC
                    # sample can still confirm a correct seek. A confirmed seeking
                    # interval is excluded by re-anchoring only after the position
                    # returns near the requested target.
                    if active_clock_anchor_at is not None:
                        expected_position = active_clock_anchor + (now - active_clock_anchor_at)
                        in_tolerance = (
                            abs(observed - expected_position)
                            <= SEEK_POSITION_TOLERANCE_SECONDS
                        )
                else:
                    in_tolerance = (
                        abs(observed - target) <= SEEK_POSITION_TOLERANCE_SECONDS
                    )
            if in_tolerance:
                consecutive += 1
                if consecutive >= SEEK_SETTLE_CONFIRMATION_SAMPLES:
                    if stable_since is None:
                        stable_since = now
                    elif now - stable_since >= SEEK_SETTLE_STABILITY_SECONDS:
                        settled = True
            else:
                consecutive = 0
                stable_since = None
            if seeking_value is True and expected_paused is False:
                active_clock_anchor = None
                active_clock_anchor_at = None
                awaiting_post_seek_anchor = True
            record_sample(pause_value=pause_value, seeking_value=seeking_value)
            if settled:
                return True, observed, None
            time.sleep(0.05)
        return False, observed, "timeout"

    def record_ipc_event(self, message: dict) -> None:
        event = message.get("event")
        if event == "file-loaded":
            self.file_loaded_serial += 1
        if event in {"start-file", "file-loaded", "end-file"}:
            # Keep only non-sensitive load diagnostics. MPV's error text can
            # include a media URL, so redact URLs and credential-like query
            # values before retaining it for the local diagnostics endpoint.
            raw_error = message.get("file_error") or message.get("error")
            error_code = (
                raw_error
                if isinstance(raw_error, int) and not isinstance(raw_error, bool)
                else None
            )
            error = raw_error
            if isinstance(error, str):
                error = re.sub(r"https?://\S+", "[url]", error, flags=re.IGNORECASE)
                error = re.sub(
                    r"(?i)(access|refresh|session|auth|token|sig(?:nature)?)\s*([=:])\s*[^&\s]+",
                    r"\1\2[redacted]",
                    error,
                )
                error = re.sub(r"[\r\n\t]+", " ", error).strip()[:240]
            else:
                error = None
            self.mpv_load_event_serial += 1
            self.recent_mpv_load_events.append({
                "serial": self.mpv_load_event_serial,
                "event": event,
                "reason": str(message.get("reason") or "")[:40] or None,
                "playlistEntryId": (
                    message.get("playlist_entry_id")
                    if isinstance(message.get("playlist_entry_id"), int)
                    and not isinstance(message.get("playlist_entry_id"), bool)
                    else None
                ),
                "errorCode": error_code,
                "error": error,
            })
            del self.recent_mpv_load_events[:-8]

    def get_file_loaded_serial(self) -> int:
        with self.lock:
            return self.file_loaded_serial

    def get_mpv_load_event_serial(self) -> int:
        with self.lock:
            return self.mpv_load_event_serial

    def get_loaded_target_diagnostics(
        self, target: str, file_loaded_after: int, load_event_after: int | None = None,
        include_path: bool = False, include_audio_params: bool = False,
    ) -> dict:
        """Return non-sensitive state explaining why a load was not accepted."""
        path_result = self.cmd(["get_property", "path"]) if include_path else None
        active = str(path_result.get("data") or "") if path_result else ""
        params_result = (
            self.cmd(["get_property", "audio-params"])
            if include_audio_params
            else None
        )
        raw_params = params_result.get("data") if params_result else None
        params = raw_params if isinstance(raw_params, dict) else {}
        def path_kind(value):
            value = str(value or "").strip()
            if not value:
                return "none"
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme in {"http", "https"}:
                return "loopback-http" if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else "remote-http"
            if parsed.scheme == "file" or not parsed.scheme:
                return "file"
            return parsed.scheme[:24]

        with self.lock:
            recent_events = [
                {key: value for key, value in event.items() if key != "serial"}
                for event in self.recent_mpv_load_events
                if load_event_after is None or event["serial"] > load_event_after
            ]
        return {
            "fileLoaded": self.get_file_loaded_serial() > file_loaded_after,
            "pathMatched": mpv_path_matches(active, target) if include_path else None,
            "activePathKind": path_kind(active) if include_path else "not-queried",
            "expectedTargetKind": path_kind(target),
            "audioParamsAvailable": bool(params.get("samplerate") and params.get("channel-count")),
            "sampleRate": params.get("samplerate") if isinstance(params.get("samplerate"), int) else 0,
            "channelCount": params.get("channel-count") if isinstance(params.get("channel-count"), int) else 0,
            "recentMpvLoadEvents": recent_events,
        }

    def wait_for_loaded_target(
        self, target: str, file_loaded_after: int, timeout: float = 20.0,
        load_event_after: int | None = None,
        playlist_entry_id: int | None = None,
    ) -> bool:
        if isinstance(playlist_entry_id, int) and not isinstance(playlist_entry_id, bool):
            # A path query can block inside libavformat while a DASH playlist is
            # still opening. The loadfile response and start-file event identify
            # the exact playlist entry, so wait for its lifecycle events on IPC
            # without issuing property requests against the loading demuxer.
            deadline = time.monotonic() + timeout
            start_serial = None

            def inspect_events() -> bool | None:
                nonlocal start_serial
                for event in self.recent_mpv_load_events:
                    if load_event_after is not None and event["serial"] <= load_event_after:
                        continue
                    if event["event"] == "start-file" and event.get("playlistEntryId") == playlist_entry_id:
                        start_serial = event["serial"]
                    elif (
                        event["event"] == "file-loaded"
                        and start_serial is not None
                        and event["serial"] > start_serial
                    ):
                        return True
                    elif (
                        event["event"] == "end-file"
                        and event.get("playlistEntryId") == playlist_entry_id
                    ):
                        return False
                return None

            while time.monotonic() < deadline:
                with self.lock:
                    state = inspect_events()
                    if state is not None:
                        return state
                    if not self.pipe:
                        return False
                    try:
                        line = read_named_pipe_line(
                            self.pipe,
                            self.pipe_buffer,
                            min(0.1, max(0.0, deadline - time.monotonic())),
                        )
                    except TimeoutError:
                        continue
                    except (OSError, ConnectionError):
                        return False
                    try:
                        message = json.loads(line.decode("utf-8", errors="replace"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(message, dict) and message.get("event"):
                        self.record_ipc_event(message)
            with self.lock:
                return inspect_events() is True

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            diagnostics = self.get_loaded_target_diagnostics(
                target, file_loaded_after, load_event_after, include_path=True
            )
            if diagnostics["fileLoaded"] and diagnostics["pathMatched"]:
                return True
            time.sleep(0.1)
        return False

    def wait_for_audio_params(self, timeout: float = 6.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.cmd(["get_property", "audio-params"])
            params = result.get("data") if result else None
            if isinstance(params, dict):
                try:
                    sample_rate = int(params.get("samplerate") or 0)
                    channel_count = int(params.get("channel-count") or 0)
                except (TypeError, ValueError, OverflowError):
                    sample_rate = channel_count = 0
                if sample_rate > 0 and channel_count > 0:
                    return params
            time.sleep(0.1)
        return None

    def initialize_audio_params_muted(self, timeout: float = 6.0) -> dict | None:
        'Initialize MPV decoder at zero volume, then restore its paused state.'
        position_result = self.cmd(["get_property", "time-pos"])
        try:
            original_position = float(position_result.get("data"))
        except (TypeError, ValueError, OverflowError, AttributeError):
            return None
        if not math.isfinite(original_position) or original_position < 0:
            return None

        volume_result = self.cmd(["get_property", "volume"])
        try:
            original_volume = float(volume_result.get("data"))
        except (TypeError, ValueError, OverflowError, AttributeError):
            return None
        if not math.isfinite(original_volume) or original_volume < 0:
            return None

        mute_result = self.cmd(["set_property", "volume", 0])
        if not mpv_command_succeeded(mute_result):
            return None

        params = None
        preflight_cleanup_ok = True
        try:
            resume_result = self.cmd(["set_property", "pause", False])
            if mpv_command_succeeded(resume_result):
                params = self.wait_for_audio_params(timeout)
        except Exception:
            params = None
        finally:
            # Do not restore audible volume unless MPV is confirmed paused.
            pause_result = self.cmd(["set_property", "pause", True])
            if mpv_command_succeeded(pause_result):
                seek_result = self.cmd(["seek", original_position, "absolute"])
                if (not mpv_command_succeeded(seek_result)
                        or not self.wait_for_position(original_position, timeout=1.0)):
                    print("[-] MPV audio preflight could not restore its original position; volume remains zero.")
                    preflight_cleanup_ok = False
                else:
                    restore_result = self.cmd(
                        ["set_property", "volume", original_volume]
                    )
                    if not mpv_command_succeeded(restore_result):
                        print("[-] MPV audio preflight could not restore its volume.")
                        preflight_cleanup_ok = False
            else:
                print("[-] MPV audio preflight could not re-pause; volume remains zero.")
                preflight_cleanup_ok = False
        return params if preflight_cleanup_ok else None

    def wait_for_selected_decoder(self, decoder: str, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.cmd(["get_property", "track-list"])
            if result and result.get("error") in (None, "success"):
                if is_selected_audio_decoder(result.get("data"), decoder):
                    return True
            time.sleep(0.1)
        return False

    def ensure_omniphony_route(self, timeout: float = 6.0) -> dict:
        """Start Studio if needed, then require its renderer and this MPV to be linked."""
        if not STUDIO_EXE.exists():
            return {"ready": False, "checks": {"studioExecutable": False}}
        initial = get_omniphony_route_status(self.proc.pid if self.proc else None)
        if not initial.get("checks", {}).get("studio"):
            try:
                subprocess.Popen([str(STUDIO_EXE)], cwd=str(STUDIO_EXE.parent))
            except OSError:
                return {"ready": False, "checks": {"studioLaunch": False}}

        deadline = time.monotonic() + timeout
        status = {"ready": False, "checks": {}}
        while time.monotonic() < deadline:
            status = get_omniphony_route_status(self.proc.pid if self.proc else None)
            if status.get("ready"):
                return status
            time.sleep(0.2)
        return status

    def wait_for_position(self, target: float, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.cmd(["get_property", "time-pos"])
            position = result.get("data") if result else None
            if isinstance(position, (int, float)) and math.isfinite(position) and abs(position - target) <= 0.25:
                return True
            time.sleep(0.05)
        return False

    def wait_for_rewind_from_eof(self, timeout: float = EOF_REWIND_TIMEOUT_SECONDS) -> bool:
        """Require a completed, stable seek to the start before resuming from EOF."""
        deadline = time.monotonic() + timeout
        stable_samples = 0
        while time.monotonic() < deadline:
            def command_with_remaining_time(name):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"error": "rewind settlement deadline expired"}
                return self.cmd(["get_property", name], timeout=remaining)

            eof_result = command_with_remaining_time("eof-reached")
            seeking_result = command_with_remaining_time("seeking")
            position_result = command_with_remaining_time("time-pos")
            eof_reached = eof_result.get("data") if mpv_command_succeeded(eof_result) else None
            seeking = seeking_result.get("data") if mpv_command_succeeded(seeking_result) else None
            position = position_result.get("data") if mpv_command_succeeded(position_result) else None
            if (
                eof_reached is False
                and seeking is False
                and isinstance(position, (int, float))
                and not isinstance(position, bool)
                and math.isfinite(position)
                and 0.0 <= position <= 0.25
            ):
                stable_samples += 1
                if stable_samples >= 3:
                    # The last IPC response can arrive after the final loop
                    # condition was checked. Do not report a settled rewind
                    # beyond the caller's absolute deadline.
                    return time.monotonic() <= deadline
            else:
                stable_samples = 0
            time.sleep(0.05)
        return False

    def update_playback_state(self, position, paused, audio_params, loaded) -> str:
        now = time.monotonic()
        with self.status_lock:
            if self.current_meta.get("trackId") is None:
                state = "idle"
            elif (
                self.current_meta.get("playbackState") == "prepared"
                and self.prepared_generation == self.current_meta.get("generation")
                and self.prepared_generation > 0
            ):
                state = "prepared"
            elif not loaded:
                state = "loading" if now - self.playback_started_at < 8 else "failed"
            elif paused:
                state = "paused"
            elif position is not None and self.last_status_position is not None \
                    and position > self.last_status_position + 0.001:
                state = "playing"
                self.last_progress_at = now
                self.has_playback_progress = True
            elif self.has_playback_progress and now - self.last_progress_at <= 0.75:
                # Another concurrent /status poll may observe the clock tick first.
                # Keep the live state stable across the shared polling interval.
                state = "playing"
            elif now - self.last_progress_at > 3:
                state = "stalled"
            else:
                state = "loading"
            self.last_status_position = position
            self.current_meta["playbackState"] = state
            return state

    def reap_orphaned_mpv(self):
        current_pid = self.proc.pid if self.proc and self.proc.poll() is None else None
        for p in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
            try:
                if (p.info['name'] and p.info['name'].lower() == 'mpv.exe'
                        and is_omnifusion_mpv_process(p.info)):
                    if current_pid and p.info['pid'] == current_pid:
                        continue
                    print(f"[*] Reaping stale OmniFusion mpv process PID {p.info['pid']}...")
                    p.terminate()
                    p.wait(timeout=2)
            except Exception:
                pass

    def connect_mpv_pipe(self, attempts: int = 1, delay: float = 0.1) -> bool:
        for _ in range(attempts):
            try:
                self.pipe = open(PIPE_PATH, "r+b", buffering=0)
                self.pipe_buffer.clear()
                return True
            except OSError:
                time.sleep(delay)
        self.pipe = None
        return False

    def invalidate_mpv_connection(self, terminate_owned_process: bool = False) -> None:
        if self.pipe:
            try:
                self.pipe.close()
            except OSError:
                pass
            self.pipe = None
        self.pipe_buffer.clear()
        if terminate_owned_process and self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.proc = None
            self.mpv_instance_id = None

    def ensure_mpv(self, device="auto"):
        with self.ensure_lock:
            return self._ensure_mpv_unlocked(device)

    def _ensure_mpv_unlocked(self, device="auto"):
        if self.proc and self.proc.poll() is None and self.pipe:
            return
        if self.proc and self.proc.poll() is None and self.connect_mpv_pipe(attempts=3, delay=0.05):
            print("[+] Reconnected to the existing OmniFusion mpv pipe.")
            return
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.proc = None
        self.mpv_instance_id = None
        if self.pipe:
            try:
                self.pipe.close()
            except OSError:
                pass
            self.pipe = None
        self.reap_orphaned_mpv()

        print("[*] Spawning headless libmpv engine...")
        args = [
            str(MPV_EXE),
            "--no-video",
            "--force-window=no",
            "--idle=yes",
            "--keep-open=yes",
            "--no-osc",
            "--no-osd-bar",
            f"--input-ipc-server={PIPE_PATH}",
            f"--ad={STRICT_RENDER_DECODER}",
            "--ad-orender-osc",
            "--audio-client-name=Omniphony-Standalone",
            "--audio-fallback-to-null=no",
            "--volume-max=150",
            # TIDAL's loopback DASH manifest is not a youtube-dl target.
            # Leave URL handling to libavformat and set the DASH demuxer per
            # file immediately before each stream load.
            "--ytdl=no",
        ]
        diagnostic_log = os.environ.get("OMNIFUSION_MPV_DIAGNOSTIC_LOG")
        if diagnostic_log:
            args.extend([
                f"--log-file={diagnostic_log}",
                "--msg-level=all=debug",
            ])
        if device and device != "auto":
            args.append(f"--audio-device={device}")

        stderr_target = subprocess.DEVNULL
        stderr_log_handle = None
        if diagnostic_log:
            diagnostic_path = Path(diagnostic_log)
            stderr_log_path = diagnostic_path.with_name(
                f"{diagnostic_path.stem}-stderr-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.log"
            )
            try:
                stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
                stderr_log_handle = open(stderr_log_path, "ab", buffering=0)
                stderr_target = stderr_log_handle
                print(f"[*] Capturing MPV diagnostic stderr to {stderr_log_path}")
            except OSError as exc:
                print(f"[-] Could not open MPV diagnostic stderr log: {exc}")

        try:
            self.proc = subprocess.Popen(
                args,
                cwd=str(MPV_DIR),
                stdout=subprocess.DEVNULL,
                stderr=stderr_target,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            self.mpv_instance_id = uuid.uuid4().hex
        finally:
            if stderr_log_handle is not None:
                stderr_log_handle.close()

        time.sleep(0.4)
        if self.connect_mpv_pipe(attempts=30, delay=0.1):
            print("[+] Connected to MPV IPC pipe.")

    def cmd(self, cmd_args, timeout=2.0) -> dict:
        with self.lock:
            self.ensure_mpv()
            if not self.pipe:
                return {"error": "pipe unavailable"}
            mpv_instance_id = self.mpv_instance_id
            try:
                self.seq += 1
                req_id = self.seq
                payload = json.dumps({"command": cmd_args, "request_id": req_id}) + "\r\n"
                self.pipe.write(payload.encode("utf-8"))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    line = read_named_pipe_line(
                        self.pipe, self.pipe_buffer, deadline - time.monotonic()
                    )
                    if not line:
                        break
                    res = json.loads(line.decode("utf-8", errors="ignore"))
                    if isinstance(res, dict) and res.get("event"):
                        self.record_ipc_event(res)
                    if res.get("request_id") == req_id:
                        res["_mpv_instance_id"] = mpv_instance_id
                        return res
                raise TimeoutError("mpv IPC response timed out")
            except TimeoutError as e:
                print(f"[-] MPV command timed out: {e}")
                # This is our recorded child process, never an arbitrary
                # system mpv. Restart it on the next command after a hard hang.
                self.invalidate_mpv_connection(terminate_owned_process=True)
                return {"error": "timeout"}
            except Exception as e:
                print(f"[-] MPV command error: {e}")
                try:
                    self.pipe.close()
                except Exception:
                    pass
                self.pipe = None
                self.pipe_buffer.clear()
                return {"error": str(e)}

    def clock_cmd(self, cmd_args, timeout=0.25) -> dict:
        """Read a clock property from the already-connected MPV only.

        Unlike cmd(), this path never ensures, reconnects, invalidates, or
        terminates an MPV process. A missed sample is disposable.
        """
        with self.lock:
            proc = self.proc
            mpv_instance_id = self.mpv_instance_id
            if (
                proc is None
                or proc.poll() is not None
                or self.pipe is None
                or not mpv_instance_id
            ):
                return {"error": "mpv unavailable", "_mpv_instance_id": mpv_instance_id}
            try:
                self.seq += 1
                req_id = self.seq
                payload = json.dumps({"command": cmd_args, "request_id": req_id}) + "\r\n"
                request_started_monotonic_ns = time.monotonic_ns()
                request_started_unix_ms = time.time_ns() / 1_000_000
                self.pipe.write(payload.encode("utf-8"))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    line = read_named_pipe_line(
                        self.pipe, self.pipe_buffer, deadline - time.monotonic()
                    )
                    if not line:
                        break
                    res = json.loads(line.decode("utf-8", errors="ignore"))
                    if isinstance(res, dict) and res.get("event"):
                        self.record_ipc_event(res)
                    if isinstance(res, dict) and res.get("request_id") == req_id:
                        res["_mpv_instance_id"] = mpv_instance_id
                        res["_request_duration_ns"] = max(
                            0, time.monotonic_ns() - request_started_monotonic_ns
                        )
                        res["_request_started_unix_ms"] = request_started_unix_ms
                        return res
                return {"error": "clock sample timeout", "_mpv_instance_id": mpv_instance_id}
            except Exception as exc:
                return {
                    "error": f"clock sample unavailable: {type(exc).__name__}",
                    "_mpv_instance_id": mpv_instance_id,
                }

engine = HeadlessAudioEngine()

def load_settings() -> dict:
    with SETTINGS_LOCK:
        if SETTINGS_FILE.is_file():
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    settings = {**DEFAULT_SETTINGS, **json.load(f)}
                    priority = settings.get("priority")
                    if (not isinstance(priority, list)
                            or len(priority) != len(SUPPORTED_FORMAT_PRIORITY)
                            or any(not isinstance(item, str) for item in priority)
                            or set(priority) != set(SUPPORTED_FORMAT_PRIORITY)):
                        settings["priority"] = list(DEFAULT_SETTINGS["priority"])
                    return settings
            except Exception as exc:
                print(f"[-] Settings read failed; using defaults: {exc}")
                backup_file = SETTINGS_FILE.with_name(
                    f"{SETTINGS_FILE.name}.corrupt-{time.time_ns()}.bak"
                )
                try:
                    shutil.copy2(SETTINGS_FILE, backup_file)
                    print(f"[+] Preserved unreadable settings at {backup_file}")
                except OSError as backup_error:
                    raise OSError(
                        "settings are unreadable and backup failed; original file was preserved"
                    ) from backup_error
        return DEFAULT_SETTINGS.copy()

def save_settings(new_settings: dict):
    with SETTINGS_LOCK:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8", newline="\r\n") as f:
                json.dump(new_settings, f, indent=2)
                f.write("\r\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_file, SETTINGS_FILE)
        finally:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass


def update_settings(changes: dict) -> dict:
    with SETTINGS_LOCK:
        settings = load_settings()
        settings.update(changes)
        save_settings(settings)
        return settings

def python_tidal_request_requires_stop(exc: Exception) -> bool:
    """Identify EbbLabs auth/backoff failures that must not trigger more calls."""
    try:
        from tidalapi.exceptions import (
            AbuseDetected,
            AuthenticationError,
            TidalBackoffError,
            TooManyRequests,
        )
    except ImportError:
        return False
    if isinstance(exc, (AbuseDetected, AuthenticationError, TidalBackoffError, TooManyRequests)):
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 401


def _resolve_eac3_joc_data_manifest(session, track_id: int, auth_profile: str) -> dict | None:
    """Return a verified stream, None for a verified negative, or raise if indeterminate.

    ``None`` is reserved for a nonempty, recognized ``formats`` list that omits
    EAC3_JOC, or for a structurally valid protected EAC3_JOC MPD. Missing,
    nullable, empty, unknown, or malformed response data raises instead.
    """
    try:
        response = session.request.request(
            "GET",
            f"trackManifests/{int(track_id)}",
            params={
                "adaptive": "false",
                "formats": "EAC3_JOC",
                "manifestType": "MPEG_DASH",
                "uriScheme": "DATA",
                "usage": "PLAYBACK",
            },
            base_url=session.config.openapi_v2_location,
        )
        if response.status_code != 200:
            # A failed request is indeterminate, not evidence that this profile
            # lacks JOC. In particular, do not trigger a second profile request
            # after transient, authorization, or server failures.
            raise RuntimeError(
                f"python-tidal JOC manifest request returned HTTP {response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("python-tidal JOC response is not a JSON object")
        data = payload.get("data")
        attributes = data.get("attributes") if isinstance(data, dict) else None
        if not isinstance(attributes, dict):
            raise ValueError("python-tidal JOC response has no manifest attributes")
        formats = attributes.get("formats")
        uri = attributes.get("uri")
        known_formats = {"HEAACV1", "AACLC", "FLAC", "FLAC_HIRES", "EAC3_JOC"}
        if (
            not isinstance(formats, list)
            or not formats
            or any(not isinstance(item, str) or item not in known_formats for item in formats)
        ):
            raise ValueError("python-tidal JOC response has an invalid formats list")
        # A successful response explicitly omitting the requested format is a
        # definitive negative for this profile, so the supported TV profile may
        # be tried. Missing/malformed data after a positive format claim is an
        # error and must fail closed below.
        if "EAC3_JOC" not in formats:
            return None
        if not isinstance(uri, str) or not uri.lower().startswith(
            "data:application/dash+xml;base64,"
        ):
            raise ValueError("python-tidal JOC response has no valid inline DASH URI")
        header, encoded = uri.split(",", 1)
        if header.lower() != "data:application/dash+xml;base64":
            raise ValueError("python-tidal JOC response has an unsupported DASH data URI")
        try:
            manifest_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("python-tidal JOC response has invalid base64") from exc
        if not manifest_bytes or len(manifest_bytes) > 8 * 1024 * 1024:
            raise ValueError("python-tidal JOC manifest is empty or exceeds the size limit")
        try:
            manifest_xml = manifest_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("python-tidal JOC manifest is not UTF-8") from exc
        # Protection is a fallback reason only if the MPD independently proves
        # it is the requested E-AC-3 JOC representation.
        if dash_manifest_contains_content_protection(manifest_xml):
            verify_eac3_joc_manifest(manifest_xml, allow_content_protection=True)
            return None

        dash = get_tidal_dash_audio_metadata(manifest_xml=manifest_xml)
        validated_xml = dash.get("manifestXml")
        segment_urls = dash.get("segmentUrls")
        if (
            not isinstance(validated_xml, str)
            or not validated_xml
            or not isinstance(segment_urls, list)
            or not python_tidal_dash_manifest_segments_are_complete(
                validated_xml, segment_urls
            )
            or dash_manifest_contains_content_protection(validated_xml)
        ):
            raise ValueError("python-tidal JOC DASH manifest is incomplete or inconsistent")

        evidence = verify_eac3_joc_manifest(validated_xml)
        normalized_codec = re.sub(
            r"[^a-z0-9]", "", str(dash.get("sourceCodec") or "").lower()
        )
        if (
            normalized_codec != evidence["normalizedCodec"]
            or dash.get("sourceManifestFormat") != evidence["sourceManifestFormat"]
            or dash.get("sourceBitrate") != evidence["sourceBitrate"]
        ):
            raise ValueError("python-tidal JOC DASH metadata does not match its representation")
        return {
            "format": "EAC3_JOC",
            "codec": "eac3_joc",
            "sourceAuthProfile": auth_profile,
            "label": "Dolby Atmos (E-AC-3 JOC)",
            "uri": uri,
            "transport": "DASH",
            "sourceCodec": evidence["sourceCodec"],
            "sourceManifestFormat": evidence["sourceManifestFormat"],
            "sourceBitrate": evidence["sourceBitrate"],
            "sourceBitrateBasis": dash.get("sourceBitrateBasis"),
            "sourceSampleRate": dash.get("sourceSampleRate"),
            "sourceChannels": dash.get("sourceChannels"),
            "sourceBitDepth": dash.get("sourceBitDepth"),
            "sourceAudioMode": "DOLBY_ATMOS",
            "manifestXml": validated_xml,
            "segmentUrls": segment_urls,
            "requestSession": session.request_session,
        }
    except Exception as exc:
        print(
            f"[-] python-tidal {auth_profile} JOC DASH check failed for track "
            f"{track_id}: {type(exc).__name__}"
        )
        raise


def resolve_eac3_joc(track_id: int) -> dict | None:
    """Resolve the selected JOC manifest using python-tidal.

    Try the authenticated PKCE DATA DASH manifest first. After a confirmed
    negative or validated protected JOC manifest, it may try the configured TV
    session. Clear E-AC-3 JOC availability depends on the track and session; an
    indeterminate PKCE failure does not trigger a profile fallback.
    """
    # Primary attempt: Web/Desktop DATA DASH
    try:
        with _TIDAL_API_LOCK:
            session = get_python_tidal_session()
            pkce_result = _resolve_eac3_joc_data_manifest(
                session, track_id, "python_tidal_pkce"
            )
            if pkce_result is not None:
                print(f"[+] python-tidal PKCE E-AC-3 JOC DASH resolved for track {track_id}")
                return pkce_result
    except Exception as exc:
        print(f"[-] python-tidal primary JOC check failed for track {track_id}: {type(exc).__name__}")
        # Fallback is reserved for a successful negative response or a valid
        # DRM-protected manifest. Every exception leaves the PKCE result
        # unknown, so trying another authenticated profile would hide failure.
        return None

    # Secondary attempt: TV Atmos OAuth profile (unencrypted E-AC-3 JOC)
    try:
        with _TIDAL_API_LOCK:
            tv_session = get_python_tidal_tv_session()
            if tv_session is not None:
                # The TV profile can return the same clear JOC DASH format as
                # the PKCE profile requests, while PKCE's manifest is DRM
                # protected. Prefer that EbbLabs manifest path before the
                # older single-file Track.get_stream() fallback.
                tv_dash_result = _resolve_eac3_joc_data_manifest(
                    tv_session, track_id, "python_tidal_tv"
                )
                if tv_dash_result is not None:
                    print(f"[+] python-tidal TV E-AC-3 JOC DASH resolved for track {track_id}")
                    return tv_dash_result

                # Preserve the existing EbbLabs TV-profile direct-stream path
                # for responses that do not provide a usable DASH manifest.
                track = tv_session.track(int(track_id))
                stream = track.get_stream()
                audio_mode = str(getattr(stream, "audio_mode", "") or "").upper()
                manifest = stream.get_stream_manifest()
                raw_codec = str(manifest.get_codecs() or "").strip().upper()
                normalized_codec = re.sub(r"[^a-z0-9]", "", raw_codec.lower())
                encryption = str(getattr(manifest, "encryption_type", "") or "").strip().upper()
                urls = manifest.get_urls() if hasattr(manifest, "get_urls") else []
                if (
                    audio_mode == "DOLBY_ATMOS"
                    and normalized_codec in {"eac3", "ec3", "eac3joc", "ec3joc"}
                    and encryption == "NONE"
                    and isinstance(urls, (list, tuple))
                    and len(urls) == 1
                    and isinstance(urls[0], str)
                    and urls[0].startswith("https://")
                ):
                    media_url = urls[0]
                    source_metrics = {}
                    try:
                        head_resp = requests.get(media_url, headers={"Range": "bytes=0-65535"}, timeout=10)
                        if head_resp.status_code in (200, 206) and head_resp.content:
                            source_metrics = parse_mp4_audio_metrics(head_resp.content)
                    except Exception:
                        pass

                    # Never present nominal Atmos values as measured stream
                    # metadata. The paused preparation/UI can report these as
                    # unavailable until the MP4 probe or decoder verifies them.
                    source_rate = source_metrics.get("sourceSampleRate")
                    source_bitrate = source_metrics.get("sourceBitrate")
                    source_channels = source_metrics.get("sourceChannels")

                    print(f"[+] python-tidal unencrypted TV E-AC-3 JOC resolved for track {track_id}")
                    return {
                        "format": "EAC3_JOC",
                        "codec": "eac3_joc",
                        "sourceAuthProfile": "python_tidal_tv",
                        "label": "Dolby Atmos (E-AC-3 JOC)",
                        "uri": media_url,
                        "transport": "DIRECT",
                        "sourceCodec": raw_codec.lower(),
                        "sourceManifestFormat": "EAC3_JOC",
                        "sourceBitrate": source_bitrate,
                        "sourceBitrateBasis": source_metrics.get("sourceBitrateBasis") or "unavailable (TV stream MP4 probe did not verify source metrics)",
                        "sourceSampleRate": source_rate,
                        "sourceChannels": source_channels,
                        "sourceBitDepth": None,
                        "sourceAudioMode": "DOLBY_ATMOS",
                        "manifestXml": None,
                        "segmentUrls": None,
                        "requestSession": tv_session.request_session,
                    }
    except Exception as exc:
        print(f"[-] python-tidal TV E-AC-3 JOC resolution failed for track {track_id}: {type(exc).__name__}")

    return None


def dash_manifest_contains_content_protection(manifest_xml: str) -> bool:
    """Reject DASH manifests that declare DRM protection at any hierarchy level."""
    if not isinstance(manifest_xml, str) or not manifest_xml.strip():
        raise ValueError("DASH manifest XML is unavailable")
    try:
        root = ET.fromstring(manifest_xml)
    except ET.ParseError as exc:
        raise ValueError("DASH manifest XML is malformed") from exc
    if root.tag.rsplit("}", 1)[-1] != "MPD":
        raise ValueError("DASH manifest root is not MPD")
    return any(
        element.tag.rsplit("}", 1)[-1] == "ContentProtection"
        for element in root.iter()
    )


def verify_eac3_joc_manifest(
    manifest_xml: str, *, allow_content_protection: bool = False
) -> dict:
    """Return facts for the single MPD representation only when it signals JOC."""
    if not isinstance(manifest_xml, str) or not manifest_xml.strip():
        raise ValueError("DASH manifest XML is unavailable")
    try:
        root = ET.fromstring(manifest_xml)
    except ET.ParseError as exc:
        raise ValueError("DASH manifest XML is malformed") from exc
    if root.tag.rsplit("}", 1)[-1] != "MPD":
        raise ValueError("DASH manifest root is not MPD")
    protected = any(
        element.tag.rsplit("}", 1)[-1] == "ContentProtection"
        for element in root.iter()
    )
    if protected and not allow_content_protection:
        raise ValueError("DASH ContentProtection is present")
    local_name = lambda element: element.tag.rsplit("}", 1)[-1]
    representations = [element for element in root.iter() if local_name(element) == "Representation"]
    if len(representations) != 1:
        raise ValueError("expected exactly one audio representation")
    representation = representations[0]
    adaptation = next(
        (
            parent
            for parent in root.iter()
            if local_name(parent) == "AdaptationSet"
            and representation in list(parent)
        ),
        None,
    )
    if adaptation is None:
        raise ValueError("selected representation has no AdaptationSet")
    representation_id = str(representation.get("id") or "")
    manifest_format = representation_id.split(",", 1)[0].strip().upper()
    codec = str(
        representation.get("codecs")
        or (adaptation.get("codecs", "") if adaptation is not None else "")
    ).strip().lower()
    normalized_codec = re.sub(r"[^a-z0-9]", "", codec)
    if manifest_format != "EAC3_JOC":
        raise ValueError("selected representation is not labeled EAC3_JOC")
    if normalized_codec not in {"ec3", "eac3", "ec3joc", "eac3joc"}:
        raise ValueError("selected representation does not declare an E-AC-3 codec")
    joc_markers = [
        element
        for scope in (adaptation, representation)
        for element in list(scope)
        if local_name(element) == "SupplementalProperty"
        and str(element.get("schemeIdUri") or "").strip().lower()
        == "tag:dolby.com,2018:dash:ec3_extensiontype:2018"
    ]
    if len(joc_markers) != 1:
        raise ValueError("expected exactly one selected Dolby JOC marker")
    if str(joc_markers[0].get("value") or "").strip().upper() != "JOC":
        raise ValueError("selected Dolby extension does not declare JOC")
    try:
        bandwidth = int(representation.get("bandwidth") or "")
    except (TypeError, ValueError, OverflowError):
        bandwidth = 0
    if bandwidth <= 0:
        raise ValueError("selected representation has no valid bitrate")
    return {
        "sourceCodec": codec,
        "normalizedCodec": normalized_codec,
        "sourceManifestFormat": manifest_format,
        "sourceBitrate": bandwidth,
    }

def _restrict_windows_session_acl(path: Path) -> None:
    """Restrict a session file to this user and LocalSystem on Windows."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    username = os.environ.get("USERNAME")
    if not username:
        raise RuntimeError("current Windows account name is unavailable")
    domain = os.environ.get("USERDOMAIN")
    account = f"{domain}\\{username}" if domain else username
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r",
         f"{account}:(F)", "*S-1-5-18:(F)"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    verified = subprocess.run(
        ["icacls", str(path)], check=True, capture_output=True,
        text=True, timeout=10,
    ).stdout
    if account not in verified or "NT AUTHORITY\\SYSTEM" not in verified:
        raise RuntimeError("session-file ACL verification failed")


def _create_secure_session_temp(target: Path, purpose: str) -> Path:
    """Create an empty unique temp file and secure it before writing tokens."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{target.name}.{purpose}-", suffix=".tmp", dir=str(target.parent)
    )
    os.close(fd)
    path = Path(name)
    try:
        _restrict_windows_session_acl(path)
        return path
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _persist_python_tidal_session(session, target: Path | None = None) -> None:
    """Atomically persist python-tidal token rotations without logging secrets."""
    target = target or PYTHON_TIDAL_SESSION_FILE
    temporary = _create_secure_session_temp(target, "refresh")
    data = {
        "token_type": {"data": session.token_type},
        "session_id": {"data": session.session_id},
        "access_token": {"data": session.access_token},
        "refresh_token": {"data": session.refresh_token},
        "is_pkce": {"data": session.is_pkce},
        "expiry_time": {
            "data": session.expiry_time.isoformat() if session.expiry_time else None
        },
    }
    try:
        # Keep EbbLabs' documented session-file schema while writing atomically.
        # save_session_to_file() calls check_login(), adding a network dependency
        # to every refresh save; this narrow serializer wrapper avoids that extra
        # API request while preserving EbbLabs' exact persisted field structure.
        with temporary.open("w", encoding="utf-8") as session_file:
            json.dump(data, session_file)
            session_file.flush()
            os.fsync(session_file.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


PYTHON_TIDAL_REQUEST_TIMEOUT = (5, 30)


def install_python_tidal_request_timeout(session) -> None:
    """Bound EbbLabs HTTP calls without replacing its auth/request machinery.

    EbbLabs 0.9.0 delegates to ``requests.Session.request`` without a default
    timeout. Install this before any session validation or refresh so a stalled
    API response cannot pin playback resolution indefinitely. Explicit
    per-request timeouts (such as DASH segment reads) remain authoritative.
    """
    request_session = getattr(session, "request_session", None)
    original_request = getattr(request_session, "request", None)
    if not callable(original_request):
        raise RuntimeError("python-tidal session has no usable requests transport")
    if getattr(request_session, "_omnifusion_timeout_installed", False) is True:
        return

    def request_with_default_timeout(method, url, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = PYTHON_TIDAL_REQUEST_TIMEOUT
        return original_request(method, url, *args, **kwargs)

    request_session.request = request_with_default_timeout
    request_session._omnifusion_timeout_installed = True


def get_python_tidal_session():
    """Return the authenticated EbbLabs/python-tidal PKCE session.

    Stream resolution uses the library-loaded PKCE session and its native
    request identity; do not replace the library's configured User-Agent.
    """
    global _TIDAL_API_SESSION
    with _TIDAL_API_LOCK:
        if _TIDAL_API_SESSION is not None:
            return _TIDAL_API_SESSION

        import tidalapi

        if not PYTHON_TIDAL_SESSION_FILE.is_file():
            raise RuntimeError("python-tidal PKCE session file is missing")

        # Restrict the bearer-token file before reading it. Reject legacy OAuth
        # sessions before sending their token to TIDAL; never log file contents.
        _restrict_windows_session_acl(PYTHON_TIDAL_SESSION_FILE)
        try:
            with PYTHON_TIDAL_SESSION_FILE.open("r", encoding="utf-8") as session_file:
                session_data = json.load(session_file)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("python-tidal session file is unreadable") from exc

        is_pkce = session_data.get("is_pkce", {}).get("data") if isinstance(session_data, dict) else None
        if is_pkce is not True:
            raise RuntimeError("python-tidal PKCE authentication is required")

        session = tidalapi.Session()
        install_python_tidal_request_timeout(session)
        # Session loading itself makes an authenticated request. If the token is
        # expired, EbbLabs refreshes it during load_session_from_file(), so install
        # persistence before loading to avoid losing a rotated refresh token.
        original_token_refresh = session.token_refresh
        def refresh_and_persist(refresh_token):
            refreshed = original_token_refresh(refresh_token)
            if refreshed:
                _persist_python_tidal_session(session)
            return refreshed
        session.token_refresh = refresh_and_persist
        if not session.load_session_from_file(PYTHON_TIDAL_SESSION_FILE):
            raise RuntimeError("python-tidal PKCE session is no longer authenticated")
        session.audio_quality = tidalapi.Quality.hi_res_lossless
        # Save the session id populated by EbbLabs after /sessions validation,
        # along with any token rotation that happened during the load.
        _persist_python_tidal_session(session)
        _TIDAL_API_SESSION = session
        return session


def get_python_tidal_mobile_session():
    """Load the separately authenticated legacy OAuth profile through EbbLabs.

    The mobile client is used only for AC-4 resolution after the standard PKCE
    EbbLabs stream request fails to return AC-4. Token refreshes are persisted
    atomically to the OAuth session file; the PKCE session remains independent.
    """
    global _TIDAL_MOBILE_API_SESSION
    with _TIDAL_API_LOCK:
        if _TIDAL_MOBILE_API_SESSION is not None:
            return _TIDAL_MOBILE_API_SESSION

        import tidalapi

        if not SESSION_FILE.is_file():
            raise RuntimeError("python-tidal OAuth session file is missing")
        _restrict_windows_session_acl(SESSION_FILE)
        try:
            with SESSION_FILE.open("r", encoding="utf-8") as session_file:
                session_data = json.load(session_file)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("python-tidal OAuth session file is unreadable") from exc
        is_pkce = session_data.get("is_pkce", {}).get("data") if isinstance(session_data, dict) else None
        if is_pkce is not False:
            raise RuntimeError("python-tidal mobile OAuth profile requires a non-PKCE session")

        config = tidalapi.Config()
        config.client_id = "km8T1xS355y7dd3H"
        config.client_secret = ""
        session = tidalapi.Session(config=config)
        install_python_tidal_request_timeout(session)
        session.audio_quality = tidalapi.Quality.hi_res_lossless

        # Install persistence before loading: the library may refresh an expired
        # access token while load_session_from_file validates the existing login.
        original_token_refresh = session.token_refresh
        def refresh_and_persist(refresh_token):
            refreshed = original_token_refresh(refresh_token)
            if refreshed:
                _persist_python_tidal_session(session, SESSION_FILE)
            return refreshed
        session.token_refresh = refresh_and_persist

        if not session.load_session_from_file(SESSION_FILE):
            raise RuntimeError("python-tidal OAuth session is no longer authenticated")
        # Save again after load so fields populated by the /sessions validation
        # (especially session_id) are persisted alongside any rotated token.
        _persist_python_tidal_session(session, SESSION_FILE)
        _TIDAL_MOBILE_API_SESSION = session
        return session



def get_python_tidal_tv_session():
    """Load the TV Atmos OAuth profile through EbbLabs.

    This uses the user's existing OAuth session token with the authorized TV client
    credentials (cgiF7TQuB97BUIu3) to obtain unencrypted E-AC-3 JOC Atmos streams.
    """
    global _TIDAL_TV_API_SESSION
    with _TIDAL_API_LOCK:
        if _TIDAL_TV_API_SESSION is not None:
            return _TIDAL_TV_API_SESSION

        import tidalapi

        # Keep the TV client's tokens isolated from the regular OAuth profile.
        # A refresh token must never be copied across client identities.
        session_file = TIDAL_TV_SESSION_FILE
        if not session_file.is_file():
            return None
        _restrict_windows_session_acl(session_file)
        try:
            with session_file.open("r", encoding="utf-8") as session_data_file:
                session_data = json.load(session_data_file)
        except Exception:
            return None

        if not isinstance(session_data, dict):
            return None
        def value(field, default=None):
            item = session_data.get(field, {})
            return item.get("data", default) if isinstance(item, dict) else default

        if value("is_pkce") is not False:
            return None
        refresh_token = value("refresh_token")
        access_token = value("access_token")
        token_type = value("token_type") or "Bearer"
        if not refresh_token or not access_token:
            return None
        try:
            expiry_raw = value("expiry_time")
            expiry = datetime.datetime.fromisoformat(expiry_raw) if expiry_raw else None
        except (TypeError, ValueError):
            expiry = None

        tv_client = {
            "client_id": "cgiF7TQuB97BUIu3",
            "client_secret": "1nqpgx8uvBdZigrx4hUPDV2hOwgYAAAG5DYXOr6uNf8=",
        }
        config = tidalapi.Config()
        config.client_id = tv_client["client_id"]
        config.client_secret = tv_client["client_secret"]
        session = tidalapi.Session(config=config)
        install_python_tidal_request_timeout(session)
        # EbbLabs' refresh implementation retains an existing refresh token if
        # TIDAL omits a rotated value. Seed it before refreshing so persistence
        # never writes a TV profile that cannot refresh again.
        session.is_pkce = False
        session.refresh_token = refresh_token

        # Persist a rotated refresh token immediately, even if the subsequent
        # /sessions validation fails or the process exits during initialization.
        original_token_refresh = session.token_refresh
        def refresh_and_persist(refresh_token):
            refreshed = original_token_refresh(refresh_token)
            if refreshed:
                _persist_python_tidal_session(session, TIDAL_TV_SESSION_FILE)
            return refreshed
        session.token_refresh = refresh_and_persist

        # Refresh only when the saved access token has expired (or has no
        # trustworthy expiry). EbbLabs owns the OAuth HTTP exchange and applies
        # this configured TV client identity.
        expiry_utc = expiry
        if expiry_utc is not None and expiry_utc.tzinfo is None:
            expiry_utc = expiry_utc.replace(tzinfo=datetime.timezone.utc)
        expired = expiry_utc is None or expiry_utc <= datetime.datetime.now(datetime.timezone.utc)
        if expired:
            if not session.token_refresh(refresh_token):
                return None
            # token_refresh updates the library session in place, including a
            # rotated refresh token. Reuse those exact values below.
            access_token = session.access_token
            refresh_token = session.refresh_token
            token_type = session.token_type
            expiry = session.expiry_time

        loaded = session.load_oauth_session(
            token_type,
            access_token,
            refresh_token,
            expiry_time=expiry,
            is_pkce=False,
        )
        # A token may have been revoked before its recorded expiry. Give the
        # library one native refresh attempt, then retry loading once.
        if not loaded and not expired and session.token_refresh(refresh_token):
            loaded = session.load_oauth_session(
                session.token_type,
                session.access_token,
                session.refresh_token,
                expiry_time=session.expiry_time,
                is_pkce=False,
            )
        if not loaded or not session.check_login():
            return None

        session.audio_quality = tidalapi.Quality.hi_res_lossless
        _persist_python_tidal_session(session, TIDAL_TV_SESSION_FILE)
        _TIDAL_TV_API_SESSION = session
        return session


def python_tidal_negative_manifest_is_complete(stream, manifest) -> bool:
    """Accept only complete, unprotected non-AC-4 manifests as PKCE negatives."""
    encryption_type = str(getattr(manifest, "encryption_type", "") or "").strip().upper()
    if encryption_type != "NONE" or getattr(manifest, "encryption_key", None):
        return False
    urls = manifest.get_urls()
    if (not isinstance(urls, (list, tuple)) or not urls
            or any(not isinstance(url, str) or not url.startswith("https://") for url in urls)):
        return False

    if bool(getattr(stream, "is_bts", False)):
        # The installed EbbLabs BTS parser currently exposes a single URL;
        # multiple URLs are outside the behavior this app has verified.
        return len(urls) == 1
    if not bool(getattr(stream, "is_mpd", False)) or len(urls) < 2:
        return False

    return python_tidal_dash_manifest_segments_are_complete(
        stream.get_manifest_data(), urls
    )


def python_tidal_dash_manifest_segments_are_complete(
    manifest_xml: str, urls: list[str] | tuple[str, ...]
) -> bool:
    """Verify EbbLabs expanded one DASH init URL plus every timeline segment."""
    if (
        not isinstance(urls, (list, tuple))
        or len(urls) < 2
        or any(not isinstance(url, str) or not url.startswith("https://") for url in urls)
    ):
        return False

    try:
        root = ET.fromstring(manifest_xml)
    except (ET.ParseError, TypeError, ValueError):
        return False
    if root.tag.rsplit("}", 1)[-1] != "MPD":
        return False
    if any(element.tag.rsplit("}", 1)[-1] == "ContentProtection" for element in root.iter()):
        return False
    representations = [
        element for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "Representation"
    ]
    if len(representations) != 1:
        return False
    representation = representations[0]
    templates = [
        element for element in representation.iter()
        if element.tag.rsplit("}", 1)[-1] == "SegmentTemplate"
    ]
    all_templates = [
        element for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "SegmentTemplate"
    ]
    if len(templates) != 1 or len(all_templates) != 1:
        return False

    template = templates[0]
    initialization = str(template.get("initialization") or "").strip()
    media = str(template.get("media") or "").strip()
    if not initialization or "$Number$" not in media:
        return False
    try:
        start_number = int(template.get("startNumber") or 1)
    except (TypeError, ValueError, OverflowError):
        return False
    if start_number < 1:
        return False

    timelines = [
        element for element in template.iter()
        if element.tag.rsplit("}", 1)[-1] == "SegmentTimeline"
    ]
    if len(timelines) != 1:
        return False
    segment_entries = [
        element for element in timelines[0]
        if element.tag.rsplit("}", 1)[-1] == "S"
    ]
    if not segment_entries:
        return False
    segment_count = 0
    for entry in segment_entries:
        try:
            duration = int(entry.get("d") or 0)
            repeat = int(entry.get("r") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if duration <= 0 or repeat < 0:
            return False
        segment_count += repeat + 1

    # The bundled EbbLabs DashInfo returns one initialization URL plus one
    # URL for each numbered timeline segment. Do not trust a partial expansion.
    return len(urls) == segment_count + 1


def python_tidal_quality_candidates(session) -> list:
    """Return every EbbLabs Quality value, starting with the session default.

    Quality is a preference, not a codec selector. Trying every value through
    Track.get_stream() lets the service establish whether a different account
    quality exposes AC-4 before it considers the separate OAuth profile. Read
    the library's enum directly; session.audio_quality may be a plain string.
    """
    import tidalapi

    current = getattr(session, "audio_quality", None)
    quality_type = getattr(tidalapi, "Quality", None)
    members = getattr(quality_type, "__members__", {})
    candidates = list(dict.fromkeys(members.values())) if members else []
    if current is not None:
        # Prefer the active value only when it is one of the library's known
        # values. An unknown session value cannot establish full coverage.
        if current in candidates:
            candidates.remove(current)
            candidates.insert(0, current)
    return candidates


class Ac4TransportUnavailable(RuntimeError):
    """A real AC-4 candidate exists, but this build cannot safely play it."""

    def __init__(self, code: str, message: str, *, transport: str, segment_count: int = 0):
        super().__init__(message)
        self.code = code
        self.transport = transport
        self.segment_count = segment_count
        self.auth_profile = "unknown"
        self.source_codec = "unknown"
        self.requested_quality = "unknown"


def resolve_ac4_ims(track_id: int) -> dict | None:
    """Resolve AC-4 through EbbLabs/python-tidal with PKCE-first profile order.

    The separate mobile OAuth profile is queried only when PKCE Track.get_stream()
    does not return AC-4; both profiles use the library Track.get_stream() API.
    This reads the authenticated track stream manifest only. It does not start
    mpv or fetch audio payload bytes; a selected AC-4 URL is returned to the
    existing OxideAV decoding path only after the manifest reports AC-4.
    """
    try:
        with _TIDAL_API_LOCK:
            selected = None
            last_diagnostic = None

            # PKCE must produce a complete, clean negative result before the
            # separate OAuth identity is even loaded. A timeout, auth error,
            # rate limit, malformed manifest, or missing Quality inventory is
            # indeterminate and must never trigger a different client profile.
            try:
                pkce_session = get_python_tidal_session()
                original_quality = getattr(pkce_session, "audio_quality", None)
                qualities = python_tidal_quality_candidates(pkce_session)
                import tidalapi
                quality_members = getattr(getattr(tidalapi, "Quality", None), "__members__", {})
                expected_qualities = list(dict.fromkeys(quality_members.values()))
                if not qualities:
                    raise RuntimeError("python-tidal Quality inventory is empty")
                complete_quality_inventory = (
                    bool(expected_qualities) and set(qualities) == set(expected_qualities)
                )
                attempted_qualities = []
                pkce_all_terminal_non_ac4 = complete_quality_inventory
                try:
                    for quality in qualities:
                        attempted_qualities.append(quality)
                        pkce_session.audio_quality = quality
                        requested_quality = str(
                            getattr(quality, "value", quality) or "unknown"
                        )
                        candidate_track = pkce_session.track(int(track_id))
                        candidate_stream = candidate_track.get_stream()
                        candidate_manifest = candidate_stream.get_stream_manifest()
                        candidate_codec = str(
                            candidate_manifest.get_codecs() or ""
                        ).strip().upper()
                        normalized_codec = candidate_codec.replace("-", "").split(".", 1)[0]
                        if normalized_codec in {"AC4", "AC4IMS"}:
                            selected = (
                                "pkce", pkce_session, candidate_track,
                                candidate_stream, candidate_manifest, candidate_codec,
                                requested_quality,
                            )
                            pkce_all_terminal_non_ac4 = False
                            break
                        if normalized_codec not in {
                            "FLAC", "AAC", "MP3", "MP4A", "ALAC", "OPUS",
                            "EAC3", "EAC3JOC", "AC3", "DTS", "DTSHD",
                        }:
                            raise RuntimeError("python-tidal returned an unknown codec declaration")
                        if not python_tidal_negative_manifest_is_complete(
                            candidate_stream, candidate_manifest
                        ):
                            raise RuntimeError("python-tidal returned an incomplete or protected negative manifest")
                        last_diagnostic = (
                            candidate_codec or "unknown",
                            str(getattr(candidate_stream, "audio_mode", "") or "unknown"),
                            requested_quality,
                            str(getattr(candidate_stream, "audio_quality", "") or ""),
                        )
                finally:
                    if original_quality is not None:
                        pkce_session.audio_quality = original_quality
                if len(attempted_qualities) != len(expected_qualities):
                    pkce_all_terminal_non_ac4 = False
            except Exception as pkce_exc:
                # Exception type only: library/network errors can carry signed
                # manifest URLs or account/session details in their message.
                print(
                    f"[-] python-tidal PKCE AC-4 check indeterminate for track "
                    f"{track_id}: {type(pkce_exc).__name__}; OAuth fallback withheld"
                )
                return None

            if selected is None and pkce_all_terminal_non_ac4:
                # Prior live checks found Natural as FLAC across all PKCE
                # qualities and AC-4 through this distinct EbbLabs profile.
                # Keep it conditional: only a complete clean PKCE negative may
                # reach this branch.
                try:
                    oauth_session = get_python_tidal_mobile_session()
                    oauth_quality = getattr(oauth_session, "audio_quality", None)
                    if oauth_quality is None:
                        raise RuntimeError("OAuth quality is unavailable")
                    oauth_track = oauth_session.track(int(track_id))
                    oauth_stream = oauth_track.get_stream()
                    oauth_manifest = oauth_stream.get_stream_manifest()
                    oauth_codec = str(oauth_manifest.get_codecs() or "").strip().upper()
                    normalized_codec = oauth_codec.replace("-", "").split(".", 1)[0]
                    if normalized_codec in {"AC4", "AC4IMS"}:
                        selected = (
                            "mobile_oauth", oauth_session, oauth_track,
                            oauth_stream, oauth_manifest, oauth_codec,
                            str(getattr(oauth_quality, "value", oauth_quality)),
                        )
                    else:
                        last_diagnostic = (
                            oauth_codec or "unknown",
                            str(getattr(oauth_stream, "audio_mode", "") or "unknown"),
                            str(getattr(oauth_quality, "value", oauth_quality)),
                            str(getattr(oauth_stream, "audio_quality", "") or ""),
                        )
                except Exception as oauth_exc:
                    print(
                        f"[-] python-tidal conditional OAuth AC-4 check failed for "
                        f"track {track_id}: {type(oauth_exc).__name__}"
                    )
                    return None

            if selected is None:
                if last_diagnostic:
                    codec_text, audio_mode, requested_quality, audio_quality = last_diagnostic
                    print(
                        f"[-] python-tidal AC-4 unavailable for track {track_id}: "
                        f"codec={codec_text}, mode={audio_mode}, "
                        f"requested_quality={requested_quality}, "
                        f"returned_quality={audio_quality}"
                    )
                return None

            profile_name, session, track, stream, manifest, raw_codec, requested_quality = selected
            codec = raw_codec.replace("-", "").split(".", 1)[0]
            encryption_type = str(getattr(manifest, "encryption_type", "") or "").strip().upper()
            encryption_key = getattr(manifest, "encryption_key", None)
            if bool(getattr(stream, "is_bts", False)):
                if encryption_type != "NONE" or encryption_key:
                    raise Ac4TransportUnavailable(
                        "AC4_PROTECTED_STREAM",
                        "AC-4 BTS stream is protected or has unknown encryption; playback was blocked",
                        transport="BTS",
                    )
                urls = manifest.get_urls()
                if (not isinstance(urls, (list, tuple)) or len(urls) != 1
                        or not isinstance(urls[0], str) or not urls[0].startswith("https://")):
                    raise Ac4TransportUnavailable(
                        "AC4_BTS_INVALID_TRANSPORT",
                        "AC-4 BTS stream did not provide exactly one HTTPS media URL; playback was blocked",
                        transport="BTS",
                    )
                # The codec name alone cannot prove an IMS presentation. The
                # OxideAV bitstream probe remains mandatory before playback.
                return {
                    "format": "AC4_IMS",
                    "codec": "ac4",
                    "label": "AC-4 (IMS presentation unverified)",
                    "presentationVerified": False,
                    "transport": "BTS",
                    "uri": urls[0],
                    "sourceCodec": raw_codec,
                    "requestedQuality": requested_quality,
                    "sourceQuality": str(getattr(stream, "audio_quality", "") or ""),
                    "sourceAudioMode": str(getattr(stream, "audio_mode", "") or ""),
                    "sourceAuthProfile": profile_name,
                }

            if bool(getattr(stream, "is_mpd", False)):
                if encryption_type != "NONE" or encryption_key:
                    raise Ac4TransportUnavailable(
                        "AC4_PROTECTED_DASH",
                        "AC-4 DASH manifest declares protection or unknown encryption; playback was blocked",
                        transport="DASH",
                    )
                urls = manifest.get_urls()
                if (not isinstance(urls, (list, tuple)) or len(urls) < 2
                        or any(not isinstance(url, str) or not url.startswith("https://") for url in urls)):
                    raise Ac4TransportUnavailable(
                        "AC4_DASH_INVALID_SEGMENTS",
                        "AC-4 DASH manifest did not provide a valid initialization segment and media segments",
                        transport="DASH",
                        segment_count=len(urls) if isinstance(urls, (list, tuple)) else 0,
                    )
                raise Ac4TransportUnavailable(
                    "AC4_DASH_UNSUPPORTED_TRANSPORT",
                    "AC-4 was found in an unprotected DASH manifest, but this build cannot yet assemble and validate its fragments; no fallback stream was selected",
                    transport="DASH",
                    segment_count=len(urls),
                )

            raise Ac4TransportUnavailable(
                "AC4_UNKNOWN_TRANSPORT",
                "AC-4 was found in an unsupported manifest type; playback was blocked",
                transport="unknown",
            )
    except Ac4TransportUnavailable as exc:
        exc.auth_profile = profile_name
        exc.source_codec = raw_codec
        exc.requested_quality = requested_quality
        raise
    except Exception as exc:
        # Exception text may contain signed URLs or API/session details.
        print(f"[-] python-tidal AC-4 resolution failed for track {track_id}: {type(exc).__name__}")
        return None


def is_verified_ac4_ims(stream_info: dict | None) -> bool:
    """Require presentation-level evidence before treating AC-4 as binaural IMS."""
    if not isinstance(stream_info, dict):
        return False
    try:
        channels = int(stream_info.get("sourceChannels") or 0)
        decoded_channels = int(stream_info.get("decodedChannels") or 0)
        source_rate = int(stream_info.get("sourceSampleRate") or 0)
        decoded_rate = int(stream_info.get("decodedSampleRate") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        stream_info.get("format") == "AC4_IMS"
        and str(stream_info.get("codec") or "").casefold() in {"ac4", "ac-4"}
        and stream_info.get("presentation") == "IMS"
        and stream_info.get("presentationVerified") is True
        and channels == 2
        and decoded_channels == 2
        and source_rate > 0
        and decoded_rate == source_rate
        and stream_info.get("sourceAudioMode") == "DOLBY_ATMOS"
    )


def mark_verified_ac4_ims(stream_info: dict) -> bool:
    """Expose the verified IMS label only after source and PCM checks pass."""
    if not is_verified_ac4_ims(stream_info):
        return False
    stream_info["label"] = "AC-4 IMS"
    return True


def parse_oxideav_ac4_probe(stdout: str) -> dict[str, str]:
    """Parse the converter's single-line key=value probe without guessing."""
    if not isinstance(stdout, str):
        return {}
    fields = {}
    for token in stdout.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key and value:
            fields[key] = value
    return fields


def is_verified_oxideav_atmos_ims_probe(fields: dict, source_rate: int) -> bool:
    """Require the adapter's MP4 DSI/TOC gate and stereo decode geometry."""
    try:
        rate = int(source_rate)
        probed_rate = int(fields.get("sample_rate", 0))
        channels = int(fields.get("channels", 0))
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        fields.get("dsi_status") == "header-matches-toc"
        and fields.get("ims_atmos_verified") == "true"
        and rate > 0
        and probed_rate == rate
        and channels == 2
    )


def get_python_tidal_track_info(track_id: int) -> dict | None:
    """Read catalog-level quality tags through the authenticated PKCE session."""
    now = time.monotonic()
    with _TIDAL_API_LOCK:
        cached = _TIDAL_API_TRACK_CACHE.get(int(track_id))
        if cached and now - cached[0] < _TIDAL_API_TRACK_CACHE_TTL:
            return dict(cached[1])
        try:
            session = get_python_tidal_session()
            track = session.track(int(track_id))
            modes = getattr(track, "audio_modes", None) or []
            if isinstance(modes, str):
                audio_mode = modes.upper()
            else:
                audio_mode = ",".join(sorted(
                    str(getattr(mode, "value", mode)).upper() for mode in modes
                ))
            info = {
                "trackIsHiRes": bool(track.is_hi_res_lossless),
                "trackIsLossless": bool(track.is_lossless),
                "trackAudioQuality": str(track.audio_quality or "").upper(),
                "audioMode": audio_mode,
            }
            _TIDAL_API_TRACK_CACHE[int(track_id)] = (now, info)
            return dict(info)
        except Exception as exc:
            print(f"[-] python-tidal catalog metadata unavailable for track {track_id}: {type(exc).__name__}")
            return None
def is_verified_python_tidal_hires(info: dict | None, dash_info: dict | None = None) -> bool:
    """Require the catalog Hi-Res tag and an explicit FLAC_HIRES rendition."""
    if not isinstance(info, dict) or not info.get("trackIsHiRes"):
        return False
    if not isinstance(dash_info, dict) or str(dash_info.get("sourceManifestFormat", "")).upper() != "FLAC_HIRES":
        return False
    try:
        sample_rate = int(dash_info.get("sourceSampleRate") or 0)
        bit_depth = int(dash_info.get("sourceBitDepth") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return sample_rate >= 8000 and bit_depth >= 24

def get_tidal_dash_audio_metadata(
    uri: str | None = None,
    request_session=None,
    *,
    manifest_xml: str | None = None,
    segment_urls: list[str] | None = None,
) -> dict:
    """Read DASH audio metadata from a manifest returned by python-tidal.

    When Track.get_stream() already supplied the decoded manifest, parse it
    directly and avoid a second manifest request. The URI form remains for
    EbbLabs manifest resolvers that return a signed DASH document URI.
    """
    try:
        if manifest_xml is None:
            if not isinstance(uri, str) or not uri.startswith("https://"):
                raise ValueError("a valid HTTPS manifest URI or manifest XML is required")
            http = request_session if request_session is not None else requests
            response = http.get(uri, timeout=10)
            response.raise_for_status()
            manifest_xml = response.text
        root = ET.fromstring(manifest_xml)

        def local_name(element):
            return element.tag.rsplit("}", 1)[-1]

        adaptation_by_rep = {}
        representations = []
        for node in root.iter():
            if local_name(node) == "AdaptationSet":
                adaptation_by_rep.update({child: node for child in node if local_name(child) == "Representation"})
            elif local_name(node) == "Representation":
                representations.append(node)
        if root.tag.rsplit("}", 1)[-1] != "MPD" or len(representations) != 1:
            # The bundled EbbLabs DashInfo and local proxy consume the first
            # Representation. Refuse ambiguous MPDs rather than report metadata
            # for a different stream than the fragments used for playback.
            raise ValueError("expected a single-representation TIDAL audio MPD")

        def positive_int(value):
            try:
                parsed = int(value)
                return parsed if parsed > 0 else None
            except (TypeError, ValueError, OverflowError):
                return None

        def rep_details(rep):
            adaptation = adaptation_by_rep.get(rep)
            attrs = {**(adaptation.attrib if adaptation is not None else {}), **rep.attrib}
            representation_id = str(attrs.get("id") or "")
            id_parts = [part.strip() for part in representation_id.split(",")]
            manifest_format = id_parts[0].upper() if id_parts else ""
            sample_rate = positive_int(attrs.get("audioSamplingRate"))
            if sample_rate is None and len(id_parts) > 1:
                sample_rate = positive_int(id_parts[1])
            bit_depth = positive_int(attrs.get("bitsPerSample") or attrs.get("bitDepth"))
            if bit_depth is None and len(id_parts) > 2:
                bit_depth = positive_int(id_parts[2])
            bitrate = positive_int(attrs.get("bandwidth"))
            codec = str(attrs.get("codecs") or (adaptation.attrib.get("codecs", "") if adaptation is not None else "")).lower()
            return {
                "sourceBitrate": bitrate,
                "sourceSampleRate": sample_rate,
                "sourceBitDepth": bit_depth,
                "sourceCodec": codec or None,
                "sourceManifestFormat": manifest_format or None,
                "sourceRepresentationId": representation_id or None,
            }

        candidates = [rep_details(rep) for rep in representations]
        selected = max(candidates, key=lambda item: item.get("sourceBitrate") or 0, default={})
        selected_representation = representations[0]

        def channel_configuration(scope):
            if scope is None:
                return None, False
            declarations = [
                node for node in list(scope)
                if local_name(node) == "AudioChannelConfiguration"
            ]
            if not declarations:
                return None, False
            for declaration in declarations:
                value = str(declaration.attrib.get("value") or "")
                if value.isdigit() and int(value) > 0:
                    return int(value), True
            # A representation-level declaration takes precedence even when its
            # scheme is unsupported; do not borrow another layer's channel count.
            return None, True

        channel_count, representation_declared_channels = channel_configuration(
            selected_representation
        )
        if not representation_declared_channels:
            channel_count, _ = channel_configuration(
                adaptation_by_rep.get(selected_representation)
            )
        selected = dict(selected)
        selected["sourceChannels"] = channel_count
        selected["sourceBitrateBasis"] = (
            "TIDAL DASH single Representation bandwidth (declared)"
            if selected.get("sourceBitrate") else None
        )
        # Keep signed segment URLs private inside the service. EbbLabs' own
        # DashInfo parser expands the MPD template in the same way as its
        # StreamManifest.get_urls() helper.
        from tidalapi.media import DashInfo
        dash_info = DashInfo.from_mpd(manifest_xml)
        selected["manifestXml"] = manifest_xml
        selected["segmentUrls"] = list(
            segment_urls if segment_urls is not None else (dash_info.urls or [])
        )
        return selected
    except Exception as exc:
        print(f"[-] TIDAL DASH bitrate metadata unavailable: {type(exc).__name__}")
        return {}

def resolve_hi_res_or_lossless(track_id: int, quality="HI_RES_LOSSLESS") -> dict | None:
    """Resolve the library-selected FLAC stream using Track.get_stream()."""
    quality = str(getattr(quality, "value", quality) or "").upper()
    if quality not in {"HI_RES_LOSSLESS", "LOSSLESS"}:
        return None
    try:
        import tidalapi

        with _TIDAL_API_LOCK:
            session = get_python_tidal_session()
            original_quality = getattr(session, "audio_quality", None)
            try:
                requested_quality = (
                    tidalapi.Quality.hi_res_lossless
                    if quality == "HI_RES_LOSSLESS"
                    else tidalapi.Quality.high_lossless
                )
                session.audio_quality = requested_quality
                track = session.track(int(track_id))
                # Track capability flags can be stale or incomplete for an
                # Atmos catalog item. Treat Track.get_stream()'s returned
                # quality and the parsed FLAC manifest as authoritative; the
                # Hi-Res path still requires its explicit manifest evidence.
                stream = track.get_stream()
                actual_quality = str(
                    getattr(stream.audio_quality, "value", stream.audio_quality) or ""
                ).upper()
                if actual_quality != quality:
                    print(
                        f"[-] python-tidal returned {actual_quality or 'unknown'} for "
                        f"requested {quality} on track {track_id}"
                    )
                    return None
                if not stream.is_mpd:
                    print(f"[-] python-tidal returned a non-DASH {quality} stream for track {track_id}")
                    return None

                manifest = stream.get_stream_manifest()
                codec = str(manifest.get_codecs() or "").strip().upper()
                if codec != "FLAC" or getattr(manifest, "is_encrypted", False):
                    print(
                        f"[-] python-tidal returned unsupported {codec or 'unknown'} "
                        f"manifest for {quality} track {track_id}"
                    )
                    return None
                manifest_xml = stream.get_manifest_data()
                # Keep this resolver aligned with the proxy's DRM boundary even
                # if a python-tidal version misreports StreamManifest.is_encrypted.
                if dash_manifest_contains_content_protection(manifest_xml):
                    print(f"[-] python-tidal returned protected DASH for track {track_id}")
                    return None
                segment_urls = list(manifest.get_urls() or [])
                if len(segment_urls) < 2 or any(
                    not isinstance(url, str) or not url.startswith("https://")
                    for url in segment_urls
                ):
                    print(f"[-] python-tidal returned incomplete DASH segment URLs for track {track_id}")
                    return None
            finally:
                session.audio_quality = original_quality

        dash = get_tidal_dash_audio_metadata(
            manifest_xml=manifest_xml,
            segment_urls=segment_urls,
        )
        if not dash or dash.get("sourceCodec") != "flac":
            return None
        if quality == "HI_RES_LOSSLESS" and not is_verified_python_tidal_hires(
            {"trackIsHiRes": True}, dash
        ):
            return None
        label = "Hi-Res FLAC" if quality == "HI_RES_LOSSLESS" else "Lossless FLAC"
        return {
            "format": quality, "codec": "flac", "label": label,
            "uri": segment_urls[0],
            "sourceCodec": codec,
            "sourceManifestFormat": dash.get("sourceManifestFormat"),
            "sourceBitrate": dash.get("sourceBitrate"),
            "sourceBitrateBasis": dash.get("sourceBitrateBasis"),
            "sourceSampleRate": dash.get("sourceSampleRate"),
            "sourceChannels": dash.get("sourceChannels"),
            "sourceBitDepth": dash.get("sourceBitDepth"),
            "sourceQuality": actual_quality,
            "manifestXml": manifest_xml,
            "segmentUrls": segment_urls,
            "requestSession": session.request_session,
        }
    except Exception as exc:
        print(f"[-] python-tidal {quality} stream resolution failed for track {track_id}: {type(exc).__name__}")
        return None


class TidalDashProxy:
    """Serve EbbLabs-expanded DASH fragments to MPV from loopback only.

    TIDAL's signed segment URLs remain inside this process. MPV sees a local
    MPD and requests each fragment through a private random path; the EbbLabs
    request session fetches the original signed CDN URL.
    """

    def __init__(self, manifest_xml: str, segment_urls: list[str], request_session):
        if not isinstance(manifest_xml, str) or not manifest_xml.strip():
            raise ValueError("TIDAL DASH manifest is unavailable")
        if not isinstance(segment_urls, list) or len(segment_urls) < 2:
            raise ValueError("TIDAL DASH manifest has no initialization and media segments")
        if any(not isinstance(url, str) or not url.startswith("https://") for url in segment_urls):
            raise ValueError("TIDAL DASH segment URL is invalid")
        self.request_session = request_session
        self.segment_urls = segment_urls
        self.segment_requests = 0
        self.failed_segments = 0
        self.bytes_served = 0
        self.recent_segment_indices = []
        self._counter_lock = threading.Lock()
        root = ET.fromstring(manifest_xml)
        if root.tag.rsplit("}", 1)[-1] != "MPD":
            raise ValueError("TIDAL DASH manifest root is not MPD")
        if dash_manifest_contains_content_protection(manifest_xml):
            raise ValueError("TIDAL DASH manifest declares ContentProtection; custom playback is blocked")
        templates = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "SegmentTemplate"]
        if len(templates) != 1:
            raise ValueError("TIDAL DASH manifest must contain exactly one audio SegmentTemplate")
        template = templates[0]
        media_template = str(template.get("media") or "")
        if "$Number$" not in media_template:
            raise ValueError("TIDAL DASH media template does not use segment numbers")
        unsupported_tokens = [
            token for token in re.findall(r"\$[^$]+\$", media_template)
            if token != "$Number$"
        ]
        if unsupported_tokens:
            raise ValueError("TIDAL DASH media template contains unsupported placeholders")
        self.start_number = int(template.get("startNumber") or 1)
        template.set("initialization", "init")
        template.set("media", "segment/$Number$")
        # Relative templates must resolve against this local manifest, never a
        # TIDAL CDN BaseURL left in the source MPD.
        for parent in list(root.iter()):
            for child in list(parent):
                if child.tag.rsplit("}", 1)[-1] == "BaseURL":
                    parent.remove(child)
        self.manifest_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        self.token = uuid.uuid4().hex
        self.server = None
        self.url = None

    def start(self) -> str:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_HEAD(self):
                self._serve(send_body=False)

            def do_GET(self):
                self._serve(send_body=True)

            def _serve(self, send_body: bool):
                if self.path == f"/{proxy.token}/manifest.mpd":
                    body = proxy.manifest_bytes
                    self.send_response(200)
                    self.send_header("Content-Type", "application/dash+xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if send_body:
                        try:
                            self.wfile.write(body)
                        except OSError:
                            pass
                    return

                parsed = urllib.parse.urlsplit(self.path)
                prefix = f"/{proxy.token}/segment/"
                if parsed.path == f"/{proxy.token}/init":
                    index = 0
                elif parsed.path.startswith(prefix):
                    try:
                        number = int(parsed.path[len(prefix):])
                    except ValueError:
                        self.send_error(404)
                        return
                    index = number - proxy.start_number + 1
                else:
                    self.send_error(404)
                    return
                if index < 0 or index >= len(proxy.segment_urls):
                    self.send_error(404)
                    return

                headers = {}
                if self.headers.get("Range"):
                    headers["Range"] = self.headers["Range"]
                try:
                    if send_body:
                        with proxy._counter_lock:
                            proxy.segment_requests += 1
                            proxy.recent_segment_indices.append(index)
                            if len(proxy.recent_segment_indices) > 64:
                                del proxy.recent_segment_indices[:-64]
                    upstream = proxy.request_session.get(
                        proxy.segment_urls[index], headers=headers,
                        stream=True, timeout=(5, 30),
                    )
                    if upstream.status_code not in (200, 206):
                        upstream.close()
                        with proxy._counter_lock:
                            proxy.failed_segments += 1
                        self.send_error(502, "TIDAL segment request failed")
                        return

                    self.send_response(upstream.status_code)
                    self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/octet-stream"))
                    content_length = upstream.headers.get("Content-Length")
                    content_range = upstream.headers.get("Content-Range")
                    if content_length:
                        self.send_header("Content-Length", content_length)
                    if content_range:
                        self.send_header("Content-Range", content_range)
                    accept_ranges = upstream.headers.get("Accept-Ranges")
                    if accept_ranges:
                        self.send_header("Accept-Ranges", accept_ranges)
                    self.end_headers()
                    if send_body:
                        try:
                            for chunk in upstream.iter_content(65536):
                                if not chunk:
                                    continue
                                self.wfile.write(chunk)
                                with proxy._counter_lock:
                                    proxy.bytes_served += len(chunk)
                        except OSError:
                            # MPV can close a request when its demuxer has
                            # enough buffered data; that is normal cancellation.
                            pass
                    upstream.close()
                except requests.RequestException as exc:
                    with proxy._counter_lock:
                        proxy.failed_segments += 1
                    print(f"[-] TIDAL segment proxy failed for segment {index}: {type(exc).__name__}")
                    try:
                        self.send_error(502, "TIDAL segment request failed")
                    except OSError:
                        pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/{self.token}/manifest.mpd"
        return self.url

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

def resolve_with_priority(track_id: int, priority_list: list[str]) -> dict:
    resolvers = {
        "EAC3_JOC": lambda tid: resolve_eac3_joc(tid),
        "AC4_IMS": lambda tid: resolve_ac4_ims(tid),
        "HI_RES_LOSSLESS": lambda tid: resolve_hi_res_or_lossless(tid, "HI_RES_LOSSLESS"),
        "LOSSLESS": lambda tid: resolve_hi_res_or_lossless(tid, "LOSSLESS"),
    }
    
    # Track candidate cache: clicked track is always evaluated
    candidate_ids = [track_id]
    
    # RealMAX cross-edition discovery: find alternate album releases (Atmos editions, Deluxe Hi-Res, etc.)
    try:
        import tidalapi
        session = get_python_tidal_mobile_session()
        if session:
            orig = session.track(track_id)
            if orig and orig.name:
                artist_name = getattr(orig.artist, "name", "") if getattr(orig, "artist", None) else ""
                query = f"{orig.name} {artist_name}".strip()
                search_res = session.search(query, models=[tidalapi.media.Track], limit=20)
                for cand in search_res.get("tracks", []):
                    if cand.id != track_id and cand.name.lower() == orig.name.lower() and abs(cand.duration - orig.duration) <= 6:
                        candidate_ids.append(cand.id)
    except Exception as exc:
        print(f"[-] RealMAX candidate search error: {exc}")

    # Evaluate each format in user's strict priority order
    for fmt in priority_list:
        if fmt not in resolvers:
            continue
        for tid in candidate_ids:
            try:
                res = resolvers[fmt](tid)
                if res:
                    if tid == track_id:
                        print(f"[+] Track {track_id} resolved directly as: {fmt}")
                    else:
                        print(f"[RealMAX] Upgraded track {track_id} -> {tid} as: {fmt}")
                    return res
            except Exception as e:
                pass

    raise RuntimeError(f"Could not resolve any audio stream for track {track_id}")



active_login_flow = {
    "flowId": None,
    "deviceCode": None,
    "userCode": None,
    "verificationUrl": None,
    "expiresIn": 0,
    "interval": 5,
    "status": "idle",
}
LOGIN_FLOW_LOCK = threading.RLock()

def update_login_flow(flow_id, **updates):
    """Apply poller state only if it still belongs to the current login flow."""
    with LOGIN_FLOW_LOCK:
        if flow_id != active_login_flow.get("flowId"):
            return False
        active_login_flow.update(updates)
        return True

def public_login_flow():
    """Return UI-safe device-login state, never the private device_code."""
    public_fields = (
        "flowId", "userCode", "verificationUrl", "expiresIn", "interval",
        "status", "browserLaunched", "browserLaunchError",
    )
    with LOGIN_FLOW_LOCK:
        return {key: active_login_flow[key] for key in public_fields if key in active_login_flow}

def _save_library_session_atomically(session, target: Path) -> None:
    """Persist an EbbLabs session using its serializer plus atomic activation."""
    temporary = _create_secure_session_temp(target, "login")
    try:
        session.save_session_to_file(temporary)
        if not temporary.is_file():
            raise RuntimeError("python-tidal did not save a valid session")
        with temporary.open("r+b") as session_file:
            session_file.flush()
            os.fsync(session_file.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def poll_device_login(session, link_login, flow_id=None):
    """Complete EbbLabs' device-login flow without hand-rolled OAuth requests."""
    if flow_id is None:
        with LOGIN_FLOW_LOCK:
            flow_id = active_login_flow.get("flowId")
    update_login_flow(flow_id, status="waiting_for_user")
    try:
        if not session.process_link_login(link_login):
            update_login_flow(flow_id, status="failed")
            return
        # Device authorization is non-PKCE. Preserve its separate OAuth file;
        # playback continues to require the dedicated PKCE session file. Guard
        # the final commit so an obsolete poller cannot replace a newer login.
        with LOGIN_FLOW_LOCK:
            if flow_id != active_login_flow.get("flowId"):
                return
            _save_library_session_atomically(session, SESSION_FILE)
            active_login_flow["status"] = "success"
    except TimeoutError:
        update_login_flow(flow_id, status="expired")
    except Exception as exc:
        print(f"[-] python-tidal device login failed: {type(exc).__name__}")
        update_login_flow(flow_id, status="failed")

class RequestHandler(http.server.BaseHTTPRequestHandler):
    def finish(self):
        try:
            super().finish()
        finally:
            candidate = getattr(self, "_candidate_stream_proxy", None)
            if candidate is not None and not getattr(self, "_candidate_stream_proxy_committed", False):
                candidate.close()

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_BROWSER_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def reject_untrusted_origin(self, require_origin=False):
        origin = self.headers.get("Origin")
        if (origin is None and not require_origin) or origin in ALLOWED_BROWSER_ORIGINS:
            return False
        self.send_json({"error": "origin is not allowed"}, status=403)
        return True

    def do_OPTIONS(self):
        if self.reject_untrusted_origin(require_origin=True):
            return
        requested_method = self.headers.get("Access-Control-Request-Method", "")
        requested_headers = {
            item.strip().lower()
            for item in self.headers.get("Access-Control-Request-Headers", "").split(",")
            if item.strip()
        }
        if requested_method not in {"GET", "POST"} or not requested_headers.issubset(
            {"content-type", "cache-control"}
        ):
            return self.send_json({"error": "preflight request is not allowed"}, status=403)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Cache-Control")
        self.send_header("Access-Control-Max-Age", "300")
        if self.headers.get("Access-Control-Request-Private-Network", "").lower() == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def send_json(self, data: dict, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.reject_untrusted_origin():
            return
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/health":
            self.send_json({
                "status": "ok",
                "service": "OmniFusion Standalone Audio Engine",
                "version": SERVICE_VERSION,
                "serviceInstanceId": SERVICE_INSTANCE_ID,
                "mpvInstanceId": (
                    engine.mpv_instance_id
                    if engine.proc and engine.proc.poll() is None and engine.pipe
                    else None
                ),
                "capabilities": ["prepared-playback-v1", "playback-clock-v1"],
            })

        elif parsed.path == "/devices":
            res = engine.cmd(["get_property", "audio-device-list"])
            devices = res.get("data", []) if res else []
            self.send_json({"devices": devices})

        elif parsed.path == "/config":
            try:
                self.send_json(load_settings())
            except OSError as exc:
                print(f"[-] Config read error: {exc}")
                self.send_json({"error": "configuration unavailable; original file preserved"}, status=500)

        elif parsed.path == "/login/status":
            self.send_json(public_login_flow())

        elif parsed.path == "/clock":
            # Keep the renderer's high-rate clock path read-only and cheap.
            # The full /status route also probes format, output and JOC route
            # diagnostics, so it remains on the slower control cadence.
            clock_error = None
            clock_error_status = 503
            clock_response = None
            # Transport commands and track replacement use this lock order.
            # Holding both across all reads prevents Pause/Resume/Seek from
            # producing a mixed position/pause snapshot.
            with engine.playback_lock:
                with engine.playback_generation_lock:
                    observed_meta = engine.current_meta
                    observed_identity = (
                        observed_meta.get("trackId"),
                        observed_meta.get("generation"),
                        observed_meta.get("requestId"),
                    )
                    observed_seek_sequence = engine.seek_sequence
                    sample_started_unix_ms = time.time_ns() / 1_000_000
                    sample_started_monotonic_ns = time.monotonic_ns()
                    pos_res = engine.clock_cmd(["get_property", "time-pos"])
                    sample_elapsed_ns = max(
                        0, time.monotonic_ns() - sample_started_monotonic_ns
                    )
                    pause_res = engine.clock_cmd(["get_property", "pause"])
                    seeking_res = engine.clock_cmd(["get_property", "seeking"])
                    current_identity = (
                        engine.current_meta.get("trackId"),
                        engine.current_meta.get("generation"),
                        engine.current_meta.get("requestId"),
                    )
                    mpv_instance_id = pos_res.get("_mpv_instance_id") if pos_res else None
                    raw_position = pos_res.get("data") if pos_res else None
                    pause_value = pause_res.get("data") if pause_res else None
                    seeking_value = seeking_res.get("data") if seeking_res else None
                    request_sequence = pos_res.get("request_id") if pos_res else None
                    property_reads_ok = all(
                        result and result.get("error") in (None, "success")
                        for result in (pos_res, pause_res, seeking_res)
                    )
                    same_mpv_instance = bool(mpv_instance_id) and all(
                        result.get("_mpv_instance_id") == mpv_instance_id
                        for result in (pos_res, pause_res, seeking_res)
                    )
                    valid_position = (
                        isinstance(raw_position, (int, float))
                        and not isinstance(raw_position, bool)
                        and math.isfinite(raw_position)
                        and raw_position >= 0
                    )
                    if not property_reads_ok or not same_mpv_instance:
                        clock_error = "MPV clock sample unavailable"
                    elif not valid_position or not isinstance(pause_value, bool) \
                            or not isinstance(seeking_value, bool):
                        clock_error = "MPV clock properties were malformed"
                    elif seeking_value:
                        clock_error = "MPV seek is still in progress"
                        clock_error_status = 409
                    elif current_identity != observed_identity \
                            or engine.seek_sequence != observed_seek_sequence:
                        clock_error = "clock sample was superseded"
                        clock_error_status = 409
                    elif not isinstance(request_sequence, int) or isinstance(request_sequence, bool):
                        clock_error = "MPV clock sample has no sequence"
                    else:
                        position_query_duration_ns = pos_res.get("_request_duration_ns")
                        position_query_duration_valid = (
                            isinstance(position_query_duration_ns, (int, float))
                            and not isinstance(position_query_duration_ns, bool)
                            and math.isfinite(position_query_duration_ns)
                            and position_query_duration_ns >= 0
                        )
                        position_query_started_unix_ms = pos_res.get(
                            "_request_started_unix_ms"
                        )
                        position_query_start_valid = (
                            isinstance(position_query_started_unix_ms, (int, float))
                            and not isinstance(position_query_started_unix_ms, bool)
                            and math.isfinite(position_query_started_unix_ms)
                            and position_query_started_unix_ms > 0
                        )
                        position_query_timing_valid = (
                            position_query_duration_valid and position_query_start_valid
                        )
                        clock_response = {
                            "serviceInstanceId": SERVICE_INSTANCE_ID,
                            "mpvInstanceId": mpv_instance_id,
                            "sampleSequence": request_sequence,
                            "trackId": observed_identity[0],
                            "playbackGeneration": observed_identity[1],
                            "requestId": observed_identity[2],
                            "timePos": raw_position,
                            "timePosSampledAtUnixMs": (
                                position_query_started_unix_ms
                                + position_query_duration_ns / 2_000_000
                                if position_query_timing_valid
                                else sample_started_unix_ms + sample_elapsed_ns / 2_000_000
                            ),
                            "timePosQueryDurationMs": (
                                position_query_duration_ns / 1_000_000
                                if position_query_timing_valid
                                else sample_elapsed_ns / 1_000_000
                            ),
                            "timePosSampleBracketHalfWidthMs": (
                                position_query_duration_ns / 2_000_000
                                if position_query_timing_valid
                                else sample_elapsed_ns / 2_000_000
                            ),
                            "timePosTimingSource": (
                                "ipc_request_bracket" if position_query_timing_valid
                                else "handler_bracket"
                            ),
                            "paused": pause_value,
                        }
            if clock_error:
                self.send_json({"error": clock_error}, status=clock_error_status)
                return
            self.send_json(clock_response)

        elif parsed.path == "/status":
            position_sample_started_unix_ms = time.time_ns() / 1_000_000
            position_sample_started_monotonic_ns = time.monotonic_ns()
            pos_res = engine.cmd(["get_property", "time-pos"])
            position_sample_elapsed_ns = max(
                0, time.monotonic_ns() - position_sample_started_monotonic_ns
            )
            time_pos_sampled_at_unix_ms = (
                position_sample_started_unix_ms + position_sample_elapsed_ns / 2_000_000
                if pos_res and pos_res.get("error") in (None, "success")
                else None
            )
            dur_res = engine.cmd(["get_property", "duration"])
            pause_res = engine.cmd(["get_property", "pause"])
            dev_res = engine.cmd(["get_property", "audio-device"])
            codec_res = engine.cmd(["get_property", "audio-codec-name"])
            params_res = engine.cmd(["get_property", "audio-params"])
            output_params_res = engine.cmd(["get_property", "audio-out-params"])
            bitrate_res = engine.cmd(["get_property", "audio-bitrate"])
            decoder_res = engine.cmd(["get_property", "current-tracks/audio/decoder"])
            position = pos_res.get("data") if pos_res else None
            paused = bool(pause_res.get("data", True)) if pause_res else True
            audio_params = params_res.get("data", {}) if params_res else {}
            path_res = engine.cmd(["get_property", "path"])
            active_path = str(path_res.get("data") or "") if path_res else ""
            expected_path = str(engine.current_meta.get("playTarget") or "")
            path_matches = mpv_path_matches(active_path, expected_path)
            playback_state = engine.update_playback_state(
                position, paused, audio_params, path_matches and bool(audio_params)
            )
            with engine.playback_generation_lock:
                observed_meta = engine.current_meta
                joc_snapshot = None
                observed_generation = observed_meta.get("generation")
                if (
                    observed_meta.get("format") == "EAC3_JOC"
                    and observed_meta.get("trackId") is not None
                    and observed_generation == engine.pending_generation
                    and observed_generation in {engine.active_generation, engine.prepared_generation}
                ):
                    joc_snapshot = {
                        "trackId": observed_meta.get("trackId"),
                        "generation": observed_meta.get("generation"),
                        "requestId": observed_meta.get("requestId"),
                        "pendingGeneration": engine.pending_generation,
                        "activeGeneration": engine.active_generation,
                        "preparedGeneration": engine.prepared_generation,
                    }

            if joc_snapshot:
                route_status = get_omniphony_route_status(engine.proc.pid if engine.proc else None)

                # Route probing can take long enough for /play to replace this
                # generation. Serialize the side effect and revalidate the full
                # observed identity so a stale JOC failure cannot pause or
                # invalidate the replacement track.
                with engine.playback_lock:
                    with engine.playback_generation_lock:
                        current_meta = engine.current_meta
                        same_observation = (
                            current_meta.get("format") == "EAC3_JOC"
                            and current_meta.get("trackId") == joc_snapshot["trackId"]
                            and current_meta.get("generation") == joc_snapshot["generation"]
                            and current_meta.get("requestId") == joc_snapshot["requestId"]
                            and engine.pending_generation == joc_snapshot["pendingGeneration"]
                            and engine.active_generation == joc_snapshot["activeGeneration"]
                            and engine.prepared_generation == joc_snapshot["preparedGeneration"]
                        )
                        if same_observation:
                            # Re-read the decoder after the route probe while
                            # replacement is excluded. A missing decoder must
                            # fail closed even though the shared predicate also
                            # supports route-only callers.
                            decoder_res = engine.cmd(["get_property", "current-tracks/audio/decoder"])
                            active_decoder = (
                                decoder_res.get("data")
                                if decoder_res and decoder_res.get("error") in (None, "success")
                                else None
                            )
                            decoder_confirmed = is_active_orender_decoder(active_decoder)
                            route_status.setdefault("checks", {})["activeDecoder"] = decoder_confirmed
                            route_status["ready"] = (
                                joc_route_is_verified(route_status, active_decoder)
                                and decoder_confirmed
                            )
                            current_meta["omniphonyRoute"] = route_status
                            if route_status["ready"]:
                                current_meta.pop("lastFailure", None)
                            else:
                                pause_result = engine.cmd(["set_property", "pause", True])
                                pause_succeeded = mpv_command_succeeded(pause_result)
                                if pause_succeeded:
                                    paused = True
                                playback_state = "failed"
                                current_meta["playbackState"] = playback_state
                                if pause_succeeded:
                                    current_meta.pop("lastFailure", None)
                                    current_meta["label"] = (
                                        "Omniphony decoder not active" if not decoder_confirmed
                                        else "Omniphony renderer disconnected"
                                    )
                                    engine.pending_generation += 1
                                    engine.pending_request_id = None
                                    engine.active_generation = 0
                                    engine.active_request_id = None
                                    engine.prepared_generation = 0
                                    engine.prepared_request_id = None
                                else:
                                    pause_error = (
                                        pause_result.get("error")
                                        if isinstance(pause_result, dict)
                                        else "no response"
                                    )
                                    current_meta["lastFailure"] = (
                                        "Omniphony route is unverified and mpv pause failed "
                                        f"({pause_error}); playback controls remain available for recovery"
                                    )
            
            track_status = public_track_status(engine.current_meta, playback_state)
            self.send_json({
                **track_status,
                "timePos": position if position is not None else 0.0,
                "timePosSampledAtUnixMs": time_pos_sampled_at_unix_ms,
                "timePosQueryDurationMs": (
                    position_sample_elapsed_ns / 1_000_000
                    if time_pos_sampled_at_unix_ms is not None else None
                ),
                "timePosSampleBracketHalfWidthMs": (
                    position_sample_elapsed_ns / 2_000_000
                    if time_pos_sampled_at_unix_ms is not None else None
                ),
                "timePosTimingSource": (
                    "handler_bracket" if time_pos_sampled_at_unix_ms is not None else None
                ),
                "timePosSampleSequence": pos_res.get("request_id") if pos_res else None,
                "serviceInstanceId": SERVICE_INSTANCE_ID,
                "mpvInstanceId": pos_res.get("_mpv_instance_id") if pos_res else None,
                "duration": dur_res.get("data", 0.0) if dur_res else 0.0,
                "paused": paused,
                "device": dev_res.get("data", "auto") if dev_res else "auto",
                "audioCodec": codec_res.get("data", "") if codec_res else "",
                "audioParams": audio_params,
                "audioOutputParams": output_params_res.get("data", {}) if output_params_res else {},
                "audioBitrate": bitrate_res.get("data", 0) if bitrate_res else 0,
                "lastSeekDiagnostic": engine.last_seek_diagnostic,
                "activeDecoder": decoder_res.get("data", "") if decoder_res else "",
                "streamTransport": (
                    "tidal-dash-proxy"
                    if track_status["trackId"] is not None and engine.stream_proxy is not None
                    else None
                ),
                "streamProxyRequests": (
                    engine.stream_proxy.segment_requests
                    if track_status["trackId"] is not None and engine.stream_proxy is not None
                    else 0
                ),
                "streamProxyBytes": (
                    engine.stream_proxy.bytes_served
                    if track_status["trackId"] is not None and engine.stream_proxy is not None
                    else 0
                ),
                "streamProxyFailures": (
                    engine.stream_proxy.failed_segments
                    if track_status["trackId"] is not None and engine.stream_proxy is not None
                    else 0
                ),
            })

        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self):
        if self.reject_untrusted_origin(require_origin=True):
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 65536:
                return self.send_json({"error": "request body too large or invalid"}, status=400)
            if length:
                self.connection.settimeout(5.0)
                raw_body = self.rfile.read(length)
                self.connection.settimeout(None)
                if len(raw_body) != length:
                    return self.send_json({"error": "incomplete request body"}, status=400)
            else:
                raw_body = b"{}"
            body = json.loads(raw_body.decode("utf-8"))
            if not isinstance(body, dict):
                return self.send_json({"error": "JSON body must be an object"}, status=400)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, socket.timeout, OSError) as exc:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
            return self.send_json({"error": f"invalid JSON request: {exc}"}, status=400)

        if parsed.path == "/config":
            if not isinstance(body, dict) or set(body) != {"priority"}:
                return self.send_json({"error": "expected an object containing only priority"}, status=400)
            priority = body["priority"]
            if (not isinstance(priority, list)
                    or len(priority) != len(SUPPORTED_FORMAT_PRIORITY)
                    or any(not isinstance(item, str) for item in priority)
                    or set(priority) != set(SUPPORTED_FORMAT_PRIORITY)):
                return self.send_json({
                    "error": "priority must contain each supported format exactly once",
                    "supported": list(SUPPORTED_FORMAT_PRIORITY),
                }, status=400)
            try:
                settings = update_settings({"priority": priority})
            except OSError as exc:
                return self.send_json({"error": f"could not save settings: {exc}"}, status=500)
            self.send_json({"status": "saved", "config": settings})

        elif parsed.path == "/device":
            dev = body.get("device")
            if dev:
                settings = update_settings({"audio_device": dev})
                engine.cmd(["set_property", "audio-device", dev])
                self.send_json({"status": "device_set", "device": dev})
            else:
                self.send_json({"error": "missing device"}, status=400)

        elif parsed.path == "/login/start":
            import tidalapi

            # Serialize initialization so two simultaneous UI clicks cannot
            # create pollers that race to persist different credentials.
            with LOGIN_FLOW_LOCK:
                if active_login_flow.get("status") == "waiting_for_user":
                    return self.send_json(public_login_flow(), status=409)
                flow_id = uuid.uuid4().hex
                try:
                    session = tidalapi.Session()
                    install_python_tidal_request_timeout(session)
                    link_login = session.get_link_login()
                except Exception as exc:
                    active_login_flow.update({
                        "flowId": flow_id, "deviceCode": None, "userCode": None,
                        "verificationUrl": None, "expiresIn": 0, "status": "failed",
                    })
                    return self.send_json({
                        "status": "failed", "error": "TIDAL authorization initialization failed",
                        "errorType": type(exc).__name__,
                    }, status=502)

                active_login_flow.update({
                    "flowId": flow_id,
                    "deviceCode": link_login.device_code,
                    "userCode": link_login.user_code,
                    "verificationUrl": link_login.verification_uri_complete,
                    "expiresIn": int(link_login.expires_in),
                    "interval": link_login.interval,
                    "status": "waiting_for_user",
                    "browserLaunched": False,
                })
                threading.Thread(
                    target=poll_device_login,
                    args=(session, link_login, flow_id),
                    daemon=True,
                ).start()

            try:
                subprocess.Popen(["cmd.exe", "/c", "start", "msedge", link_login.verification_uri_complete])
                update_login_flow(flow_id, browserLaunched=True)
            except OSError as exc:
                update_login_flow(flow_id, browserLaunchError=type(exc).__name__)
            self.send_json(public_login_flow())

        elif parsed.path == "/cancel-play":
            try:
                request_id = validate_play_request_id(body.get("requestId"))
                result = engine.cancel_play_request(request_id)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, status=400)
            except RuntimeError as exc:
                return self.send_json({"error": str(exc)}, status=502)
            self.send_json({"status": result, "requestId": request_id})

        elif parsed.path == "/play":
            track_id = body.get("trackId")
            time_pos = body.get("timePos", 0.0)
            title = body.get("title", "")
            artist = body.get("artist", "")
            start_paused = body.get("startPaused", False)
            request_id = body.get("requestId")

            if not track_id:
                return self.send_json({"error": "missing trackId"}, status=400)
            if not isinstance(start_paused, bool):
                return self.send_json({"error": "startPaused must be a boolean"}, status=400)
            if request_id is not None or start_paused:
                try:
                    request_id = validate_play_request_id(request_id)
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, status=400)
                if engine.is_play_request_cancelled(request_id):
                    return self.send_json({
                        "status": "cancelled", "requestId": request_id,
                    }, status=409)
            try:
                time_pos = float(time_pos)
            except (TypeError, ValueError, OverflowError):
                return self.send_json({"error": "timePos must be a finite nonnegative number"}, status=400)
            if not math.isfinite(time_pos) or time_pos < 0:
                return self.send_json({"error": "timePos must be a finite nonnegative number"}, status=400)

            try:
                generation = engine.begin_playback(request_id)
            except ValueError:
                return self.send_json({
                    "status": "cancelled", "requestId": request_id,
                }, status=409)
            is_current_play_request = lambda: engine.is_current_playback(generation, request_id)

            settings = load_settings()
            priority = settings.get("priority", DEFAULT_SETTINGS["priority"])
            device = settings.get("audio_device", "auto")

            try:
                stream_info = resolve_with_priority(int(track_id), priority)
            except Ac4TransportUnavailable as exc:
                return self.send_json({
                    "error": str(exc),
                    "code": exc.code,
                    "format": "AC4_IMS",
                    "transport": exc.transport,
                    "segmentCount": exc.segment_count,
                    "authProfile": exc.auth_profile,
                    "sourceCodec": exc.source_codec,
                    "requestedQuality": exc.requested_quality,
                    "fallbackAttempted": False,
                }, status=422)
            if not isinstance(stream_info, dict) or not stream_info.get("uri") or not stream_info.get("format"):
                return self.send_json({"error": "no supported TIDAL stream resolved for this track"}, status=502)
            if not is_current_play_request():
                return self.send_json({"status": "superseded", "trackId": track_id}, status=409)

            uri = stream_info["uri"]
            fmt = stream_info["format"]
            play_target = uri
            candidate_proxy = None
            route_status = None

            if fmt in {"HI_RES_LOSSLESS", "LOSSLESS"} or (fmt == "EAC3_JOC" and stream_info.get("transport") == "DASH"):
                try:
                    candidate_proxy = TidalDashProxy(
                        stream_info.get("manifestXml"),
                        stream_info.get("segmentUrls"),
                        stream_info.get("requestSession"),
                    )
                    play_target = candidate_proxy.start()
                    self._candidate_stream_proxy = candidate_proxy
                except Exception as exc:
                    if candidate_proxy is not None:
                        candidate_proxy.close()
                    return self.send_json({
                        "error": f"{fmt} DASH transport initialization failed: {type(exc).__name__}",
                    }, status=502)

            elif fmt == "AC4_IMS":
                # Download AC-4 stream and transcode to WAV via OxideAV ac4-to-wav
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                download_temp = CACHE_DIR / f".ac4_{track_id}_{uuid.uuid4().hex}.download"
                conversion_temp = None
                try:
                    print(f"[*] Downloading AC-4 IMS stream for {track_id}...")
                    dl_resp = requests.get(uri, timeout=30)
                    dl_resp.raise_for_status()
                    media_bytes = dl_resp.content
                    source_hash = hashlib.sha256(media_bytes).hexdigest()
                    converter_meta = AC4_CONVERTER.stat()
                    converter_tag = f"{converter_meta.st_size}:{converter_meta.st_mtime_ns}"
                    cache_key = hashlib.sha256(f"{source_hash}:{converter_tag}".encode()).hexdigest()[:20]
                    mp4_cache = CACHE_DIR / f"ac4_{track_id}_{source_hash[:20]}.mp4"
                    wav_cache = CACHE_DIR / f"ac4_{track_id}_{cache_key}.wav"
                    if not mp4_cache.exists():
                        download_temp.write_bytes(media_bytes)
                        os.replace(download_temp, mp4_cache)

                    source_metrics = parse_mp4_audio_metrics(mp4_cache.read_bytes())
                    source_channels = int(source_metrics.get("sourceChannels") or 0)
                    source_rate = int(source_metrics.get("sourceSampleRate") or 0)
                    source_mode = str(stream_info.get("sourceAudioMode") or "").upper()
                    probe_res = subprocess.run(
                        [str(AC4_CONVERTER), "--probe", str(mp4_cache)],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if probe_res.returncode != 0:
                        raise RuntimeError(
                            "OxideAV AC-4 presentation probe failed: "
                            f"{probe_res.stderr[-1000:]}"
                        )
                    probe_fields = parse_oxideav_ac4_probe(probe_res.stdout)
                    source_ims_atmos_verified = is_verified_oxideav_atmos_ims_probe(
                        probe_fields, source_rate
                    )
                    if (
                        source_mode != "DOLBY_ATMOS"
                        or source_channels != 2
                        or source_rate <= 0
                        or not source_ims_atmos_verified
                    ):
                        return self.send_json({
                            "error": "AC-4 stream is not verified by its MP4 DSI and bitstream TOC as two-channel Dolby Atmos IMS",
                            "format": "AC4_UNVERIFIED",
                            "sourceAudioMode": source_mode or "unknown",
                            "sourceChannels": source_channels,
                            "sourceSampleRate": source_rate,
                            "presentationProbe": probe_fields,
                            "trackId": track_id,
                        }, status=422)

                    if not wav_cache.exists():
                        conversion_temp = CACHE_DIR / f".{wav_cache.stem}_{uuid.uuid4().hex}.wav"
                        print(f"[*] Transcoding AC-4 IMS to binaural PCM via OxideAV...")
                        conv_res = subprocess.run(
                            [str(AC4_CONVERTER), str(mp4_cache), str(conversion_temp)],
                            capture_output=True,
                            text=True,
                            timeout=120
                        )
                        if conv_res.returncode != 0:
                            raise RuntimeError(f"OxideAV AC-4 conversion failed: {conv_res.stderr[-1200:]}")
                        decoded_metrics = validate_oxideav_wav(conversion_temp)
                        os.replace(conversion_temp, wav_cache)
                        conversion_temp = None
                    else:
                        # A prior interrupted or altered cache must not count as decoded AC-4.
                        decoded_metrics = validate_oxideav_wav(wav_cache)
                finally:
                    for temporary in (download_temp, conversion_temp):
                        if temporary is not None:
                            try:
                                temporary.unlink(missing_ok=True)
                            except OSError:
                                pass
                play_target = str(wav_cache)
                stream_info.update(source_metrics)
                stream_info["decodedChannels"] = decoded_metrics["channels"]
                stream_info["decodedSampleRate"] = decoded_metrics["sampleRate"]
                stream_info["presentation"] = "IMS"
                stream_info["presentationVerified"] = (
                    source_mode == "DOLBY_ATMOS"
                    and source_ims_atmos_verified
                    and source_channels == 2
                    and decoded_metrics["channels"] == 2
                    and source_rate == decoded_metrics["sampleRate"]
                )
                if not mark_verified_ac4_ims(stream_info):
                    return self.send_json({
                        "error": "OxideAV output did not preserve verified AC-4 IMS stereo",
                        "format": "AC4_UNVERIFIED",
                        "sourceChannels": source_channels,
                        "decodedChannels": decoded_metrics["channels"],
                        "sourceSampleRate": source_rate,
                        "decodedSampleRate": decoded_metrics["sampleRate"],
                        "trackId": track_id,
                    }, status=422)
                if source_metrics.get("sourceBitrate"):
                    print(
                        f"[+] AC-4 source bitrate: "
                        f"{source_metrics['sourceBitrate'] / 1000:.0f} kb/s "
                        f"({source_metrics['sourceBitrateBasis']})"
                    )

            # A newer request may arrive while stream resolution or OxideAV
            # conversion is running. Only the newest generation may touch mpv.
            if not is_current_play_request():
                return self.send_json({"status": "superseded", "trackId": track_id}, status=409)

            with engine.playback_lock:
                with engine.playback_generation_lock:
                    if not is_current_play_request():
                        return self.send_json({"status": "superseded", "trackId": track_id}, status=409)
                    engine.ensure_mpv(device=device)
                    try:
                        prepare_mpv_audio_device(device, engine.cmd)
                    except RuntimeError as exc:
                        engine.current_meta = {
                            **engine.current_meta,
                            "trackId": None,
                            "playbackState": "failed",
                            "label": "Required audio device unavailable",
                        }
                        engine.active_generation = 0
                        return self.send_json({
                            "error": f"{fmt} playback blocked before load: {exc}; no output-device fallback was attempted"
                        }, status=502)
                    decoder = STRICT_RENDER_DECODER if fmt == "EAC3_JOC" else ""
                    cmd_result = engine.cmd(["set_property", "ad", decoder])
                    if not mpv_command_succeeded(cmd_result):
                        raise RuntimeError(f"Could not configure decoder for {fmt}: {cmd_result['error']}")
                    try:
                        prepare_mpv_output_channel_mode(fmt, engine.cmd)
                    except RuntimeError as exc:
                        engine.current_meta = {
                            **engine.current_meta,
                            "trackId": None,
                            "playbackState": "failed",
                            "label": "Required channel mode unavailable",
                        }
                        engine.active_generation = 0
                        return self.send_json({
                            "error": f"{fmt} playback blocked before load: {exc}; no channel fallback was attempted"
                        }, status=502)
                    try:
                        prepare_mpv_demuxer_format(
                            fmt, engine.cmd, stream_info.get("transport")
                        )
                    except RuntimeError as exc:
                        engine.current_meta = {
                            **engine.current_meta,
                            "trackId": None,
                            "playbackState": "failed",
                            "label": "Required demuxer unavailable",
                        }
                        engine.active_generation = 0
                        return self.send_json({
                            "error": f"{fmt} playback blocked before load: {exc}; no demuxer fallback was attempted"
                        }, status=502)
                    pause_result = engine.cmd(["set_property", "pause", True])
                    if not mpv_command_succeeded(pause_result):
                        raise RuntimeError(f"Could not hold mpv during stream load: {pause_result.get('error')}")
                    file_loaded_before = engine.get_file_loaded_serial()
                    load_event_before = engine.get_mpv_load_event_serial()
                    load_result = engine.cmd(["loadfile", play_target, "replace"])
                    if not mpv_command_succeeded(load_result):
                        raise RuntimeError(f"mpv rejected the selected {fmt} stream: {load_result['error']}")
                    load_data = load_result.get("data")
                    playlist_entry_id = (
                        load_data.get("playlist_entry_id")
                        if isinstance(load_data, dict)
                        else None
                    )
                    # loadfile replaces the old stream immediately. Do not let
                    # its TIDAL generation continue issuing controls while the
                    # replacement is loading, seeking, or being superseded.
                    engine.active_generation = 0
                    engine.prepared_generation = 0
                    engine.prepared_request_id = None
                    engine.active_request_id = None
                    engine.current_meta = {
                        **engine.current_meta,
                        "playbackState": "loading",
                    }
                if not engine.wait_for_loaded_target(
                    play_target,
                    file_loaded_before,
                    load_event_after=load_event_before,
                    playlist_entry_id=playlist_entry_id,
                ):
                    load_diagnostics = engine.get_loaded_target_diagnostics(
                        play_target, file_loaded_before, load_event_before
                    )
                    if candidate_proxy is not None:
                        with candidate_proxy._counter_lock:
                            load_diagnostics["dashProxy"] = {
                                "segmentRequests": candidate_proxy.segment_requests,
                                "bytesServed": candidate_proxy.bytes_served,
                                "failedSegments": candidate_proxy.failed_segments,
                            }
                    with engine.playback_generation_lock:
                        if is_current_play_request():
                            engine.current_meta = {**engine.current_meta, "trackId": None, "playbackState": "failed", "label": "Load failed"}
                            engine.active_generation = 0
                    return self.send_json({
                        "error": "mpv did not load the requested media path",
                        "diagnostics": load_diagnostics,
                    }, status=502)
                # MPV confirmed the replacement; the prior file can no longer
                # request its segments, so release that proxy now.
                previous_proxy = engine.stream_proxy
                engine.stream_proxy = None
                if previous_proxy is not None:
                    previous_proxy.close()
                decoded_params_result = engine.cmd(["get_property", "audio-params"])
                decoded_params = (
                    decoded_params_result.get("data")
                    if decoded_params_result
                    and decoded_params_result.get("error") in (None, "success")
                    else None
                )
                if not isinstance(decoded_params, dict) or not decoded_params.get("samplerate") or not decoded_params.get("channel-count"):
                    # Some MPV builds defer decoder parameters while a file is
                    # held paused. Initialize briefly at zero volume, then pause
                    # and restore the previous volume before output negotiation.
                    decoded_params = engine.initialize_audio_params_muted()
                if not isinstance(decoded_params, dict) or not decoded_params.get("samplerate") or not decoded_params.get("channel-count"):
                    load_diagnostics = engine.get_loaded_target_diagnostics(play_target, file_loaded_before)
                    load_diagnostics["mutedAudioPreflightAttempted"] = True
                    with engine.playback_generation_lock:
                        if is_current_play_request():
                            engine.current_meta = {**engine.current_meta, "trackId": None, "playbackState": "failed", "label": "Audio initialization failed"}
                            engine.active_generation = 0
                    return self.send_json({
                        "error": "mpv loaded the requested file but did not initialize decoded audio parameters after muted preflight",
                        "diagnostics": load_diagnostics,
                    }, status=502)
                try:
                    def profile_command(command):
                        with engine.playback_generation_lock:
                            if not is_current_play_request():
                                raise RuntimeError("playback request was superseded")
                            return engine.cmd(command)

                    audio_profile = configure_mpv_output_profile(
                        engine,
                        fmt,
                        decoded_params,
                        command=profile_command,
                        channel_mode_preconfigured=True,
                        expected_device=device,
                    )
                except RuntimeError as exc:
                    with engine.playback_generation_lock:
                        if is_current_play_request():
                            engine.current_meta = {
                                **engine.current_meta,
                                "trackId": None,
                                "playbackState": "failed",
                                "label": "Required audio output profile unavailable",
                            }
                            engine.active_generation = 0
                    return self.send_json({
                        "error": f"{fmt} playback blocked: {exc}; no output-profile fallback was attempted"
                    }, status=502)
                if fmt == "EAC3_JOC":
                    route_status = wait_for_verified_joc_route(engine, timeout=12.0)
                    if not route_status.get("ready"):
                        with engine.playback_generation_lock:
                            if is_current_play_request():
                                engine.current_meta = {
                                    **engine.current_meta,
                                    "trackId": None,
                                    "playbackState": "failed",
                                    "label": "Omniphony route not confirmed",
                                    "omniphonyRoute": route_status,
                                }
                                engine.active_generation = 0
                        return self.send_json({
                            "error": "E-AC-3 JOC blocked: the stream remains paused until the selected orender decoder and Studio route are both confirmed; no fallback or playback start was attempted",
                            "omniphonyRoute": route_status,
                        }, status=503)
                with engine.playback_generation_lock:
                    if not is_current_play_request():
                        return self.send_json({"status": "superseded", "trackId": track_id}, status=409)
                    if time_pos > 0:
                        seek_result = engine.cmd(["seek", time_pos, "absolute"])
                        if not mpv_command_succeeded(seek_result):
                            engine.current_meta = {**engine.current_meta, "trackId": None, "playbackState": "failed", "label": "Seek failed"}
                            engine.active_generation = 0
                            return self.send_json({"error": "mpv rejected the requested start position"}, status=502)
                if time_pos > 0 and not engine.wait_for_position(time_pos):
                    with engine.playback_generation_lock:
                        if is_current_play_request():
                            engine.current_meta = {**engine.current_meta, "trackId": None, "playbackState": "failed", "label": "Seek failed"}
                            engine.active_generation = 0
                    return self.send_json({"error": "mpv did not reach the requested start position"}, status=502)
                with engine.playback_generation_lock:
                    if not is_current_play_request():
                        return self.send_json({"status": "superseded", "trackId": track_id}, status=409)
                    if fmt == "EAC3_JOC":
                        route_status = get_omniphony_route_status(engine.proc.pid if engine.proc else None)
                        active_decoder_result = engine.cmd(["get_property", "current-tracks/audio/decoder"])
                        active_decoder = active_decoder_result.get("data") if active_decoder_result else None
                        decoder_confirmed = is_active_orender_decoder(active_decoder)
                        route_status.setdefault("checks", {})["activeDecoder"] = decoder_confirmed
                        route_status["ready"] = joc_route_is_verified(route_status, active_decoder)
                        if not route_status.get("ready"):
                            engine.current_meta = {
                                **engine.current_meta,
                                "trackId": None,
                                "playbackState": "failed",
                                "label": (
                                    "Omniphony decoder not active" if not decoder_confirmed
                                    else "Omniphony renderer disconnected"
                                ),
                                "omniphonyRoute": route_status,
                            }
                            engine.active_generation = 0
                            return self.send_json({
                                "error": "E-AC-3 JOC blocked: Studio route and selected orender decoder must both remain connected before playback start",
                                "omniphonyRoute": route_status,
                            }, status=503)
                    engine.current_meta = {
                        "trackId": track_id,
                        "generation": generation,
                        "playbackState": "prepared" if start_paused else "loading",
                        "playTarget": play_target,
                        "format": fmt,
                        "codec": stream_info["codec"],
                        "label": stream_info["label"],
                        "omniphonyRoute": route_status if fmt == "EAC3_JOC" else None,
                        "decoder": "orender" if fmt == "EAC3_JOC" else None,
                        "sourceBitrate": stream_info.get("sourceBitrate"),
                        "sourceSampleRate": stream_info.get("sourceSampleRate"),
                        "sourceChannels": stream_info.get("sourceChannels"),
                        "sourceBitDepth": stream_info.get("sourceBitDepth"),
                        "sourceCodec": stream_info.get("sourceCodec"),
                        "sourceBitrateBasis": stream_info.get("sourceBitrateBasis"),
                        "sourceAuthProfile": stream_info.get("sourceAuthProfile"),
                        "requestedQuality": stream_info.get("requestedQuality"),
                        "sourceQuality": stream_info.get("sourceQuality"),
                        "sourceAudioMode": stream_info.get("sourceAudioMode"),
                        "presentation": stream_info.get("presentation"),
                        "presentationVerified": stream_info.get("presentationVerified", False),
                        "decodedSampleRate": stream_info.get("decodedSampleRate"),
                        "decodedChannels": stream_info.get("decodedChannels"),
                        "audioProfile": audio_profile,
                        "title": title,
                        "artist": artist,
                        "requestId": request_id,
                    }
                    engine.active_generation = 0 if start_paused else generation
                    engine.prepared_generation = generation if start_paused else 0
                    engine.prepared_request_id = request_id if start_paused else None
                    engine.active_request_id = None if start_paused else request_id
                    with engine.status_lock:
                        engine.playback_started_at = time.monotonic()
                        engine.last_status_position = None
                        engine.last_progress_at = engine.playback_started_at
                        engine.has_playback_progress = False
                    if not start_paused:
                        resume_result = engine.cmd(["set_property", "pause", False])
                        if not mpv_command_succeeded(resume_result):
                            engine.current_meta = {**engine.current_meta, "trackId": None, "playbackState": "failed", "label": "Resume failed"}
                            engine.active_generation = 0
                            return self.send_json({"error": "mpv rejected the playback start command"}, status=502)
                    engine.stream_proxy = candidate_proxy
                    if candidate_proxy is not None:
                        self._candidate_stream_proxy_committed = True

            print(f"[*] Playing Track {track_id} [{fmt}]")

            self.send_json({
                "status": "loaded",
                "playbackState": "prepared" if start_paused else "loaded",
                "trackId": track_id,
                "format": fmt,
                "label": stream_info["label"],
                "codec": stream_info["codec"],
                "generation": generation,
                "requestId": request_id,
            })

        elif parsed.path == "/pause":
            with engine.playback_lock:
                with engine.playback_generation_lock:
                    if not engine.control_matches(
                        body.get("trackId"), body.get("playbackGeneration"), body.get("requestId")
                    ):
                        return self.send_json({"error": "stale track command"}, status=409)
                    result = engine.cmd(["set_property", "pause", True])
                    if not mpv_command_succeeded(result):
                        return self.send_json({"error": f"mpv pause failed: {result.get('error') if result else 'no response'}"}, status=502)
            self.send_json({"status": "paused"})

        elif parsed.path == "/resume":
            with engine.playback_lock:
                with engine.playback_generation_lock:
                    track_id = body.get("trackId")
                    try:
                        generation = int(body.get("playbackGeneration"))
                    except (TypeError, ValueError, OverflowError):
                        generation = 0
                    prepared = (
                        str(track_id) == str(engine.current_meta.get("trackId"))
                        and generation > 0
                        and generation == engine.prepared_generation
                        and generation == engine.pending_generation
                        and body.get("requestId") == engine.prepared_request_id
                        and engine.prepared_request_id not in engine.cancelled_play_request_ids
                        and engine.current_meta.get("playbackState") == "prepared"
                    )
                    active = engine.control_matches(track_id, generation, body.get("requestId"))
                    if not (prepared or active):
                        return self.send_json({"error": "stale track command"}, status=409)
                    if engine.current_meta.get("format") == "EAC3_JOC":
                        route_status = get_omniphony_route_status(engine.proc.pid if engine.proc else None)
                        decoder_result = engine.cmd(["get_property", "current-tracks/audio/decoder"])
                        active_decoder = decoder_result.get("data") if decoder_result else None
                        route_status.setdefault("checks", {})["activeDecoder"] = is_active_orender_decoder(active_decoder)
                        route_status["ready"] = joc_route_is_verified(route_status, active_decoder)
                        if not route_status.get("ready"):
                            return self.send_json({
                                "error": "E-AC-3 JOC resume blocked: Omniphony route or selected orender decoder is not verified",
                                "omniphonyRoute": route_status,
                            }, status=503)
                        engine.current_meta["omniphonyRoute"] = route_status
                    eof_deadline = time.monotonic() + EOF_REWIND_TIMEOUT_SECONDS
                    eof_remaining = eof_deadline - time.monotonic()
                    if eof_remaining <= 0:
                        return self.send_json({
                            "error": "mpv EOF verification deadline expired"
                        }, status=502)
                    eof_result = engine.cmd(
                        ["get_property", "eof-reached"],
                        timeout=eof_remaining,
                    )
                    if (
                        not mpv_command_succeeded(eof_result)
                        or not isinstance(eof_result.get("data"), bool)
                    ):
                        return self.send_json({
                            "error": "mpv EOF state could not be verified"
                        }, status=502)
                    restarted_from_end = eof_result["data"]
                    if restarted_from_end:
                        remaining = eof_deadline - time.monotonic()
                        if remaining <= 0:
                            return self.send_json({
                                "error": "mpv EOF rewind deadline expired; playback remains paused"
                            }, status=502)
                        rewind_result = engine.cmd(
                            ["seek", 0, "absolute+exact"], timeout=remaining
                        )
                        if not mpv_command_succeeded(rewind_result):
                            return self.send_json({
                                "error": f"mpv rewind from end failed: {rewind_result.get('error') if rewind_result else 'no response'}"
                            }, status=502)
                        remaining = eof_deadline - time.monotonic()
                        if remaining <= 0 or not engine.wait_for_rewind_from_eof(timeout=remaining):
                            return self.send_json({
                                "error": "mpv rewind from end did not settle; playback remains paused"
                            }, status=502)
                    remaining = eof_deadline - time.monotonic()
                    if remaining <= 0:
                        return self.send_json({
                            "error": "mpv resume deadline expired; playback remains paused"
                        }, status=502)
                    result = engine.cmd(
                        ["set_property", "pause", False], timeout=remaining
                    )
                    if not mpv_command_succeeded(result):
                        return self.send_json({"error": f"mpv resume failed: {result.get('error') if result else 'no response'}"}, status=502)
                    if prepared:
                        engine.active_generation = generation
                        engine.prepared_generation = 0
                        engine.prepared_request_id = None
                        engine.active_request_id = engine.current_meta.get("requestId")
                        engine.current_meta["playbackState"] = "loading"
            self.send_json({"status": "resumed", "restartedFromEnd": restarted_from_end})

        elif parsed.path == "/seek":
            pos = body.get("timePos", 0.0)
            if isinstance(pos, bool) or not isinstance(pos, (int, float)) \
                    or not math.isfinite(float(pos)) or float(pos) < 0:
                return self.send_json({"error": "timePos must be a finite nonnegative number"}, status=400)
            pos = float(pos)
            expected_paused = body.get("expectedPaused")
            if expected_paused is not None and not isinstance(expected_paused, bool):
                return self.send_json({"error": "expectedPaused must be a boolean when supplied"}, status=400)
            dispatch_error = None
            dispatch_status = 502
            seek_sequence = None
            seek_samples = []
            proxy_requests_before = None
            proxy_indices_before = None
            with engine.playback_lock:
                with engine.playback_generation_lock:
                    track_id = body.get("trackId")
                    generation = body.get("playbackGeneration")
                    request_id = body.get("requestId")
                    pause_result = engine.cmd(["get_property", "pause"])
                    pause_value = pause_result.get("data") if isinstance(pause_result, dict) else None
                    if not mpv_command_succeeded(pause_result) or not isinstance(pause_value, bool):
                        dispatch_error = "could not verify MPV pause state before seek"
                    else:
                        observed_paused = pause_value
                        if expected_paused is not None and observed_paused is not expected_paused:
                            dispatch_error = "MPV pause state changed before seek"
                            dispatch_status = 409
                        else:
                            expected_paused = observed_paused
                            if not engine.control_matches(
                                track_id, generation, request_id, allow_prepared=True
                            ):
                                dispatch_error = "stale track command"
                                dispatch_status = 409
                            else:
                                pre_seek_position_result = engine.cmd(["get_property", "time-pos"])
                                pre_seek_state_result = engine.cmd(["get_property", "seeking"])
                                proxy = getattr(engine, "stream_proxy", None)
                                if proxy is not None:
                                    with proxy._counter_lock:
                                        proxy_requests_before = proxy.segment_requests
                                        proxy_indices_before = list(proxy.recent_segment_indices)
                                result = engine.cmd(["seek", pos, USER_SEEK_MODE])
                                if not mpv_command_succeeded(result):
                                    detail = result.get("error") if result else "no response"
                                    dispatch_error = f"mpv seek failed: {detail}"
                                else:
                                    # A failed MPV dispatch must not supersede a
                                    # seek that is already settling successfully.
                                    seek_sequence = engine.begin_seek(
                                        track_id, generation, request_id
                                    )
                                    if seek_sequence is None:
                                        dispatch_error = "stale track command"
                                        dispatch_status = 409
            if dispatch_error is not None:
                return self.send_json({"error": dispatch_error}, status=dispatch_status)
            settled, observed_position, settle_error = engine.wait_for_seek_position(
                pos, seek_sequence, track_id, generation, request_id,
                expected_paused=expected_paused,
                diagnostic_samples=seek_samples,
            )
            if settle_error == "superseded":
                return self.send_json({
                    "error": "seek was superseded by a newer command or playback identity",
                    "timePos": observed_position,
                }, status=409)
            if settle_error == "ipc-error":
                return self.send_json({
                    "error": "could not confirm MPV seek position over IPC",
                    "observedTimePos": observed_position,
                    "seekDiagnostic": {
                        "targetTimePos": pos,
                        "settlementError": settle_error,
                        "settlementSamples": seek_samples,
                    },
                }, status=502)
            if settle_error == "pause-changed":
                return self.send_json({
                    "error": "MPV pause state changed while the seek was settling",
                    "observedTimePos": observed_position,
                    "seekDiagnostic": {
                        "targetTimePos": pos,
                        "settlementError": settle_error,
                        "settlementSamples": seek_samples,
                    },
                }, status=409)
            if not settled:
                if not engine.seek_is_current(seek_sequence, track_id, generation, request_id):
                    return self.send_json({
                        "error": "seek was superseded before timeout diagnostics",
                        "timePos": observed_position,
                    }, status=409)
                # Capture fresh native libmpv properties after timeout. A 502
                # means the position gate failed; these fields help distinguish
                # a delayed demuxer seek from a stale or inconsistent snapshot.
                post_seek_position_result = engine.cmd(["get_property", "time-pos"])
                post_seek_state_result = engine.cmd(["get_property", "seeking"])
                if not engine.seek_is_current(seek_sequence, track_id, generation, request_id):
                    return self.send_json({
                        "error": "seek was superseded during timeout diagnostics",
                        "timePos": observed_position,
                    }, status=409)
                seek_diagnostic = {
                    "targetTimePos": pos,
                    "seekMode": USER_SEEK_MODE,
                    "proxyRequestsBefore": proxy_requests_before,
                    "proxyRequestsAfter": None,
                    "settlementError": settle_error,
                    "seekSequence": seek_sequence,
                    "settlementSamples": seek_samples,
                    "preSeekTimePos": (
                        pre_seek_position_result.get("data")
                        if mpv_command_succeeded(pre_seek_position_result) else None
                    ),
                    "preSeekInProgress": (
                        pre_seek_state_result.get("data")
                        if mpv_command_succeeded(pre_seek_state_result) else None
                    ),
                    "settlementTimePos": observed_position,
                    "postSeekTimePos": (
                        post_seek_position_result.get("data")
                        if mpv_command_succeeded(post_seek_position_result) else None
                    ),
                    "postSeekInProgress": (
                        post_seek_state_result.get("data")
                        if mpv_command_succeeded(post_seek_state_result) else None
                    ),
                    "paused": expected_paused,
                }
                proxy = getattr(engine, "stream_proxy", None)
                if proxy is not None:
                    with proxy._counter_lock:
                        seek_diagnostic["proxyRequestsAfter"] = proxy.segment_requests
                        seek_diagnostic["proxyRecentIndexesBefore"] = proxy_indices_before
                        seek_diagnostic["proxyRecentIndexesAfter"] = list(proxy.recent_segment_indices)
                # Retain only the bounded, sanitized native settlement trace
                # so the local TIDAL status poll can expose why a scrub failed.
                engine.last_seek_diagnostic = seek_diagnostic
                return self.send_json({
                    "error": "mpv did not settle at the requested seek position",
                    "timePos": pos,
                    "observedTimePos": observed_position,
                    "seekDiagnostic": seek_diagnostic,
                }, status=502)
            acknowledgement_error = None
            acknowledgement_status = 502
            with engine.playback_generation_lock:
                if not engine.seek_is_current(seek_sequence, track_id, generation, request_id):
                    acknowledgement_error = {
                        "error": "seek was superseded before acknowledgement",
                        "timePos": observed_position,
                    }
                    acknowledgement_status = 409
                else:
                    final_pause_result = engine.cmd(["get_property", "pause"])
                    final_pause_value = final_pause_result.get("data") if isinstance(final_pause_result, dict) else None
                    if not mpv_command_succeeded(final_pause_result) or not isinstance(final_pause_value, bool):
                        acknowledgement_error = {
                            "error": "could not verify MPV pause state at seek acknowledgement"
                        }
                    elif final_pause_value is not expected_paused:
                        acknowledgement_error = {
                            "error": "MPV pause state changed before seek acknowledgement",
                            "observedTimePos": observed_position,
                        }
                        acknowledgement_status = 409
                    else:
                        response_body = {"status": "seeked", "timePos": pos, "paused": expected_paused}
            if acknowledgement_error is not None:
                return self.send_json(acknowledgement_error, status=acknowledgement_status)
            self.send_json(response_body)

        elif parsed.path == "/volume":
            vol = body.get("volume")
            if isinstance(vol, bool) or not isinstance(vol, (int, float)) \
                    or not math.isfinite(float(vol)) or not 0 <= float(vol) <= 100:
                return self.send_json({"error": "volume must be a finite number from 0 to 100"}, status=400)
            with engine.playback_lock:
                if not engine.control_matches(
                    body.get("trackId"), body.get("playbackGeneration"), body.get("requestId")
                ):
                    return self.send_json({"error": "stale track command"}, status=409)
                result = engine.cmd(["set_property", "volume", vol])
                if not mpv_command_succeeded(result):
                    return self.send_json({"error": f"mpv volume change failed: {result.get('error') if result else 'no response'}"}, status=502)
            self.send_json({"status": "volume_set", "volume": vol})

        else:
            self.send_json({"error": "not found"}, status=404)

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def run():
    settings = load_settings()
    save_settings(settings)
    threading.Thread(target=engine.ensure_mpv, kwargs={"device": settings.get("audio_device", "auto")}, daemon=True).start()

    server = ThreadedHTTPServer(("127.0.0.1", PORT), RequestHandler)
    print(f"[+] OmniFusion Standalone Audio Service listening on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if engine.stream_proxy is not None:
            engine.stream_proxy.close()
            engine.stream_proxy = None
        if engine.pipe:
            engine.pipe.close()
        if engine.proc:
            engine.proc.terminate()

if __name__ == "__main__":
    run()




