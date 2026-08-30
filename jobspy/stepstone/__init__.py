# __init__.py
from __future__ import annotations

import random
import time
import urllib.parse
from typing import Optional, List, Dict, Any
from bs4 import BeautifulSoup, Tag

from jobspy.exception import StepStoneException
from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Country,
    DescriptionFormat,
)
from jobspy.stepstone.constant import headers, job_card_selectors
from jobspy.stepstone.util import (
    parse_location,
    parse_relative_date,
    parse_compensation,
    is_job_remote,
)
from jobspy.util import (
    create_logger,
    create_session,
    markdown_converter,
    plain_converter,
)

log = create_logger("StepStone")


class StepStone(Scraper):
    delay = 2
    band_delay = 2

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.STEPSTONE, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.session = create_session(
            proxies=self.proxies,
            ca_cert=ca_cert,
            is_tls=True,
            has_retry=True,
            delay=3,
        )
        self.session.headers.update(headers)
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self.scraper_input = None

    def _get_base_url(self, country: Optional[Country]) -> str:
        if country == Country.AUSTRIA:
            return "https://www.stepstone.at"
        return "https://www.stepstone.de"

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes StepStone jobs based on criteria in scraper_input.
        """
        self.scraper_input = scraper_input
        job_list: list[JobPost] = []
        seen_ids = set()
        page = 1
        base_url = self._get_base_url(scraper_input.country)

        # Build base search path
        query_part = urllib.parse.quote(scraper_input.search_term.strip()) if scraper_input.search_term else ""
        location_part = urllib.parse.quote(scraper_input.location.strip()) if scraper_input.location else ""

        if query_part and location_part:
            path = f"/jobs/{query_part}/in-{location_part}"
        elif query_part:
            path = f"/jobs/{query_part}"
        elif location_part:
            path = f"/jobs/in-{location_part}"
        else:
            path = "/jobs"

        search_url = f"{base_url}{path}"

        while len(job_list) < scraper_input.results_wanted:
            params: Dict[str, Any] = {"page": page}
            if scraper_input.distance is not None:
                params["radius"] = scraper_input.distance
            if scraper_input.hours_old:
                params["days"] = max(1, scraper_input.hours_old // 24)

            log.info(f"Fetching StepStone page {page} from {search_url} (params: {params})")

            try:
                # Use timeout_seconds for tls_client session
                timeout_s = getattr(scraper_input, "request_timeout", 30)
                response = self.session.get(
                    search_url,
                    params=params,
                    timeout_seconds=timeout_s,
                )

                if response.status_code not in (200, 301, 302):
                    log.error(f"StepStone returned status code {response.status_code}")
                    break

                soup = BeautifulSoup(response.text, "html.parser")
                job_cards = self._extract_job_cards(soup)

                if not job_cards:
                    log.info(f"No job cards found on StepStone page {page}. Ending pagination.")
                    break

                initial_count = len(job_list)
                for card in job_cards:
                    try:
                        job_post = self._process_card(card, base_url)
                        if job_post and job_post.id not in seen_ids:
                            seen_ids.add(job_post.id)
                            job_list.append(job_post)
                            if len(job_list) >= scraper_input.results_wanted:
                                break
                    except Exception as e:
                        log.error(f"Error processing StepStone card: {e}")
                        continue

                if len(job_list) == initial_count:
                    log.info("No new unique jobs found on this page. Ending pagination.")
                    break

                page += 1
                time.sleep(random.uniform(self.delay, self.delay + self.band_delay))

            except Exception as e:
                log.error(f"Error during StepStone scraping: {e}")
                break

        job_list = job_list[: scraper_input.results_wanted]
        return JobResponse(jobs=job_list)

    def _extract_job_cards(self, soup: BeautifulSoup) -> List[Tag]:
        """
        Extracts valid job listing cards, filtering out filter/facet-only cards.
        """
        # Select cards that have an actual job link
        cards = soup.select("article[data-at='job-item'], article[data-genesis-element='CARD']")
        valid_cards = []
        for card in cards:
            if card.find("a", href=lambda h: h and ("/stellenangebote--" in h or "/jobs--" in h or "-inline.html" in h)):
                valid_cards.append(card)

        if not valid_cards:
            # Fallback to any element containing job links
            job_links = soup.find_all("a", href=lambda h: h and ("/stellenangebote--" in h or "/jobs--" in h))
            for link in job_links:
                parent = link.find_parent("article") or link.find_parent("div")
                if parent and parent not in valid_cards:
                    valid_cards.append(parent)

        return valid_cards

    def _process_card(self, card: Tag, base_url: str) -> Optional[JobPost]:
        """
        Parses a single job card element into a JobPost object.
        """
        # Extract title and URL
        title_link = (
            card.select_one("a[data-at='job-item-title']")
            or card.select_one("a[href*='/stellenangebote--']")
            or card.select_one("a[href*='/jobs--']")
            or card.select_one("h2 a")
            or card.select_one("a")
        )

        if not title_link or not title_link.get("href"):
            return None

        title = title_link.get_text(strip=True)
        raw_url = title_link["href"]
        job_url = urllib.parse.urljoin(base_url, raw_url)

        # Generate unique ID from URL
        job_id = f"stepstone-{abs(hash(job_url))}"

        # Extract company name
        company_elem = (
            card.select_one("[data-at='job-item-company-name']")
            or card.select_one("[data-genesis-element='CARD_SUBTITLE']")
            or card.select_one("span[class*='company']")
            or card.select_one("div[class*='company']")
        )
        company_name = company_elem.get_text(strip=True) if company_elem else "N/A"

        # Extract location
        loc_elem = (
            card.select_one("[data-at='job-item-location']")
            or card.select_one("[data-genesis-element='CARD_LOCATION']")
            or card.select_one("span[class*='location']")
            or card.select_one("div[class*='location']")
        )
        loc_text = loc_elem.get_text(strip=True) if loc_elem else None
        location = parse_location(loc_text, Country.GERMANY)

        # Extract date
        time_elem = (
            card.select_one("time")
            or card.select_one("[data-at='job-item-time-ago']")
            or card.select_one("span[class*='date']")
        )
        date_text = time_elem.get_text(strip=True) if time_elem else None
        date_posted = parse_relative_date(date_text)

        # Extract compensation if listed
        salary_elem = card.select_one("[data-at='job-item-salary']") or card.select_one("span[class*='salary']")
        salary_text = salary_elem.get_text(strip=True) if salary_elem else None
        compensation = parse_compensation(salary_text)

        # Check remote
        card_text = card.get_text(separator=" ", strip=True)
        is_remote = is_job_remote(title, loc_text or "", card_text)

        return JobPost(
            id=job_id,
            title=title,
            company_name=company_name,
            location=location,
            job_url=job_url,
            date_posted=date_posted,
            compensation=compensation,
            is_remote=is_remote,
            site=self.site,
        )


__all__ = ["StepStone"]
