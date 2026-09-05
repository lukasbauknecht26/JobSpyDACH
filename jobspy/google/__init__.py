from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import re
import sys
from typing import Tuple

from playwright.sync_api import sync_playwright

from jobspy.google.constant import async_param, headers_initial, headers_jobs
from jobspy.google.util import (
    find_job_info,
    find_job_info_initial_page,
    log,
    parse_google_jobs_html,
)
from jobspy.model import (
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import create_session, extract_emails_from_text, extract_job_type


def _get_platform_user_agent() -> str:
    if sys.platform.startswith("linux"):
        return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    elif sys.platform == "darwin":
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class Google(Scraper):
    def __init__(
            self,
            proxies: list[str] | str | None = None,
            ca_cert: str | None = None,
            user_agent: str | None = None,
    ):
        """
        Initializes Google Scraper with the Goodle jobs search url
        """
        site = Site(Site.GOOGLE)
        super().__init__(site, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)

        self.country = None
        self.session = None
        self.scraper_input = None
        self.jobs_per_page = 10
        self.seen_urls = set()
        self.url = "https://www.google.com/search"
        self.jobs_url = "https://www.google.com/async/callback:550"

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Google for jobs with scraper_input criteria.
        :param scraper_input: Information about job search criteria.
        :return: JobResponse containing a list of jobs.
        """
        self.scraper_input = scraper_input
        self.scraper_input.results_wanted = min(900, scraper_input.results_wanted)

        if self.scraper_input.google_use_playwright:
            return self._scrape_playwright(self.scraper_input)
        # ToDo: Pagination funktioniert vermutlich nicht?

        self.session = create_session(
            proxies=self.proxies, ca_cert=self.ca_cert, is_tls=False, has_retry=True
        )
        forward_cursor, job_list = self._get_initial_cursor_and_jobs()
        if forward_cursor is None:
            log.warning(
                "initial cursor not found, try changing your query or there was at most 10 results"
            )
            return JobResponse(jobs=job_list)

        page = 1

        while (
                len(self.seen_urls) < scraper_input.results_wanted + scraper_input.offset
                and forward_cursor
        ):
            log.info(
                f"search page: {page} / {math.ceil(scraper_input.results_wanted / self.jobs_per_page)}"
            )
            try:
                jobs, forward_cursor = self._get_jobs_next_page(forward_cursor)
            except Exception as e:
                log.error(f"failed to get jobs on page: {page}, {e}")
                break
            if not jobs:
                log.info(f"found no jobs on page: {page}")
                break
            job_list += jobs
            page += 1
        return JobResponse(
            jobs=job_list[
                scraper_input.offset: scraper_input.offset
                                      + scraper_input.results_wanted
            ]
        )

    def _scrape_playwright(self, scraper_input: ScraperInput) -> JobResponse:
        google_url = "https://www.google.com/search"
        google_url = (
                google_url
                + "?q="
                + scraper_input.google_search_term.replace(" ", "+")
                + "&udm=8&hl=de"
        )

        chosen_ua = self.user_agent or _get_platform_user_agent()

        with sync_playwright() as p:
            # 1. Bot-Erkennung umgehen & Docker-taugliche Flags
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=chosen_ua,
                locale="de-DE",
                viewport={"width": 1920, "height": 1080},
            )

            # 2. Google Consent-Cookie vorab setzen (verhindert Redirects / Cookie-Modals)
            context.add_cookies(
                [
                    {
                        "name": "SOCS",
                        "value": "CAESHAgBEhJnd3NfMjAyNDA5MDQtMF9SQzIaAmRlIAEaBgiA_L22Bg",
                        "domain": ".google.com",
                        "path": "/",
                    }
                ]
            )

            # 3. Stealth-Maskierung (webdriver, languages, plugins)
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['de-DE', 'de', 'en-US', 'en']
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
            """)

            page = context.new_page()
            page.goto(google_url, wait_until="domcontentloaded")

            # 4. Cookie-Banner wegklicken, falls dennoch eines angezeigt wird
            try:
                reject_btn = page.locator(
                    'button:has-text("Alle ablehnen"), button:has-text("Reject all"), button:has-text("Alles ablehnen"), [aria-label="Alle ablehnen"]'
                ).first
                reject_btn.wait_for(state="visible", timeout=3000)
                reject_btn.click()
            except Exception:
                pass

            # 5. Warten bis die Job-Karten geladen sind (mit Docker-tauglichem Timeout)
            try:
                page.wait_for_selector("div.EimVGf", timeout=20000)
            except Exception as e:
                log.error(f"Fehler bei URL: {page.url}")
                log.error(f"Seitentitel: {page.title()}")
                page.screenshot(path="google_debug.png", full_page=True)
                browser.close()
                raise e

            html = page.content()

            # 6. JobSpy-HTML-Parser ausführen
            jobs = parse_google_jobs_html(html)
            log.info(f"Gefundene Google Jobs über Browser: {len(jobs)}")
            browser.close()
            return JobResponse(jobs=jobs)

    def _get_initial_cursor_and_jobs(self) -> Tuple[str, list[JobPost]]:
        """Gets initial cursor and jobs to paginate through job listings"""
        query = f"{self.scraper_input.search_term} jobs"

        def get_time_range(hours_old):
            if hours_old <= 24:
                return "since yesterday"
            elif hours_old <= 72:
                return "in the last 3 days"
            elif hours_old <= 168:
                return "in the last week"
            else:
                return "in the last month"

        job_type_mapping = {
            JobType.FULL_TIME: "Full time",
            JobType.PART_TIME: "Part time",
            JobType.INTERNSHIP: "Internship",
            JobType.CONTRACT: "Contract",
        }

        if self.scraper_input.job_type in job_type_mapping:
            query += f" {job_type_mapping[self.scraper_input.job_type]}"

        if self.scraper_input.location:
            query += f" near {self.scraper_input.location}"

        if self.scraper_input.hours_old:
            time_filter = get_time_range(self.scraper_input.hours_old)
            query += f" {time_filter}"

        if self.scraper_input.is_remote:
            query += " remote"

        if self.scraper_input.google_search_term:
            query = self.scraper_input.google_search_term

        params = {"q": query, "udm": "8"}
        response = self.session.get(self.url, headers=headers_initial, params=params)

        pattern_fc = r'<div jsname="Yust4d"[^>]+data-async-fc="([^"]+)"'
        match_fc = re.search(pattern_fc, response.text)
        data_async_fc = match_fc.group(1) if match_fc else None
        jobs_raw = find_job_info_initial_page(response.text)
        jobs = []
        for job_raw in jobs_raw:
            job_post = self._parse_job(job_raw)
            if job_post:
                jobs.append(job_post)
        return data_async_fc, jobs

    def _get_jobs_next_page(self, forward_cursor: str) -> Tuple[list[JobPost], str]:
        params = {"fc": [forward_cursor], "fcv": ["3"], "async": [async_param]}
        response = self.session.get(self.jobs_url, headers=headers_jobs, params=params)
        return self._parse_jobs(response.text)

    def _parse_jobs(self, job_data: str) -> Tuple[list[JobPost], str]:
        """
        Parses jobs on a page with next page cursor
        """
        start_idx = job_data.find("[[[")
        end_idx = job_data.rindex("]]]") + 3
        s = job_data[start_idx:end_idx]
        parsed = json.loads(s)[0]

        pattern_fc = r'data-async-fc="([^"]+)"'
        match_fc = re.search(pattern_fc, job_data)
        data_async_fc = match_fc.group(1) if match_fc else None
        jobs_on_page = []
        for array in parsed:
            _, job_data = array
            if not job_data.startswith("[[["):
                continue
            job_d = json.loads(job_data)

            job_info = find_job_info(job_d)
            job_post = self._parse_job(job_info)
            if job_post:
                jobs_on_page.append(job_post)
        return jobs_on_page, data_async_fc

    def _parse_job(self, job_info: list):
        job_url = job_info[3][0][0] if job_info[3] and job_info[3][0] else None
        if job_url in self.seen_urls:
            return
        self.seen_urls.add(job_url)

        title = job_info[0]
        company_name = job_info[1]
        location = city = job_info[2]
        state = country = date_posted = None
        if location and "," in location:
            city, state, *country = [*map(lambda x: x.strip(), location.split(","))]

        days_ago_str = job_info[12]
        if type(days_ago_str) == str:
            match = re.search(r"\d+", days_ago_str)
            days_ago = int(match.group()) if match else None
            date_posted = (datetime.now() - timedelta(days=days_ago)).date()

        description = job_info[19]

        job_post = JobPost(
            id=f"go-{job_info[28]}",
            title=title,
            company_name=company_name,
            location=Location(
                city=city, state=state, country=country[0] if country else None
            ),
            job_url=job_url,
            date_posted=date_posted,
            is_remote="remote" in description.lower() or "wfh" in description.lower(),
            description=description,
            emails=extract_emails_from_text(description),
            job_type=extract_job_type(description),
        )
        return job_post
