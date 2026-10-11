from contextlib import ExitStack
from datetime import date
import gzip
from importlib import resources
import json
import unittest
from unittest.mock import patch

from jobspy import scrape_jobs
from jobspy.location import _gazetteer, _normalize_parts, normalize_location
from jobspy.model import Country, JobPost, JobResponse, Location, Site
from jobspy.util import desired_order

BW = "Baden-W\u00fcrttemberg"
CANONICAL = f"Konstanz, {BW}, Germany"
KONSTANZ_EXAMPLES = [
    f"Constance, {BW}, Germany",
    "Konstanz, BW, DE, Germany",
    "Konstanz, Baden-Wurttemberg, Germany",
    CANONICAL,
    "78467 Konstanz, BADEN_WUERTTEMBERG, Germany",
]


class LocationTests(unittest.TestCase):
    def test_konstanz_strings_and_structured_fields(self):
        locations = [Location(city=value) for value in KONSTANZ_EXAMPLES]
        locations.extend(
            [
                Location(city="Constance", state=BW, country=Country.GERMANY),
                Location(city="Konstanz, BW, DE", country=Country.GERMANY),
                Location(city="Konstanz", state="BW, DE", country="Germany"),
                Location(city="Konstanz", state="Baden-Wurttemberg", country="DE"),
                Location(
                    city="78467 Konstanz",
                    state="BADEN_WUERTTEMBERG",
                    country="Deutschland",
                ),
                Location(city="DE-78467 Konstanz", state="DE-BW", country="deu"),
            ]
        )
        for location in locations:
            with self.subTest(location=location):
                before = location.model_copy()
                normalized = normalize_location(location)
                self.assertEqual(normalized.display_location(), CANONICAL)
                self.assertEqual(location, before)
                self.assertEqual(normalize_location(normalized), normalized)

    def test_german_names_regions_and_small_places(self):
        examples = [
            ("Munich", "Bavaria", "DE", "M\u00fcnchen, Bayern, Germany"),
            ("Muenchen", "BY", "Germany", "M\u00fcnchen, Bayern, Germany"),
            ("Konstanz", None, "Germany", CANONICAL),
            ("Vienna", None, "AT", "Wien, Wien, Austria"),
            ("Wien", "AT-9", "\u00d6sterreich", "Wien, Wien, Austria"),
            ("Zurich", "ZH", "CH", "Z\u00fcrich, Z\u00fcrich, Switzerland"),
            (
                "8001 Zuerich",
                "CH-ZH",
                "Suisse",
                "Z\u00fcrich, Z\u00fcrich, Switzerland",
            ),
            ("Geneva", "GE", "Switzerland", "Genf, Genf, Switzerland"),
            ("Gen\u00e8ve", None, "CH", "Genf, Genf, Switzerland"),
            (
                "Immenstaad am Bodensee",
                "BW",
                "DE",
                f"Immenstaad am Bodensee, {BW}, Germany",
            ),
            ("Heimertingen", "BAYERN", "DE", "Heimertingen, Bayern, Germany"),
            ("  konstanz  ", "Baden_Wu\u0308rttemberg", " germany ", CANONICAL),
        ]
        for city, state, country, expected in examples:
            with self.subTest(city=city, state=state):
                normalized = normalize_location(
                    Location(city=city, state=state, country=country)
                )
                self.assertEqual(normalized.display_location(), expected)

    def test_canonical_place_name_wins_over_same_name_alias(self):
        expected = f"\u00dcberlingen, {BW}, Germany"
        examples = [
            Location(city="\u00dcberlingen", state=BW, country="Germany"),
            Location(
                city="\u00dcberlingen", state="Baden-Wurttemberg", country="DE"
            ),
            Location(city=f"\u00dcberlingen, {BW}, Germany"),
            Location(city="\u00dcberlingen, Baden-Wurttemberg, Germany"),
        ]
        for location in examples:
            with self.subTest(location=location):
                normalized = normalize_location(location)
                self.assertEqual(normalized.display_location(), expected)

    def test_wuerttemberg_suffix_requires_baden_wuerttemberg_confirmation(self):
        expected = f"Ravensburg, {BW}, Germany"
        examples = [
            Location(
                city="Ravensburg",
                state="W\u00fcrttemberg, BADEN_WUERTTEMBERG",
                country="Germany",
            ),
            Location(
                city="Ravensburg, W\u00fcrttemberg",
                state="BADEN_WUERTTEMBERG",
                country="Germany",
            ),
            Location(
                city="Ravensburg, W\u00fcrttemberg, BADEN_WUERTTEMBERG, Germany"
            ),
        ]
        for location in examples:
            with self.subTest(location=location):
                self.assertEqual(
                    normalize_location(location).display_location(), expected
                )

        for location in (
            Location(city="Ravensburg", state="W\u00fcrttemberg", country="Germany"),
            Location(
                city="Ravensburg",
                state="W\u00fcrttemberg, Bayern",
                country="Germany",
            ),
        ):
            with self.subTest(location=location):
                self.assertIs(normalize_location(location), location)

    def test_uncertain_or_non_dach_locations_remain_intact(self):
        examples = [
            ("Konstanz", "Berlin", "Germany"),
            ("Konstanz, BW, Berlin, Germany", None, None),
            ("Konstanz", "Unknown region", "Germany"),
            ("Frankfurt am Main", "M\u00fcnchen", "Germany"),
            ("Frankfurt am Main, M\u00fcnchen, Germany", None, None),
            ("Konstanz / Berlin", None, "Germany"),
            ("Konstanz (Hybrid)", None, "Germany"),
            ("Konstnanz", "BW", "DE"),
            ("Lindenberg", None, "DE"),
            ("Lindenberg", "Bavaria", "DE"),
            ("M\u00fcnchen", None, "Germany"),
            ("Konstanz, DE", None, "Austria"),
            ("Konstanz", "BW", None),
            ("Vienna", None, Country.USA),
            ("Remote", None, "Germany"),
            ("bundesweit", "Home-Office", "Germany"),
            ("Home-Office", None, "DE"),
            ("Deutschlandweit / Homeoffice", None, "Germany"),
            ("Berlin", "CT", "USA"),
            ("Paris", None, Country.FRANCE),
            ("Paris, France", None, None),
            (None, None, Country.GERMANY),
            ("", None, None),
            ("Germany", None, None),
            ("78467", None, "DE"),
        ]
        for city, state, country in examples:
            with self.subTest(city=city, state=state, country=country):
                original = Location(city=city, state=state, country=country)
                self.assertIs(normalize_location(original), original)

    def test_region_aliases_are_country_scoped(self):
        gazetteer = _gazetteer()
        self.assertEqual(gazetteer.region("BE", "DE"), "DE.16")
        self.assertEqual(gazetteer.region("BE", "CH"), "CH.BE")
        self.assertEqual(gazetteer.region("ST", "DE"), "DE.14")
        self.assertEqual(gazetteer.region("ST", "AT"), "AT.06")

    def test_missing_or_corrupt_data_warns_once_and_preserves_locations(self):
        for error in (FileNotFoundError("missing"), None):
            with self.subTest(error=error):
                _gazetteer.cache_clear()
                _normalize_parts.cache_clear()
                self.addCleanup(_gazetteer.cache_clear)
                self.addCleanup(_normalize_parts.cache_clear)
                with (
                    patch("jobspy.location.resources.files") as files,
                    patch("jobspy.location.log.warning") as warning,
                ):
                    reader = files.return_value.joinpath.return_value.read_bytes
                    if error:
                        reader.side_effect = error
                    else:
                        reader.return_value = b"corrupt gzip"
                    for city in ("Konstanz", "Munich", "Vienna"):
                        original = Location(city=city, country="Germany")
                        self.assertIs(normalize_location(original), original)
                    warning.assert_called_once()
                    reader.assert_called_once()

    def test_invalid_schema_falls_back(self):
        _gazetteer.cache_clear()
        _normalize_parts.cache_clear()
        self.addCleanup(_gazetteer.cache_clear)
        self.addCleanup(_normalize_parts.cache_clear)
        with (
            patch("jobspy.location.resources.files") as files,
            patch("jobspy.location.log.warning") as warning,
        ):
            files.return_value.joinpath.return_value.read_bytes.return_value = (
                gzip.compress(b'{"schema_version": 999}')
            )
            original = Location(city="Konstanz", country="DE")
            self.assertIs(normalize_location(original), original)
            warning.assert_called_once()

    def test_bundled_data_integrity(self):
        import hashlib

        data_dir = resources.files("jobspy").joinpath("data")
        data = data_dir.joinpath("locations.json.gz").read_bytes()
        metadata = json.loads(data_dir.joinpath("locations.sources.json").read_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), metadata["data_sha256"])
        gazetteer = _gazetteer()
        self.assertEqual(len(gazetteer.regions), 51)
        self.assertEqual(len(gazetteer.places), metadata["place_count"])


