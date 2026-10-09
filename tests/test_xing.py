import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from bs4 import BeautifulSoup
from requests.exceptions import Timeout

from jobspy import scrape_jobs
from jobspy.model import (
    Compensation,
    CompensationInterval,
    Country,
    JobPost,
    JobResponse,
    JobType,
    SalarySource,
    ScraperInput,
    Site,
)
from jobspy.util import desired_order
from jobspy.xing import Xing
from jobspy.xing.constant import REMOTE_FILTER, SEARCH_URL
from jobspy.xing.util import (
    canonical_job_url,
    extract_apollo_state,
    extract_job_posting,
    parse_compensation,
    parse_datetime,
    parse_job_types,
    parse_location,
    resolve,
)

FIXTURES = Path(__file__).parent / "fixtures" / "xing"
SEARCH_HTML = (FIXTURES / "search.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "detail.html").read_text(encoding="utf-8")
JOB_KEY = "VisibleJob:100000001.abc123"
NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def response(html="", status=200):
    return SimpleNamespace(status_code=status, text=html)


def state_html(state, posting=None):
    scripts = '<script>window.crate={"unused":undefined,"APOLLO_STATE":'
    scripts += (
        json.dumps(state).replace("<", "\\u003c") + ',"tail":undefined};</script>'
    )
    if posting is not None:
        scripts += (
            '<script type="application/ld+json">'
            + json.dumps(posting).replace("<", "\\u003c")
            + "</script>"
        )
    return scripts


class XingTests(unittest.TestCase):
    def setUp(self):
        self.search_state = extract_apollo_state(
            BeautifulSoup(SEARCH_HTML, "html.parser")
        )
        self.detail_state = extract_apollo_state(
            BeautifulSoup(DETAIL_HTML, "html.parser")
        )
        self.posting = extract_job_posting(
            BeautifulSoup(DETAIL_HTML, "html.parser"), "100000001"
        )
        self.session = Mock(headers={})
        self.session.get.side_effect = [response(SEARCH_HTML), response(DETAIL_HTML)]
        session_patch = patch("jobspy.xing.create_session", return_value=self.session)
        self.factory = session_patch.start()
        self.addCleanup(session_patch.stop)
        self.scraper = Xing()
        self.scraper.delay = 0
        date_patch = patch("jobspy.xing.datetime", wraps=datetime)
        date_mock = date_patch.start()
        date_mock.now.return_value = NOW
        self.addCleanup(date_patch.stop)

    def scrape(self, **kwargs):
        return self.scraper.scrape(ScraperInput(site_type=[Site.XING], **kwargs)).jobs

    def detail(self, posting=None, **changes):
        state = copy.deepcopy(self.detail_state)
        state[JOB_KEY].update(changes)
        return response(state_html(state, self.posting if posting is None else posting))

    def search_page(self, numbers, total=-1):
        state = copy.deepcopy(self.search_state)
        result = next(iter(state["ROOT_QUERY"].values()))
        result["total"] = total
        result["collection"] = []
        template = state.pop(JOB_KEY)
        for number in numbers:
            item = copy.deepcopy(template)
            identifier = 100000000 + number
            item["id"] = f"{identifier}.abc123"
            item["url"] = f"https://www.xing.com/jobs/job-{identifier}"
            key = f"VisibleJob:{item['id']}"
            state[key] = item
            result["collection"].append({"jobDetail": {"__ref": key}})
        return response(state_html(state))

    def test_constructor_preserves_connection_options(self):
        Xing(proxies="http://proxy:8080", ca_cert="test.pem", user_agent="TestAgent")
        self.factory.assert_called_with(
            proxies="http://proxy:8080",
            ca_cert="test.pem",
            is_tls=False,
            has_retry=True,
        )
        self.assertEqual(self.session.headers["User-Agent"], "TestAgent")

    def test_fixture_mapping_and_estimated_salary(self):
        jobs = self.scrape(country=Country.GERMANY, request_timeout=7)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.id, "xing-100000001")
        self.assertEqual(job.title, "Python Entwickler in M\u00fcnchen")
        self.assertEqual(job.company_name, "Beispiel GmbH")
        self.assertEqual(job.location.country, Country.GERMANY)
        self.assertEqual(job.location.city, "M\u00fcnchen")
        self.assertEqual(job.job_type, [JobType.FULL_TIME])
        self.assertTrue(job.is_remote)
        self.assertEqual(job.job_url_direct, "https://example.org/jobs/python")
        self.assertEqual(job.company_url_direct, "https://example.org")
        self.assertEqual(job.company_industry, "Software")
        self.assertEqual(job.job_level, "Mit Berufserfahrung")
        self.assertEqual(job.emails, ["jobs@example.org"])
        self.assertEqual(job.compensation.salary_source, SalarySource.ESTIMATED_DATA)
        self.assertEqual(job.compensation.interval, CompensationInterval.YEARLY)
        self.assertNotIn("?", job.job_url)
        self.assertTrue(
            all(call.kwargs["timeout"] == 7 for call in self.session.get.call_args_list)
        )

    def test_description_formats(self):
        for format_name, expected in (
            ("markdown", "**Python**"),
            ("html", "<strong>Python</strong>"),
            ("plain", "Entwickle Python"),
        ):
            with self.subTest(format=format_name):
                self.session.get.side_effect = [
                    response(SEARCH_HTML),
                    response(DETAIL_HTML),
                ]
                job = self.scrape(description_format=format_name)[0]
                self.assertIn(expected, job.description)

    def test_description_drops_executable_content(self):
        self.posting["description"] = (
            '<p onclick="bad()">Work</p><script>bad()</script><style>noise</style>'
        )
        self.session.get.side_effect = [response(SEARCH_HTML), self.detail()]
        self.assertEqual(
            self.scrape(description_format="html")[0].description, "<p>Work</p>"
        )

    def test_dach_country_mapping_and_filter(self):
        for code, country in (
            ("DE", Country.GERMANY),
            ("AT", Country.AUSTRIA),
            ("CH", Country.SWITZERLAND),
        ):
            with self.subTest(country=country):
                self.posting["jobLocation"][0]["address"]["addressCountry"] = code
                self.session.get.side_effect = [response(SEARCH_HTML), self.detail()]
                self.assertEqual(
                    self.scrape(country=country)[0].location.country, country
                )
        self.session.get.side_effect = [response(SEARCH_HTML), self.detail()]
        self.assertEqual(self.scrape(country=Country.GERMANY), [])

    def test_search_parameters(self):
        self.scrape(
            search_term="Python",
            location="Z\u00fcrich",
            distance=25,
            is_remote=True,
            job_type=JobType.FULL_TIME,
            hours_old=48,
        )
        params = self.session.get.call_args_list[0].kwargs["params"]
        self.assertEqual(
            params,
            {
                "keywords": "Python",
                "location": "Z\u00fcrich",
                "radius": 25,
                "remoteOption": REMOTE_FILTER,
                "employmentType": "FULL_TIME.ef2fe9",
                "since": "2026-10-06T12:00:00Z",
                "page": 1,
            },
        )
        self.assertNotIn(
            "location", Xing._search_params(ScraperInput(site_type=[Site.XING]), None)
        )
        params = Xing._search_params(
            ScraperInput(site_type=[Site.XING], country=Country.AUSTRIA, distance=50),
            None,
        )
        self.assertEqual(params, {"location": "\u00d6sterreich"})

    def test_local_filters_include_hybrid_and_check_unknowns(self):
        for options, count in (
            (["FULL_REMOTE"], 1),
            (["PARTLY_REMOTE"], 1),
            (["NON_REMOTE"], 0),
            ([], 0),
        ):
            with self.subTest(options=options):
                self.session.get.side_effect = [
                    response(SEARCH_HTML),
                    self.detail(remoteOptions=options),
                ]
                self.assertEqual(len(self.scrape(is_remote=True)), count)
        self.session.get.side_effect = [response(SEARCH_HTML), response(DETAIL_HTML)]
        self.assertEqual(self.scrape(job_type=JobType.PART_TIME), [])
        self.session.get.side_effect = [response(SEARCH_HTML), response(DETAIL_HTML)]
        self.assertEqual(self.scrape(hours_old=23), [])

    def test_easy_apply_true_false_and_unknown(self):
        for application, direct_apply, wanted, count in (
            ({"__typename": "JobXingCustomApplication"}, True, True, 1),
            ({"__typename": "UrlApplication"}, False, False, 1),
            ({"__typename": "UrlApplication"}, False, True, 0),
            ({"__typename": "UnknownApplication"}, None, False, 0),
        ):
            with self.subTest(application=application, wanted=wanted):
                self.posting["directApply"] = direct_apply
                self.session.get.side_effect = [
                    response(SEARCH_HTML),
                    self.detail(application=application),
                ]
                self.assertEqual(len(self.scrape(easy_apply=wanted)), count)

    def test_missing_details_keep_card_without_fabricating_country(self):
        self.session.get.side_effect = [response(SEARCH_HTML), Timeout("test timeout")]
        job = self.scrape()[0]
        self.assertIsNone(job.description)
        self.assertIsNone(job.location.country)
        for filters in ({"country": Country.GERMANY}, {"is_remote": True}):
            self.session.get.side_effect = [
                response(SEARCH_HTML),
                Timeout("test timeout"),
            ]
            self.assertEqual(self.scrape(**filters), [])

    def test_removed_and_expired_ads_are_skipped(self):
        for status in (404, 410):
            self.session.get.side_effect = [
                response(SEARCH_HTML),
                response(status=status),
            ]
            self.assertEqual(self.scrape(), [])
        self.posting["validThrough"] = "2000-01-01"
        self.session.get.side_effect = [response(SEARCH_HTML), self.detail()]
        self.assertEqual(self.scrape(), [])

    def test_search_errors_and_challenges_return_empty_response(self):
        for value in (
            response(status=403),
            response(status=429),
            Timeout("test"),
            response("<h1>Log in</h1>"),
            response('<script>"APOLLO_STATE":{bad}</script>'),
        ):
            with self.subTest(value=value):
                self.session.get.side_effect = [value]
                self.assertEqual(self.scrape(), [])

    def test_detail_block_stops_further_requests_and_keeps_partial_data(self):
        for status in (403, 429):
            self.session.get.reset_mock()
            self.session.get.side_effect = [
                self.search_page([1, 2]),
                response(status=status),
            ]
            self.assertEqual(len(self.scrape()), 1)
            self.assertEqual(self.session.get.call_count, 2)

    def test_offset_spans_pages_and_removed_ads_do_not_consume_limit(self):
        def get(url, **kwargs):
            if url == SEARCH_URL:
                page = kwargs["params"]["page"]
                return self.search_page(range(1, 21) if page == 1 else range(21, 41))
            return response(status=410 if url.endswith("100000020") else 503)

        self.session.get.side_effect = get
        jobs = self.scrape(offset=19, results_wanted=3)
        self.assertEqual(
            [job.id for job in jobs],
            ["xing-100000021", "xing-100000022", "xing-100000023"],
        )
        self.assertEqual(self.session.get.call_count, 6)

    def test_duplicates_and_repeated_page_terminate_pagination(self):
        self.session.get.side_effect = lambda url, **kwargs: (
            self.search_page([1] * 20) if url == SEARCH_URL else response(status=503)
        )
        jobs = self.scrape(results_wanted=30)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.session.get.call_count, 3)

    def test_result_limit_zero_and_known_total(self):
        self.assertEqual(self.scrape(results_wanted=0), [])
        self.session.get.assert_not_called()
        self.session.get.side_effect = lambda url, **kwargs: (
            self.search_page(range(1, 21), total=20)
            if url == SEARCH_URL
            else response(status=503)
        )
        self.assertEqual(len(self.scrape(results_wanted=30)), 20)
        self.assertEqual(self.session.get.call_count, 21)

    def test_page_cap(self):
        self.session.get.side_effect = lambda url, **kwargs: (
            self.search_page(range(1, 21))
            if url == SEARCH_URL
            else response(status=503)
        )
        self.assertEqual(len(self.scrape(offset=1999, results_wanted=2)), 1)
        self.assertEqual(
            self.session.get.call_args_list[0].kwargs["params"]["page"], 100
        )
        self.session.get.reset_mock()
        self.assertEqual(self.scrape(offset=2000), [])
        self.session.get.assert_not_called()

    def test_malformed_job_does_not_discard_following_job(self):
        page = self.search_page([1, 2])
        state = extract_apollo_state(BeautifulSoup(page.text, "html.parser"))
        state[JOB_KEY]["title"] = {"invalid": "title"}
        self.session.get.side_effect = [
            response(state_html(state)),
            response(status=503),
            response(status=503),
        ]
        self.assertEqual([job.id for job in self.scrape()], ["xing-100000002"])

    def test_detail_jsonld_survives_corrupted_apollo(self):
        html = state_html({}, self.posting).replace(
            '"APOLLO_STATE":{}', '"APOLLO_STATE":{bad}'
        )
        self.session.get.side_effect = [response(SEARCH_HTML), response(html)]
        self.assertEqual(
            self.scrape(country=Country.GERMANY)[0].location.city, "M\u00fcnchen"
        )

    def test_missing_detail_data_is_logged_and_keeps_card(self):
        self.session.get.side_effect = [
            response(SEARCH_HTML),
            response("<h1>Log in</h1>"),
        ]
        with self.assertLogs("JobSpy:Xing", level="WARNING") as logs:
            jobs = self.scrape()
        self.assertEqual(len(jobs), 1)
        self.assertIn("No structured Xing detail data", logs.output[0])

    def test_search_failure_keeps_preceding_results(self):
        def get(url, **kwargs):
            if url == SEARCH_URL:
                if kwargs["params"]["page"] == 1:
                    return self.search_page(range(1, 21))
                return response(status=429)
            return response(status=503)

        self.session.get.side_effect = get
        self.assertEqual(len(self.scrape(results_wanted=30)), 20)
        self.assertEqual(self.session.get.call_count, 22)


