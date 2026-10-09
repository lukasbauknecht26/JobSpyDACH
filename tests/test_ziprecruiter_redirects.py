from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup

import jobspy
from jobspy.model import Country, DescriptionFormat, JobPost, JobResponse, Location, Site
from jobspy.ziprecruiter import ZipRecruiter


class ZipRecruiterRedirectTests(unittest.TestCase):
    def _scraper(self) -> ZipRecruiter:
        scraper = ZipRecruiter.__new__(ZipRecruiter)
        scraper.scraper_input = SimpleNamespace(
            country=Country.GERMANY,
            description_format=DescriptionFormat.MARKDOWN,
        )
        scraper.seen_urls = set()
        return scraper

    @staticmethod
    def _listing():
        return BeautifulSoup(
            """
            <li class="job-listing">
              <a class="jobList-title" href="/jobs/123-example">Developer</a>
              <ul class="jobList-introMeta"><li>Example GmbH</li><li>Berlin</li></ul>
              <div class="jobList-description">Zip snippet</div>
            </li>
            """,
            "html.parser",
        ).li

    def test_stepstone_redirect_uses_stepstone_parser_and_site(self):
        scraper = self._scraper()
        target_url = "https://www.stepstone.de/stellenangebote--developer--berlin.html"
        expected_job = JobPost(
            id="stepstone-123",
            title="Developer",
            company_name="Example GmbH",
            location=Location(city="Berlin", country=Country.GERMANY),
            job_url=target_url,
            site=Site.STEPSTONE,
        )
        scraper.stepstone = SimpleNamespace(
            scraper_input=None,
            process_job_url=Mock(return_value=expected_job),
        )
        scraper._get_descr = Mock(return_value={"resolved_url": target_url, "date_posted": None})

        job = scraper._process_job(self._listing())

        self.assertIs(job, expected_job)
        self.assertIs(scraper.stepstone.scraper_input, scraper.scraper_input)
        scraper.stepstone.process_job_url.assert_called_once_with(
            target_url,
            title="Developer",
            company_name="Example GmbH",
            location_text="Berlin",
            date_posted=None,
            country=Country.GERMANY,
        )

    def test_other_redirect_replaces_alertsclk_url(self):
        scraper = self._scraper()
        target_url = "https://careers.example.com/jobs/123?source=ziprecruiter"
        scraper.stepstone = SimpleNamespace()
        scraper._get_descr = Mock(
            return_value={
                "resolved_url": target_url,
                "description": None,
                "date_posted": None,
                "job_type": None,
                "company": None,
                "city": None,
                "state": None,
                "country": None,
                "job_url_direct": None,
            }
        )

        job = scraper._process_job(self._listing())

        self.assertEqual(job.job_url, target_url)
        self.assertNotIn("alertsclk.com", job.job_url)

    def test_detail_response_records_final_redirect_url(self):
        scraper = self._scraper()
        scraper.session = SimpleNamespace(
            get=Mock(
                return_value=SimpleNamespace(
                    ok=True,
                    url="https://careers.example.com/jobs/123",
                    text="<html></html>",
                )
            )
        )

        data = scraper._get_descr("https://www.alertsclk.com/redirect")

        self.assertEqual(data["resolved_url"], "https://careers.example.com/jobs/123")
        scraper.session.get.assert_called_once_with(
            "https://www.alertsclk.com/redirect", allow_redirects=True
        )

    def test_alertsclk_page_uses_package_data_target_url(self):
        scraper = self._scraper()
        alertsclk_url = "https://www.alertsclk.com/ekn/encoded-link?tsid=123"
        target_url = "https://careers.example.com/jobs/123"
        scraper.session = SimpleNamespace(
            get=Mock(
                side_effect=[
                    SimpleNamespace(
                        ok=True,
                        url=alertsclk_url,
                        text='<script>window.clickId = "click-123";</script>',
                    ),
                    SimpleNamespace(
                        ok=True,
                        status_code=200,
                        json=lambda: {"url": target_url},
                    ),
                ]
            )
        )

        data = scraper._get_descr(alertsclk_url)

        self.assertEqual(data["resolved_url"], target_url)
        self.assertEqual(scraper.session.get.call_count, 2)
        self.assertEqual(
            scraper.session.get.call_args_list[1].args[0],
            "https://www.alertsclk.com/kn-api/get-package-data",
        )
        self.assertEqual(
            scraper.session.get.call_args_list[1].kwargs["params"],
            {
                "original_url": alertsclk_url,
                "partner_referer_host": "",
                "click_id": "click-123",
            },
        )

    def test_alertsclk_without_click_id_keeps_original_url(self):
        scraper = self._scraper()
        alertsclk_url = "https://www.alertsclk.com/ekn/encoded-link"
        scraper.session = SimpleNamespace(
            get=Mock(
                return_value=SimpleNamespace(
                    ok=True,
                    url=alertsclk_url,
                    text="<html></html>",
                )
            )
        )

        data = scraper._get_descr(alertsclk_url)

        self.assertEqual(data["resolved_url"], alertsclk_url)
        scraper.session.get.assert_called_once_with(alertsclk_url, allow_redirects=True)

    def test_alertsclk_package_data_failure_keeps_original_url(self):
        scraper = self._scraper()
        alertsclk_url = "https://www.alertsclk.com/ekn/encoded-link"
        scraper.session = SimpleNamespace(
            get=Mock(
                side_effect=[
                    SimpleNamespace(
                        ok=True,
                        url=alertsclk_url,
                        text='<script>window.clickId = "click-123";</script>',
                    ),
                    SimpleNamespace(ok=False, status_code=503),
                ]
            )
        )

        data = scraper._get_descr(alertsclk_url)

        self.assertEqual(data["resolved_url"], alertsclk_url)

    def test_only_real_stepstone_domains_use_stepstone_parser(self):
        self.assertTrue(ZipRecruiter._is_stepstone_url("https://www.stepstone.de/jobs--example"))
        self.assertTrue(ZipRecruiter._is_stepstone_url("https://jobs.stepstone.at/jobs--example"))
        self.assertFalse(ZipRecruiter._is_stepstone_url("https://notstepstone.de/jobs--example"))

    def test_only_real_alertsclk_domains_use_client_side_resolution(self):
        self.assertTrue(ZipRecruiter._is_alertsclk_url("https://www.alertsclk.com/ekn/example"))
        self.assertTrue(ZipRecruiter._is_alertsclk_url("https://api.alertsclk.com/example"))
        self.assertFalse(ZipRecruiter._is_alertsclk_url("https://notalertsclk.com/example"))


