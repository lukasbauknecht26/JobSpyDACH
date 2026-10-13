import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests.exceptions import Timeout

from jobspy.arbeitsagentur import Arbeitsagentur
from jobspy.model import DescriptionFormat


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


if __name__ == "__main__":
    unittest.main()
