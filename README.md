<img src="https://github.com/cullenwatson/JobSpy/assets/78247585/ae185b7e-e444-4712-8bb9-fa97f53e896b" width="400">

**JobSpy** is a job scraping library with the goal of aggregating all the jobs from popular job boards with one tool.

## Features

- Scrapes job postings from **LinkedIn**, **Indeed**, **Glassdoor**, **Google**, **ZipRecruiter**, **StepStone**, **Arbeitsagentur**, and **Xing** concurrently
- Aggregates the job postings in a dataframe
- Proxies support to bypass blocking

![jobspy](https://github.com/cullenwatson/JobSpy/assets/78247585/ec7ef355-05f6-4fd3-8161-a817e31c5c57)

### Installation

```
pip install -U python-jobspy
```

_Python version >= [3.10](https://www.python.org/downloads/release/python-3100/) required_

### Usage

```python
import csv
from jobspy import scrape_jobs

jobs = scrape_jobs(
    site_name=["indeed", "linkedin", "zip_recruiter", "google"], # "glassdoor", "stepstone", "arbeitsagentur", "xing"
    search_term="software engineer",
    google_search_term="software engineer jobs near San Francisco, CA since yesterday",
    location="San Francisco, CA",
    results_wanted=20,
    hours_old=72,
    country_indeed='USA',
    
    # linkedin_fetch_description=True # gets more info such as description, direct job url (slower)
    # proxies=["208.195.175.46:65095", "208.195.175.45:65095", "localhost"],
)
print(f"Found {len(jobs)} jobs")
print(jobs.head())
jobs.to_csv("jobs.csv", quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False) # to_excel
```
### Strict remote search from Germany

Use `remote_mode="strict"` to search without a city and keep jobs whose posting
supports **95–100% remote work from Germany**:

```python
from jobspy import scrape_jobs

jobs = scrape_jobs(
    site_name=["xing", "linkedin", "stepstone", "indeed", "arbeitsagentur"],
    search_term="Java Entwickler",
    country="germany",
    remote_mode="strict",
    results_wanted=20,
)
print(jobs[["site", "title", "remote_min_percent", "remote_evidence", "job_url"]])
```

`country="germany"` is required explicitly (also accepts `Country.GERMANY`).
It selects Germany for every supported source, including Indeed, regardless of
the legacy `country_indeed` default. Omit `location`; `Germany`, `Deutschland`
and `DE` are also accepted. A city raises `ValueError`; `distance` is ignored.
Strict mode enables remote filtering and full LinkedIn descriptions automatically.

The following native settings find **candidates**, which are then checked locally:

| Source | Native search setting | Evidence checked for each job |
| --- | --- | --- |
| Xing | `remoteOption=FULL_REMOTE.050e26` | `FULL_REMOTE`, `TELECOMMUTE` or explicit full-remote text |
| LinkedIn | `f_WT=2`, location `Germany` | Detail-page `TELECOMMUTE` or explicit full-remote text |
| StepStone | `wp=611350` | Detail-page `TELECOMMUTE` or explicit full-remote text |
| Indeed | Remote attribute `DSQF7`, country `DE` | The individual job's Remote classification or explicit full-remote text |
| Arbeitsagentur | `homeoffice=prozentual_95` | Explicit minimum in the full description; the advertised percentage alone can be an upper bound |

Native settings were checked on 2026-10-27 and may change. Indeed distinguishes
[fully remote, hybrid and on-the-road postings](https://www.indeed.com/help/employers/articles/how-to-post-a-remote-job-on-indeed?co=US&hl=en).
In strict mode, its remote and job-type filters are combined; `hours_old` is
checked locally against `dateOnIndeed`, since the API rejects date filters
inside the same composite filter. With `easy_apply=True`, Indeed's server-side
Easy Apply filter is used on its own: combining it with attribute filters currently
causes an API error. Remote evidence, job type and age are still required and
checked locally, so this combination may need to examine more candidates.

Every accepted job needs a full description and job-specific evidence. Search
filter membership, an inferred display country and the old `is_remote` Boolean
are insufficient. The conservative German/English rules reject recognized
contradictions, conditional offers such as “up to 100%” or “nach Absprache”,
hybrid arrangements without an explicit minimum of 95%, and unclear mandatory
office attendance or travel. Separately stated office and travel percentages
are added and must total at most 5%. Clearly time-bounded initial onboarding
on site is allowed; the threshold applies to the regular working arrangement.

Applicant restrictions take precedence over the office address. An explicit
Germany/EU/EMEA/worldwide work area can qualify a job with a foreign employer;
otherwise an actual German job country is required. Recognized regional
residency restrictions and foreign-only work areas are excluded. The checks
use the source description before HTML/Markdown/plain output conversion.

Strict results add `remote_min_percent` and `remote_evidence` columns, including
for empty results. The percentage is derived from the posting's claims, **not
a confidence score or a guarantee of the employer's actual policy**. The rules
favor precision over coverage and can miss valid jobs or unrecognized wording;
keep the evidence when reviewing an offer.

`results_wanted` and `offset` count accepted jobs per source, not downloaded
advertisements or pages. Many candidates may fail verification. Rejected pages
do not stop pagination, but each source now defaults to **100 unique candidates**
(`remote_max_candidates`) and a **120-second search budget** (`remote_max_seconds`).
Either limit ends that source's search and returns verified partial results,
possibly fewer than `results_wanted`. The candidate limit can be set from 1 to
1,000; the time budget must be positive and finite. These settings apply only
to strict mode, independently for each source, including candidates skipped by
`offset`. To explicitly allow a longer scan, for example:

```python
jobs = scrape_jobs(
    search_term="Java Entwickler", country="germany", remote_mode="strict",
    results_wanted=20, remote_max_candidates=300, remote_max_seconds=300,
)
```

Time limits are checked between work steps; an in-flight request may finish
after the budget expires. They are not a hard deadline for the entire call.
LinkedIn also caps request timeouts to the remaining budget and disables HTTP
retries/backoff during strict requests. Its progress logs show the current page,
candidates checked, accepted jobs and elapsed time, rather than a guessed total
page count. Sources also stop on exhausted/repeated results or blocking and log
rejection counts. Google, Glassdoor and ZipRecruiter are
skipped with a warning when mixed with supported sources; selecting only
unsupported sources raises `ValueError` before making requests.

Omitting `remote_mode` preserves the existing search behavior and output columns.
`is_remote=True` continues to provide the broader remote/homeoffice search.

### DACH Location Normalization
By default, all portals share an offline normalization step for the `location`
column in Germany, Austria and Switzerland. For example, `Constance, BW, DE`
and `78467 Konstanz, BADEN_WUERTTEMBERG, Germany` become
`Konstanz, Baden-Württemberg, Germany`.

Cities and regions use German names where available, followed by `Germany`,
`Austria` or `Switzerland`. An unambiguous city can supply a missing region.
Postcodes and duplicate country names are removed only after a successful match.
Postcodes are not validated or used to choose between otherwise ambiguous places.
Conflicting regions, multiple workplaces, unknown qualifiers, remote locations
and ambiguous matches keep their original formatting. A DACH country must be
present in the job's location fields; the search location is not used to guess it.

Use `scrape_jobs(..., normalize_locations=False)` to retain the original output.
The column layout and row ordering stay the same; use `jobs.sort_values("location")`
to sort by the normalized location. Existing CSV files are not rewritten.

The bundled [GeoNames](https://www.geonames.org/) extract is used under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), with modifications for
normalization. No API account or runtime network requests are needed.
See [data provenance and updates](jobspy/data/README.md) for snapshot details.

### ZipRecruiter Browser Fallback

For an attended run, enable `ziprecruiter_browser_fallback=True` in `scrape_jobs()`.
If a known referral link encounters a Cloudflare challenge, the default mode
`ziprecruiter_browser_mode="system"` opens the link in your normal system browser
and opens a local URL form once. Complete the redirect, paste the destination URL
into the form and select **Uebernehmen**. The default for the fallback is `False`;
unattended runs do not open a browser or start a local form server.
The fallback only handles `/ekn` links on ZipRecruiter `.com`/`.de` domains and
Alertsclk links. Direct detail pages do not open a browser, including when their
detail request is blocked. A destination already reached by HTTP redirects is
retained even if fetching its content fails.

```python
jobs = scrape_jobs(
    site_name=["zip_recruiter"],
    search_term="Softwareentwickler",
    location="Berlin",
    country_indeed="germany",
    ziprecruiter_browser_fallback=True,
    ziprecruiter_browser_mode="system",
    ziprecruiter_browser_timeout=180,
)
```

`ziprecruiter_browser_timeout` must be a positive number of seconds and includes
manual URL entry. **Ueberspringen** keeps the current unresolved link and continues
with the next one. **Fuer diesen Lauf abbrechen**, a timeout or a browser failure
disables the fallback for the rest of that run. Closing the form tab is not
detectable; the request waits until its timeout. The scraper closes its local
server at the end, but leaves your normal browser and tabs open.

The system mode uses the browser's existing profile and network/proxy settings.
It does not control the browser, read its cookies or install an extension. The
form only accepts session-bound requests on `127.0.0.1` at a temporary port.

The previous automated browser remains available with
`ziprecruiter_browser_mode="playwright"`. It prefers installed Chrome, otherwise
Playwright Chromium (`python -m playwright install chromium`), and requires a
graphical desktop. Its dedicated persistent profile is
`.cache/jobspy/ziprecruiter-browser` relative to the working directory. Keep that
directory private and do not share it between simultaneous runs. In this mode,
the first configured scraper proxy is kept for the whole browser session, and
closing the window disables the fallback. Cloudflare may keep rejecting it.

Both modes resolve job links; fetching StepStone details is a separate request
and may still be blocked. A manually entered URL does not imply a successful HTTP
request or provide job HTML.

To diagnose one link interactively, run
`python -m scripts.diagnose_ziprecruiter_redirect '<ekn-URL>' --browser-fallback`
from this repository. Its optional `--browser-timeout` also defaults to 180 seconds.
Use `--browser-mode playwright` to select the previous automated browser.

### Output

```
SITE           TITLE                             COMPANY           CITY          STATE  JOB_TYPE  INTERVAL  MIN_AMOUNT  MAX_AMOUNT  JOB_URL                                            DESCRIPTION
indeed         Software Engineer                 AMERICAN SYSTEMS  Arlington     VA     None      yearly    200000      150000      https://www.indeed.com/viewjob?jk=5e409e577046...  THIS POSITION COMES WITH A 10K SIGNING BONUS!...
indeed         Senior Software Engineer          TherapyNotes.com  Philadelphia  PA     fulltime  yearly    135000      110000      https://www.indeed.com/viewjob?jk=da39574a40cb...  About Us TherapyNotes is the national leader i...
linkedin       Software Engineer - Early Career  Lockheed Martin   Sunnyvale     CA     fulltime  yearly    None        None        https://www.linkedin.com/jobs/view/3693012711      Description:By bringing together people that u...
linkedin       Full-Stack Software Engineer      Rain              New York      NY     fulltime  yearly    None        None        https://www.linkedin.com/jobs/view/3696158877      Rain’s mission is to create the fastest and ea...
zip_recruiter Software Engineer - New Grad       ZipRecruiter      Santa Monica  CA     fulltime  yearly    130000      150000      https://www.ziprecruiter.com/jobs/ziprecruiter...  We offer a hybrid work environment. Most US-ba...
zip_recruiter Software Developer                 TEKsystems        Phoenix       AZ     fulltime  hourly    65          75          https://www.ziprecruiter.com/jobs/teksystems-0...  Top Skills' Details• 6 years of Java developme...

```

### Parameters for `scrape_jobs()`

```plaintext
Optional
├── site_name (list|str): 
|    linkedin, zip_recruiter, indeed, glassdoor, google, stepstone, arbeitsagentur, xing
|    (default is all)
│
├── search_term (str)
|
├── google_search_term (str)
|     search term for google jobs. This is the only param for filtering google jobs.
│
├── indeed_search_term (str)
|     search term specifically for indeed jobs. Overrides search_term for indeed if provided.
│
├── location (str)
│
├── normalize_locations (bool):
|    normalize DACH output locations using bundled GeoNames data (default True)
│
├── distance (int): 
|    in km, default 50
│
├── job_type (str): 
|    fulltime, parttime, internship, contract, apprenticeship
│
├── proxies (list): 
|    in format ['user:pass@host:port', 'localhost']
|    each job board scraper will round robin through the proxies
|
├── is_remote (bool)
│
├── remote_mode ("strict" | None):
|    verified 95–100% remote candidates from Germany; requires country="germany"
|    and no city. Supported: xing, linkedin, stepstone, indeed, arbeitsagentur.
│
├── remote_max_candidates (int):
|    strict-mode candidate limit per source, default 100, range 1–1000
│
├── remote_max_seconds (float):
|    strict-mode search budget per source, default 120 seconds; in-flight requests may overrun
│
├── results_wanted (int): 
|    number of job results to retrieve for each site specified in 'site_name'
│
├── easy_apply (bool): 
|    filters for jobs that are hosted on the job board site (LinkedIn easy apply filter no longer works)
|
├── user_agent (str): 
|    override the default user agent which may be outdated
│
├── description_format (str): 
|    markdown, html (Format type of the job descriptions. Default is markdown.)
│
├── offset (int): 
|    starts the search from an offset (e.g. 25 will start the search from the 25th result)
│
├── hours_old (int): 
|    filters jobs by the number of hours since the job was posted 
|    (ZipRecruiter and Glassdoor round up to next day.)
  |    Arbeitsagentur checks first publication locally with day precision;
  |    the cutoff day is included. Missing/invalid dates are skipped when filtering.
│
├── verbose (int) {0, 1, 2}: 
|    Controls the verbosity of the runtime printouts 
|    (0 prints only errors, 1 is errors+warnings, 2 is all logs. Default is 2.)

├── linkedin_fetch_description (bool): 
|    fetches full description and direct job url for LinkedIn (Increases requests by O(n))
│
├── linkedin_company_ids (list[int]): 
|    searches for linkedin jobs with specific company ids
|
├── country_indeed (str): 
|    filters the country on Indeed, Glassdoor & Xing (see below for supported countries)
|
├── country (Country | str):
|    optional country context for Google locations when the result omits its country
|    required as "germany" for remote_mode="strict"; then selects all source countries
|
├── enforce_annual_salary (bool): 
|    converts wages to annual salary
|
├── ca_cert (str)
|    path to CA Certificate file for proxies
```

```
├── Indeed limitations:
|    Without remote_mode="strict", only one from this list can be used in a search:
|    - hours_old
|    - job_type & is_remote
|    - easy_apply
│
└── LinkedIn limitations:
|    Only one from this list can be used in a search:
|    - hours_old
|    - easy_apply
```

## Supported Countries for Job Searching

### **LinkedIn**

LinkedIn searches globally & uses only the `location` parameter. 

### **ZipRecruiter**

ZipRecruiter searches for jobs in **US/Canada** & uses only the `location` parameter.

### **Xing**

Xing uses public search and job detail pages on `www.xing.com` (`www.xing.de`
redirects there). No account, cookies, or browser setup is required.

```python
jobs = scrape_jobs(
    site_name="xing",
    search_term="Python Entwickler",
    location="Berlin",
    country_indeed="germany",  # also "austria" or "switzerland"
    distance=50,
    results_wanted=20,
    description_format="markdown",  # also "html" or "plain"
)
```

- `location` supports DACH cities, including Vienna and Zurich. `country_indeed`
  values `germany`, `austria`, and `switzerland` additionally filter the actual job
  locations. Without a city, the country name is used as the search location.
  The default `usa` does not impose a country filter on Xing or label jobs as US jobs.
- Xing uses semantic search, so results can include related roles. `distance` is
  passed to Xing in kilometres; fully remote results may lie outside the city radius.
- `is_remote=True` includes both fully remote and hybrid jobs. `easy_apply=True`
  selects Xing applications; `False` selects external applications.
- `job_type` and `hours_old` are checked against available job metadata. Jobs with
  missing metadata needed to verify an active filter are skipped. `date_posted`
  uses Xing's published date, or its refresh date when only search data is available.
- Full descriptions are fetched automatically. `offset` skips raw search positions
  before local filtering. At most 100 pages of 20 search positions are examined.
- Salary amounts retain their source currency (including EUR and CHF). Employer
  salaries use `salary_source="direct_data"`; Xing estimates use
  `salary_source="estimated_data"`. The latter are estimates, not employer offers.
- Missing data stays empty. Removed jobs are skipped, and temporary detail failures
  retain available search data when filters can still be verified. Blocking or
  changed search markup is logged; the scraper returns the results collected so far.

### **Indeed / Glassdoor**

Indeed & Glassdoor supports most countries, but the `country_indeed` parameter is required. Additionally, use the `location`
parameter to narrow down the location, e.g. city & state if necessary. 

You can specify the following countries when searching on Indeed (use the exact name, * indicates support for Glassdoor):

|                      |              |            |                |
|----------------------|--------------|------------|----------------|
| Argentina            | Australia*   | Austria*   | Bahrain        |
| Belgium*             | Brazil*      | Canada*    | Chile          |
| China                | Colombia     | Costa Rica | Czech Republic |
| Denmark              | Ecuador      | Egypt      | Finland        |
| France*              | Germany*     | Greece     | Hong Kong*     |
| Hungary              | India*       | Indonesia  | Ireland*       |
| Israel               | Italy*       | Japan      | Kuwait         |
| Luxembourg           | Malaysia     | Mexico*    | Morocco        |
| Netherlands*         | New Zealand* | Nigeria    | Norway         |
| Oman                 | Pakistan     | Panama     | Peru           |
| Philippines          | Poland       | Portugal   | Qatar          |
| Romania              | Saudi Arabia | Singapore* | South Africa   |
| South Korea          | Spain*       | Sweden     | Switzerland*   |
| Taiwan               | Thailand     | Turkey     | Ukraine        |
| United Arab Emirates | UK*          | USA*       | Uruguay        |
| Venezuela            | Vietnam*     |            |                |

## Notes
* Indeed is the best scraper currently with no rate limiting.  
* All the job board endpoints are capped at around 1000 jobs on a given search.  
* LinkedIn is the most restrictive and usually rate limits around the 10th page with one ip. Proxies are a must basically.

## Frequently Asked Questions

---
**Q: Why is Indeed giving unrelated roles?**  
**A:** Indeed searches the description too.

- use - to remove words
- "" for exact match

Example of a good Indeed query

```py
search_term='"engineering intern" software summer (java OR python OR c++) 2025 -tax -marketing'
```

This searches the description/title and must include software, summer, 2025, one of the languages, engineering intern exactly, no tax, no marketing.

---

**Q: No results when using "google"?**  
**A:** You have to use super specific syntax. Search for google jobs on your browser and then whatever pops up in the google jobs search box after applying some filters is what you need to copy & paste into the google_search_term. 

---

**Q: Received a response code 429?**  
**A:** This indicates that you have been blocked by the job board site for sending too many requests. All of the job board sites are aggressive with blocking. We recommend:

- Wait some time between scrapes (site-dependent).
- Try using the proxies param to change your IP address.

---

### JobPost Schema

```plaintext
JobPost
├── title
├── company
├── company_url
├── job_url
├── location
│   ├── country
│   ├── city
│   ├── state
├── is_remote
├── description
├── job_type: fulltime, parttime, internship, contract, apprenticeship
├── job_function
│   ├── interval: yearly, monthly, weekly, daily, hourly
│   ├── min_amount
│   ├── max_amount
│   ├── currency
│   └── salary_source: direct_data, description (parsed from posting), estimated_data (Xing estimate)
├── date_posted
└── emails

Linkedin specific
└── job_level

Linkedin & Indeed specific
└── company_industry

Indeed specific
├── company_country
├── company_addresses
├── company_employees_label
├── company_revenue_label
├── company_description
└── company_logo
```
