from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from jobspy.model import (
    Compensation,
    CompensationInterval,
    Country,
    JobType,
    Location,
    SalarySource,
)
from jobspy.util import get_enum_from_job_type
from jobspy.xing.constant import (
    BASE_URL,
    COUNTRY_CODES,
    COUNTRY_NAMES,
    EMPLOYMENT_TYPES,
)


def extract_apollo_state(soup: BeautifulSoup) -> dict:
    """Decode only the JSON cache, not the surrounding JavaScript configuration."""
    for script in soup.find_all("script"):
        content = script.string or ""
        match = re.search(r'"APOLLO_STATE"\s*:\s*', content)
        if match:
            state, _ = json.JSONDecoder().raw_decode(content, match.end())
            if not isinstance(state, dict):
                raise ValueError("Xing APOLLO_STATE is not an object")
            return state
    return {}


def resolve(value, state: dict) -> dict:
    seen = set()
    while isinstance(value, dict) and "__ref" in value:
        reference = value["__ref"]
        if not isinstance(reference, str) or reference in seen:
            return {}
        seen.add(reference)
        value = state.get(reference)
    return value if isinstance(value, dict) else {}


def job_id(value, url: str = "") -> str | None:
    match = re.fullmatch(r"(\d+)(?:\.[\w]+)?", str(value or ""))
    if not match:
        match = re.search(r"-(\d+)/?$", urlsplit(url).path)
    return match.group(1) if match else None


