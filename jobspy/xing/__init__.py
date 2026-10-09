from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from html import escape

from bs4 import BeautifulSoup
from requests.exceptions import RequestException, RetryError

from jobspy.model import (
    DescriptionFormat,
    JobPost,
    JobResponse,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import (
    create_logger,
    create_session,
    extract_emails_from_text,
    markdown_converter,
    plain_converter,
)
from jobspy.xing.constant import (
    COUNTRY_NAMES,
    EMPLOYMENT_FILTERS,
    JOBS_PER_PAGE,
    MAX_PAGES,
    REMOTE_FILTER,
    SEARCH_URL,
    headers,
)
from jobspy.xing.util import (
    canonical_job_url,
    extract_apollo_state,
    extract_job_posting,
    http_url,
    job_id,
    parse_compensation,
    parse_datetime,
    parse_easy_apply,
    parse_job_types,
    parse_location,
    parse_remote,
    resolve,
)

log = create_logger("Xing")


class Xing(Scraper):
    delay = 1

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(
            Site.XING, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent
        )
        self.session = create_session(
            proxies=proxies, ca_cert=ca_cert, is_tls=False, has_retry=True
        )
        self.session.headers.update(headers)
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self.scraper_input = None
        self._blocked = False
        self._has_requested = False

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        self._blocked = False
        self._has_requested = False
        if scraper_input.results_wanted <= 0:
            return JobResponse(jobs=[])

        now = datetime.now(timezone.utc)
        cutoff = (
            now - timedelta(hours=max(0, scraper_input.hours_old))
            if scraper_input.hours_old is not None
            else None
        )
        params = self._search_params(scraper_input, cutoff)
        offset = max(0, scraper_input.offset or 0)
        first_page, skip = divmod(offset, JOBS_PER_PAGE)
        jobs = []
        seen_ids = set()
        seen_pages = set()

        for page in range(first_page + 1, MAX_PAGES + 1):
            if self._blocked:
                break
            log.info("Fetching Xing search page %s", page)
            result = self._search_page({**params, "page": page})
            if result is None:
                break
            entries, total, state = result
            if not entries:
                break
            candidates = [
                resolve(resolve(entry, state).get("jobDetail"), state)
                for entry in entries
            ]
            signature = tuple(str(candidate.get("id", "")) for candidate in candidates)
            if signature in seen_pages:
                log.info("Xing repeated a result page; ending pagination")
                break
            seen_pages.add(signature)
            new_ids = False

            for index, candidate in enumerate(candidates):
                if self._blocked or len(jobs) >= scraper_input.results_wanted:
                    break
                if candidate.get("__typename") != "VisibleJob":
                    continue
                try:
                    url = canonical_job_url(candidate.get("url"))
                    identifier = job_id(candidate.get("id"), url or "")
                    if not url or not identifier or identifier in seen_ids:
                        continue
                    seen_ids.add(identifier)
                    new_ids = True
                    if page == first_page + 1 and index < skip:
                        continue
                    post = self._process_job(
                        candidate, state, url, identifier, now, cutoff
                    )
                    if post:
                        jobs.append(post)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    log.warning("Skipping malformed Xing job: %s", exc)

            if len(jobs) >= scraper_input.results_wanted:
                break
            if not new_ids or len(entries) < JOBS_PER_PAGE:
                break
            if total is not None and page * JOBS_PER_PAGE >= total:
                break
        return JobResponse(jobs=jobs)

    @staticmethod
    def _search_params(scraper_input: ScraperInput, cutoff: datetime | None) -> dict:
        params = {}
        if scraper_input.search_term:
            params["keywords"] = scraper_input.search_term
        location = scraper_input.location or COUNTRY_NAMES.get(scraper_input.country)
        if location:
            params["location"] = location
            if scraper_input.location and scraper_input.distance is not None:
                params["radius"] = scraper_input.distance
        if scraper_input.job_type in EMPLOYMENT_FILTERS:
            params["employmentType"] = EMPLOYMENT_FILTERS[scraper_input.job_type]
        if scraper_input.is_remote:
            params["remoteOption"] = REMOTE_FILTER
        if cutoff:
            params["since"] = cutoff.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
        return params

    def _get(self, url: str, **kwargs):
        if self._blocked:
            return None
        if self._has_requested:
            time.sleep(self.delay)
        self._has_requested = True
        try:
            response = self.session.get(
                url, timeout=self.scraper_input.request_timeout, **kwargs
            )
        except RequestException as exc:
            if isinstance(exc, RetryError):
                self._blocked = True
            log.warning("Xing request failed: %s", exc)
            return None
        if response.status_code in {403, 429}:
            self._blocked = True
        if response.status_code != 200:
            log.warning("Xing returned HTTP %s for %s", response.status_code, url)
        # Xing declares UTF-8 in HTML, but requests may default to ISO-8859-1.
        response.encoding = "utf-8"
        return response

    def _search_page(self, params: dict) -> tuple[list, int | None, dict] | None:
        response = self._get(SEARCH_URL, params=params)
        if response is None or response.status_code != 200:
            return None
        try:
            state = extract_apollo_state(BeautifulSoup(response.text, "html.parser"))
            root = resolve(state.get("ROOT_QUERY"), state)
            for key, value in root.items():
                if key.startswith("jobSearchByQuery("):
                    result = resolve(value, state)
                    entries = result.get("collection")
                    if not isinstance(entries, list):
                        raise ValueError("missing search result collection")
                    total = result.get("total")
                    total = total if isinstance(total, int) and total >= 0 else None
                    return entries, total, state
            raise ValueError("missing search data (possibly a login or challenge page)")
        except (ValueError, TypeError) as exc:
            log.warning("Could not parse Xing search: %s", exc)
            return None

    def _process_job(
        self,
        candidate: dict,
        search_state: dict,
        url: str,
        identifier: str,
        now: datetime,
        cutoff: datetime | None,
    ) -> JobPost | None:
        state = search_state
        posting = {}
        job = candidate
        response = self._get(url)
        if response is not None and response.status_code in {404, 410}:
            return None
        if response is not None and response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            posting = extract_job_posting(soup, identifier)
            try:
                detail_state = extract_apollo_state(soup)
                state = {**search_state, **detail_state}
                detail = resolve(
                    detail_state.get(f"VisibleJob:{candidate.get('id')}"), state
                )
                if not detail and not posting:
                    log.warning("No structured Xing detail data found for %s", url)
                job = {
                    **candidate,
                    **{
                        key: value for key, value in detail.items() if value is not None
                    },
                }
            except (ValueError, TypeError) as exc:
                log.warning("Could not parse Xing details for %s: %s", url, exc)

        expires = parse_datetime(posting.get("validThrough") or job.get("activeUntil"))
        if expires and expires < now:
            return None
        title = posting.get("title") or job.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        title = title.strip()
        posted = parse_datetime(posting.get("datePosted") or job.get("refreshedAt"))
        location = parse_location(job, posting, state, self.scraper_input.country)
        job_types = parse_job_types(job, posting, state, title)
        remote = parse_remote(job, posting)
        easy_apply = parse_easy_apply(job, posting, state)

        if cutoff and (posted is None or posted < cutoff):
            return None
        if self.scraper_input.country in COUNTRY_NAMES and (
            location is None or location.country != self.scraper_input.country
        ):
            return None
        if self.scraper_input.job_type and self.scraper_input.job_type not in (
            job_types or []
        ):
            return None
        if self.scraper_input.is_remote and remote is not True:
            return None
        if (
            self.scraper_input.easy_apply is not None
            and easy_apply != self.scraper_input.easy_apply
        ):
            return None

        description_data = resolve(job.get("description"), state)
        description = posting.get("description") or description_data.get("content")
        if (
            description
            and not posting.get("description")
            and description_data.get("__typename") == "TextDescription"
        ):
            description = escape(description)
        description = self._format_description(description)
        organization = resolve(posting.get("hiringOrganization"), state)
        company_info = resolve(job.get("companyInfo"), state)
        company = resolve(company_info.get("company"), state)
        application = resolve(job.get("application"), state)
        logo = organization.get("logo")
        if isinstance(logo, dict):
            logo = logo.get("url")
        company_url = http_url(organization.get("url") or company.get("url"))
        website = http_url(organization.get("sameAs"))

        return JobPost(
            id=f"xing-{identifier}",
            title=title,
            company_name=company_info.get("companyNameOverride")
            or organization.get("name")
            or company.get("name"),
            company_url=company_url,
            company_url_direct=website if website != company_url else None,
            company_logo=http_url(
                logo or resolve(company.get("logos"), state).get("x1")
            ),
            company_industry=posting.get("industry")
            or resolve(job.get("industry"), state).get("localizationValue"),
            job_level=resolve(job.get("careerLevel"), state).get("localizationValue"),
            location=location,
            job_url=url,
            job_url_direct=http_url(application.get("applyUrl")),
            date_posted=posted.date() if posted else None,
            job_type=job_types,
            is_remote=remote,
            compensation=parse_compensation(job, posting, state),
            description=description,
            emails=extract_emails_from_text(description),
        )

    def _format_description(self, description: str | None) -> str | None:
        if not isinstance(description, str) or not description.strip():
            return None
        soup = BeautifulSoup(description, "html.parser")
        for tag in soup.find_all(["script", "style", "noscript"]):
            tag.decompose()
        for tag in soup.find_all(True):
            tag.attrs = {
                key: value
                for key, value in tag.attrs.items()
                if key in {"href", "src", "alt", "title"}
            }
        html = str(soup)
        if self.scraper_input.description_format == DescriptionFormat.HTML:
            return html
        if self.scraper_input.description_format == DescriptionFormat.PLAIN:
            return plain_converter(html)
        return markdown_converter(html)


__all__ = ["Xing"]
