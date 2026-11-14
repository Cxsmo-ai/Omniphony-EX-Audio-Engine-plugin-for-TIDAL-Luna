"""Loopback-only development store server with wildcard CORS for TidaLuna artifacts."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PLUGIN_BASE = "Omniphony EX Audio Engine"
ARTIFACTS = {
    "/store.json": (DIST / "store.json", "application/json; charset=utf-8"),
    f"/{PLUGIN_BASE}.json": (DIST / f"{PLUGIN_BASE}.json", "application/json; charset=utf-8"),
    f"/{PLUGIN_BASE}.mjs": (DIST / f"{PLUGIN_BASE}.mjs", "text/javascript; charset=utf-8"),
}


class LunaDevHandler(BaseHTTPRequestHandler):
    server_version = "OmniphonyLunaDev/1.0"

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if self.headers.get("Access-Control-Request-Private-Network", "").lower() == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

    def end_headers(self) -> None:
        self._cors_headers()
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(self, *, include_body: bool) -> None:
        path = unquote(urlsplit(self.path).path)
        artifact = ARTIFACTS.get(path)
        if artifact is None:
            self.send_error(404, "Artifact not found")
            return

        file_path, content_type = artifact
        try:
            resolved = file_path.resolve(strict=True)
            resolved.relative_to(DIST.resolve(strict=True))
            payload = resolved.read_bytes()
        except (OSError, ValueError):
            self.send_error(404, "Artifact unavailable")
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if include_body:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

    def do_GET(self) -> None:  # noqa: N802
        self._serve(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(include_body=False)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback TidaLuna store artifact server")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), LunaDevHandler)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
