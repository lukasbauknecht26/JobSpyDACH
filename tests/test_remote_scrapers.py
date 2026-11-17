import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from requests import HTTPError, Session
from requests.adapters import HTTPAdapter

from jobspy.arbeitsagentur import Arbeitsagentur
from jobspy.indeed import Indeed
from jobspy.linkedin import LinkedIn
from jobspy.model import Country, JobType, ScraperInput, Site
from jobspy.remote import StrictRemoteSearch
from jobspy.stepstone import StepStone
from jobspy.xing import Xing
from jobspy.xing.util import extract_apollo_state

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


def response(html="", *, data=None, status=200, url="https://example.org"):
    result = Mock(
        status_code=status,
        text=html,
        content=html.encode("utf-8"),
        ok=status == 200,
        url=url,
        json=Mock(return_value=data),
    )
    if status >= 400:
        result.raise_for_status.side_effect = HTTPError(str(status))
    return result


def inputs(site, **kwargs):
    return ScraperInput(
        site_type=[site],
        country=Country.GERMANY,
        remote_mode="strict",
        **{"results_wanted": 1, "description_format": "plain", **kwargs},
    )


def schema_html(url, country="DE", *, telecommute=False, applicants=None):
    data = {"@type": "JobPosting", "url": url}
    if country:
        data["jobLocation"] = {"address": {"addressCountry": country}}
    if telecommute:
        data["jobLocationType"] = "TELECOMMUTE"
    if applicants is not None:
        data["applicantLocationRequirements"] = {"@type": "Country", "name": applicants}
    return '<script type="application/ld+json">' + json.dumps(data) + "</script>"


