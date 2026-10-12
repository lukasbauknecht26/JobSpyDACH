from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from pydantic import ValidationError

import jobspy
from jobspy.model import Country, JobPost, JobResponse, Location, ScraperInput, Site
from jobspy.ziprecruiter import ZipRecruiter
from jobspy.ziprecruiter.browser import (
    BrowserPage,
    BrowserRedirectResolver,
    _browser_proxy,
    _is_terminal_url,
    is_cloudflare_challenge,
    is_referral_url,
)
from jobspy.ziprecruiter.manual_browser import ManualResolvedLink

SOURCE = "https://www.ziprecruiter.com/ekn/token"
TARGET = "https://www.stepstone.de/stellenangebote--developer--123-inline.html"


def challenge_response(status=403):
    return SimpleNamespace(
        ok=status == 200,
        status_code=status,
        url=SOURCE,
        headers={"cf-mitigated": "challenge"},
        text="<title>Just a moment...</title>",
    )


class BrowserFallbackIntegrationTests(unittest.TestCase):
    def scraper(self, enabled=False, mode="system"):
        scraper = ZipRecruiter.__new__(ZipRecruiter)
        scraper.scraper_input = ScraperInput(
            site_type=[Site.ZIP_RECRUITER],
            ziprecruiter_browser_fallback=enabled,
            ziprecruiter_browser_mode=mode,
        )
        scraper.proxies = None
        scraper._browser_resolver = None
        scraper.session = SimpleNamespace(get=Mock(return_value=challenge_response()))
        return scraper

    def test_default_never_creates_browser(self):
        scraper = self.scraper()
        scraper._scrape_jobs = Mock(return_value=JobResponse(jobs=[]))
        with (
            patch("jobspy.ziprecruiter.BrowserRedirectResolver") as constructor,
            patch("jobspy.ziprecruiter.ManualBrowserResolver") as manual,
        ):
            scraper.scrape(scraper.scraper_input)
            self.assertIsNone(scraper._get_descr(SOURCE)["resolved_url"])
        constructor.assert_not_called()
        manual.assert_not_called()

    def test_challenge_uses_browser_url_and_html(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        scraper._browser_resolver.resolve.return_value = BrowserPage(
            TARGET,
            '<script type="application/ld+json">{"description":"Browser description"}</script>',
            200,
            {},
        )
        for status in (403, 200):
            with self.subTest(status=status):
                scraper.session.get.return_value = challenge_response(status)
                data = scraper._get_descr(SOURCE)
                self.assertEqual(data["resolved_url"], TARGET)
                self.assertEqual(data["description"], "Browser description")
        self.assertEqual(scraper._browser_resolver.resolve.call_count, 2)

    def test_plain_403_does_not_use_browser(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        scraper.session.get.return_value = SimpleNamespace(
            ok=False, status_code=403, url=SOURCE, headers={}, text="Access denied"
        )
        self.assertIsNone(scraper._get_descr(SOURCE)["resolved_url"])
        scraper._browser_resolver.resolve.assert_not_called()

    def test_direct_detail_success_keeps_metadata_without_browser(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        url = "https://www.ziprecruiter.de/jobs/123-developer"
        scraper.session.get.return_value = SimpleNamespace(
            ok=True,
            url=url,
            headers={},
            text='<script type="application/ld+json">{"description":"Job description"}</script>',
        )
        data = scraper._get_descr(url)
        self.assertEqual(data["resolved_url"], url)
        self.assertEqual(data["description"], "Job description")
        scraper._browser_resolver.resolve.assert_not_called()

    def test_direct_detail_challenge_keeps_url_without_browser(self):
        for status in (200, 403):
            with self.subTest(status=status):
                scraper = self.scraper(enabled=True)
                scraper._browser_resolver = Mock()
                url = "https://www.ziprecruiter.de/jobs/123-developer"
                scraper.session.get.return_value = challenge_response(status)
                scraper.session.get.return_value.url = url
                data = scraper._get_descr(url)
                self.assertEqual(data["resolved_url"], url)
                self.assertIsNone(data["description"])
                scraper._browser_resolver.resolve.assert_not_called()

    def test_referral_already_redirected_to_stepstone_does_not_use_browser(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        scraper.session.get.return_value.url = TARGET
        data = scraper._get_descr(SOURCE)
        self.assertEqual(data["resolved_url"], TARGET)
        self.assertIsNone(data["description"])
        scraper._browser_resolver.resolve.assert_not_called()

    def test_response_referral_url_is_used_instead_of_original_detail_url(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        scraper._browser_resolver.resolve.return_value = ManualResolvedLink(TARGET)
        data = scraper._get_descr("https://www.ziprecruiter.de/jobs/123-developer")
        scraper._browser_resolver.resolve.assert_called_once_with(SOURCE)
        self.assertEqual(data["resolved_url"], TARGET)

    def test_missing_response_url_uses_original_referral_url(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        scraper.session.get.return_value.url = None
        scraper._browser_resolver.resolve.return_value = ManualResolvedLink(TARGET)
        self.assertEqual(scraper._get_descr(SOURCE)["resolved_url"], TARGET)
        scraper._browser_resolver.resolve.assert_called_once_with(SOURCE)

    def test_alertsclk_challenge_still_uses_browser(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        scraper._browser_resolver.resolve.return_value = ManualResolvedLink(TARGET)
        alerts_url = "https://www.alertsclk.com/redirect"
        scraper.session.get.return_value.url = alerts_url
        self.assertEqual(scraper._get_descr(alerts_url)["resolved_url"], TARGET)
        scraper._browser_resolver.resolve.assert_called_once_with(alerts_url)

    def test_resolved_stepstone_target_is_delegated(self):
        scraper = self.scraper(enabled=True)
        scraper.seen_urls = set()
        scraper._browser_resolver = Mock()
        scraper._browser_resolver.resolve.return_value = ManualResolvedLink(TARGET)
        expected = JobPost(
            title="Developer",
            company_name="Example GmbH",
            location=Location(country=Country.GERMANY),
            job_url=TARGET,
            site=Site.STEPSTONE,
        )
        scraper.stepstone = SimpleNamespace(process_job_url=Mock(return_value=expected))
        listing = BeautifulSoup(
            f'<li><a class="jobList-title" href="{SOURCE}">Developer</a></li>',
            "html.parser",
        ).li
        self.assertIs(scraper._process_job(listing), expected)
        self.assertEqual(scraper.stepstone.process_job_url.call_args.args[0], TARGET)

    def test_failed_browser_retains_original_fallback(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        scraper._browser_resolver.resolve.return_value = None
        self.assertIsNone(scraper._get_descr(SOURCE)["resolved_url"])

    def test_scrape_closes_browser_even_on_exception(self):
        scraper = self.scraper(enabled=True, mode="playwright")
        scraper._scrape_jobs = Mock(side_effect=RuntimeError("scrape failed"))
        with patch("jobspy.ziprecruiter.BrowserRedirectResolver") as constructor:
            with self.assertRaisesRegex(RuntimeError, "scrape failed"):
                scraper.scrape(scraper.scraper_input)
        constructor.assert_called_once_with(timeout=180, proxies=None)
        constructor.return_value.close.assert_called_once()
        self.assertIsNone(scraper._browser_resolver)

    def test_system_mode_closes_form_even_on_exception(self):
        scraper = self.scraper(enabled=True)
        scraper._scrape_jobs = Mock(side_effect=RuntimeError("scrape failed"))
        with patch("jobspy.ziprecruiter.ManualBrowserResolver") as constructor:
            with self.assertRaisesRegex(RuntimeError, "scrape failed"):
                scraper.scrape(scraper.scraper_input)
        constructor.assert_called_once_with(timeout=180)
        constructor.return_value.close.assert_called_once()
        self.assertIsNone(scraper._browser_resolver)

    def test_manual_url_does_not_parse_challenge_html(self):
        scraper = self.scraper(enabled=True)
        scraper._browser_resolver = Mock()
        scraper._browser_resolver.resolve.return_value = ManualResolvedLink(TARGET)
        scraper.session.get.return_value.text += (
            '<script type="application/ld+json">{"description":"Blocked page"}</script>'
        )
        data = scraper._get_descr(SOURCE)
        self.assertEqual(data["resolved_url"], TARGET)
        self.assertIsNone(data["description"])

    def test_public_parameters_reach_scraper(self):
        captured = []

        class StubScraper:
            def __init__(self, **kwargs):
                pass

            def scrape(self, scraper_input):
                captured.append(scraper_input)
                return JobResponse(jobs=[])

        with patch.object(jobspy, "ZipRecruiter", StubScraper):
            jobspy.scrape_jobs(
                site_name="zip_recruiter",
                ziprecruiter_browser_fallback=True,
                ziprecruiter_browser_timeout=42,
                ziprecruiter_browser_mode="playwright",
            )
        self.assertTrue(captured[0].ziprecruiter_browser_fallback)
        self.assertEqual(captured[0].ziprecruiter_browser_timeout, 42)
        self.assertEqual(captured[0].ziprecruiter_browser_mode, "playwright")
        self.assertEqual(
            ScraperInput(site_type=[Site.ZIP_RECRUITER]).ziprecruiter_browser_mode,
            "system",
        )
        with self.assertRaises(ValidationError):
            ScraperInput(site_type=[Site.ZIP_RECRUITER], ziprecruiter_browser_timeout=0)
        with self.assertRaises(ValidationError):
            ScraperInput(
                site_type=[Site.ZIP_RECRUITER], ziprecruiter_browser_mode="unknown"
            )


class BrowserResolverTests(unittest.TestCase):
    def test_parallel_calls_and_cleanup_use_one_worker(self):
        resolver = BrowserRedirectResolver()
        threads = []

        def navigate(url):
            threads.append(get_ident())
            return BrowserPage(TARGET, "Job", 200, {})

        try:
            with patch.object(resolver, "_navigate", side_effect=navigate):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(resolver.resolve, [SOURCE] * 8))
            with patch.object(
                resolver,
                "_close_browser",
                side_effect=lambda: threads.append(get_ident()),
            ):
                resolver.close()
            self.assertEqual(len(results), 8)
            self.assertEqual(len(set(threads)), 1)
            self.assertNotEqual(threads[0], get_ident())
            self.assertIsNone(resolver.resolve(SOURCE))
        finally:
            resolver.close()

    def test_timeout_disables_queued_attempts(self):
        resolver = BrowserRedirectResolver()
        try:
            with patch.object(
                resolver, "_navigate", side_effect=TimeoutError
            ) as navigate:
                self.assertIsNone(resolver.resolve(SOURCE))
                self.assertIsNone(resolver.resolve(SOURCE))
                navigate.assert_called_once()
        finally:
            resolver.close()

    def test_waits_for_terminal_page_and_reuses_browser(self):
        resolver = BrowserRedirectResolver(timeout=5)
        clock = [0.0]
        response = SimpleNamespace(url=TARGET, status=200, headers={})
        page = Mock()
        page.url = TARGET
        page.content.return_value = "<h1>Job</h1>"
        page.is_closed.return_value = False

        def navigate(*args, **kwargs):
            page.url = SOURCE
            resolver._response = SimpleNamespace(
                url=SOURCE, status=403, headers={"cf-mitigated": "challenge"}
            )

        def wait(ms):
            clock[0] += ms / 1000
            page.url = TARGET
            resolver._response = response

        page.goto.side_effect = navigate
        page.wait_for_timeout.side_effect = wait

        def launch(deadline):
            resolver._page = page
            resolver._context = Mock()

        try:
            with patch.object(resolver, "_launch", side_effect=launch) as start:
                with patch(
                    "jobspy.ziprecruiter.browser.time.monotonic",
                    side_effect=lambda: clock[0],
                ):
                    self.assertEqual(resolver.resolve(SOURCE).url, TARGET)
                    self.assertEqual(resolver.resolve(SOURCE).url, TARGET)
            start.assert_called_once()
            self.assertGreaterEqual(clock[0], 2)
        finally:
            resolver.close()

    def test_closed_window_disables_fallback(self):
        resolver = BrowserRedirectResolver()
        resolver._context = Mock()
        resolver._page = Mock()
        resolver._page.is_closed.return_value = True
        try:
            self.assertIsNone(resolver.resolve(SOURCE))
            self.assertTrue(resolver._disabled)
            self.assertIsNone(resolver._context)
        finally:
            resolver.close()

    def test_intermediate_page_is_not_returned_when_deadline_expires(self):
        resolver = BrowserRedirectResolver(timeout=1)
        clock = [0.0]
        resolver._context = Mock()
        page = resolver._page = Mock()
        page.url = SOURCE
        page.content.return_value = "Intermediate page"
        page.is_closed.return_value = False
        page.goto.side_effect = lambda *args, **kwargs: setattr(
            resolver, "_response", SimpleNamespace(url=SOURCE, status=200, headers={})
        )
        page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(
            0, clock[0] + ms / 1000
        )
        try:
            with patch(
                "jobspy.ziprecruiter.browser.time.monotonic",
                side_effect=lambda: clock[0],
            ):
                self.assertIsNone(resolver.resolve(SOURCE))
            self.assertTrue(resolver._disabled)
        finally:
            resolver.close()

    def test_missing_chrome_tries_chromium_with_same_profile(self):
        from playwright.sync_api import Error

        with TemporaryDirectory() as profile:
            resolver = BrowserRedirectResolver(profile_dir=profile)
            playwright = Mock()
            playwright.chromium.launch_persistent_context.side_effect = [
                Error("Chromium distribution 'chrome' is not found"),
                Mock(pages=[Mock()]),
            ]
            try:
                with patch("playwright.sync_api.sync_playwright") as start:
                    start.return_value.start.return_value = playwright
                    resolver._launch(10**20)
                calls = playwright.chromium.launch_persistent_context.call_args_list
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0].args, calls[1].args)
                self.assertEqual(calls[0].kwargs["channel"], "chrome")
                self.assertNotIn("channel", calls[1].kwargs)
                self.assertFalse(calls[1].kwargs["headless"])
            finally:
                resolver.close()

    def test_cleanup_stops_playwright_when_context_close_fails(self):
        resolver = BrowserRedirectResolver()
        context, playwright = Mock(), Mock()
        context.close.side_effect = RuntimeError("already closed")
        resolver._context, resolver._playwright = context, playwright
        resolver.close()
        playwright.stop.assert_called_once()
        self.assertIsNone(resolver._context)

    def test_detection_and_intermediates(self):
        self.assertTrue(is_cloudflare_challenge({"CF-Mitigated": "challenge"}, ""))
        self.assertTrue(
            is_cloudflare_challenge({"server": "cloudflare"}, "Just a moment...")
        )
        self.assertFalse(
            is_cloudflare_challenge({"server": "cloudflare"}, "Access denied")
        )
        self.assertFalse(_is_terminal_url(SOURCE))
        self.assertFalse(_is_terminal_url("https://www.alertsclk.com/redirect"))
        self.assertFalse(_is_terminal_url("about:blank"))
        self.assertTrue(_is_terminal_url(TARGET))

    def test_referral_classifier_limits_host_and_path(self):
        for url in (
            SOURCE,
            "https://www.ziprecruiter.de/ekn/token",
            "https://ziprecruiter.com/ekn",
            "https://api.alertsclk.com/redirect",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_referral_url(url))
        for url in (
            TARGET,
            "https://www.ziprecruiter.de/jobs/123",
            "https://www.ziprecruiter.de/jobs/123?next=/ekn/token",
            "https://www.ziprecruiter.com/ekn-extra/token",
            "https://notziprecruiter.com/ekn/token",
            "https://ziprecruiter.com.unrelated.example/ekn/token",
            "https://notalertsclk.com/redirect",
            "file://ziprecruiter.com/ekn/token",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_referral_url(url))

    def test_proxy_credentials_and_fixed_first_proxy(self):
        self.assertEqual(
            _browser_proxy(["http://name:p%40ss@proxy.example:8080", "other:3128"]),
            {
                "server": "http://proxy.example:8080",
                "username": "name",
                "password": "p@ss",
            },
        )
        self.assertIsNone(_browser_proxy("localhost"))
        self.assertEqual(
            _browser_proxy("localhost:8080"), {"server": "http://localhost:8080"}
        )


if __name__ == "__main__":
    unittest.main()
