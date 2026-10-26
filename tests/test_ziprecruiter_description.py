import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bs4 import BeautifulSoup

from jobspy.model import Country, DescriptionFormat
from jobspy.ziprecruiter import ZipRecruiter
from jobspy.ziprecruiter.browser import is_cloudflare_challenge

PASSIVE_SCRIPT = (
    "<script>var script = document.createElement('script');"
    "script.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>"
)


class ZipRecruiterDescriptionTests(unittest.TestCase):
    def test_direct_job_uses_full_detail_description_with_passive_cloudflare(self):
        description = (
            "<p>Einleitung der Stellenanzeige.</p>"
            "<h2>Aufgaben</h2><ul><li>Software entwickeln</li></ul>"
            "<p>Bewerbung an jobs@example.com</p>"
        )
        listing = BeautifulSoup(
            '<li><a class="jobList-title" href="/jobs/123-developer">Developer</a>'
            '<div class="jobList-description">Einleitung...</div></li>',
            "html.parser",
        ).li
        for fmt in DescriptionFormat:
            with self.subTest(format=fmt):
                scraper = ZipRecruiter.__new__(ZipRecruiter)
                scraper.seen_urls = set()
                scraper.scraper_input = SimpleNamespace(
                    country=Country.GERMANY, description_format=fmt
                )
                scraper.session = SimpleNamespace(
                    get=Mock(
                        return_value=SimpleNamespace(
                            ok=True,
                            status_code=200,
                            url="https://www.ziprecruiter.de/jobs/123-developer",
                            headers={"server": "cloudflare"},
                            text=(
                                '<script type="application/ld+json">'
                                + json.dumps(
                                    {"@type": "JobPosting", "description": description}
                                )
                                + "</script>"
                                + PASSIVE_SCRIPT
                            ),
                        )
                    )
                )

                job = scraper._process_job(listing)

                self.assertEqual(job.id, "zr-123")
                self.assertIn("Software entwickeln", job.description)
                self.assertIn("jobs@example.com", job.description)
                self.assertNotIn("Einleitung...", job.description)
                self.assertEqual(job.emails, ["jobs@example.com"])
                if fmt == DescriptionFormat.HTML:
                    self.assertEqual(job.description, description)

    def test_job_body_is_used_when_structured_description_is_missing(self):
        scraper = ZipRecruiter.__new__(ZipRecruiter)
        scraper.session = SimpleNamespace(
            get=Mock(
                return_value=SimpleNamespace(
                    ok=True,
                    headers={"server": "cloudflare"},
                    text='<div class="job-body"><h2>Aufgaben</h2><p>Volltext</p></div>'
                    + PASSIVE_SCRIPT,
                )
            )
        )
        description = scraper._get_descr("https://www.ziprecruiter.de/jobs/123")[
            "description"
        ]
        self.assertIn("Volltext", description)
        self.assertIn("<h2>", description)
        self.assertNotIn("<script>", description)

    def test_blocking_cloudflare_challenges_are_still_detected(self):
        for path in (
            "/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1",
            "/cdn-cgi/challenge-platform/orchestrate/chl_page/v1",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    is_cloudflare_challenge(
                        {"server": "cloudflare"}, f'<script src="{path}"></script>'
                    )
                )
        self.assertTrue(
            is_cloudflare_challenge({"cf-mitigated": "challenge"}, PASSIVE_SCRIPT)
        )
        self.assertIs(
            is_cloudflare_challenge({"server": "cloudflare"}, PASSIVE_SCRIPT), False
        )


if __name__ == "__main__":
    unittest.main()