def canonical_job_url(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    parts = urlsplit(urljoin(BASE_URL, url))
    if parts.hostname not in {"xing.com", "www.xing.com", "xing.de", "www.xing.de"}:
        return None
    if parts.scheme not in {"http", "https"} or not parts.path.startswith("/jobs/"):
        return None
    if not job_id(None, parts.path):
        return None
    return urlunsplit(("https", "www.xing.com", parts.path.rstrip("/"), "", ""))


def http_url(value) -> str | None:
    if isinstance(value, str) and urlsplit(value).scheme in {"http", "https"}:
        return value if urlsplit(value).hostname else None
    return None


def extract_job_posting(soup: BeautifulSoup, expected_id: str) -> dict:
    def postings(value):
        if isinstance(value, list):
            for child in value:
                yield from postings(child)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if (
                types == "JobPosting"
                or isinstance(types, list)
                and "JobPosting" in types
            ):
                yield value
            yield from postings(value.get("@graph"))

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for posting in postings(data):
            url = posting.get("url")
            if not url or isinstance(url, str) and job_id(None, url) == expected_id:
                return posting
    return {}


def parse_datetime(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def parse_country(value) -> Country | str | None:
    if isinstance(value, dict):
        value = value.get("countryCode") or value.get("name")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.upper() in COUNTRY_CODES:
        return COUNTRY_CODES[value.upper()]
    for country, name in COUNTRY_NAMES.items():
        if value.casefold() == name.casefold():
            return country
    try:
        return Country.from_string(value)
    except ValueError:
        return value


def parse_location(
    job: dict, posting: dict, state: dict, country=None
) -> Location | None:
    locations = []
    values = posting.get("jobLocation") or []
    if isinstance(values, dict):
        values = [values]
    for value in values:
        address = resolve(resolve(value, state).get("address"), state)
        if address:
            locations.append(
                Location(
                    city=address.get("addressLocality"),
                    state=address.get("addressRegion"),
                    country=parse_country(address.get("addressCountry")),
                )
            )
    for value in job.get("locations") or [job.get("location")]:
        value = resolve(value, state)
        if value:
            raw_country = value.get("country")
            if isinstance(raw_country, dict):
                raw_country = resolve(raw_country, state)
            locations.append(
                Location(
                    city=value.get("city"),
                    state=value.get("region"),
                    country=parse_country(raw_country),
                )
            )
    if country in COUNTRY_NAMES:
        for location in locations:
            if location.country == country:
                return location
    return locations[0] if locations else None


def parse_job_types(
    job: dict, posting: dict, state: dict, title: str
) -> list[JobType] | None:
    if re.search(
        r"\b(ausbildung|auszubildende\w*|azubi|duales? studium|apprentice\w*)\b",
        title,
        re.I,
    ):
        return [JobType.APPRENTICESHIP]
    if re.search(
        r"\b(werkstudent\w*|praktikant\w*|praktikum|internship)\b", title, re.I
    ):
        return [JobType.INTERNSHIP]
    employment = resolve(job.get("employmentType"), state)
    values = posting.get("employmentType") or []
    if not isinstance(values, list):
        values = [values]
    values = values + [employment.get("id"), employment.get("localizationValue")]
    aliases = {
        "studierende": JobType.INTERNSHIP,
        "selbstst\u00e4ndig": JobType.CONTRACT,
        "aushilfe": JobType.TEMPORARY,
        "saisonarbeit": JobType.SUMMER,
        "ehrenamt": JobType.VOLUNTEER,
    }
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        key = value.split(".")[0].upper()
        kind = EMPLOYMENT_TYPES.get(key) or aliases.get(value.casefold())
        kind = kind or get_enum_from_job_type(value.replace("_", " "))
        if kind and kind not in result:
            result.append(kind)
    return result or None


def parse_remote(job: dict, posting: dict) -> bool | None:
    options = job.get("remoteOptions") or []
    if any(value in options for value in ("FULL_REMOTE", "PARTLY_REMOTE")):
        return True
    if "NON_REMOTE" in options:
        return False
    if posting.get("jobLocationType") == "TELECOMMUTE":
        return True
    return None


def parse_easy_apply(job: dict, posting: dict, state: dict) -> bool | None:
    application = resolve(job.get("application"), state)
    kind = application.get("__typename", "")
    if kind.startswith("JobXing") and "Application" in kind:
        return True
    if kind == "UrlApplication":
        return False
    value = posting.get("directApply")
    return value if isinstance(value, bool) else None


def parse_compensation(job: dict, posting: dict, state: dict) -> Compensation | None:
    def number(value):
        try:
            result = float(value)
            return (
                result
                if not isinstance(value, bool) and math.isfinite(result) and result > 0
                else None
            )
        except (TypeError, ValueError):
            return None

    def compensation(low, high, currency, unit, source=SalarySource.DIRECT_DATA):
        low, high = number(low), number(high)
        if (
            low is None
            and high is None
            or low is not None
            and high is not None
            and high < low
        ):
            return None
        intervals = {
            "YEAR": CompensationInterval.YEARLY,
            "MONTH": CompensationInterval.MONTHLY,
            "WEEK": CompensationInterval.WEEKLY,
            "DAY": CompensationInterval.DAILY,
            "HOUR": CompensationInterval.HOURLY,
        }
        return Compensation(
            min_amount=low,
            max_amount=high,
            currency=currency or None,
            interval=intervals.get(str(unit).upper()),
            salary_source=source,
        )

    base = resolve(posting.get("baseSalary"), state)
    value = base.get("value")
    if isinstance(value, dict):
        result = compensation(
            value.get("minValue", value.get("value")),
            value.get("maxValue", value.get("value")),
            base.get("currency"),
            value.get("unitText"),
        )
        if result:
            return result

    salary = resolve(job.get("salary"), state)
    kind = salary.get("__typename")
    if kind not in {"Salary", "SalaryRange", "SalaryEstimate"}:
        return None
    # Xing's typed salary amounts and estimates are annual amounts.
    return compensation(
        salary.get("minimum", salary.get("amount")),
        salary.get("maximum", salary.get("amount")),
        salary.get("currency"),
        "YEAR",
        (
            SalarySource.ESTIMATED_DATA
            if kind == "SalaryEstimate"
            else SalarySource.DIRECT_DATA
        ),
    )
