"""Conservative, source-independent evidence checks for the strict remote mode.

Search filters find candidates. Only evidence belonging to an individual job can
qualify it. Unknown/conditional arrangements deliberately fail closed.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from jobspy.model import (
    JobPost,
    JobResponse,
    RemoteLocation,
    RemoteMetadata,
    ScraperInput,
    Site,
)

STRICT_REMOTE_SITES = frozenset(
    {Site.XING, Site.LINKEDIN, Site.STEPSTONE, Site.INDEED, Site.ARBEITSAGENTUR}
)
STRICT_REMOTE_COLUMNS = ["remote_min_percent", "remote_evidence"]
MAX_REMOTE_CANDIDATES = 1000
MIN_REMOTE_PERCENT = 95

_REMOTE = (
    r"(?:remote(?:[- ]arbeit)?|home[- ]?office|work(?:ing)? from home|wfh|telearbeit)"
)
_PRESENCE = (
    r"(?:vor ort|on[- ]site|in[- ]person|präsenz\w*|praesenz\w*|büro\w*|buero\w*|"
    r"(?<!home )(?<!home-)\boffice(?: attendance| visits)?\b|dienstreise\w*|reisebereitschaft|reisen|"
    r"(?:business )?travel|kundenbesuch\w*|kundeneinsätz\w*|teamtreffen|offsite\w*)"
)
_NUMBER = r"\d{1,3}(?:[.,]\d+)?"
_PERCENT = re.compile(
    rf"(?P<low>{_NUMBER})\s*(?:%?\s*(?:[-–—]|bis|to)\s*(?P<high>{_NUMBER}))?\s*(?:%|prozent\b|percent\b)"
)
_FULL_REMOTE = re.compile(
    rf"\b(?:fully|full|completely|entirely|exclusively|vollständig\w*|vollstaendig\w*|"
    rf"komplett\w*|ausschließlich\w*|ausschliesslich\w*)[- ]+(?:im |in |aus dem )?{_REMOTE}\b"
    rf"|\b{_REMOTE}[- ]+(?:vollständig|vollstaendig|komplett|exclusively|entirely)\b"
    r"|#li-remote\b"
)
_CONDITIONAL = re.compile(
    r"\b(?:bis zu|up to|nach absprache|nach vereinbarung|nach rücksprache|"
    r"nach ruecksprache|verhandelbar|negotiable|subject to|depending on|"
    r"je nach|nach bedarf|upon agreement|by agreement|nach der probezeit|after probation)\b"
)
_NEGATED_REMOTE = re.compile(
    rf"\b(?:kein\w*|nicht|not|no|never)\s+(?:(?:rein\w*|vollständig\w*|vollstaendig\w*|"
    rf"komplett\w*|fully|full|completely|entirely|{_NUMBER}\s*%|möglichkeit|option|for|für|"
    rf"a|an|eine?\w*|zur?|of)\s+){{0,5}}{_REMOTE}"
    rf"|{_REMOTE}.{{0,35}}\b(?:nicht möglich|nicht moeglich|ausgeschlossen|not possible|not available|not permitted|not an option)\b"
)
_HYBRID_WORK = re.compile(
    r"\b(?:hybrid(?:es|e|en)?\s+(?:arbeit\w*|modell\w*|work\w*|role|position)|"
    r"(?:workplace|arbeitsmodell|arbeitsplatztyp)\s*:\s*hybrid|"
    r"(?:work|working|arbeiten|arbeitest)\s+hybrid|"
    r"(?:role|position|job)\s+is\s+hybrid|hybrid(?=\s*[.!;]|$))\b"
)
_PARTIAL_REMOTE = re.compile(
    rf"\b(?:teilweise\w*|partly|partially|occasionally|gelegentlich\w*|anteilig\w*)\s+{_REMOTE}\b"
)
_ONBOARDING = re.compile(r"\b(?:einarbeit\w*|onboard\w*|induction|training period)\b")
_BOUNDED_ONBOARDING = re.compile(
    r"\b(?:\d+|ein\w*|zwei|drei|vier|one|two|three|four)\s*[- ]?"
    r"(?:wöchig\w*|woechig\w*|tägig\w*|taegig\w*|woche\w*|tage?\w*|monate?\w*|weeks?|days?|months?)\b"
)
_RECURRING = re.compile(
    r"\b(?:jede\w*|every|pro woche|per week|weekly|wöchentlich|woechentlich|"
    r"monatlich|monthly|quarterly|vierteljährlich|jährlich|jaehrlich|annually|annual|yearly|"
    r"regularly|regelmäßig|regelmaessig)\b"
)
_OBLIGATION = re.compile(
    r"\b(?:pflicht\w*|verpflicht\w*|muss|müssen|muessen|erforderlich|vorausgesetzt|"
    r"must|required|requires?|need(?:ed)?|mandatory|expected|erwartet|attendance|gelegentlich|occasional\w*|regelmäßig|"
    r"regelmaessig|regular\w*|tage?|days?|reisebereitschaft|dienstreisen|travel)\b"
)
_OPTIONAL = re.compile(
    r"\b(?:freiwillig\w*|optional\w*|auf wunsch|nach wunsch|if you wish)\b"
)
_GERMANY = r"(?:deutschland(?:s)?|germany|de|german(?:y)? residency)"
_BROAD = (
    r"(?:weltweit|worldwide|globally|anywhere|europa|europe|european union|eu|eea|emea)"
)
_ELIGIBLE_SCOPE = rf"(?:{_GERMANY}|{_BROAD})"
_RESIDENCY = re.compile(
    r"\b(?:wohnsitz|wohnort|wohnhaft|resid\w*|must (?:live|be based|be located)|"
    r"(?:candidates|applicants) (?:in|from|based)|du wohnst|sie wohnen|ansässig|ansaessig|"
    r"(?:you (?:are|will be)|du bist|sie sind)\s+(?:based|located|ansässig))\b"
)
_REGIONAL = re.compile(
    r"\b(?:umkreis|pendel\w*|nähe|naehe|commut\w*|radius|within \d+|"
    r"im raum|in der region|in the vicinity|near our)\b"
)


def is_strict(scraper_input) -> bool:
    return getattr(scraper_input, "remote_mode", None) == "strict"


def plain_remote_text(value: str | None) -> str:
    """Retain paragraph boundaries, but join inline HTML (e.g. 100% <b>remote</b>)."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    if re.search(r"</?[a-z][^>]*>", value, re.I):
        soup = BeautifulSoup(value, "html.parser")
        for tag in soup.find_all(["script", "style", "noscript", "svg"]):
            tag.decompose()
        for tag in soup.find_all(["p", "li", "div", "br", "h1", "h2", "h3", "h4"]):
            tag.append("\x1e")
        value = soup.get_text(" ").replace("\x1e", "\n")
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = value.replace("**", "").replace("__", "")
    return "\n".join(
        " ".join(line.split()) for line in value.splitlines() if line.strip()
    )


