# __init__.py
from __future__ import annotations

import random
import time
from datetime import date, datetime, timedelta
from math import ceil
from typing import Optional, List, Dict, Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

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
    DescriptionFormat,
    RemoteMetadata,
)
from jobspy.remote import StrictRemoteSearch, is_strict
from jobspy.util import (
    create_logger,
    create_session,
    markdown_converter,
    plain_converter,
    remove_attributes,
    extract_emails_from_text,
)

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
        super().__init__(
            Site.ARBEITSAGENTUR, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent
        )
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
        self._blocked = False

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Jobsuche Bundesagentur für Arbeit via official REST API v6.
        """
        self.scraper_input = scraper_input
        self._blocked = False
        strict = (
            StrictRemoteSearch(scraper_input, log) if is_strict(scraper_input) else None
        )
        target = strict.target if strict else scraper_input.results_wanted
        job_list: list[JobPost] = []
        seen_ids = set()
        page = 1
        page_size = (
            min(scraper_input.results_wanted, 50)
            if scraper_input.results_wanted
            else 25
        )

        params: Dict[str, Any] = {
            "size": page_size,
            "pav": False,
        }
        if strict:
            params["homeoffice"] = "prozentual_95"
            params["size"] = page_size = 50

        if scraper_input.search_term:
            params["was"] = scraper_input.search_term

        if scraper_input.location:
            params["wo"] = scraper_input.location
            params["umkreis"] = (
                scraper_input.distance if scraper_input.distance is not None else 25
            )

        cutoff_date = None
        if scraper_input.hours_old is not None:
            cutoff_date = (
                datetime.now() - timedelta(hours=scraper_input.hours_old)
            ).date()
            days_old = max(0, ceil(scraper_input.hours_old / 24))
            if days_old <= 100:
                params["veroeffentlichtseit"] = days_old

        while (
            len(job_list) < target
            and not self._blocked
            and not (strict and strict.done)
        ):
            params["page"] = page
            log.info(
                f"Fetching Arbeitsagentur jobs page {page} for '{scraper_input.search_term}'"
            )

            try:
                response = self.session.get(
                    API_URL,
                    params=params,
                    timeout=getattr(scraper_input, "request_timeout", 30),
                )

                if response.status_code != 200:
                    log.error(
                        f"Arbeitsagentur response status {response.status_code}: {response.text[:200]}"
                    )
                    break

                data = response.json()
                items = data.get("ergebnisliste", [])
                if not items:
                    log.info("No more jobs found from Arbeitsagentur.")
                    break

                initial_count = len(seen_ids)
                for item in items:
                    if self._blocked or strict and strict.done:
                        break
                    try:
                        refnr = item.get("referenznummer")
                        if not refnr or refnr in seen_ids:
                            continue
                        if strict and not strict.visit(refnr):
                            continue
                        seen_ids.add(refnr)
                        # The API may ignore its age filter. Check first publication
                        # locally before fetching details; dates have day precision.
                        if cutoff_date is not None:
                            posted = self._get_date_posted(item)
                            if posted is None or posted < cutoff_date:
                                if strict:
                                    strict.reject("age_filter")
                                continue
                        job_post = self._process_job_item(item)
                        if strict.accept(job_post) if strict else job_post is not None:
                            job_list.append(job_post)
                            if len(job_list) >= target:
                                break
                    except Exception as e:
                        if strict:
                            strict.reject("malformed_job")
                        log.error(f"Error processing Arbeitsagentur job item: {e}")
                        continue

                if len(seen_ids) == initial_count:
                    log.info("No new unique jobs on this page. Ending pagination.")
                    break

                if strict and (
                    strict.done
                    or self._blocked
                    or isinstance(data.get("maxErgebnisse"), int)
                    and page * page_size >= data["maxErgebnisse"]
                ):
                    break

                page += 1
                time.sleep(random.uniform(self.delay, self.delay + self.band_delay))

            except Exception as e:
                log.error(f"Error during Arbeitsagentur scraping: {e}")
                break

        if strict:
            return strict.finish(job_list)
        job_list = job_list[: scraper_input.results_wanted]
        return JobResponse(jobs=job_list)

    def _fetch_job_details(
        self, job_url: str, *, remote_metadata: RemoteMetadata | None = None
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Fetches the description and external description URL from the detail page.
        """
        if not job_url:
            return None, None

        job_url_direct = None
        try:
            timeout = (
                getattr(self.scraper_input, "request_timeout", 15)
                if self.scraper_input
                else 15
            )
            response = self.session.get(job_url, timeout=timeout)
            if is_strict(self.scraper_input) and response.status_code in {403, 429}:
                self._blocked = True
            if response.status_code != 200:
                log.warning(
                    f"Failed to fetch Arbeitsagentur detail page {job_url}: status {response.status_code}"
                )
                return None, None

            soup = BeautifulSoup(response.content, "html.parser")
            external_link = soup.select_one(
                "a#detail-beschreibung-externe-url-btn[href]"
            ) or soup.select_one(
                "#detail-beschreibung-extern a[href]:not(#externe-url-quelle)"
            )
            if external_link:
                href = external_link.get("href", "").strip()
                if href:
                    external_url = urljoin(job_url, href)
                    if urlsplit(external_url).scheme in ("http", "https"):
                        job_url_direct = external_url

            desc_elem = soup.find(id="detail-beschreibung-text-container") or soup.find(
                "div", class_=lambda c: c and "ba-copytext" in c.split()
            )
            if not desc_elem:
                return None, job_url_direct

            desc_elem = remove_attributes(desc_elem)
            html_desc = desc_elem.prettify(formatter="html")
            if remote_metadata is not None:
                remote_metadata.description_text = html_desc

            desc_format = (
                getattr(
                    self.scraper_input, "description_format", DescriptionFormat.MARKDOWN
                )
                if self.scraper_input
                else DescriptionFormat.MARKDOWN
            )
            if isinstance(desc_format, str):
                desc_format = DescriptionFormat(desc_format.lower())

            if desc_format == DescriptionFormat.HTML:
                return html_desc, job_url_direct
            elif desc_format == DescriptionFormat.PLAIN:
                return plain_converter(html_desc), job_url_direct
            else:
                return markdown_converter(html_desc), job_url_direct

        except Exception as e:
            log.warning(f"Error fetching detail description from {job_url}: {e}")
            return None, job_url_direct

    @staticmethod
    def _get_date_posted(item: Dict[str, Any]) -> Optional[date]:
        date_posted_str = item.get("datumErsteVeroeffentlichung")
        if not date_posted_str and isinstance(
            item.get("veroeffentlichungszeitraum"), dict
        ):
            date_posted_str = item["veroeffentlichungszeitraum"].get("von")
        return parse_date(date_posted_str)

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
        date_posted = self._get_date_posted(item)

        job_types = parse_job_type_v6(item)
        hauptberuf = item.get("hauptberuf")

        # Build fallback description overview
        description_parts = []
        if hauptberuf:
            description_parts.append(f"**Beruf:** {hauptberuf}")
        if item.get("stellenangebotsart"):
            description_parts.append(
                f"**Angebotsart:** {item.get('stellenangebotsart')}"
            )
        if item.get("homeofficetyp"):
            description_parts.append(f"**Homeoffice:** {item.get('homeofficetyp')}")
        fallback_description = (
            "\n".join(description_parts) if description_parts else None
        )

        # Fetch full description from detail page
        raw_description_metadata = RemoteMetadata()
        if is_strict(self.scraper_input):
            full_description, external_description_url = self._fetch_job_details(
                job_url, remote_metadata=raw_description_metadata
            )
        else:
            full_description, external_description_url = self._fetch_job_details(
                job_url
            )
        job_url_direct = external_description_url or job_url_direct
        description = full_description if full_description else fallback_description

        emails = extract_emails_from_text(description) if description else None
        is_remote = is_job_remote(item)

        remote_metadata = (
            RemoteMetadata(
                description_complete=bool(full_description)
                and not external_description_url,
                description_text=raw_description_metadata.description_text,
                advertised_percent=(
                    item.get("homeofficeprozent")
                    if item.get("homeofficetyp") == "ANGABE_IN_PROZENT"
                    else None
                ),
                job_countries=[
                    place["adresse"]["land"]
                    for place in item.get("stellenlokationen") or []
                    if isinstance(place, dict)
                    and isinstance(place.get("adresse"), dict)
                    and isinstance(place["adresse"].get("land"), str)
                ],
            )
            if is_strict(self.scraper_input)
            else RemoteMetadata()
        )

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
            remote_metadata=remote_metadata,
            description=description,
            emails=emails,
            listing_type=hauptberuf,
            site=self.site,
        )


__all__ = ["Arbeitsagentur"]