class LocationIntegrationTests(unittest.TestCase):
    def test_all_portals_share_normalization_without_network_or_schema_changes(self):
        scraper_names = (
            "LinkedIn",
            "Indeed",
            "ZipRecruiter",
            "Glassdoor",
            "Google",
            "StepStone",
            "Arbeitsagentur",
            "Xing",
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "socket.socket.connect",
                    side_effect=AssertionError("Unexpected network request"),
                )
            )
            for index, name in enumerate(scraper_names):
                scraper = stack.enter_context(patch(f"jobspy.{name}"))
                scraper.return_value.scrape.return_value = JobResponse(
                    jobs=[
                        JobPost(
                            id=f"job-{index}",
                            title="Developer",
                            company_name="Example",
                            job_url=f"https://example.com/{index}",
                            location=Location(
                                city=KONSTANZ_EXAMPLES[index % len(KONSTANZ_EXAMPLES)]
                            ),
                            date_posted=date(2026, 10, 9),
                        )
                    ]
                )
            result = scrape_jobs(
                site_name=list(Site), country_indeed="germany", verbose=0
            )
        self.assertEqual(len(result), len(Site))
        self.assertEqual(set(result["location"]), {CANONICAL})
        self.assertEqual(list(result.columns), desired_order)
        self.assertEqual(result["site"].tolist(), sorted(site.value for site in Site))

    def test_normalization_can_be_disabled_without_loading_data(self):
        with (
            patch("jobspy.Indeed") as scraper,
            patch(
                "jobspy.location._gazetteer",
                side_effect=AssertionError("Data must not load"),
            ),
        ):
            scraper.return_value.scrape.return_value = JobResponse(
                jobs=[
                    JobPost(
                        title="Developer",
                        company_name="Example",
                        job_url="https://example.com/job",
                        location=Location(
                            city="Constance", state="BW", country=Country.GERMANY
                        ),
                    )
                ]
            )
            result = scrape_jobs(
                site_name="indeed",
                country_indeed="germany",
                normalize_locations=False,
                verbose=0,
            )
        self.assertEqual(result.iloc[0]["location"], "Constance, BW, Germany")


if __name__ == "__main__":
    unittest.main()