class IndeedStrictTests(unittest.TestCase):
    def raw(self, key, description="<p>Develop software.</p>", **changes):
        data = json.loads((FIXTURES / "remote" / "indeed_job.json").read_text())
        data.update(key=key, description={"html": description}, **changes)
        return data

    def run_search(self, pages, **kwargs):
        session = Mock(headers={})
        session.post.side_effect = [
            response(
                data={
                    "data": {
                        "jobSearch": {
                            "results": [{"job": item} for item in jobs],
                            "pageInfo": {"nextCursor": cursor},
                        }
                    }
                }
            )
            for jobs, cursor in pages
        ]
        with patch("jobspy.indeed.create_session", return_value=session):
            scraper = Indeed()
        with patch("jobspy.remote.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            result = scraper.scrape(inputs(Site.INDEED, **kwargs))
        return result.jobs, session

    def test_remote_age_job_type_and_easy_apply_are_combined(self):
        recent = int(NOW.timestamp() * 1000)
        jobs, session = self.run_search(
            [
                ([self.raw("old", dateOnIndeed=0)], "next"),
                (
                    [
                        self.raw(
                            "hybrid",
                            "<p>Hybrid work: two days in the office.</p>",
                            dateOnIndeed=recent,
                        )
                    ],
                    "last",
                ),
                ([self.raw("good", dateOnIndeed=recent)], None),
            ],
            hours_old=48,
            job_type=JobType.FULL_TIME,
            easy_apply=True,
        )
        self.assertEqual([j.id for j in jobs], ["in-good"])
        self.assertEqual(session.post.call_count, 3)
        query = session.post.call_args_list[0].kwargs["json"]["query"]
        self.assertNotIn('keys: ["DSQF7"]', query)
        self.assertNotIn('keys: ["CF3CP"]', query)
        self.assertIn('field: "indeedApplyScope"', query)
        self.assertNotIn('field: "dateOnIndeed"', query)
        self.assertNotIn("radius:", query)
        self.assertNotIn("where:", query)
        self.assertEqual(
            session.post.call_args_list[0].kwargs["headers"]["indeed-co"], "DE"
        )

    def test_full_text_and_remote_metadata_are_required_independently(self):
        jobs, _ = self.run_search(
            [
                ([self.raw("no-description", "")], "next"),
                (
                    [
                        self.raw(
                            "not-proven",
                            attributes=[{"label": "Full-time", "key": "CF3CP"}],
                        )
                    ],
                    "last",
                ),
                ([self.raw("good")], None),
            ]
        )
        self.assertEqual([j.id for j in jobs], ["in-good"])

    def test_remote_and_job_type_native_filters_without_easy_apply(self):
        jobs, session = self.run_search(
            [([self.raw("good")], None)],
            job_type=JobType.FULL_TIME,
        )
        self.assertEqual(len(jobs), 1)
        query = session.post.call_args.kwargs["json"]["query"]
        self.assertIn('keys: ["DSQF7"]', query)
        self.assertIn('keys: ["CF3CP"]', query)
        self.assertNotIn('field: "indeedApplyScope"', query)

    def test_easy_apply_still_enforces_remote_and_job_type_locally(self):
        jobs, _ = self.run_search(
            [
                (
                    [
                        self.raw(
                            "parttime",
                            attributes=[
                                {"label": "Remote", "key": "DSQF7"},
                                {"label": "Part-time", "key": "75GKK"},
                            ],
                        ),
                        self.raw(
                            "not-remote",
                            attributes=[{"label": "Full-time", "key": "CF3CP"}],
                        ),
                        self.raw("good"),
                    ],
                    None,
                )
            ],
            job_type=JobType.FULL_TIME,
            easy_apply=True,
        )
        self.assertEqual([j.id for j in jobs], ["in-good"])

    def test_offset_is_applied_to_accepted_results(self):
        jobs, _ = self.run_search(
            [
                (
                    [
                        self.raw("bad", "<p>Work in our office is mandatory.</p>"),
                        self.raw("skip"),
                        self.raw("keep"),
                    ],
                    None,
                ),
            ],
            offset=1,
        )
        self.assertEqual([j.id for j in jobs], ["in-keep"])

    def test_repeated_cursor_stops_even_when_all_jobs_are_rejected(self):
        jobs, session = self.run_search(
            [
                ([self.raw("one", "<p>Hybrid work.</p>")], "same"),
                ([self.raw("two", "<p>Hybrid work.</p>")], "same"),
            ]
        )
        self.assertEqual(jobs, [])
        self.assertEqual(session.post.call_count, 2)

    def test_malformed_result_does_not_discard_other_jobs_on_the_page(self):
        jobs, _ = self.run_search(
            [
                (
                    [
                        self.raw("one"),
                        ["invalid job"],
                        {"key": ["invalid key"]},
                        self.raw("two"),
                    ],
                    None,
                )
            ],
            results_wanted=2,
        )
        self.assertEqual([j.id for j in jobs], ["in-one", "in-two"])

    def test_candidate_budget_and_http_errors_return_partial_results(self):
        with patch("jobspy.remote.MAX_REMOTE_CANDIDATES", 2):
            jobs, session = self.run_search(
                [
                    (
                        [
                            self.raw("one"),
                            self.raw("two", "<p>Hybrid work.</p>"),
                            self.raw("three"),
                        ],
                        "next",
                    ),
                ],
                results_wanted=10,
            )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(session.post.call_count, 1)
        session = Mock(headers={})
        session.post.return_value = response(status=429)
        with patch("jobspy.indeed.create_session", return_value=session):
            self.assertEqual(Indeed().scrape(inputs(Site.INDEED)).jobs, [])
        self.assertEqual(session.post.call_count, 1)


class ArbeitsagenturStrictTests(unittest.TestCase):
    def run_search(self, pages, descriptions, **kwargs):
        session = Mock(headers={})
        searches, details = [], []

        def get(url, **options):
            if "params" in options:
                searches.append(dict(options["params"]))
                items = pages[options["params"]["page"] - 1]
                return response(data={"ergebnisliste": items})
            key = url.rsplit("/", 1)[-1]
            details.append(key)
            value = descriptions[key]
            if isinstance(value, int):
                return response(status=value)
            return response(value)

        session.get.side_effect = get
        with (
            patch("jobspy.arbeitsagentur.create_session", return_value=session),
            patch("jobspy.arbeitsagentur.time.sleep"),
        ):
            result = Arbeitsagentur().scrape(inputs(Site.ARBEITSAGENTUR, **kwargs))
        return result.jobs, searches, details

    @staticmethod
    def item(key, country="DEUTSCHLAND", **changes):
        return {
            "referenznummer": key,
            "stellenangebotsTitel": "Developer",
            "firma": "Example",
            "stellenlokationen": [{"adresse": {"land": country}}],
            "homeofficemoeglich": True,
            "homeofficetyp": "ANGABE_IN_PROZENT",
            "homeofficeprozent": 100,
            **changes,
        }

    @staticmethod
    def description(text):
        return '<div id="detail-beschreibung-text-container"><p>' + text + "</p></div>"

    def test_native_percentage_filter_and_rejected_pages(self):
        jobs, searches, _ = self.run_search(
            [[self.item("bad")], [self.item("good", homeofficeprozent=98)]],
            {
                "bad": self.description("Bis zu 100% Homeoffice."),
                "good": self.description("98% remote."),
            },
        )
        self.assertEqual([j.id for j in jobs], ["aa-good"])
        self.assertEqual(jobs[0].remote_min_percent, 98)
        self.assertEqual([p["page"] for p in searches], [1, 2])
        self.assertEqual(searches[0]["homeoffice"], "prozentual_95")
        self.assertNotIn("wo", searches[0])
        self.assertNotIn("umkreis", searches[0])

    def test_external_and_synthetic_descriptions_do_not_qualify(self):
        external = '<div id="detail-beschreibung-extern"><a id="detail-beschreibung-externe-url-btn" href="https://example.org/job">Full description</a></div>'
        jobs, _, _ = self.run_search(
            [[self.item("synthetic"), self.item("partial"), self.item("good")]],
            {
                "synthetic": external,
                "partial": external + self.description("100% remote."),
                "good": self.description("100% remote."),
            },
        )
        self.assertEqual([j.id for j in jobs], ["aa-good"])

    def test_country_and_offset(self):
        jobs, _, _ = self.run_search(
            [
                [
                    self.item("foreign", country="USA"),
                    self.item("skip"),
                    self.item("keep"),
                ]
            ],
            {
                key: self.description("100% remote.")
                for key in ["foreign", "skip", "keep"]
            },
            offset=1,
        )
        self.assertEqual([j.id for j in jobs], ["aa-keep"])

    def test_repeated_pages_and_blocked_details_stop(self):
        page = [self.item("bad")]
        jobs, searches, details = self.run_search(
            [page, page], {"bad": self.description("Homeoffice möglich.")}
        )
        self.assertEqual(jobs, [])
        self.assertEqual(len(searches), 2)
        self.assertEqual(details, ["bad"])
        jobs, _, details = self.run_search(
            [[self.item("blocked"), self.item("later")]], {"blocked": 403}
        )
        self.assertEqual(jobs, [])
        self.assertEqual(details, ["blocked"])

    def test_unknown_country_is_not_filled_from_search_context(self):
        jobs, _, _ = self.run_search(
            [[self.item("unknown", stellenlokationen=[])], []],
            {"unknown": self.description("100% remote.")},
        )
        self.assertEqual(jobs, [])


class StepStoneStrictTests(unittest.TestCase):
    @staticmethod
    def url(key):
        return f"https://www.stepstone.de/stellenangebote--Developer--{100000000 + key}-inline.html"

    def run_search(self, pages, descriptions, **kwargs):
        session = Mock(headers={})
        searches, details = [], []

        def get(url, **options):
            if "params" in options:
                searches.append(dict(options["params"]))
                keys = pages[options["params"]["page"] - 1]
                return response(
                    "".join(
                        '<article data-at="job-item"><a data-at="job-item-title" href="'
                        + self.url(key)
                        + '">Developer</a><span data-at="job-item-location">Berlin</span></article>'
                        for key in keys
                    )
                )
            key = int(url.split("--")[-1].split("-")[0]) - 100000000
            details.append(key)
            value = descriptions[key]
            return (
                response(status=value)
                if isinstance(value, int)
                else response(value, url=url)
            )

        session.get.side_effect = get
        with (
            patch("jobspy.stepstone.create_session", return_value=session),
            patch("jobspy.stepstone.time.sleep"),
        ):
            result = StepStone().scrape(inputs(Site.STEPSTONE, **kwargs))
        return result.jobs, searches, details

    def detail(self, key, text, country="DE", **schema):
        return (
            schema_html(self.url(key), country, **schema)
            + '<div data-at="job-ad-content"><p>'
            + text
            + "</p></div>"
        )

    def test_actual_remote_facet_and_continue_after_rejected_page(self):
        jobs, searches, _ = self.run_search(
            [[1], [2]],
            {1: self.detail(1, "Hybrid work."), 2: self.detail(2, "100% remote.")},
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].remote_min_percent, 100)
        self.assertEqual([p["page"] for p in searches], [1, 2])
        self.assertEqual(searches[0]["wp"], "611350")
        self.assertNotIn("radius", searches[0])

    def test_filter_membership_and_german_domain_are_not_evidence(self):
        jobs, _, _ = self.run_search(
            [[1, 2, 3]],
            {
                1: self.detail(1, "Homeoffice möglich."),
                2: self.detail(2, "100% remote.", country=None),
                3: self.detail(3, "100% remote.", country="US", applicants="Germany"),
            },
        )
        self.assertEqual([j.job_url for j in jobs], [self.url(3)])

    def test_missing_full_description_and_offset(self):
        jobs, _, _ = self.run_search(
            [[1, 2, 3]],
            {
                1: schema_html(self.url(1), telecommute=True)
                + "<div>100% remote preview</div>",
                2: self.detail(2, "100% remote."),
                3: self.detail(3, "100% remote."),
            },
            offset=1,
        )
        self.assertEqual([j.job_url for j in jobs], [self.url(3)])

    def test_repeated_rejected_page_does_not_loop(self):
        jobs, searches, details = self.run_search(
            [[1], [1]], {1: self.detail(1, "Hybrid work.")}
        )
        self.assertEqual(jobs, [])
        self.assertEqual(len(searches), 2)
        self.assertEqual(details, [1])

    def test_blocked_detail_stops_further_requests(self):
        jobs, _, details = self.run_search([[1, 2]], {1: 429})
        self.assertEqual(jobs, [])
        self.assertEqual(details, [1])


