"""Check that the local API belongs to this TIDAL plugin instance."""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


HEALTH_URL = "http://127.0.0.1:8765/health"


def _normalized_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return os.path.normcase(os.path.normpath(value.strip()))


def check_health(
    health: object,
    *,
    expected_version: str,
    expected_owner_pid: int,
    expected_plugin_root: str,
) -> bool:
    if not isinstance(health, dict):
        return False
    return (
        health.get("status") == "ok"
        and health.get("version") == expected_version
        and health.get("tidalOwnerPid") == expected_owner_pid
        and health.get("tidalOwnerAlive") is True
        and _normalized_path(health.get("pluginRoot"))
        == _normalized_path(expected_plugin_root)
    )


def read_health(url: str = HEALTH_URL) -> tuple[int, object | None]:
    """Return (0, JSON) on a response, (2, None) when nothing responds."""
    try:
        with urlopen(url, timeout=2) as response:
            return 0, json.loads(response.read().decode("utf-8"))
    except HTTPError:
        return 1, None
    except URLError as error:
        reason = error.reason
        refused = isinstance(reason, ConnectionRefusedError) or (
            isinstance(reason, OSError)
            and (
                reason.errno == errno.ECONNREFUSED
                or getattr(reason, "winerror", None) == 10061
            )
        )
        return (2, None) if refused else (1, None)
    except (OSError, TimeoutError, ValueError):
        # A response that cannot be parsed means a listener exists but is not trusted.
        return 1, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    owner_pid = os.environ.get("OMNIFUSION_TIDAL_PID", "")
    plugin_root = os.environ.get("OMNIFUSION_PLUGIN_DIR", "")
    if not owner_pid.isdecimal() or not plugin_root:
        return 1

    response_code, health = read_health()
    if response_code != 0:
        return response_code
    return 0 if check_health(
        health,
        expected_version=args.version,
        expected_owner_pid=int(owner_pid),
        expected_plugin_root=plugin_root,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
