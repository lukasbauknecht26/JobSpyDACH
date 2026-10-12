from __future__ import annotations

import json
import math
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlsplit, urlunsplit

from jobspy.util import create_logger
from jobspy.ziprecruiter.browser import _is_terminal_url, is_referral_url

log = create_logger("ZipRecruiter")


@dataclass(frozen=True)
class ManualResolvedLink:
    url: str


@dataclass
class _PendingLink:
    id: str
    source_url: str
    deadline: float
    event: Event = field(default_factory=Event)
    result: ManualResolvedLink | None = None
    outcome: str = "active"


def _valid_target(url: str, source_url: str) -> bool:
    if not url or any(char.isspace() or ord(char) < 32 for char in url):
        return False
    try:
        parts = urlsplit(url)
        parts.port
        if parts.username is not None or not _is_terminal_url(url):
            return False
        source = urlsplit(source_url)
        canonical = lambda value: urlunsplit(
            (value.scheme.lower(), value.netloc.lower(), value.path, value.query, "")
        )
        return canonical(parts) != canonical(source)
    except ValueError:
        return False


class ManualBrowserResolver:
    """Serialize manual URL handoffs without controlling the user's browser."""

    def __init__(self, timeout: int = 180):
        self.timeout = timeout
        self._resolve_lock = Lock()
        self._state_lock = Lock()
        self._closed = False
        self._disabled = False
        self._pending = None
        self._phase = "waiting"
        self._server = None
        self._server_thread = None
        self._base_path = f"/{secrets.token_urlsafe(32)}/"
        self.form_url = None

    def resolve(self, job_url: str) -> ManualResolvedLink | None:
        if not is_referral_url(job_url):
            return None
        with self._resolve_lock:
            with self._state_lock:
                if self._closed or self._disabled:
                    return None
                pending = _PendingLink(
                    secrets.token_urlsafe(16), job_url, time.monotonic() + self.timeout
                )
                self._pending = pending
                self._phase = "active"
            try:
                self._start_server()
                with self._state_lock:
                    if self._closed or self._disabled:
                        return None
                if not webbrowser.open_new_tab(job_url):
                    raise RuntimeError("System browser could not open the job link")
                log.info(
                    "ZipRecruiter link opened in the system browser at host %s. "
                    "Submit its destination in the local form (up to %s seconds).",
                    urlsplit(job_url).hostname,
                    self.timeout,
                )
                pending.event.wait(max(0, pending.deadline - time.monotonic()))
                with self._state_lock:
                    if pending.outcome == "active":
                        pending.outcome = self._phase = "expired"
                        self._disabled = True
                        log.warning(
                            "ZipRecruiter manual URL handoff timed out; "
                            "system browser fallback disabled for this run"
                        )
                    return pending.result
            except Exception as exc:
                with self._state_lock:
                    self._disabled = True
                    self._phase = "failed"
                    pending.outcome = "failed"
                    pending.event.set()
                log.warning(
                    "ZipRecruiter system browser fallback unavailable (%s); "
                    "original link retained",
                    type(exc).__name__,
                )
                return None
            finally:
                with self._state_lock:
                    if self._pending is pending:
                        self._pending = None

    def _start_server(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Manual handoff closed")
            if self._server is not None:
                return
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
            self._server.daemon_threads = True
            self.form_url = (
                f"http://127.0.0.1:{self._server.server_port}{self._base_path}"
            )
            self._server_thread = Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="ziprecruiter-url-form",
                daemon=True,
            )
            self._server_thread.start()
        if not webbrowser.open_new_tab(self.form_url):
            raise RuntimeError("System browser could not open the URL form")

    def _status(self) -> dict:
        with self._state_lock:
            pending = self._pending
            active = pending is not None and pending.outcome == "active"
            return {
                "phase": self._phase,
                "job_id": pending.id if active else None,
                "source_url": pending.source_url if active else None,
                "remaining_seconds": (
                    max(0, math.ceil(pending.deadline - time.monotonic()))
                    if active
                    else 0
                ),
            }

    def _action(self, data: dict) -> tuple[int, dict]:
        with self._state_lock:
            pending = self._pending
            if (
                self._closed
                or self._disabled
                or pending is None
                or pending.event.is_set()
                or data.get("job_id") != pending.id
                or time.monotonic() >= pending.deadline
            ):
                return 409, {"error": "Dieser Vorgang ist nicht mehr aktiv."}
            action = data.get("action")
            if action == "accept":
                url = data.get("url")
                url = url.strip() if isinstance(url, str) else ""
                if not _valid_target(url, pending.source_url):
                    return 400, {
                        "error": "Bitte eine HTTP(S)-Ziel-URL eingeben, "
                        "keinen Quell-Link oder Weiterleitungs-Link."
                    }
                pending.result = ManualResolvedLink(url)
                pending.outcome = self._phase = "submitted"
            elif action == "skip":
                pending.outcome = self._phase = "skipped"
            elif action == "abort":
                pending.outcome = self._phase = "aborted"
                self._disabled = True
            else:
                return 400, {"error": "Unbekannte Aktion."}
            pending.event.set()
            return 200, {"phase": self._phase}

    def _handler(self):
        resolver = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass

            def _respond(self, status, body, content_type="application/json"):
                if isinstance(body, dict):
                    body = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
                    "form-action 'self'",
                )
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                origin = f"http://127.0.0.1:{self.server.server_port}"
                if (
                    self.headers.get("Host") != origin.removeprefix("http://")
                    or self.headers.get("Origin", origin) != origin
                ):
                    self._respond(403, {"error": "Local session required"})
                    return False
                return True

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path == resolver._base_path:
                    html = Path(__file__).with_name("manual_browser.html").read_bytes()
                    self._respond(200, html, "text/html")
                elif self.path == resolver._base_path + "status":
                    self._respond(200, resolver._status())
                else:
                    self._respond(404, {"error": "Session not found"})

            def do_POST(self):
                if not self._authorized():
                    return
                if self.path != resolver._base_path + "action":
                    self._respond(404, {"error": "Session not found"})
                    return
                if (
                    self.headers.get("Content-Type", "").split(";", 1)[0]
                    != "application/json"
                ):
                    self._respond(415, {"error": "JSON required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        raise ValueError("Invalid body size")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("Object required")
                except (ValueError, UnicodeError):
                    self._respond(400, {"error": "Invalid request"})
                    return
                self._respond(*resolver._action(data))

        return Handler

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = self._disabled = True
            self._phase = "closed"
            if self._pending is not None:
                self._pending.outcome = "aborted"
                self._pending.event.set()
            server, thread = self._server, self._server_thread
            self._server = self._server_thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join()