class CanonicalJobUrlTests(unittest.TestCase):
    def test_stepstone_job_wins_when_canonical_urls_match(self):
        stepstone_url = "https://www.stepstone.de/jobs--developer--berlin.html"
        zip_url = f"{stepstone_url}?source=ziprecruiter"

        class ZipScraper:
            def __init__(self, **_kwargs):
                pass

            def scrape(self, _scraper_input):
                return JobResponse(
                    jobs=[
                        JobPost(
                            id="zip-1",
                            title="Developer",
                            company_name="Example GmbH",
                            location=Location(city="Berlin", country=Country.GERMANY),
                            job_url=zip_url,
                            site=Site.ZIP_RECRUITER,
                        )
                    ]
                )

        class StepStoneScraper:
            def __init__(self, **_kwargs):
                pass

            def scrape(self, _scraper_input):
                return JobResponse(
                    jobs=[
                        JobPost(
                            id="stepstone-1",
                            title="Developer",
                            company_name="Example GmbH",
                            location=Location(city="Berlin", country=Country.GERMANY),
                            job_url=stepstone_url,
                            site=Site.STEPSTONE,
                        )
                    ]
                )

        original_zip, original_stepstone = jobspy.ZipRecruiter, jobspy.StepStone
        jobspy.ZipRecruiter, jobspy.StepStone = ZipScraper, StepStoneScraper
        try:
            jobs = jobspy.scrape_jobs(
                site_name=[Site.ZIP_RECRUITER, Site.STEPSTONE],
                results_wanted=1,
                country_indeed="germany",
            )
        finally:
            jobspy.ZipRecruiter, jobspy.StepStone = original_zip, original_stepstone

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs.iloc[0]["site"], Site.STEPSTONE.value)
        self.assertEqual(jobs.iloc[0]["job_url"], stepstone_url)


if __name__ == "__main__":
    unittest.main()
