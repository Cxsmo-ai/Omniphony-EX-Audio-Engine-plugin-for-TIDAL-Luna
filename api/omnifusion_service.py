"""
omnifusion_service.py - Ultra-Reliable Standalone Audio Engine & TIDAL RealMax API
Manages headless libmpv, zero-leak multi-profile stream resolving, and native device selection.
"""

from __future__ import annotations

import base64
import binascii
import copy
import ctypes
import ctypes.wintypes
import datetime
import email.utils
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import math
import msvcrt
import os
import queue
import re
import socket
import shutil
import socketserver
import ssl
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
from eclipse_streams import (
    MAX_STREAM_VARIANTS,
    _bounded_integer,
    flatten_stream_payload,
    declared_addon_capabilities,
    _normalize_stream,
    normalize_stream,
    quality_candidate_limit_exceeded,
    quality_candidates,
    quality_options_unsafe,
    quality_selector_label_map,
    build_stream_request_plan,
    _renderer_duration,
    _renderer_text,
    stream_payload_has_malformed_items,
    stream_payload_truncated,
    stream_has_protection_indicators,
    unclassified_select_settings,
)

MAX_ECLIPSE_STREAM_REQUESTS = 4096
MAX_ECLIPSE_MEDIA_MANIFESTS_PER_INVENTORY = 64


def configured_path(environment_name: str, fallback: Path) -> Path:
    value = os.environ.get(environment_name, "").strip()
    return Path(value).expanduser().resolve() if value else fallback.resolve()


PLUGIN_DIR = configured_path("OMNIFUSION_PLUGIN_DIR", SERVICE_DIR.parent)
# Keep the old name for internal call sites while making its meaning explicit:
# this is immutable plugin code, never a writable user-data directory.
STANDALONE_DIR = PLUGIN_DIR
DATA_DIR = configured_path("OMNIFUSION_DATA_DIR", PLUGIN_DIR / "data")
MPV_DIR = configured_path("OMNIFUSION_MPV_DIR", PLUGIN_DIR / "external" / "mpv")
OXIDE_DIR = configured_path("OMNIFUSION_OXIDE_DIR", SERVICE_DIR / "oxideav")
CACHE_DIR = DATA_DIR / "cache"
DOWNLOAD_DIR = DATA_DIR / "downloads"
AC4_PLAYBACK_CACHE_MAX_BYTES = 256 * 1024 * 1024
AC4_PLAYBACK_CACHE_MIN_AGE_SECONDS = 600
AC4_PARTIAL_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
AC4_CONVERTER = OXIDE_DIR / "adapter-v0.1.71" / "ac4-to-wav.exe"
STUDIO_EXE = configured_path(
    "OMNIFUSION_STUDIO_EXE",
    PLUGIN_DIR / "external" / "omniphony-studio-egui.exe",
)
RENDERER_EXE = STUDIO_EXE.parent / "orender.exe"
# Studio must register with the in-process mpv/orender OSC listener. Launching
# it with no target can make Studio connect to its own saved listener instead.
STUDIO_REGISTER_TARGET = "127.0.0.1:9000"
# This portable MPV build is compiled against the bundled Omniphony ABI.
# Do not let its runtime loader select the older Studio-wide copy first.
ORENDER_DLL = MPV_DIR / "orender.dll"
ORENDER_BRIDGE_DIR = configured_path(
    "OMNIFUSION_HARLETTY_DIR",
    SERVICE_DIR / "harletty" / "v0.9.0",
)
ORENDER_BRIDGE_PATHS = (
    ORENDER_BRIDGE_DIR / "harletty_dolby_bridge.dll",
    ORENDER_BRIDGE_DIR / "harletty_dts_bridge.dll",
    ORENDER_BRIDGE_DIR / "harletty_iamf_bridge.dll",
)
STRICT_RENDER_DECODER = "orender"
STREAM_TRANSPORT_TIDAL_DASH_PROXY = "tidal-dash-proxy"
STREAM_TRANSPORT_OXIDEAV_RAW_S16LE = "oxideav-raw-s16le"


def stream_transport_label(proxy) -> str | None:
    """Classify concrete proxy types without guessing from codec or field presence."""
    if proxy is None:
        return None
    if isinstance(proxy, Ac4RawPcmProxy):
        return STREAM_TRANSPORT_OXIDEAV_RAW_S16LE
    if isinstance(proxy, TidalDashProxy):
        return STREAM_TRANSPORT_TIDAL_DASH_PROXY
    return None


CONFIG_DIR = DATA_DIR / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
STUDIO_CONFIG_FILE = CONFIG_DIR / "omniphony" / "config.yaml"


def studio_process_environment() -> dict[str, str]:
    """Pass Studio the exact config directory whose route settings we verify."""
    environment = os.environ.copy()
    environment["OMNIPHONY_CONFIG_DIR"] = str(STUDIO_CONFIG_FILE.parent)
    return environment


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.wintypes.DWORD),
        ("SchedulingClass", ctypes.wintypes.DWORD),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsOwnedProcessJob:
    """Own a launched process tree so Studio children cannot outlive the service."""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows Job Objects are required for owned Studio startup")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel32.CreateJobObjectW.restype = ctypes.wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            ctypes.wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = ctypes.wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = ctypes.wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _JobExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        process_handle = ctypes.wintypes.HANDLE(int(process._handle))
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle = self._handle
        if not handle:
            return
        if not self._kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = None


def _resume_suspended_process_primary_thread(pid: int) -> None:
    """Resume the sole initial thread of a newly created suspended process."""
    if os.name != "nt":
        raise OSError("Windows thread control is required for owned Studio startup")
    threads = psutil.Process(pid).threads()
    if len(threads) != 1:
        raise OSError(f"expected one suspended primary thread, found {len(threads)}")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.argtypes = [
        ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD,
    ]
    kernel32.OpenThread.restype = ctypes.wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.ResumeThread.restype = ctypes.wintypes.DWORD
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    thread_handle = kernel32.OpenThread(0x0002, False, int(threads[0].id))
    if not thread_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        previous_suspend_count = kernel32.ResumeThread(thread_handle)
        if previous_suspend_count != 1:
            if previous_suspend_count == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            raise OSError(
                f"expected primary thread suspend count 1, got {previous_suspend_count}"
            )
    finally:
        kernel32.CloseHandle(thread_handle)


def launch_process_in_windows_job(
    args: list[str], *, cwd: str, env: dict[str, str],
) -> tuple[subprocess.Popen, WindowsOwnedProcessJob]:
    """Launch suspended, contain before execution, then resume the owned tree."""
    job = WindowsOwnedProcessJob()
    process = None
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            creationflags=0x00000004,  # CREATE_SUSPENDED
        )
        job.assign(process)
        _resume_suspended_process_primary_thread(process.pid)
        return process, job
    except Exception:
        try:
            job.close()
        except OSError as exc:
            print(f"[-] Could not close failed Studio job handle: {type(exc).__name__}")
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"[-] Could not reap failed Studio startup: {type(exc).__name__}")
        raise


SESSION_FILE = CONFIG_DIR / "tidal-session-oauth.json"
TIDAL_TV_SESSION_FILE = CONFIG_DIR / "tidal-session-tv.json"
PYTHON_TIDAL_SESSION_FILE = CONFIG_DIR / "tidal-session-pkce.json"

MPV_EXE = MPV_DIR / "mpv.exe"
PIPE_NAME = "omnifusion-standalone.mpv"
PIPE_PATH = f"//./pipe/{PIPE_NAME}".replace("/", "\\")
PORT = 8765
SERVICE_VERSION = "2.1.46"
SERVICE_INSTANCE_ID = uuid.uuid4().hex
RUNTIME_BUNDLE_SHA256 = os.environ.get("OMNIFUSION_RUNTIME_SHA256", "").strip().upper()
if not re.fullmatch(r"[A-F0-9]{64}", RUNTIME_BUNDLE_SHA256):
    RUNTIME_BUNDLE_SHA256 = ""
SHUTDOWN_TOKEN = os.environ.get("OMNIFUSION_SHUTDOWN_TOKEN", "")
if not re.fullmatch(r"[a-f0-9]{64}", SHUTDOWN_TOKEN):
    SHUTDOWN_TOKEN = ""
def _process_session_id(pid: int):
    if os.name != "nt":
        return None
    session_id = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(session_id)):
        return None
    return int(session_id.value)


def _verified_tidal_parent_pid():
    """Infer ownership only from a live, same-user/session TIDAL parent in its normal install path."""
    if os.name != "nt":
        return None
    try:
        child = psutil.Process(os.getpid())
        parent = child.parent()
        if parent is None or int(parent.pid) <= 0:
            return None
        parent_pid = int(parent.pid)
        parent_exe = Path(parent.exe()).resolve()
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            return None
        tidal_root = (Path(local_app_data) / "TIDAL").resolve()
        if (
            parent.name().casefold() != "tidal.exe"
            or parent_exe.name.casefold() != "tidal.exe"
            or parent_exe.parent.parent != tidal_root
            or not parent_exe.parent.name.casefold().startswith("app-")
            or parent.username().casefold() != child.username().casefold()
            or parent.create_time() >= child.create_time()
        ):
            return None
        parent_session = _process_session_id(parent_pid)
        child_session = _process_session_id(os.getpid())
        if parent_session is None or parent_session != child_session:
            return None
        return parent_pid
    except (psutil.Error, OSError, TypeError, ValueError):
        return None


_raw_tidal_owner_pid = os.environ.get("OMNIFUSION_TIDAL_PID")


def _resolve_tidal_owner(raw_value):
    if raw_value is None:
        return None, "unavailable"
    if isinstance(raw_value, str) and not raw_value.strip():
        owner_pid = _verified_tidal_parent_pid()
        return owner_pid, "verified_parent_fallback" if owner_pid is not None else "unavailable"
    try:
        owner_pid = int(raw_value)
    except (TypeError, ValueError):
        return None, "invalid_environment"
    return (owner_pid, "environment") if owner_pid > 0 else (None, "invalid_environment")


TIDAL_OWNER_PID, TIDAL_OWNER_SOURCE = _resolve_tidal_owner(_raw_tidal_owner_pid)
TIDAL_OWNER_CREATE_TIME = None
EOF_REWIND_TIMEOUT_SECONDS = 3.0


def prune_ac4_playback_cache(
    cache_dir: Path,
    protected_paths=(),
    max_bytes: int = AC4_PLAYBACK_CACHE_MAX_BYTES,
    min_age_seconds: float = AC4_PLAYBACK_CACHE_MIN_AGE_SECONDS,
    partial_max_age_seconds: float = AC4_PARTIAL_CACHE_MAX_AGE_SECONDS,
) -> dict:
    """Bound the service-owned compressed AC-4 playback cache without touching downloads."""
    if max_bytes < 0 or min_age_seconds < 0 or partial_max_age_seconds < 0:
        raise ValueError("AC-4 cache limits must be nonnegative")
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return {"removedFiles": 0, "removedBytes": 0, "remainingBytes": 0}

    now = time.time()
    protected = set()
    for item in protected_paths:
        if item:
            try:
                protected.add(Path(item).resolve())
            except (OSError, RuntimeError, TypeError):
                continue

    cached = []
    removed_files = 0
    removed_bytes = 0
    cache_pattern = re.compile(r"^ac4_[A-Za-z0-9_-]+_[0-9a-f]{20}\.mp4$", re.IGNORECASE)
    partial_pattern = re.compile(r"^\.ac4_[A-Za-z0-9_-]+_[0-9a-f]{32}\.download$", re.IGNORECASE)
    try:
        entries = list(cache_dir.iterdir())
    except OSError:
        return {"removedFiles": 0, "removedBytes": 0, "remainingBytes": 0}

    for path in entries:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            if partial_pattern.fullmatch(path.name):
                if now - stat.st_mtime >= partial_max_age_seconds:
                    path.unlink()
                    removed_files += 1
                    removed_bytes += stat.st_size
                continue
            if cache_pattern.fullmatch(path.name):
                cached.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            continue

    remaining_bytes = sum(size for _, size, _ in cached)
    for modified, size, path in sorted(cached):
        if remaining_bytes <= max_bytes:
            break
        if now - modified < min_age_seconds or path.resolve() in protected:
            continue
        try:
            path.unlink()
            remaining_bytes -= size
            removed_files += 1
            removed_bytes += size
        except OSError:
            continue

    return {
        "removedFiles": removed_files,
        "removedBytes": removed_bytes,
        "remainingBytes": remaining_bytes,
    }

DEFAULT_SETTINGS = {
    "priority": ["AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"],
    "audio_device": "auto",
    "volume": 100,
    "realmax_source": "both",
    "realmax_provider_order": None,
}
SUPPORTED_FORMAT_PRIORITY = ("AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS")
REALMAX_SOURCE_ALIASES = {
    "auto": "both",
    "both": "both",
    "tidal_only": "tidal_api",
    "tidal_api": "tidal_api",
    "eclipse_only": "eclipse_addons",
    "amazon_only": "eclipse_addons",
    "eclipse_addons": "eclipse_addons",
}
CANCELLED_PLAY_REQUEST_TTL = 600.0
MAX_CANCELLED_PLAY_REQUESTS = 512
SEEK_SETTLE_TIMEOUT_SECONDS = 2.0
SEEK_SETTLE_CONFIRMATION_SAMPLES = 3
SEEK_POSITION_TOLERANCE_SECONDS = 0.25
SEEK_SETTLE_STABILITY_SECONDS = 0.30
USER_SEEK_MODE = "absolute"


def validate_realmax_provider_order(value) -> list[str] | None:
    """Validate selected providers and their order without persisting addon URLs."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 81:
        raise ValueError("realmax_provider_order must be null or a list of at most 81 provider keys")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or not (
            item == "tidal_api" or re.fullmatch(r"eclipse:[0-9a-f]{20}", item)
        ):
            raise ValueError("provider keys must be tidal_api or eclipse:<20 lowercase hex characters>")
        if item in seen:
            raise ValueError("provider order cannot contain duplicate keys")
        seen.add(item)
        result.append(item)
    return result


def normalize_realmax_source(value) -> str:
    """Normalize earlier source-mode labels to the three current provider families."""
    return REALMAX_SOURCE_ALIASES.get(value, "both") if isinstance(value, str) else "both"


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
    "sourceProvider",
    "sourceName",
    "presentation",
    "decodedSampleRate",
    "decodedChannels",
    "requestId",
    "streamTransport",
)


def public_track_status(metadata: dict, playback_state: str) -> dict:
    """Expose stream metadata only while it belongs to a known active track."""
    track_id = metadata.get("trackId")
    state = str(playback_state or "idle").lower()
    last_failure = metadata.get("lastFailure")
    label = str(metadata.get("label") or "")
    route = metadata.get("omniphonyRoute")
    stale_joc_failures = {
        "Omniphony renderer disconnected",
        "Omniphony decoder not active",
    }
    verified_ready_joc = (
        track_id is not None
        and metadata.get("format") == "EAC3_JOC"
        and state in {"playing", "paused", "prepared"}
        and isinstance(route, dict)
        and route.get("ready") is True
    )
    if verified_ready_joc:
        if last_failure in stale_joc_failures:
            last_failure = None
        # Clear only exact stale JOC failure labels after the current route is
        # independently confirmed ready. Preserve every unrelated failure.
        if label in stale_joc_failures and not last_failure:
            label = "Dolby Atmos (E-AC-3 JOC)"
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
        "label": label,
        "title": metadata.get("title"),
        "artist": metadata.get("artist"),
        "presentationVerified": metadata.get("presentationVerified", False),
        "sourceProvider": metadata.get("sourceProvider", "TIDAL"),
        "album": metadata.get("album"),
        "cover": metadata.get("cover") or metadata.get("artworkURL"),
        "artworkURL": metadata.get("artworkURL") or metadata.get("cover"),
        "lastFailure": last_failure,
    })
    return fields


def mpv_loading_status_payload(engine) -> dict:
    """Build a /status response without querying MPV during demuxer startup.

    Some MPV properties synchronously enter libavformat and can remain blocked
    while a DASH manifest is opening.  A routine UI poll must never turn that
    temporary wait into an MPV restart.
    """
    with engine.playback_generation_lock:
        metadata = dict(engine.current_meta)
    settings = load_settings()
    return {
        **public_track_status(
            metadata,
            metadata.get("playbackState")
            if metadata.get("playbackState") in {"loading", "failed", "prepared"}
            else "loading",
        ),
        "timePos": 0.0,
        "timePosSampledAtUnixMs": None,
        "timePosQueryDurationMs": None,
        "timePosSampleBracketHalfWidthMs": None,
        "timePosTimingSource": None,
        "timePosSampleSequence": None,
        "serviceInstanceId": SERVICE_INSTANCE_ID,
        "mpvInstanceId": engine.mpv_instance_id,
        "duration": 0.0,
        "paused": True,
        "device": metadata.get("audioDevice") or settings.get("audio_device", "auto"),
        "audioCodec": "",
        "audioParams": {},
        "audioOutputParams": {},
        "audioBitrate": 0,
        "lastSeekDiagnostic": engine.last_seek_diagnostic,
        "activeDecoder": "",
        "streamTransport": None,
        "streamProxyRequests": 0,
        "streamProxyBytes": 0,
        "streamProxyFailures": 0,
    }
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


def snapshot_seek_proxy_counters(proxy):
    """Read shared seek counters for DASH and raw-PCM proxy types."""
    if proxy is None:
        return None, None
    counter_lock = getattr(proxy, "_counter_lock", None)
    if counter_lock is None:
        request_count = getattr(proxy, "segment_requests", None)
        recent_indexes = getattr(proxy, "recent_segment_indices", None)
    else:
        with counter_lock:
            request_count = getattr(proxy, "segment_requests", None)
            recent_indexes = getattr(proxy, "recent_segment_indices", None)
            if isinstance(recent_indexes, list):
                recent_indexes = list(recent_indexes)
    if not isinstance(recent_indexes, list):
        recent_indexes = None
    return request_count, recent_indexes


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
    requires_stereo_output = fmt in {"AC4_IMS", "EAC3_JOC"}
    exclusive_mode = "no"
    result = command(["set_property", "options/audio-exclusive", exclusive_mode])
    if not mpv_command_succeeded(result):
        raise RuntimeError(
            "MPV rejected output profile property options/audio-exclusive: "
            f"{result.get('error')}"
        )

    channel_mode = "stereo" if requires_stereo_output else "auto"
    result = command(["set_property", "audio-channels", channel_mode])
    if not mpv_command_succeeded(result):
        raise RuntimeError(f"MPV rejected output profile property audio-channels: {result.get('error')}")
    return channel_mode


def prepare_mpv_demuxer_format(fmt: str, command, transport: str | None = None) -> str:
    """Select a concrete libavformat demuxer for proxied DASH and audio MP4.

    ``demuxer-lavf-format`` selects the container only after libavformat is
    selected. Leaving ``demuxer=auto`` made extensionless loopback MPD URLs
    fail before MPV contacted the DASH proxy.
    """
    demuxer_format = ""
    demuxer = "auto"
    if transport is None:
        # Preserve the resolver's historical format defaults for direct callers
        # and tests. Playback passes the resolved transport explicitly below.
        is_dash = fmt in {"EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"}
    else:
        normalized_transport = str(transport).strip().upper().replace("-", "_")
        if normalized_transport in {"DASH", "MPEG_DASH", "MPD"}:
            is_dash = True
            demuxer = "lavf"
        elif normalized_transport == "MP4":
            # Eclipse providers may return an extensionless signed URL. Their
            # audio/mp4 body needs FFmpeg's ISO-BMFF demuxer explicitly, and
            # MPV must select lavf before applying the container override.
            demuxer_format = "mov"
            demuxer = "lavf"
            is_dash = False
        elif normalized_transport in {"DIRECT", "HTTP", "FILE"}:
            is_dash = False
        elif normalized_transport == "BTS" and fmt == "AC4_IMS":
            is_dash = False
        else:
            raise RuntimeError(f"Unsupported stream transport for {fmt}: {transport!r}")
    if is_dash:
        # Explicit and inferred DASH routes both require libavformat before
        # the DASH container override can take effect. Leaving the inferred
        # route on `auto` fails for extensionless loopback MPD URLs.
        demuxer = "lavf"
        demuxer_format = "dash"
    elif demuxer_format != "mov":
        demuxer_format = ""
    result = command(["set_property", "options/demuxer", demuxer])
    if not mpv_command_succeeded(result):
        raise RuntimeError(
            f"MPV rejected demuxer selection for {fmt}: {result.get('error')}"
        )
    result = command(["set_property", "demuxer-lavf-format", demuxer_format])
    if not mpv_command_succeeded(result):
        raise RuntimeError(
            "MPV rejected demuxer-lavf-format for "
            f"{fmt}: {result.get('error')}"
        )
    return demuxer_format


def stream_requires_dash_proxy(fmt: str, stream_info: dict) -> bool:
    """Select the DASH proxy only for DASH transports, never MP4/direct media."""
    if fmt not in {"EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"}:
        return False
    transport = stream_info.get("transport")
    if isinstance(transport, str) and transport.strip():
        normalized_transport = transport.strip().upper().replace("-", "_")
        return normalized_transport in {"DASH", "MPEG_DASH", "MPD"}
    segment_urls = stream_info.get("segmentUrls")
    return bool(
        isinstance(stream_info.get("manifestXml"), str)
        and stream_info["manifestXml"].strip()
        and isinstance(segment_urls, (list, tuple))
        and len(segment_urls) >= 2
    )


def prepare_mpv_raw_audio(sample_rate: int, channels: int, command) -> None:
    """Configure mpv for the OxideAV raw S16LE AC-4 playback endpoint."""
    if not 8_000 <= int(sample_rate) <= 384_000 or int(channels) != 2:
        raise RuntimeError("AC-4 raw PCM must be stereo with a valid sample rate")
    options = (
        ("options/demuxer", "rawaudio"),
        ("options/demuxer-rawaudio-format", "s16le"),
        ("options/demuxer-rawaudio-rate", int(sample_rate)),
        ("options/demuxer-rawaudio-channels", "stereo"),
        ("demuxer-lavf-format", ""),
    )
    for name, value in options:
        result = command(["set_property", name, value])
        if not mpv_command_succeeded(result):
            raise RuntimeError(f"MPV rejected {name} for AC-4 raw PCM: {result.get('error')}")


def reset_mpv_raw_audio(command) -> None:
    """Prevent a previous raw-PCM track from forcing the next demuxer."""
    result = command(["set_property", "options/demuxer", "auto"])
    if not mpv_command_succeeded(result):
        raise RuntimeError(f"MPV could not restore automatic demuxer selection: {result.get('error')}")


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

    requires_stereo_output = fmt in {"AC4_IMS", "EAC3_JOC"}
    if requires_stereo_output and input_channels != 2:
        raise RuntimeError(
            f"{fmt} headphone playback requires a two-channel rendered program "
            f"(MPV decoder reported {input_channels} channels)"
        )

    channel_mode = "stereo" if requires_stereo_output else "auto"
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
    if active_ao is None and hasattr(engine, "initialize_audio_params_muted"):
        engine.initialize_audio_params_muted(timeout=2.0)
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
    non_stereo_output_channels = None
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
                # audio-params and audio-out-params describe different stages.
                # Keep both visible; a wider audio-API layout is not itself
                # proof that the renderer delivered stereo to the headphones.
                # JOC still requires the downstream Studio/orender route.
                if requires_stereo_output and output_channels < 2:
                    non_stereo_output_channels = output_channels
                    if output_channels == 1:
                        mono_output_seen = True
                    time.sleep(0.05)
                    continue
                return {
                    "inputSampleRate": sample_rate,
                    "inputChannels": input_channels,
                    "programSampleRate": sample_rate,
                    "programChannels": input_channels,
                    "outputSampleRate": output_rate,
                    "outputChannels": output_channels,
                    "audioApiSampleRate": output_rate,
                    "audioApiChannels": output_channels,
                    "channelMode": channel_mode,
                    "exclusiveMode": exclusive_mode,
                    "outputConverted": output_rate != sample_rate or output_channels != input_channels,
                }
        time.sleep(0.05)

    if mono_output_seen:
        raise RuntimeError(
            f"WASAPI negotiated mono output; {fmt} headphone playback requires stereo"
        )
    if non_stereo_output_channels is not None:
        raise RuntimeError(
            f"WASAPI negotiated {non_stereo_output_channels}-channel output; "
            f"{fmt} headphone playback requires at least two channels"
        )
    if fmt == "AC4_IMS":
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
                "sourceDuration": duration,
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


def studio_config_binaural_sofa_path(config_text: str, config_dir: Path) -> Path | None:
    """Resolve the one configured SOFA path for the active binaural profile."""
    if not isinstance(config_text, str) or not config_text:
        return None
    if not studio_config_has_binaural_output_mode(config_text):
        return None

    in_render = False
    in_binaural = False
    values = {"hrir_source": [], "hrtf_sofa_path": []}
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
        if not in_binaural or indent != 4 or ":" not in content:
            continue
        key, raw_value = content.split(":", 1)
        key = key.strip()
        if key not in values:
            continue
        value = raw_value.strip()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        elif value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return None
        else:
            value = value.split("#", 1)[0].strip()
        values[key].append(value)

    if any(len(items) != 1 for items in values.values()):
        return None
    if values["hrir_source"][0].casefold() != "sofa":
        return None
    configured_path = values["hrtf_sofa_path"][0]
    if not configured_path or "\x00" in configured_path:
        return None

    path = Path(os.path.expandvars(configured_path)).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path


def has_sofa_container_signature(path: Path) -> bool:
    """Check a readable .sofa HDF5/NetCDF container signature, not SOFA semantics."""
    if not isinstance(path, Path) or path.suffix.casefold() != ".sofa":
        return False
    try:
        if not path.is_file():
            return False
        file_size = path.stat().st_size
        if file_size < 8:
            return False
        with path.open("rb") as sofa_file:
            signature = sofa_file.read(8)
            if signature[:4] in {b"CDF\x01", b"CDF\x02", b"CDF\x05"}:
                return True
            hdf5_signature = b"\x89HDF\r\n\x1a\n"
            if signature == hdf5_signature:
                return True
            offset = 512
            while offset + len(hdf5_signature) <= file_size:
                sofa_file.seek(offset)
                if sofa_file.read(len(hdf5_signature)) == hdf5_signature:
                    return True
                offset *= 2
    except (OSError, ValueError):
        return False
    return False


def studio_binaural_sofa_container_is_configured() -> bool:
    """Verify the configured headphone SOFA path has a readable container."""
    try:
        config_text = STUDIO_CONFIG_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    path = studio_config_binaural_sofa_path(config_text, STUDIO_CONFIG_FILE.parent)
    return path is not None and has_sofa_container_signature(path)


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
    sofa_file_container_verified: bool = False,
    standalone_renderer_absent: bool = True,
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
        and sofa_file_container_verified
        and standalone_renderer_absent
    )


def is_managed_omniphony_process(info: dict, executable_name: str) -> bool:
    """Recognize the exact externally configured Studio/renderer executable."""
    if not isinstance(info, dict):
        return False
    name = str(info.get("name") or "").casefold()
    if name != Path(executable_name).name.casefold():
        return False
    executable = str(info.get("exe") or "")
    if not executable:
        return False
    try:
        candidate = os.path.normcase(os.path.realpath(executable))
        expected_paths = {
            os.path.normcase(os.path.realpath(str(STUDIO_EXE))),
            os.path.normcase(os.path.realpath(str(RENDERER_EXE))),
        }
        return candidate in expected_paths
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
        except psutil.NoSuchProcess:
            # The standalone renderer is optional: JOC uses libmpv's in-process
            # orender.dll and Studio's direct MPV connection. It may exit after
            # Toolhelp snapshots its PID, so its missing metadata cannot erase
            # otherwise valid Studio/MPV candidates or invalidate that route.
            if name.casefold() == RENDERER_EXE.name.casefold():
                continue
            raise
        except (psutil.AccessDenied, OSError):
            # A discovered renderer whose executable cannot be inspected is
            # not proven absent. Fail closed rather than trusting an unknown
            # process to be harmless.
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
        "mpv": len(mpvs) == 1,
        "embeddedRenderer": orender_module_loaded,
        "orenderModuleLoaded": orender_module_loaded,
        "studioRendererConnected": studio_renderer_connected,
        "processIdentity": process_identity_valid,
        "headphoneBinauralConfigured": studio_binaural_output_is_configured(),
        "sofaFileContainerVerified": studio_binaural_sofa_container_is_configured(),
        "standaloneRendererAbsent": len(renderers) == 0,
    }
    ready = omniphony_route_is_ready(
        len(studios), len(mpvs), orender_module_loaded, studio_renderer_connected,
        checks["headphoneBinauralConfigured"],
        sofa_file_container_verified=checks["sofaFileContainerVerified"],
        standalone_renderer_absent=checks["standaloneRendererAbsent"],
    ) and process_identity_valid
    return {
        "ready": ready,
        "checks": checks,
        "studioPid": studios[0]["pid"] if len(studios) == 1 else None,
        "standaloneRendererPid": renderers[0]["pid"] if len(renderers) == 1 else None,
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


def refresh_invalidated_joc_route_telemetry(engine) -> bool:
    """Refresh route diagnostics for an invalidated JOC generation only.

    A Studio reconnect does not revive the old MPV/orender stream. This
    read-only probe updates only omniphonyRoute, and only if the same failed
    generation and MPV process are still current when the probe completes.
    """
    with engine.playback_lock:
        with engine.playback_generation_lock:
            observed_meta = engine.current_meta
            generation = observed_meta.get("generation")
            if (
                observed_meta.get("format") != "EAC3_JOC"
                or observed_meta.get("trackId") is None
                or not isinstance(generation, int)
                or generation <= 0
                or generation in {
                    engine.pending_generation,
                    engine.active_generation,
                    engine.prepared_generation,
                }
            ):
                return False
            observed_identity = (
                observed_meta.get("trackId"), generation,
                observed_meta.get("requestId"),
                engine.pending_generation, engine.pending_request_id,
                engine.active_generation, engine.active_request_id,
                engine.prepared_generation, engine.prepared_request_id,
            )
            observed_proc = engine.proc
            observed_pid = observed_proc.pid if observed_proc else None

    try:
        route_status = get_omniphony_route_status(observed_pid)
    except Exception as exc:
        print(f"[!] Failed invalidated JOC route telemetry refresh: {type(exc).__name__}")
        return False
    if not isinstance(route_status, dict):
        return False

    with engine.playback_lock:
        with engine.playback_generation_lock:
            current_meta = engine.current_meta
            current_generation = current_meta.get("generation")
            current_identity = (
                current_meta.get("trackId"), current_generation,
                current_meta.get("requestId"),
                engine.pending_generation, engine.pending_request_id,
                engine.active_generation, engine.active_request_id,
                engine.prepared_generation, engine.prepared_request_id,
            )
            if (
                current_meta is not observed_meta
                or current_meta.get("format") != "EAC3_JOC"
                or not isinstance(current_generation, int)
                or current_generation in {
                    engine.pending_generation,
                    engine.active_generation,
                    engine.prepared_generation,
                }
                or current_identity != observed_identity
                or engine.proc is not observed_proc
                or (engine.proc.pid if engine.proc else None) != observed_pid
            ):
                return False
            current_meta["omniphonyRoute"] = route_status
            return True


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
        self.studio_lock = threading.RLock()
        self.studio_proc: subprocess.Popen | None = None
        self.studio_job: WindowsOwnedProcessJob | None = None
        self.playback_lock = threading.Lock()
        self.playback_generation_lock = threading.RLock()
        self.seek_sequence = 0
        self.pause_intent_revision = 0
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
        self.mpv_media_loading = False
        self.mpv_media_load_epoch = 0
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
                self.pause_intent_revision += 1
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
        diagnostic_samples=None, expected_pause_intent_revision=None,
        recover_raw_ac4_pause=False,
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
        saw_seeking_during_request = False
        restored_internal_pause = False
        in_tolerance = False
        expected_position = None

        def pause_intent_changed():
            if expected_pause_intent_revision is None:
                return True
            with self.playback_generation_lock:
                return self.pause_intent_revision != expected_pause_intent_revision

        def internal_seek_pause(pause_value, seeking_value=None):
            return (
                expected_paused is False
                and pause_value is True
                and (
                    saw_seeking_during_request
                    or seeking_value is True
                )
                and recover_raw_ac4_pause
                and not pause_intent_changed()
            )

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
            if seeking_value is True:
                saw_seeking_during_request = True
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
                if pause_value is not expected_paused and not internal_seek_pause(
                    pause_value, seeking_value
                ):
                    record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="pause-changed")
                    return False, observed, "pause-changed"
            if (
                expected_paused is False
                and pause_value is True
                and internal_seek_pause(pause_value, seeking_value)
                and seeking_value is False
            ):
                # The raw-PCM demuxer can transiently pause MPV after its
                # range-backed seek completes. Restore the pre-seek play
                # state only when no authoritative TIDAL pause/resume command
                # arrived while the seek was settling.
                if restored_internal_pause:
                    record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="internal-pause-not-restored")
                    return False, observed, "pause-changed"
                if observed is not None and abs(observed - target) <= SEEK_POSITION_TOLERANCE_SECONDS:
                    with self.playback_generation_lock:
                        if not self.seek_is_current(sequence, track_id, generation, request_id):
                            record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="superseded")
                            return False, observed, "superseded"
                        if self.pause_intent_revision != expected_pause_intent_revision:
                            record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="pause-changed")
                            return False, observed, "pause-changed"
                        resume_result = self.cmd(["set_property", "pause", False])
                        if not mpv_command_succeeded(resume_result):
                            record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="seek-resume-error")
                            return False, observed, "ipc-error"
                    restored_internal_pause = True
                    active_clock_anchor = observed
                    active_clock_anchor_at = time.monotonic()
                    awaiting_post_seek_anchor = False
                    consecutive = 0
                    stable_since = None
                    record_sample(pause_value=pause_value, seeking_value=seeking_value, failure="internal-pause-restored")
                    time.sleep(0.05)
                    continue
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

    def begin_mpv_media_load(self) -> int:
        with self.lock:
            self.mpv_media_load_epoch += 1
            self.mpv_media_loading = True
            return self.mpv_media_load_epoch

    def finish_mpv_media_load(self, epoch: int, *, resolved: bool) -> bool:
        """Clear the status guard only for the still-current resolved load."""
        with self.lock:
            if epoch != self.mpv_media_load_epoch or not resolved:
                return False
            self.mpv_media_loading = False
            return True

    def is_mpv_media_loading(self) -> bool:
        with self.lock:
            return self.mpv_media_loading

    def is_playback_preparing(self) -> bool:
        """Return true until the current load is ready or explicitly failed."""
        with self.playback_generation_lock:
            return (
                self.pending_generation > 0
                and self.active_generation == 0
                and self.prepared_generation == 0
                and self.current_meta.get("playbackState") == "loading"
            )

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
        path_result = self.clock_cmd(["get_property", "path"]) if include_path else None
        active = str(path_result.get("data") or "") if path_result else ""
        params_result = (
            self.clock_cmd(["get_property", "audio-params"])
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
            with self.studio_lock:
                if (
                    self.studio_proc is not None
                    and self.studio_proc.poll() is not None
                ) or (self.studio_proc is None and self.studio_job is not None):
                    if not self.stop_owned_omniphony_studio():
                        return {"ready": False, "checks": {"studioCleanup": False}}
                if not self.studio_proc and not self._start_owned_omniphony_studio():
                    return {"ready": False, "checks": {"studioLaunch": False}}

        deadline = time.monotonic() + timeout
        status = {"ready": False, "checks": {}}
        restarted_stale_studio = False
        while time.monotonic() < deadline:
            status = get_omniphony_route_status(self.proc.pid if self.proc else None)
            if status.get("ready"):
                return status
            checks = status.get("checks", {})
            if (
                not restarted_stale_studio
                and checks.get("studio")
                and checks.get("mpv")
                and checks.get("orenderModuleLoaded")
                and not checks.get("studioRendererConnected")
                and status.get("studioPid")
            ):
                # Check if Studio has a self-loop on 127.0.0.1:9000 -> 127.0.0.1:9000
                try:
                    s_conns = psutil.Process(int(status["studioPid"])).net_connections(kind="tcp")
                    has_self_loop = any(
                        c.laddr and c.raddr and c.laddr.port == 9000 and c.raddr.port == 9000
                        for c in s_conns
                    )
                    if has_self_loop:
                        with self.studio_lock:
                            owned = self.studio_proc
                            owns_stale_studio = (
                                owned is not None
                                and owned.poll() is None
                                and owned.pid == int(status["studioPid"])
                            )
                            if owns_stale_studio:
                                restarted_stale_studio = True
                                if not self.stop_owned_omniphony_studio():
                                    return {"ready": False, "checks": {"studioCleanup": False}}
                                if not self._start_owned_omniphony_studio():
                                    return {"ready": False, "checks": {"studioLaunch": False}}
                except Exception:
                    pass
            time.sleep(0.2)
        return status

    def _start_owned_omniphony_studio(self) -> bool:
        """Launch one service-owned Studio process inside a kill-on-close job."""
        with self.studio_lock:
            if self.studio_proc is not None and self.studio_proc.poll() is None:
                return True
            if self.studio_proc is not None or self.studio_job is not None:
                if not self.stop_owned_omniphony_studio():
                    return False
            try:
                process, job = launch_process_in_windows_job(
                    [str(STUDIO_EXE), "--register", STUDIO_REGISTER_TARGET],
                    cwd=str(STUDIO_EXE.parent),
                    env=studio_process_environment(),
                )
            except (OSError, psutil.Error, subprocess.SubprocessError) as exc:
                self.studio_proc = None
                self.studio_job = None
                print(f"[-] Could not launch contained Omniphony Studio: {type(exc).__name__}")
                return False
            self.studio_proc = process
            self.studio_job = job
            try:
                self._monitor_owned_omniphony_studio(process, job)
            except RuntimeError as exc:
                print(f"[-] Could not monitor contained Omniphony Studio: {type(exc).__name__}")
                self.stop_owned_omniphony_studio()
                return False
            return True

    def _monitor_owned_omniphony_studio(
        self,
        process: subprocess.Popen,
        job: WindowsOwnedProcessJob,
    ) -> None:
        watcher = threading.Thread(
            target=self._watch_owned_omniphony_studio,
            args=(process, job),
            name=f"omniphony-studio-watch-{process.pid}",
            daemon=True,
        )
        watcher.start()

    def _watch_owned_omniphony_studio(
        self,
        process: subprocess.Popen,
        job: WindowsOwnedProcessJob,
    ) -> None:
        """Close this Studio tree's job immediately when its owned root exits."""
        try:
            process.wait()
        except (OSError, ChildProcessError) as exc:
            print(f"[-] Could not observe owned Omniphony Studio exit: {type(exc).__name__}")
            return

        with self.studio_lock:
            # A stop/restart may have already released these references and
            # installed a new Studio tree while this watcher was waking up.
            if self.studio_proc is not process or self.studio_job is not job:
                return
            try:
                job.close()
            except OSError as exc:
                # Keep both identities so the next lifecycle operation can
                # retry cleanup; never adopt or kill a process by executable name.
                print(f"[-] Could not close exited Studio process tree: {type(exc).__name__}")
                return
            self.studio_proc = None
            self.studio_job = None

    def stop_owned_omniphony_studio(self) -> bool:
        """Close the job containing only the Studio tree launched by this service."""
        with self.studio_lock:
            process = self.studio_proc
            job = self.studio_job
            if process is None and job is None:
                return False
            process_stopped = process is None
            try:
                if job is not None:
                    job.close()
                    if self.studio_job is job:
                        self.studio_job = None
                if process is not None and process.poll() is None:
                    if job is None:
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                        process_stopped = True
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=3)
                            process_stopped = True
                        except subprocess.TimeoutExpired:
                            print("[-] Owned Omniphony Studio tree did not exit; retaining its process handle")
                            return False
                if process_stopped or (process is not None and process.poll() is not None):
                    if self.studio_proc is process:
                        self.studio_proc = None
                    return True
                return False
            except OSError as exc:
                if process is not None and process.poll() is not None and self.studio_proc is process:
                    self.studio_proc = None
                print(f"[-] Could not stop owned Omniphony Studio process: {type(exc).__name__}")
                return False

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
            f"--ad-orender-config={STUDIO_CONFIG_FILE}",
            f"--ad-orender-library={ORENDER_DLL}",
            "--ad-orender-bridge-path=" + ";".join(str(path) for path in ORENDER_BRIDGE_PATHS),
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
                env=studio_process_environment(),
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
                except Ac4TransportUnavailable:
                    raise
                except Exception:
                    pass
                self.pipe = None
                self.pipe_buffer.clear()
                return {"error": str(e)}

    def clock_cmd(self, cmd_args, timeout=0.25) -> dict:
        """Read a transient property from the already-connected MPV only.

        Unlike cmd(), this path never ensures, reconnects, invalidates, or
        terminates an MPV process. A missed status sample is disposable.
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
            except TimeoutError:
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
                    loaded_settings = json.load(f)
                settings = {**DEFAULT_SETTINGS, **loaded_settings}
                # Remove paths and access tokens embedded in legacy manually
                # pasted manifest URLs. Eclipse providers now come only from
                # the authenticated account's Connections list.
                needs_save = "eclipse_addons" in settings
                if needs_save:
                    settings.pop("eclipse_addons", None)
                normalized_source = normalize_realmax_source(settings.get("realmax_source"))
                if settings.get("realmax_source") != normalized_source:
                    settings["realmax_source"] = normalized_source
                    needs_save = True
                if needs_save:
                    save_settings(settings)
                try:
                    settings["realmax_provider_order"] = validate_realmax_provider_order(
                        settings.get("realmax_provider_order")
                    )
                except ValueError as exc:
                    print(f"[-] Ignoring invalid RealMAX provider order: {exc}")
                    settings["realmax_provider_order"] = None
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


def tidal_manifest_client_not_entitled(exc: Exception) -> bool:
    """Recognize only TIDAL's explicit per-client manifest entitlement denial.

    A 403 by itself remains indeterminate. This exact JSON API error permits one
    fallback to the separately configured TV profile, whose response is still
    independently validated before accepting a JOC manifest.
    """
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 403:
        return False
    try:
        payload = response.json()
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    errors = payload.get("errors")
    return isinstance(errors, list) and any(
        isinstance(error, dict)
        and error.get("code") == "CLIENT_NOT_ENTITLED"
        for error in errors
    )


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
    if not str(track_id).strip().isdigit():
        return None
    """Resolve the selected JOC manifest using python-tidal.

    Try the authenticated PKCE DATA DASH manifest first. A confirmed negative,
    validated protected JOC manifest, or explicit CLIENT_NOT_ENTITLED response
    may proceed to the configured TV session. Other indeterminate PKCE failures
    do not trigger a profile fallback.
    """
    # Primary attempt: Web/Desktop DATA DASH
    try:
        with _TIDAL_API_LOCK:
            session = get_python_tidal_session()
            try:
                pkce_result = _resolve_eac3_joc_data_manifest(
                    session, track_id, "python_tidal_pkce"
                )
            except Exception as exc:
                if not tidal_manifest_client_not_entitled(exc):
                    raise
                print(
                    f"[-] python-tidal PKCE client is not entitled to the JOC manifest "
                    f"for track {track_id}; checking the configured TV profile"
                )
                pkce_result = None
            if pkce_result is not None:
                print(f"[+] python-tidal PKCE E-AC-3 JOC DASH resolved for track {track_id}")
                return pkce_result
    except Exception as exc:
        print(f"[-] python-tidal primary JOC check failed for track {track_id}: {type(exc).__name__}")
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
        refresh_token = session_data.get("refresh_token", {}).get("data")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RuntimeError("python-tidal mobile OAuth profile has no refresh token")

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

        # The saved access token may have been minted for another OAuth client.
        # The prior AC-4 path explicitly exchanged this refresh token with the
        # mobile client ID on every probe. Reproduce that through EbbLabs so the
        # subsequent library get_stream() sees a mobile-client bearer token.
        session.is_pkce = False
        session.refresh_token = refresh_token
        if not session.token_refresh(refresh_token):
            raise RuntimeError("python-tidal mobile OAuth refresh failed")
        if not session.load_oauth_session(
            session.token_type,
            session.access_token,
            session.refresh_token,
            expiry_time=session.expiry_time,
            is_pkce=False,
        ):
            raise RuntimeError("python-tidal OAuth session is no longer authenticated")
        # Match the Android request identity used by the prior verified AC-4
        # probe while keeping playback-info requests inside EbbLabs/python-tidal.
        # The generic library WebView UA negotiates MP4A on historical AC-4 IDs.
        session.request.user_agent = "TIDAL_ANDROID/1057 CFNetwork/1410.0.3 Darwin/22.4.0"
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
    if not str(track_id).strip().isdigit():
        return None
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
    if not str(track_id).strip().isdigit():
        return None
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
    if not str(track_id).strip().isdigit():
        return None
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
            "transport": "DASH",
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


class Ac4RawPcmProxy:
    """Expose OxideAV-decoded AC-4 PCM to mpv without a playback WAV file."""

    def __init__(self, converter: Path, input_path: Path, sample_rate: int,
                 channels: int, duration: float):
        if not converter.is_file() or not input_path.is_file():
            raise FileNotFoundError("AC-4 decoder or cached MP4 is unavailable")
        if not 8_000 <= int(sample_rate) <= 384_000 or int(channels) != 2:
            raise ValueError("AC-4 raw PCM route requires a valid rate and stereo output")
        if not math.isfinite(float(duration)) or float(duration) <= 0:
            raise ValueError("AC-4 MP4 has no usable audio duration")
        self.converter = converter
        self.input_path = input_path
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.block_align = self.channels * 2
        self.total_bytes = int(round(float(duration) * self.sample_rate)) * self.block_align
        if self.total_bytes <= 0:
            raise ValueError("AC-4 raw PCM stream has an invalid byte length")
        self.token = uuid.uuid4().hex
        self.server = None
        self.url = None
        self._lock = threading.RLock()
        self._processes: set[subprocess.Popen] = set()
        self.segment_requests = 0
        self.bytes_served = 0
        self.failed_segments = 0
        self._counter_lock = threading.Lock()

    @staticmethod
    def _range_start(value: str | None, total_bytes: int) -> tuple[int, int, bool]:
        if not value:
            return 0, total_bytes, False
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", value.strip())
        if not match:
            raise ValueError("unsupported raw PCM byte range")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else total_bytes - 1
        if start >= total_bytes or end < start:
            raise IndexError("raw PCM byte range is unsatisfiable")
        end = min(end, total_bytes - 1)
        return start, end - start + 1, True

    def start(self) -> str:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_HEAD(self):
                self._serve(False)

            def do_GET(self):
                self._serve(True)

            def _serve(self, send_body: bool):
                if self.path != f"/{proxy.token}/audio.raw":
                    self.send_error(404)
                    return
                try:
                    start, length, partial = proxy._range_start(
                        self.headers.get("Range"), proxy.total_bytes
                    )
                except IndexError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{proxy.total_bytes}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                except ValueError:
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if start % proxy.block_align:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{proxy.total_bytes}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                process = None
                if send_body:
                    with proxy._counter_lock:
                        proxy.segment_requests += 1
                    try:
                        process = subprocess.Popen(
                            [str(proxy.converter), "--raw-mp4", str(proxy.input_path)],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                    except OSError as exc:
                        with proxy._counter_lock:
                            proxy.failed_segments += 1
                        print(f"[-] Could not start OxideAV AC-4 decoder: {type(exc).__name__}")
                        self.send_error(502, "AC-4 decoder unavailable")
                        return
                    with proxy._lock:
                        proxy._processes.add(process)

                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if partial:
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{start + length - 1}/{proxy.total_bytes}",
                    )
                self.end_headers()
                if process is None:
                    return

                remaining_skip = start
                remaining_send = length
                bytes_sent = 0
                full_stream_requested = start == 0 and length == proxy.total_bytes
                try:
                    while remaining_send > 0:
                        chunk = process.stdout.read(min(64 * 1024, remaining_skip + remaining_send))
                        if not chunk:
                            break
                        if remaining_skip:
                            skipped = min(remaining_skip, len(chunk))
                            chunk = chunk[skipped:]
                            remaining_skip -= skipped
                        if chunk:
                            chunk = chunk[:remaining_send]
                            self.wfile.write(chunk)
                            bytes_sent += len(chunk)
                            remaining_send -= len(chunk)
                            with proxy._counter_lock:
                                proxy.bytes_served += len(chunk)
                    if remaining_send:
                        with proxy._counter_lock:
                            proxy.failed_segments += 1
                        print(
                            f"[-] OxideAV raw PCM ended early: sent {bytes_sent} of {length} requested bytes"
                        )
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    pass
                finally:
                    stream_completed = full_stream_requested and remaining_send == 0
                    if process.stdout:
                        process.stdout.close()
                    if process.poll() is None and not stream_completed:
                        process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    error_output = process.stderr.read() if process.stderr else b""
                    if process.stderr:
                        process.stderr.close()
                    with proxy._lock:
                        proxy._processes.discard(process)
                    if process.returncode not in (0, -15) and stream_completed:
                        print(f"[-] OxideAV AC-4 decoder exited with code {process.returncode}")
                    if error_output and process.returncode not in (0, -15) and stream_completed:
                        print("[-] OxideAV AC-4 decode reported an error")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/{self.token}/audio.raw"
        return self.url

    def close(self):
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                process.terminate()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        for process in processes:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


AC4_RAW_PCM_SEEK_TIMEOUT_BASE_SECONDS = 2.0
AC4_RAW_PCM_SEEK_SECONDS_PER_AUDIO_SECOND = 0.08
AC4_RAW_PCM_SEEK_TIMEOUT_MAX_SECONDS = 30.0


def seek_settle_timeout_seconds(proxy, position: float) -> float:
    """Allow the raw AC-4 proxy time to re-decode PCM up to the seek offset."""
    if not isinstance(proxy, Ac4RawPcmProxy):
        return SEEK_SETTLE_TIMEOUT_SECONDS
    position = max(0.0, float(position))
    return min(
        AC4_RAW_PCM_SEEK_TIMEOUT_MAX_SECONDS,
        max(
            SEEK_SETTLE_TIMEOUT_SECONDS,
            AC4_RAW_PCM_SEEK_TIMEOUT_BASE_SECONDS
            + position * AC4_RAW_PCM_SEEK_SECONDS_PER_AUDIO_SECOND,
        ),
    )


_ECLIPSE_AUTH_LOCK = threading.RLock()
_ECLIPSE_STREAM_DISCOVERY_SEMAPHORE = threading.BoundedSemaphore(2)
_ECLIPSE_INVENTORY_CANCEL_LOCK = threading.Lock()
_ECLIPSE_INVENTORY_OPERATIONS: dict[str, tuple[threading.Event, float]] = {}


def _register_eclipse_inventory_operation(operation_id: str) -> threading.Event | None:
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", operation_id):
        return None
    now = time.monotonic()
    with _ECLIPSE_INVENTORY_CANCEL_LOCK:
        for key, (_event, created) in list(_ECLIPSE_INVENTORY_OPERATIONS.items()):
            if now - created > 180:
                _ECLIPSE_INVENTORY_OPERATIONS.pop(key, None)
        existing = _ECLIPSE_INVENTORY_OPERATIONS.get(operation_id)
        if existing is not None:
            return existing[0] if existing[0].is_set() else None
        if len(_ECLIPSE_INVENTORY_OPERATIONS) >= 32:
            return None
        event = threading.Event()
        _ECLIPSE_INVENTORY_OPERATIONS[operation_id] = (event, now)
        return event


def _cancel_eclipse_inventory_operation(operation_id: str) -> bool:
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", operation_id):
        return False
    with _ECLIPSE_INVENTORY_CANCEL_LOCK:
        operation = _ECLIPSE_INVENTORY_OPERATIONS.get(operation_id)
        if operation is None:
            if len(_ECLIPSE_INVENTORY_OPERATIONS) >= 32:
                return False
            event = threading.Event()
            event.set()
            _ECLIPSE_INVENTORY_OPERATIONS[operation_id] = (event, time.monotonic())
            return True
        operation[0].set()
        return True


def _unregister_eclipse_inventory_operation(operation_id: str, event: threading.Event) -> None:
    with _ECLIPSE_INVENTORY_CANCEL_LOCK:
        operation = _ECLIPSE_INVENTORY_OPERATIONS.get(operation_id)
        if operation is not None and operation[0] is event:
            _ECLIPSE_INVENTORY_OPERATIONS.pop(operation_id, None)
_ECLIPSE_DNS_LOOKUP_SLOTS = threading.BoundedSemaphore(4)
_ECLIPSE_REQUEST_WORKER_SLOTS = threading.BoundedSemaphore(4)
_ECLIPSE_AUTH_CACHE: dict[str, object] = {
    "jwt": None,
    "addon_tokens": {},
    "account_addons": {},
    "manifest_cache": {},
    "manifest_errors": {},
    "search_cache": {},
    "stream_cache": {},
    "discovered_streams": {},
    "tido_manifest_url": None,
}
_ECLIPSE_AUTH_GENERATION = 0
ECLIPSE_SESSION_FILE = CONFIG_DIR / "eclipse-session.json"
ECLIPSE_AUTH_VERIFY_URL = "https://api.eclipsemusic.app/api/auth/mobile/verify"
ECLIPSE_ACCOUNT_LINKING_SUPPORTED = True
_ECLIPSE_ACCOUNT_STATUS: dict[str, object] = {"user": None, "checked_at": 0.0, "session_mtime": None}
_ECLIPSE_LAST_LOGIN_ERROR: str | None = None
_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID: str | None = None
_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT = 0.0
_ECLIPSE_FINALIZED_LOGIN_ATTEMPTS: dict[str, float] = {}
ECLIPSE_LOGIN_ATTEMPT_TTL_SECONDS = 900.0
ECLIPSE_FINALIZED_ATTEMPT_TTL_SECONDS = 900.0
_ECLIPSE_ACCOUNT_SESSION_GENERATION = 0


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent Eclipse bearer and add-on access headers crossing origins."""

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None] | None:
        try:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme.lower() != "https" or not parsed.hostname:
                return None
            port = parsed.port
            if port not in (None, 443):
                return None
            return "https", parsed.hostname.lower(), 443
        except ValueError:
            return None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _validate_account_manifest_url(req.full_url)
            _validate_account_manifest_url(newurl)
        except (TypeError, ValueError):
            return None
        if self._origin(req.full_url) != self._origin(newurl):
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        cancellation = getattr(req, "_omni_cancel_state", None)
        if redirected is not None and isinstance(cancellation, _EclipseRequestCancellation):
            redirected._omni_cancel_state = cancellation
        return redirected