def _clauses(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(
            r"\n+|(?<=[.!?;])\s+|\s+(?:aber|but|jedoch|however)\s+", text.casefold()
        )
        if part.strip()
    ]


def _percentages(clause: str, topic: str) -> list[tuple[float, float]]:
    """Only bind percentages immediately describing the topic, not employment FTE."""
    values = []
    bridge = r"(?:\s|[-:(),]|\b(?:im|in|aus|dem|der|deiner|arbeitszeit|zeit|arbeiten|work|working|von|of|the|at|least|most|mindestens|maximal|höchstens|bis|zu|up|to|anteil|beträgt|is)\b)*"
    for match in _PERCENT.finditer(clause):
        before, after = clause[: match.start()], clause[match.end() :]
        if not (
            re.search(rf"{topic}{bridge}$", before)
            or re.match(rf"{bridge}{topic}\b", after)
        ):
            continue
        low = float(match["low"].replace(",", "."))
        high = float((match["high"] or match["low"]).replace(",", "."))
        values.append((low, high))
    return values


def _optional_presence(clause: str) -> bool:
    # Remove narrowly scoped negated requirements before testing other obligations.
    cleaned = re.sub(
        r"\b(?:keine?\w*\s+(?:büro|buero|präsenz|praesenz)pflicht|"
        r"keine?\s+(?:dienstreisen|reisebereitschaft)|"
        r"no\s+(?:mandatory\s+)?(?:office attendance|office visits|business travel)(?: (?:is |are )?required)?|"
        r"(?:office attendance|office visits|travel) (?:is |are )?not required)\b",
        "optional",
        clause,
    )
    return bool(_OPTIONAL.search(cleaned)) and not bool(_OBLIGATION.search(cleaned))


