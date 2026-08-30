# __init__.py
from __future__ import annotations

import random
import time
from typing import Optional, List, Dict, Any

from jobspy.arbeitsagentur.constant import API_URL, BASE_WEB_URL, headers
from jobspy.arbeitsagentur.util import (
    parse_location,
    parse_date,
    parse_job_type_v6,
    is_job_remote,
)
from jobspy.exception import ArbeitsagenturException
from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
)
from jobspy.util import create_logger, create_session

log = create_logger("Arbeitsagentur")


class Arbeitsagentur(Scraper):
    delay = 1
    band_delay = 1

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.ARBEITSAGENTUR, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.session = create_session(
            proxies=self.proxies,
            ca_cert=ca_cert,
            is_tls=False,
            has_retry=True,
            delay=3,
        )
        self.session.headers.update(headers)
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self.scraper_input = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Jobsuche Bundesagentur für Arbeit via official REST API v6.
        """
        self.scraper_input = scraper_input
        job_list: list[JobPost] = []
        seen_ids = set()
        page = 1
        page_size = min(scraper_input.results_wanted, 50) if scraper_input.results_wanted else 25

        params: Dict[str, Any] = {
            "size": page_size,
            "pav": False,
        }

        if scraper_input.search_term:
            params["was"] = scraper_input.search_term

        if scraper_input.location:
            params["wo"] = scraper_input.location
            params["umkreis"] = scraper_input.distance if scraper_input.distance is not None else 25

        if scraper_input.hours_old:
            days_old = max(1, scraper_input.hours_old // 24)
            params["veroeffentlichtab"] = days_old

        while len(job_list) < scraper_input.results_wanted:
            params["page"] = page
            log.info(f"Fetching Arbeitsagentur jobs page {page} for '{scraper_input.search_term}'")

            try:
                response = self.session.get(
                    API_URL,
                    params=params,
                    timeout=getattr(scraper_input, "request_timeout", 30),
                )

                if response.status_code != 200:
                    log.error(f"Arbeitsagentur response status {response.status_code}: {response.text[:200]}")
                    break

                data = response.json()
                items = data.get("ergebnisliste", [])
                if not items:
                    log.info("No more jobs found from Arbeitsagentur.")
                    break

                initial_count = len(job_list)
                for item in items:
                    try:
                        job_post = self._process_job_item(item)
                        if job_post and job_post.id not in seen_ids:
                            seen_ids.add(job_post.id)
                            job_list.append(job_post)
                            if len(job_list) >= scraper_input.results_wanted:
                                break
                    except Exception as e:
                        log.error(f"Error processing Arbeitsagentur job item: {e}")
                        continue

                if len(job_list) == initial_count:
                    log.info("No new unique jobs on this page. Ending pagination.")
                    break

                page += 1
                time.sleep(random.uniform(self.delay, self.delay + self.band_delay))

            except Exception as e:
                log.error(f"Error during Arbeitsagentur scraping: {e}")
                break

        job_list = job_list[: scraper_input.results_wanted]
        return JobResponse(jobs=job_list)

    def _process_job_item(self, item: Dict[str, Any]) -> Optional[JobPost]:
        """
        Transforms a REST API v6 job entry into a JobPost object.
        """
        refnr = item.get("referenznummer")
        if not refnr:
            return None

        job_id = f"aa-{refnr}"
        title = item.get("stellenangebotsTitel") or item.get("hauptberuf") or "N/A"
        company_name = item.get("firma") or "N/A"
        job_url = BASE_WEB_URL.format(refnr=refnr)
        job_url_direct = item.get("externeUrl")

        location = parse_location(item.get("stellenlokationen"))
        
        # Parse date posted
        date_posted_str = item.get("datumErsteVeroeffentlichung")
        if not date_posted_str and isinstance(item.get("veroeffentlichungszeitraum"), dict):
            date_posted_str = item.get("veroeffentlichungszeitraum", {}).get("von")
        date_posted = parse_date(date_posted_str)

        job_types = parse_job_type_v6(item)
        hauptberuf = item.get("hauptberuf")

        # Build description overview
        description_parts = []
        if hauptberuf:
            description_parts.append(f"**Beruf:** {hauptberuf}")
        if item.get("stellenangebotsart"):
            description_parts.append(f"**Angebotsart:** {item.get('stellenangebotsart')}")
        if item.get("homeofficetyp"):
            description_parts.append(f"**Homeoffice:** {item.get('homeofficetyp')}")
        description = "\n".join(description_parts) if description_parts else None

        is_remote = is_job_remote(item)

        return JobPost(
            id=job_id,
            title=title,
            company_name=company_name,
            location=location,
            job_url=job_url,
            job_url_direct=job_url_direct,
            date_posted=date_posted,
            job_type=job_types,
            is_remote=is_remote,
            description=description,
            listing_type=hauptberuf,
            site=self.site,
        )


__all__ = ["Arbeitsagentur"]