class _EclipseRequestCancellation:
    """Own sockets for one bounded request so its deadline can interrupt I/O."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self.cancelled = threading.Event()

    def register(self, sock: socket.socket) -> None:
        with self._lock:
            if self.cancelled.is_set():
                sock.close()
                raise TimeoutError("Eclipse request was cancelled")
            self._sockets.add(sock)

    def replace(self, old_socket: socket.socket, new_socket: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(old_socket)
            if self.cancelled.is_set():
                new_socket.close()
                raise TimeoutError("Eclipse request was cancelled")
            self._sockets.add(new_socket)

    def discard(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            sockets, self._sockets = tuple(self._sockets), set()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def _public_https_addresses(
    host: str,
    port: int,
    deadline: float | None = None,
    cancellation: _EclipseRequestCancellation | None = None,
) -> list[tuple[int, int, int, str, tuple]]:
    """Resolve once and retain only globally routable socket destinations."""
    if deadline is None:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    else:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _ECLIPSE_DNS_LOOKUP_SLOTS.acquire(timeout=max(0, remaining)):
            raise TimeoutError("Eclipse DNS resolution exceeded its request deadline")
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def resolve_in_background():
            try:
                result_queue.put((True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
            except Exception as exc:
                result_queue.put((False, exc))
            finally:
                _ECLIPSE_DNS_LOOKUP_SLOTS.release()

        try:
            worker = threading.Thread(target=resolve_in_background, name="Omniphony-Eclipse-DNS", daemon=True)
            worker.start()
        except Exception:
            _ECLIPSE_DNS_LOOKUP_SLOTS.release()
            raise
        while True:
            if cancellation is not None and cancellation.cancelled.is_set():
                raise TimeoutError("Eclipse DNS request was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Eclipse DNS resolution exceeded its request deadline")
            try:
                resolved, addresses = result_queue.get(timeout=min(remaining, 0.05))
                break
            except queue.Empty:
                continue
        if not resolved:
            raise addresses
    public = []
    for family, socktype, protocol, canonname, sockaddr in addresses:
        try:
            address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (address.is_global and not address.is_multicast and not address.is_reserved
                and not address.is_unspecified and not address.is_loopback
                and not address.is_link_local and not address.is_private):
            public.append((family, socktype, protocol, canonname, sockaddr))
    if not public:
        raise OSError("Eclipse HTTPS host did not resolve to a public address")
    return public


class _PublicAddressHTTPSConnection(http.client.HTTPSConnection):
    """Connect only to a checked DNS answer while preserving TLS host checks."""

    def connect(self):
        if self._tunnel_host:
            raise OSError("HTTPS proxy tunnels are disabled for Eclipse requests")
        last_error = None
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 8.0
        deadline = time.monotonic() + max(0.05, float(timeout))
        cancellation = getattr(self, "_omni_cancel_state", None)
        for family, socktype, protocol, _canonname, sockaddr in _public_https_addresses(
            self.host, self.port or 443, deadline,
            cancellation if isinstance(cancellation, _EclipseRequestCancellation) else None,
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            raw_socket = socket.socket(family, socktype, protocol)
            try:
                active_socket = raw_socket
                if isinstance(cancellation, _EclipseRequestCancellation):
                    cancellation.register(raw_socket)
                raw_socket.settimeout(remaining)
                raw_socket.connect(sockaddr)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Eclipse TLS connection exceeded its request deadline")
                raw_socket.settimeout(remaining)
                context = self._context or ssl.create_default_context()
                wrapped_socket = context.wrap_socket(
                    raw_socket, server_hostname=self.host, do_handshake_on_connect=False
                )
                if isinstance(cancellation, _EclipseRequestCancellation):
                    cancellation.replace(raw_socket, wrapped_socket)
                active_socket = wrapped_socket
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Eclipse TLS handshake exceeded its request deadline")
                wrapped_socket.settimeout(remaining)
                wrapped_socket.do_handshake()
                self.sock = wrapped_socket
                return
            except OSError as exc:
                last_error = exc
                if isinstance(cancellation, _EclipseRequestCancellation):
                    cancellation.discard(active_socket)
                active_socket.close()
            except Exception:
                if isinstance(cancellation, _EclipseRequestCancellation):
                    cancellation.discard(active_socket)
                active_socket.close()
                raise
        if last_error:
            raise last_error
        raise OSError("Eclipse HTTPS host has no usable public address")


class _PublicAddressHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        cancel_state = getattr(request, "_omni_cancel_state", None)
        connection_type = _PublicAddressHTTPSConnection
        if isinstance(cancel_state, _EclipseRequestCancellation):
            class _CancelablePublicAddressHTTPSConnection(_PublicAddressHTTPSConnection):
                _omni_cancel_state = cancel_state
            connection_type = _CancelablePublicAddressHTTPSConnection
        return self.do_open(
            connection_type,
            request,
            context=self._context,
        )


def _eclipse_authenticated_urlopen(
    request, timeout: float, *, expected_generation: int | None = None,
):
    """Open HTTPS with DNS-pinned public destinations and same-origin redirects."""
    if expected_generation is not None:
        with _ECLIPSE_AUTH_LOCK:
            if _ECLIPSE_AUTH_GENERATION != expected_generation:
                raise RuntimeError("Eclipse account changed before network dispatch")
    _validate_account_manifest_url(request.full_url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PublicAddressHTTPSHandler(context=ssl.create_default_context()),
        _SameOriginRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


def _read_eclipse_response(response, max_bytes: int, deadline: float) -> bytes:
    """Read in chunks under one monotonic deadline, including slow trickle bodies."""
    payload = bytearray()
    while len(payload) <= max_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Eclipse response exceeded its operation deadline")
        file_object = getattr(response, "fp", None)
        raw = getattr(file_object, "raw", None)
        response_socket = getattr(raw, "_sock", None) or getattr(file_object, "_sock", None)
        if response_socket is not None:
            response_socket.settimeout(remaining)
        reader = getattr(response, "read1", None) or response.read
        chunk = reader(min(64 * 1024, max_bytes + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)


def _bounded_eclipse_response(
    request, max_bytes: int, deadline: float, timeout: float,
    expected_generation: int | None = None,
    *, return_headers: bool = False,
) -> bytes | tuple[bytes, dict[str, str]]:
    """Bound total request time, including DNS, TLS, redirects and response headers.

    urllib's socket timeout is an inactivity timeout and can be reset by a slow
    peer between bytes. A small fixed worker pool lets the caller enforce the
    absolute deadline without allowing stalled network operations to spawn
    unbounded threads.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _ECLIPSE_REQUEST_WORKER_SLOTS.acquire(timeout=max(0, remaining)):
        raise TimeoutError("Eclipse request capacity or deadline exceeded")
    result_queue: queue.Queue = queue.Queue(maxsize=1)
    cancellation = _EclipseRequestCancellation()
    request._omni_cancel_state = cancellation

    def request_body():
        try:
            if expected_generation is not None:
                with _ECLIPSE_AUTH_LOCK:
                    if _ECLIPSE_AUTH_GENERATION != expected_generation:
                        raise RuntimeError("Eclipse account changed before queued request dispatch")
            request_timeout = min(timeout, max(0.05, deadline - time.monotonic()))
            with _eclipse_authenticated_urlopen(request, timeout=request_timeout) as response:
                body = _read_eclipse_response(response, max_bytes, deadline)
                response_headers = {
                    str(key).lower(): str(value)
                    for key, value in response.headers.items()
                } if return_headers else {}
                result_queue.put((True, (body, response_headers)))
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            result_queue.put((False, exc))
        finally:
            cancellation.cancel()
            _ECLIPSE_REQUEST_WORKER_SLOTS.release()

    try:
        worker = threading.Thread(target=request_body, name="Omniphony-Eclipse-HTTP", daemon=True)
        worker.start()
    except Exception:
        cancellation.cancel()
        _ECLIPSE_REQUEST_WORKER_SLOTS.release()
        raise
    remaining = deadline - time.monotonic()
    try:
        if remaining <= 0:
            raise queue.Empty
        succeeded, result = result_queue.get(timeout=remaining)
    except queue.Empty as exc:
        cancellation.cancel()
        raise TimeoutError("Eclipse request exceeded its absolute deadline") from exc
    if not succeeded:
        if isinstance(result, urllib.error.URLError) and isinstance(result.reason, TimeoutError):
            raise TimeoutError("Eclipse TLS connection exceeded its request deadline") from result
        raise result
    if return_headers:
        return result
    return result[0]


def _eclipse_session_file_mtime() -> float | None:
    try:
        return ECLIPSE_SESSION_FILE.stat().st_mtime
    except OSError:
        return None


def _normalize_verified_eclipse_user(raw_user: object) -> dict | None:
    """Accept a verified account only when Eclipse returns a usable identity."""
    if not isinstance(raw_user, dict):
        return None
    user = {}
    for key in ("id", "userId", "username", "name", "email", "image"):
        value = raw_user.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or len(value) > 512 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            continue
        user[key] = value
    if re.fullmatch(r"omni_test_user_[a-z0-9_-]+@test\.com", user.get("email", ""), re.IGNORECASE):
        return None
    if not any(user.get(key) for key in ("id", "userId", "email")):
        return None
    return user


def _is_synthetic_eclipse_user(raw_user: object) -> bool:
    if not isinstance(raw_user, dict):
        return False
    email = raw_user.get("email")
    return isinstance(email, str) and re.fullmatch(
        r"omni_test_user_[a-z0-9_-]+@test\.com", email.strip(), re.IGNORECASE
    ) is not None


def _validate_account_manifest_url(value: str) -> str:
    """Validate an HTTPS URL used for an Eclipse account manifest or add-on request."""
    if not isinstance(value, str):
        raise ValueError("manifest URL must be a string")
    candidate = value.strip()
    if not candidate or len(candidate) > 2048 or any(ord(char) < 0x20 for char in candidate):
        raise ValueError("manifest URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(candidate)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("manifest URL is malformed") from exc
    if parsed.scheme.lower() != "https" or not hostname:
        raise ValueError("Eclipse add-on URLs must use HTTPS")
    if parsed.port not in (None, 443):
        raise ValueError("Eclipse add-on URLs must use the standard HTTPS port")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("manifest URL must not contain embedded credentials")
    if parsed.fragment:
        raise ValueError("manifest URL must not contain a fragment")
    return candidate


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.wintypes.BYTE))]


def _eclipse_token_protect(token: str) -> str:
    """Protect an Eclipse bearer token with current-user Windows DPAPI."""
    if os.name != "nt":
        raise OSError("Eclipse account token protection requires Windows DPAPI")
    raw = token.encode("utf-8")
    source_buffer = ctypes.create_string_buffer(raw, len(raw))
    source = _DataBlob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.wintypes.BYTE)))
    protected = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
    if not crypt32.CryptProtectData(ctypes.byref(source), "Omniphony EX Eclipse account", None, None, None, 1, ctypes.byref(protected)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return base64.b64encode(ctypes.string_at(protected.pbData, protected.cbData)).decode("ascii")
    finally:
        local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(protected.pbData)


def _eclipse_token_unprotect(value: str) -> str:
    """Unprotect a current-user Windows DPAPI token without exposing it in logs."""
    if os.name != "nt":
        raise OSError("Eclipse account token protection requires Windows DPAPI")
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    source_buffer = ctypes.create_string_buffer(raw, len(raw))
    source = _DataBlob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.wintypes.BYTE)))
    clear = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(clear)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(clear.pbData, clear.cbData).decode("utf-8")
    finally:
        local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(clear.pbData)


def _read_eclipse_session() -> dict | None:
    try:
        saved = json.loads(ECLIPSE_SESSION_FILE.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            return None
        token = saved.get("jwt")
        protected = saved.get("jwt_dpapi")
        if isinstance(protected, str) and protected:
            token = _eclipse_token_unprotect(protected)
        elif isinstance(token, str) and token.strip():
            # Migrate a session created by the earlier local prototype to DPAPI.
            token = token.strip()
            _write_eclipse_session(token, saved.get("user") if isinstance(saved.get("user"), dict) else None)
        return {"jwt": token, "user": saved.get("user") if isinstance(saved.get("user"), dict) else None}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_eclipse_session(token: str, user: dict | None = None) -> None:
    protected = _eclipse_token_protect(token)
    public_user = {}
    if isinstance(user, dict):
        for key in ("id", "userId", "name", "email", "image"):
            value = user.get(key)
            if isinstance(value, str) and len(value) <= 512:
                public_user[key] = value
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = ECLIPSE_SESSION_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 1, "jwt_dpapi": protected, "user": public_user}), encoding="utf-8")
    os.replace(temporary, ECLIPSE_SESSION_FILE)


