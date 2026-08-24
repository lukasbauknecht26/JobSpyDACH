from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from bs4 import BeautifulSoup

from jobspy.ziprecruiter.constant import headers
from jobspy.util import (
    extract_emails_from_text,
    create_session,
    markdown_converter,
    plain_converter,
    remove_attributes,
    create_logger,
)
from jobspy.model import (
    JobPost,
    Compensation,
    Location,
    JobResponse,
    Country,
    DescriptionFormat,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.ziprecruiter.util import (
    get_job_type_enum,
    add_params,
    fetch_geo_coordinates,
)

log = create_logger("ZipRecruiter")


class ZipRecruiter(Scraper):
    base_url = "https://www.ziprecruiter.de"

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        """
        Initializes ZipRecruiter scraper for ziprecruiter.de
        """
        super().__init__(Site.ZIP_RECRUITER, proxies=proxies)

        self.scraper_input = None
        self.session = create_session(proxies=proxies, ca_cert=ca_cert, is_tls=True)
        self.session.headers.update(headers)
        if user_agent:
            self.session.headers["user-agent"] = user_agent

        self.delay = 3
        self.jobs_per_page = 20
        self.seen_urls = set()
        self.lat = None
        self.long = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes ZipRecruiter.de for jobs with scraper_input criteria.
        :param scraper_input: Information about job search criteria.
        :return: JobResponse containing a list of jobs.
        """
        self.scraper_input = scraper_input
        job_list: list[JobPost] = []

        try:
            self.session.get(self.base_url)
        except Exception as e:
            log.debug(f"ZipRecruiter session init: {e}")

        # Fetch coordinates for location if provided
        if scraper_input.location:
            lat, lng, _ = fetch_geo_coordinates(
                self.session, scraper_input.location, base_url=self.base_url
            )
            self.lat = lat
            self.long = lng

        max_pages = math.ceil(scraper_input.results_wanted / self.jobs_per_page)
        for page in range(1, max_pages + 1):
            if len(job_list) >= scraper_input.results_wanted:
                break
            if page > 1:
                time.sleep(self.delay)

            log.info(f"search page: {page} / {max_pages}")
            jobs_on_page, has_next = self._find_jobs_in_page(scraper_input, page=page)
            if jobs_on_page:
                job_list.extend(jobs_on_page)
            else:
                break

            if not has_next:
                break

        return JobResponse(jobs=job_list[: scraper_input.results_wanted])

    def _find_jobs_in_page(
        self, scraper_input: ScraperInput, page: int = 1
    ) -> tuple[list[JobPost], bool]:
        """
        Scrapes a page of ziprecruiter.de for jobs with scraper_input criteria.
        :param scraper_input: Search criteria
        :param page: Current page number
        :return: (list of jobs on page, has_next_page boolean)
        """
        params = add_params(scraper_input, page=page, lat=self.lat, long=self.long)
        search_url = f"{self.base_url}/jobs/search"

        try:
            res = self.session.get(search_url, params=params)
            if res.status_code not in range(200, 400):
                if res.status_code == 429:
                    err = "429 Response - Blocked by ZipRecruiter for too many requests"
                else:
                    err = f"ZipRecruiter response status code {res.status_code}"
                log.error(err)
                return [], False
        except Exception as e:
            log.error(f"ZipRecruiter request failed: {str(e)}")
            return [], False

        soup = BeautifulSoup(res.text, "html.parser")
        listings = soup.find_all("li", class_="job-listing")
        if not listings:
            return [], False

        has_next = (
            soup.find("a", rel="next") is not None
            or f"page={page + 1}" in res.text
        )

        with ThreadPoolExecutor(max_workers=min(len(listings), self.jobs_per_page)) as executor:
            job_results = [
                executor.submit(self._process_job, li) for li in listings
            ]

        job_list = list(filter(None, (result.result() for result in job_results)))
        return job_list, has_next

    def _process_job(self, li) -> JobPost | None:
        """
        Processes an individual job listing HTML element.
        """
        title_a = li.find("a", class_="jobList-title")
        if not title_a:
            return None

        title = title_a.get_text(strip=True)
        href = title_a.get("href", "")
        job_url = f"{self.base_url}{href}" if href.startswith("/") else href

        if job_url in self.seen_urls:
            return None
        self.seen_urls.add(job_url)

        m = re.search(r"/jobs/(\d+)", href)
        job_id = f"zr-{m.group(1)}" if m else None

        company = None
        loc_str = None
        meta = li.find("ul", class_="jobList-introMeta")
        if meta:
            meta_items = meta.find_all("li")
            if len(meta_items) > 0 and meta_items[0].get_text(strip=True):
                company = meta_items[0].get_text(strip=True)
            if len(meta_items) > 1 and meta_items[1].get_text(strip=True):
                loc_str = meta_items[1].get_text(strip=True)

        snippet_div = li.find("div", class_="jobList-description")
        snippet = snippet_div.get_text(strip=True) if snippet_div else ""

        # Fetch detail page
        detail_data = self._get_descr(job_url)

        description = detail_data.get("description") or snippet
        if (
            self.scraper_input.description_format == DescriptionFormat.MARKDOWN
            and description
        ):
            description = markdown_converter(description)
        elif (
            self.scraper_input.description_format == DescriptionFormat.PLAIN_TEXT
            and description
        ):
            description = plain_converter(description)

        date_posted = detail_data.get("date_posted")
        job_type = detail_data.get("job_type")
        if not company and detail_data.get("company"):
            company = detail_data.get("company")

        city = detail_data.get("city") or loc_str
        state = detail_data.get("state")
        country = detail_data.get("country") or (
            self.scraper_input.country if self.scraper_input else Country.GERMANY
        )

        location = Location(
            city=city,
            state=state,
            country=country,
        )

        return JobPost(
            id=job_id,
            title=title,
            company_name=company,
            location=location,
            job_type=job_type,
            date_posted=date_posted,
            job_url=job_url,
            description=description,
            emails=extract_emails_from_text(description) if description else None,
            job_url_direct=detail_data.get("job_url_direct"),
        )

    def _get_descr(self, job_url: str) -> dict:
        """
        Fetches job detail page and extracts Schema.org JSON-LD and HTML elements.
        """
        data = {
            "description": None,
            "date_posted": None,
            "job_type": None,
            "company": None,
            "city": None,
            "state": None,
            "country": None,
            "job_url_direct": None,
        }

        try:
            res = self.session.get(job_url, allow_redirects=True)
            if not res.ok:
                return data

            soup = BeautifulSoup(res.text, "html.parser")

            # Try parsing Schema.org JSON-LD
            json_ld = soup.find("script", type="application/ld+json")
            if json_ld and json_ld.string:
                try:
                    ld_json = json.loads(json_ld.string)
                    if isinstance(ld_json, dict):
                        if "description" in ld_json:
                            data["description"] = ld_json["description"]
                        if "datePosted" in ld_json:
                            try:
                                data["date_posted"] = datetime.fromisoformat(
                                    ld_json["datePosted"]
                                ).date()
                            except Exception:
                                pass
                        if "employmentType" in ld_json:
                            data["job_type"] = get_job_type_enum(
                                ld_json["employmentType"]
                            )
                        if "hiringOrganization" in ld_json and isinstance(
                            ld_json["hiringOrganization"], dict
                        ):
                            data["company"] = ld_json["hiringOrganization"].get("name")

                        job_loc = ld_json.get("jobLocation", {})
                        if isinstance(job_loc, dict):
                            address = job_loc.get("address", {})
                            if isinstance(address, dict):
                                data["city"] = address.get("addressLocality")
                                data["state"] = address.get("addressRegion")
                                c_str = address.get("addressCountry")
                                if c_str:
                                    try:
                                        data["country"] = Country.from_string(c_str)
                                    except Exception:
                                        pass
                except Exception as e:
                    log.debug(f"JSON-LD parse error for {job_url}: {e}")

            # Fallback to HTML description if not in JSON-LD
            if not data["description"]:
                job_descr_div = soup.find("div", class_="job_description") or soup.find(
                    "div", class_="job-description"
                )
                if job_descr_div:
                    data["description"] = remove_attributes(job_descr_div).prettify(
                        formatter="html"
                    )

        except Exception as e:
            log.debug(f"Error fetching description from {job_url}: {e}")

        return data