class XingParserTests(unittest.TestCase):
    def test_reference_cycles_and_missing_references(self):
        self.assertEqual(resolve({"__ref": "missing"}, {}), {})
        self.assertEqual(resolve({"__ref": "a"}, {"a": {"__ref": "a"}}), {})

    def test_url_normalization_and_external_rejection(self):
        self.assertEqual(
            canonical_job_url("https://www.xing.de/jobs/python-123/?x=1#top"),
            "https://www.xing.com/jobs/python-123",
        )
        self.assertIsNone(canonical_job_url("https://example.org/jobs/python-123"))
        self.assertIsNone(canonical_job_url("/jobs/search/ki"))

    def test_job_posting_selection_skips_unrelated_nodes(self):
        soup = BeautifulSoup(
            state_html(
                {},
                [
                    {"@type": "Organization"},
                    {"@type": "JobPosting", "url": "/jobs/job-999"},
                    {
                        "@type": ["JobPosting"],
                        "url": "/jobs/job-123",
                        "title": "Correct",
                    },
                ],
            ),
            "html.parser",
        )
        self.assertEqual(extract_job_posting(soup, "123")["title"], "Correct")

    def test_datetime_timezone_and_invalid_input(self):
        self.assertEqual(parse_datetime("2026-10-08T14:00:00+02:00"), NOW)
        self.assertEqual(parse_datetime("2026-10-08T12:00:00"), NOW)
        self.assertIsNone(parse_datetime("Yesterday"))

    def test_job_types_and_apprenticeship_title_override(self):
        for title, expected in (
            ("Ausbildung Fachinformatiker", JobType.APPRENTICESHIP),
            ("Duales Studium Informatik", JobType.APPRENTICESHIP),
            ("Werkstudent Python", JobType.INTERNSHIP),
            ("Python Developer", JobType.FULL_TIME),
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    parse_job_types({}, {"employmentType": "FULL_TIME"}, {}, title),
                    [expected],
                )
        self.assertIsNone(parse_job_types({}, {}, {}, "Developer"))

    def test_multi_location_prefers_requested_country(self):
        locations = [
            {"address": {"addressLocality": city, "addressCountry": code}}
            for city, code in (("Berlin", "DE"), ("Wien", "AT"))
        ]
        result = parse_location({}, {"jobLocation": locations}, {}, Country.AUSTRIA)
        self.assertEqual(result.city, "Wien")

    def test_salary_sources_and_base_salary_precedence(self):
        salary = {
            "__typename": "SalaryEstimate",
            "minimum": 50000,
            "maximum": 65000,
            "currency": "CHF",
        }
        estimated = parse_compensation({"salary": salary}, {}, {})
        self.assertEqual(estimated.salary_source, SalarySource.ESTIMATED_DATA)
        self.assertEqual(estimated.currency, "CHF")
        posting = {
            "baseSalary": {
                "currency": "EUR",
                "value": {"minValue": 4000, "maxValue": 5000, "unitText": "MONTH"},
            }
        }
        direct = parse_compensation({"salary": salary}, posting, {})
        self.assertEqual(direct.salary_source, SalarySource.DIRECT_DATA)
        self.assertEqual(direct.interval, CompensationInterval.MONTHLY)
        self.assertEqual(direct.min_amount, 4000)
        for kind in ("SalaryRange", "Salary"):
            salary.update(__typename=kind, amount=55000)
            self.assertEqual(
                parse_compensation({"salary": salary}, {}, {}).salary_source,
                SalarySource.DIRECT_DATA,
            )

    def test_invalid_salary_does_not_fabricate_amounts(self):
        for values in (
            {"minimum": "nan"},
            {"minimum": -100},
            {"minimum": 100, "maximum": 50},
            {},
        ):
            self.assertIsNone(
                parse_compensation(
                    {"salary": {"__typename": "SalaryRange", **values}}, {}, {}
                )
            )