def _verify_eclipse_account_token(token: str, deadline: float | None = None) -> tuple[bool, dict | None, str | None]:
    request = urllib.request.Request(
        ECLIPSE_AUTH_VERIFY_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "OmniphonyEX TIDAL Luna plugin",
        },
    )
    try:
        remaining = 8.0 if deadline is None else min(8.0, deadline - time.monotonic())
        if remaining <= 0:
            return False, None, "deadline_exceeded"
        request_deadline = time.monotonic() + remaining
        raw_payload = _bounded_eclipse_response(request, 1_000_000, request_deadline, remaining)
        payload = json.loads(raw_payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, None, "unauthorized" if exc.code == 401 else "verification_failed"
    except (OSError, ValueError, json.JSONDecodeError):
        return False, None, "verification_unavailable"
    if not isinstance(payload, dict):
        return False, None, "verification_failed"
    if payload.get("success") is False:
        return False, None, "unauthorized"
    if payload.get("success") is not True:
        return False, None, "verification_failed"
    raw_user = payload.get("user")
    if _is_synthetic_eclipse_user(raw_user):
        return False, None, "synthetic_account_rejected"
    user = _normalize_verified_eclipse_user(raw_user)
    if user is None:
        return False, None, "invalid_identity"
    replacement = payload.get("token") if isinstance(payload.get("token"), str) else None
    return True, user, replacement


def _complete_eclipse_login(
    token: str,
    attempt_id: str | None = None,
    callback_user_id: str | None = None,
    callback_email: str | None = None,
) -> tuple[bool, dict | None, str | None]:
    global _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID, _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT
    global _ECLIPSE_LAST_LOGIN_ERROR, _ECLIPSE_ACCOUNT_SESSION_GENERATION
    if not ECLIPSE_ACCOUNT_LINKING_SUPPORTED:
        return False, None, "unsupported_account_integration"
    if not token or len(token) > 8192 or any(ord(char) < 0x20 for char in token):
        return False, None, "invalid_token"
    with _ECLIPSE_AUTH_LOCK:
        if attempt_id is not None:
            if (_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID != attempt_id
                    or time.time() - _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT > ECLIPSE_LOGIN_ATTEMPT_TTL_SECONDS):
                return False, None, "account_changed"
        auth_generation = _ECLIPSE_AUTH_GENERATION
    valid, user, replacement = _verify_eclipse_account_token(token)
    if not valid:
        return False, None, replacement
    if callback_user_id is not None or callback_email is not None:
        verified_user_id = user.get("id") or user.get("userId")
        verified_email = user.get("email")
        if (not isinstance(callback_user_id, str) or not isinstance(callback_email, str)
                or not isinstance(verified_user_id, str) or not isinstance(verified_email, str)
                or callback_user_id != verified_user_id
                or callback_email.casefold() != verified_email.casefold()):
            return False, None, "identity_mismatch"
    saved_token = replacement.strip() if isinstance(replacement, str) and replacement.strip() else token
    with _ECLIPSE_AUTH_LOCK:
        if _ECLIPSE_AUTH_GENERATION != auth_generation:
            return False, None, "account_changed"
        if attempt_id is not None:
            if (_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID != attempt_id
                    or time.time() - _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT > ECLIPSE_LOGIN_ATTEMPT_TTL_SECONDS):
                return False, None, "account_changed"
        try:
            _write_eclipse_session(saved_token, user)
        except (OSError, ValueError, TypeError):
            return False, None, "secure_storage_unavailable"
        _invalidate_eclipse_account_cache(clear_jwt=True)
        _ECLIPSE_AUTH_CACHE["jwt"] = saved_token
        _ECLIPSE_ACCOUNT_STATUS.update({
            "user": user,
            "checked_at": time.time(),
            "session_mtime": _eclipse_session_file_mtime(),
        })
        if attempt_id is not None:
            _finalize_eclipse_login_attempt_locked(attempt_id)
            _ECLIPSE_LAST_LOGIN_ERROR = None
        _ECLIPSE_ACCOUNT_SESSION_GENERATION += 1
    return True, user, None


def _invalidate_eclipse_account_cache(clear_jwt: bool = False) -> None:
    """Clear auth-derived caches when the saved Eclipse account changes or disappears."""
    global _ECLIPSE_AUTH_GENERATION
    _ECLIPSE_AUTH_GENERATION += 1
    account_cache = _ECLIPSE_AUTH_CACHE.get("account_addons")
    account_urls = set()
    if isinstance(account_cache, dict):
        cached = account_cache.get("current")
        result = cached.get("result") if isinstance(cached, dict) else None
        addons = result.get("addons") if isinstance(result, dict) else None
        if isinstance(addons, list):
            account_urls = {
                item.get("manifestUrl") for item in addons
                if isinstance(item, dict) and isinstance(item.get("manifestUrl"), str)
            }
    manifest_cache = _ECLIPSE_AUTH_CACHE.get("manifest_cache")
    if isinstance(manifest_cache, dict):
        for url in account_urls:
            manifest_cache.pop(url, None)
    manifest_errors = _ECLIPSE_AUTH_CACHE.get("manifest_errors")
    if isinstance(manifest_errors, dict):
        for url in account_urls:
            manifest_errors.pop(hashlib.sha256(url.encode("utf-8")).hexdigest(), None)
    if clear_jwt:
        _ECLIPSE_AUTH_CACHE["jwt"] = None
        _ECLIPSE_ACCOUNT_STATUS.update({"user": None, "checked_at": 0.0, "session_mtime": None})
    for key in ("addon_tokens", "account_addons", "search_cache", "stream_cache", "discovered_streams", "addon_cooldowns"):
        cached = _ECLIPSE_AUTH_CACHE.get(key)
        if isinstance(cached, dict):
            cached.clear()


def _get_eclipse_jwt(force_refresh: bool = False) -> str | None:
    """Load the saved Eclipse account token; force_refresh reloads local storage only."""
    if not ECLIPSE_ACCOUNT_LINKING_SUPPORTED:
        return None
    with _ECLIPSE_AUTH_LOCK:
        if not force_refresh and isinstance(_ECLIPSE_AUTH_CACHE.get("jwt"), str) and _ECLIPSE_AUTH_CACHE["jwt"]:
            return str(_ECLIPSE_AUTH_CACHE["jwt"])
        saved = _read_eclipse_session() if ECLIPSE_SESSION_FILE.is_file() else None
        token = saved.get("jwt") if isinstance(saved, dict) else None
        if isinstance(token, str) and token.strip():
            token = token.strip()
            if token != _ECLIPSE_AUTH_CACHE.get("jwt"):
                _invalidate_eclipse_account_cache(clear_jwt=True)
            _ECLIPSE_AUTH_CACHE["jwt"] = token
            return token

        if force_refresh:
            _invalidate_eclipse_account_cache(clear_jwt=True)
        return None


def eclipse_account_status(force_refresh: bool = False, deadline: float | None = None) -> dict:
    """Verify the local Eclipse session and return only non-secret account details."""
    if not ECLIPSE_ACCOUNT_LINKING_SUPPORTED:
        return {
            "connected": False,
            "available": False,
            "user": None,
            "reason": "unsupported_account_integration",
        }
    token = _get_eclipse_jwt(force_refresh=force_refresh)
    if not token:
        return {"connected": False, "user": None}
    session_mtime = _eclipse_session_file_mtime()
    with _ECLIPSE_AUTH_LOCK:
        cached_user = _ECLIPSE_ACCOUNT_STATUS.get("user")
        checked_at = float(_ECLIPSE_ACCOUNT_STATUS.get("checked_at") or 0)
        if (not force_refresh and isinstance(cached_user, dict)
                and _normalize_verified_eclipse_user(cached_user) is not None
                and session_mtime is not None
                and _ECLIPSE_ACCOUNT_STATUS.get("session_mtime") == session_mtime
                and time.time() - checked_at < 60):
            return {"connected": True, "user": cached_user}
        auth_generation = _ECLIPSE_AUTH_GENERATION
    valid, user, replacement = _verify_eclipse_account_token(token, deadline=deadline)
    with _ECLIPSE_AUTH_LOCK:
        if (_ECLIPSE_AUTH_GENERATION != auth_generation
                or _ECLIPSE_AUTH_CACHE.get("jwt") != token):
            return {"connected": False, "user": None, "reason": "account_changed"}
        if not valid:
            permanent_failure = replacement in {"unauthorized", "synthetic_account_rejected", "invalid_token"}
            cleanup_failed = False
            if permanent_failure:
                try:
                    ECLIPSE_SESSION_FILE.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed = True
            _invalidate_eclipse_account_cache(clear_jwt=permanent_failure)
            if not permanent_failure:
                _ECLIPSE_ACCOUNT_STATUS.update({"user": None, "checked_at": 0.0, "session_mtime": None})
            reason = "session_cleanup_failed" if cleanup_failed else replacement
            return {"connected": False, "user": None, "reason": reason}
        saved_token = replacement.strip() if isinstance(replacement, str) and replacement.strip() else token
        if saved_token != token or not isinstance(_ECLIPSE_ACCOUNT_STATUS.get("user"), dict):
            try:
                _write_eclipse_session(saved_token, user)
            except (OSError, ValueError, TypeError):
                return {"connected": False, "user": None, "reason": "secure_storage_unavailable"}
        if saved_token != token:
            _invalidate_eclipse_account_cache(clear_jwt=True)
            _ECLIPSE_AUTH_CACHE["jwt"] = saved_token
        _ECLIPSE_ACCOUNT_STATUS.update({
            "user": user,
            "checked_at": time.time(),
            "session_mtime": _eclipse_session_file_mtime(),
        })
    return {"connected": True, "user": user}


def _verified_eclipse_session_snapshot(force_refresh: bool = False, deadline: float | None = None) -> tuple[str, dict, int] | None:
    """Capture the credential, verified identity and generation for one account operation."""
    status = eclipse_account_status(force_refresh=force_refresh, deadline=deadline)
    if status.get("connected") is not True:
        return None
    with _ECLIPSE_AUTH_LOCK:
        token = _ECLIPSE_AUTH_CACHE.get("jwt")
        user = _normalize_verified_eclipse_user(_ECLIPSE_ACCOUNT_STATUS.get("user"))
        if (not isinstance(token, str) or not token or user is None
                or user != status.get("user")):
            return None
        return token, user, _ECLIPSE_AUTH_GENERATION


def _eclipse_identity_key(user: dict) -> tuple[str, str] | None:
    normalized = _normalize_verified_eclipse_user(user)
    if normalized is None:
        return None
    for key in ("id", "userId"):
        value = normalized.get(key)
        if value:
            return key, value.casefold()
    email = normalized.get("email")
    return ("email", email.casefold()) if email else None


def public_eclipse_auth_status() -> dict:
    """Expose account state plus a safe, transient sign-in failure code."""
    status = dict(eclipse_account_status())
    with _ECLIPSE_AUTH_LOCK:
        _expire_eclipse_login_attempt_locked(time.time())
        last_login_error = _ECLIPSE_LAST_LOGIN_ERROR
        status["sessionGeneration"] = _ECLIPSE_ACCOUNT_SESSION_GENERATION
        status["loginAttemptActive"] = _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID is not None
    if last_login_error:
        status["lastLoginError"] = last_login_error
    return status


def record_eclipse_login_flow_status(reason: object, attempt_id: object) -> str:
    """Record a correlated, allowlisted status from the native OAuth handoff."""
    global _ECLIPSE_LAST_LOGIN_ERROR, _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID
    global _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT
    if not isinstance(reason, str) or reason not in {"started", "callback_missing", "invalid_token", "oauth_denied", "oauth_cancelled"}:
        return "invalid"
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
        return "invalid"
    with _ECLIPSE_AUTH_LOCK:
        now = time.time()
        _expire_eclipse_login_attempt_locked(now)
        if reason == "started":
            finalized_until = _ECLIPSE_FINALIZED_LOGIN_ATTEMPTS.get(attempt_id, 0.0)
            if finalized_until > now:
                return "stale"
            if _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID == attempt_id:
                return "recorded"
            if _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID is not None:
                return "busy"
            _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = attempt_id
            _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT = now
            _ECLIPSE_LAST_LOGIN_ERROR = None
            return "recorded"
        if _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID != attempt_id:
            return "stale"
        _ECLIPSE_LAST_LOGIN_ERROR = reason
        _finalize_eclipse_login_attempt_locked(attempt_id)
        return "recorded"


def _expire_eclipse_login_attempt_locked(now: float) -> None:
    """Expire stale attempts and trim finalized IDs while holding the auth lock."""
    global _ECLIPSE_LAST_LOGIN_ERROR
    if (_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID is not None
            and now - _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT > ECLIPSE_LOGIN_ATTEMPT_TTL_SECONDS):
        expired = _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID
        _finalize_eclipse_login_attempt_locked(expired, now=now)
        _ECLIPSE_LAST_LOGIN_ERROR = "oauth_cancelled"
    for attempt, expires_at in tuple(_ECLIPSE_FINALIZED_LOGIN_ATTEMPTS.items()):
        if expires_at <= now:
            _ECLIPSE_FINALIZED_LOGIN_ATTEMPTS.pop(attempt, None)


def _finalize_eclipse_login_attempt_locked(attempt_id: str, now: float | None = None) -> None:
    """Mark one attempt terminal so delayed duplicate start/callback messages stay stale."""
    global _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID, _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT
    finalized_at = time.time() if now is None else now
    _ECLIPSE_FINALIZED_LOGIN_ATTEMPTS[attempt_id] = finalized_at + ECLIPSE_FINALIZED_ATTEMPT_TTL_SECONDS
    if _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID == attempt_id:
        _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = None
        _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT = 0.0


ECLIPSE_ACCOUNT_ADDONS_URL = "https://eclipsemusic.app/music/addons"
ECLIPSE_ACCOUNT_ADDONS_TTL_SECONDS = 45.0


def eclipse_account_addons(force_refresh: bool = False) -> dict:
    """Read the signed-in account's Eclipse Connections list without returning its token."""
    if not ECLIPSE_ACCOUNT_LINKING_SUPPORTED:
        return {
            "connected": False,
            "available": False,
            "addons": [],
            "order": [],
            "reason": "unsupported_account_integration",
        }
    session = _verified_eclipse_session_snapshot(force_refresh=force_refresh)
    if session is None:
        status = eclipse_account_status()
        reason = status.get("reason") if status.get("connected") is not True else "account_changed"
        return {
            "connected": False,
            "available": False,
            "addons": [],
            "order": [],
            **({"reason": reason} if isinstance(reason, str) else {}),
        }
    token, _user, auth_generation = session
    token_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _ECLIPSE_AUTH_LOCK:
        if (_ECLIPSE_AUTH_CACHE.get("jwt") != token
                or _ECLIPSE_AUTH_GENERATION != auth_generation):
            return {"connected": False, "addons": [], "order": [], "reason": "account_changed"}
    now = time.time()
    with _ECLIPSE_AUTH_LOCK:
        if (_ECLIPSE_AUTH_GENERATION != auth_generation
                or _ECLIPSE_AUTH_CACHE.get("jwt") != token):
            return {"connected": False, "available": False, "addons": [], "order": [], "reason": "account_changed"}
        cache = _ECLIPSE_AUTH_CACHE.setdefault("account_addons", {})
        cached = cache.get("current") if isinstance(cache, dict) else None
        if (not force_refresh and isinstance(cached, dict)
                and cached.get("token_key") == token_key
                and cached.get("accountGeneration") == auth_generation
                and now < float(cached.get("expires_at") or 0)):
            result = cached.get("result")
            if isinstance(result, dict):
                return dict(result)

    request = urllib.request.Request(
        ECLIPSE_ACCOUNT_ADDONS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Omniphony EX TIDAL Luna plugin",
        },
    )
    try:
        request_deadline = time.monotonic() + 8.0
        with _eclipse_authenticated_urlopen(request, timeout=8.0) as response:
            payload = json.loads(_read_eclipse_response(response, 1_000_000, request_deadline).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        reason = "unauthorized" if exc.code == 401 else "account_addons_unavailable"
        return {"connected": True, "available": False, "addons": [], "order": [], "reason": reason}
    except (OSError, ValueError, json.JSONDecodeError):
        return {"connected": True, "available": False, "addons": [], "order": [], "reason": "account_addons_unavailable"}

    if not isinstance(payload, dict) or not isinstance(payload.get("addons"), list):
        return {"connected": True, "available": False, "addons": [], "order": [], "reason": "invalid_account_addons_response"}

    normalized = []
    seen_ids = set()
    for item in payload["addons"][:64]:
        if not isinstance(item, dict):
            continue
        addon_id = item.get("addonId")
        manifest_url = item.get("manifestUrl")
        if not isinstance(addon_id, str) or not addon_id.strip() or len(addon_id) > 256:
            continue
        addon_id = addon_id.strip()
        if addon_id in seen_ids:
            continue
        if not isinstance(manifest_url, str) or not manifest_url.strip():
            continue
        try:
            manifest_url = _validate_account_manifest_url(manifest_url)
        except (TypeError, ValueError):
            continue
        enabled_value = item.get("isEnabled")
        if not isinstance(enabled_value, bool):
            continue
        seen_ids.add(addon_id)
        display_name = item.get("displayName")
        kind = item.get("kind")
        normalized.append({
            "addonId": addon_id,
            "displayName": display_name.strip()[:256] if isinstance(display_name, str) else addon_id,
            "kind": kind.strip()[:64] if isinstance(kind, str) else "community",
            "manifestUrl": manifest_url,
            "isEnabled": enabled_value,
        })

    raw_order = payload.get("order")
    order = [item.strip() for item in raw_order if isinstance(item, str) and item.strip()][:64] if isinstance(raw_order, list) else []
    if order:
        rank = {addon_id: index for index, addon_id in enumerate(order)}
        normalized.sort(key=lambda addon: rank.get(addon["addonId"], len(rank)))
    result = {"connected": True, "available": True, "addons": normalized, "order": order}
    with _ECLIPSE_AUTH_LOCK:
        if (_ECLIPSE_AUTH_GENERATION != auth_generation
                or _ECLIPSE_AUTH_CACHE.get("jwt") != token):
            return {"connected": False, "available": False, "addons": [], "order": [], "reason": "account_changed"}
        cache = _ECLIPSE_AUTH_CACHE.setdefault("account_addons", {})
        if isinstance(cache, dict):
            cache.clear()
            cache["current"] = {
                "token_key": token_key,
                "accountGeneration": auth_generation,
                "expires_at": time.time() + ECLIPSE_ACCOUNT_ADDONS_TTL_SECONDS,
                "result": result,
            }
    return dict(result)


def public_eclipse_account_addons(account: dict) -> dict:
    """Expose only account addon metadata and opaque local keys to the Luna renderer."""
    if not isinstance(account, dict):
        return {"connected": False, "addons": [], "order": []}
    public_addons = []
    for addon in account.get("addons", []):
        if not isinstance(addon, dict):
            continue
        manifest_url = addon.get("manifestUrl")
        public_addons.append({
            "key": _eclipse_provider_key(manifest_url, str(addon.get("addonId") or "")) if isinstance(manifest_url, str) else None,
            "addonId": addon.get("addonId"),
            "displayName": addon.get("displayName"),
            "kind": addon.get("kind"),
            "isEnabled": addon.get("isEnabled") is True,
        })
    return {
        **{key: account[key] for key in ("connected", "available", "reason", "order") if key in account},
        "addons": public_addons,
    }


def _eclipse_provider_key(manifest_url: str = "", addon_id: str = "") -> str:
    """Return a stable provider key for one Eclipse account connection."""
    identity = f"account:{addon_id.strip()}" if isinstance(addon_id, str) and addon_id.strip() else f"manifest:{manifest_url}"
    return "eclipse:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def realmax_provider_configuration(settings: dict | None = None, force_account_refresh: bool = False) -> dict:
    """Build available RealMAX providers, preserving account and manifest ordering."""
    settings = settings if isinstance(settings, dict) else load_settings()
    account = eclipse_account_addons(force_refresh=force_account_refresh)
    records = []
    for addon in account.get("addons", []) if account.get("available") is True else []:
        if not isinstance(addon, dict) or not isinstance(addon.get("manifestUrl"), str):
            continue
        url = addon["manifestUrl"]
        record = {
            "key": _eclipse_provider_key(url, str(addon.get("addonId") or "")),
            "name": str(addon.get("displayName") or addon.get("addonId") or "Eclipse add-on")[:256],
            "addonId": str(addon.get("addonId") or "")[:256],
            "kind": str(addon.get("kind") or "community")[:64],
            "manifestUrl": url,
            "available": addon.get("isEnabled") is True,
            "accountConnected": True,
            "source": "eclipse_account",
        }
        records.append(record)

    authorized = is_main_tidal_api_authorized()
    default_order = (["tidal_api"] if authorized else []) + [
        row["key"] for row in records if row["available"]
    ]
    saved_order = settings.get("realmax_provider_order")
    if isinstance(saved_order, list):
        allowed_keys = {"tidal_api"} | {row["key"] for row in records if row["available"]}
        legacy_key_map = {
            _eclipse_provider_key(row["manifestUrl"]): row["key"]
            for row in records
            if row.get("accountConnected") and isinstance(row.get("manifestUrl"), str)
        }
        configured_order = list(dict.fromkeys(legacy_key_map.get(key, key) for key in saved_order))
        order = [key for key in configured_order if key in allowed_keys]
    else:
        configured_order = default_order
        order = default_order

    known_keys = {row["key"] for row in records} | {"tidal_api"}
    for key in configured_order:
        if key not in known_keys and key.startswith("eclipse:"):
            records.append({
                "key": key,
                "name": "Unavailable Eclipse add-on",
                "addonId": "",
                "kind": "community",
                "available": False,
                "accountConnected": False,
                "source": "unavailable",
            })

    providers = [{
        "key": "tidal_api",
        "name": "TIDAL API (EbbLabs / python-tidal)",
        "type": "tidal_api",
        "available": authorized,
        "accountConnected": False,
    }, *({key: value for key, value in row.items() if key != "manifestUrl"} for row in records)]
    selected = set(configured_order)
    for provider in providers:
        provider["selected"] = provider["key"] in selected
        provider["priority"] = configured_order.index(provider["key"]) + 1 if provider["key"] in selected else None
    return {
        "providers": providers,
        "order": order,
        "configuredOrder": configured_order,
        "customized": isinstance(saved_order, list),
        "accountConnected": account.get("connected") is True,
        "accountAvailable": account.get("available") is True,
        "accountReason": account.get("reason"),
        "_internal_records": {row["key"]: row for row in records},
    }


def _ordered_resolution_providers(settings: dict | None = None) -> tuple[list[str], dict[str, dict]]:
    settings = settings if isinstance(settings, dict) else load_settings()
    source_mode = normalize_realmax_source(settings.get("realmax_source", "both"))
    if source_mode == "tidal_api":
        saved_order = settings.get("realmax_provider_order")
        tidal_selected = not isinstance(saved_order, list) or "tidal_api" in saved_order
        return (["tidal_api"] if tidal_selected and is_main_tidal_api_authorized() else []), {}
    config = realmax_provider_configuration(settings)
    by_key = {item["key"]: item for item in config["providers"]}
    order = list(config["order"])
    if source_mode == "eclipse_addons":
        order = [key for key in order if key != "tidal_api"]
    records = config.get("_internal_records", {})
    return order, records


def _get_eclipse_addon_access_token(addon_id: str = "com.yourname.amazonmusic", force_refresh: bool = False, deadline: float | None = None) -> str | None:
    """Mint a short-lived X-Eclipse-Access token for an Eclipse addon."""
    session = _verified_eclipse_session_snapshot(force_refresh=force_refresh, deadline=deadline)
    if session is None:
        return None
    jwt_token, user, auth_generation = session
    identity_key = _eclipse_identity_key(user)
    if identity_key is None:
        return None
    now = time.time()
    with _ECLIPSE_AUTH_LOCK:
        if (_ECLIPSE_AUTH_CACHE.get("jwt") != jwt_token
                or _ECLIPSE_AUTH_GENERATION != auth_generation):
            return None
        addon_tokens = _ECLIPSE_AUTH_CACHE.setdefault("addon_tokens", {})
        if not force_refresh and isinstance(addon_tokens, dict):
            entry = addon_tokens.get(addon_id)
            if isinstance(entry, dict):
                token = entry.get("token")
                expires_at = float(entry.get("expires_at") or 0)
                entry_generation = entry.get("accountGeneration")
                if (isinstance(token, str) and token and now < expires_at - 20
                        and entry_generation == auth_generation):
                    return token

    for attempt in range(2):
        if attempt > 0:
            refreshed = _verified_eclipse_session_snapshot(force_refresh=True, deadline=deadline)
            if refreshed is None:
                return None
            refreshed_token, refreshed_user, refreshed_generation = refreshed
            if _eclipse_identity_key(refreshed_user) != identity_key:
                return None
            jwt_token = refreshed_token
            auth_generation = refreshed_generation

        token_url = f"https://api.eclipsemusic.app/api/addon-access/token?addon={urllib.parse.quote(addon_id)}"
        req = urllib.request.Request(
            token_url,
            data=b"",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        try:
            remaining = 6.0 if deadline is None else min(6.0, deadline - time.monotonic())
            if remaining <= 0:
                return None
            token_deadline = time.monotonic() + remaining
            raw_payload = _bounded_eclipse_response(req, 65536, token_deadline, remaining)
            if len(raw_payload) > 65536:
                return None
            data = json.loads(raw_payload.decode("utf-8"))
            addon_token = data.get("token") if isinstance(data, dict) else None
            if (not isinstance(addon_token, str) or not addon_token
                    or addon_token != addon_token.strip() or len(addon_token) > 8192
                    or any(ord(char) < 0x20 or ord(char) == 0x7F for char in addon_token)):
                return None
            try:
                expires_in = float(data.get("expiresIn") or 300)
            except (TypeError, ValueError, OverflowError):
                expires_in = 300.0
            if not math.isfinite(expires_in) or expires_in <= 0:
                expires_in = 300.0
            expires_in = min(expires_in, 3600.0)
            with _ECLIPSE_AUTH_LOCK:
                current_user = _normalize_verified_eclipse_user(_ECLIPSE_ACCOUNT_STATUS.get("user"))
                if (_ECLIPSE_AUTH_CACHE.get("jwt") != jwt_token
                        or _ECLIPSE_AUTH_GENERATION != auth_generation
                        or _eclipse_identity_key(current_user or {}) != identity_key):
                    return None
                current_tokens = _ECLIPSE_AUTH_CACHE.setdefault("addon_tokens", {})
                if isinstance(current_tokens, dict):
                    current_tokens[addon_id] = {
                        "token": addon_token,
                        "expires_at": time.time() + expires_in,
                        "accountGeneration": auth_generation,
                    }
            return addon_token
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and attempt == 0:
                continue
            return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        except Exception:
            return None
    return None


def _is_tido_eclipse_addon(addon_manifest_url: str) -> bool:
    """Return True if the manifest URL points to the Tido TIDAL Eclipse addon."""
    u = (addon_manifest_url or "").strip().lower()
    return "tido.rickyaddons" in u or "com.yourname.tidal.hifi" in u


def _eclipse_worker_base_url(addon_manifest_url: str) -> str:
    """Resolve the base worker URL for an Eclipse addon.

    Eclipse tokenized addon URLs keep their path prefix on every resource
    request. Removing it turns a valid per-user URL into an unscoped request.
    """
    u = _validate_account_manifest_url(addon_manifest_url)
    parsed = urllib.parse.urlsplit(u)
    if parsed.scheme and parsed.netloc:
        path = parsed.path
        if path.endswith("/manifest.json"):
            path = path[:-len("/manifest.json")]
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))
    return u.rsplit("/manifest.json", 1)[0].rstrip("/")


def _eclipse_worker_url(addon_manifest_url: str, resource_path: str, params: dict | None = None) -> str:
    """Build an addon resource URL while retaining manifest-level query configuration."""
    base = _eclipse_worker_base_url(addon_manifest_url)
    manifest_query = urllib.parse.parse_qsl(urllib.parse.urlsplit(addon_manifest_url).query, keep_blank_values=True)
    resource_params = list((params or {}).items())
    # Per-request values replace matching configured values; duplicate keys
    # otherwise leave the addon's chosen quality ambiguous to query parsers.
    override_keys = {str(key) for key, _ in resource_params}
    merged = [(key, value) for key, value in manifest_query if key not in override_keys] + resource_params
    query = urllib.parse.urlencode(merged, doseq=True)
    result = f"{base}/{resource_path.lstrip('/')}" + (f"?{query}" if query else "")
    manifest_origin = _SameOriginRedirectHandler._origin(addon_manifest_url)
    result_origin = _SameOriginRedirectHandler._origin(result)
    if manifest_origin is None or result_origin != manifest_origin:
        raise ValueError("Eclipse add-on resource URL left its manifest origin")
    return result


def _eclipse_manifest_failure_code(exc: Exception) -> str:
    """Return a renderer-safe manifest failure category without provider details."""
    if isinstance(exc, urllib.error.HTTPError):
        code = int(exc.code)
        return f"manifest_http_{code}" if 400 <= code <= 599 else "manifest_http_error"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "manifest_timeout"
    if isinstance(exc, ssl.SSLError):
        return "manifest_tls_error"
    if isinstance(exc, socket.gaierror):
        return "manifest_dns_error"
    if isinstance(exc, (UnicodeError, json.JSONDecodeError)):
        return "manifest_invalid_json"
    if isinstance(exc, ValueError):
        return "manifest_url_or_payload_invalid"
    if isinstance(exc, urllib.error.URLError):
        return "manifest_network_error"
    if isinstance(exc, OSError):
        return "manifest_network_error"
    return "manifest_fetch_error"


def _record_eclipse_manifest_failure(addon_manifest_url: str, reason: str) -> None:
    key = hashlib.sha256(addon_manifest_url.encode("utf-8", errors="replace")).hexdigest()
    with _ECLIPSE_AUTH_LOCK:
        errors = _ECLIPSE_AUTH_CACHE.setdefault("manifest_errors", {})
        if isinstance(errors, dict):
            now = time.time()
            for stale_key in [
                stale_key for stale_key, item in errors.items()
                if not isinstance(item, dict) or now >= float(item.get("expires_at") or 0)
            ]:
                errors.pop(stale_key, None)
            if key not in errors and len(errors) >= 128:
                oldest_key = min(
                    errors,
                    key=lambda stale_key: float(errors[stale_key].get("expires_at") or 0)
                    if isinstance(errors[stale_key], dict) else 0,
                )
                errors.pop(oldest_key, None)
            errors[key] = {"reason": reason, "expires_at": time.time() + 60}


def _eclipse_manifest_failure_reason(addon_manifest_url: str) -> str:
    key = hashlib.sha256(addon_manifest_url.encode("utf-8", errors="replace")).hexdigest()
    with _ECLIPSE_AUTH_LOCK:
        errors = _ECLIPSE_AUTH_CACHE.setdefault("manifest_errors", {})
        item = errors.get(key) if isinstance(errors, dict) else None
        if isinstance(item, dict) and time.time() < float(item.get("expires_at") or 0):
            reason = item.get("reason")
            if isinstance(reason, str) and re.fullmatch(r"manifest_(?:http_[45][0-9]{2}|[a-z_]+)", reason):
                return reason
    return "manifest_unavailable"


def _get_eclipse_manifest(addon_manifest_url: str, force_refresh: bool = False, timeout: float = 6.0) -> dict | None:
    """Fetch and briefly cache a public Eclipse addon manifest."""
    manifest_url = addon_manifest_url.strip()
    now = time.time()
    with _ECLIPSE_AUTH_LOCK:
        cache = _ECLIPSE_AUTH_CACHE.setdefault("manifest_cache", {})
        if not force_refresh and isinstance(cache, dict):
            item = cache.get(manifest_url)
            if isinstance(item, dict) and now < float(item.get("expires_at") or 0):
                manifest = item.get("manifest")
                return manifest if isinstance(manifest, dict) else None
    try:
        manifest_url = _validate_account_manifest_url(manifest_url)
        req = urllib.request.Request(manifest_url, headers={"User-Agent": "OmniphonyEX/1.0 Eclipse addon client"})
        request_deadline = time.monotonic() + max(0.1, float(timeout))
        raw_manifest = _bounded_eclipse_response(
            req, 1_000_000, request_deadline, max(0.1, float(timeout))
        )
        if len(raw_manifest) > 1_000_000:
            _record_eclipse_manifest_failure(manifest_url, "manifest_payload_too_large")
            return None
        manifest = json.loads(raw_manifest.decode("utf-8"))
        if not isinstance(manifest, dict):
            _record_eclipse_manifest_failure(manifest_url, "manifest_payload_invalid")
            return None
        manifest_id = manifest.get("id")
        if not isinstance(manifest_id, str) or not manifest_id.strip():
            _record_eclipse_manifest_failure(manifest_url, "manifest_identity_missing_or_invalid")
            return None
    except Exception as exc:
        _record_eclipse_manifest_failure(addon_manifest_url, _eclipse_manifest_failure_code(exc))
        print(f"[-] Eclipse manifest fetch failed ({type(exc).__name__})")
        return None
    with _ECLIPSE_AUTH_LOCK:
        errors = _ECLIPSE_AUTH_CACHE.setdefault("manifest_errors", {})
        if isinstance(errors, dict):
            errors.pop(hashlib.sha256(manifest_url.encode("utf-8")).hexdigest(), None)
        cache = _ECLIPSE_AUTH_CACHE.setdefault("manifest_cache", {})
        if isinstance(cache, dict):
            cache[manifest_url] = {"manifest": manifest, "expires_at": time.time() + 300}
    return manifest


def is_main_tidal_api_authorized() -> bool:
    """Return True when the main EbbLabs python-tidal session is authorized and usable."""
    for sess_getter in (get_python_tidal_session, get_python_tidal_mobile_session, get_python_tidal_tv_session):
        try:
            sess = sess_getter()
            if sess is not None:
                return True
        except Exception:
            continue
    return False


def _probe_direct_audio_bitrate(
    url: str, duration: float, headers: dict[str, str], *,
    expected_generation: int | None = None,
) -> tuple[int | None, str | None]:
    """Compute average bitrate from HTTP Content-Range total byte length and duration."""
    if not url or not duration or duration <= 0:
        return None, None
    try:
        if expected_generation is not None:
            with _ECLIPSE_AUTH_LOCK:
                if _ECLIPSE_AUTH_GENERATION != expected_generation:
                    return None, None
        probe_headers = {**headers, "Range": "bytes=0-0"}
        req = urllib.request.Request(url, headers=probe_headers)
        with _eclipse_authenticated_urlopen(
            req, timeout=4, expected_generation=expected_generation,
        ) as resp:
            content_range = resp.headers.get("Content-Range") or ""
            total_bytes = 0
            if "/" in content_range:
                tail = content_range.rsplit("/", 1)[-1].strip()
                if tail.isdigit():
                    total_bytes = int(tail)
            if total_bytes <= 0:
                cl = resp.headers.get("Content-Length") or ""
                if cl.isdigit():
                    total_bytes = int(cl)
            if total_bytes > 1024:
                bitrate = int(round((total_bytes * 8) / float(duration)))
                if 32000 <= bitrate <= 25000000:
                    return bitrate, "HTTP Content-Range average"
    except Exception:
        pass
    return None, None