def _location_name(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("countryCode")
    return value.strip() if isinstance(value, str) and value.strip() else None


def metadata_from_job_posting(
    posting: dict, *, description_complete=False
) -> RemoteMetadata:
    result = RemoteMetadata(description_complete=description_complete)
    if posting.get("jobLocationType") == "TELECOMMUTE":
        result.full_remote = True
        result.evidence.append("jobLocationType=TELECOMMUTE")
    locations = posting.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    if isinstance(locations, list):
        for location in locations:
            if not isinstance(location, dict):
                continue
            address = location.get("address")
            if isinstance(address, dict) and (
                country := _location_name(address.get("addressCountry"))
            ):
                result.job_countries.append(country)
    if "applicantLocationRequirements" in posting:
        result.has_applicant_restrictions = True
        requirements = posting.get("applicantLocationRequirements") or []
        if not isinstance(requirements, list):
            requirements = [requirements]
        for requirement in requirements:
            name = _location_name(requirement)
            if not name:
                continue
            kind = "unknown"
            if isinstance(requirement, dict):
                kind = "country" if requirement.get("@type") == "Country" else "region"
            result.applicant_locations.append(RemoteLocation(name=name, kind=kind))
    return result


def extract_job_posting(soup: BeautifulSoup, job_url: str) -> dict:
    """Ignore unrelated/recommended postings and ambiguous JSON-LD collections."""
    postings = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if (
                types == "JobPosting"
                or isinstance(types, list)
                and "JobPosting" in types
            ):
                postings.append(value)
            if "@graph" in value:
                visit(value["@graph"])

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            visit(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue

    target = urlsplit(job_url)
    target_ids = re.findall(r"\d{6,}", target.path)
    matches = []
    for posting in postings:
        url = posting.get("url")
        if not isinstance(url, str):
            continue
        parts = urlsplit(url)
        if parts.hostname != target.hostname:
            continue
        if parts.path.rstrip("/") == target.path.rstrip("/") or (
            target_ids and re.findall(r"\d{6,}", parts.path)[-1:] == target_ids[-1:]
        ):
            matches.append(posting)
    if len(matches) == 1:
        return matches[0]
    if len(postings) == 1 and not postings[0].get("url"):
        return postings[0]
    return {}


def _germany_eligibility(
    metadata: RemoteMetadata, clauses: list[str]
) -> tuple[bool, str]:
    text_proof = None
    for clause in clauses:
        if re.search(
            rf"(?:excluding|except|not from|nicht aus|außer|ausser)\s+{_GERMANY}\b",
            clause,
        ) or re.search(
            rf"\b{_GERMANY}\s+(?:is )?(?:excluded|not eligible|not included|ausgeschlossen)\b",
            clause,
        ):
            return False, "country_restricted"
        remote_context = bool(re.search(_REMOTE, clause))
        residence = bool(_RESIDENCY.search(clause))
        if _REGIONAL.search(clause) and (
            residence or remote_context or _OBLIGATION.search(clause)
        ):
            return False, "country_restricted"
        # A declared remote work area must include all of Germany. Unknown city,
        # region or country names fail closed, including "anywhere in France".
        remote_scopes = list(
            re.finditer(
                rf"{_REMOTE}\s+(?:(?:work|working|arbeit\w*|position|role|job|tätigkeit|stelle)\s+)?"
                r"(?:from|within|across|throughout|aus|innerhalb|in)\s+(?P<scope>.+)",
                clause,
            )
        )
        if residence:
            remote_scopes.extend(
                re.finditer(
                    r"\b(?:from|within|across|throughout|aus|innerhalb|in|wohnsitz:)\s+(?P<scope>.+)",
                    clause,
                )
            )
        for declared in remote_scopes:
            scope_text = re.sub(
                r"^(?:(?:anywhere in|überall in|ueberall in|ganzem|ganzes|ganz|all of|the)\s+)+",
                "",
                declared["scope"],
            )
            allowed_scope = re.match(rf"{_ELIGIBLE_SCOPE}\b", scope_text)
            if not allowed_scope:
                return False, "country_restricted"
            remainder = scope_text[allowed_scope.end() :].lstrip()
            if re.match(
                r"(?:[,(/]|in\b|within\b|around\b|aus\b|only\b|nur\b|except\b|excluding\b)",
                remainder,
            ):
                return False, "country_restricted"
        scope = re.search(
            rf"\b(?:from|within|across|throughout|aus|innerhalb|in|wohnsitz:|location:)\s+"
            rf"(?:(?:anywhere|überall|ueberall|ganz|ganzem|ganzes|ganz|all|the|von|in)\s+)*{_ELIGIBLE_SCOPE}\b"
            rf"|\b(?:deutschlandweit|bundesweit|weltweit|worldwide)\b",
            clause,
        )
        if residence and not scope:
            return False, "country_restricted"
        if scope and (
            remote_context
            or residence
            or re.search(r"\b(?:arbeiten|work|working)\b", clause)
        ):
            text_proof = clause
        if (
            remote_context
            and not scope
            and re.search(
                r"\b(?:from|within|in|aus)\s+(?:the\s+)?(?:us|usa|uk|united states|united kingdom|österreich|austria|schweiz|switzerland)\b",
                clause,
            )
        ):
            return False, "country_restricted"
        # Explicit foreign-only work scopes override a German office address.
        if remote_context and re.search(
            r"\b(?:only|ausschließlich|ausschliesslich|nur)\b", clause
        ):
            if (
                re.search(
                    r"\b(?:us|usa|uk|united states|united kingdom|österreich|austria|schweiz|switzerland)\b",
                    clause,
                )
                and not scope
            ):
                return False, "country_restricted"

    if metadata.has_applicant_restrictions:
        allowed = False
        for location in metadata.applicant_locations:
            name = location.name.casefold().strip()
            if re.fullmatch(_BROAD, name):
                allowed = True
            elif location.kind != "region" and re.fullmatch(_GERMANY, name):
                allowed = True
            elif location.kind == "region":
                return False, "country_restricted"
        if not allowed:
            return False, "country_restricted"
        return True, "applicant locations: " + ", ".join(
            x.name for x in metadata.applicant_locations
        )
    if text_proof:
        return True, text_proof
    if any(
        re.fullmatch(_GERMANY, name.casefold().strip())
        for name in metadata.job_countries
    ):
        return True, "source job country: Germany; no narrower applicant restriction"
    return False, "country_unknown"


@dataclass(frozen=True)
class RemoteAssessment:
    min_percent: float | None = None
    reason: str = "remote_not_proven"
    evidence: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.min_percent is not None and self.min_percent >= MIN_REMOTE_PERCENT


def assess_remote_job(job: JobPost) -> RemoteAssessment:
    metadata = job.remote_metadata
    description = plain_remote_text(
        metadata.description_text
        if metadata.description_text is not None
        else job.description
    )
    if not metadata.description_complete or not description:
        return RemoteAssessment(reason="missing_full_description")
    clauses = _clauses(plain_remote_text(job.title) + "\n" + description)
    proofs = (
        (list(metadata.evidence) or ["source full-remote classification"])
        if metadata.full_remote
        else []
    )
    percentages = [100.0] if metadata.full_remote else []
    presence_percent = 0.0
    onboarding = []
    for clause in clauses:
        remote = bool(re.search(_REMOTE, clause))
        numbers = _percentages(clause, _REMOTE)
        full = bool(_FULL_REMOTE.search(clause))
        if _NEGATED_REMOTE.search(clause):
            return RemoteAssessment(reason="remote_contradiction")
        if remote and _CONDITIONAL.search(clause):
            return RemoteAssessment(reason="conditional_remote")
        if numbers:
            if any(
                low < MIN_REMOTE_PERCENT or high > 100 or low > high
                for low, high in numbers
            ):
                return RemoteAssessment(reason="remote_below_threshold")
            percentages.extend(low for low, _ in numbers)
            proofs.append(clause)
        elif full:
            percentages.append(100.0)
            proofs.append(clause)
        if _HYBRID_WORK.search(clause) and not (
            numbers
            or full
            and re.search(
                r"\b(?:oder|or|wahlweise|alternativ|choice|optional)\b", clause
            )
        ):
            return RemoteAssessment(reason="hybrid_work")
        if _PARTIAL_REMOTE.search(clause) and not numbers:
            return RemoteAssessment(reason="partial_remote")
        presence = bool(re.search(_PRESENCE, clause))
        onboard = bool(_ONBOARDING.search(clause))
        if onboard and (presence or re.search(r"\b(?:nach|after)\b", clause)):
            if (
                not _BOUNDED_ONBOARDING.search(clause)
                or _RECURRING.search(clause)
                or re.search(
                    r"\b(?:und|and)\b.*\b(?:travel|dienstreisen|reisebereitschaft)\b",
                    clause,
                )
            ):
                return RemoteAssessment(reason="onboarding_unclear")
            onboarding.append(clause)
            continue
        if presence and not _optional_presence(clause):
            attendance = _percentages(clause, _PRESENCE)
            if attendance:
                if (
                    any(low < 0 or high > 5 or low > high for low, high in attendance)
                    or re.search(
                        r"\b(?:at least|mindestens|more than|mehr als|tage?|days?)\b",
                        clause,
                    )
                    or _RECURRING.search(clause)
                ):
                    return RemoteAssessment(reason="presence_exceeds_limit")
                presence_percent += sum(high for _, high in attendance)
                if presence_percent > 5:
                    return RemoteAssessment(reason="presence_exceeds_limit")
                proofs.append(clause)
            elif (
                _OBLIGATION.search(clause)
                or _RECURRING.search(clause)
                or re.search(
                    r"\b(?:in[- ]person|you (?:will )?work|du arbeitest|sie arbeiten|"
                    r"arbeitsplatz befindet sich|workplace is|"
                    r"(?:on[- ]site|vor ort)[- ]+(?:position|role|job|tätigkeit))\b",
                    clause,
                )
            ):
                return RemoteAssessment(reason="presence_unclear")
    if metadata.onsite:
        return RemoteAssessment(reason="onsite_metadata")
    if (
        metadata.advertised_percent is not None
        and not 95 <= metadata.advertised_percent <= 100
    ):
        return RemoteAssessment(reason="remote_below_threshold")
    if not percentages:
        return RemoteAssessment()
    eligible, country_evidence = _germany_eligibility(metadata, clauses)
    if not eligible:
        return RemoteAssessment(reason=country_evidence)
    percent = min(min(percentages), 100 - presence_percent)
    evidence = tuple(dict.fromkeys([*proofs, country_evidence, *onboarding]))
    return RemoteAssessment(percent, "accepted", evidence)


class StrictRemoteSearch:
    """Per-source accounting, bounded candidate scans, and verified-result offsets."""

    def __init__(self, scraper_input: ScraperInput, logger):
        self.input = scraper_input
        self.logger = logger
        self.seen: set[str] = set()
        self.accepted = 0
        self.rejections: Counter[str] = Counter()
        self.target = scraper_input.offset + scraper_input.results_wanted
        self.started_at = monotonic()

    @property
    def max_candidates(self) -> int:
        return min(self.input.remote_max_candidates, MAX_REMOTE_CANDIDATES)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, monotonic() - self.started_at)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.input.remote_max_seconds - self.elapsed_seconds)

    @property
    def stop_reason(self) -> str | None:
        if self.input.results_wanted == 0 or self.accepted >= self.target:
            return "target reached"
        if len(self.seen) >= self.max_candidates:
            return "candidate limit reached"
        if self.remaining_seconds <= 0:
            return "time budget reached"
        return None

    @property
    def done(self) -> bool:
        return self.stop_reason is not None

    def log_progress(self, page: int) -> None:
        self.logger.info(
            "Strict remote: search page %s; candidates %s/%s; accepted %s/%s "
            "(including offset); elapsed %.1f/%.1fs",
            page,
            len(self.seen),
            self.max_candidates,
            self.accepted,
            self.target,
            self.elapsed_seconds,
            self.input.remote_max_seconds,
        )

    def visit(self, identifier: str) -> bool:
        if identifier in self.seen or self.done:
            return False
        self.seen.add(identifier)
        return True

    def reject(self, reason: str) -> None:
        self.rejections[reason] += 1

    def accept(self, job: JobPost | None) -> bool:
        if job is None:
            self.reject("unavailable_or_other_filter")
            return False
        result = assess_remote_job(job)
        if not result.accepted:
            self.reject(result.reason)
            return False
        job.is_remote = True
        job.remote_min_percent = result.min_percent
        job.remote_evidence = " | ".join(result.evidence)
        self.accepted += 1
        return True

    def finish(self, jobs: list[JobPost]) -> JobResponse:
        self.logger.info(
            "Strict remote: %s candidates, %s accepted, rejections=%s",
            len(self.seen),
            self.accepted,
            dict(self.rejections),
        )
        reason = self.stop_reason
        if reason in {"candidate limit reached", "time budget reached"}:
            self.logger.warning(
                "Strict remote %s; returning verified partial results (%s/%s accepted, %.1fs elapsed)",
                reason,
                self.accepted,
                self.target,
                self.elapsed_seconds,
            )
        start = self.input.offset
        return JobResponse(jobs=jobs[start : start + self.input.results_wanted])


def indeed_date_is_recent(
    value, hours_old: int | None, now: datetime | None = None
) -> bool:
    if hours_old is None:
        return True
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            posted = datetime.fromtimestamp(value / 1000, timezone.utc)
        elif isinstance(value, str):
            posted = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
        else:
            return False
        age = ((now or datetime.now(timezone.utc)) - posted).total_seconds()
        return 0 <= age <= hours_old * 3600
    except (ValueError, OverflowError, OSError):
        return False
