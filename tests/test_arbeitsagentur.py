import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests.exceptions import Timeout

from jobspy.arbeitsagentur import Arbeitsagentur
from jobspy.model import DescriptionFormat, ScraperInput, Site


EXTERNAL_URL = (
    "https://www.jobexport.de/job/5259280.html?exp=81"
    "&cid=c1404a44-ba79-4a54-8fbb-546492bfba0b"
)
EXTERNAL_HTML = """
<div id="detail-beschreibung-extern" class="externe-Beschreibung">
  <h3>Vollstaendige Stellenbeschreibung bei unserem Kooperationspartner einsehen:</h3>
  <a id="detail-beschreibung-externe-url-btn"
     href="https://www.jobexport.de/job/5259280.html?exp=81&amp;cid=c1404a44-ba79-4a54-8fbb-546492bfba0b">
    Externe Seite oeffnen
  </a>
  <p><a id="externe-url-quelle" href="https://www.jobexport.de">jobexport.de</a></p>
</div>
"""


class ArbeitsagenturTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(headers={})
        session_patch = patch(
            "jobspy.arbeitsagentur.create_session", return_value=self.session
        )
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.scraper = Arbeitsagentur()
        self.scraper.scraper_input = SimpleNamespace(
            description_format=DescriptionFormat.PLAIN, request_timeout=12
        )

    def process(self, html="", status=200, **fields):
        self.session.get.return_value = SimpleNamespace(
            status_code=status, content=html.encode("utf-8")
        )
        return self.scraper._process_job_item(
            {
                "referenznummer": "13635-c1404a44_JB5259280-S",
                "hauptberuf": "Java Developer",
                **fields,
            }
        )

    def test_external_description_without_local_text(self):
        job = self.process(EXTERNAL_HTML)
        self.assertEqual(job.job_url_direct, EXTERNAL_URL)
        self.assertEqual(job.description, "**Beruf:** Java Developer")
        self.session.get.assert_called_once_with(job.job_url, timeout=12)

    def test_external_description_link_takes_precedence_over_api_url(self):
        job = self.process(EXTERNAL_HTML, externeUrl="https://example.org/apply")
        self.assertEqual(job.job_url_direct, EXTERNAL_URL)

    def test_local_description_and_api_url_are_preserved(self):
        job = self.process(
            '<div id="detail-beschreibung-text-container"><p>Full description</p></div>',
            externeUrl="https://example.org/apply",
        )
        self.assertEqual(job.description, "Full description")
        self.assertEqual(job.job_url_direct, "https://example.org/apply")

    def test_external_link_and_partial_local_description(self):
        job = self.process(
            EXTERNAL_HTML
            + '<div id="detail-beschreibung-text-container"><p>Partial text</p></div>'
        )
        self.assertEqual(job.job_url_direct, EXTERNAL_URL)
        self.assertEqual(job.description, "Partial text")

    def test_source_homepage_is_not_used_as_job_url(self):
        job = self.process(
            '<div id="detail-beschreibung-extern">'
            '<a id="externe-url-quelle" href="https://www.jobexport.de">Source</a>'
            '</div>'
        )
        self.assertIsNone(job.job_url_direct)

    def test_container_link_without_button_id(self):
        job = self.process(
            EXTERNAL_HTML.replace('id="detail-beschreibung-externe-url-btn"', "")
        )
        self.assertEqual(job.job_url_direct, EXTERNAL_URL)

    def test_invalid_or_empty_external_link_keeps_api_url(self):
        for href in ("", "   ", "javascript:alert(1)"):
            with self.subTest(href=href):
                job = self.process(
                    f'<a id="detail-beschreibung-externe-url-btn" href="{href}">Open</a>',
                    externeUrl="https://example.org/apply",
                )
                self.assertEqual(job.job_url_direct, "https://example.org/apply")

    def test_failed_detail_request_keeps_job_and_api_url(self):
        job = self.process(status=503, externeUrl="https://example.org/apply")
        self.assertEqual(job.job_url_direct, "https://example.org/apply")
        self.assertEqual(job.description, "**Beruf:** Java Developer")
        self.session.get.side_effect = Timeout("detail request timed out")
        job = self.process(externeUrl="https://example.org/apply")
        self.assertEqual(job.job_url_direct, "https://example.org/apply")
        self.assertEqual(job.description, "**Beruf:** Java Developer")


