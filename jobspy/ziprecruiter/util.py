from __future__ import annotations

import logging

from jobspy.model import JobType

log = logging.getLogger("JobSpy:ZipRecruiter")


def fetch_geo_coordinates(
        session, location: str, base_url: str = "https://www.ziprecruiter.de"
) -> tuple[float | None, float | None, str | None]:
    """
    Fetches latitude and longitude for a given location using ZipRecruiter's geo_locate endpoints.
    """
    if not location:
        return None, None, None

    try:
        suggest_url = f"{base_url}/geo_locate/suggest_locations"
        res = session.get(suggest_url, params={"keyword": location})
        if res.status_code == 200:
            suggestions = res.json()
            if suggestions and isinstance(suggestions, list) and len(suggestions) > 0:
                first = suggestions[0]
                place_id = first.get("place_id")
                label = first.get("label", location)
                if place_id:
                    geocode_url = f"{base_url}/geo_locate/geocode_place"
                    geo_res = session.get(geocode_url, params={"place_id": place_id})
                    if geo_res.status_code == 200:
                        geo_data = geo_res.json()
                        lat = geo_data.get("latitude")
                        lng = geo_data.get("longitude")
                        return lat, lng, label
    except Exception as e:
        log.debug(f"Could not fetch geo coordinates for '{location}': {e}")

    return None, None, location


def add_params(
        scraper_input,
        page: int = 1,
        lat: float | None = None,
        long: float | None = None,
) -> dict[str, str | int]:
    """
    Constructs search query parameters for ziprecruiter.de /jobs/search.
    """
    params: dict[str, str | int] = {
        "q": scraper_input.search_term,
        "l": scraper_input.location,
    }
    if scraper_input.distance:
        params["d"] = scraper_input.distance

    if lat is not None and long is not None:
        params["lat"] = lat
        params["long"] = long

    if page and page > 1:
        params["page"] = page

    if scraper_input.hours_old:
        params["sort"] = "published_at"

    return {k: v for k, v in params.items() if v is not None}


def get_job_type_enum(job_type_str: str) -> list[JobType] | None:
    if not job_type_str:
        return None
    normalized = job_type_str.lower().strip()
    for job_type in JobType:
        if any(val in normalized for val in job_type.value):
            return [job_type]
    return None