def _parse_hls_attribute_list(value: str) -> dict[str, str]:
    """Parse the quoted-string aware attribute lists used by HLS master tags."""
    parts = []
    start = 0
    quoted = False
    for index, char in enumerate(value):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    result = {}
    for part in parts:
        key, separator, raw = part.partition("=")
        if not separator:
            continue
        result[key.strip().upper()] = raw.strip().strip('"')
    return result


def _hls_stream_variant(attributes: dict, index: int) -> dict | None:
    codecs = [item.strip() for item in attributes.get("CODECS", "").split(",") if item.strip()]
    if not codecs and not attributes.get("BANDWIDTH"):
        return None
    resolution = attributes.get("RESOLUTION")
    video_codec = any(re.match(r"^(?:avc|hev|hvc|vp0|vp9|av01|theora)", codec, re.I) for codec in codecs)
    bandwidth = _bounded_integer(attributes.get("BANDWIDTH"), minimum=1, maximum=1_000_000_000)
    average_bandwidth = _bounded_integer(attributes.get("AVERAGE-BANDWIDTH"), minimum=1, maximum=1_000_000_000)
    clean_codecs = [_renderer_text(codec, 96) for codec in codecs[:16]]
    clean_codecs = [codec for codec in clean_codecs if codec]
    return {
        "index": index,
        "kind": "hls_variant",
        "codecs": clean_codecs,
        "declaredBandwidth": bandwidth,
        "averageBandwidth": average_bandwidth,
        "bandwidthScope": "aggregate" if video_codec or resolution else "rendition_unspecified_scope",
        "resolution": _renderer_text(resolution, 32),
        "audioGroup": _renderer_text(attributes.get("AUDIO"), 96),
        "protectionStatus": "unknown",
    }


def _hls_audio_rendition(attributes: dict, index: int) -> dict:
    channels = attributes.get("CHANNELS", "").split("/", 1)[0]
    try:
        channels_value = _bounded_integer(channels, minimum=1, maximum=64)
    except Exception:
        channels_value = None
    return {
        "index": index,
        "kind": "hls_audio_rendition",
        "name": _renderer_text(attributes.get("NAME"), 160),
        "group": _renderer_text(attributes.get("GROUP-ID"), 96),
        "language": _renderer_text(attributes.get("LANGUAGE"), 64),
        "channels": channels_value,
        "default": attributes.get("DEFAULT", "NO").upper() == "YES",
        "autoselect": attributes.get("AUTOSELECT", "NO").upper() == "YES",
        "protectionStatus": "unknown",
    }


def _validated_eclipse_manifest_headers(request_headers: dict | None) -> dict[str, str]:
    """Return exactly the bounded, sanitized provider headers sent to a manifest."""
    if not isinstance(request_headers, dict):
        return {}
    forbidden = {"host", "connection", "content-length", "transfer-encoding", "proxy-authorization"}
    normalized: dict[str, tuple[str, str]] = {}
    for key, value in list(request_headers.items())[:32]:
        if (isinstance(key, str) and isinstance(value, str) and len(key) <= 96
                and len(value) <= 4096 and not any(ord(ch) < 0x20 and ch not in "\t" for ch in key + value)
                and key.casefold() not in forbidden):
            normalized[key.casefold()] = (key, value)
    return {key: value for key, value in normalized.values()}


def _collect_dash_audio_variants(root, output: list[dict]) -> bool:
    """Collect declared audio representations without surfacing their URLs."""
    def local_name(element):
        return element.tag.rsplit("}", 1)[-1]

    def descendants(element, tag):
        return [child for child in list(element) if local_name(child) == tag]

    root_protected = any(local_name(child) == "ContentProtection" for child in list(root))
    complete = True
    for period_index, period in enumerate(descendants(root, "Period")):
        period_protected = root_protected or any(
            local_name(child) == "ContentProtection" for child in list(period)
        )
        for adaptation_index, adaptation in enumerate(descendants(period, "AdaptationSet")):
            inherited_protection = period_protected or any(
                local_name(child) == "ContentProtection" for child in list(adaptation)
            )
            representations = descendants(adaptation, "Representation")
            for representation_index, representation in enumerate(representations):
                if len(output) >= 256:
                    raise ValueError("DASH audio representation count exceeded inspection bound")
                mime = representation.get("mimeType") or adaptation.get("mimeType", "")
                codecs = representation.get("codecs") or adaptation.get("codecs", "")
                content_type = (representation.get("contentType") or adaptation.get("contentType") or "").lower()
                codec_tokens = re.split(r"[,\s]+", codecs.strip()) if codecs else []
                codec_audio = bool(re.search(r"(?:mp4a|ac-3|ec-3|ac-4|opus|flac|alac|mha[12])", codecs, re.I))
                codec_video = any(re.match(r"(?:avc|hev|hvc|vp0|vp9|av01|theora)", token, re.I) for token in codec_tokens)
                is_audio = content_type == "audio" or mime.lower().startswith("audio/") or codec_audio
                is_video = content_type == "video" or mime.lower().startswith("video/") or codec_video
                representation_protected = inherited_protection or any(
                    local_name(child) == "ContentProtection" for child in list(representation)
                )
                if not is_audio:
                    if not is_video:
                        complete = False
                        output.append({
                            "index": len(output),
                            "kind": "dash_unclassified_representation",
                            "id": _renderer_text(representation.get("id"), 96),
                            "periodIndex": period_index,
                            "adaptationIndex": adaptation_index,
                            "mimeType": _renderer_text(mime, 96),
                            "codecs": _renderer_text(codecs, 160),
                            "contentType": _renderer_text(content_type, 32),
                            "classificationStatus": "unclassified",
                            "protectionStatus": "protected" if representation_protected else "unknown",
                        })
                    continue
                channel_configuration = next((child for child in representation.iter()
                                              if local_name(child) == "AudioChannelConfiguration"), None)
                if channel_configuration is None:
                    channel_configuration = next((child for child in adaptation.iter()
                                                  if local_name(child) == "AudioChannelConfiguration"), None)
                channel_value = channel_configuration.get("value") if channel_configuration is not None else None
                channel_scheme = channel_configuration.get("schemeIdUri") if channel_configuration is not None else None
                channels_value = _bounded_integer(channel_value, minimum=1, maximum=64)
                mixed_audio_video = is_audio and is_video
                output.append({
                    "index": len(output),
                    "kind": "dash_mixed_representation" if mixed_audio_video else "dash_audio_representation",
                    "id": _renderer_text(representation.get("id"), 96),
                    "periodIndex": period_index,
                    "adaptationIndex": adaptation_index,
                    "bandwidth": _bounded_integer(representation.get("bandwidth"), minimum=1, maximum=1_000_000_000),
                    "bandwidthScope": "aggregate_audiovisual" if mixed_audio_video else "audio_representation",
                    "codecs": _renderer_text(codecs, 160),
                    "mimeType": _renderer_text(mime, 96),
                    "sampleRate": _bounded_integer(representation.get("audioSamplingRate") or adaptation.get("audioSamplingRate"), minimum=8_000, maximum=768_000),
                    "channels": channels_value,
                    "channelConfiguration": _renderer_text(channel_value, 64),
                    "channelConfigurationScheme": _renderer_text(channel_scheme, 160),
                    "language": _renderer_text(adaptation.get("lang"), 64),
                    "classificationStatus": "mixed_audio_video" if mixed_audio_video else "audio",
                    "protectionStatus": "protected" if representation_protected else "unknown",
                })
    return complete


def _eclipse_manifest_protection_status(
    url: str,
    manifest_type: str | None = None,
    *,
    timeout: float = 8.0,
    expected_generation: int | None = None,
    request_headers: dict | None = None,
    media_variants_out: list[dict] | None = None,
    media_variants_complete_out: list[bool] | None = None,
) -> bool | None:
    """Inspect declared HLS/DASH protection without resolving keys or media segments.

    Returns False for a signature-verified direct audio resource or after
    validating a clear manifest graph. Protected, malformed, unreachable,
    unsupported, or over-limit candidates remain unknown/protected so callers
    can keep them descriptor-only.
    """
    if not isinstance(url, str):
        return None
    parsed = urllib.parse.urlsplit(url)
    declared = str(manifest_type or "").strip().lower()
    kind = "hls" if declared == "hls" or parsed.path.lower().endswith(".m3u8") else (
        "dash" if declared == "dash" or parsed.path.lower().endswith(".mpd") else None
    )
    deadline = time.monotonic() + max(0.1, min(30.0, float(timeout)))

    def report_variants_complete(value: bool) -> None:
        if media_variants_complete_out is not None:
            if media_variants_complete_out:
                media_variants_complete_out[0] = value
            else:
                media_variants_complete_out.append(value)

    private_headers = _validated_eclipse_manifest_headers(request_headers)

    original_origin = _SameOriginRedirectHandler._origin(url)

    def fetch_manifest(target: str, *, sniff: bool = False) -> tuple[bytes, dict[str, str]]:
        target_origin = _SameOriginRedirectHandler._origin(target)
        authorized_headers = private_headers if target_origin == original_origin else {}
        request = urllib.request.Request(target, headers={
            "User-Agent": "OmniphonyEX/1.0 Eclipse media manifest inspector",
            "Accept": "application/vnd.apple.mpegurl, application/dash+xml, */*",
            **authorized_headers,
            **({"Range": "bytes=0-8191"} if sniff else {}),
        })
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("media manifest inspection deadline exceeded")
        result = _bounded_eclipse_response(
            request, 1_000_000, deadline, min(8.0, remaining),
            expected_generation=expected_generation,
            return_headers=True,
        )
        body, response_headers = result
        if len(body) > 1_000_000:
            raise ValueError("media manifest exceeds the inspection size limit")
        return body, response_headers

    def detect_manifest_type(body: bytes, response_headers: dict[str, str]) -> str | None:
        content_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
        text_prefix = body[:4096].decode("utf-8-sig", errors="replace").lstrip("\t\r\n ").lower()
        if content_type in {
            "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/mpegurl",
            "audio/mpegurl", "audio/x-mpegurl", "application/m3u",
        } or text_prefix.startswith("#extm3u"):
            return "hls"
        if content_type in {"application/dash+xml", "application/xml", "text/xml"} or "<mpd" in text_prefix:
            return "dash"
        # For direct clear elementary/container formats, require a recognized
        # payload signature in addition to the endpoint metadata. ISO-BMFF is
        # intentionally left unknown here: a small prefix cannot establish
        # that its later sample-encryption boxes are absent.
        is_audio_signature = (
            body.startswith((b"ID3", b"fLaC", b"OggS", b"RIFF"))
            or len(body) >= 2 and body[:2] in {b"\x0b\x77", b"\xac\x40", b"\xac\x41"}
            or len(body) >= 2 and body[0] == 0xFF and (body[1] & 0xF6) == 0xF0
        )
        if is_audio_signature and content_type in {
            "audio/mpeg", "audio/mp3", "audio/mpeg3", "audio/x-mpeg-3",
            "audio/flac", "audio/x-flac", "audio/aac", "audio/aacp", "audio/x-aac",
            "audio/ogg", "audio/opus",
            "audio/wav", "audio/x-wav", "audio/vnd.wave", "audio/vnd.dolby.dd-raw",
            "audio/eac3", "audio/x-eac3", "audio/ac4", "application/octet-stream",
            "application/x-flac",
        }:
            return "direct"
        return None

    try:
        if kind is None:
            body, response_headers = fetch_manifest(url, sniff=True)
            kind = detect_manifest_type(body, response_headers)
            if kind == "direct":
                report_variants_complete(True)
                return False
            if kind is None:
                report_variants_complete(False)
                return None
            # Sniffing is only for type detection; a Range response or server
            # that ignores Range may be partial. Re-fetch the complete bounded
            # manifest before making a protection decision.
            first_body = None
        else:
            first_body = None
        if kind == "dash":
            raw_xml = first_body if first_body is not None else fetch_manifest(url)[0]
            xml = raw_xml.decode("utf-8-sig", errors="replace") if isinstance(raw_xml, bytes) else raw_xml
            root = ET.fromstring(xml)
            if root.tag.rsplit("}", 1)[-1] != "MPD":
                return None
            protected = any(
                element.tag.rsplit("}", 1)[-1] == "ContentProtection"
                for element in root.iter()
            )
            if media_variants_out is not None:
                complete = _collect_dash_audio_variants(root, media_variants_out)
                report_variants_complete(complete)
            else:
                report_variants_complete(True)
            return protected

        pending = [url]
        visited: set[str] = set()
        any_protected = False
        while pending:
            current = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            if len(visited) > 16:
                return None
            raw_text = first_body if first_body is not None and current == url else fetch_manifest(current)[0]
            text = raw_text.decode("utf-8-sig", errors="replace") if isinstance(raw_text, bytes) else raw_text
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if not lines or lines[0] != "#EXTM3U":
                return None
            for line in lines:
                if not line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                    continue
                attributes = line.partition(":")[2]
                method = re.search(r'(?:^|,)\s*METHOD\s*=\s*"?([^,"]+)', attributes, re.IGNORECASE)
                if not method or method.group(1).strip().upper() != "NONE":
                    any_protected = True
            children = []
            pending_variant_uri = False
            pending_variant_attributes = None
            for line in lines:
                if line.startswith("#EXT-X-STREAM-INF:"):
                    if pending_variant_uri:
                        return None
                    pending_variant_uri = True
                    pending_variant_attributes = _parse_hls_attribute_list(line.partition(":")[2])
                elif pending_variant_uri and not line.startswith("#"):
                    children.append(line)
                    if media_variants_out is not None:
                        variant = _hls_stream_variant(pending_variant_attributes or {}, len(media_variants_out))
                        if variant is None or len(media_variants_out) >= 256:
                            return None
                        media_variants_out.append(variant)
                    pending_variant_uri = False
                    pending_variant_attributes = None
            if pending_variant_uri:
                return None
            for line in lines:
                if not line.startswith("#EXT-X-MEDIA:"):
                    continue
                attributes = _parse_hls_attribute_list(line.partition(":")[2])
                if attributes.get("TYPE", "").upper() != "AUDIO":
                    continue
                if media_variants_out is not None:
                    if len(media_variants_out) >= 256:
                        return None
                    media_variants_out.append(_hls_audio_rendition(attributes, len(media_variants_out)))
                if attributes.get("URI"):
                    children.append(attributes["URI"])
            if children:
                for child in children:
                    child_url = urllib.parse.urljoin(current, child)
                    _validate_account_manifest_url(child_url)
                    if child_url not in visited and child_url not in pending:
                        pending.append(child_url)
                        if len(pending) + len(visited) > 16:
                            return None
        report_variants_complete(True)
        return any_protected
    except Exception:
        report_variants_complete(False)
        return None


def _probe_direct_media_transport(
    url: str, headers: dict[str, str], *, expected_generation: int | None = None,
) -> str:
    """Identify extensionless ISO-BMFF streams without downloading the media."""
    if not url:
        return "DIRECT"
    try:
        if expected_generation is not None:
            with _ECLIPSE_AUTH_LOCK:
                if _ECLIPSE_AUTH_GENERATION != expected_generation:
                    return "DIRECT"
        probe_headers = {**headers, "Range": "bytes=0-63"}
        req = urllib.request.Request(url, headers=probe_headers)
        with _eclipse_authenticated_urlopen(
            req, timeout=5, expected_generation=expected_generation,
        ) as resp:
            status = getattr(resp, "status", 200)
            content_range = resp.headers.get("Content-Range") or ""
            if status not in {200, 206}:
                return "DIRECT"
            total_length = None
            range_end = None
            if status == 206:
                match = re.fullmatch(r"bytes\s+0-(\d+)/(\d+|\*)", content_range.strip(), re.IGNORECASE)
                if not match:
                    return "DIRECT"
                range_end = int(match.group(1))
                if range_end > 63:
                    return "DIRECT"
                if match.group(2) != "*":
                    total_length = int(match.group(2))
                    if total_length <= range_end:
                        return "DIRECT"
            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            content_length = resp.headers.get("Content-Length") or ""
            expected_range_bytes = range_end + 1 if status == 206 else None
            if (
                expected_range_bytes is not None
                and content_length.isdigit()
                and int(content_length) != expected_range_bytes
            ):
                return "DIRECT"
            prefix = resp.read(64)
            if expected_range_bytes is not None and len(prefix) != expected_range_bytes:
                return "DIRECT"
            is_mp4_mime = content_type in {"audio/mp4", "video/mp4", "application/mp4"}
            # The first ISO-BMFF box must be a structurally plausible ftyp
            # box; searching for the word anywhere in the sample can mislabel
            # unrelated payloads. Extended-size boxes carry a 64-bit size.
            if len(prefix) < 16 or prefix[4:8] != b"ftyp":
                continue_probe = False
            else:
                box_size = int.from_bytes(prefix[:4], "big")
                if box_size == 1:
                    box_size = int.from_bytes(prefix[8:16], "big") if len(prefix) >= 24 else 0
                    continue_probe = len(prefix) >= 24 and box_size >= 24
                else:
                    continue_probe = box_size >= 16
                known_length = total_length if status == 206 else None
                if known_length is None and status == 200 and content_length.isdigit():
                    known_length = int(content_length)
                if known_length is not None and box_size > known_length:
                    continue_probe = False
            if is_mp4_mime and continue_probe:
                return "MP4"
    except Exception:
        # Container probing is advisory for ordinary streams. Playback keeps
        # its existing transport path when the provider cannot be probed.
        pass
    return "DIRECT"


def _select_eclipse_search_track(tracks: list[dict], title: str, artist: str, requested_fmt: str, isrc: str = "", duration: int | float = 0) -> dict | None:
    """Select the best matching track from Eclipse search results."""
    valid = [t for t in tracks if isinstance(t, dict) and t.get("id")]
    if not valid:
        return None
    norm_title = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    norm_artist = re.sub(r"[^a-z0-9]+", " ", (artist or "").lower()).strip()
    norm_isrc = (isrc or "").strip().upper()
    target_dur = float(duration or 0)

    def identity_and_score(item: dict) -> tuple[bool, tuple[int, int, int, int, int]]:
        item_isrc = str(item.get("isrc") or "").strip().upper()
        isrc_match = 1 if (norm_isrc and item_isrc == norm_isrc) else 0
        if norm_isrc and item_isrc and not isrc_match:
            return False, (0, 0, 0, 0, 0)
        t_title = re.sub(r"[^a-z0-9]+", " ", str(item.get("title") or "").lower()).strip()
        t_artist = re.sub(r"[^a-z0-9]+", " ", str(item.get("artist") or "").lower()).strip()
        exact_title = 2 if (norm_title and t_title == norm_title) else (1 if (norm_title and (norm_title in t_title or t_title in norm_title)) else 0)
        item_dur = float(item.get("duration") or 0)
        dur_match = 1 if (target_dur > 0 and item_dur > 0 and abs(item_dur - target_dur) <= 6) else 0
        artist_match = 1 if (norm_artist and (norm_artist in t_artist or t_artist in norm_artist)) else 0
        atmos_pref = 1 if (requested_fmt == "EAC3_JOC" and (item.get("atmosAvailable") or item.get("isAtmos"))) else 0
        duration_conflict = target_dur > 0 and item_dur > 0 and abs(item_dur - target_dur) > 8
        identity_match = bool(isrc_match or (exact_title and artist_match and not duration_conflict))
        return identity_match, (isrc_match, exact_title, dur_match, artist_match, atmos_pref)

    matching = [(item, score) for item in valid for matches, score in [identity_and_score(item)] if matches]
    return max(matching, key=lambda row: row[1])[0] if matching else None


def _finalize_stream_reference_inventory(
    streams: list[dict], stream_store: dict, account_generation: int, completed_at: float,
) -> bool:
    """Verify returned handles and refresh TTL without exceeding provider expiry."""
    references_complete = True
    for descriptor in streams:
        entry = stream_store.get(descriptor.get("streamRef"))
        if not isinstance(entry, dict) or entry.get("accountGeneration") != account_generation:
            descriptor["availability"] = "reference_unavailable"
            descriptor["playbackCompatibility"] = "stream_reference_not_retained"
            descriptor["streamRef"] = None
            descriptor["referenceExpiresAt"] = None
            references_complete = False
            continue
        provider_expiry = entry.get("expiresAt")
        try:
            provider_expiry = float(provider_expiry) if provider_expiry is not None else completed_at + 120.0
        except (TypeError, ValueError, OverflowError):
            provider_expiry = completed_at
        entry["expires_at"] = min(completed_at + 120.0, provider_expiry)
        descriptor["referenceExpiresAt"] = entry["expires_at"]
        descriptor["expired"] = provider_expiry <= completed_at
        if descriptor["expired"]:
            descriptor["playbackCompatibility"] = "expired"
    return references_complete