class ArbeitsagenturAgeTests(unittest.TestCase):
    def run_search(self, pages, hours_old=96, results_wanted=10):
        session = Mock(headers={})
        requested_params = []

        def get_page(url, **kwargs):
            requested_params.append(dict(kwargs["params"]))
            items = pages[kwargs["params"]["page"] - 1]
            return Mock(status_code=200, json=Mock(return_value={"ergebnisliste": items}))

        session.get.side_effect = get_page
        with patch("jobspy.arbeitsagentur.create_session", return_value=session):
            scraper = Arbeitsagentur()
        with (
            patch("jobspy.arbeitsagentur.datetime") as clock,
            patch("jobspy.arbeitsagentur.time.sleep"),
            patch.object(scraper, "_fetch_job_details", return_value=(None, None)) as details,
        ):
            clock.now.return_value = datetime(2026, 10, 27, 12)
            response = scraper.scrape(ScraperInput(
                site_type=[Site.ARBEITSAGENTUR], hours_old=hours_old,
                results_wanted=results_wanted,
            ))
        return response.jobs, requested_params, details.call_count

    @staticmethod
    def item(refnr, posted=None, **fields):
        return {"referenznummer": refnr, "datumErsteVeroeffentlichung": posted, **fields}

    def test_old_and_unknown_dates_are_skipped_before_detail_requests(self):
        jobs, params, details = self.run_search([[
            self.item("old", "2025-01-22", aenderungsdatum="2026-10-27T10:00:00"),
            self.item("outside", "2026-10-22"),
            self.item("missing"), self.item("invalid", "invalid"),
            self.item("boundary", "2026-10-23"),
            self.item("new", "2026-10-27"),
            self.item("fallback", veroeffentlichungszeitraum={"von": "2026-10-26"}),
        ], []])
        self.assertEqual([job.id for job in jobs], ["aa-boundary", "aa-new", "aa-fallback"])
        self.assertEqual(details, 3)
        self.assertEqual(params[0]["veroeffentlichtseit"], 4)
        self.assertNotIn("veroeffentlichtab", params[0])

    def test_filtered_page_does_not_stop_pagination(self):
        jobs, params, details = self.run_search([
            [self.item("old", "2025-01-22")],
            [self.item("new", "2026-10-27")],
        ], results_wanted=1)
        self.assertEqual([job.id for job in jobs], ["aa-new"])
        self.assertEqual([p["page"] for p in params], [1, 2])
        self.assertEqual(details, 1)

    def test_repeated_filtered_page_stops_pagination(self):
        old = [self.item("old", "2025-01-22")]
        jobs, params, details = self.run_search([old, old])
        self.assertEqual(jobs, [])
        self.assertEqual(len(params), 2)
        self.assertEqual(details, 0)

    def test_no_filter_keeps_old_and_undated_jobs(self):
        jobs, params, details = self.run_search([
            [self.item("old", "2025-01-22"), self.item("missing")], []
        ], hours_old=None)
        self.assertEqual(len(jobs), 2)
        self.assertNotIn("veroeffentlichtseit", params[0])

    def test_partial_days_zero_and_large_windows(self):
        for hours, api_days in [(25, 2), (0, 0), (2401, None)]:
            with self.subTest(hours=hours):
                jobs, params, _ = self.run_search([[
                    self.item("today", "2026-10-27"),
                    self.item("yesterday", "2026-10-26"),
                ], []], hours_old=hours)
                self.assertEqual(params[0].get("veroeffentlichtseit"), api_days)
                self.assertEqual(len(jobs), 1 if hours == 0 else 2)


if __name__ == "__main__":
    unittest.main()
