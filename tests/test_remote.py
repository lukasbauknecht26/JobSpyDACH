import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from jobspy import scrape_jobs
from jobspy.model import (
    Country,
    JobPost,
    JobResponse,
    Location,
    RemoteLocation,
    RemoteMetadata,
    ScraperInput,
    Site,
)
from jobspy.remote import (
    STRICT_REMOTE_COLUMNS,
    StrictRemoteSearch,
    assess_remote_job,
    extract_job_posting,
    indeed_date_is_recent,
    metadata_from_job_posting,
)
from jobspy.util import desired_order


def job(description="100% remote in Germany.", **metadata):
    return JobPost(
        id="example-1",
        title="Software Developer",
        company_name="Example",
        job_url="https://example.org/jobs/123456",
        location=Location(country="DE"),
        description=description,
        remote_metadata=RemoteMetadata(
            **{"description_complete": True, "job_countries": ["DE"], **metadata}
        ),
    )


class RemotePolicyTests(unittest.TestCase):
    def test_policy_examples(self):
        fixtures = Path(__file__).parent / "fixtures" / "remote" / "policies.json"
        for case in json.loads(fixtures.read_text(encoding="utf-8")):
            with self.subTest(case=case["name"]):
                result = assess_remote_job(
                    job(
                        case["text"],
                        full_remote=case.get("native_full", False),
                        job_countries=case.get("countries", ["DE"]),
                        description_complete=case.get("complete", True),
                    )
                )
                self.assertEqual(result.min_percent, case["percent"], result)

    def test_upper_bound_metadata_needs_a_positive_statement(self):
        self.assertFalse(
            assess_remote_job(
                job(
                    "Homeoffice möglich.",
                    advertised_percent=100,
                )
            ).accepted
        )
        self.assertEqual(
            assess_remote_job(
                job(
                    "98% remote.",
                    advertised_percent=98,
                )
            ).min_percent,
            98,
        )
        self.assertFalse(
            assess_remote_job(
                job(
                    "100% remote.",
                    advertised_percent=80,
                )
            ).accepted
        )

    def test_native_hybrid_option_does_not_cancel_a_full_remote_choice(self):
        self.assertTrue(
            assess_remote_job(
                job(
                    "Choose where to work.",
                    full_remote=True,
                    hybrid=True,
                )
            ).accepted
        )
        self.assertFalse(
            assess_remote_job(
                job(
                    "Homeoffice möglich.",
                    hybrid=True,
                )
            ).accepted
        )

    def test_explicit_applicant_requirements_override_office_country(self):
        for locations, expected in (
            ([RemoteLocation(name="Germany", kind="country")], True),
            ([RemoteLocation(name="US", kind="country")], False),
            ([RemoteLocation(name="Bavaria, Germany", kind="region")], False),
            ([RemoteLocation(name="EMEA", kind="region")], True),
            ([], False),
        ):
            with self.subTest(locations=locations):
                result = assess_remote_job(
                    job(
                        "Fully remote worldwide.",
                        has_applicant_restrictions=True,
                        applicant_locations=locations,
                        job_countries=["US"],
                    )
                )
                self.assertEqual(result.accepted, expected)

    def test_text_restrictions_override_positive_metadata(self):
        result = assess_remote_job(
            job(
                "Fully remote. Wohnsitz in Berlin erforderlich.",
                has_applicant_restrictions=True,
                applicant_locations=[RemoteLocation(name="Germany", kind="country")],
            )
        )
        self.assertFalse(result.accepted)

    def test_only_individual_job_metadata_can_supply_remote_proof(self):
        candidate = job("You develop Java applications.")
        candidate.is_remote = True
        self.assertFalse(assess_remote_job(candidate).accepted)
        candidate.remote_metadata.full_remote = True
        self.assertTrue(assess_remote_job(candidate).accepted)

    def test_metadata_never_leaks_to_serialized_output(self):
        self.assertNotIn("remote_metadata", job().model_dump())


