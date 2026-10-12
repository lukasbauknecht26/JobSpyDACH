"""Inspect each HTTP hop of a ZipRecruiter job link with the scraper session."""

from __future__ import annotations

import argparse
from urllib.parse import urljoin, urlparse

from jobspy.ziprecruiter import ZipRecruiter
from jobspy.ziprecruiter.browser import (
    BrowserRedirectResolver,
    is_cloudflare_challenge,
    is_referral_url,
)
from jobspy.ziprecruiter.manual_browser import ManualBrowserResolver

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
INSPECT_HEADERS = (
    "Content-Type",
    "Server",
    "CF-Ray",
    "Via",
    "X-Cache",
    "Retry-After",
    "WWW-Authenticate",
)


def trace_redirects(session, job_url: str, max_hops: int = 10):
    """Yield request URL and response for every hop, stopping at the first non-redirect."""
    url = job_url
    seen = set()
    for hop in range(max_hops + 1):
        if url in seen:
            raise RuntimeError(f"Redirect loop at {url}")
        seen.add(url)

        response = session.get(url, allow_redirects=False)
        yield url, response

        if response.status_code not in REDIRECT_STATUSES:
            return
        headers = {key.lower(): value for key, value in response.headers.items()}
        location = headers.get("location")
        if not location:
            return
        if hop == max_hops:
            raise RuntimeError(f"Redirect limit ({max_hops}) reached")

        url = urljoin(response.url or url, location)
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError(f"Unsupported redirect target: {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_url", help="The failing /ekn link")
    parser.add_argument("--proxy", help="Use the same proxy as the scraper run")
    parser.add_argument(
        "--user-agent", help="Use the same user agent as the scraper run"
    )
    parser.add_argument("--max-hops", type=int, default=10)
    parser.add_argument(
        "--browser-fallback",
        action="store_true",
        help="Allow visible browser confirmation",
    )
    parser.add_argument("--browser-timeout", type=int, default=180)
    parser.add_argument(
        "--browser-mode", choices=["system", "playwright"], default="system"
    )
    args = parser.parse_args()
    if args.max_hops < 0:
        parser.error("--max-hops must be non-negative")
    if args.browser_timeout <= 0:
        parser.error("--browser-timeout must be positive")
    if urlparse(args.job_url).scheme not in {"http", "https"}:
        parser.error("job_url must be an HTTP(S) URL")

    scraper = ZipRecruiter(proxies=args.proxy, user_agent=args.user_agent)
    try:
        warmup = scraper.session.get(scraper.base_url)
        print(f"Session warm-up: {warmup.status_code} {warmup.url}")
    except Exception as exc:
        print(f"Session warm-up failed: {type(exc).__name__}: {exc}")

    try:
        for index, (requested_url, response) in enumerate(
            trace_redirects(scraper.session, args.job_url, args.max_hops)
        ):
            headers = {key.lower(): value for key, value in response.headers.items()}
            print(f"\nHop {index}: {response.status_code}")
            print(f"  Requested: {requested_url}")
            print(f"  Response:  {response.url}")
            location = headers.get("location")
            if location:
                print(f"  Location:  {location}")
            for header in INSPECT_HEADERS:
                value = headers.get(header.lower())
                if value:
                    print(f"  {header}: {value}")
            if response.status_code == 403:
                excerpt = " ".join((response.text or "").split())[:400]
                print(f"  Body excerpt: {excerpt}")
            if is_cloudflare_challenge(headers, response.text or ""):
                print("  Cause: Cloudflare browser challenge at this hop")
                if args.browser_fallback:
                    response_url = response.url or requested_url
                    if is_referral_url(response_url):
                        if args.browser_mode == "system":
                            resolver = ManualBrowserResolver(
                                timeout=args.browser_timeout
                            )
                        else:
                            resolver = BrowserRedirectResolver(
                                timeout=args.browser_timeout, proxies=args.proxy
                            )
                        try:
                            result = resolver.resolve(response_url)
                            if result is not None:
                                print(f"  Browser target: {result.url}")
                            else:
                                print(
                                    "  Browser resolution failed; original link retained"
                                )
                        finally:
                            resolver.close()
                    else:
                        print("  Direct response URL; no browser resolution required")
    except (RuntimeError, ValueError) as exc:
        print(f"Trace stopped: {exc}")


if __name__ == "__main__":
    main()
