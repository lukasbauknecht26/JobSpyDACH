import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup

from jobspy.arbeitsagentur.util import parse_job_type_v6
from jobspy.google.util import extract_job_type
from jobspy.indeed.util import get_job_type
from jobspy.linkedin import LinkedIn
from jobspy.model import JobPost, JobType, normalize_job_types
from jobspy.stepstone.util import parse_job_types
from jobspy.util import (
    extract_job_type as extract_description_job_type,
    get_enum_from_job_type,
    get_enum_from_value,
)
from jobspy.xing.util import parse_job_types as parse_xing_job_types


class JobTypeTests(unittest.TestCase):
    def test_job_titles_detect_training_roles_without_metadata(self):
        cases = (
            ("Students - Cloud Backend & Infrastructure (m/f/d)", JobType.INTERNSHIP),
            ("Werkstudent Software-, DevSecOps- & Platform Engineering [m, w, d]", JobType.INTERNSHIP),
            ("Student IT", JobType.INTERNSHIP),
            ("Studentin IT", JobType.INTERNSHIP),
            ("Studenten IT", JobType.INTERNSHIP),
            ("Werkstudent:in IT", JobType.INTERNSHIP),
            ("Working-Student IT", JobType.INTERNSHIP),
            ("Pflichtpraktikum IT", JobType.INTERNSHIP),
            ("Intern Software Engineering", JobType.INTERNSHIP),
            ("Ausbildung IT", JobType.APPRENTICESHIP),
            ("Auszubildender IT", JobType.APPRENTICESHIP),
            ("Azubi IT", JobType.APPRENTICESHIP),
            ("Apprenticeship IT", JobType.APPRENTICESHIP),
            ("Duales Bachelorstudium IT", JobType.APPRENTICESHIP),
            ("Dualer Studiengang IT", JobType.APPRENTICESHIP),
            ("DHBW IT", JobType.APPRENTICESHIP),
        )
        for title, expected in cases:
            for metadata in ({}, {"job_type": [JobType.PART_TIME]}):
                with self.subTest(title=title, metadata=metadata):
                    job = JobPost(
                        title=title,
                        company_name=None,
                        job_url="https://example.com/job",
                        location=None,
                        **metadata,
                    )
                    self.assertEqual(job.job_type, [expected])

    def test_unspecific_title_words_do_not_trigger_training_types(self):
        for title in (
            "International Software Engineer",
            "Interne Kommunikation",
            "Studentisches Recruiting Management",
            "Software Engineer mit Studium",
            "Ausbildungsleiter IT",
            "Bachelor Professional IT",
        ):
            with self.subTest(title=title):
                job = JobPost(
                    title=title,
                    company_name=None,
                    job_url="https://example.com/job",
                    location=None,
                    job_type=[JobType.FULL_TIME],
                )
                self.assertEqual(job.job_type, [JobType.FULL_TIME])

    def test_linkedin_werkstudent_title_overrides_parttime_metadata(self):
        scraper = LinkedIn.__new__(LinkedIn)
        scraper.country = "germany"
        scraper._get_job_details = Mock(return_value={
            "job_type": [JobType.PART_TIME],
            "job_level": "Internship",
        })
        card = BeautifulSoup(
            '<div><span class="sr-only">'
            'Werkstudent Software-, DevSecOps- &amp; Platform Engineering [m, w, d]'
            '</span></div>',
            "html.parser",
        ).div
        for full_description in (False, True):
            with self.subTest(full_description=full_description):
                job = scraper._process_job(card, "4474897366", full_description)
                self.assertEqual(job.job_type, [JobType.INTERNSHIP])
                if full_description:
                    self.assertEqual(job.job_level, "internship")

    def test_xing_student_title_is_recognized_before_filtering(self):
        self.assertEqual(
            parse_xing_job_types(
                {}, {"employmentType": "PART_TIME"}, {}, "Student IT"
            ),
            [JobType.INTERNSHIP],
        )

    def test_training_types_exclude_all_other_types(self):
        training_types = (JobType.APPRENTICESHIP, JobType.INTERNSHIP)
        for training_type in training_types:
            for other_type in JobType:
                if other_type in training_types:
                    continue
                for types in (
                    [other_type, training_type],
                    [training_type, other_type],
                ):
                    with self.subTest(types=types):
                        self.assertEqual(normalize_job_types(types), [training_type])
                        job = JobPost(
                            title="Example",
                            company_name=None,
                            job_url="https://example.com/job",
                            location=None,
                            job_type=types,
                        )
                        self.assertEqual(job.job_type, [training_type])

    def test_normalization_preserves_regular_and_training_combinations(self):
        for types in (
            None,
            [],
            [JobType.FULL_TIME, JobType.PART_TIME, JobType.TEMPORARY],
            [JobType.INTERNSHIP, JobType.APPRENTICESHIP],
        ):
            with self.subTest(types=types):
                self.assertEqual(normalize_job_types(types), types)

    def test_parsers_exclude_work_time_for_training_types(self):
        for label, expected in (
            ("Ausbildung", JobType.APPRENTICESHIP),
            ("Duales Studium", JobType.APPRENTICESHIP),
            ("Praktikum", JobType.INTERNSHIP),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    parse_job_type_v6(
                        {
                            "stellenangebotsart": label,
                            "arbeitszeitVollzeit": True,
                            "arbeitszeitTeilzeit": True,
                            "vertragsdauer": "Befristet",
                        }
                    ),
                    [expected],
                )
                self.assertEqual(
                    get_job_type(
                        [{"label": value} for value in ("Vollzeit", label, "Teilzeit")]
                    ),
                    [expected],
                )
                self.assertEqual(
                    parse_xing_job_types(
                        {},
                        {"employmentType": ["FULL_TIME", label, "PART_TIME"]},
                        {},
                        "Example",
                    ),
                    [expected],
                )

        for contract, expected in (
            ("Ausbildung, Studium", JobType.APPRENTICESHIP),
            ("Praktikum", JobType.INTERNSHIP),
            ("Studentenjobs, Werkstudent", JobType.INTERNSHIP),
        ):
            with self.subTest(contract=contract):
                soup = BeautifulSoup(
                    f'<span data-at="metadata-contract-type">{contract}</span>'
                    '<span data-at="metadata-work-type">Vollzeit, Teilzeit</span>',
                    "html.parser",
                )
                self.assertEqual(parse_job_types(soup), [expected])

        for extractor in (extract_job_type, extract_description_job_type):
            for label, expected in (
                ("apprenticeship", JobType.APPRENTICESHIP),
                ("internship", JobType.INTERNSHIP),
            ):
                with self.subTest(extractor=extractor, label=label):
                    self.assertEqual(
                        extractor(f"full time {label} part time contract"), [expected]
                    )

    def test_apprenticeship_aliases_resolve_to_a_single_type(self):
        for value in ("apprenticeship", "dual course", "ausbildung", "duales studium"):
            with self.subTest(value=value):
                self.assertEqual(get_enum_from_value(value), JobType.APPRENTICESHIP)
                self.assertEqual(get_enum_from_job_type(value), JobType.APPRENTICESHIP)

    def test_normalized_apprenticeship_aliases_resolve(self):
        self.assertEqual(get_enum_from_job_type("dualesstudium"), JobType.APPRENTICESHIP)
        self.assertEqual(get_enum_from_job_type("dual-course"), JobType.APPRENTICESHIP)

    def test_arbeitsagentur_classifies_apprenticeships_separately(self):
        self.assertEqual(
            parse_job_type_v6({"stellenangebotsart": "Ausbildung"}),
            [JobType.APPRENTICESHIP],
        )
        self.assertEqual(
            parse_job_type_v6({"stellenangebotsart": "Duales Studium"}),
            [JobType.APPRENTICESHIP],
        )
        self.assertEqual(
            parse_job_type_v6({"stellenangebotsart": "Praktikum"}),
            [JobType.INTERNSHIP],
        )

    def test_google_detects_apprenticeships(self):
        self.assertEqual(
            extract_job_type("Ausbildung zum Fachinformatiker"),
            [JobType.APPRENTICESHIP],
        )

    def test_stepstone_maps_contract_and_work_type_metadata(self):
        soup = BeautifulSoup(
            """
            <span data-at="metadata-contract-type"><span>Ausbildung, Studium</span></span>
            <span data-at="metadata-work-type"><span>Vollzeit, Homeoffice</span></span>
            """,
            "html.parser",
        )

        self.assertEqual(
            parse_job_types(soup),
            [JobType.APPRENTICESHIP],
        )

    def test_stepstone_maps_all_known_contract_types(self):
        expected_types = {
            "Befristeter Vertrag": JobType.TEMPORARY,
            "Studentenjobs, Werkstudent": JobType.INTERNSHIP,
            "Praktikum": JobType.INTERNSHIP,
        }

        for contract_type, expected_type in expected_types.items():
            with self.subTest(contract_type=contract_type):
                soup = BeautifulSoup(
                    f'<span data-at="metadata-contract-type">{contract_type}</span>',
                    "html.parser",
                )
                self.assertEqual(parse_job_types(soup), [expected_type])

    def test_stepstone_maps_work_type_without_contract_mapping(self):
        soup = BeautifulSoup(
            """
            <span data-at="metadata-contract-type">Feste Anstellung</span>
            <span data-at="metadata-work-type">Teilzeit, Homeoffice</span>
            """,
            "html.parser",
        )

        self.assertEqual(parse_job_types(soup), [JobType.PART_TIME])

    def test_stepstone_ignores_unknown_or_missing_metadata(self):
        self.assertIsNone(parse_job_types(BeautifulSoup("", "html.parser")))
        self.assertIsNone(
            parse_job_types(
                BeautifulSoup(
                    '<span data-at="metadata-contract-type">Feste Anstellung</span>',
                    "html.parser",
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
