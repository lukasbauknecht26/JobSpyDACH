import unittest
from unittest.mock import patch

from jobspy import scrape_jobs
from jobspy.google import Google
from jobspy.google.util import parse_google_jobs_html
from jobspy.model import Country, JobResponse, ScraperInput, Site


def make_job_info(location):
    info = [None] * 29
    info[0] = "Engineer"
    info[1] = "Example GmbH"
    info[2] = location
    info[3] = [["https://example.com/job"]]
    info[12] = None
    info[19] = "Description"
    info[28] = "job-id"
    return info


class GoogleLocationCountryTests(unittest.TestCase):
    def parse_location(self, raw_location, fallback=None):
        google = Google()
        google.scraper_input = ScraperInput(
            site_type=[Site.GOOGLE], location_country=fallback
        )
        return google._parse_job(make_job_info(raw_location)).location

    def parse_html_location(self, raw_location, fallback=None):
        html = f'''
            <div class="EimVGf" data-share-url="https://example.com/job">
                <div class="IFnjPb">Engineer</div>
                <div class="a3jPc">Example GmbH</div>
                <div class="FqK3wc">{raw_location}</div>
            </div>
        '''
        return parse_google_jobs_html(html, location_country=fallback)[0].location

    def test_bare_city_uses_country_context_and_normalizes_region(self):
        from jobspy.location import normalize_location

        location = self.parse_location("Ravensburg", Country.GERMANY)
        self.assertEqual(
            normalize_location(location).display_location(),
            "Ravensburg, Baden-W\u00fcrttemberg, Germany",
        )

    def test_card_country_takes_precedence_over_context(self):
        location = self.parse_location("Ravensburg, Germany", Country.AUSTRIA)
        self.assertEqual(location.country, "Germany")

    def test_fallback_does_not_change_existing_state(self):
        location = self.parse_location("Ravensburg, Baden-W\u00fcrttemberg", Country.GERMANY)
        self.assertEqual(location.state, "Baden-W\u00fcrttemberg")
        self.assertEqual(location.country, Country.GERMANY)

    def test_no_context_leaves_bare_city_unchanged(self):
        location = self.parse_location("Ravensburg")
        self.assertEqual(location.city, "Ravensburg")
        self.assertIsNone(location.country)

    def test_html_parser_completes_bare_city_with_context(self):
        from jobspy.location import normalize_location

        location = self.parse_html_location("Ravensburg", Country.GERMANY)
        self.assertEqual(
            normalize_location(location).display_location(),
            "Ravensburg, Baden-W\u00fcrttemberg, Germany",
        )

    def test_html_parser_keeps_card_country_over_context(self):
        location = self.parse_html_location("Ravensburg, Germany", Country.AUSTRIA)
        self.assertEqual(location.country, Country.GERMANY)

    def test_html_parser_falls_back_without_overwriting_state(self):
        location = self.parse_html_location(
            "Ravensburg, Baden-W\u00fcrttemberg", Country.GERMANY
        )
        self.assertEqual(location.state, "Baden-W\u00fcrttemberg")
        self.assertEqual(location.country, Country.GERMANY)

    def test_html_parser_without_context_leaves_bare_city(self):
        location = self.parse_html_location("Ravensburg")
        self.assertEqual(location.city, "Ravensburg")
        self.assertIsNone(location.country)

    def test_public_country_argument_is_separate_from_indeed_country(self):
        captured = []

        class FakeGoogle:
            def __init__(self, **kwargs):
                pass

            def scrape(self, scraper_input):
                captured.append(scraper_input)
                return JobResponse(jobs=[])

        for country in (Country.GERMANY, "Germany"):
            with self.subTest(country=country), patch("jobspy.Google", FakeGoogle):
                scrape_jobs(
                    site_name=Site.GOOGLE,
                    country=country,
                    country_indeed="usa",
                    verbose=0,
                )
        self.assertEqual(len(captured), 2)
        for scraper_input in captured:
            self.assertEqual(scraper_input.country, Country.USA)
            self.assertEqual(scraper_input.location_country, Country.GERMANY)

    def test_country_indeed_does_not_become_google_location_fallback(self):
        captured = []

        class FakeGoogle:
            def __init__(self, **kwargs):
                pass

            def scrape(self, scraper_input):
                captured.append(scraper_input)
                return JobResponse(jobs=[])

        with patch("jobspy.Google", FakeGoogle):
            scrape_jobs(
                site_name=Site.GOOGLE, country_indeed="germany", verbose=0
            )
        self.assertEqual(captured[0].country, Country.GERMANY)
        self.assertIsNone(captured[0].location_country)


if __name__ == "__main__":
    unittest.main()