class XingIntegrationTests(unittest.TestCase):
    @staticmethod
    def job(compensation=None):
        return JobPost(
            id="xing-123",
            title="Python",
            company_name="Example",
            job_url="https://www.xing.com/jobs/python-123",
            location=None,
            compensation=compensation,
            description="$50,000 - $60,000",
        )

    def test_string_enum_and_default_site_registration(self):
        empty = JobResponse(jobs=[])
        classes = (
            "Xing",
            "Indeed",
            "LinkedIn",
            "ZipRecruiter",
            "Glassdoor",
            "Google",
            "StepStone",
            "Arbeitsagentur",
        )
        mocks = {}
        for name in classes:
            patcher = patch(f"jobspy.{name}")
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
            mocks[name].return_value.scrape.return_value = empty
        for site in ("xing", Site.XING, None):
            scrape_jobs(site_name=site, results_wanted=0, verbose=0)
        self.assertEqual(mocks["Xing"].call_count, 3)
        self.assertEqual(mocks["Indeed"].call_count, 1)

    def test_estimated_salary_reaches_dataframe_and_annual_conversion(self):
        comp = Compensation(
            min_amount=4000,
            max_amount=5000,
            currency="CHF",
            interval=CompensationInterval.MONTHLY,
            salary_source=SalarySource.ESTIMATED_DATA,
        )
        with patch("jobspy.Xing") as scraper:
            scraper.return_value.scrape.return_value = JobResponse(
                jobs=[self.job(comp)]
            )
            frame = scrape_jobs(site_name="xing", enforce_annual_salary=True, verbose=0)
        self.assertEqual(list(frame.columns), desired_order)
        self.assertEqual(frame.iloc[0]["salary_source"], "estimated_data")
        self.assertEqual(frame.iloc[0]["min_amount"], 48000)
        self.assertEqual(frame.iloc[0]["currency"], "CHF")
        self.assertEqual(frame.iloc[0]["site"], "xing")

    def test_existing_direct_salary_and_description_fallback_unchanged(self):
        for comp, expected in (
            (Compensation(min_amount=50000, max_amount=60000), "direct_data"),
            (None, "description"),
        ):
            with self.subTest(comp=comp), patch("jobspy.Indeed") as scraper:
                scraper.return_value.scrape.return_value = JobResponse(
                    jobs=[self.job(comp)]
                )
                frame = scrape_jobs(site_name="indeed", verbose=0)
                self.assertEqual(frame.iloc[0]["salary_source"], expected)

    def test_xing_does_not_use_us_description_salary_fallback(self):
        with patch("jobspy.Xing") as scraper:
            scraper.return_value.scrape.return_value = JobResponse(jobs=[self.job()])
            frame = scrape_jobs(site_name="xing", verbose=0)
        self.assertTrue(pd.isna(frame.iloc[0]["salary_source"]))
        self.assertTrue(pd.isna(frame.iloc[0]["currency"]))

    def test_maximum_only_salary_preserves_source(self):
        comp = Compensation(
            max_amount=60000, currency="EUR", salary_source=SalarySource.ESTIMATED_DATA
        )
        with patch("jobspy.Xing") as scraper:
            scraper.return_value.scrape.return_value = JobResponse(
                jobs=[self.job(comp)]
            )
            frame = scrape_jobs(site_name="xing", verbose=0)
        self.assertEqual(frame.iloc[0]["salary_source"], "estimated_data")
        self.assertEqual(frame.iloc[0]["max_amount"], 60000)
        self.assertTrue(pd.isna(frame.iloc[0]["min_amount"]))


if __name__ == "__main__":
    unittest.main()
