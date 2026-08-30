# util.py
from __future__ import annotations

import base64
from datetime import datetime, date
from typing import Optional, List, Dict, Any

from jobspy.model import Location, Country, JobType


def encode_refnr(refnr: str) -> str:
    """Encodes a reference number to base64 for detail API lookup."""
    return base64.b64encode(refnr.encode("utf-8")).decode("utf-8")


def parse_location(locations: Optional[List[Dict[str, Any]]]) -> Location:
    """
    Parses the 'stellenlokationen' list from the Arbeitsagentur v6 API into a Location object.
    """
    if not locations or not isinstance(locations, list):
        return Location(country=Country.GERMANY)

    primary_loc = locations[0]
    adresse = primary_loc.get("adresse", {}) if isinstance(primary_loc, dict) else {}

    city = adresse.get("ort")
    plz = adresse.get("plz")
    region = adresse.get("region")
    country_str = adresse.get("land", "DEUTSCHLAND")

    city_display = f"{plz} {city}".strip() if plz and city else (city or plz)

    country_enum = Country.GERMANY
    if country_str:
        country_lower = country_str.lower()
        if "österreich" in country_lower or "austria" in country_lower:
            country_enum = Country.AUSTRIA
        elif "schweiz" in country_lower or "switzerland" in country_lower:
            country_enum = Country.SWITZERLAND
        elif "deutschland" in country_lower or "germany" in country_lower:
            country_enum = Country.GERMANY

    return Location(
        city=city_display,
        state=region,
        country=country_enum,
    )


def parse_date(date_str: Optional[str]) -> Optional[date]:
    """
    Parses an ISO date string (e.g. '2026-08-25' or '2026-08-25T00:00:00') to a date object.
    """
    if not date_str:
        return None

    try:
        clean_date = date_str.split("T")[0]
        return datetime.strptime(clean_date, "%Y-%m-%d").date()
    except (ValueError, Exception):
        return None


def parse_job_type_v6(item: Dict[str, Any]) -> Optional[List[JobType]]:
    """
    Maps Arbeitsagentur v6 boolean flags and fields to JobType enums.
    """
    types = set()
    if item.get("arbeitszeitVollzeit"):
        types.add(JobType.FULL_TIME)
    if item.get("arbeitszeitTeilzeit"):
        types.add(JobType.PART_TIME)

    art = (item.get("stellenangebotsart") or "").upper()
    if "AUSBILDUNG" in art or "PRAKTIKUM" in art:
        types.add(JobType.INTERNSHIP)
    elif "BEFRISTET" in (item.get("vertragsdauer") or "").upper():
        types.add(JobType.TEMPORARY)

    return list(types) if types else None


def is_job_remote(item: Dict[str, Any]) -> bool:
    """
    Detects if a job has remote / home office options from v6 fields.
    """
    if item.get("homeofficemoeglich"):
        return True

    title = item.get("stellenangebotsTitel") or ""
    remote_keywords = [
        "remote",
        "homeoffice",
        "home office",
        "home-office",
        "telearbeit",
        "mobiles arbeiten",
        "work from home",
        "wfh",
        "ortsunabhängig",
    ]

    return any(kw in title.lower() for kw in remote_keywords)