def _discover_eclipse_addon_streams_impl(
    addon_manifest_url: str,
    *,
    addon_id: str,
    addon_name: str,
    track_id: str = "",
    title: str = "",
    artist: str = "",
    isrc: str = "",
    duration: int | float = 0,
    provider_key: str | None = None,
    timeout_ms: int = 120_000,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Enumerate declared addon quality options independently of local playback.

    Raw media URLs and authenticated request headers remain inside the service.
    The response is a metadata inventory and explicitly distinguishes incomplete
    network results from a successfully returned stream descriptor.
    """
    try:
        timeout_ms = max(1, min(120_000, int(timeout_ms)))
    except (TypeError, ValueError, OverflowError):
        timeout_ms = 120_000
    discovery_deadline = time.monotonic() + timeout_ms / 1000.0
    def canceled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def canceled_result() -> dict:
        return {"complete": False, "reason": "inventory_canceled", "streams": [], "attempts": []}

    if canceled():
        return canceled_result()
    manifest_remaining = discovery_deadline - time.monotonic()
    if manifest_remaining <= 0:
        return {"complete": False, "reason": "discovery_deadline_exceeded", "streams": [], "attempts": []}
    manifest = _get_eclipse_manifest(addon_manifest_url, timeout=min(6.0, manifest_remaining))
    if canceled():
        return canceled_result()
    if time.monotonic() >= discovery_deadline:
        return {"complete": False, "reason": "discovery_deadline_exceeded", "streams": [], "attempts": []}
    if not isinstance(manifest, dict):
        return {
            "complete": False,
            "reason": _eclipse_manifest_failure_reason(addon_manifest_url),
            "streams": [],
            "attempts": [],
        }
    raw_manifest_id = manifest.get("id")
    manifest_id = raw_manifest_id.strip() if isinstance(raw_manifest_id, str) else ""
    requested_addon_id = addon_id.strip() if isinstance(addon_id, str) else ""
    if (not manifest_id or len(manifest_id) > 256 or any(ord(char) < 0x20 for char in manifest_id)
            or not requested_addon_id or len(requested_addon_id) > 256
            or any(ord(char) < 0x20 for char in requested_addon_id)):
        return {"complete": False, "reason": "manifest_identity_missing_or_invalid", "streams": [], "attempts": []}
    if requested_addon_id != manifest_id:
        return {"complete": False, "reason": "manifest_addon_identity_mismatch", "streams": [], "attempts": []}
    normalized_id = requested_addon_id
    normalized_name = str(addon_name or manifest.get("name") or normalized_id or "Eclipse add-on").strip()[:256]
    capabilities = declared_addon_capabilities(manifest)
    platform_blocked_profiles = (
        [profile for profile in capabilities.get("playbackProfiles", []) if profile == "apple-fps-alac-v1"]
        if normalized_id == "app.eclipsemusic.gravity" else []
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "X-Client-Platform": "web",
        "Origin": "https://eclipsemusic.app",
        "Referer": "https://eclipsemusic.app/",
    }
    if time.monotonic() >= discovery_deadline:
        return {"complete": False, "reason": "discovery_deadline_exceeded", "streams": [], "attempts": []}
    token = _get_eclipse_addon_access_token(normalized_id, deadline=discovery_deadline)
    if time.monotonic() >= discovery_deadline:
        return {"complete": False, "reason": "discovery_deadline_exceeded", "streams": [], "attempts": []}
    # Access-token acquisition verifies the saved Eclipse session and may
    # initialize or rotate it, incrementing _ECLIPSE_AUTH_GENERATION. Anchor
    # this inventory afterward so the cold first request does not reject
    # itself as an account change.
    with _ECLIPSE_AUTH_LOCK:
        account_generation = _ECLIPSE_AUTH_GENERATION
        if token:
            addon_tokens = _ECLIPSE_AUTH_CACHE.get("addon_tokens")
            token_entry = addon_tokens.get(normalized_id) if isinstance(addon_tokens, dict) else None
            token_generation = token_entry.get("accountGeneration") if isinstance(token_entry, dict) else None
            if provider_key is not None and (
                not isinstance(token_entry, dict)
                or token_entry.get("token") != token
                or not isinstance(token_generation, int)
                or token_generation != account_generation
            ):
                return {"complete": False, "reason": "account_changed_during_discovery", "streams": [], "attempts": []}
            if (isinstance(token_entry, dict) and token_entry.get("token") == token
                    and isinstance(token_generation, int)):
                if token_generation != account_generation:
                    return {"complete": False, "reason": "account_changed_during_discovery", "streams": [], "attempts": []}
        if provider_key is not None:
            # Bind the supplied manifest and addon ID to the currently cached
            # account connection. This prevents a provider record resolved for
            # one account from being replayed with another account's fresh token.
            # An addon access token is optional at the protocol boundary because
            # some connected addons expose public resources; when one is present,
            # it must have an exact current-generation cache binding. A missing
            # token still requires a verified, current-generation account link.
            account_cache = _ECLIPSE_AUTH_CACHE.get("account_addons")
            cached_account = account_cache.get("current") if isinstance(account_cache, dict) else None
            account_result = cached_account.get("result") if isinstance(cached_account, dict) else None
            current_jwt = _ECLIPSE_AUTH_CACHE.get("jwt")
            current_key = hashlib.sha256(current_jwt.encode("utf-8")).hexdigest() if isinstance(current_jwt, str) and current_jwt else None
            connection_matches = (
                isinstance(cached_account, dict)
                and cached_account.get("token_key") == current_key
                and cached_account.get("accountGeneration") == account_generation
                and time.time() < float(cached_account.get("expires_at") or 0)
                and isinstance(account_result, dict)
                and any(
                    isinstance(item, dict)
                    and item.get("addonId") == normalized_id
                    and item.get("manifestUrl") == addon_manifest_url
                    and item.get("isEnabled") is True
                    for item in account_result.get("addons", [])
                )
            )
            if not connection_matches:
                return {"complete": False, "reason": "connected_addon_changed", "streams": [], "attempts": []}
        cooldowns = _ECLIPSE_AUTH_CACHE.setdefault("addon_cooldowns", {})
        cooldown = cooldowns.get(normalized_id) if isinstance(cooldowns, dict) else None
        if isinstance(cooldown, dict) and cooldown.get("accountGeneration") == account_generation:
            retry_at = float(cooldown.get("retry_at") or 0)
            if retry_at > time.time():
                return {
                    "complete": False,
                    "reason": "upstream_rate_limited",
                    "retryAfterSeconds": max(1, int(retry_at - time.time())),
                    "streams": [], "attempts": [],
                }
            cooldowns.pop(normalized_id, None)
    if token:
        headers["X-Eclipse-Access"] = token

    last_retry_after = None

    def fetch_json(url: str, timeout: float = 6.0):
        nonlocal token, last_retry_after
        last_retry_after = None
        for attempt in range(3):
            if canceled():
                return None, "inventory_canceled"
            remaining = discovery_deadline - time.monotonic()
            if remaining <= 0:
                return None, "discovery_deadline_exceeded"
            with _ECLIPSE_AUTH_LOCK:
                if _ECLIPSE_AUTH_GENERATION != account_generation:
                    return None, "account_changed"
            request = urllib.request.Request(url, headers=headers)
            request_deadline = time.monotonic() + min(timeout, remaining)
            try:
                raw = _bounded_eclipse_response(
                    request, 1_000_000, request_deadline, min(timeout, remaining),
                    expected_generation=account_generation,
                )
                with _ECLIPSE_AUTH_LOCK:
                    if _ECLIPSE_AUTH_GENERATION != account_generation:
                        return None, "account_changed"
                if len(raw) > 1_000_000:
                    return None, "response_too_large"
                parsed = json.loads(raw.decode("utf-8"))
                return (parsed, None) if isinstance(parsed, (dict, list)) else (None, "invalid_response")
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry_header = exc.headers.get("Retry-After") if exc.headers else None
                    if isinstance(retry_header, str) and len(retry_header) <= 128:
                        last_retry_after = retry_header.strip()
                    try:
                        cooldown_seconds = max(1.0, min(3600.0, float(last_retry_after)))
                    except (TypeError, ValueError, OverflowError):
                        try:
                            retry_at = email.utils.parsedate_to_datetime(last_retry_after).timestamp()
                            cooldown_seconds = max(1.0, min(3600.0, retry_at - time.time()))
                        except (TypeError, ValueError, OverflowError, OSError):
                            cooldown_seconds = 60.0
                    last_retry_after = str(int(cooldown_seconds))
                    with _ECLIPSE_AUTH_LOCK:
                        current_cooldowns = _ECLIPSE_AUTH_CACHE.setdefault("addon_cooldowns", {})
                        if (_ECLIPSE_AUTH_GENERATION == account_generation
                                and isinstance(current_cooldowns, dict)):
                            current_cooldowns[normalized_id] = {
                                "accountGeneration": account_generation,
                                "retry_at": time.time() + cooldown_seconds,
                            }
                if exc.code == 503 and attempt < 2:
                    retry_header = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        retry_seconds = max(0.0, min(30.0, float(retry_header)))
                    except (TypeError, ValueError, OverflowError):
                        try:
                            retry_at = email.utils.parsedate_to_datetime(retry_header).timestamp()
                            retry_seconds = max(0.0, min(30.0, retry_at - time.time()))
                        except (TypeError, ValueError, OverflowError, OSError):
                            retry_seconds = 1.0
                    last_retry_after = str(int(retry_seconds))
                    if retry_seconds >= discovery_deadline - time.monotonic():
                        return None, "discovery_deadline_exceeded"
                    if retry_seconds > 0:
                        if cancel_event is not None:
                            if cancel_event.wait(retry_seconds):
                                return None, "inventory_canceled"
                        else:
                            time.sleep(retry_seconds)
                    continue
                if exc.code in (401, 403) and attempt == 0:
                    refreshed = _get_eclipse_addon_access_token(
                        normalized_id, force_refresh=True, deadline=discovery_deadline
                    )
                    if refreshed:
                        with _ECLIPSE_AUTH_LOCK:
                            if _ECLIPSE_AUTH_GENERATION != account_generation:
                                return None, "account_changed"
                        token = refreshed
                        headers["X-Eclipse-Access"] = refreshed
                        continue
                return None, f"http_{exc.code}"
            except TimeoutError:
                return None, "discovery_deadline_exceeded"
            except Exception as exc:
                return None, type(exc).__name__.lower()
        return None, "authorization_failed"

    selected_track = None
    lookup_errors = []
    clean_track_id = str(track_id or "").strip()[:512]
    if clean_track_id:
        selected_track = {"id": clean_track_id}
    elif title or isrc:
        # Eclipse recommends exact ISRC resolution before fuzzy search.
        if isrc and capabilities["resolveISRC"] is True:
            isrc_endpoint = _eclipse_worker_url(addon_manifest_url, "resolve-isrc", {"isrc": isrc})
            resolved, resolve_error = fetch_json(isrc_endpoint, timeout=6.0)
            if resolve_error:
                lookup_errors.append("resolve_isrc_" + resolve_error)
                if resolve_error in {"http_401", "http_403", "http_429", "account_changed", "discovery_deadline_exceeded"}:
                    return {"complete": False, "reason": "track_resolve_isrc_" + resolve_error, "streams": [], "attempts": []}
            elif isinstance(resolved, dict):
                item = resolved.get("item") if isinstance(resolved.get("item"), dict) else resolved
                resolved_id = item.get("trackId") or item.get("id")
                if resolved_id is not None:
                    selected_track = {**item, "id": resolved_id}
        if selected_track is None and capabilities["search"] is not False:
            query = f"{title} {artist}".strip() if title else str(isrc)
            payload, search_error = fetch_json(
                _eclipse_worker_url(addon_manifest_url, "search", {"q": query}), timeout=7.0
            )
            if search_error:
                lookup_errors.append("search_" + search_error)
                if search_error in {"http_401", "http_403", "http_429", "account_changed", "discovery_deadline_exceeded"}:
                    return {"complete": False, "reason": "track_search_" + search_error, "streams": [], "attempts": []}
            else:
                rows = payload.get("tracks") or payload.get("results") or [] if isinstance(payload, dict) else payload
                selected_track = _select_eclipse_search_track(
                    rows if isinstance(rows, list) else [], title, artist, "", isrc=isrc, duration=duration
                )
        if selected_track is None and capabilities["resolveIdentity"] is True:
            duration_value = _renderer_duration(duration)
            duration_ms = int(round(duration_value * 1000)) if duration_value is not None else None
            params = {"isrc": isrc or "", "title": title or "", "artist": artist or ""}
            if duration_ms is not None:
                params["durationMs"] = str(duration_ms)
            resolved, resolve_error = fetch_json(
                _eclipse_worker_url(addon_manifest_url, "resolve", params), timeout=6.0
            )
            if resolve_error:
                lookup_errors.append("resolve_" + resolve_error)
                if resolve_error in {"http_401", "http_403", "http_429", "account_changed", "discovery_deadline_exceeded"}:
                    return {"complete": False, "reason": "track_resolve_" + resolve_error, "streams": [], "attempts": []}
            if not resolve_error and isinstance(resolved, dict):
                item = resolved.get("item") if isinstance(resolved.get("item"), dict) else resolved
                resolved_id = item.get("trackId") or item.get("id")
                if resolved_id is not None:
                    selected_track = {**item, "id": resolved_id}
    if not isinstance(selected_track, dict) or selected_track.get("id") is None:
        return {
            "complete": False,
            "reason": "track_not_found",
            "trackLookupErrors": lookup_errors,
            "streams": [], "attempts": [],
        }

    resolved_track_id = str(selected_track["id"]).strip()[:512]
    quality_requests = quality_candidates(manifest, include_default=True) if capabilities["stream"] is not False else []
    quality_labels_by_selector = quality_selector_label_map(manifest)
    quality_limit_exceeded = quality_candidate_limit_exceeded(manifest, include_default=True)
    quality_options_unsafe_flag = quality_options_unsafe(manifest)
    stream_request_plan, protocol_request_truncated = build_stream_request_plan(
        manifest, quality_requests, limit=MAX_ECLIPSE_STREAM_REQUESTS,
        addon_id=normalized_id,
    )
    streams: list[dict] = []
    attempts: list[dict] = []
    incomplete = quality_options_unsafe_flag or not capabilities.get("capabilitiesComplete", True)
    stream_identity_refs: dict[str, dict] = {}
    stream_results_truncated = False
    malformed_stream_candidates = False
    media_variants_complete = True
    media_manifest_inspection_count = 0
    media_manifest_inspection_cache: dict[str, tuple[bool | None, list[dict], bool]] = {}

    def retain_native_descriptor(descriptor: dict, native: dict) -> bool:
        with _ECLIPSE_AUTH_LOCK:
            if _ECLIPSE_AUTH_GENERATION != account_generation:
                return False
            stream_store = _ECLIPSE_AUTH_CACHE.setdefault("discovered_streams", {})
            if not isinstance(stream_store, dict):
                return False
            if len(stream_store) > MAX_ECLIPSE_STREAM_REQUESTS:
                for old_ref, entry in list(stream_store.items()):
                    if not isinstance(entry, dict) or time.time() >= float(entry.get("expires_at") or 0):
                        stream_store.pop(old_ref, None)
            while len(stream_store) >= MAX_ECLIPSE_STREAM_REQUESTS:
                oldest_ref = next(iter(stream_store), None)
                if oldest_ref is None:
                    break
                stream_store.pop(oldest_ref, None)
            raw_descriptor = native.get("descriptor") if isinstance(native, dict) else {}
            if isinstance(raw_descriptor, dict):
                raw_descriptor = {key: value for key, value in raw_descriptor.items() if key not in {"streams", "_parent"}}
            stream_store[descriptor["streamRef"]] = {
                "accountGeneration": account_generation,
                "providerKey": provider_key,
                "addonId": normalized_id,
                "manifestHash": hashlib.sha256(addon_manifest_url.encode("utf-8")).hexdigest(),
                "expiresAt": descriptor.get("expiresAt"),
                "expires_at": min(
                    time.time() + 120.0,
                    float(descriptor["expiresAt"]) if descriptor.get("expiresAt") else time.time() + 120.0,
                ),
                "addonRequestHeaders": dict(headers),
                "native": {
                    "url": native.get("url"),
                    "descriptor": raw_descriptor,
                    "effectiveProtection": native.get("effectiveProtection"),
                },
                "descriptor": dict(descriptor),
            }
            return True

    def add_stream(normalized: tuple[dict, dict], requested_quality: object, request_settings: dict) -> bool:
        nonlocal stream_results_truncated, media_variants_complete, media_manifest_inspection_count
        descriptor, native = normalized
        evidence = {
            "quality": descriptor.get("requestedQuality"),
            "settings": descriptor.get("requestSettings") or {},
            "qualityMatch": descriptor.get("qualityMatch"),
            "qualityMatchStatus": descriptor.get("qualityMatchStatus", "unreported"),
        }
        descriptor["requestedQualities"] = [evidence]
        raw_descriptor = native.get("descriptor") if isinstance(native.get("descriptor"), dict) else {}
        raw_descriptor = {key: value for key, value in raw_descriptor.items() if key not in {"streams", "_parent"}}
        native = {
            "url": native.get("url"),
            "descriptor": raw_descriptor,
            "effectiveProtection": native.get("effectiveProtection"),
        }
        native_json = json.dumps(native, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        identity = hashlib.sha256(native_json.encode("utf-8")).hexdigest()
        existing = stream_identity_refs.get(identity)
        if existing is not None:
            if evidence not in existing["requestedQualities"]:
                existing["requestedQualities"].append(evidence)
            existing["qualityMatch"] = (
                existing["requestedQualities"][0].get("qualityMatch")
                if len(existing["requestedQualities"]) == 1 else None
            )
            existing["qualityMatchStatus"] = (
                existing["requestedQualities"][0].get("qualityMatchStatus", "unreported")
                if len(existing["requestedQualities"]) == 1 else "ambiguous"
            )
            with _ECLIPSE_AUTH_LOCK:
                store = _ECLIPSE_AUTH_CACHE.get("discovered_streams")
                entry = store.get(existing["streamRef"]) if isinstance(store, dict) else None
                if isinstance(entry, dict):
                    entry["descriptor"] = dict(existing)
                else:
                    if not retain_native_descriptor(descriptor, native):
                        return False
                    existing["streamRef"] = descriptor["streamRef"]
                    existing["availability"] = "returned"
                    existing["playbackCompatibility"] = descriptor.get("playbackCompatibility")
                    rebound = store.get(existing["streamRef"]) if isinstance(store, dict) else None
                    if isinstance(rebound, dict):
                        rebound["descriptor"] = dict(existing)
            return True
        if len(streams) >= MAX_STREAM_VARIANTS:
            stream_results_truncated = True
            return True
        manifest_type_value = descriptor.get("manifestType")
        manifest_type = str(manifest_type_value).lower() if isinstance(manifest_type_value, str) and manifest_type_value else None
        manifest_url = native.get("url")
        media_path = urllib.parse.urlsplit(manifest_url).path.lower() if isinstance(manifest_url, str) else ""
        media_suffix = os.path.splitext(media_path)[1]
        known_direct_suffixes = {
            ".mp3", ".flac", ".aac", ".aiff", ".aif", ".ogg", ".opus", ".wav",
            ".wave", ".ac3", ".eac3", ".ec3", ".ac4", ".m4a", ".mp2", ".mpa",
        }
        inspect_adaptive = (
            manifest_type in {"hls", "dash"}
            or not media_suffix
            or media_suffix not in known_direct_suffixes
        )
        if descriptor.get("urlAvailable") is True and isinstance(manifest_url, str) and inspect_adaptive:
            raw_headers = {}
            for key in ("headers", "httpHeaders", "requestHeaders"):
                if isinstance(raw_descriptor.get(key), dict):
                    raw_headers = raw_descriptor[key]
                    break
            canonical_headers = {
                str(key).casefold(): value
                for key, value in _validated_eclipse_manifest_headers(raw_headers).items()
            }
            inspection_context = json.dumps({
                "url": manifest_url,
                "manifestType": manifest_type,
                "headers": canonical_headers,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            manifest_key = hashlib.sha256(inspection_context.encode("utf-8")).hexdigest()
            cached_inspection = media_manifest_inspection_cache.get(manifest_key)
            if cached_inspection is None:
                if media_manifest_inspection_count >= MAX_ECLIPSE_MEDIA_MANIFESTS_PER_INVENTORY:
                    media_variants_complete = False
                    descriptor["mediaVariants"] = []
                    descriptor["mediaVariantsInspectionComplete"] = False
                    descriptor["manifestProtectionStatus"] = "unknown"
                else:
                    media_manifest_inspection_count += 1
                    remaining = discovery_deadline - time.monotonic()
                    variants = []
                    variants_complete_result = []
                    manifest_protected = None
                    if remaining > 0.05:
                        manifest_protected = _eclipse_manifest_protection_status(
                            manifest_url, manifest_type,
                            timeout=min(8.0, remaining),
                            expected_generation=account_generation,
                            request_headers=raw_headers,
                            media_variants_out=variants,
                            media_variants_complete_out=variants_complete_result,
                        )
                    variants_parse_complete = (
                        variants_complete_result[0] if variants_complete_result
                        else manifest_protected is not None
                    )
                    media_manifest_inspection_cache[manifest_key] = (
                        manifest_protected, variants, variants_parse_complete,
                    )
                    cached_inspection = media_manifest_inspection_cache[manifest_key]
            if cached_inspection is not None:
                manifest_protected, variants, variants_parse_complete = cached_inspection
                safe_variants = [dict(item) for item in variants]
                parent_protection = descriptor.get("protection")
                if isinstance(parent_protection, dict) and parent_protection.get("status") == "protected":
                    for item in safe_variants:
                        item["protectionStatus"] = "protected"
                        item["protectionSystems"] = list(parent_protection.get("systems") or [])
                descriptor["mediaVariants"] = safe_variants
                descriptor["mediaVariantsInspectionComplete"] = manifest_protected is not None and variants_parse_complete
                descriptor["manifestProtectionStatus"] = (
                    "protected" if manifest_protected is True
                    else "no_protection_signal_found" if manifest_protected is False
                    else "unknown"
                )
                if manifest_protected is None or not variants_parse_complete:
                    media_variants_complete = False
        if not retain_native_descriptor(descriptor, native):
            return False
        stream_identity_refs[identity] = descriptor
        streams.append(descriptor)
        return True

    # A search streamURL is a real addon response and may be the addon's default
    # quality. Keep it as an independent descriptor; it does not stand in for
    # any explicitly declared quality option.
    direct = selected_track.get("streamURL") or selected_track.get("streamUrl")
    if isinstance(direct, str) and direct:
        normalized = _normalize_stream(
            {**selected_track, "url": direct}, addon_id=normalized_id, addon_name=normalized_name,
            track_id=resolved_track_id, requested_quality=None, request_settings={}, source="search",
        )
        if normalized:
            if not add_stream(normalized, None, {}):
                return {"complete": False, "reason": "account_changed_during_discovery", "streams": [], "attempts": []}
        else:
            malformed_stream_candidates = True
            incomplete = True
    elif direct:
        malformed_stream_candidates = True
        incomplete = True

    for candidate in stream_request_plan:
        if canceled():
            return canceled_result()
        remaining = discovery_deadline - time.monotonic()
        if remaining <= 0.05:
            incomplete = True
            attempts.append({
                "requestedQuality": candidate.get("requestedQuality"),
                "qualityKey": candidate.get("qualityKey"),
                "negotiation": candidate.get("negotiation") or {},
                "status": "not_attempted",
                "availability": "unknown",
                "reason": "discovery_deadline_exceeded",
            })
            continue
        params = dict(candidate.get("settings") or {})
        quality_keys = candidate.get("qualityKey")
        quality_keys = quality_keys if isinstance(quality_keys, list) else [quality_keys]
        quality_selector_values = [
            params[key] for key in quality_keys
            if isinstance(key, str) and key in params and isinstance(params[key], str)
        ]
        stream_url = _eclipse_worker_url(
            addon_manifest_url, f"stream/{urllib.parse.quote(resolved_track_id, safe='')}", params
        )
        payload, error = fetch_json(stream_url, timeout=min(8.0, max(0.1, remaining)))
        attempt = {
            "requestedQuality": candidate.get("requestedQuality"),
            "qualityKey": candidate.get("qualityKey"),
            "negotiation": candidate.get("negotiation") or {},
            "status": "failed" if error else "no_stream_returned",
            "reason": error,
        }
        if error:
            incomplete = True
            if error in {"http_401", "http_403", "http_429", "response_too_large"}:
                attempt["availability"] = "unknown"
            attempts.append(attempt)
            if error in {"http_401", "http_403", "http_429"}:
                if error == "http_429" and last_retry_after is not None:
                    try:
                        attempt["retryAfterSeconds"] = max(1, min(3600, int(float(last_retry_after))))
                    except (ValueError, OverflowError):
                        attempt["retryAfterSeconds"] = 60
                for pending in stream_request_plan[len(attempts):]:
                    attempts.append({
                        "requestedQuality": pending.get("requestedQuality"),
                        "qualityKey": pending.get("qualityKey"),
                        "negotiation": pending.get("negotiation") or {},
                        "status": "not_attempted",
                        "availability": "unknown",
                        "reason": "stopped_after_upstream_authorization_or_rate_limit",
                    })
                break
            continue
        normalized_any = False
        if stream_payload_truncated(payload):
            stream_results_truncated = True
            incomplete = True
        if stream_payload_has_malformed_items(payload):
            malformed_stream_candidates = True
            incomplete = True
        for item in flatten_stream_payload(payload):
            if canceled():
                return canceled_result()
            parent = item.pop("_parent", None)
            normalized = _normalize_stream(
                item,
                addon_id=normalized_id,
                addon_name=normalized_name,
                track_id=resolved_track_id,
                requested_quality=candidate.get("requestedQuality"),
                request_settings=candidate.get("publicSettings") or {},
                quality_selector_values=quality_selector_values,
                quality_selector_labels=quality_labels_by_selector,
                parent=parent,
            )
            if normalized:
                if not add_stream(normalized, candidate.get("requestedQuality"), params):
                    return {"complete": False, "reason": "account_changed_during_discovery", "streams": [], "attempts": []}
                normalized_any = True
            else:
                malformed_stream_candidates = True
                incomplete = True
        if normalized_any:
            attempt["status"] = "returned"
            attempt["reason"] = None
            attempt["availability"] = "returned"
        elif (
            isinstance(payload, list) and payload
            or isinstance(payload, dict) and (
                "streams" in payload and (
                    not isinstance(payload.get("streams"), list) or bool(payload.get("streams"))
                )
                or "streams" not in payload and bool(payload)
            )
        ):
            attempt["status"] = "invalid_stream_descriptors"
            attempt["reason"] = "no_valid_stream_descriptor"
            attempt["availability"] = "unknown"
            malformed_stream_candidates = True
            incomplete = True
        attempts.append(attempt)
    with _ECLIPSE_AUTH_LOCK:
        if canceled():
            return canceled_result()
        if _ECLIPSE_AUTH_GENERATION != account_generation:
            return {"complete": False, "reason": "account_changed_during_discovery", "streams": [], "attempts": []}
        # Start the local opaque-reference TTL when discovery completes, not
        # when an early stream is found. Provider expiry remains authoritative.
        completed_at = time.time()
        stream_store = _ECLIPSE_AUTH_CACHE.get("discovered_streams")
        native_references_complete = _finalize_stream_reference_inventory(
            streams,
            stream_store if isinstance(stream_store, dict) else {},
            account_generation,
            completed_at,
        )
    enumeration_complete = (
        not incomplete and not quality_limit_exceeded and not protocol_request_truncated
        and not stream_results_truncated and media_variants_complete
        and not platform_blocked_profiles and native_references_complete
    )
    return {
        "complete": enumeration_complete,
        "completeScope": "declared quality options and supported playback-negotiation requests plus inspected HLS/DASH audio representations; provider-wide and account-hidden coverage is unknown",
        "providerCoverageStatus": "unknown",
        "completeness": {
            "manifestSettingsComplete": (
                isinstance(manifest.get("settings"), list)
                and not unclassified_select_settings(manifest)
            ),
            "addonCapabilitiesComplete": capabilities.get("capabilitiesComplete", True),
            "malformedAddonCapabilityFields": capabilities.get("malformedCapabilityFields", []),
            "unclassifiedSelectSettings": unclassified_select_settings(manifest),
            "qualityRequestsComplete": (
                not incomplete and not quality_limit_exceeded
                and not protocol_request_truncated and not stream_results_truncated
                and not malformed_stream_candidates
            ),
            "qualityEnumerationTruncated": quality_limit_exceeded,
            "protocolRequestEnumerationTruncated": protocol_request_truncated,
            "qualityOptionsUnsafe": quality_options_unsafe_flag,
            "streamResultsTruncated": stream_results_truncated,
            "malformedStreamCandidates": malformed_stream_candidates,
            "nativeReferencesComplete": native_references_complete,
            "mediaVariantsComplete": media_variants_complete,
            "paginationComplete": None,
            "coverage": "declared_addon_quality_options_playback_profiles_returned_descriptors_and_inspected_adaptive_audio_representations",
            "providerExhaustivenessVerified": False,
            "platformBlockedProfiles": platform_blocked_profiles,
            "providerParityVerified": False,
        },
        "reason": (
            None if enumeration_complete else
            "quality_option_product_exceeds_discovery_limit" if quality_limit_exceeded else
            "playback_negotiation_matrix_exceeds_discovery_limit" if protocol_request_truncated else
            "adaptive_manifest_rendition_inspection_incomplete" if not media_variants_complete else
            "protected_playback_profile_requires_unavailable_fairplay" if platform_blocked_profiles else
            "native_stream_reference_retention_incomplete" if not native_references_complete else
            "unsafe_quality_option_metadata_omitted" if quality_options_unsafe_flag else
            "one_or_more_quality_requests_unverified"
        ),
        "trackId": _renderer_text(resolved_track_id, 256),
        "track": {
            "title": _renderer_text(selected_track.get("title") or selected_track.get("name") or title, 512),
            "artist": _renderer_text(selected_track.get("artist") or selected_track.get("artistName") or artist, 512),
            "isrc": _renderer_text(selected_track.get("isrc") or isrc, 64),
            "duration": _renderer_duration(selected_track.get("duration") or duration),
        },
        "addonCapabilities": capabilities,
        "qualityOptions": [
            {key: value for key, value in option.items() if key != "settings"}
            for option in quality_requests
        ],
        "streams": streams,
        "attempts": attempts,
        "settingsSource": "manifest_quality_options; Eclipse account-specific settings are not exposed by the account addon connection response",
        "urlPolicy": "media URLs and authorization headers are not serialized to the renderer",
    }


def discover_eclipse_addon_streams(addon_manifest_url: str, *, timeout_ms: int = 120_000,
                                   cancel_event: threading.Event | None = None, **kwargs) -> dict:
    """Run at most two authenticated addon discoveries concurrently."""
    if not _ECLIPSE_STREAM_DISCOVERY_SEMAPHORE.acquire(blocking=False):
        return {"complete": False, "reason": "discovery_capacity_reached", "streams": [], "attempts": []}
    try:
        return _discover_eclipse_addon_streams_impl(
            addon_manifest_url, timeout_ms=timeout_ms, cancel_event=cancel_event, **kwargs,
        )
    finally:
        _ECLIPSE_STREAM_DISCOVERY_SEMAPHORE.release()


def get_discovered_eclipse_stream(stream_ref: str, provider_key: str) -> dict | None:
    """Look up an opaque native-only descriptor after checking auth/provider scope."""
    if not isinstance(stream_ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", stream_ref):
        return None
    if not isinstance(provider_key, str) or not re.fullmatch(r"eclipse:[0-9a-f]{20}", provider_key):
        return None
    with _ECLIPSE_AUTH_LOCK:
        store = _ECLIPSE_AUTH_CACHE.get("discovered_streams")
        entry = store.get(stream_ref) if isinstance(store, dict) else None
        if not isinstance(entry, dict):
            return None
        if entry.get("providerKey") != provider_key:
            return None
        if (entry.get("accountGeneration") != _ECLIPSE_AUTH_GENERATION
                or time.time() >= float(entry.get("expires_at") or 0)):
            store.pop(stream_ref, None)
            return None
        native_entry = dict(entry)
    provider_order, addon_providers = _ordered_resolution_providers(load_settings())
    provider = addon_providers.get(provider_key)
    if provider_key not in provider_order or not isinstance(provider, dict):
        with _ECLIPSE_AUTH_LOCK:
            store = _ECLIPSE_AUTH_CACHE.get("discovered_streams")
            if isinstance(store, dict):
                store.pop(stream_ref, None)
        return None
    manifest_url = provider.get("manifestUrl")
    if (not isinstance(manifest_url, str)
            or hashlib.sha256(manifest_url.encode("utf-8")).hexdigest() != native_entry.get("manifestHash")):
        return None
    with _ECLIPSE_AUTH_LOCK:
        store = _ECLIPSE_AUTH_CACHE.get("discovered_streams")
        current_entry = store.get(stream_ref) if isinstance(store, dict) else None
        if (not isinstance(current_entry, dict)
                or current_entry.get("providerKey") != provider_key
                or current_entry.get("accountGeneration") != _ECLIPSE_AUTH_GENERATION
                or current_entry.get("accountGeneration") != native_entry.get("accountGeneration")
                or time.time() >= float(current_entry.get("expires_at") or 0)
                or current_entry.get("manifestHash") != native_entry.get("manifestHash")):
            return None
        return copy.deepcopy(current_entry)


def _tidal_alternate_track_matches(original, candidate) -> bool:
    """Accept only the same recording when expanding TIDAL album editions."""
    if getattr(candidate, "id", None) == getattr(original, "id", None):
        return False
    normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    original_title = normalize(getattr(original, "name", ""))
    candidate_title = normalize(getattr(candidate, "name", ""))
    if not original_title or candidate_title != original_title:
        return False
    try:
        if abs(float(getattr(candidate, "duration", 0)) - float(getattr(original, "duration", 0))) > 6:
            return False
    except (TypeError, ValueError):
        return False

    original_isrc = str(getattr(original, "isrc", "") or "").strip().upper()
    candidate_isrc = str(getattr(candidate, "isrc", "") or "").strip().upper()
    if original_isrc and candidate_isrc:
        return original_isrc == candidate_isrc

    def artist_identity(track):
        artist = getattr(track, "artist", None)
        primary = getattr(artist, "name", "") if artist is not None else ""
        return normalize(primary)

    original_artist = artist_identity(original)
    candidate_artist = artist_identity(candidate)
    return bool(original_artist and candidate_artist and original_artist == candidate_artist)


def resolve_tido_eclipse_addon(
    addon_manifest_url: str,
    tidal_track_id: int | str,
    requested_fmt: str,
    title: str = "",
    artist: str = "",
    isrc: str = "",
    duration: int | float = 0,
) -> dict | None:
    """Resolve TIDAL streams (EAC3_JOC, HI_RES_LOSSLESS, LOSSLESS) via the Tido Eclipse addon when main API is unauthorized."""
    if requested_fmt == "AC4_IMS":
        return None
    try:
        base = _eclipse_worker_base_url(addon_manifest_url)
        addon_token = _get_eclipse_addon_access_token("com.yourname.tidal.hifi")
        with _ECLIPSE_AUTH_LOCK:
            account_generation = _ECLIPSE_AUTH_GENERATION
            if addon_token:
                token_cache = _ECLIPSE_AUTH_CACHE.get("addon_tokens")
                token_entry = token_cache.get("com.yourname.tidal.hifi") if isinstance(token_cache, dict) else None
                if (not isinstance(token_entry, dict) or token_entry.get("token") != addon_token
                        or token_entry.get("accountGeneration") != account_generation):
                    return None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "X-Client-Platform": "web",
            "Origin": "https://eclipsemusic.app",
            "Referer": "https://eclipsemusic.app/",
        }
        if addon_token:
            headers["X-Eclipse-Access"] = addon_token

        def _fetch_json(url: str, timeout: float = 6.0) -> dict | list | None:
            for attempt in range(2):
                with _ECLIPSE_AUTH_LOCK:
                    if _ECLIPSE_AUTH_GENERATION != account_generation:
                        return None
                req = urllib.request.Request(url, headers=headers)
                try:
                    request_deadline = time.monotonic() + timeout
                    raw = _bounded_eclipse_response(
                        req, 1_000_000, request_deadline, timeout,
                        expected_generation=account_generation,
                    )
                    with _ECLIPSE_AUTH_LOCK:
                        if _ECLIPSE_AUTH_GENERATION != account_generation:
                            return None
                    return json.loads(raw.decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    if exc.code in (401, 403) and attempt == 0:
                        refreshed = _get_eclipse_addon_access_token("com.yourname.tidal.hifi", force_refresh=True)
                        if refreshed:
                            with _ECLIPSE_AUTH_LOCK:
                                if _ECLIPSE_AUTH_GENERATION != account_generation:
                                    return None
                                token_cache = _ECLIPSE_AUTH_CACHE.get("addon_tokens")
                                token_entry = token_cache.get("com.yourname.tidal.hifi") if isinstance(token_cache, dict) else None
                                if (not isinstance(token_entry, dict) or token_entry.get("token") != refreshed
                                        or token_entry.get("accountGeneration") != account_generation):
                                    return None
                            headers["X-Eclipse-Access"] = refreshed
                            continue
                    return None
                except Exception:
                    return None
            return None

        quality_map = {
            "EAC3_JOC": "MAX",
            "HI_RES_LOSSLESS": "HIRES",
            "LOSSLESS": "LOSSLESS",
        }
        quality_param = quality_map.get(requested_fmt)
        if not quality_param:
            return None

        candidate_ids = [str(tidal_track_id)] if tidal_track_id else []
        if title:
            now = time.time()
            search_cache = _ECLIPSE_AUTH_CACHE.setdefault("search_cache", {})
            lookup_key = f"tido|{base}|{(title or '').strip().lower()}|{(artist or '').strip().lower()}"
            cached_tracks = None
            if isinstance(search_cache, dict):
                entry = search_cache.get(lookup_key)
                if (isinstance(entry, dict) and entry.get("accountGeneration") == account_generation
                        and now < float(entry.get("expires_at") or 0)):
                    cached_tracks = entry.get("tracks")
            if not isinstance(cached_tracks, list):
                query = f"{title} {artist}".strip()
                s_res = _fetch_json(_eclipse_worker_url(addon_manifest_url, "search", {"q": query}), timeout=5.0)
                cached_tracks = s_res.get("tracks", []) if isinstance(s_res, dict) else (s_res if isinstance(s_res, list) else [])
                if isinstance(search_cache, dict) and isinstance(cached_tracks, list):
                    with _ECLIPSE_AUTH_LOCK:
                        if _ECLIPSE_AUTH_GENERATION != account_generation:
                            return None
                        search_cache[lookup_key] = {
                            "tracks": cached_tracks, "expires_at": now + 600.0,
                            "accountGeneration": account_generation,
                        }
            if isinstance(cached_tracks, list):
                norm_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
                for item in cached_tracks:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    cid = str(item["id"])
                    if cid in candidate_ids:
                        continue
                    t_title = re.sub(r"[^a-z0-9]+", " ", str(item.get("title") or "").lower()).strip()
                    if norm_title and (t_title == norm_title or norm_title in t_title):
                        if requested_fmt == "EAC3_JOC" and item.get("isAtmos"):
                            candidate_ids.insert(1 if candidate_ids else 0, cid)
                        else:
                            candidate_ids.append(cid)

        for cid in candidate_ids[:4]:
            stream_url = _eclipse_worker_url(addon_manifest_url, f"stream/{urllib.parse.quote(cid)}", {"quality": quality_param, "videosEnabled": "false"})
            s_data = _fetch_json(stream_url, timeout=8.0)
            if not isinstance(s_data, dict) or not s_data.get("url"):
                continue
            with _ECLIPSE_AUTH_LOCK:
                if _ECLIPSE_AUTH_GENERATION != account_generation:
                    return None
            if stream_has_protection_indicators(s_data):
                continue

            raw_codec = str(s_data.get("codec") or "").strip().lower()
            tier = str(s_data.get("tier") or "").strip().upper()
            quality_label = str(s_data.get("quality") or s_data.get("format") or "").strip()
            sample_rate_raw = s_data.get("sampleRate")
            if sample_rate_raw is None:
                sample_rate = 48000 if "eac3" in raw_codec or "ec-3" in raw_codec else 44100
            else:
                sample_rate = _bounded_integer(sample_rate_raw, minimum=8_000, maximum=768_000)
                if sample_rate is None:
                    continue
            bit_depth_raw = s_data.get("bitDepth")
            bit_depth = None if bit_depth_raw is None else _bounded_integer(bit_depth_raw, minimum=1, maximum=64)
            if bit_depth_raw is not None and bit_depth is None:
                continue

            is_atmos = tier == "EAC3_JOC" or raw_codec in {
                "eac3_joc", "e-ac-3 joc", "eac3-joc", "ec-3 joc",
            }
            is_flac = raw_codec in {"flac", "audio/flac"} or tier in {"FLAC", "FLAC_HIRES"}
            if not is_atmos and not is_flac:
                # An Atmos label or generic E-AC-3 codec does not prove JOC;
                # unknown codecs must not be relabelled as lossless FLAC.
                continue
            is_hires = not is_atmos and (tier == "FLAC_HIRES" or sample_rate > 48000 or (bit_depth is not None and bit_depth > 16))

            if is_atmos:
                stream_fmt = "EAC3_JOC"
            elif is_hires:
                stream_fmt = "HI_RES_LOSSLESS"
            else:
                stream_fmt = "LOSSLESS"

            if requested_fmt and stream_fmt != requested_fmt:
                continue

            media_url = str(s_data["url"])
            manifest_type = s_data.get("manifest") if isinstance(s_data.get("manifest"), str) else None
            manifest_protection = _eclipse_manifest_protection_status(
                media_url, manifest_type, expected_generation=account_generation
            )
            if manifest_protection is not False:
                continue
            if str(manifest_type or "").lower() == "dash" or ".mpd" in media_url.lower():
                req_mpd = urllib.request.Request(media_url, headers={"User-Agent": "Mozilla/5.0"})
                mpd_deadline = time.monotonic() + 8.0
                manifest_xml = _bounded_eclipse_response(
                    req_mpd, 4_000_000, mpd_deadline, 8.0,
                    expected_generation=account_generation,
                ).decode("utf-8-sig")
                if dash_manifest_contains_content_protection(manifest_xml):
                    continue
                dash = get_tidal_dash_audio_metadata(manifest_xml=manifest_xml)
                validated_xml = dash.get("manifestXml")
                segment_urls = dash.get("segmentUrls")
                if not validated_xml or not isinstance(segment_urls, list) or len(segment_urls) < 2:
                    continue

                if is_atmos:
                    evidence = verify_eac3_joc_manifest(validated_xml)
                    with _ECLIPSE_AUTH_LOCK:
                        if _ECLIPSE_AUTH_GENERATION != account_generation:
                            return None
                    return {
                        "format": "EAC3_JOC",
                        "codec": "eac3_joc",
                        "sourceAuthProfile": "tido_eclipse_addon",
                        "label": "Dolby Atmos (E-AC-3 JOC)",
                        "uri": segment_urls[0],
                        "transport": "DASH",
                        "sourceCodec": evidence["sourceCodec"],
                        "sourceManifestFormat": evidence["sourceManifestFormat"],
                        "sourceBitrate": evidence["sourceBitrate"],
                        "sourceBitrateBasis": dash.get("sourceBitrateBasis"),
                        "sourceSampleRate": dash.get("sourceSampleRate") or 48000,
                        "sourceChannels": dash.get("sourceChannels") or 6,
                        "sourceBitDepth": dash.get("sourceBitDepth"),
                        "sourceAudioMode": "DOLBY_ATMOS",
                        "sourceProvider": "TIDAL_ECLIPSE",
                        "sourceName": "Tido Eclipse add-on",
                        "manifestXml": validated_xml,
                        "segmentUrls": segment_urls,
                        "requestSession": requests.Session(),
                    }
                else:
                    label = "Hi-Res FLAC" if stream_fmt == "HI_RES_LOSSLESS" else "Lossless FLAC"
                    with _ECLIPSE_AUTH_LOCK:
                        if _ECLIPSE_AUTH_GENERATION != account_generation:
                            return None
                    return {
                        "format": stream_fmt,
                        "codec": "flac",
                        "sourceAuthProfile": "tido_eclipse_addon",
                        "label": label,
                        "uri": segment_urls[0],
                        "transport": "DASH",
                        "sourceCodec": "FLAC",
                        "sourceManifestFormat": dash.get("sourceManifestFormat"),
                        "sourceBitrate": dash.get("sourceBitrate"),
                        "sourceBitrateBasis": dash.get("sourceBitrateBasis"),
                        "sourceSampleRate": dash.get("sourceSampleRate") or sample_rate,
                        "sourceChannels": dash.get("sourceChannels") or 2,
                        "sourceBitDepth": dash.get("sourceBitDepth") or bit_depth or (24 if stream_fmt == "HI_RES_LOSSLESS" else 16),
                        "sourceQuality": stream_fmt,
                        "sourceAudioMode": "STEREO",
                        "sourceProvider": "TIDAL_ECLIPSE",
                        "sourceName": "Tido Eclipse add-on",
                        "manifestXml": validated_xml,
                        "segmentUrls": segment_urls,
                        "requestSession": requests.Session(),
                    }
            else:
                media_probe_headers = {
                    "User-Agent": headers["User-Agent"],
                    "Accept": "*/*",
                }
                probed_br, br_basis = _probe_direct_audio_bitrate(
                    media_url, float(duration or 0), media_probe_headers,
                    expected_generation=account_generation,
                )
                with _ECLIPSE_AUTH_LOCK:
                    if _ECLIPSE_AUTH_GENERATION != account_generation:
                        return None
                return {
                    "format": stream_fmt,
                    "codec": "eac3_joc" if is_atmos else "flac",
                    "sourceAuthProfile": "tido_eclipse_addon",
                    "label": "Dolby Atmos (E-AC-3 JOC)" if is_atmos else ("Hi-Res FLAC" if is_hires else "Lossless FLAC"),
                    "uri": media_url,
                    "transport": "DIRECT",
                    "sourceCodec": "EAC3_JOC" if is_atmos else "FLAC",
                    "sourceBitrate": probed_br or (768000 if is_atmos else 1411200),
                    "sourceBitrateBasis": br_basis or "Estimated",
                    "sourceSampleRate": sample_rate,
                    "sourceChannels": 6 if is_atmos else 2,
                    "sourceBitDepth": None if is_atmos else (bit_depth or (24 if is_hires else 16)),
                    "sourceQuality": quality_label or stream_fmt,
                    "sourceAudioMode": "DOLBY_ATMOS" if is_atmos else "STEREO",
                    "sourceProvider": "TIDAL_ECLIPSE",
                    "sourceName": "Tido Eclipse add-on",
                }
    except Exception as exc:
        print(f"[-] Tido Eclipse addon resolution error ({requested_fmt}): {type(exc).__name__}")
    return None

def resolve_eclipse_addon(addon_manifest_url: str, isrc: str, requested_fmt: str, title: str = "", artist: str = "", duration: int | float = 0, explicit_track_id: str = "", addon_id: str = "", addon_name: str = "") -> dict | None:
    """Resolve streams from Eclipse Music addons adhering strictly to the official Eclipse specification."""
    if requested_fmt == "AC4_IMS":
        return None
    if _is_tido_eclipse_addon(addon_manifest_url):
        return resolve_tido_eclipse_addon(addon_manifest_url, isrc, requested_fmt, title=title, artist=artist, isrc=isrc, duration=duration)
    try:
        base = _eclipse_worker_base_url(addon_manifest_url)
        normalized_addon_id = addon_id.strip()[:256] if isinstance(addon_id, str) else ""
        normalized_addon_name = addon_name.strip()[:256] if isinstance(addon_name, str) else ""
        if not normalized_addon_id:
            manifest = _get_eclipse_manifest(addon_manifest_url)
            normalized_addon_id = str(manifest.get("id") or "").strip()[:256] if isinstance(manifest, dict) else ""
            if not normalized_addon_name and isinstance(manifest, dict):
                normalized_addon_name = str(manifest.get("name") or "").strip()[:256]
        is_gravity_addon = normalized_addon_id.lower() == "app.eclipsemusic.gravity"
        is_amazon = (
            normalized_addon_id.lower() == "com.yourname.amazonmusic"
            or "amazon" in normalized_addon_name.lower()
            or normalized_addon_name.upper() == "AMZM"
        )
        provider_label = "AMAZON" if is_amazon else re.sub(r"[\[\]\r\n]+", " ", normalized_addon_name or normalized_addon_id or "ECLIPSE").strip()[:64]
        addon_token = _get_eclipse_addon_access_token(normalized_addon_id) if normalized_addon_id else None
        with _ECLIPSE_AUTH_LOCK:
            account_generation = _ECLIPSE_AUTH_GENERATION
            if addon_token:
                token_cache = _ECLIPSE_AUTH_CACHE.get("addon_tokens")
                token_entry = token_cache.get(normalized_addon_id) if isinstance(token_cache, dict) else None
                if (not isinstance(token_entry, dict) or token_entry.get("token") != addon_token
                        or token_entry.get("accountGeneration") != account_generation):
                    return None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "X-Client-Platform": "web",
            "Origin": "https://eclipsemusic.app",
            "Referer": "https://eclipsemusic.app/",
        }
        if addon_token:
            headers["X-Eclipse-Access"] = addon_token

        def _fetch_json(url: str, timeout: float = 5.0) -> dict | list | None:
            for attempt in range(2):
                with _ECLIPSE_AUTH_LOCK:
                    if _ECLIPSE_AUTH_GENERATION != account_generation:
                        return None
                req = urllib.request.Request(url, headers=headers)
                try:
                    request_deadline = time.monotonic() + timeout
                    raw = _bounded_eclipse_response(
                        req, 1_000_000, request_deadline, timeout,
                        expected_generation=account_generation,
                    )
                    with _ECLIPSE_AUTH_LOCK:
                        if _ECLIPSE_AUTH_GENERATION != account_generation:
                            return None
                    return json.loads(raw.decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    if exc.code == 415 and is_gravity_addon:
                        print("[-] Eclipse/Gravity rejected the stream request (HTTP 415); cause unverified")
                        return None
                    if exc.code in (401, 403) and attempt == 0:
                        refreshed = _get_eclipse_addon_access_token(normalized_addon_id, force_refresh=True) if normalized_addon_id else None
                        if refreshed:
                            with _ECLIPSE_AUTH_LOCK:
                                if _ECLIPSE_AUTH_GENERATION != account_generation:
                                    return None
                                token_cache = _ECLIPSE_AUTH_CACHE.get("addon_tokens")
                                token_entry = token_cache.get(normalized_addon_id) if isinstance(token_cache, dict) else None
                                if (not isinstance(token_entry, dict) or token_entry.get("token") != refreshed
                                        or token_entry.get("accountGeneration") != account_generation):
                                    return None
                            headers["X-Eclipse-Access"] = refreshed
                            continue
                    return None
                except Exception:
                    return None
            return None

        now = time.time()
        search_cache = _ECLIPSE_AUTH_CACHE.setdefault("search_cache", {})
        stream_cache = _ECLIPSE_AUTH_CACHE.setdefault("stream_cache", {})
        lookup_key = f"{base}|{isrc or ''}|{(title or '').strip().lower()}|{(artist or '').strip().lower()}"

        track_id = None
        matched_track_meta: dict = {}
        if explicit_track_id:
            track_id = str(explicit_track_id).strip()
            # Bind cached identity metadata to this addon's exact lookup key.
            # Track IDs are provider-local and routinely collide across addons.
            if isinstance(search_cache, dict):
                entry = search_cache.get(lookup_key)
                if (isinstance(entry, dict) and entry.get("accountGeneration") == account_generation
                        and entry.get("track_id") == track_id
                        and now < float(entry.get("expires_at") or 0)
                        and isinstance(entry.get("meta"), dict)):
                    matched_track_meta = entry["meta"]
        else:
            cached_lookup = search_cache.get(lookup_key) if isinstance(search_cache, dict) else None
            if (isinstance(cached_lookup, dict)
                    and cached_lookup.get("accountGeneration") == account_generation
                    and now < float(cached_lookup.get("expires_at") or 0)):
                track_id = cached_lookup.get("track_id")
                matched_track_meta = cached_lookup.get("meta") or {}

        # 1. Primary lookup: GET /search?q={query} when title is known (fastest & most reliable on AMZM worker)
        if not track_id and (title or isrc):
            query = f"{title} {artist}".strip() if title else isrc
            search_url = _eclipse_worker_url(addon_manifest_url, "search", {"q": query})
            data = _fetch_json(search_url, timeout=5.0)
            if isinstance(data, dict):
                tracks = data.get("tracks") or data.get("results") or []
            elif isinstance(data, list):
                tracks = data
            else:
                tracks = []
            if isinstance(tracks, list) and tracks:
                chosen = _select_eclipse_search_track(tracks, title, artist, requested_fmt, isrc=isrc, duration=duration)
                if chosen:
                    track_id = chosen.get("id")
                    matched_track_meta = chosen

        # 2. Fallback Spec 7: ISRC resolution -> GET /resolve-isrc/{isrc}
        if not track_id and isrc:
            for isrc_url in (
                _eclipse_worker_url(addon_manifest_url, f"resolve-isrc/{urllib.parse.quote(isrc)}"),
                _eclipse_worker_url(addon_manifest_url, "resolve-isrc", {"isrc": isrc}),
            ):
                data = _fetch_json(isrc_url, timeout=4.0)
                if isinstance(data, dict):
                    track_id = data.get("trackId") or data.get("id")
                    if track_id:
                        break

        # 3. Fallback Spec 11: Universal Resolve -> GET /resolve
        if not track_id and (isrc or title):
            params = {"isrc": isrc or "", "title": title or "", "artist": artist or ""}
            resolve_url = _eclipse_worker_url(addon_manifest_url, "resolve", params)
            data = _fetch_json(resolve_url, timeout=4.0)
            if isinstance(data, dict):
                item = data.get("item")
                if isinstance(item, dict) and item.get("id"):
                    track_id = item.get("id")
                    matched_track_meta = item

        if track_id and isinstance(search_cache, dict):
            with _ECLIPSE_AUTH_LOCK:
                if _ECLIPSE_AUTH_GENERATION != account_generation:
                    return None
                search_cache[lookup_key] = {
                    "track_id": track_id,
                    "meta": matched_track_meta,
                    "expires_at": now + 600.0,
                    "accountGeneration": account_generation,
                }

        # 4. Spec 3: Stream Resolution -> GET /stream/{id}?quality={quality}
        target_id = track_id or isrc
        if not target_id:
            return None

        quality_map = {
            "EAC3_JOC": "Atmos" if is_gravity_addon else "Atmos_EC-3",
            "HI_RES_LOSSLESS": "Max",
            "LOSSLESS": "Max",
        }
        quality_param = quality_map.get(requested_fmt, "Max")
        stream_cache_key = f"{base}|{target_id}|{quality_param}"
        s_data = None
        if isinstance(stream_cache, dict):
            cached_s = stream_cache.get(stream_cache_key)
            if (isinstance(cached_s, dict)
                    and cached_s.get("accountGeneration") == account_generation
                    and now < float(cached_s.get("expires_at") or 0)):
                s_data = cached_s.get("data")

        if not isinstance(s_data, dict):
            stream_url = _eclipse_worker_url(addon_manifest_url, f"stream/{urllib.parse.quote(str(target_id))}", {"quality": quality_param})
            s_data = _fetch_json(stream_url, timeout=6.0)
            if isinstance(s_data, dict) and isinstance(stream_cache, dict):
                with _ECLIPSE_AUTH_LOCK:
                    if _ECLIPSE_AUTH_GENERATION != account_generation:
                        return None
                    stream_cache[stream_cache_key] = {
                        "data": s_data,
                        "expires_at": now + 120.0,
                        "accountGeneration": account_generation,
                    }

        if not isinstance(s_data, dict):
            return None

        with _ECLIPSE_AUTH_LOCK:
            if _ECLIPSE_AUTH_GENERATION != account_generation:
                return None

        if stream_has_protection_indicators(s_data):
            return None

        # If LOSSLESS was explicitly requested and Max returned >48kHz/24-bit, request 16-bit/44.1kHz Lossless specifically
        if requested_fmt == "LOSSLESS":
            sr_check = _bounded_integer(s_data.get("sampleRate"), minimum=8_000, maximum=768_000)
            bd_check = _bounded_integer(s_data.get("bitDepth"), minimum=1, maximum=64)
            if (sr_check is not None and sr_check > 48000) or (bd_check is not None and bd_check > 16):
                lossless_url = _eclipse_worker_url(addon_manifest_url, f"stream/{urllib.parse.quote(str(target_id))}", {"quality": "Lossless"})
                lossless_data = _fetch_json(lossless_url, timeout=6.0)
                if isinstance(lossless_data, dict):
                    s_data = lossless_data
                with _ECLIPSE_AUTH_LOCK:
                    if _ECLIPSE_AUTH_GENERATION != account_generation:
                        return None

        if stream_has_protection_indicators(s_data):
            return None

        streams = s_data.get("streams") if isinstance(s_data.get("streams"), list) else [s_data]
        for s in streams:
            if not isinstance(s, dict) or not s.get("url"):
                continue
            if stream_has_protection_indicators(s, parent=s_data):
                continue
            raw_codec = str(s.get("codec") or s.get("format") or "").strip().lower()
            codec_token = str(s.get("codec") or "").strip().lower()
            quality_label = str(s.get("quality") or s.get("format") or "").strip()
            tier = str(s.get("tier") or "").strip().upper()
            is_explicit_joc = codec_token in {"eac3_joc", "e-ac-3 joc", "eac3-joc", "ec-3 joc"} or tier == "EAC3_JOC"
            is_flac = codec_token in {"flac", "audio/flac"} or tier in {"FLAC", "FLAC_HIRES"}
            if is_gravity_addon:
                if not is_explicit_joc and not is_flac:
                    continue
                if requested_fmt == "EAC3_JOC" and not is_explicit_joc:
                    continue
                if requested_fmt in {"HI_RES_LOSSLESS", "LOSSLESS"} and not is_flac:
                    continue
            elif not is_explicit_joc and not is_flac:
                # Playback remains limited to explicitly supported codecs;
                # the separate inventory preserves all other addon formats.
                continue
            sample_rate_raw = s.get("sampleRate")
            sample_rate = None if sample_rate_raw is None else _bounded_integer(
                sample_rate_raw, minimum=8_000, maximum=768_000
            )
            if sample_rate_raw is not None and sample_rate is None:
                continue
            is_atmos = is_explicit_joc
            bit_depth_raw = s.get("bitDepth")
            bit_depth = None if bit_depth_raw is None else _bounded_integer(
                bit_depth_raw, minimum=1, maximum=64
            )
            if bit_depth_raw is not None and bit_depth is None:
                continue
            is_hires = is_flac and ((sample_rate is not None and sample_rate > 48000) or (bit_depth is not None and bit_depth > 16))

            if is_atmos:
                stream_fmt = "EAC3_JOC"
            elif is_hires:
                stream_fmt = "HI_RES_LOSSLESS"
            else:
                stream_fmt = "LOSSLESS"

            if requested_fmt and stream_fmt != requested_fmt:
                continue

            duration_raw = s.get("duration") or matched_track_meta.get("duration") or 0
            try:
                duration = float(duration_raw)
            except (TypeError, ValueError, OverflowError):
                duration = 0.0
            if not math.isfinite(duration) or duration < 0:
                duration = 0.0
            manifest_protection = _eclipse_manifest_protection_status(
                s["url"], s.get("manifest") if isinstance(s.get("manifest"), str) else None,
                expected_generation=account_generation,
            )
            if manifest_protection is not False:
                continue
            # Addon access tokens are scoped to the addon's own origin. Never
            # forward them to a CDN URL supplied by an untrusted response.
            media_probe_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "*/*",
            }
            probed_bitrate, bitrate_basis = _probe_direct_audio_bitrate(
                s["url"], duration, media_probe_headers,
                expected_generation=account_generation,
            )
            detected_transport = _probe_direct_media_transport(
                s["url"], media_probe_headers,
                expected_generation=account_generation,
            )
            with _ECLIPSE_AUTH_LOCK:
                if _ECLIPSE_AUTH_GENERATION != account_generation:
                    return None
            explicit_bitrate_raw = s.get("bitrate")
            explicit_bitrate = None if explicit_bitrate_raw is None else _bounded_integer(
                explicit_bitrate_raw, minimum=1, maximum=100_000_000
            )
            if explicit_bitrate is not None:
                source_bitrate = explicit_bitrate
                source_bitrate_basis = "Addon reported"
            elif probed_bitrate:
                source_bitrate = probed_bitrate
                source_bitrate_basis = bitrate_basis
            elif is_atmos:
                source_bitrate = 768000
                source_bitrate_basis = "Estimated E-AC-3 JOC"
            else:
                channels_est = _bounded_integer(s.get("channels"), minimum=1, maximum=64)
                source_bitrate = (
                    int(sample_rate * bit_depth * channels_est * 0.65)
                    if sample_rate is not None and bit_depth is not None
                    and channels_est is not None
                    else None
                )
                source_bitrate_basis = "Estimated FLAC" if source_bitrate is not None else None

            if is_atmos:
                human_label = "[AMAZON] Dolby Atmos (E-AC-3 JOC)" if is_amazon else f"[{provider_label}] Dolby Atmos (E-AC-3 JOC)"
                norm_codec = "eac3_joc"
                source_codec = "EAC3_JOC"
                source_audio_mode = "DOLBY_ATMOS"
                channels = int(s["channels"]) if isinstance(s.get("channels"), (int, float)) and s["channels"] > 0 else None
            elif is_hires:
                detail = "/".join(([f"{bit_depth}-bit"] if bit_depth else []) + ([f"{sample_rate / 1000:g}kHz"] if sample_rate else []))
                human_label = f"[{provider_label}] Hi-Res FLAC ({detail})" if detail else f"[{provider_label}] Hi-Res FLAC"
                norm_codec = "flac"
                source_codec = "FLAC"
                source_audio_mode = "STEREO"
                channels = int(s["channels"]) if isinstance(s.get("channels"), (int, float)) and s["channels"] > 0 else None
            else:
                human_label = f"[{provider_label}] Lossless FLAC"
                norm_codec = "flac"
                source_codec = "FLAC"
                source_audio_mode = "STEREO"
                channels = int(s["channels"]) if isinstance(s.get("channels"), (int, float)) and s["channels"] > 0 else None

            return {
                "format": stream_fmt,
                "codec": norm_codec,
                "label": human_label,
                "uri": s["url"],
                "transport": detected_transport,
                "sourceCodec": source_codec,
                "sourceBitrate": source_bitrate,
                "sourceBitrateBasis": source_bitrate_basis,
                "sourceSampleRate": sample_rate,
                "sourceChannels": channels,
                "sourceBitDepth": bit_depth,
                "sourceQuality": quality_label or stream_fmt,
                "sourceAudioMode": source_audio_mode,
                "sourceProvider": "AMAZON" if is_amazon else "ECLIPSE",
                "sourceName": normalized_addon_name or normalized_addon_id or "Eclipse add-on",
                "sourceAuthProfile": "eclipse_addon",
                "sourceMetadata": dict(matched_track_meta),
            }
    except Exception as exc:
        print(f"[-] Eclipse addon resolution error ({requested_fmt}): {type(exc).__name__}")
    return None


def resolve_with_priority(track_id: int | str, priority_list: list[str], title_hint: str = "", artist_hint: str = "", provider_key: str | None = None) -> dict:
    resolvers = {
        "EAC3_JOC": lambda tid: resolve_eac3_joc(tid),
        "AC4_IMS": lambda tid: resolve_ac4_ims(tid),
        "HI_RES_LOSSLESS": lambda tid: resolve_hi_res_or_lossless(tid, "HI_RES_LOSSLESS"),
        "LOSSLESS": lambda tid: resolve_hi_res_or_lossless(tid, "LOSSLESS"),
    }

    clean_id_str = str(track_id).strip()
    is_numeric = clean_id_str.isdigit()

    settings = load_settings()
    provider_order, addon_providers = _ordered_resolution_providers(settings)
    main_api_authorized = is_main_tidal_api_authorized()
    if provider_key:
        if provider_key == "tidal_api":
            if provider_key not in provider_order or not main_api_authorized:
                raise RuntimeError("The selected TIDAL API provider is unavailable or not authorized")
        else:
            provider = addon_providers.get(provider_key)
            if provider_key not in provider_order or not isinstance(provider, dict):
                raise RuntimeError("The selected Eclipse provider is unavailable or no longer enabled")
            addon_url = provider.get("manifestUrl")
            if not isinstance(addon_url, str) or not addon_url:
                raise RuntimeError("The selected Eclipse provider has no valid manifest")
            clean_id = str(track_id).strip()
            for fmt in priority_list:
                if fmt == "AC4_IMS":
                    continue
                if _is_tido_eclipse_addon(addon_url):
                    resolved = resolve_tido_eclipse_addon(
                        addon_url, clean_id, fmt, title=title_hint, artist=artist_hint,
                    )
                else:
                    resolved = resolve_eclipse_addon(
                        addon_url, "", fmt, title_hint, artist_hint,
                        explicit_track_id=clean_id,
                        addon_id=str(provider.get("addonId") or ""),
                        addon_name=str(provider.get("name") or provider.get("displayName") or ""),
                    )
                if resolved:
                    return resolved
            raise RuntimeError(f"Could not resolve track {clean_id} from the selected Eclipse provider")
    tidal_selected = "tidal_api" in provider_order
    selected_addons = [
        (key, addon_providers[key]) for key in provider_order
        if key != "tidal_api" and key in addon_providers
    ]

    if not is_numeric:
        non_tidal_addons = [row for _, row in selected_addons if not _is_tido_eclipse_addon(row.get("manifestUrl", ""))]
        if not non_tidal_addons:
            raise RuntimeError(f"Cannot resolve non-numeric track '{clean_id_str}' without a selected compatible Eclipse add-on")
        for fmt in priority_list:
            if fmt == "AC4_IMS":
                continue
            for provider in non_tidal_addons:
                addon_url = provider["manifestUrl"]
                addon_res = resolve_eclipse_addon(
                    addon_url,
                    "",
                    fmt,
                    title_hint or "",
                    artist_hint or "",
                    duration=0,
                    explicit_track_id=clean_id_str,
                    addon_id=str(provider.get("addonId") or ""),
                    addon_name=str(provider.get("name") or provider.get("displayName") or ""),
                )
                if addon_res:
                    print(f"[RealMAX] Selected Eclipse add-on {provider.get('name') or provider.get('addonId') or provider_key} for track {clean_id_str} ({fmt})")
                    return addon_res
        raise RuntimeError(f"Could not resolve any audio stream for track {clean_id_str}")

    track_id = int(clean_id_str)
    # Track candidate cache: clicked track is always evaluated
    candidate_ids = [track_id]
    track_isrc = None
    track_title = title_hint or ""
    track_artist = artist_hint or ""
    orig = None

    if tidal_selected and main_api_authorized:
        try:
            import tidalapi
            for sess_getter in (get_python_tidal_session, get_python_tidal_mobile_session):
                try:
                    sess = sess_getter()
                except Exception:
                    sess = None
                if not sess:
                    continue
                try:
                    orig = sess.track(int(track_id))
                    if orig:
                        track_isrc = getattr(orig, "isrc", None) or track_isrc
                        full_name = getattr(orig, "full_name", None)
                        if isinstance(full_name, str) and full_name.strip():
                            track_title = full_name.strip()
                        elif getattr(orig, "name", None):
                            ver = getattr(orig, "version", None)
                            track_title = f"{orig.name} ({ver})" if isinstance(ver, str) and ver.strip() and ver.lower() not in orig.name.lower() else orig.name
                        if getattr(orig, "artist", None) and getattr(orig.artist, "name", None):
                            track_artist = orig.artist.name
                        break
                except Exception:
                    continue
        except Exception:
            pass

    # RealMAX cross-edition discovery: find alternate album releases (Atmos editions, Deluxe Hi-Res, etc.)
    if tidal_selected and main_api_authorized:
        try:
            import tidalapi
            session = None
            for sess_getter in (get_python_tidal_session, get_python_tidal_mobile_session):
                try:
                    session = sess_getter()
                    if session:
                        break
                except Exception:
                    continue
            if session and orig and getattr(orig, "name", None):
                query = f"{track_title} {track_artist}".strip()
                search_res = session.search(query, models=[tidalapi.media.Track], limit=20)
                for cand in search_res.get("tracks", []):
                    if _tidal_alternate_track_matches(orig, cand):
                        candidate_ids.append(cand.id)
        except Exception as exc:
            print(f"[-] RealMAX candidate search error: {exc}")

    # Format order is the outer preference; within each format, use the
    # user's selected provider order. This lets AC-4 win when preferred while
    # still allowing account add-ons to outrank one another for E-AC-3/FLAC.
    for fmt in priority_list:
        if fmt not in resolvers:
            continue
        for provider_key in provider_order:
            if provider_key == "tidal_api":
                if not main_api_authorized:
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
                    except Ac4TransportUnavailable:
                        raise
                    except Exception:
                        pass
                continue
            provider = addon_providers.get(provider_key)
            if not provider or fmt == "AC4_IMS":
                continue
            addon_url = provider.get("manifestUrl")
            if not isinstance(addon_url, str) or not addon_url:
                continue
            if _is_tido_eclipse_addon(addon_url):
                addon_res = resolve_tido_eclipse_addon(
                    addon_url, track_id, fmt, title=track_title,
                    artist=track_artist, isrc=track_isrc or "",
                )
            else:
                addon_res = resolve_eclipse_addon(
                    addon_url, track_isrc or "", fmt, track_title,
                    track_artist, duration=getattr(orig, "duration", 0) if orig else 0,
                    addon_id=str(provider.get("addonId") or ""),
                    addon_name=str(provider.get("name") or provider.get("displayName") or ""),
                )
            if addon_res:
                print(f"[RealMAX] Selected Eclipse provider {provider.get('displayName') or provider.get('addonId') or provider_key} for {track_id} ({fmt})")
                return addon_res

    raise RuntimeError(f"Could not resolve any audio stream for track {track_id}")




def execute_multi_source_search(query: str, limit: int = 10) -> dict:
    """Search the primary TIDAL API and every configured Eclipse addon."""
    clean_q = (query or "").strip()
    if not clean_q:
        return {"query": "", "tracks": []}

    results = {"query": clean_q, "tracks": []}
    tidal_tracks = []
    eclipse_tracks = []
    settings = load_settings()
    provider_order, addon_providers = _ordered_resolution_providers(settings)

    # 1. Search TIDAL
    if "tidal_api" in provider_order:
        try:
            sess = get_python_tidal_session()
            if sess:
                import tidalapi
                t_res = sess.search(clean_q, models=[tidalapi.media.Track], limit=limit)
                for t in t_res.get("tracks", []):
                    modes = getattr(t, "audio_modes", []) or []
                    is_atmos = any("DOLBY_ATMOS" in str(m).upper() for m in modes)
                    tidal_tracks.append({
                        "id": t.id,
                        "providerKey": "tidal_api",
                        "title": t.name,
                        "artist": t.artist.name if getattr(t, "artist", None) else "",
                        "album": t.album.name if getattr(t, "album", None) else "",
                        "duration": int(getattr(t, "duration", 0) or 0),
                        "isAtmos": is_atmos,
                        "quality": getattr(t, "audio_quality", "LOSSLESS"),
                        "cover": t.album.image(640) if hasattr(t.album, "image") else "",
                        "isrc": getattr(t, "isrc", "") or "",
                        "source": "TIDAL",
                    })
        except Exception as exc:
            print(f"[-] TIDAL multi-search error: {exc}")

    # Search only selected Eclipse providers, preserving the configured
    # provider order. Account-linked providers are loaded from Eclipse itself.
    for provider_key in provider_order:
        if provider_key == "tidal_api":
            continue
        provider_info = addon_providers.get(provider_key) or {}
        addon_url = provider_info.get("manifestUrl")
        if not isinstance(addon_url, str) or not addon_url.strip():
            continue
        addon_id = provider_info.get("addonId")
        addon_name = str(provider_info.get("name") or provider_info.get("displayName") or addon_id or "Eclipse add-on").strip()
        if not isinstance(addon_id, str) or not addon_id.strip():
            manifest = _get_eclipse_manifest(addon_url)
            if not manifest:
                continue
            addon_id = manifest["id"].strip()
            addon_name = str(manifest.get("name") or addon_id).strip()
        if addon_id.lower() == "com.yourname.amazonmusic" or "amazon" in addon_name.lower() or addon_name.upper() == "AMZM":
            provider = "AMAZON"
        elif _is_tido_eclipse_addon(addon_url) or addon_id.lower() == "com.yourname.tidal.hifi":
            provider = "TIDAL_ECLIPSE"
        else:
            provider = "ECLIPSE"
        headers = {
            "User-Agent": "OmniphonyEX/1.0 Eclipse addon client",
            "Accept": "application/json, text/plain, */*",
            "X-Client-Platform": "web",
            "Origin": "https://eclipsemusic.app",
            "Referer": "https://eclipsemusic.app/",
        }
        token = _get_eclipse_addon_access_token(addon_id)
        if token:
            headers["X-Eclipse-Access"] = token
        search_url = _eclipse_worker_url(addon_url, "search", {"q": clean_q})
        try:
            req = urllib.request.Request(search_url, headers=headers)
            with _eclipse_authenticated_urlopen(req, timeout=6.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            tracks = payload.get("tracks") or payload.get("results") or [] if isinstance(payload, dict) else payload
            for track in tracks[:max(1, min(int(limit), 50))] if isinstance(tracks, list) else []:
                if not isinstance(track, dict) or track.get("id") is None:
                    continue
                atmosphere = track.get("atmosAvailable") or track.get("isAtmos") or track.get("atmos")
                eclipse_tracks.append({
                    "id": track.get("id"),
                    "providerKey": provider_key,
                    "title": track.get("title") or track.get("name") or "Unknown Title",
                    "artist": track.get("artist") or track.get("artistName") or "Unknown Artist",
                    "album": track.get("album") or track.get("albumTitle") or "Unknown Album",
                    "duration": int(track.get("duration") or track.get("durationSeconds") or 0),
                    "isAtmos": bool(atmosphere),
                    "quality": track.get("audioQuality") or track.get("quality") or "",
                    "cover": track.get("artworkURL") or track.get("cover") or track.get("artwork") or "",
                    "isrc": track.get("isrc") or "",
                    "source": provider,
                    "sourceName": addon_name,
                })
        except urllib.error.HTTPError as exc:
            print(f"[-] Eclipse search failed for {addon_id}: HTTP {exc.code}")
        except Exception as exc:
            print(f"[-] Eclipse search failed for {addon_id}: {type(exc).__name__}")

    # 3. Merge, Deduplicate & Badge
    seen_isrc = set()
    seen_keys = set()

    for item in tidal_tracks:
        norm_t = re.sub(r"[^a-z0-9]+", " ", item["title"].lower()).strip()
        norm_a = re.sub(r"[^a-z0-9]+", " ", item["artist"].lower()).strip()
        key = f"{norm_t}|{norm_a}"
        if item["isrc"]:
            seen_isrc.add(item["isrc"].upper())
        seen_keys.add(key)

        # Cross-reference Amazon for Atmos upgrade
        amz_match = next((
            a for a in eclipse_tracks if a["source"] == "AMAZON"
            if (a["isrc"] and a["isrc"].upper() == item["isrc"].upper())
            or (re.sub(r"[^a-z0-9]+", " ", a["title"].lower()).strip() == norm_t and re.sub(r"[^a-z0-9]+", " ", a["artist"].lower()).strip() == norm_a)
        ), None)

        if amz_match and amz_match["isAtmos"] and not item["isAtmos"]:
            item["upgrade"] = {
                "provider": "AMAZON",
                "format": "EAC3_JOC",
                "label": "Amazon Dolby Atmos 5.1",
            }
        results["tracks"].append(item)

    for a in eclipse_tracks:
        norm_t = re.sub(r"[^a-z0-9]+", " ", a["title"].lower()).strip()
        norm_a = re.sub(r"[^a-z0-9]+", " ", a["artist"].lower()).strip()
        key = f"{norm_t}|{norm_a}"
        # Keep cross-provider matches visible with the correct provider badge;
        # suppress repeated copies only within the same addon.
        dedup_key = (str(a.get("providerKey") or a.get("source") or "ECLIPSE"), str(a.get("id")))
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        matched_tid = next((t for t in tidal_tracks if
            (a["isrc"] and t["isrc"] and a["isrc"].upper() == t["isrc"].upper()) or
            (re.sub(r"[^a-z0-9]+", " ", t["title"].lower()).strip() == norm_t and
             re.sub(r"[^a-z0-9]+", " ", t["artist"].lower()).strip() == norm_a)), None)
        results["tracks"].append({
            **a,
            "exclusive": a["source"] == "AMAZON" and matched_tid is None,
        })

    return results


DOWNLOAD_QUEUE_LOCK = threading.RLock()
ACTIVE_DOWNLOADS = {}

def pack_flac_metadata(comments: dict[str, str], picture_bytes: bytes = None, picture_mime: str = "image/jpeg") -> list[tuple[int, bytes]]:
    blocks = []
    import io, struct
    b = io.BytesIO()
    vendor = "Omniphony EX RealMAX".encode("utf-8")
    b.write(struct.pack("<I", len(vendor)))
    b.write(vendor)
    normalized_comments = []
    for k, values in comments.items():
        key = str(k).strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]+", key):
            continue
        if not isinstance(values, (list, tuple)):
            values = (values,)
        for value in values:
            if value is not None and str(value) != "":
                normalized_comments.append(f"{key}={str(value).replace(chr(0), ' ')}")
    b.write(struct.pack("<I", len(normalized_comments)))
    for item in normalized_comments:
        c_bytes = item.encode("utf-8", errors="replace")
        b.write(struct.pack("<I", len(c_bytes)))
        b.write(c_bytes)
    blocks.append((4, b.getvalue()))

    if picture_bytes and len(picture_bytes) > 0:
        pb = io.BytesIO()
        pb.write(struct.pack(">I", 3)) # Cover Front
        m_bytes = picture_mime.encode("ascii", errors="replace")
        pb.write(struct.pack(">I", len(m_bytes)))
        pb.write(m_bytes)
        pb.write(struct.pack(">I", 0))
        # Zero means unknown. Never invent image dimensions or bit depth.
        pb.write(struct.pack(">IIII", 0, 0, 0, 0))
        pb.write(struct.pack(">I", len(picture_bytes)))
        pb.write(picture_bytes)
        blocks.append((6, pb.getvalue()))

    return blocks

def inject_flac_metadata(raw_flac_bytes: bytes, comments: dict[str, str], picture_bytes: bytes = None, picture_mime: str = "image/jpeg") -> bytes:
    if len(raw_flac_bytes) < 4 or raw_flac_bytes[:4] != b"fLaC":
        raise ValueError("download output is not a FLAC stream")
    import io, struct
    f = io.BytesIO(raw_flac_bytes)
    f.seek(4)
    kept_blocks = []
    old_comments = {}
    saw_streaminfo = False
    saw_last = False
    while True:
        h = f.read(4)
        if len(h) != 4:
            raise ValueError("truncated FLAC metadata header")
        is_last = (h[0] & 0x80) != 0
        b_type = h[0] & 0x7F
        length = struct.unpack(">I", b"\x00" + h[1:4])[0]
        data = f.read(length)
        if len(data) != length:
            raise ValueError("truncated FLAC metadata block")
        if not saw_streaminfo:
            if b_type != 0 or length != 34:
                raise ValueError("FLAC stream is missing a valid STREAMINFO block")
            saw_streaminfo = True
        if b_type == 0:
            kept_blocks.append((0, data))
        elif b_type == 4:
            if len(data) >= 8:
                vendor_len = struct.unpack_from("<I", data, 0)[0]
                pos = 4 + vendor_len
                if pos + 4 <= len(data):
                    count = struct.unpack_from("<I", data, pos)[0]
                    pos += 4
                    for _ in range(min(count, 100000)):
                        if pos + 4 > len(data):
                            break
                        item_len = struct.unpack_from("<I", data, pos)[0]
                        pos += 4
                        if pos + item_len > len(data):
                            break
                        item = data[pos:pos + item_len].decode("utf-8", errors="replace")
                        pos += item_len
                        if "=" in item:
                            key, value = item.split("=", 1)
                            old_comments.setdefault(key.upper(), []).append(value)
        elif b_type == 6 and picture_bytes:
            # Replace stale provider artwork with the exact resolved track art.
            pass
        else:
            # Keep seek tables, application blocks, cuesheets and all other
            # metadata blocks that are not explicitly replaced.
            kept_blocks.append((b_type, data))
        if is_last:
            saw_last = True
            break
    if not saw_streaminfo or not saw_last:
        raise ValueError("FLAC metadata chain is incomplete")
    audio_stream_data = f.read()
    if not audio_stream_data:
        raise ValueError("FLAC stream contains no audio frames")
    merged_comments = old_comments
    for key, value in comments.items():
        merged_comments[str(key).upper()] = value
    new_blocks = pack_flac_metadata(merged_comments, picture_bytes=picture_bytes, picture_mime=picture_mime)
    all_blocks = kept_blocks + new_blocks
    out = io.BytesIO()
    out.write(b"fLaC")
    for i, (bt, bd) in enumerate(all_blocks):
        last_flag = (0x80 if i == len(all_blocks) - 1 else 0x00)
        out.write(bytes([last_flag | (bt & 0x7F)]) + struct.pack(">I", len(bd))[1:])
        out.write(bd)
    out.write(audio_stream_data)
    return out.getvalue()

def sanitize_filename_component(val: str) -> str:
    s = re.sub(r'[\/:*?"<>|]+', '_', str(val or '')).strip()
    return s or "Unknown"

def sanitize_download_component(val: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(val or "")).strip(" .")
    if not value:
        return "Unknown"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if value.split(".", 1)[0].upper() in reserved:
        value = "_" + value
    return value[:160].rstrip(" .") or "Unknown"


def build_download_output_path(dest_dir: Path, path_pattern: str, replacements: dict[str, str], extension: str) -> Path:
    """Resolve a user naming template strictly beneath the selected folder."""
    from pathlib import PureWindowsPath
    pattern = str(path_pattern or "{artist}/{album}/{trackNumber} - {title}")
    for key, value in replacements.items():
        pattern = pattern.replace(key, str(value))
    if re.search(r"\{[^{}]+\}", pattern):
        raise ValueError("download path template contains an unknown placeholder")
    if PureWindowsPath(pattern).is_absolute() or PureWindowsPath(pattern).drive:
        raise ValueError("download path template must be relative to the selected folder")
    parts = [part for part in re.split(r"[\\/]+", pattern) if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("download path template escapes the selected folder")
    safe_parts = [sanitize_download_component(part) for part in parts]
    if extension not in {"flac", "wav"}:
        raise ValueError("unsupported download output extension")
    safe_parts[-1] = f"{safe_parts[-1]}.{extension}"
    root = Path(dest_dir).resolve()
    target = root.joinpath(*safe_parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError("download path resolves outside the selected folder")
    return target


def choose_nonconflicting_path(path: Path, companion_suffix: str | None = None) -> Path:
    """Allocate a numbered name without deleting an existing completed file."""
    def available(candidate: Path) -> bool:
        companion = candidate.with_suffix(companion_suffix) if companion_suffix else None
        return not candidate.exists() and (companion is None or not companion.exists())

    if available(path):
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if available(candidate):
            return candidate
    raise RuntimeError("could not allocate a unique output filename")


def publish_file_without_overwrite(temp_path: Path, target_path: Path) -> Path:
    """Atomically publish with a unique suffix; never replace an existing file."""
    for index in range(1, 10000):
        candidate = target_path if index == 1 else target_path.with_name(f"{target_path.stem} ({index}){target_path.suffix}")
        try:
            os.link(temp_path, candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise RuntimeError(f"atomic no-overwrite publication is unavailable at {candidate.parent}: {exc}") from exc
        temp_path.unlink(missing_ok=True)
        return candidate
    raise RuntimeError("could not allocate a unique output filename")


def validate_picture_bytes(data: bytes, content_type: str = "") -> tuple[bytes | None, str | None]:
    """Accept only recognizable image data before writing embedded artwork."""
    if not data:
        return None, None
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return data, "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        return data, "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")) and data.endswith(b";"):
        return data, "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, "image/webp"
    return None, None


def _id3_synchsafe(value: int) -> bytes:
    if value < 0 or value > 0x0FFFFFFF:
        raise ValueError("ID3 frame is too large")
    return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


def make_id3_wav_chunk(comments: dict[str, str], picture_bytes: bytes = None, picture_mime: str = "image/jpeg") -> bytes:
    """Build an ID3v2.4 tag carrying WAV metadata, lyrics and cover art."""
    def frame(frame_id: str, payload: bytes) -> bytes:
        return frame_id.encode("ascii") + _id3_synchsafe(len(payload)) + b"\x00\x00" + payload

    field_frames = {
        "TITLE": "TIT2", "ARTIST": "TPE1", "ALBUM": "TALB", "ALBUMARTIST": "TPE2",
        "DATE": "TDRC", "TRACKNUMBER": "TRCK", "DISCNUMBER": "TPOS", "GENRE": "TCON",
        "COMPOSER": "TCOM", "COPYRIGHT": "TCOP", "ISRC": "TSRC",
    }
    frames = []
    for key, frame_id in field_frames.items():
        value = comments.get(key)
        if value:
            frames.append(frame(frame_id, b"\x03" + str(value).encode("utf-8")))
    reserved = set(field_frames) | {"LYRICS", "SYNCEDLYRICS"}
    for key, value in comments.items():
        key = str(key).upper()
        if key not in reserved and value:
            frames.append(frame("TXXX", b"\x03" + key.encode("utf-8") + b"\x00" + str(value).encode("utf-8")))
    if comments.get("LYRICS"):
        frames.append(frame("USLT", b"\x03eng\x00" + str(comments["LYRICS"]).encode("utf-8")))
    if comments.get("SYNCEDLYRICS"):
        body = bytearray(b"\x03eng\x02\x01\x00")
        found = False
        for line in str(comments["SYNCEDLYRICS"]).splitlines():
            match = re.match(r"^\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\](.*)$", line)
            if not match:
                continue
            minutes, seconds, fraction, lyric = match.groups()
            millis = int((fraction or "0").ljust(3, "0"))
            stamp = (int(minutes) * 60 + int(seconds)) * 1000 + millis
            body.extend(lyric.encode("utf-8") + b"\x00" + struct.pack(">I", stamp))
            found = True
        if found:
            frames.append(frame("SYLT", bytes(body)))
    if picture_bytes:
        frames.append(frame("APIC", b"\x03" + picture_mime.encode("ascii") + b"\x00\x03\x00" + picture_bytes))
    payload = b"".join(frames)
    return b"ID3\x04\x00\x00" + _id3_synchsafe(len(payload)) + payload


def tag_wav_bytes(raw_wav_bytes: bytes, comments: dict[str, str], picture_bytes: bytes = None, picture_mime: str = "image/jpeg") -> bytes:
    """Preserve RIFF audio chunks and add INFO plus ID3 metadata to WAV."""
    if len(raw_wav_bytes) < 12 or raw_wav_bytes[:4] != b"RIFF" or raw_wav_bytes[8:12] != b"WAVE":
        raise ValueError("download output is not a RIFF/WAVE file")
    chunks = []
    pos = 12
    while pos + 8 <= len(raw_wav_bytes):
        chunk_id = raw_wav_bytes[pos:pos + 4]
        size = struct.unpack_from("<I", raw_wav_bytes, pos + 4)[0]
        start = pos + 8
        end = start + size
        if end > len(raw_wav_bytes):
            raise ValueError("WAV contains a truncated chunk")
        data = raw_wav_bytes[start:end]
        if chunk_id not in {b"id3 ", b"ID3 "} and not (chunk_id == b"LIST" and data.startswith(b"INFO")):
            chunks.append((chunk_id, data))
        pos = end + (size & 1)
    if pos != len(raw_wav_bytes):
        raise ValueError("WAV chunk table is malformed")
    if not any(chunk_id == b"fmt " for chunk_id, _ in chunks) or not any(chunk_id == b"data" for chunk_id, _ in chunks):
        raise ValueError("WAV is missing a format or audio-data chunk")

    info_map = {"TITLE": b"INAM", "ARTIST": b"IART", "ALBUM": b"IPRD", "DATE": b"ICRD", "TRACKNUMBER": b"IPRT", "GENRE": b"IGNR", "COMMENT": b"ICMT", "COMPOSER": b"IENG", "COPYRIGHT": b"ICOP", "ISRC": b"ISRC"}
    info = bytearray(b"INFO")
    for key, chunk_id in info_map.items():
        value = comments.get(key)
        if not value:
            continue
        data = str(value).encode("utf-8") + b"\x00"
        info.extend(chunk_id + struct.pack("<I", len(data)) + data)
        if len(data) & 1:
            info.extend(b"\x00")
    id3 = make_id3_wav_chunk(comments, picture_bytes, picture_mime)

    out = bytearray(b"RIFF\x00\x00\x00\x00WAVE")
    inserted_tags = False
    for chunk_id, data in chunks:
        if not inserted_tags and chunk_id == b"data":
            if len(info) > 4:
                out.extend(b"LIST" + struct.pack("<I", len(info)) + info)
                if len(info) & 1:
                    out.extend(b"\x00")
            out.extend(b"id3 " + struct.pack("<I", len(id3)) + id3)
            if len(id3) & 1:
                out.extend(b"\x00")
            inserted_tags = True
        out.extend(chunk_id + struct.pack("<I", len(data)) + data)
        if len(data) & 1:
            out.extend(b"\x00")
    if not inserted_tags:
        raise ValueError("WAV has no audio-data chunk")
    riff_size = len(out) - 8
    if riff_size > 0xFFFFFFFF:
        raise ValueError("WAV output exceeds RIFF's 4 GiB size limit")
    struct.pack_into("<I", out, 4, riff_size)
    return bytes(out)


def metadata_text(meta: dict, *keys: str) -> str:
    for key in keys:
        value = meta.get(key) if isinstance(meta, dict) else None
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or value.get("value")
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(item.get("name") or item) if isinstance(item, dict) else str(item) for item in value if item)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def apply_provider_metadata(meta: dict, state: dict) -> None:
    """Apply fields tied to the resolved provider result, without cache guessing."""
    for target, keys in {
        "title": ("title", "name", "fullName"),
        "artist": ("artist", "artistName", "artists"),
        "album": ("album", "albumTitle", "albumName"),
        "album_artist": ("albumArtist", "album_artist", "albumArtistName"),
        "year": ("year", "releaseYear", "releaseDate"),
        "track_num": ("trackNumber", "track_number", "trackNum"),
        "disc_num": ("discNumber", "disc_number", "volumeNumber"),
        "isrc": ("isrc", "ISRC"),
        "genre": ("genre", "genres"),
        "composer": ("composer", "composers"),
        "copyright": ("copyright", "copyrightLine"),
        "art_url": ("artworkURL", "artworkUrl", "albumArtUrl", "cover", "image"),
        "lyrics_text": ("lyrics", "plainLyrics", "unsyncedLyrics"),
        "lyrics_synced": ("syncedLyrics", "subtitles", "lrc"),
    }.items():
        value = metadata_text(meta, *keys)
        if value:
            if target == "year":
                value = value[:4]
            state[target] = value


def inspect_flac_stream(raw_flac_bytes: bytes) -> dict:
    """Validate FLAC metadata framing and report encoded stream properties."""
    if len(raw_flac_bytes) < 42 or raw_flac_bytes[:4] != b"fLaC":
        raise ValueError("render output is not a complete FLAC stream")
    first = raw_flac_bytes[4:8]
    if first[0] & 0x7F != 0 or int.from_bytes(first[1:4], "big") != 34:
        raise ValueError("FLAC stream is missing STREAMINFO")
    info = raw_flac_bytes[8:42]
    packed = int.from_bytes(info[10:18], "big")
    sample_rate = packed >> 44
    channels = ((packed >> 41) & 0x7) + 1
    bits_per_sample = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & ((1 << 36) - 1)
    if not (8000 <= sample_rate <= 384000) or channels > 8 or bits_per_sample not in range(4, 33):
        raise ValueError("FLAC STREAMINFO contains invalid sample properties")
    if len(raw_flac_bytes) <= 42:
        raise ValueError("FLAC stream contains no audio frames")
    return {"sampleRate": sample_rate, "channels": channels, "bitsPerSample": bits_per_sample, "totalSamples": total_samples}


def build_joc_download_render_command(
    input_uri: str,
    output_path: Path,
    transport: str | None = None,
) -> list[str]:
    """Build the export renderer command with the same Omniphony chain as playback."""
    command = [
        str(MPV_EXE),
        "--no-config",
        "--vid=no",
        "--vo=null",
        "--ad=orender",
        f"--ad-orender-library={ORENDER_DLL}",
        "--ad-orender-bridge-path=" + ";".join(str(path) for path in ORENDER_BRIDGE_PATHS),
        "--ad-orender-osc",
        "--audio-channels=stereo",
        f"-o={output_path}",
        "--oac=flac",
        "--ytdl=no",
    ]
    if str(transport or "").strip().upper().replace("-", "_") in {"DASH", "MPEG_DASH", "MPD"}:
        # Match the live engine's explicit override: these loopback MPD URLs
        # have no .mpd suffix, so MPV's automatic probe cannot identify them.
        command.extend(["--demuxer=lavf", "--demuxer-lavf-format=dash"])
    command.append(input_uri)
    return command


def safe_process_error(stderr: bytes | str, stdout: bytes | str = b"", limit: int = 1600) -> str:
    """Return useful subprocess diagnostics without leaking signed stream URLs."""
    def decode(value: bytes | str) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")

    text = "\n".join(part for part in (decode(stderr), decode(stdout)) if part.strip())
    text = re.sub(r"https?://[^\s\"'<>]+", "<redacted-url>", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)(token|authorization|signature|sig|key|policy)=([^&\s]+)", r"\1=<redacted>", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = text.strip()
    return text[-limit:] if text else "no stderr output"


def execute_track_download(job_id: str, payload: dict) -> None:
    track_id = payload.get("trackId")
    title = payload.get("title") or "Unknown Title"
    artist = payload.get("artist") or "Unknown Artist"
    album = payload.get("album") or "Unknown Album"
    year = payload.get("year") or ""
    track_num = payload.get("trackNumber") or ""
    disc_num = payload.get("discNumber") or ""
    album_artist = payload.get("albumArtist") or ""
    genre = payload.get("genre") or ""
    composer = payload.get("composer") or ""
    copyright_text = payload.get("copyright") or ""
    isrc = payload.get("isrc") or ""
    art_url = payload.get("albumArtUrl") or ""

    with DOWNLOAD_QUEUE_LOCK:
        ACTIVE_DOWNLOADS[job_id] = {
            "jobId": job_id, "trackId": track_id, "title": title, "artist": artist,
            "status": "resolving", "progress": 5, "error": None, "outputPath": None, "warnings": [],
        }

    temp_dest = None
    temp_lyrics = None
    try:
        dest_dir = Path(payload.get("destinationFolder") or str(DOWNLOAD_DIR))
        ac4_fmt = str(payload.get("ac4Format") or "flac").lower()
        path_pattern = str(payload.get("pathPattern") or "{artist}/{album}/{title}")
        if ac4_fmt not in {"flac", "wav"}:
            raise ValueError("AC-4 download format must be WAV or FLAC")
        dest_dir.mkdir(parents=True, exist_ok=True)
        settings = load_settings()
        priority = settings.get("priority", DEFAULT_SETTINGS["priority"])

        with DOWNLOAD_QUEUE_LOCK:
            ACTIVE_DOWNLOADS[job_id]["status"] = "resolving"
            ACTIVE_DOWNLOADS[job_id]["progress"] = 15

        track_id_param = int(track_id) if str(track_id).strip().isdigit() else str(track_id).strip()
        stream_info = resolve_with_priority(
            track_id_param, priority, title_hint=title, artist_hint=artist,
            provider_key=payload.get("providerKey"),
        )
        if not stream_info or not stream_info.get("uri"):
            raise RuntimeError("Could not resolve audio stream for track")

        fmt = stream_info.get("format")
        if fmt not in {"EAC3_JOC", "AC4_IMS", "HI_RES_LOSSLESS", "LOSSLESS"}:
            raise RuntimeError(f"resolver returned an unsupported download format: {fmt!r}")
        provider = stream_info.get("sourceProvider") or "TIDAL"
        if fmt == "AC4_IMS" and provider == "AMAZON":
            raise RuntimeError("Amazon Music does not provide AC-4 IMS streams")
        stream_uri = stream_info["uri"]
        transport = stream_info.get("transport")

        with DOWNLOAD_QUEUE_LOCK:
            ACTIVE_DOWNLOADS[job_id]["status"] = "rendering" if fmt == "EAC3_JOC" else "downloading"
            ACTIVE_DOWNLOADS[job_id]["format"] = fmt
            ACTIVE_DOWNLOADS[job_id]["provider"] = provider
            ACTIVE_DOWNLOADS[job_id]["progress"] = 30

        pic_bytes = None
        pic_mime = "image/jpeg"
        lyrics_text = str(payload.get("lyrics") or "")
        lyrics_synced = str(payload.get("syncedLyrics") or "")
        resolved_cover_url = art_url

        # Eclipse metadata is taken only from the exact resolved stream result.
        metadata_state = {
            "title": title, "artist": artist, "album": album, "album_artist": album_artist,
            "year": year, "track_num": track_num, "disc_num": disc_num, "isrc": isrc,
            "genre": genre, "composer": composer, "copyright": copyright_text,
            "art_url": resolved_cover_url, "lyrics_text": lyrics_text, "lyrics_synced": lyrics_synced,
        }
        source_metadata = stream_info.get("sourceMetadata")
        if provider in {"AMAZON", "ECLIPSE", "TIDAL_ECLIPSE"} and isinstance(source_metadata, dict):
            apply_provider_metadata(source_metadata, metadata_state)
        title = metadata_state["title"]
        artist = metadata_state["artist"]
        album = metadata_state["album"]
        album_artist = metadata_state["album_artist"]
        year = metadata_state["year"]
        track_num = metadata_state["track_num"]
        disc_num = metadata_state["disc_num"]
        isrc = metadata_state["isrc"]
        genre = metadata_state["genre"]
        composer = metadata_state["composer"]
        copyright_text = metadata_state["copyright"]
        resolved_cover_url = metadata_state["art_url"]
        lyrics_text = metadata_state["lyrics_text"]
        lyrics_synced = metadata_state["lyrics_synced"]

        # Only a main TIDAL API result can be looked up with python-tidal.
        # Eclipse addon IDs can be numeric too, but belong to their own catalog.
        try:
            source_key = str(payload.get("providerKey") or "")
            tidal_origin = source_key == "tidal_api" or (not source_key and provider == "TIDAL")
            sess = get_python_tidal_session() if provider == "TIDAL" and tidal_origin else None
            if sess and str(track_id).strip().isdigit():
                t_obj = sess.track(int(track_id))
                if t_obj:
                    full_name = getattr(t_obj, "full_name", None)
                    if isinstance(full_name, str) and full_name.strip():
                        title = full_name.strip()
                    elif getattr(t_obj, "name", None):
                        ver = getattr(t_obj, "version", None)
                        title = f"{t_obj.name} ({ver})" if isinstance(ver, str) and ver.strip() and ver.lower() not in t_obj.name.lower() else t_obj.name

                    if getattr(t_obj, "artist", None) and getattr(t_obj.artist, "name", None):
                        artist = t_obj.artist.name
                    artists = [getattr(item, "name", "") for item in (getattr(t_obj, "artists", None) or [])]
                    if artists:
                        album_artist = "; ".join(name for name in artists if name) or album_artist

                    if getattr(t_obj, "album", None):
                        if getattr(t_obj.album, "name", None):
                            album = t_obj.album.name
                        if getattr(t_obj.album, "release_date", None):
                            year = str(t_obj.album.release_date)[:4]
                        # Keep provider art when present; otherwise use TIDAL's largest image.
                        if not resolved_cover_url and hasattr(t_obj.album, "image"):
                            resolved_cover_url = t_obj.album.image(1280) or t_obj.album.image(640)

                    if getattr(t_obj, "track_num", None):
                        track_num = t_obj.track_num
                    disc_num = getattr(t_obj, "volume_num", None) or getattr(t_obj, "disc_num", None) or disc_num
                    if getattr(t_obj, "isrc", None):
                        isrc = t_obj.isrc
                    if getattr(t_obj, "copyright", None):
                        copyright_text = t_obj.copyright
                    album_artist_obj = getattr(getattr(t_obj, "album", None), "artist", None)
                    if getattr(album_artist_obj, "name", None):
                        album_artist = album_artist_obj.name

                    if hasattr(t_obj, "lyrics"):
                        try:
                            lyr = t_obj.lyrics()
                            if lyr:
                                lyrics_text = getattr(lyr, "text", "") or ""
                                lyrics_synced = getattr(lyr, "subtitles", "") or ""
                        except Exception:
                            pass
        except Exception:
            pass

        replacements = {
            "{artist}": sanitize_download_component(artist),
            "{album}": sanitize_download_component(album),
            "{title}": sanitize_download_component(title),
            "{trackNumber}": str(track_num).zfill(2) if track_num else "01",
            "{year}": sanitize_download_component(str(year)) if year else "",
            "{isrc}": sanitize_download_component(isrc),
        }
        ext = "wav" if (fmt == "AC4_IMS" and ac4_fmt == "wav") else "flac"
        final_file_path = build_download_output_path(dest_dir, path_pattern, replacements, ext)
        final_file_path.parent.mkdir(parents=True, exist_ok=True)
        final_file_path = choose_nonconflicting_path(final_file_path, ".lrc" if lyrics_synced else None)
        temp_dest = final_file_path.with_name(f".{final_file_path.stem}.{uuid.uuid4().hex}.tmp.{ext}")

        # Download only a recognized image payload for the resolved track.
        if resolved_cover_url:
            try:
                art_req = urllib.request.Request(resolved_cover_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(art_req, timeout=10) as aresp:
                    ctype = aresp.headers.get("Content-Type", "")
                    raw_picture = aresp.read()
                    pic_bytes, detected_mime = validate_picture_bytes(raw_picture, ctype)
                    if pic_bytes:
                        pic_mime = detected_mime
                    else:
                        print("[-] Cover art response was not a recognized image; skipping artwork")
            except Exception as e:
                print(f"[-] Cover art download error: {safe_process_error(str(e), limit=500)}")

        comments = {
            "TITLE": title,
            "ARTIST": artist,
            "ALBUM": album,
            "ALBUMARTIST": album_artist,
            "DATE": str(year) if year else "",
            "TRACKNUMBER": str(track_num) if track_num else "",
            "DISCNUMBER": str(disc_num) if disc_num else "",
            "ISRC": isrc,
            "GENRE": genre,
            "COMPOSER": composer,
            "COPYRIGHT": copyright_text,
            "PROVIDER": provider,
            "REALMAX_FORMAT": fmt,
            "SOURCE_QUALITY": str(stream_info.get("sourceQuality") or fmt),
            "SOURCE_CODEC": str(stream_info.get("sourceCodec") or fmt),
            "SOURCE_SAMPLE_RATE_HZ": str(stream_info.get("sourceSampleRate") or ""),
            "SOURCE_CHANNELS": str(stream_info.get("sourceChannels") or ""),
            "SOURCE_BIT_DEPTH": str(stream_info.get("sourceBitDepth") or ""),
            "SOURCE_BITRATE_BPS": str(stream_info.get("sourceBitrate") or ""),
            "SOURCE_BITRATE_BASIS": str(stream_info.get("sourceBitrateBasis") or ""),
            "COMMENT": f"Omniphony EX RealMAX ({fmt}) via {provider}",
        }
        if lyrics_text:
            comments["LYRICS"] = lyrics_text
        if lyrics_synced:
            comments["SYNCEDLYRICS"] = lyrics_synced

        if fmt == "EAC3_JOC":
            comments["SPATIAL_BINAURAL"] = "Dolby Atmos E-AC-3 JOC rendered to stereo PCM"
            comments["RENDERER_REQUESTED"] = "mpv --ad=orender"
            mpv_input = stream_uri
            candidate_proxy = None
            if transport == "DASH" and stream_info.get("manifestXml"):
                candidate_proxy = TidalDashProxy(
                    stream_info.get("manifestXml"),
                    stream_info.get("segmentUrls"),
                    stream_info.get("requestSession") or requests.Session(),
                )
                mpv_input = candidate_proxy.start()

            cmd = build_joc_download_render_command(mpv_input, temp_dest, transport)
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=300)
                if res.returncode != 0:
                    detail = safe_process_error(res.stderr, res.stdout)
                    raise RuntimeError(f"mpv binaural FLAC render failed (exit {res.returncode}): {detail}")
            finally:
                if candidate_proxy:
                    candidate_proxy.close()

            flac_bytes = temp_dest.read_bytes()
            rendered_info = inspect_flac_stream(flac_bytes)
            if rendered_info["channels"] != 2:
                raise RuntimeError("E-AC-3 JOC render did not produce stereo output")
            comments["OUTPUT_SAMPLE_RATE_HZ"] = str(rendered_info["sampleRate"])
            comments["OUTPUT_CHANNELS"] = str(rendered_info["channels"])
            comments["OUTPUT_BIT_DEPTH"] = str(rendered_info["bitsPerSample"])
            tagged_flac = inject_flac_metadata(flac_bytes, comments, picture_bytes=pic_bytes, picture_mime=pic_mime)
            temp_dest.write_bytes(tagged_flac)

        elif fmt == "AC4_IMS":
            comments["SPATIAL_BINAURAL"] = "Dolby AC-4 IMS -> 48kHz Binaural (OxideAV)"
            dl_resp = requests.get(stream_uri, timeout=45)
            dl_resp.raise_for_status()
            ac4_bytes = dl_resp.content
            temp_ac4 = CACHE_DIR / f".dl_{track_id}_{uuid.uuid4().hex}.mp4"
            temp_wav = CACHE_DIR / f".dl_{track_id}_{uuid.uuid4().hex}.wav"
            try:
                temp_ac4.write_bytes(ac4_bytes)
                conv_res = subprocess.run([str(AC4_CONVERTER), str(temp_ac4), str(temp_wav)], capture_output=True, timeout=120)
                if conv_res.returncode != 0 or not temp_wav.is_file():
                    raise RuntimeError("OxideAV AC-4 decode failed")
                wav_info = validate_oxideav_wav(temp_wav)
                comments["OUTPUT_SAMPLE_RATE_HZ"] = str(wav_info["sampleRate"])
                comments["OUTPUT_CHANNELS"] = str(wav_info["channels"])
                comments["OUTPUT_BIT_DEPTH"] = str(wav_info["sampleWidth"] * 8)
                comments["SPATIAL_BINAURAL"] = "Dolby AC-4 IMS decoded to stereo PCM by OxideAV"

                if ac4_fmt == "wav":
                    shutil.move(temp_wav, temp_dest)
                    tagged_wav = tag_wav_bytes(temp_dest.read_bytes(), comments, picture_bytes=pic_bytes, picture_mime=pic_mime)
                    temp_dest.write_bytes(tagged_wav)
                    validate_oxideav_wav(temp_dest)
                else:
                    cmd = [str(MPV_EXE), str(temp_wav), "--vid=no", "--vo=null", f"-o={temp_dest}", "--oac=flac", "--ytdl=no"]
                    c_res = subprocess.run(cmd, capture_output=True, timeout=120)
                    if c_res.returncode != 0:
                        raise RuntimeError("Failed to encode AC-4 WAV to FLAC")
                    flac_bytes = temp_dest.read_bytes()
                    flac_info = inspect_flac_stream(flac_bytes)
                    if flac_info["channels"] != 2:
                        raise RuntimeError("AC-4 FLAC output did not preserve stereo decoded PCM")
                    comments["OUTPUT_SAMPLE_RATE_HZ"] = str(flac_info["sampleRate"])
                    comments["OUTPUT_CHANNELS"] = str(flac_info["channels"])
                    comments["OUTPUT_BIT_DEPTH"] = str(flac_info["bitsPerSample"])
                    tagged_flac = inject_flac_metadata(flac_bytes, comments, picture_bytes=pic_bytes, picture_mime=pic_mime)
                    temp_dest.write_bytes(tagged_flac)
            finally:
                temp_ac4.unlink(missing_ok=True)
                temp_wav.unlink(missing_ok=True)

        else:
            comments["AUDIO_QUALITY"] = fmt
            if transport == "DIRECT":
                r_dl = requests.get(stream_uri, timeout=60)
                r_dl.raise_for_status()
                flac_bytes = r_dl.content
            else:
                candidate_proxy = TidalDashProxy(
                    stream_info.get("manifestXml"),
                    stream_info.get("segmentUrls"),
                    stream_info.get("requestSession") or requests.Session(),
                )
                try:
                    mpv_input = candidate_proxy.start()
                    cmd = [str(MPV_EXE), mpv_input, "--vid=no", "--vo=null", f"-o={temp_dest}", "--oac=flac", "--ytdl=no"]
                    res = subprocess.run(cmd, capture_output=True, timeout=300)
                    if res.returncode != 0:
                        raise RuntimeError("DASH FLAC assembly failed")
                    flac_bytes = temp_dest.read_bytes()
                finally:
                    candidate_proxy.close()

            flac_info = inspect_flac_stream(flac_bytes)
            source_rate = stream_info.get("sourceSampleRate")
            source_depth = stream_info.get("sourceBitDepth")
            if source_rate and flac_info["sampleRate"] != int(source_rate):
                raise RuntimeError(f"FLAC output sample rate {flac_info['sampleRate']} Hz does not match source {source_rate} Hz")
            if source_depth and flac_info["bitsPerSample"] != int(source_depth):
                raise RuntimeError(f"FLAC output bit depth {flac_info['bitsPerSample']} does not match source {source_depth}")
            comments["OUTPUT_SAMPLE_RATE_HZ"] = str(flac_info["sampleRate"])
            comments["OUTPUT_CHANNELS"] = str(flac_info["channels"])
            comments["OUTPUT_BIT_DEPTH"] = str(flac_info["bitsPerSample"])
            tagged_flac = inject_flac_metadata(flac_bytes, comments, picture_bytes=pic_bytes, picture_mime=pic_mime)
            temp_dest.write_bytes(tagged_flac)

        if ext == "wav":
            validate_oxideav_wav(temp_dest)

        warnings = []
        if lyrics_synced:
            temp_lyrics = final_file_path.with_name(f".{final_file_path.stem}.{uuid.uuid4().hex}.tmp.lrc")
            temp_lyrics.write_text(lyrics_synced, encoding="utf-8", newline="\n")

        final_file_path = publish_file_without_overwrite(temp_dest, final_file_path)
        if temp_lyrics:
            try:
                lrc_path = final_file_path.with_suffix(".lrc")
                os.link(temp_lyrics, lrc_path)
                temp_lyrics.unlink(missing_ok=True)
                temp_lyrics = None
            except Exception as lyrics_exc:
                warnings.append(f"audio saved, but synced-lyrics sidecar could not be published: {lyrics_exc}")

        with DOWNLOAD_QUEUE_LOCK:
            ACTIVE_DOWNLOADS[job_id]["status"] = "completed"
            ACTIVE_DOWNLOADS[job_id]["progress"] = 100
            ACTIVE_DOWNLOADS[job_id]["outputPath"] = str(final_file_path)
            ACTIVE_DOWNLOADS[job_id]["warnings"] = warnings
            ACTIVE_DOWNLOADS[job_id]["metadata"] = {
                "title": title,
                "artist": artist,
                "album": album,
                "albumArtist": album_artist or None,
                "trackNumber": str(track_num) if track_num else None,
                "discNumber": str(disc_num) if disc_num else None,
                "year": str(year) if year else None,
                "isrc": isrc or None,
                "artworkEmbedded": bool(pic_bytes),
                "lyricsEmbedded": bool(lyrics_text or lyrics_synced),
                "syncedLyricsSidecar": str(final_file_path.with_suffix(".lrc")) if lyrics_synced and not warnings else None,
                "outputFormat": fmt,
            }

    except Exception as exc:
        safe_error = safe_process_error(str(exc), limit=500)
        print(f"[-] Download error for {title}: {safe_error}")
        with DOWNLOAD_QUEUE_LOCK:
            ACTIVE_DOWNLOADS[job_id]["status"] = "failed"
            ACTIVE_DOWNLOADS[job_id]["error"] = safe_error
            ACTIVE_DOWNLOADS[job_id]["errorType"] = type(exc).__name__
    finally:
        for leftover in (temp_dest, temp_lyrics):
            if leftover:
                try:
                    Path(leftover).unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    print(f"[-] Could not clean temporary download file: {cleanup_exc}")


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
        # This loopback API is intentionally callable from any browser origin.
        # Wildcard CORS cannot be combined with credentialed cookies, which
        # this local service does not use for its API requests.
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def reject_untrusted_origin(self, require_origin=False):
        origin = self.headers.get("Origin")
        if origin is not None or not require_origin:
            return False
        self.send_json({"error": "Origin header is required for preflight"}, status=403)
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
        try:
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            # The client disconnected while receiving the response. Do not
            # retry or attempt another response on this dead connection.
            self.close_connection = True

    def do_GET(self):
        if self.reject_untrusted_origin():
            return
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path in ("/omniphony", "/omniphony/manifest.json", "/omniphony.json", "/manifest.json"):
            manifest_path = PLUGIN_DIR / "manifest.json"
            if manifest_path.exists():
                data = manifest_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        elif parsed.path in ("/omniphony/omniphony.mjs", "/omniphony.mjs", "/omniphony-live-sync.mjs", "/index.js"):
            mjs_path = PLUGIN_DIR / "omniphony.mjs"
            if mjs_path.exists():
                data = mjs_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        elif parsed.path in ("/omniphony/store.json", "/store.json"):
            store_path = PLUGIN_DIR / "store.json"
            if store_path.exists():
                data = store_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        elif parsed.path == "/health":
            self.send_json({
                "status": "ok",
                "service": "OmniFusion Standalone Audio Engine",
                "version": SERVICE_VERSION,
                "runtimeBundleSha256": RUNTIME_BUNDLE_SHA256,
                "serviceInstanceId": SERVICE_INSTANCE_ID,
                "servicePid": os.getpid(),
                "tidalOwnerPid": TIDAL_OWNER_PID,
                "tidalOwnerCreateTime": TIDAL_OWNER_CREATE_TIME,
                "tidalOwnerSource": TIDAL_OWNER_SOURCE,
                "tidalOwnerAlive": (
                    _tidal_process_create_time(TIDAL_OWNER_PID) == TIDAL_OWNER_CREATE_TIME
                    if TIDAL_OWNER_PID is not None else None
                ),
                "pluginRoot": str(PLUGIN_DIR),
                "dataRoot": str(DATA_DIR),
                "mpvDirectory": str(MPV_DIR),
                "studioExecutable": str(STUDIO_EXE),
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
                public_settings = dict(load_settings())
                self.send_json(public_settings)
            except OSError as exc:
                print(f"[-] Config read error: {exc}")
                self.send_json({"error": "configuration unavailable; original file preserved"}, status=500)

        elif parsed.path == "/realmax/providers":
            force_refresh = urllib.parse.parse_qs(parsed.query).get("refresh", [""])[0] == "1"
            try:
                provider_config = realmax_provider_configuration(load_settings(), force_account_refresh=force_refresh)
                provider_config.pop("_internal_records", None)
                self.send_json(provider_config)
            except Exception as exc:
                self.send_json({"error": f"provider list unavailable ({type(exc).__name__})"}, status=502)

        elif parsed.path == "/eclipse/streams":
            if not eclipse_native_request_authorized(self.headers):
                self.send_json({"error": "native Eclipse discovery authorization failed"}, status=403)
                return
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            operation_id = query.get("operationId", [""])[0]
            if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", operation_id):
                self.send_json({"error": "a valid inventory operation ID is required"}, status=400)
                return
            provider_key = query.get("providerKey", [""])[0]
            if not isinstance(provider_key, str) or not re.fullmatch(r"eclipse:[0-9a-f]{20}", provider_key):
                self.send_json({"error": "a valid connected Eclipse providerKey is required"}, status=400)
                return
            try:
                # Inventory is a diagnostic for connected Eclipse addons; its
                # availability must not depend on whether playback is currently
                # configured for TIDAL-only mode.
                provider_config = realmax_provider_configuration(load_settings())
                addon_providers = provider_config.get("_internal_records", {})
                provider = addon_providers.get(provider_key)
                if (not isinstance(provider, dict)
                        or provider.get("accountConnected") is not True
                        or provider.get("available") is not True):
                    self.send_json({"error": "connected Eclipse provider is unavailable or disabled"}, status=404)
                    return
                manifest_url = provider.get("manifestUrl")
                if not isinstance(manifest_url, str) or not manifest_url:
                    self.send_json({"error": "selected Eclipse provider has no valid manifest"}, status=404)
                    return
                try:
                    duration = float(query.get("duration", ["0"])[0] or 0)
                except (TypeError, ValueError, OverflowError):
                    duration = 0
                try:
                    timeout_ms = max(1, min(120_000, int(query.get("timeoutMs", ["120000"])[0] or 120_000)))
                except (TypeError, ValueError, OverflowError):
                    timeout_ms = 120_000
                cancel_event = _register_eclipse_inventory_operation(operation_id)
                if cancel_event is None:
                    self.send_json({"error": "inventory operation is duplicate or capacity is full"}, status=409)
                    return
                inventory = discover_eclipse_addon_streams(
                    manifest_url,
                    addon_id=str(provider.get("addonId") or ""),
                    addon_name=str(provider.get("name") or provider.get("displayName") or ""),
                    track_id=query.get("trackId", [""])[0],
                    title=query.get("title", [""])[0],
                    artist=query.get("artist", [""])[0],
                    isrc=query.get("isrc", [""])[0],
                    duration=duration,
                    provider_key=provider_key,
                    timeout_ms=timeout_ms,
                    cancel_event=cancel_event,
                )
                _unregister_eclipse_inventory_operation(operation_id, cancel_event)
                self.send_json(inventory)
            except Exception as exc:
                if "cancel_event" in locals() and cancel_event is not None:
                    _unregister_eclipse_inventory_operation(operation_id, cancel_event)
                self.send_json({"error": f"Eclipse stream discovery failed ({type(exc).__name__})"}, status=502)

        elif parsed.path == "/login/status":
            self.send_json(public_login_flow())

        elif parsed.path == "/eclipse-auth/status":
            self.send_json(public_eclipse_auth_status())

        elif parsed.path == "/eclipse-auth/addons":
            force_refresh = urllib.parse.parse_qs(parsed.query).get("refresh", [""])[0] == "1"
            account = eclipse_account_addons(force_refresh=force_refresh)
            self.send_json(public_eclipse_account_addons(account))

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

        elif parsed.path == "/lyrics":
            query_params = urllib.parse.parse_qs(parsed.query)
            title_val = query_params.get("title", [""])[0].strip()
            artist_val = query_params.get("artist", [""])[0].strip()
            track_id_val = query_params.get("trackId", [""])[0].strip()
            lyrics_data = None
            try:
                sess = get_python_tidal_session()
                t_obj = None
                if sess and track_id_val.isdigit():
                    try:
                        t_obj = sess.track(int(track_id_val))
                    except Exception:
                        pass
                if sess and not t_obj and title_val:
                    import tidalapi
                    q_str = f"{artist_val} {title_val}".strip()
                    search_res = sess.search(q_str, models=[tidalapi.media.Track], limit=3)
                    for cand in search_res.get("tracks", []):
                        if cand and getattr(cand, "name", None):
                            t_obj = cand
                            break
                if t_obj and hasattr(t_obj, "lyrics"):
                    lyr = t_obj.lyrics()
                    if lyr and (getattr(lyr, "subtitles", None) or getattr(lyr, "text", None)):
                        sub = getattr(lyr, "subtitles", "") or ""
                        txt = getattr(lyr, "text", "") or ""
                        lines = []
                        if sub:
                            for line in sub.splitlines():
                                m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\s*\]\s*(.*)", line)
                                if m:
                                    sec = int(m.group(1)) * 60 + float(m.group(2))
                                    lines.append({"startTime": sec, "text": m.group(3).strip()})
                        if not lines and txt:
                            for idx, line in enumerate(txt.splitlines()):
                                if line.strip():
                                    lines.append({"startTime": idx * 4.0, "text": line.strip()})
                        if lines:
                            lyrics_data = {
                                "type": "Line",
                                "data": lines,
                                "trackId": str(t_obj.id),
                            }
            except Exception as exc:
                print(f"[-] Service /lyrics resolution error: {exc}")
            if lyrics_data:
                self.send_json(lyrics_data)
            else:
                self.send_json({"error": "lyrics unavailable"}, status=404)

        elif parsed.path == "/search/multi":
            query_params = urllib.parse.parse_qs(parsed.query)
            q = query_params.get("q", [""])[0]
            limit_val = 10
            try:
                limit_val = int(query_params.get("limit", [10])[0])
            except Exception:
                limit_val = 10
            search_results = execute_multi_source_search(q, limit=limit_val)
            self.send_json(search_results)

        elif parsed.path == "/download/status":
            with DOWNLOAD_QUEUE_LOCK:
                self.send_json({"jobs": list(ACTIVE_DOWNLOADS.values())})

        elif parsed.path == "/status":
            if engine.is_mpv_media_loading() or engine.is_playback_preparing():
                # MPV properties such as path and duration can block while the
                # DASH demuxer opens. Do not let routine UI polling time out and
                # invalidate the process that is actively fetching segments.
                self.send_json(mpv_loading_status_payload(engine))
                return
            position_sample_started_unix_ms = time.time_ns() / 1_000_000
            position_sample_started_monotonic_ns = time.monotonic_ns()
            pos_res = engine.clock_cmd(["get_property", "time-pos"])
            position_sample_elapsed_ns = max(
                0, time.monotonic_ns() - position_sample_started_monotonic_ns
            )
            time_pos_sampled_at_unix_ms = (
                position_sample_started_unix_ms + position_sample_elapsed_ns / 2_000_000
                if pos_res and pos_res.get("error") in (None, "success")
                else None
            )
            dur_res = engine.clock_cmd(["get_property", "duration"])
            pause_res = engine.clock_cmd(["get_property", "pause"])
            dev_res = engine.clock_cmd(["get_property", "audio-device"])
            codec_res = engine.clock_cmd(["get_property", "audio-codec-name"])
            params_res = engine.clock_cmd(["get_property", "audio-params"])
            output_params_res = engine.clock_cmd(["get_property", "audio-out-params"])
            bitrate_res = engine.clock_cmd(["get_property", "audio-bitrate"])
            decoder_res = engine.clock_cmd(["get_property", "current-tracks/audio/decoder"])
            position = pos_res.get("data") if pos_res else None
            paused = bool(pause_res.get("data", True)) if pause_res else True
            audio_params = params_res.get("data", {}) if params_res else {}
            path_res = engine.clock_cmd(["get_property", "path"])
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
                            decoder_res = engine.clock_cmd(["get_property", "current-tracks/audio/decoder"])
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
                                if current_meta.get("lastFailure") in {
                                    "Omniphony renderer disconnected",
                                    "Omniphony decoder not active",
                                }:
                                    current_meta.pop("lastFailure", None)
                            else:
                                pause_result = engine.cmd(["set_property", "pause", True])
                                pause_succeeded = mpv_command_succeeded(pause_result)
                                if pause_succeeded:
                                    paused = True
                                playback_state = "failed"
                                current_meta["playbackState"] = playback_state
                                if pause_succeeded:
                                    if current_meta.get("lastFailure") == "Omniphony renderer disconnected":
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

            if not joc_snapshot:
                refresh_invalidated_joc_route_telemetry(engine)
            
            with engine.playback_generation_lock:
                status_metadata = dict(engine.current_meta)
                status_proxy = engine.stream_proxy
            track_status = public_track_status(status_metadata, playback_state)
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
                "streamTransport": track_status.get("streamTransport"),
                "streamProxyRequests": (
                    status_proxy.segment_requests
                    if track_status["trackId"] is not None
                    and track_status.get("streamTransport") is not None
                    and status_proxy is not None
                    else 0
                ),
                "streamProxyBytes": (
                    status_proxy.bytes_served
                    if track_status["trackId"] is not None
                    and track_status.get("streamTransport") is not None
                    and status_proxy is not None
                    else 0
                ),
                "streamProxyFailures": (
                    status_proxy.failed_segments
                    if track_status["trackId"] is not None
                    and track_status.get("streamTransport") is not None
                    and status_proxy is not None
                    else 0
                ),
            })

        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self):
        try:
            return self._do_POST()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            route = urllib.parse.urlsplit(getattr(self, "path", "")).path
            error_type = type(exc).__name__
            print(f"[-] POST {route} handler failed unexpectedly ({error_type})")
            try:
                self.send_json({
                    "error": "request failed unexpectedly",
                    "code": "INTERNAL_ERROR",
                    "errorType": error_type,
                }, status=500)
            except OSError:
                self.close_connection = True

    def _do_POST(self):
        global _ECLIPSE_LAST_LOGIN_ERROR, _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID, _ECLIPSE_ACCOUNT_SESSION_GENERATION
        if self.reject_untrusted_origin(require_origin=True):
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/eclipse-auth/complete", "/eclipse-auth/flow-status", "/eclipse/streams/cancel"}:
            if not eclipse_native_request_authorized(self.headers):
                return self.send_json({"error": "native Eclipse request authorization failed"}, status=403)
        if parsed.path == "/shutdown":
            if not shutdown_request_authorized(self.headers):
                return self.send_json({"error": "service shutdown authorization failed"}, status=403)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 5000000:
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

        if parsed.path == "/eclipse/streams/cancel":
            operation_id = body.get("operationId")
            if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", operation_id):
                return self.send_json({"error": "a valid inventory operation ID is required"}, status=400)
            return self.send_json({"canceled": _cancel_eclipse_inventory_operation(operation_id)})

        if parsed.path == "/eclipse-auth/complete":
            if not ECLIPSE_ACCOUNT_LINKING_SUPPORTED:
                return self.send_json({
                    "connected": False,
                    "error": "unsupported_account_integration",
                }, status=501)
            token = body.get("token")
            callback_user_id = body.get("userId")
            callback_email = body.get("email")
            attempt_id = body.get("attemptId")
            if not isinstance(token, str) or not token.strip():
                return self.send_json({"connected": False, "error": "missing account token"}, status=400)
            if (not isinstance(callback_user_id, str) or not callback_user_id.strip()
                    or len(callback_user_id) > 512
                    or any(ord(char) < 0x20 or ord(char) == 0x7F for char in callback_user_id)
                    or not isinstance(callback_email, str) or not callback_email.strip()
                    or len(callback_email) > 512
                    or any(ord(char) < 0x20 or ord(char) == 0x7F for char in callback_email)):
                return self.send_json({"connected": False, "error": "invalid callback identity"}, status=400)
            if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
                return self.send_json({"connected": False, "error": "invalid login attempt"}, status=400)
            with _ECLIPSE_AUTH_LOCK:
                if _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID != attempt_id:
                    return self.send_json({"connected": False, "error": "stale_login_attempt"}, status=409)
            connected, user, error = _complete_eclipse_login(
                token.strip(), attempt_id, callback_user_id.strip(), callback_email.strip()
            )
            if not connected:
                with _ECLIPSE_AUTH_LOCK:
                    if _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID != attempt_id:
                        return self.send_json({"connected": False, "error": "stale_login_attempt"}, status=409)
                    _ECLIPSE_LAST_LOGIN_ERROR = error or "verification_failed"
                    _finalize_eclipse_login_attempt_locked(attempt_id)
            if not connected:
                status = 401 if error in {
                    "unauthorized", "synthetic_account_rejected", "account_not_verified",
                    "invalid_token", "identity_mismatch",
                } else 502
                return self.send_json({"connected": False, "error": error}, status=status)
            return self.send_json({"connected": True, "user": user})

        elif parsed.path == "/eclipse-auth/flow-status":
            if not ECLIPSE_ACCOUNT_LINKING_SUPPORTED:
                return self.send_json({
                    "recorded": False,
                    "error": "unsupported_account_integration",
                }, status=501)
            reason = body.get("reason")
            attempt_id = body.get("attemptId")
            result = record_eclipse_login_flow_status(reason, attempt_id)
            if result == "invalid":
                return self.send_json({"error": "unsupported login flow status"}, status=400)
            if result == "busy":
                return self.send_json({"error": "another Eclipse sign-in attempt is already active"}, status=409)
            if result == "stale":
                return self.send_json({"error": "login attempt is no longer active"}, status=409)
            return self.send_json({"recorded": True})

        elif parsed.path == "/eclipse-auth/disconnect":
            with _ECLIPSE_AUTH_LOCK:
                try:
                    ECLIPSE_SESSION_FILE.unlink(missing_ok=True)
                except OSError:
                    return self.send_json({"error": "could not remove the saved account session"}, status=500)
                if _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID is not None:
                    _finalize_eclipse_login_attempt_locked(_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID)
                _invalidate_eclipse_account_cache(clear_jwt=True)
                _ECLIPSE_LAST_LOGIN_ERROR = None
                _ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = None
                _ECLIPSE_ACCOUNT_SESSION_GENERATION += 1
            return self.send_json({"connected": False})

        elif parsed.path == "/diag":
            debug_dump = DATA_DIR / "logs" / "tidal_redux_dump.json"
            debug_dump.parent.mkdir(parents=True, exist_ok=True)
            with open(debug_dump, "w", encoding="utf-8") as f:
                json.dump(body, f, indent=2, default=str)
            return self.send_json({"ok": True})

        if parsed.path == "/config":
            if not isinstance(body, dict) or not set(body).issubset({
                "priority", "realmax_source", "realmax_provider_order"
            }):
                return self.send_json({"error": "unsupported configuration field"}, status=400)
            updates = {}
            if "priority" in body:
                priority = body["priority"]
                if (not isinstance(priority, list)
                        or len(priority) != len(SUPPORTED_FORMAT_PRIORITY)
                        or any(not isinstance(item, str) for item in priority)
                        or set(priority) != set(SUPPORTED_FORMAT_PRIORITY)):
                    return self.send_json({
                        "error": "priority must contain each supported format exactly once",
                        "supported": list(SUPPORTED_FORMAT_PRIORITY),
                    }, status=400)
                updates["priority"] = priority
            if "realmax_source" in body and body["realmax_source"] in REALMAX_SOURCE_ALIASES:
                updates["realmax_source"] = normalize_realmax_source(body["realmax_source"])
            elif "realmax_source" in body:
                return self.send_json({
                    "error": "realmax_source must be both, tidal_api, or eclipse_addons",
                    "supported": ["both", "tidal_api", "eclipse_addons"],
                }, status=400)
            if "realmax_provider_order" in body:
                try:
                    updates["realmax_provider_order"] = validate_realmax_provider_order(
                        body["realmax_provider_order"]
                    )
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, status=400)
            try:
                settings = update_settings(updates)
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
            provider_key = body.get("providerKey")
            time_pos = body.get("timePos", 0.0)
            title = body.get("title", "")
            artist = body.get("artist", "")
            start_paused = body.get("startPaused", False)
            request_id = body.get("requestId")

            if not track_id:
                return self.send_json({"error": "missing trackId"}, status=400)
            if provider_key is not None:
                try:
                    validate_realmax_provider_order([provider_key])
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, status=400)
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
                track_id_param = int(track_id) if str(track_id).strip().isdigit() else str(track_id).strip()
                stream_info = resolve_with_priority(
                    track_id_param, priority, title_hint=title, artist_hint=artist,
                    provider_key=provider_key,
                )
            except RuntimeError as exc:
                return self.send_json({
                    "error": str(exc),
                    "trackId": track_id,
                    "sourceMode": normalize_realmax_source(settings.get("realmax_source", "both")),
                }, status=404)
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

            if stream_requires_dash_proxy(fmt, stream_info):
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
                # Cache only the encoded source MP4. OxideAV decodes it into a
                # seekable local raw-PCM endpoint; playback never writes a WAV.
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                active_proxy = engine.stream_proxy
                cache_result = prune_ac4_playback_cache(
                    CACHE_DIR,
                    protected_paths=(getattr(active_proxy, "input_path", None),),
                )
                if cache_result["removedFiles"]:
                    print(
                        "[+] Pruned AC-4 playback cache: "
                        f"{cache_result['removedFiles']} files, "
                        f"{cache_result['removedBytes']} bytes removed"
                    )
                download_temp = CACHE_DIR / f".ac4_{track_id}_{uuid.uuid4().hex}.download"
                try:
                    print(f"[*] Downloading AC-4 IMS stream for {track_id}...")
                    dl_resp = requests.get(uri, timeout=30)
                    dl_resp.raise_for_status()
                    media_bytes = dl_resp.content
                    source_hash = hashlib.sha256(media_bytes).hexdigest()
                    mp4_cache = CACHE_DIR / f"ac4_{track_id}_{source_hash[:20]}.mp4"
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

                finally:
                    try:
                        download_temp.unlink(missing_ok=True)
                    except OSError:
                        pass
                decoded_metrics = {"channels": 2, "sampleRate": source_rate}
                candidate_proxy = Ac4RawPcmProxy(
                    AC4_CONVERTER,
                    mp4_cache,
                    source_rate,
                    decoded_metrics["channels"],
                    source_metrics["sourceDuration"],
                )
                try:
                    play_target = candidate_proxy.start()
                    self._candidate_stream_proxy = candidate_proxy
                except Exception:
                    candidate_proxy.close()
                    raise
                stream_info.update(source_metrics)
                stream_info["decodedChannels"] = decoded_metrics["channels"]
                stream_info["decodedSampleRate"] = decoded_metrics["sampleRate"]
                stream_info["presentation"] = "IMS"
                stream_info["transport"] = "RAW_PCM"
                stream_info["playbackTransport"] = "OXIDEAV_RAW_S16LE"
                stream_info["presentationVerified"] = False
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
                        if fmt == "AC4_IMS":
                            prepare_mpv_raw_audio(
                                int(stream_info.get("decodedSampleRate") or 0),
                                int(stream_info.get("decodedChannels") or 0),
                                engine.cmd,
                            )
                        else:
                            reset_mpv_raw_audio(engine.cmd)
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
                    engine.current_meta = {
                        **engine.current_meta,
                        "trackId": track_id,
                        "generation": generation,
                        "playbackState": "loading",
                        "streamTransport": None,
                        "format": fmt,
                        "codec": stream_info.get("codec"),
                        "label": stream_info.get("label"),
                        "title": title,
                        "artist": artist,
                        "requestId": request_id,
                    }
                    media_load_epoch = engine.begin_mpv_media_load()
                    load_result = engine.cmd(["loadfile", play_target, "replace"])
                    if not mpv_command_succeeded(load_result):
                        engine.finish_mpv_media_load(media_load_epoch, resolved=True)
                        with engine.playback_generation_lock:
                            if is_current_play_request():
                                engine.current_meta = {
                                    **engine.current_meta,
                                    "trackId": None,
                                    "playbackState": "failed",
                                    "label": "Load command failed",
                                }
                                engine.active_generation = 0
                                engine.prepared_generation = 0
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
                # The matching file-loaded event proves demuxer startup has
                # ended. Keep the preparation guard active through format and
                # renderer checks, while allowing terminal failures below to
                # expose their authoritative failed status instead of leaving
                # the load guard latched forever.
                engine.finish_mpv_media_load(media_load_epoch, resolved=True)
                # MPV confirmed the replacement; the prior file can no longer
                # request its segments, so release that proxy now.
                with engine.playback_generation_lock:
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
                if fmt == "AC4_IMS":
                    try:
                        actual_rate = int(decoded_params.get("samplerate"))
                        actual_channels = int(decoded_params.get("channel-count"))
                    except (TypeError, ValueError, OverflowError):
                        actual_rate, actual_channels = 0, 0
                    if (
                        source_mode != "DOLBY_ATMOS"
                        or not source_ims_atmos_verified
                        or source_channels != 2
                        or actual_channels != 2
                        or actual_rate != source_rate
                    ):
                        with engine.playback_generation_lock:
                            engine.current_meta = {
                                **engine.current_meta,
                                "trackId": None,
                                "playbackState": "failed",
                                "label": "AC-4 IMS PCM verification failed",
                            }
                            engine.active_generation = 0
                        return self.send_json({
                            "error": "OxideAV raw playback did not produce the verified AC-4 IMS stereo sample rate",
                            "format": "AC4_UNVERIFIED",
                            "sourceChannels": source_channels,
                            "decodedChannels": actual_channels,
                            "sourceSampleRate": source_rate,
                            "decodedSampleRate": actual_rate,
                            "trackId": track_id,
                        }, status=422)
                    stream_info["decodedChannels"] = actual_channels
                    stream_info["decodedSampleRate"] = actual_rate
                    stream_info["presentationVerified"] = True
                    if not mark_verified_ac4_ims(stream_info):
                        return self.send_json({
                            "error": "OxideAV output did not preserve verified AC-4 IMS stereo",
                            "format": "AC4_UNVERIFIED",
                            "sourceChannels": source_channels,
                            "decodedChannels": actual_channels,
                            "sourceSampleRate": source_rate,
                            "decodedSampleRate": actual_rate,
                            "trackId": track_id,
                        }, status=422)
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
                        "sourceProvider": stream_info.get("sourceProvider", "TIDAL"),
                        "sourceName": stream_info.get("sourceName") or ("TIDAL API" if stream_info.get("sourceProvider", "TIDAL") == "TIDAL" else ""),
                        "presentation": stream_info.get("presentation"),
                        "presentationVerified": stream_info.get("presentationVerified", False),
                        "decodedSampleRate": stream_info.get("decodedSampleRate"),
                        "decodedChannels": stream_info.get("decodedChannels"),
                        "audioProfile": audio_profile,
                        "title": title,
                        "artist": artist,
                        "album": body.get("album") or stream_info.get("album", ""),
                        "cover": body.get("cover") or body.get("art") or stream_info.get("cover", ""),
                        "artworkURL": body.get("cover") or body.get("art") or stream_info.get("cover", ""),
                        "requestId": request_id,
                        "streamTransport": stream_transport_label(candidate_proxy),
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
                    engine.finish_mpv_media_load(media_load_epoch, resolved=True)

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

        elif parsed.path == "/shutdown":
            # Let the normal serve_forever/finally path stop owned mpv and close
            # proxies. os._exit() here skipped cleanup and could leave children
            # or files behind. shutdown() must run off the serving thread.
            self.send_json({"status": "shutting_down"})
            schedule_server_shutdown(self.server)
            return

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
                    engine.pause_intent_revision += 1
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
                        decoder_confirmed = is_active_orender_decoder(active_decoder)
                        route_status.setdefault("checks", {})["activeDecoder"] = decoder_confirmed
                        route_status["ready"] = (
                            joc_route_is_verified(route_status, active_decoder)
                            and decoder_confirmed
                        )
                        if not route_status["ready"]:
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
                    engine.pause_intent_revision += 1
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
            pause_intent_revision = None
            recover_raw_ac4_pause = False
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
                                pause_intent_revision = engine.pause_intent_revision
                                recover_raw_ac4_pause = (
                                    engine.current_meta.get("format") == "AC4_IMS"
                                    and engine.current_meta.get("streamTransport")
                                    == STREAM_TRANSPORT_OXIDEAV_RAW_S16LE
                                )
                                pre_seek_position_result = engine.cmd(["get_property", "time-pos"])
                                pre_seek_state_result = engine.cmd(["get_property", "seeking"])
                                proxy = getattr(engine, "stream_proxy", None)
                                proxy_requests_before, proxy_indices_before = (
                                    snapshot_seek_proxy_counters(proxy)
                                )
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
            seek_timeout = seek_settle_timeout_seconds(
                getattr(engine, "stream_proxy", None), pos
            )
            settled, observed_position, settle_error = engine.wait_for_seek_position(
                pos, seek_sequence, track_id, generation, request_id,
                timeout=seek_timeout,
                expected_paused=expected_paused,
                diagnostic_samples=seek_samples,
                expected_pause_intent_revision=pause_intent_revision,
                recover_raw_ac4_pause=recover_raw_ac4_pause,
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
                    "settleTimeoutSeconds": seek_timeout,
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
                    proxy_requests_after, proxy_indices_after = (
                        snapshot_seek_proxy_counters(proxy)
                    )
                    seek_diagnostic["proxyRequestsBefore"] = proxy_requests_before
                    seek_diagnostic["proxyRequestsAfter"] = proxy_requests_after
                    seek_diagnostic["proxyRecentIndexesBefore"] = proxy_indices_before
                    seek_diagnostic["proxyRecentIndexesAfter"] = proxy_indices_after
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

        elif parsed.path == "/download":
            track_id = body.get("trackId")
            if not track_id:
                return self.send_json({"error": "missing trackId"}, status=400)
            if body.get("providerKey") is not None:
                try:
                    validate_realmax_provider_order([body.get("providerKey")])
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, status=400)
            job_id = uuid.uuid4().hex
            threading.Thread(target=execute_track_download, args=(job_id, body), daemon=True).start()
            self.send_json({"status": "queued", "jobId": job_id})

        elif parsed.path == "/download/status":
            job_id = body.get("jobId")
            with DOWNLOAD_QUEUE_LOCK:
                if job_id:
                    info = ACTIVE_DOWNLOADS.get(job_id)
                    if not info:
                        return self.send_json({"error": "job not found"}, status=404)
                    return self.send_json(info)
                return self.send_json({"jobs": list(ACTIVE_DOWNLOADS.values())})

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
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.serve_ready = threading.Event()
        self.shutdown_requested = threading.Event()
        super().__init__(*args, **kwargs)

    def service_actions(self):
        # BaseServer calls this only from inside its active serve_forever loop.
        self.serve_ready.set()


def schedule_server_shutdown(server):
    """Request orderly HTTP shutdown without blocking a request worker."""
    if hasattr(server, "shutdown_requested"):
        server.shutdown_requested.set()
    worker = threading.Thread(target=server.shutdown, daemon=True)
    worker.start()
    return worker


def shutdown_request_authorized(headers):
    """Require a private per-launch credential and the exact service instance."""
    authorization = headers.get("Authorization", "")
    supplied_token = authorization[7:] if authorization.startswith("Bearer ") else ""
    supplied_instance = headers.get("X-Omniphony-Service-Instance", "")
    return bool(
        SHUTDOWN_TOKEN
        and hmac.compare_digest(supplied_token, SHUTDOWN_TOKEN)
        and hmac.compare_digest(supplied_instance, SERVICE_INSTANCE_ID)
    )


def eclipse_native_request_authorized(headers):
    """Require the current native supervisor's per-launch service credential."""
    return shutdown_request_authorized(headers)


def _tidal_process_create_time(pid):
    """Return the start time for a live TIDAL.exe process, or None if it is gone."""
    import psutil

    try:
        process = psutil.Process(int(pid))
        if process.name().casefold() != "tidal.exe":
            return None
        return process.create_time()
    except (psutil.NoSuchProcess, psutil.ZombieProcess, TypeError, ValueError):
        return None
    except psutil.AccessDenied:
        print(f"[-] Cannot verify TIDAL owner PID {pid}; stopping the bound service rather than treating it as alive.")
        return None


def tidal_process_watchdog(engine, server, owner_pid=None):
    """Stop only the service bound to its exact verified TIDAL process identity."""
    owner_pid = owner_pid if owner_pid is not None else TIDAL_OWNER_PID
    if owner_pid is None:
        print("[-] Service watchdog requires a verified TIDAL owner; refusing ownerless operation.")
        if hasattr(server, "shutdown_requested"):
            server.shutdown_requested.set()
        server.shutdown()
        return
    owner_create_time = (
        TIDAL_OWNER_CREATE_TIME
        if owner_pid == TIDAL_OWNER_PID and TIDAL_OWNER_CREATE_TIME is not None
        else _tidal_process_create_time(owner_pid)
    )
    if owner_create_time is None:
        print("[*] TIDAL owner process is already gone. Shutting down OmniFusion service...")
        if hasattr(server, "shutdown_requested"):
            server.shutdown_requested.set()
        server.shutdown()
        return
    while True:
        time.sleep(0.5)
        current_create_time = _tidal_process_create_time(owner_pid)
        if current_create_time is None or current_create_time != owner_create_time:
            print("[*] Owning TIDAL process exited. Stopping OmniFusion service and its owned mpv process...")
            if hasattr(server, "shutdown_requested"):
                server.shutdown_requested.set()
            server.shutdown()
            return

_SINGLE_INSTANCE_MUTEX = None

def ensure_single_instance() -> bool:
    """Strictly enforce that only one OmniFusion service instance is active."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/health",
            headers={"User-Agent": "OmniFusion-SingleInstance-Probe"}
        )
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            if resp.status == 200:
                print(f"[+] OmniFusion service is already active and healthy on port {PORT}. Exiting redundant process.")
                return False
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            CreateMutexW = kernel32.CreateMutexW
            CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            CreateMutexW.restype = wintypes.HANDLE
            ERROR_ALREADY_EXISTS = 183

            global _SINGLE_INSTANCE_MUTEX
            _SINGLE_INSTANCE_MUTEX = CreateMutexW(None, True, "Local\\OmniFusion_Audio_Engine_Port8765_Mutex")
            if not _SINGLE_INSTANCE_MUTEX:
                error = ctypes.get_last_error()
                print(f"[-] Could not create the service single-instance mutex (Windows error {error}); refusing a potentially duplicate launch.")
                return False
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                print("[*] Single-instance mutex already held by another process. Verifying health on port...")
                for _ in range(4):
                    try:
                        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/health", headers={"User-Agent": "OmniFusion-SingleInstance-Probe"})
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            if resp.status == 200:
                                print(f"[+] Active instance confirmed healthy on port {PORT}. Exiting cleanly.")
                                kernel32.CloseHandle(_SINGLE_INSTANCE_MUTEX)
                                _SINGLE_INSTANCE_MUTEX = None
                                return False
                    except Exception:
                        time.sleep(0.5)
                print("[-] Another service process owns the instance mutex but has not become healthy; refusing to start a duplicate.")
                kernel32.CloseHandle(_SINGLE_INSTANCE_MUTEX)
                _SINGLE_INSTANCE_MUTEX = None
                return False
        except Exception as exc:
            print(f"[-] Single-instance mutex check failed; refusing a potentially duplicate launch: {exc}")
            return False
    return True

def run():
    global TIDAL_OWNER_CREATE_TIME
    if TIDAL_OWNER_PID is None:
        print(f"[-] Refusing to start without a verified TIDAL owner (source: {TIDAL_OWNER_SOURCE}).")
        sys.exit(1)
    TIDAL_OWNER_CREATE_TIME = _tidal_process_create_time(TIDAL_OWNER_PID)
    if TIDAL_OWNER_CREATE_TIME is None:
        print("[-] Refusing to start: the configured TIDAL owner PID is not a verifiable live TIDAL process.")
        sys.exit(1)
    if not ensure_single_instance():
        sys.exit(0)

    try:
        server = ThreadedHTTPServer(("127.0.0.1", PORT), RequestHandler)
    except OSError as exc:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/health")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    print(f"[+] Port {PORT} already bound and healthy. Exiting redundant instance.")
                    sys.exit(0)
        except Exception:
            pass
        print(f"[-] Fatal: could not bind port {PORT}: {exc}")
        sys.exit(1)

    settings = load_settings()
    save_settings(settings)
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    if not server.serve_ready.wait(timeout=5.0) or not serve_thread.is_alive():
        print("[-] HTTP server did not enter its serving loop; refusing to start the lifecycle watchdog or mpv.")
        server.server_close()
        sys.exit(1)

    print(f"[+] OmniFusion Standalone Audio Service listening on http://127.0.0.1:{PORT}")
    threading.Thread(
        target=tidal_process_watchdog,
        args=(engine, server),
        kwargs={"owner_pid": TIDAL_OWNER_PID},
        daemon=True,
    ).start()
    try:
        if MPV_EXE.is_file():
            if (not server.shutdown_requested.is_set()
                    and _tidal_process_create_time(TIDAL_OWNER_PID) == TIDAL_OWNER_CREATE_TIME):
                try:
                    engine.ensure_mpv(device=settings.get("audio_device", "auto"))
                except Exception as exc:
                    print(f"[-] Could not start the owned mpv engine: {type(exc).__name__}")
        else:
            print("[!] External MPV folder is not configured; set it in Omniphony EX settings.")
        serve_thread.join()
    except KeyboardInterrupt:
        if not server.shutdown_requested.is_set():
            server.shutdown_requested.set()
            server.shutdown()
        serve_thread.join(timeout=5.0)
    finally:
        try:
            if engine.stream_proxy is not None:
                try:
                    engine.stream_proxy.close()
                except Exception as exc:
                    print(f"[-] Could not close the owned stream proxy: {type(exc).__name__}")
                with engine.playback_generation_lock:
                    engine.stream_proxy = None
            if engine.pipe:
                try:
                    engine.pipe.close()
                except Exception as exc:
                    print(f"[-] Could not close the owned mpv pipe: {type(exc).__name__}")
            if engine.proc and engine.proc.poll() is None:
                try:
                    engine.proc.terminate()
                    engine.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        engine.proc.kill()
                        engine.proc.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        print(f"[-] Could not reap the owned mpv process: {type(exc).__name__}")
                except OSError as exc:
                    print(f"[-] Could not stop the owned mpv process: {type(exc).__name__}")
        finally:
            engine.stop_owned_omniphony_studio()

if __name__ == "__main__":
    run()




