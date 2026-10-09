from jobspy.model import Country, JobType

BASE_URL = "https://www.xing.com"
SEARCH_URL = f"{BASE_URL}/jobs/search/ki"
JOBS_PER_PAGE = 20
MAX_PAGES = 100

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

COUNTRY_NAMES = {
    Country.GERMANY: "Deutschland",
    Country.AUSTRIA: "\u00d6sterreich",
    Country.SWITZERLAND: "Schweiz",
}
COUNTRY_CODES = {
    "DE": Country.GERMANY,
    "AT": Country.AUSTRIA,
    "CH": Country.SWITZERLAND,
}

# Public filter IDs used by Xing's logged-out search client.
EMPLOYMENT_FILTERS = {
    JobType.FULL_TIME: "FULL_TIME.ef2fe9",
    JobType.PART_TIME: "PART_TIME.58889d",
    JobType.CONTRACT: "CONTRACTOR.0ed397",
    JobType.INTERNSHIP: "INTERN.dc571c",
    JobType.TEMPORARY: "TEMPORARY.8ff6ad",
    JobType.SUMMER: "SEASONAL.e4ab1d",
    JobType.VOLUNTEER: "VOLUNTARY.c61099",
}
EMPLOYMENT_TYPES = {
    value.split(".")[0]: key for key, value in EMPLOYMENT_FILTERS.items()
}
REMOTE_FILTER = "FULL_REMOTE.050e26*PARTLY_REMOTE.ca71ca"