class RemoteStructuredDataTests(unittest.TestCase):
    def test_matching_json_ld_and_geographical_restrictions(self):
        posting = {
            "@type": "JobPosting",
            "url": "https://example.org/jobs/123456",
            "jobLocationType": "TELECOMMUTE",
            "jobLocation": {"address": {"addressCountry": {"name": "US"}}},
            "applicantLocationRequirements": {"@type": "Country", "name": "Germany"},
        }
        html = (
            '<script type="application/ld+json">'
            + json.dumps({"@graph": [posting]})
            + "</script>"
        )
        parsed = extract_job_posting(
            BeautifulSoup(html, "html.parser"), posting["url"] + "?tracking=1"
        )
        metadata = metadata_from_job_posting(parsed, description_complete=True)
        self.assertTrue(metadata.full_remote)
        self.assertEqual(metadata.job_countries, ["US"])
        self.assertEqual(metadata.applicant_locations[0].name, "Germany")
        candidate = job("Develop software.")
        candidate.remote_metadata = metadata
        self.assertTrue(assess_remote_job(candidate).accepted)

    def test_recommendations_do_not_supply_remote_evidence(self):
        posting = {
            "@type": "JobPosting",
            "url": "https://example.org/jobs/999999",
            "jobLocationType": "TELECOMMUTE",
        }
        soup = BeautifulSoup(
            '<script type="application/ld+json">' + json.dumps(posting) + "</script>",
            "html.parser",
        )
        self.assertEqual(
            extract_job_posting(soup, "https://example.org/jobs/123456"), {}
        )

    def test_ambiguous_and_malformed_structured_data(self):
        for text in ("{broken}", '[{"@type":"JobPosting"},{"@type":"JobPosting"}]'):
            soup = BeautifulSoup(
                '<script type="application/ld+json">' + text + "</script>",
                "html.parser",
            )
            self.assertEqual(
                extract_job_posting(soup, "https://example.org/jobs/123456"), {}
            )


class RemoteSearchTests(unittest.TestCase):
    def inputs(self, **kwargs):
        return ScraperInput(
            site_type=[Site.XING],
            country=Country.GERMANY,
            remote_mode="strict",
            **kwargs,
        )

    def test_verified_offsets_and_candidate_cap(self):
        search = StrictRemoteSearch(self.inputs(offset=1, results_wanted=2), Mock())
        jobs = []
        with patch("jobspy.remote.MAX_REMOTE_CANDIDATES", 4):
            for i, description in enumerate(
                ["Hybrid work.", "100% remote.", "100% remote.", "100% remote."]
            ):
                self.assertTrue(search.visit(str(i)))
                candidate = job(description)
                candidate.id = str(i)
                if search.accept(candidate):
                    jobs.append(candidate)
            self.assertTrue(search.done)
            self.assertFalse(search.visit("5"))
            self.assertEqual([j.id for j in search.finish(jobs).jobs], ["2", "3"])
            self.assertTrue(all(j.remote_evidence and j.is_remote for j in jobs))

    def test_duplicate_candidates_do_not_use_the_budget(self):
        search = StrictRemoteSearch(self.inputs(), Mock())
        self.assertTrue(search.visit("same"))
        self.assertFalse(search.visit("same"))
        self.assertEqual(len(search.seen), 1)

    def test_time_budget_returns_partial_results_and_refuses_new_candidates(self):
        logger = Mock()
        with patch("jobspy.remote.monotonic", return_value=100) as clock:
            search = StrictRemoteSearch(self.inputs(remote_max_seconds=10), logger)
            self.assertTrue(search.visit("first"))
            candidate = job()
            self.assertTrue(search.accept(candidate))
            clock.return_value = 110
            self.assertTrue(search.done)
            self.assertFalse(search.visit("later"))
            self.assertEqual(search.finish([candidate]).jobs, [candidate])
            self.assertEqual(logger.warning.call_args.args[1], "time budget reached")

    def test_indeed_age_boundary_and_bad_dates(self):
        now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        boundary = (now - timedelta(hours=48)).timestamp() * 1000
        self.assertTrue(indeed_date_is_recent(boundary, 48, now))
        self.assertFalse(indeed_date_is_recent(boundary - 1, 48, now))
        for value in (None, "bad", True, float("inf")):
            self.assertFalse(indeed_date_is_recent(value, 48, now))
        self.assertTrue(indeed_date_is_recent(now.isoformat(), 0, now))


