from __future__ import annotations

import math
from datetime import datetime
from typing import Tuple

from jobspy.indeed.constant import job_search_query, api_headers
from jobspy.indeed.util import is_job_remote, get_compensation, get_job_type
from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    Location,
    JobResponse,
    JobType,
    DescriptionFormat,
    RemoteMetadata,
)
from jobspy.remote import StrictRemoteSearch, indeed_date_is_recent, is_strict
from jobspy.util import (
    extract_emails_from_text,
    markdown_converter,
    create_session,
    create_logger,
)

log = create_logger("Indeed")


class Indeed(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        """
        Initializes IndeedScraper with the Indeed API url
        """
        super().__init__(Site.INDEED, proxies=proxies)

        self.session = create_session(
            proxies=self.proxies, ca_cert=ca_cert, is_tls=False
        )
        self.scraper_input = None
        self.jobs_per_page = 100
        self.num_workers = 10
        self.seen_urls = set()
        self.headers = None
        self.api_country_code = None
        self.base_url = None
        self.api_url = "https://apis.indeed.com/graphql"

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Indeed for jobs with scraper_input criteria
        :param scraper_input:
        :return: job_response
        """
        self.scraper_input = scraper_input
        domain, self.api_country_code = self.scraper_input.country.indeed_domain_value
        self.base_url = f"https://{domain}.indeed.com"
        self.headers = api_headers.copy()
        self.headers["indeed-co"] = self.scraper_input.country.indeed_domain_value
        self._strict = (
            StrictRemoteSearch(scraper_input, log) if is_strict(scraper_input) else None
        )
        if self._strict:
            self.seen_urls = set()
            return self._scrape_strict()
        job_list = []
        page = 1

        cursor = None

        while len(self.seen_urls) < scraper_input.results_wanted + scraper_input.offset:
            log.info(
                f"search page: {page} / {math.ceil(scraper_input.results_wanted / self.jobs_per_page)}"
            )
            jobs, cursor = self._scrape_page(cursor)
            if not jobs:
                log.info(f"found no jobs on page: {page}")
                break
            job_list += jobs
            page += 1
        return JobResponse(
            jobs=job_list[
                scraper_input.offset : scraper_input.offset
                + scraper_input.results_wanted
            ]
        )

    def _scrape_strict(self) -> JobResponse:
        jobs = []
        cursor = None
        cursors = set()
        while not self._strict.done:
            before = len(self._strict.seen)
            try:
                page_jobs, next_cursor = self._scrape_page(cursor)
            except Exception as exc:
                log.warning("Indeed strict remote search stopped: %s", exc)
                break
            jobs.extend(page_jobs)
            if (
                not next_cursor
                or next_cursor in cursors
                or len(self._strict.seen) == before
            ):
                break
            cursors.add(next_cursor)
            cursor = next_cursor
        return self._strict.finish(jobs)

    def _scrape_page(self, cursor: str | None) -> Tuple[list[JobPost], str | None]:
        """
        Scrapes a page of Indeed for jobs with scraper_input criteria
        :param cursor:
        :return: jobs found on page, next page cursor
        """
        jobs = []
        new_cursor = None
        filters = self._build_filters()
        search_val = (
            self.scraper_input.indeed_search_term
            if self.scraper_input.indeed_search_term
            else self.scraper_input.search_term
        )
        search_term = search_val.replace('"', '\\"') if search_val else ""
        location_clause = ""
        if self.scraper_input.location:
            radius_clause = (
                f", radius: {self.scraper_input.distance}, radiusUnit: KILOMETERS"
                if self.scraper_input.distance is not None
                else ""
            )
            location_clause = (
                f'location: {{where: "{self.scraper_input.location}"{radius_clause}}}'
            )

        query = job_search_query.format(
            what=(f'what: "{search_term}"' if search_term else ""),
            location=location_clause,
            dateOnIndeed=self.scraper_input.hours_old,
            cursor=f'cursor: "{cursor}"' if cursor else "",
            filters=filters,
        )
        payload = {
            "query": query,
        }
        api_headers_temp = api_headers.copy()
        api_headers_temp["indeed-co"] = self.api_country_code
        response = self.session.post(
            self.api_url,
            headers=api_headers_temp,
            json=payload,
            timeout=10,
            verify=False,
        )
        if not response.ok:
            log.info(
                f"responded with status code: {response.status_code} (submit GitHub issue if this appears to be a bug)"
            )
            return jobs, new_cursor
        data = response.json()
        if is_strict(self.scraper_input) and not (data.get("data") or {}).get(
            "jobSearch"
        ):
            log.warning("Indeed returned no usable search data: %s", data.get("errors"))
            return [], None
        jobs = data["data"]["jobSearch"]["results"]
        new_cursor = data["data"]["jobSearch"]["pageInfo"]["nextCursor"]

        job_list = []
        for job in jobs:
            strict = getattr(self, "_strict", None)
            if strict:
                if strict.done:
                    break
                raw = job.get("job") if isinstance(job, dict) else None
                if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
                    strict.reject("malformed_job")
                    continue
                identifier = raw.get("key")
                if not identifier or not strict.visit(identifier):
                    continue
                if not indeed_date_is_recent(
                    raw.get("dateOnIndeed"), self.scraper_input.hours_old
                ):
                    strict.reject("age_filter")
                    continue
                try:
                    processed_job = self._process_job(raw)
                    if (
                        processed_job
                        and self.scraper_input.job_type
                        and self.scraper_input.job_type
                        not in (processed_job.job_type or [])
                    ):
                        strict.reject("job_type_filter")
                        continue
                    if strict.accept(processed_job):
                        job_list.append(processed_job)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    strict.reject("malformed_job")
                    log.warning("Skipping malformed Indeed job: %s", exc)
                continue
            processed_job = self._process_job(job["job"])
            if processed_job:
                job_list.append(processed_job)

        return job_list, new_cursor

    def _build_filters(self):
        """
        Builds the filters dict for job type/is_remote. If hours_old is provided, composite filter for job_type/is_remote is not possible.
        IndeedApply: filters: { keyword: { field: "indeedApplyScope", keys: ["DESKTOP"] } }
        """
        filters_str = ""
        if is_strict(self.scraper_input):
            # Indeed accepts this filter on its own, but combining it with
            # attributes currently fails in job-retrieval-service. All remote,
            # age and job-type requirements are still enforced per job locally.
            if self.scraper_input.easy_apply:
                return (
                    'filters: {keyword: {field: "indeedApplyScope", keys: ["DESKTOP"]}}'
                )
            # The API rejects date filters nested inside a composite. Keep remote
            # server-side and enforce hours_old against dateOnIndeed locally.
            keywords = ['{keyword: {field: "attributes", keys: ["DSQF7"]}}']
            job_type_key = {
                JobType.FULL_TIME: "CF3CP",
                JobType.PART_TIME: "75GKK",
                JobType.CONTRACT: "NJXCK",
                JobType.INTERNSHIP: "VDTG7",
            }.get(self.scraper_input.job_type)
            if job_type_key:
                keywords.append(
                    '{keyword: {field: "attributes", keys: ["' + job_type_key + '"]}}'
                )
            return "filters: {composite: {filters: [" + ", ".join(keywords) + "]}}"
        if self.scraper_input.hours_old:
            filters_str = """
            filters: {{
                date: {{
                  field: "dateOnIndeed",
                  start: "{start}h"
                }}
            }}
            """.format(start=self.scraper_input.hours_old)
        elif self.scraper_input.easy_apply:
            filters_str = """
            filters: {
                keyword: {
                  field: "indeedApplyScope",
                  keys: ["DESKTOP"]
                }
            }
            """
        elif self.scraper_input.job_type or self.scraper_input.is_remote:
            job_type_key_mapping = {
                JobType.FULL_TIME: "CF3CP",
                JobType.PART_TIME: "75GKK",
                JobType.CONTRACT: "NJXCK",
                JobType.INTERNSHIP: "VDTG7",
            }

            keys = []
            if self.scraper_input.job_type:
                key = job_type_key_mapping.get(self.scraper_input.job_type)
                if key:
                    keys.append(key)

            if self.scraper_input.is_remote:
                keys.append("DSQF7")

            if keys:
                keys_str = '", "'.join(keys)
                filters_str = f"""
                filters: {{
                  composite: {{
                    filters: [{{
                      keyword: {{
                        field: "attributes",
                        keys: ["{keys_str}"]
                      }}
                    }}]
                  }}
                }}
                """
        return filters_str

    def _process_job(self, job: dict) -> JobPost | None:
        """
        Parses the job dict into JobPost model
        :param job: dict to parse
        :return: JobPost if it's a new job
        """
        job_url = f'{self.base_url}/viewjob?jk={job["key"]}'
        if job_url in self.seen_urls:
            return
        self.seen_urls.add(job_url)
        description = (
            (job.get("description") or {}).get("html") or ""
            if is_strict(self.scraper_input)
            else job["description"]["html"]
        )
        remote_metadata = RemoteMetadata()
        if is_strict(self.scraper_input):
            attributes = job.get("attributes") or []
            full_remote = any(attr.get("key") == "DSQF7" for attr in attributes)
            country = (job.get("location") or {}).get("countryCode")
            remote_metadata = RemoteMetadata(
                description_complete=bool(description),
                description_text=description,
                full_remote=full_remote,
                hybrid=any(
                    "hybrid" in attr.get("label", "").casefold() for attr in attributes
                ),
                onsite=any(
                    attr.get("label", "").casefold()
                    in {"on-site", "onsite", "in-person"}
                    for attr in attributes
                )
                and not full_remote,
                job_countries=[country] if isinstance(country, str) else [],
                evidence=["Indeed attributes: DSQF7 (Remote)"] if full_remote else [],
            )
        if self.scraper_input.description_format == DescriptionFormat.MARKDOWN:
            description = markdown_converter(description)

        job_type = get_job_type(job["attributes"])
        timestamp_seconds = job["datePublished"] / 1000
        date_posted = datetime.fromtimestamp(timestamp_seconds).strftime("%Y-%m-%d")
        employer = job["employer"].get("dossier") if job["employer"] else None
        employer_details = employer.get("employerDetails", {}) if employer else {}
        rel_url = job["employer"]["relativeCompanyPageUrl"] if job["employer"] else None
        return JobPost(
            id=f'in-{job["key"]}',
            title=job["title"],
            description=description,
            company_name=job["employer"].get("name") if job.get("employer") else None,
            company_url=(f"{self.base_url}{rel_url}" if job["employer"] else None),
            company_url_direct=(
                employer["links"]["corporateWebsite"] if employer else None
            ),
            location=Location(
                city=job.get("location", {}).get("city"),
                state=job.get("location", {}).get("admin1Code"),
                country=job.get("location", {}).get("countryCode"),
            ),
            job_type=job_type,
            compensation=get_compensation(job["compensation"]),
            date_posted=date_posted,
            job_url=job_url,
            job_url_direct=(
                job["recruit"].get("viewJobUrl") if job.get("recruit") else None
            ),
            emails=extract_emails_from_text(description) if description else None,
            is_remote=is_job_remote(job, description),
            remote_metadata=remote_metadata,
            company_addresses=(
                employer_details["addresses"][0]
                if employer_details.get("addresses")
                else None
            ),
            company_industry=(
                employer_details["industry"]
                .replace("Iv1", "")
                .replace("_", " ")
                .title()
                .strip()
                if employer_details.get("industry")
                else None
            ),
            company_num_employees=employer_details.get("employeesLocalizedLabel"),
            company_revenue=employer_details.get("revenueLocalizedLabel"),
            company_description=employer_details.get("briefDescription"),
            company_logo=(
                employer["images"].get("squareLogoUrl")
                if employer and employer.get("images")
                else None
            ),
        )
