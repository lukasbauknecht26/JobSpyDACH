import unittest

from bs4 import BeautifulSoup

from jobspy.arbeitsagentur.util import parse_job_type_v6
from jobspy.google.util import extract_job_type
from jobspy.model import JobType
from jobspy.stepstone.util import parse_job_types
from jobspy.util import get_enum_from_job_type, get_enum_from_value


class JobTypeTests(unittest.TestCase):
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
            [JobType.APPRENTICESHIP, JobType.FULL_TIME],
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
