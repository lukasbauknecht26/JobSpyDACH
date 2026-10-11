from datetime import date, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup

import jobspy
from jobspy.model import Country, DescriptionFormat, JobPost, JobResponse, JobType, Location, Site
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
    def _listing(date_text=None, title="Developer"):
        listing_date = (
            f'<div class="jobList-date text-muted u-textNoWrap">{date_text}</div>'
            if date_text
            else ""
        )
        return BeautifulSoup(
            f"""
            <li class="job-listing">
              <a class="jobList-title" href="/jobs/123-example">{title}</a>
              <ul class="jobList-introMeta"><li>Example GmbH</li><li>Berlin</li></ul>
              <div class="jobList-description">Zip snippet</div>
              {listing_date}
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
        scraper._get_descr = Mock(
            return_value={"resolved_url": target_url, "date_posted": date(2020, 1, 1)}
        )

        job = scraper._process_job(self._listing("2. Okt"))

        self.assertIs(job, expected_job)
        self.assertIs(scraper.stepstone.scraper_input, scraper.scraper_input)
        scraper.stepstone.process_job_url.assert_called_once_with(
            target_url,
            title="Developer",
            company_name="Example GmbH",
            location_text="Berlin",
            date_posted=ZipRecruiter._parse_listing_date("2. Okt"),
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

    def test_title_supplements_detail_employment_type(self):
        cases = (
            ("Pflichtpraktikum Softwareentwicklung", JobType.INTERNSHIP),
            ("Praktikantin im Marketing", JobType.INTERNSHIP),
            ("Werkstudententätigkeit IT", JobType.INTERNSHIP),
            ("Working Student Finance", JobType.INTERNSHIP),
            ("Duales Studium Informatik", JobType.APPRENTICESHIP),
            ("Duales Bachelorstudium BWL", JobType.APPRENTICESHIP),
            ("Dualer Studiengang Informatik", JobType.APPRENTICESHIP),
            ("DHBW Wirtschaftsinformatik", JobType.APPRENTICESHIP),
        )
        for title, expected_type in cases:
            with self.subTest(title=title):
                scraper = self._scraper()
                scraper.stepstone = SimpleNamespace()
                scraper._get_descr = Mock(return_value={"job_type": [JobType.FULL_TIME]})

                job = scraper._process_job(self._listing(title=title))

                self.assertEqual(job.job_type, [JobType.FULL_TIME, expected_type])

    def test_title_type_is_added_without_detail_metadata_or_duplicates(self):
        scraper = self._scraper()
        scraper.stepstone = SimpleNamespace()
        scraper._get_descr = Mock(return_value={"job_type": None})
        job = scraper._process_job(self._listing(title="Werkstudent Data Science"))
        self.assertEqual(job.job_type, [JobType.INTERNSHIP])
        self.assertEqual(
            ZipRecruiter._supplement_job_type_from_title(
                "Praktikum Data Science", [JobType.INTERNSHIP]
            ),
            [JobType.INTERNSHIP],
        )
        self.assertEqual(
            ZipRecruiter._supplement_job_type_from_title(
                "Praktikum im dualen Studium", [JobType.INTERNSHIP]
            ),
            [JobType.INTERNSHIP, JobType.APPRENTICESHIP],
        )

    def test_unspecific_titles_keep_detail_type(self):
        for title in ("Bachelor Professional IT", "Interne Kommunikation", "Ausbildung IT"):
            with self.subTest(title=title):
                self.assertEqual(
                    ZipRecruiter._supplement_job_type_from_title(
                        title, [JobType.FULL_TIME]
                    ),
                    [JobType.FULL_TIME],
                )

    def test_stepstone_redirect_supplements_title_type(self):
        scraper = self._scraper()
        target_url = "https://www.stepstone.de/stellenangebote--student--berlin.html"
        delegated_job = JobPost(
            title="Werkstudent Softwareentwicklung",
            company_name="Example GmbH",
            location=Location(city="Berlin", country=Country.GERMANY),
            job_url=target_url,
            site=Site.STEPSTONE,
            job_type=[JobType.PART_TIME],
        )
        scraper.stepstone = SimpleNamespace(process_job_url=Mock(return_value=delegated_job))
        scraper._get_descr = Mock(return_value={"resolved_url": target_url})

        job = scraper._process_job(
            self._listing(title="Werkstudent Softwareentwicklung")
        )

        self.assertIs(job, delegated_job)
        self.assertEqual(job.job_type, [JobType.PART_TIME, JobType.INTERNSHIP])

    def test_listing_date_takes_precedence_over_detail_date(self):
        scraper = self._scraper()
        scraper.stepstone = SimpleNamespace()
        scraper._get_descr = Mock(return_value={"date_posted": date(2020, 1, 1)})

        job = scraper._process_job(self._listing("Gestern"))

        self.assertEqual(job.date_posted, date.today() - timedelta(days=1))

    def test_detail_date_is_fallback_when_listing_date_is_missing(self):
        scraper = self._scraper()
        detail_date = date(2020, 1, 1)
        scraper.stepstone = SimpleNamespace()
        scraper._get_descr = Mock(return_value={"date_posted": detail_date})

        job = scraper._process_job(self._listing())

        self.assertEqual(job.date_posted, detail_date)

    def test_listing_date_parser_handles_german_formats_and_year_rollover(self):
        today = date.today()
        expected_october = date(today.year, 10, 2)
        if expected_october > today:
            expected_october = date(today.year - 1, 10, 2)

        self.assertEqual(ZipRecruiter._parse_listing_date(" 2. Okt "), expected_october)
        self.assertEqual(
            ZipRecruiter._parse_listing_date("vor 3 Tagen"),
            today - timedelta(days=3),
        )
        self.assertEqual(ZipRecruiter._parse_listing_date("Heute"), today)
        self.assertIsNone(ZipRecruiter._parse_listing_date("unbekannt"))

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
