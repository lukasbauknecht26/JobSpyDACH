from types import SimpleNamespace
import unittest

from bs4 import BeautifulSoup

from jobspy.model import Site
from jobspy.stepstone import StepStone


class StepStoneDetailStatusTests(unittest.TestCase):
    def _scraper_with_status(self, status_code: int) -> StepStone:
        scraper = StepStone.__new__(StepStone)
        scraper.site = Site.STEPSTONE
        scraper.scraper_input = SimpleNamespace(request_timeout=30)
        scraper.session = SimpleNamespace(
            get=lambda _url, timeout_seconds: SimpleNamespace(status_code=status_code, text="")
        )
        return scraper

    def _job_card(self):
        return BeautifulSoup(
            '<article><a data-at="job-item-title" href="/jobs--example">Developer</a></article>',
            "html.parser",
        ).article

    def test_stepstone_skips_jobs_with_gone_detail_page(self):
        job = self._scraper_with_status(410)._process_card(
            self._job_card(), "https://www.stepstone.de"
        )

        self.assertIsNone(job)

    def test_stepstone_keeps_jobs_with_other_failed_detail_statuses(self):
        job = self._scraper_with_status(404)._process_card(
            self._job_card(), "https://www.stepstone.de"
        )

        self.assertIsNotNone(job)
        self.assertIsNone(job.description)


if __name__ == "__main__":
    unittest.main()