class LinkedInStrictTests(unittest.TestCase):
    def test_default_budget_stops_long_run_and_logs_actual_progress(self):
        pages = [list(range(page * 10 + 1, page * 10 + 11)) for page in range(32)]
        descriptions = {
            key: self.detail(key, "Hybrid work.") for page in pages for key in page
        }
        with self.assertLogs("JobSpy:LinkedIn", level="INFO") as logs:
            jobs, searches, details = self.run_search(
                pages, descriptions, results_wanted=20
            )
        self.assertEqual(jobs, [])
        self.assertEqual(len(searches), 10)
        self.assertEqual(len(details), 100)
        self.assertTrue(
            any("candidates 90/100; accepted 0/20" in entry for entry in logs.output)
        )
        self.assertTrue(
            any("candidate limit reached" in entry for entry in logs.output)
        )
        self.assertFalse(any("/ 2" in entry for entry in logs.output))

    def test_candidate_budget_keeps_verified_offset_results(self):
        jobs, _, details = self.run_search(
            [[1, 2, 3, 4]],
            {
                1: self.detail(1, "100% remote."),
                2: self.detail(2, "Hybrid work."),
                3: self.detail(3, "100% remote."),
                4: self.detail(4, "100% remote."),
            },
            remote_max_candidates=3,
            results_wanted=20,
            offset=1,
        )
        self.assertEqual([j.id for j in jobs], ["li-100000003"])
        self.assertEqual(details, [1, 2, 3])

    def test_time_budget_stops_between_details_without_discarding_results(self):
        original_details = LinkedIn._get_job_details
        with patch("jobspy.remote.monotonic", return_value=100) as clock:

            def slow_details(scraper, job_id):
                result = original_details(scraper, job_id)
                clock.return_value += 5
                return result

            with patch.object(LinkedIn, "_get_job_details", slow_details):
                jobs, searches, details = self.run_search(
                    [[1, 2, 3]],
                    {key: self.detail(key, "100% remote.") for key in [1, 2, 3]},
                    results_wanted=20,
                    remote_max_seconds=10,
                )
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(searches), 1)
        self.assertEqual(details, [1, 2])

    def test_expired_budget_after_search_does_not_fetch_details(self):
        original_get = LinkedIn._get
        with patch("jobspy.remote.monotonic", return_value=100) as clock:

            def slow_search(scraper, url, **kwargs):
                result = original_get(scraper, url, **kwargs)
                if "params" in kwargs:
                    clock.return_value += 10
                return result

            with patch.object(LinkedIn, "_get", slow_search):
                jobs, searches, details = self.run_search(
                    [[1]], {}, remote_max_seconds=10
                )
        self.assertEqual(jobs, [])
        self.assertEqual(len(searches), 1)
        self.assertEqual(details, [])

    def test_strict_requests_cap_timeouts_and_restore_retry_policy(self):
        session = Session()
        adapter = HTTPAdapter(max_retries=3)
        session.mount("https://", adapter)
        retries = adapter.max_retries
        with patch("jobspy.linkedin.create_session", return_value=session):
            scraper = LinkedIn()
        with patch("jobspy.remote.monotonic", return_value=100) as clock:
            scraper._strict = StrictRemoteSearch(
                inputs(Site.LINKEDIN, remote_max_seconds=10), Mock()
            )
            clock.return_value = 108

            def check_request(*args, **kwargs):
                self.assertEqual(adapter.max_retries, 0)
                self.assertEqual(kwargs["timeout"], 2)
                raise HTTPError("unavailable")

            with patch.object(session, "get", side_effect=check_request) as get:
                with self.assertRaises(HTTPError):
                    scraper._get(self.url(1), timeout=5)
                self.assertIs(adapter.max_retries, retries)
                clock.return_value = 110
                with self.assertRaises(TimeoutError):
                    scraper._get(self.url(2), timeout=5)
                self.assertEqual(get.call_count, 1)
            scraper._strict = None
            with patch.object(session, "get", return_value=response()) as get:
                scraper._get(self.url(1), timeout=5)
                self.assertEqual(get.call_args.kwargs["timeout"], 5)
                self.assertIs(adapter.max_retries, retries)
        session.close()

    @staticmethod
    def url(key):
        return f"https://www.linkedin.com/jobs/view/{100000000 + key}"

    def run_search(self, pages, descriptions, **kwargs):
        session = Mock(headers={})
        searches, details = [], []

        def get(url, **options):
            if "params" in options:
                searches.append(dict(options["params"]))
                keys = pages[len(searches) - 1]
                return response(
                    "".join(
                        '<div class="base-search-card"><a class="base-card__full-link" href="'
                        + self.url(key)
                        + '"></a><span class="sr-only">Developer</span><div class="base-search-card__metadata"><span class="job-search-card__location">Berlin, Germany</span></div></div>'
                        for key in keys
                    )
                )
            key = int(url.rsplit("/", 1)[-1]) - 100000000
            details.append(key)
            value = descriptions[key]
            return (
                response(status=value, url=url)
                if isinstance(value, int)
                else response(value, url=url)
            )

        session.get.side_effect = get
        with (
            patch("jobspy.linkedin.create_session", return_value=session),
            patch("jobspy.linkedin.time.sleep"),
        ):
            result = LinkedIn().scrape(inputs(Site.LINKEDIN, **kwargs))
        return result.jobs, searches, details

    def detail(self, key, text, country="DE", **schema):
        return (
            schema_html(self.url(key), country, **schema)
            + '<div class="show-more-less-html__markup"><p>'
            + text
            + "</p></div>"
        )

    def test_native_filter_and_required_description(self):
        jobs, searches, details = self.run_search(
            [[1], [2]],
            {1: self.detail(1, "Hybrid work."), 2: self.detail(2, "Fully remote.")},
            linkedin_fetch_description=False,
        )
        self.assertEqual([j.id for j in jobs], ["li-100000002"])
        self.assertEqual(details, [1, 2])
        self.assertEqual(searches[0]["location"], "Germany")
        self.assertEqual(searches[0]["f_WT"], 2)
        self.assertNotIn("distance", searches[0])

    def test_per_job_telecommute_and_applicant_requirements(self):
        jobs, _, _ = self.run_search(
            [[1, 2]],
            {
                1: self.detail(
                    1,
                    "Develop software.",
                    telecommute=True,
                    country="US",
                    applicants="US",
                ),
                2: self.detail(
                    2,
                    "Develop software.",
                    telecommute=True,
                    country="US",
                    applicants="Germany",
                ),
            },
        )
        self.assertEqual([j.id for j in jobs], ["li-100000002"])

    def test_verified_offset_and_missing_details(self):
        jobs, _, details = self.run_search(
            [[1, 2, 3]],
            {
                1: 503,
                2: self.detail(2, "100% remote."),
                3: self.detail(3, "100% remote."),
            },
            offset=1,
        )
        self.assertEqual([j.id for j in jobs], ["li-100000003"])
        self.assertEqual(details, [1, 2, 3])

    def test_repeated_page_and_blocked_detail_stop(self):
        jobs, searches, details = self.run_search(
            [[1], [1]], {1: self.detail(1, "Hybrid work.")}
        )
        self.assertEqual(jobs, [])
        self.assertEqual(len(searches), 2)
        self.assertEqual(details, [1])
        jobs, _, details = self.run_search([[1, 2]], {1: 403})
        self.assertEqual(jobs, [])
        self.assertEqual(details, [1])


