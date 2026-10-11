"""Conservative, offline normalization of DACH job locations."""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import gzip
from importlib import resources
import json
import re
import unicodedata
import zlib

from jobspy.model import Country, Location
from jobspy.util import create_logger

log = create_logger("Location")
_COUNTRIES = {"DE": Country.GERMANY, "AT": Country.AUSTRIA, "CH": Country.SWITZERLAND}
_COUNTRY_ALIASES = {
    "DE": ("de", "deu", "germany", "deutschland"),
    "AT": ("at", "aut", "austria", "\u00f6sterreich", "oesterreich"),
    "CH": ("ch", "che", "switzerland", "schweiz", "suisse", "svizzera", "svizra"),
}
_COUNTRY_CODES = {
    alias: code for code, aliases in _COUNTRY_ALIASES.items() for alias in aliases
}
_UMLAUTS = str.maketrans({"\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue"})


def _key(value: str) -> str:
    value = unicodedata.normalize("NFC", value).casefold()
    value = re.sub(r"[_\-\u2010-\u2015]", " ", value)
    return " ".join(value.split())


def _folded_keys(value: str) -> set[str]:
    key = _key(value)
    return {
        "".join(
            char
            for char in unicodedata.normalize("NFKD", variant)
            if not unicodedata.combining(char)
        )
        for variant in (key, key.translate(_UMLAUTS))
    }


def _country_code(value: str) -> str | None:
    return _COUNTRY_CODES.get(unicodedata.normalize("NFC", value).strip().casefold())


class _Gazetteer:
    def __init__(self, data: dict):
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("Unsupported location data schema")
        self.regions = {}
        self.region_aliases = defaultdict(set)
        self.places = {}
        self.exact = defaultdict(set)
        self.folded = defaultdict(set)
        for code, (name, aliases) in data["regions"].items():
            country = code.split(".")[0]
            if country not in _COUNTRIES or not isinstance(name, str) or not name:
                raise ValueError("Invalid region")
            self.regions[code] = name
            for alias in [name, *aliases]:
                for key in _folded_keys(alias):
                    self.region_aliases[country, key].add(code)
        for geoname_id, region, name, aliases in data["places"]:
            if (
                not isinstance(geoname_id, int)
                or geoname_id in self.places
                or region not in self.regions
                or not isinstance(name, str)
                or not name
            ):
                raise ValueError("Invalid place")
            self.places[geoname_id] = (name, region)
            country = region.split(".")[0]
            for alias in [name, *aliases]:
                self.exact[country, _key(alias)].add(geoname_id)
                for key in _folded_keys(alias):
                    self.folded[country, key].add(geoname_id)
        if not self.places or not self.regions:
            raise ValueError("Empty location data")

    def region(self, value: str, country: str) -> str | None:
        matches = set()
        for key in _folded_keys(value):
            matches.update(self.region_aliases.get((country, key), ()))
        return next(iter(matches)) if len(matches) == 1 else None

    def place(self, value: str, country: str, region: str | None):
        matches = self.exact.get((country, _key(value)), set())
        if matches:
            canonical = {
                identifier
                for identifier in matches
                if _key(self.places[identifier][0]) == _key(value)
            }
            if canonical:
                matches = canonical
        if not matches:
            matches = set()
            for key in _folded_keys(value):
                matches.update(self.folded.get((country, key), ()))
        if region:
            matches = {
                identifier
                for identifier in matches
                if self.places[identifier][1] == region
            }
        if len(matches) != 1:
            return None
        name, region = self.places[next(iter(matches))]
        return name, self.regions[region], country


@lru_cache(maxsize=1)
def _gazetteer() -> _Gazetteer | None:
    try:
        source = resources.files("jobspy").joinpath("data", "locations.json.gz")
        return _Gazetteer(json.loads(gzip.decompress(source.read_bytes())))
    except (
        OSError,
        EOFError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        zlib.error,
    ) as exc:
        log.warning("Location data unavailable; keeping original locations: %s", exc)
        return None


def _without_postcode(value: str, country: str) -> str:
    digits = 5 if country == "DE" else 4
    value = re.sub(
        rf"^(?:{country}-)?\d{{{digits}}}\s+", "", value, flags=re.IGNORECASE
    )
    return re.sub(rf"\s+\d{{{digits}}}$", "", value).strip()


def _is_wuerttemberg(value: str) -> bool:
    return bool(_folded_keys(value) & {"wurttemberg", "wuerttemberg"})


@lru_cache(maxsize=8192)
def _normalize_parts(city: str | None, state: str | None, country: str | None):
    if not city or not city.strip():
        return None
    countries = set()
    if country and country.strip():
        code = _country_code(country)
        if not code:
            return None
        countries.add(code)
    city_parts = [part.strip() for part in city.split(",") if part.strip()]
    state_parts = [part.strip() for part in (state or "").split(",") if part.strip()]
    for parts in (city_parts, state_parts):
        while parts and (code := _country_code(parts[-1])):
            countries.add(code)
            parts.pop()
    if len(countries) != 1 or not city_parts:
        return None
    code = next(iter(countries))
    gazetteer = _gazetteer()
    if gazetteer is None:
        return None

    regions = set()
    unknown_state_parts = []
    for part in state_parts:
        region = gazetteer.region(_without_postcode(part, code), code)
        if region is None:
            unknown_state_parts.append(part)
        else:
            regions.add(region)
    while len(city_parts) > 1:
        region = gazetteer.region(_without_postcode(city_parts[-1], code), code)
        if region is None:
            break
        regions.add(region)
        city_parts.pop()
    if len(regions) > 1:
        return None

    baden_wuerttemberg = gazetteer.region("Baden-W\u00fcrttemberg", "DE")
    confirmed_baden_wuerttemberg = code == "DE" and regions == {baden_wuerttemberg}
    if unknown_state_parts and (
        not confirmed_baden_wuerttemberg
        or any(not _is_wuerttemberg(part) for part in unknown_state_parts)
    ):
        return None
    while (
        len(city_parts) > 1
        and _is_wuerttemberg(city_parts[-1])
        and confirmed_baden_wuerttemberg
    ):
        city_parts.pop()

    city_name = _without_postcode(", ".join(city_parts), code)
    return gazetteer.place(city_name, code, next(iter(regions), None))


def normalize_location(location: Location) -> Location:
    """Return a canonical copy only when country, place and region agree uniquely."""
    country = location.country
    if isinstance(country, Country):
        country = country.value[0]
    result = _normalize_parts(location.city, location.state, country)
    if result is None:
        return location
    city, state, country_code = result
    return Location(city=city, state=state, country=_COUNTRIES[country_code])
