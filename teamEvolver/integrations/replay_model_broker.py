"""Ephemeral loopback model broker for isolated local replay workers."""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from urllib.parse import urlsplit

import httpx


class _ThreadingUnixHTTPServer(
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer,
):
    daemon_threads = True


class ReplayModelBroker:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        socket_path: Path,
        token: str,
        timeout_seconds: int,
    ) -> None:
        endpoint = str(base_url or "").strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "replay model base_url must be an http(s) URL "
                "without embedded credentials"
            )
        if not str(api_key or ""):
            raise ValueError("replay model API key is unavailable")
        self.base_url = endpoint
        self.api_key = api_key
        self.socket_path = socket_path
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.server: _ThreadingUnixHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                if (
                    str(self.headers.get("Authorization") or "")
                    != f"Bearer {broker.token}"
                ):
                    self.send_error(401)
                    return
                path = self.path.split("?", 1)[0]
                if not path.startswith("/upstream/"):
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 32 * 1024 * 1024:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                target = (
                    broker.base_url
                    + "/"
                    + path.removeprefix("/upstream/")
                )
                try:
                    with httpx.stream(
                        "POST",
                        target,
                        headers={
                            "Authorization": f"Bearer {broker.api_key}",
                            "Content-Type": str(
                                self.headers.get("Content-Type")
                                or "application/json"
                            ),
                            "Accept": str(
                                self.headers.get("Accept")
                                or "application/json"
                            ),
                        },
                        content=body,
                        timeout=broker.timeout_seconds,
                        follow_redirects=False,
                    ) as response:
                        self.send_response(response.status_code)
                        self.send_header(
                            "Content-Type",
                            response.headers.get(
                                "content-type",
                                "application/json",
                            ),
                        )
                        self.send_header("Connection", "close")
                        self.end_headers()
                        for chunk in response.iter_bytes():
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except Exception as exc:  # noqa: BLE001
                    payload = json.dumps(
                        {
                            "error": {
                                "message": (
                                    "replay model broker failed: "
                                    f"{type(exc).__name__}"
                                )
                            }
                        }
                    ).encode("utf-8")
                    try:
                        self.send_response(502)
                        self.send_header(
                            "Content-Type",
                            "application/json",
                        )
                        self.send_header(
                            "Content-Length",
                            str(len(payload)),
                        )
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(payload)
                    except OSError:
                        return

            def log_message(
                self,
                _format: str,
                *_args: Any,
            ) -> None:
                return

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.server = _ThreadingUnixHTTPServer(
            str(self.socket_path),
            Handler,
        )
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="teamevolver-replay-model-broker",
            daemon=True,
        )
        self.thread.start()

    @staticmethod
    def worker_base_url(sidecar_port: int) -> str:
        return f"http://127.0.0.1:{sidecar_port}/upstream"

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.socket_path.unlink(missing_ok=True)


class ReplayModelSidecar:
    """Private-network HTTP bridge to the parent Unix-socket broker."""

    def __init__(
        self,
        *,
        socket_path: Path,
        port: int,
        timeout_seconds: int,
    ) -> None:
        self.socket_path = socket_path
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.server: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        sidecar = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 32 * 1024 * 1024:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                transport = httpx.HTTPTransport(
                    uds=str(sidecar.socket_path)
                )
                try:
                    with httpx.Client(
                        transport=transport,
                        timeout=sidecar.timeout_seconds,
                    ).stream(
                        "POST",
                        "http://broker" + self.path,
                        headers={
                            "Authorization": str(
                                self.headers.get("Authorization") or ""
                            ),
                            "Content-Type": str(
                                self.headers.get("Content-Type")
                                or "application/json"
                            ),
                            "Accept": str(
                                self.headers.get("Accept")
                                or "application/json"
                            ),
                        },
                        content=body,
                    ) as response:
                        self.send_response(response.status_code)
                        self.send_header(
                            "Content-Type",
                            response.headers.get(
                                "content-type",
                                "application/json",
                            ),
                        )
                        self.send_header("Connection", "close")
                        self.end_headers()
                        for chunk in response.iter_bytes():
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except Exception as exc:  # noqa: BLE001
                    self.send_error(
                        502,
                        f"replay sidecar failed: {type(exc).__name__}",
                    )

            def log_message(
                self,
                _format: str,
                *_args: Any,
            ) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port),
            Handler,
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="teamevolver-replay-model-sidecar",
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


@contextmanager
def replay_model_broker(
    *,
    base_url: str,
    api_key: str,
    socket_path: Path,
    token: str,
    timeout_seconds: int,
) -> Generator[ReplayModelBroker, None, None]:
    broker = ReplayModelBroker(
        base_url=base_url,
        api_key=api_key,
        socket_path=socket_path,
        token=token,
        timeout_seconds=timeout_seconds,
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.close()


__all__ = [
    "ReplayModelBroker",
    "ReplayModelSidecar",
    "replay_model_broker",
]
