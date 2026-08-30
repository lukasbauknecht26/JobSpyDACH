# constant.py
from __future__ import annotations

API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
DETAIL_API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{refnr_b64}"
BASE_WEB_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"

headers = {
    "User-Agent": "Jobsuche/2.9.2 (de.arbeitsagentur.jobboerse; build:1085; iOS 16.5.1) Alamofire/5.6.4",
    "X-API-Key": "jobboerse-jobsuche",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
