# util.py
from __future__ import annotations

import re
from datetime import datetime, date, timedelta
from typing import Optional, List, Any
from bs4 import BeautifulSoup, Tag

from jobspy.model import Location, Country, JobType, Compensation, CompensationInterval


def parse_location(location_text: Optional[str], default_country: Country = Country.GERMANY) -> Location:
    """
    Parses a location string from StepStone.
    """
    if not location_text:
        return Location(country=default_country)

    # Clean text
    clean_loc = location_text.strip()
    parts = [p.strip() for p in clean_loc.split(",") if p.strip()]

    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None

    return Location(
        city=city,
        state=state,
        country=default_country,
    )


def parse_relative_date(date_text: Optional[str]) -> Optional[date]:
    """
    Parses German relative date strings like 'vor 2 Tagen', 'vor 5 Stunden', 'vor 1 Monat', 'heute', 'gestern'.
    """
    if not date_text:
        return None

    text = date_text.strip().lower()
    today = datetime.now().date()

    if "heute" in text or "gerade" in text:
        return today
    if "gestern" in text:
        return today - timedelta(days=1)

    # Match 'vor X Tagen/Stunden/Wochen/Monaten'
    match_days = re.search(r"vor\s+(\d+)\s+tag", text)
    if match_days:
        return today - timedelta(days=int(match_days.group(1)))

    match_hours = re.search(r"vor\s+(\d+)\s+stunde", text)
    if match_hours:
        return today

    match_weeks = re.search(r"vor\s+(\d+)\s+woche", text)
    if match_weeks:
        return today - timedelta(days=int(match_weeks.group(1)) * 7)

    match_months = re.search(r"vor\s+(\d+)\s+monat", text)
    if match_months:
        return today - timedelta(days=int(match_months.group(1)) * 30)

    # Try ISO or standard German date format (dd.mm.yyyy)
    match_de_date = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match_de_date:
        try:
            return datetime.strptime(match_de_date.group(0), "%d.%m.%Y").date()
        except ValueError:
            pass

    return None


def parse_compensation(salary_text: Optional[str]) -> Optional[Compensation]:
    """
    Extracts salary range if StepStone displays it (e.g. '45.000 € - 65.000 € pro Jahr').
    """
    if not salary_text:
        return None

    # Search for € amounts
    matches = re.findall(r"(\d+(?:[\.,]\d+)?)\s*(?:€|EUR|k)", salary_text, re.IGNORECASE)
    if not matches:
        return None

    try:
        def clean_num(val_str: str) -> float:
            val_str = val_str.replace(".", "").replace(",", ".")
            return float(val_str)

        amounts = [clean_num(m) for m in matches]
        if not amounts:
            return None

        min_amt = min(amounts)
        max_amt = max(amounts) if len(amounts) > 1 else min_amt

        interval = CompensationInterval.YEARLY
        if "monat" in salary_text.lower():
            interval = CompensationInterval.MONTHLY
        elif "stunde" in salary_text.lower():
            interval = CompensationInterval.HOURLY

        return Compensation(
            interval=interval,
            min_amount=min_amt,
            max_amount=max_amt,
            currency="EUR",
        )
    except Exception:
        return None


def is_job_remote(title: str, location_text: str = "", card_text: str = "") -> bool:
    """
    Detects if a job listing offers remote or home office.
    """
    remote_keywords = [
        "homeoffice",
        "home office",
        "home-office",
        "remote",
        "mobiles arbeiten",
        "telearbeit",
        "wfh",
    ]
    full_text = f"{title} {location_text} {card_text}".lower()
    return any(kw in full_text for kw in remote_keywords)
