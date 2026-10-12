from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import unquote, urlsplit

from jobspy.util import create_logger

log = create_logger("ZipRecruiter")


def is_cloudflare_challenge(headers: dict, html: str) -> bool:
    headers = {key.lower(): value for key, value in headers.items()}
    return headers.get("cf-mitigated") == "challenge" or (
        "cloudflare" in str(headers.get("server", "")).lower()
        and (
            "just a moment" in html[:2000].lower()
            or "/cdn-cgi/challenge-platform/" in html
        )
    )


def is_referral_url(url: str) -> bool:
    """Recognize the ZipRecruiter/Alertsclk links that still need resolution."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        return False
    if host == "alertsclk.com" or host.endswith(".alertsclk.com"):
        return True
    is_ziprecruiter = host in {"ziprecruiter.com", "ziprecruiter.de"} or host.endswith(
        (".ziprecruiter.com", ".ziprecruiter.de")
    )
    return is_ziprecruiter and parts.path.split("/")[1:2] == ["ekn"]


def _is_terminal_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme in {"http", "https"}
        and bool(parts.hostname)
        and not is_referral_url(url)
    )


def _browser_proxy(proxies):
    proxy = proxies[0] if isinstance(proxies, list) and proxies else proxies
    if not proxy:
        return None
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if parts.hostname == "localhost" and parts.port is None:
        return None
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    server = f"{parts.scheme}://{host}"
    if parts.port:
        server += f":{parts.port}"
    result = {"server": server}
    if parts.username is not None:
        result["username"] = unquote(parts.username)
    if parts.password is not None:
        result["password"] = unquote(parts.password)
    return result


@dataclass
class BrowserPage:
    url: str
    text: str
    status_code: int
    headers: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class BrowserRedirectResolver:
    """Own Playwright on one worker; allow the user to confirm a challenge there."""

    def __init__(self, timeout: int = 180, proxies=None, profile_dir=None):
        self.timeout = timeout
        self.proxy = _browser_proxy(proxies)
        self.profile_dir = Path(
            profile_dir or ".cache/jobspy/ziprecruiter-browser"
        ).resolve()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ziprecruiter-browser"
        )
        self._lock = Lock()
        self._closed = False
        self._disabled = False
        self._playwright = None
        self._context = None
        self._page = None
        self._response = None

    def resolve(self, job_url: str) -> BrowserPage | None:
        with self._lock:
            if self._closed:
                return None
            future = self._executor.submit(self._resolve, job_url)
        return future.result()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            future = self._executor.submit(self._close_browser)
        try:
            future.result()
        finally:
            self._executor.shutdown(wait=True)

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Browser resolution timed out")
        return max(1, int(remaining * 1000))

    def _launch(self, deadline: float) -> None:
        from playwright.sync_api import Error, sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        options = {"headless": False, "locale": "de-DE"}
        if self.proxy:
            options["proxy"] = self.proxy
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="chrome",
                timeout=min(30000, self._remaining_ms(deadline)),
                **options,
            )
        except Error as exc:
            message = str(exc).lower()
            if "not found" not in message and "executable doesn't exist" not in message:
                raise
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                timeout=min(30000, self._remaining_ms(deadline)),
                **options,
            )
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        self._page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        if (
            response.request.is_navigation_request()
            and response.frame == self._page.main_frame
        ):
            self._response = response

    def _resolve(self, job_url: str) -> BrowserPage | None:
        if self._disabled:
            return None
        try:
            return self._navigate(job_url)
        except Exception as exc:
            self._disabled = True
            message = str(exc).lower()
            if "not found" in message or "executable doesn't exist" in message:
                reason = (
                    "Chrome/Chromium missing; install Chrome or Playwright Chromium"
                )
            elif "timeout" in type(exc).__name__.lower():
                reason = "browser resolution timed out"
            else:
                reason = "browser could not start or was closed"
            log.warning(
                "ZipRecruiter browser fallback disabled for this run: %s (%s). "
                "Original link retained.",
                reason,
                type(exc).__name__,
            )
            self._close_browser()
            return None

    def _navigate(self, job_url: str) -> BrowserPage:
        from playwright.sync_api import Error

        deadline = time.monotonic() + self.timeout
        if self._context is None:
            self._launch(deadline)
        log.info(
            "ZipRecruiter browser opened for host %s. Confirm the challenge if shown "
            "(up to %s seconds); close the window to stop the fallback.",
            urlsplit(job_url).hostname,
            self.timeout,
        )
        self._response = None
        self._page.goto(
            job_url,
            wait_until="domcontentloaded",
            timeout=self._remaining_ms(deadline),
        )
        stable_url = None
        stable_since = 0.0
        while time.monotonic() < deadline:
            if self._page.is_closed():
                raise RuntimeError("Browser window closed")
            try:
                html = self._page.content()
            except Error as exc:
                message = str(exc).lower()
                if "navigat" not in message and "execution context" not in message:
                    raise
                self._page.wait_for_timeout(min(250, self._remaining_ms(deadline)))
                continue
            url = self._page.url
            response = self._response
            if (
                response is not None
                and url.split("#", 1)[0] == response.url.split("#", 1)[0]
                and 200 <= response.status < 300
                and _is_terminal_url(url)
                and not is_cloudflare_challenge(response.headers, html)
            ):
                if stable_url != url:
                    stable_url, stable_since = url, time.monotonic()
                elif time.monotonic() - stable_since >= 1:
                    return BrowserPage(url, html, response.status, response.headers)
            else:
                stable_url = None
            self._page.wait_for_timeout(min(250, self._remaining_ms(deadline)))
        raise TimeoutError("Browser resolution timed out")

    def _close_browser(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            log.debug("ZipRecruiter browser context already closed")
        finally:
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            except Exception:
                log.debug("ZipRecruiter Playwright already stopped")
            finally:
                self._context = self._playwright = self._page = self._response = None