class XingStrictTests(unittest.TestCase):
    def run_search(self, pages, descriptions, **kwargs):
        search_template = extract_apollo_state(
            BeautifulSoup(
                (FIXTURES / "xing" / "search.html").read_text(encoding="utf-8"),
                "html.parser",
            )
        )
        session = Mock(headers={})
        searches, details = [], []

        def get(url, **options):
            if "params" in options:
                searches.append(dict(options["params"]))
                numbers = pages[options["params"]["page"] - 1]
                state = copy.deepcopy(search_template)
                template = state.pop("VisibleJob:100000001.abc123")
                result = next(iter(state["ROOT_QUERY"].values()))
                result["collection"] = []
                result["total"] = 100
                for number in numbers:
                    candidate = copy.deepcopy(template)
                    candidate["id"] = f"{100000000 + number}.abc123"
                    candidate["url"] = (
                        f"https://www.xing.com/jobs/developer-{100000000 + number}"
                    )
                    key = "VisibleJob:" + candidate["id"]
                    state[key] = candidate
                    result["collection"].append({"jobDetail": {"__ref": key}})
                return response(
                    '<script>window.crate={"APOLLO_STATE":'
                    + json.dumps(state)
                    + "};</script>"
                )
            number = int(url.rsplit("-", 1)[-1]) - 100000000
            details.append(number)
            value = descriptions[number]
            if isinstance(value, int):
                return response(status=value)
            text, options, country, applicants = value
            posting = {
                "@type": "JobPosting",
                "url": url,
                "title": "Developer",
                "description": text,
                "jobLocation": {"address": {"addressCountry": country}},
            }
            if applicants:
                posting["applicantLocationRequirements"] = {
                    "@type": "Country",
                    "name": applicants,
                }
            state = {
                f"VisibleJob:{100000000 + number}.abc123": {"remoteOptions": options}
            }
            html = (
                '<script type="application/ld+json">'
                + json.dumps(posting)
                + "</script>"
            )
            html += (
                '<script>window.crate={"APOLLO_STATE":'
                + json.dumps(state)
                + "};</script>"
            )
            return response(html)

        session.get.side_effect = get
        with (
            patch("jobspy.xing.create_session", return_value=session),
            patch("jobspy.xing.time.sleep"),
        ):
            result = Xing().scrape(inputs(Site.XING, **kwargs))
        return result.jobs, searches, details

    @staticmethod
    def detail(text="Develop software.", options=None, country="DE", applicants=None):
        return (
            text,
            options if options is not None else ["FULL_REMOTE"],
            country,
            applicants,
        )

    def test_full_remote_only_and_country_restrictions(self):
        jobs, searches, _ = self.run_search(
            [[1, 2, 3]],
            {
                1: self.detail(options=["PARTLY_REMOTE"]),
                2: self.detail(country="US", applicants="US"),
                3: self.detail(country="US", applicants="Germany"),
            },
        )
        self.assertEqual([j.id for j in jobs], ["xing-100000003"])
        self.assertEqual(searches[0]["remoteOption"], "FULL_REMOTE.050e26")
        self.assertNotIn("radius", searches[0])

    def test_rejected_full_page_is_followed_by_next_page(self):
        rejected = list(range(1, 21))
        data = {key: self.detail("Hybrid work.") for key in rejected}
        data[21] = self.detail()
        jobs, searches, _ = self.run_search([rejected, [21]], data)
        self.assertEqual([j.id for j in jobs], ["xing-100000021"])
        self.assertEqual([p["page"] for p in searches], [1, 2])

    def test_mixed_options_and_verified_offset(self):
        jobs, _, _ = self.run_search(
            [[1, 2, 3]],
            {
                1: self.detail("Hybrid work."),
                2: self.detail(),
                3: self.detail(options=["FULL_REMOTE", "PARTLY_REMOTE"]),
            },
            offset=1,
        )
        self.assertEqual([j.id for j in jobs], ["xing-100000003"])

    def test_failed_and_blocked_details_never_qualify(self):
        jobs, _, details = self.run_search([[1, 2, 3]], {1: 503, 2: 403})
        self.assertEqual(jobs, [])
        self.assertEqual(details, [1, 2])

    def test_repeated_page_and_budget_stop(self):
        page = list(range(1, 21))
        data = {key: self.detail("Hybrid work.") for key in page}
        jobs, searches, details = self.run_search([page, page], data)
        self.assertEqual(jobs, [])
        self.assertEqual(len(searches), 2)
        self.assertEqual(len(details), 20)
        with patch("jobspy.remote.MAX_REMOTE_CANDIDATES", 2):
            jobs, searches, details = self.run_search([page], data)
        self.assertEqual(jobs, [])
        self.assertEqual(len(details), 2)


