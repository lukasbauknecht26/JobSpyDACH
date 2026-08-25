from datetime import datetime, timedelta
import re

from jobspy.model import JobPost, CompensationInterval, Compensation, Location, JobType
from jobspy.util import create_logger

from bs4 import BeautifulSoup

log = create_logger("Google")

def parse_google_jobs_html(html_text: str) -> list[JobPost]:
    """Extracts job records directly from Google's modern search HTML DOM.

    Parses job cards (div.EimVGf) and their embedded <template> structures
    containing job titles, companies, locations, dates, descriptions,
    compensation, and direct apply links.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    jobs: list[JobPost] = []
    seen_urls: set[str] = set()

    cards = soup.find_all("div", class_="EimVGf")
    if not cards:
        cards = soup.find_all("div", attrs={"data-share-url": True})

    for card in cards:
        template = card.find("template")
        template_soup = None
        if template:
            try:
                template_soup = BeautifulSoup(template.decode_contents(), "html.parser")
            except Exception:
                template_soup = None

        # 1. Title
        title_el = None
        if template_soup:
            title_el = template_soup.find("div", class_="IFnjPb") or template_soup.find(
                "div", class_="tNxQIb"
            )
        if not title_el:
            title_el = card.find("div", class_="tNxQIb") or card.find(
                "div", class_="IFnjPb"
            )
        if not title_el:
            continue
        title = title_el.get_text().strip()

        # 2. Company
        company = None
        company_el = card.find("div", class_="a3jPc") or card.find(
            "div", class_="knLYHc"
        )
        if company_el:
            company = company_el.get_text().strip()
        elif template_soup:
            company_el = template_soup.find(
                "div", class_="knLYHc"
            ) or template_soup.find("div", class_="a3jPc")
            if company_el:
                company = company_el.get_text().strip()
            else:
                aw_el = template_soup.find("div", class_="aW97bd")
                if aw_el:
                    spans = aw_el.find_all("span")
                    if spans:
                        company = spans[0].get_text().strip()

        # 3. Location
        location_raw = None
        loc_el = card.find("div", class_="FqK3wc")
        if loc_el:
            location_raw = loc_el.get_text().strip()
        elif template_soup and template_soup.find("div", class_="aW97bd"):
            aw_el = template_soup.find("div", class_="aW97bd")
            spans = aw_el.find_all("span")
            if len(spans) > 1:
                location_raw = spans[1].get_text().strip()

        city = state = country = None
        if location_raw:
            cleaned_loc = re.split(r"[•·]|\büber\b|\bvia\b", location_raw)[0].strip()
            if cleaned_loc:
                if "," in cleaned_loc:
                    parts = [p.strip() for p in cleaned_loc.split(",")]
                    city = parts[0]
                    state = parts[1] if len(parts) > 1 else None
                    country = parts[2] if len(parts) > 2 else None
                else:
                    city = cleaned_loc

        # 4. Job URL / Direct apply URL
        job_url = None
        job_url_direct = None
        apply_links = (
            template_soup.find_all("a", class_="brKmxb") if template_soup else []
        ) or card.find_all("a", class_="brKmxb")
        if apply_links:
            for link in apply_links:
                href = link.get("href")
                if href and href.startswith("http"):
                    job_url_direct = href
                    job_url = href
                    break

        if not job_url and card.get("data-share-url"):
            job_url = card["data-share-url"]

        if job_url in seen_urls:
            continue
        if job_url:
            seen_urls.add(job_url)

        # 5. Job ID / Docid
        docid = None
        docid_el = (
            template_soup.find(attrs={"data-encoded-docid": True})
            if template_soup
            else None
        ) or card.find(attrs={"data-encoded-docid": True})
        if docid_el and docid_el.get("data-encoded-docid"):
            docid = docid_el["data-encoded-docid"]
        elif card.get("id"):
            docid = card["id"]
        job_id = f"go-{docid}" if docid else None

        # 6. Description
        desc_el = (
            template_soup.find("span", class_="INXbXb") if template_soup else None
        ) or card.find("span", class_="INXbXb")
        description = desc_el.get_text(separator="\n").strip() if desc_el else None

        # 7. Date posted
        date_posted = None
        date_text = ""
        for span in card.find_all("span", class_="Yf9oye"):
            txt = span.get_text().strip()
            if any(
                w in txt.lower()
                for w in [
                    "tag",
                    "day",
                    "stund",
                    "hour",
                    "woche",
                    "week",
                    "monat",
                    "month",
                ]
            ):
                date_text = txt
                break
        if date_text:
            match_days = re.search(r"(\d+)\s*(?:Tag|day|d)", date_text, re.I)
            match_hours = re.search(r"(\d+)\s*(?:Stunde|hour|h)", date_text, re.I)
            match_weeks = re.search(r"(\d+)\s*(?:Woche|week|w)", date_text, re.I)
            match_months = re.search(r"(\d+)\s*(?:Monat|month|m)", date_text, re.I)
            if match_days:
                days_ago = int(match_days.group(1))
                date_posted = (datetime.now() - timedelta(days=days_ago)).date()
            elif match_hours:
                date_posted = datetime.now().date()
            elif match_weeks:
                weeks_ago = int(match_weeks.group(1))
                date_posted = (datetime.now() - timedelta(days=weeks_ago * 7)).date()
            elif match_months:
                months_ago = int(match_months.group(1))
                date_posted = (datetime.now() - timedelta(days=months_ago * 30)).date()

        # 8. Job Type & Compensation
        job_type = None
        if description:
            job_type = extract_job_type(description)
        if not job_type:
            for badge in card.find_all("span", class_="Yf9oye"):
                badge_txt = badge.get_text().strip()
                jt = extract_job_type(badge_txt)
                if jt:
                    job_type = jt
                    break

        compensation = None
        for span in card.find_all("span", class_="Yf9oye"):
            aria_label = span.get("aria-label", "")
            txt = span.get_text().strip()
            if (
                "gehalt" in aria_label.lower()
                or "salary" in aria_label.lower()
                or "€" in txt
                or "$" in txt
            ):
                sal_text = aria_label or txt
                interval = (
                    CompensationInterval.YEARLY
                    if "jahr" in sal_text.lower() or "year" in sal_text.lower()
                    else (
                        CompensationInterval.HOURLY
                        if "stunde" in sal_text.lower() or "hour" in sal_text.lower()
                        else CompensationInterval.MONTHLY
                    )
                )
                nums = re.findall(r"(\d+(?:[\.,]\d+)?)", sal_text)
                if nums:

                    def parse_num(n):
                        return float(n.replace(".", "").replace(",", "."))

                    try:
                        if len(nums) >= 2:
                            min_sal = parse_num(nums[0])
                            max_sal = parse_num(nums[1])
                        else:
                            min_sal = max_sal = parse_num(nums[0])
                        currency = (
                            "EUR"
                            if "€" in sal_text
                            else ("USD" if "$" in sal_text else "USD")
                        )
                        compensation = Compensation(
                            interval=interval,
                            min_amount=min_sal,
                            max_amount=max_sal,
                            currency=currency,
                        )
                    except Exception:
                        pass
                break

        is_remote = False
        if description:
            is_remote = any(
                w in description.lower()
                for w in ["remote", "wfh", "homeoffice", "home-office", "home office"]
            )

        emails = extract_emails_from_text(description) if description else None

        job_post = JobPost(
            id=job_id,
            title=title,
            company_name=company,
            location=Location(city=city, state=state, country=country),
            job_url=job_url or "",
            job_url_direct=job_url_direct,
            date_posted=date_posted,
            is_remote=is_remote,
            description=description,
            job_type=job_type,
            compensation=compensation,
            emails=emails,
        )
        jobs.append(job_post)
    return jobs

def extract_emails_from_text(text: str) -> list[str] | None:
    if not text:
        return None
    email_regex = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    return email_regex.findall(text)

def extract_job_type(description: str):
    if not description:
        return []

    keywords = {
        JobType.FULL_TIME: r"full\s?time",
        JobType.PART_TIME: r"part\s?time",
        JobType.INTERNSHIP: r"internship",
        JobType.CONTRACT: r"contract",
    }

    listing_types = []
    for key, pattern in keywords.items():
        if re.search(pattern, description, re.IGNORECASE):
            listing_types.append(key)

    return listing_types if listing_types else None

def find_job_info(jobs_data: list | dict) -> list | None:
    """Iterates through the JSON data to find the job listings"""
    if isinstance(jobs_data, dict):
        for key, value in jobs_data.items():
            if key == "520084652" and isinstance(value, list):
                return value
            else:
                result = find_job_info(value)
                if result:
                    return result
    elif isinstance(jobs_data, list):
        for item in jobs_data:
            result = find_job_info(item)
            if result:
                return result
    return None


def find_job_info_initial_page(html_text: str):
    pattern = f'520084652":(' + r"\[.*?\]\s*])\s*}\s*]\s*]\s*]\s*]\s*]"
    results = []
    matches = re.finditer(pattern, html_text)

    import json

    for match in matches:
        try:
            parsed_data = json.loads(match.group(1))
            results.append(parsed_data)

        except json.JSONDecodeError as e:
            log.error(f"Failed to parse match: {str(e)}")
            results.append({"raw_match": match.group(0), "error": str(e)})
    return results
