# __init__.py
from __future__ import annotations

import random
import re
import time
import urllib.parse
from typing import Optional, List, Dict, Any

from bs4 import BeautifulSoup, Tag

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
    remove_attributes,
    markdown_converter,
    plain_converter,
    extract_emails_from_text,
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
                    if len(job_list) >= scraper_input.results_wanted:
                        break
                    try:
                        # Quick check to skip already seen job IDs before fetching detail page
                        title_link = (
                            card.select_one("a[data-at='job-item-title']")
                            or card.select_one("a[href*='/stellenangebote--']")
                            or card.select_one("a[href*='/jobs--']")
                            or card.select_one("h2 a")
                            or card.select_one("a")
                        )
                        if not title_link or not title_link.get("href"):
                            continue
                        candidate_url = urllib.parse.urljoin(base_url, title_link["href"])
                        candidate_id = f"stepstone-{abs(hash(candidate_url))}"
                        if candidate_id in seen_ids:
                            continue

                        job_post = self._process_card(card, base_url)
                        if job_post and job_post.id not in seen_ids:
                            seen_ids.add(job_post.id)
                            job_list.append(job_post)
                            if len(job_list) >= scraper_input.results_wanted:
                                break
                            time.sleep(random.uniform(0.3, 0.7))
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
            if card.find("a",
                         href=lambda h: h and ("/stellenangebote--" in h or "/jobs--" in h or "-inline.html" in h)):
                valid_cards.append(card)

        if not valid_cards:
            # Fallback to any element containing job links
            job_links = soup.find_all("a", href=lambda h: h and ("/stellenangebote--" in h or "/jobs--" in h))
            for link in job_links:
                parent = link.find_parent("article") or link.find_parent("div")
                if parent and parent not in valid_cards:
                    valid_cards.append(parent)

        return valid_cards

    def _fetch_job_description(self, job_url: str) -> Optional[str]:
        """
        Fetches the job detail page and extracts the full description.
        Target container: <div class="job-ad-display-..."> (e.g. job-ad-display-1t26un2 or job-ad-display-e6cidt)
        or data-at="job-ad-content".
        """
        if not job_url:
            return None

        try:
            timeout_s = getattr(self.scraper_input, "request_timeout", 30) if self.scraper_input else 30
            response = self.session.get(job_url, timeout_seconds=timeout_s)
            if response.status_code != 200:
                log.warning(f"Failed to fetch StepStone detail page {job_url}: status {response.status_code}")
                return None

            soup = BeautifulSoup(response.text, "html.parser")

            # 1. Primary: data-at="job-ad-content" / data-atx-component="JobAdContent" (carries class job-ad-display-*)
            desc_elem = (
                soup.find("div", attrs={"data-at": "job-ad-content"})
                or soup.find("div", attrs={"data-atx-component": "JobAdContent"})
            )

            # 2. Fallback: div with class starting with job-ad-display- that contains section-text
            if not desc_elem:
                for div in soup.find_all("div"):
                    classes = div.get("class", [])
                    if any(c.startswith("job-ad-display-") for c in classes):
                        if div.find(attrs={"data-at": re.compile(r"^section-text-")}) or div.find(
                            class_=re.compile(r"^at-section-text-")
                        ):
                            desc_elem = div
                            break

            # 3. Fallback: div with class starting with job-ad-display- having substantial text
            if not desc_elem:
                candidates = []
                for div in soup.find_all("div"):
                    classes = div.get("class", [])
                    if any(c.startswith("job-ad-display-") for c in classes):
                        text_len = len(div.get_text(strip=True))
                        if text_len > 200:
                            candidates.append((text_len, div))
                if candidates:
                    candidates.sort(key=lambda x: x[0], reverse=True)
                    desc_elem = candidates[0][1]

            if not desc_elem:
                return None

            # Decompose style, script, and svg tags so CSS emotion styles do not contaminate the text
            for tag in desc_elem.find_all(["style", "script", "svg"]):
                tag.decompose()

            # Clean attribute noise from descendant elements while preserving essential links/images
            for tag in desc_elem.find_all(True):
                tag.attrs = {k: v for k, v in tag.attrs.items() if k in ("href", "src", "alt", "title")}

            desc_elem = remove_attributes(desc_elem)
            html_desc = desc_elem.prettify(formatter="html")

            desc_format = (
                getattr(self.scraper_input, "description_format", DescriptionFormat.MARKDOWN)
                if self.scraper_input
                else DescriptionFormat.MARKDOWN
            )
            if isinstance(desc_format, str):
                desc_format = DescriptionFormat(desc_format.lower())

            if desc_format == DescriptionFormat.HTML:
                return html_desc
            elif desc_format == DescriptionFormat.PLAIN:
                return plain_converter(html_desc)
            else:
                return markdown_converter(html_desc)

        except Exception as e:
            log.warning(f"Error fetching detail description from {job_url}: {e}")
            return None

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

        # Fetch detail description
        description = self._fetch_job_description(job_url)
        emails = extract_emails_from_text(description) if description else None

        # Check remote
        card_text = card.get_text(separator=" ", strip=True)
        is_remote = is_job_remote(title, loc_text or "", f"{card_text} {description or ''}")

        return JobPost(
            id=job_id,
            title=title,
            company_name=company_name,
            location=location,
            job_url=job_url,
            date_posted=date_posted,
            compensation=compensation,
            is_remote=is_remote,
            description=description,
            emails=emails,
            site=self.site,
        )


__all__ = ["StepStone"]