class RemoteOutputFormatTests(unittest.TestCase):
    def test_evidence_is_checked_before_output_formatting_for_every_source(self):
        # A plain converter joins these paragraphs. That must not turn the
        # presence cap into an ambiguous "up to 100% remote" statement.
        text = "<p>100% remote in Germany</p><p>Business travel: up to 5%</p>"
        for description_format in ("html", "markdown", "plain"):
            kwargs = {"description_format": description_format}
            indeed = IndeedStrictTests()
            agency = ArbeitsagenturStrictTests()
            stepstone = StepStoneStrictTests()
            linkedin = LinkedInStrictTests()
            xing = XingStrictTests()
            searches = {
                "indeed": lambda: indeed.run_search(
                    [([indeed.raw("format", text)], None)], **kwargs
                ),
                "arbeitsagentur": lambda: agency.run_search(
                    [[agency.item("format")]],
                    {"format": agency.description(text)},
                    **kwargs,
                ),
                "stepstone": lambda: stepstone.run_search(
                    [[1]], {1: stepstone.detail(1, text)}, **kwargs
                ),
                "linkedin": lambda: linkedin.run_search(
                    [[1]], {1: linkedin.detail(1, text)}, **kwargs
                ),
                "xing": lambda: xing.run_search(
                    [[1]], {1: xing.detail(text)}, **kwargs
                ),
            }
            for source, search in searches.items():
                with self.subTest(source=source, description_format=description_format):
                    jobs, *_ = search()
                    self.assertEqual(len(jobs), 1)
                    self.assertEqual(jobs[0].remote_min_percent, 95)
                    self.assertIn("<p>", jobs[0].remote_metadata.description_text)


if __name__ == "__main__":
    unittest.main()