class RemotePublicApiTests(unittest.TestCase):
    def test_invalid_requests_fail_before_starting_scrapers(self):
        for kwargs in (
            {"remote_mode": "strict"},
            {"remote_mode": "strict", "country": "austria"},
            {"remote_mode": "strict", "country": "germany", "location": "Berlin"},
            {"remote_mode": "strict", "country": "germany", "offset": -1},
            {"remote_mode": "strict", "country": "germany", "site_name": "google"},
            {"remote_mode": "unknown"},
            {"remote_mode": "strict", "country": "germany", "remote_max_candidates": 0},
            {
                "remote_mode": "strict",
                "country": "germany",
                "remote_max_candidates": 1001,
            },
            {"remote_mode": "strict", "country": "germany", "remote_max_seconds": 0},
            {
                "remote_mode": "strict",
                "country": "germany",
                "remote_max_seconds": float("inf"),
            },
            {
                "remote_mode": "strict",
                "country": "germany",
                "remote_max_seconds": float("nan"),
            },
        ):
            with self.subTest(kwargs=kwargs), patch("jobspy.Indeed") as factory:
                with self.assertRaises(ValueError):
                    scrape_jobs(**kwargs)
                factory.assert_not_called()

    def test_strict_country_normalization_and_unsupported_sources(self):
        received = []

        def scrape(inputs):
            received.append(inputs)
            candidate = job()
            assessment = StrictRemoteSearch(inputs, Mock())
            assessment.visit(candidate.id)
            assessment.accept(candidate)
            return assessment.finish([candidate])

        factory = Mock(return_value=Mock(scrape=scrape))
        with patch("jobspy.Indeed", factory), patch("jobspy.Google") as google:
            frame = scrape_jobs(
                site_name=["indeed", "google"],
                remote_mode="strict",
                country="germany",
                location="Deutschland",
                distance=100,
                linkedin_fetch_description=False,
                remote_max_candidates=50,
                remote_max_seconds=30,
            )
        google.assert_not_called()
        self.assertEqual(received[0].country, Country.GERMANY)
        self.assertIsNone(received[0].location)
        self.assertIsNone(received[0].distance)
        self.assertTrue(received[0].linkedin_fetch_description)
        self.assertTrue(received[0].is_remote)
        self.assertEqual(received[0].remote_max_candidates, 50)
        self.assertEqual(received[0].remote_max_seconds, 30)
        self.assertEqual(list(frame.columns), desired_order + STRICT_REMOTE_COLUMNS)
        self.assertEqual(frame.iloc[0].remote_min_percent, 100)

    def test_each_source_receives_its_own_input_copy(self):
        received = []

        def scrape(inputs):
            received.append(inputs)
            inputs.results_wanted = 1
            return JobResponse(jobs=[])

        factory = Mock(return_value=Mock(scrape=scrape))
        with patch("jobspy.Indeed", factory), patch("jobspy.Xing", factory):
            scrape_jobs(
                site_name=["indeed", "xing"], remote_mode="strict", country="germany"
            )
        self.assertIsNot(received[0], received[1])

    def test_empty_strict_output_has_schema_and_zero_does_not_request(self):
        with patch("jobspy.Indeed") as factory:
            frame = scrape_jobs(
                site_name="indeed",
                remote_mode="strict",
                country="germany",
                results_wanted=0,
            )
            factory.assert_not_called()
        self.assertEqual(list(frame.columns), desired_order + STRICT_REMOTE_COLUMNS)

    def test_legacy_output_keeps_its_columns(self):
        with patch(
            "jobspy.Indeed",
            return_value=Mock(scrape=Mock(return_value=JobResponse(jobs=[job()]))),
        ):
            frame = scrape_jobs(
                site_name="indeed", country_indeed="germany", is_remote=True
            )
        self.assertEqual(list(frame.columns), desired_order)


if __name__ == "__main__":
    unittest.main()
