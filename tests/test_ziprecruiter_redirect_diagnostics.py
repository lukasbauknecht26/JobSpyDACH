import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.diagnose_ziprecruiter_redirect import main, trace_redirects


class RedirectDiagnosticTests(unittest.TestCase):
    def test_follows_relative_location_until_first_403(self):
        session = SimpleNamespace(
            get=Mock(
                side_effect=[
                    SimpleNamespace(
                        status_code=302,
                        url="https://www.ziprecruiter.com/ekn/link",
                        headers={"location": "/interstitial"},
                    ),
                    SimpleNamespace(
                        status_code=403,
                        url="https://www.ziprecruiter.com/interstitial",
                        headers={},
                    ),
                ]
            )
        )

        hops = list(trace_redirects(session, "https://www.ziprecruiter.com/ekn/link"))

        self.assertEqual([response.status_code for _, response in hops], [302, 403])
        self.assertEqual(
            session.get.call_args_list[1].args[0],
            "https://www.ziprecruiter.com/interstitial",
        )
        self.assertTrue(
            all(
                call.kwargs == {"allow_redirects": False}
                for call in session.get.call_args_list
            )
        )

    def test_stops_on_redirect_loop(self):
        session = SimpleNamespace(
            get=Mock(
                return_value=SimpleNamespace(
                    status_code=302,
                    url="https://www.ziprecruiter.com/ekn/link",
                    headers={"Location": "/ekn/link"},
                )
            )
        )

        with self.assertRaisesRegex(RuntimeError, "Redirect loop"):
            list(trace_redirects(session, "https://www.ziprecruiter.com/ekn/link"))
        self.assertEqual(session.get.call_count, 1)


class DiagnosticReferralFilterTests(unittest.TestCase):
    source = "https://www.ziprecruiter.com/ekn/token"
    detail = "https://www.ziprecruiter.de/jobs/123"
    target = "https://www.stepstone.de/jobs--example.html"

    def run_diagnostic(self, source, responses):
        warmup = SimpleNamespace(status_code=200, url="https://www.ziprecruiter.de")
        with (
            patch("scripts.diagnose_ziprecruiter_redirect.ZipRecruiter") as scraper,
            patch(
                "scripts.diagnose_ziprecruiter_redirect.ManualBrowserResolver"
            ) as resolver,
            patch(
                "scripts.diagnose_ziprecruiter_redirect.BrowserRedirectResolver"
            ) as playwright,
            patch("sys.argv", ["diagnose", source, "--browser-fallback"]),
            redirect_stdout(StringIO()),
        ):
            scraper.return_value.session.get.side_effect = [warmup] + responses
            resolver.return_value.resolve.return_value = None
            main()
        return resolver, playwright

    @staticmethod
    def response(url, status=403, location=None):
        headers = {"cf-mitigated": "challenge"}
        if location:
            headers = {"Location": location}
        return SimpleNamespace(status_code=status, url=url, headers=headers, text="")

    def test_direct_detail_challenge_does_not_create_resolver(self):
        resolver, playwright = self.run_diagnostic(
            self.detail, [self.response(self.detail)]
        )
        resolver.assert_not_called()
        playwright.assert_not_called()

    def test_already_reached_destination_does_not_create_resolver(self):
        resolver, playwright = self.run_diagnostic(
            self.source,
            [self.response(self.source, 302, self.target), self.response(self.target)],
        )
        resolver.assert_not_called()
        playwright.assert_not_called()

    def test_challenged_referral_uses_current_hop(self):
        resolver, playwright = self.run_diagnostic(
            self.detail,
            [self.response(self.detail, 302, self.source), self.response(self.source)],
        )
        resolver.return_value.resolve.assert_called_once_with(self.source)
        resolver.return_value.close.assert_called_once()
        playwright.assert_not_called()

    def test_challenge_with_http_200_still_uses_referral_filter(self):
        for url, expected in ((self.source, True), (self.detail, False)):
            with self.subTest(url=url):
                resolver, _ = self.run_diagnostic(url, [self.response(url, 200)])
                self.assertEqual(resolver.called, expected)


if __name__ == "__main__":
    unittest.main()
