import requests
from bs4 import BeautifulSoup
import urllib.parse
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
GUEST_JOB = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
GUEST_TYPEAHEAD = "https://www.linkedin.com/jobs-guest/api/typeaheadHits"
PUBLIC_SEARCH = "https://www.linkedin.com/jobs/search"

# ─── LinkedIn's Own Filter Vocabularies ───
# These codes and labels are exactly what linkedin.com uses in its search URLs,
# so a selection here maps 1:1 onto a filter chip on the LinkedIn website.

WORK_TYPES = {"1": "On-site", "2": "Remote", "3": "Hybrid"}

JOB_TYPES = {
    "F": "Full-time", "P": "Part-time", "C": "Contract", "T": "Temporary",
    "I": "Internship", "V": "Volunteer", "O": "Other",
}

EXPERIENCE_LEVELS = {
    "1": "Internship", "2": "Entry level", "3": "Associate",
    "4": "Mid-Senior level", "5": "Director", "6": "Executive",
}

DATE_POSTED = {"r86400": "Past 24 hours", "r604800": "Past week", "r2592000": "Past month"}

# Reverse lookups: LinkedIn's human-readable job criteria -> filter code.
_EMPLOYMENT_TO_CODE = {v.lower(): k for k, v in JOB_TYPES.items()}
_SENIORITY_TO_CODE = {v.lower(): k for k, v in EXPERIENCE_LEVELS.items()}
_WORKPLACE_TO_CODE = {"on-site": "1", "onsite": "1", "remote": "2", "hybrid": "3"}

# ─── Location Handling ───

# Shorthand that LinkedIn's own geo typeahead does NOT understand.
# "UAE" returns nothing and "KSA" returns towns in Morocco, so these must be
# expanded before we ask LinkedIn to resolve them. This only expands an
# abbreviation into its full country name — it never narrows a country to a city.
LOCATION_ALIASES = {
    "uae": "United Arab Emirates",
    "u.a.e": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "emirates": "United Arab Emirates",
    "ksa": "Saudi Arabia",
    "k.s.a": "Saudi Arabia",
    "saudi": "Saudi Arabia",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "britain": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a": "United States",
    "u.s.a.": "United States",
    "us": "United States",
    "america": "United States",
    "holland": "Netherlands",
    "czechia": "Czech Republic",
    "turkiye": "Turkey",
    "türkiye": "Turkey",
    "korea": "South Korea",
    "uae dubai": "Dubai, United Arab Emirates",
    "drc": "Democratic Republic of the Congo",
    "ivory coast": "Côte d'Ivoire",
    "burma": "Myanmar",
    "swaziland": "Eswatini",
}

_GLOBAL_TERMS = {"worldwide", "anywhere", "global", "any", "all", ""}

_geo_cache = {}
_geo_lock = threading.Lock()
_local = threading.local()

# LinkedIn's guest endpoints throttle at roughly 3 requests/second no matter how
# many connections you open. Pacing ourselves just under that keeps responses
# flowing; exceeding it earns 429s that make searches look empty.
MIN_REQUEST_INTERVAL = 0.34
_rate_lock = threading.Lock()
_next_slot = [0.0]

# Job details are immutable for the life of a search session, and searches
# overlap heavily, so caching them removes most repeat traffic.
_details_cache = {}
_details_lock = threading.Lock()
_DETAILS_CACHE_MAX = 3000


def _session() -> requests.Session:
    """One requests.Session per thread — the pools below are multi-threaded."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.session = s
    return s


def _throttle():
    """Global pacer shared by every thread. Reserves a slot, then waits for it."""
    with _rate_lock:
        slot = max(time.monotonic(), _next_slot[0])
        _next_slot[0] = slot + MIN_REQUEST_INTERVAL
    delay = slot - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def _penalise(seconds: float):
    """Pushes every thread's next request back after a rate-limit rejection."""
    with _rate_lock:
        _next_slot[0] = max(_next_slot[0], time.monotonic() + seconds)


def _get(url: str, timeout: int = 12, retries: int = 2):
    """GET with global pacing and backoff. Returns None only if it truly failed."""
    for attempt in range(retries + 1):
        _throttle()
        try:
            r = _session().get(url, timeout=timeout)
            if r.status_code in (429, 999):
                _penalise(3 * (attempt + 1))
                if attempt < retries:
                    continue
                return None
            return r
        except Exception:
            if attempt < retries:
                time.sleep(1)
                continue
            return None
    return None


def resolve_location(location: str):
    """
    Resolves any free-text location to LinkedIn's own canonical name + geoId
    using LinkedIn's public geo typeahead — the same service that powers the
    location box on linkedin.com. Works for every country and city LinkedIn
    knows about, not a hardcoded list.

    Returns (display_name, geo_id). geo_id is None when resolution fails,
    in which case the raw text is still passed through to LinkedIn.
    """
    if not location:
        return "Worldwide", None

    raw = location.strip()
    key = raw.lower()

    if key in _GLOBAL_TERMS:
        return "Worldwide", None
    # "Remote" is a work type, not a place — searching it as a location finds nothing.
    if key == "remote":
        return "Worldwide", None

    query = LOCATION_ALIASES.get(key, raw)

    with _geo_lock:
        if query.lower() in _geo_cache:
            return _geo_cache[query.lower()]

    result = (query, None)
    url = GUEST_TYPEAHEAD + "?" + urllib.parse.urlencode({
        "query": query,
        "typeaheadType": "GEO",
        "geoTypes": "POPULATED_PLACE,ADMIN_DIVISION_2,MARKET_AREA,COUNTRY_REGION",
    })
    r = _get(url, timeout=10)
    if r is not None and r.status_code == 200:
        try:
            hits = r.json()
        except Exception:
            hits = []
        if hits:
            # Prefer an exact name match so "Lebanon" resolves to the country
            # rather than to Lebanon, Tennessee.
            exact = next(
                (h for h in hits if h.get("displayName", "").lower() == query.lower()),
                None,
            )
            best = exact or hits[0]
            if best.get("displayName") and best.get("id"):
                result = (best["displayName"], str(best["id"]))

    with _geo_lock:
        _geo_cache[query.lower()] = result
    return result


def normalize_location(location: str) -> str:
    """Canonical LinkedIn display name for a location (no geoId)."""
    return resolve_location(location)[0]


def _as_codes(value) -> list:
    """Accepts None, 'F', 'F,C' or ['F', 'C'] and returns a clean list of codes."""
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def build_linkedin_url(keywords: str, location: str = "Worldwide", date_posted=None,
                       work_type=None, job_type=None, experience_level=None,
                       easy_apply: bool = False, sort_by: str = None) -> str:
    """
    Builds the equivalent linkedin.com search URL for the same query.

    Opened while signed in to LinkedIn, this URL reproduces the bot's result set
    with every filter applied as a real LinkedIn filter chip — which is what makes
    the two searches directly comparable.
    """
    display, geo_id = resolve_location(location)
    params = {"keywords": keywords}
    if display and display.lower() not in _GLOBAL_TERMS:
        params["location"] = display
    if geo_id:
        params["geoId"] = geo_id
    if date_posted:
        params["f_TPR"] = date_posted
    for key, value in (("f_WT", work_type), ("f_JT", job_type), ("f_E", experience_level)):
        codes = _as_codes(value)
        if codes:
            params[key] = ",".join(codes)
    if easy_apply:
        params["f_AL"] = "true"
    if sort_by:
        params["sortBy"] = sort_by
    return PUBLIC_SEARCH + "?" + urllib.parse.urlencode(params)


# ─── Job Search ───

def search_linkedin_jobs(
    keywords: str,
    location: str = "Worldwide",
    limit: int = 100,
    date_posted: str = None,
    work_type=None,
    job_type=None,
    experience_level=None,
    easy_apply: bool = False,
    strict_location: bool = False,
) -> list:
    """
    Fetches job postings from LinkedIn's guest pagination API.

    Only the parameters LinkedIn actually honours while signed out are sent here
    (keywords, location/geoId, f_TPR, f_AL). LinkedIn silently ignores f_JT,
    f_E and f_WT on this endpoint — passing them changes nothing — so those three
    are applied afterwards by apply_linkedin_filters() against each posting's real
    LinkedIn criteria. work_type / job_type / experience_level are accepted here
    only so callers can hand the whole filter set to one function.
    """
    display, geo_id = resolve_location(location)

    jobs = []
    seen_ids = set()
    start = 0
    page_size = 10   # LinkedIn's guest API returns 10 per call

    base_params = {"keywords": keywords}
    if display and display.lower() not in _GLOBAL_TERMS:
        base_params["location"] = display
    else:
        # An omitted location silently defaults to the United States;
        # the literal string returns a genuinely global mix.
        base_params["location"] = "Worldwide"
    if geo_id:
        base_params["geoId"] = geo_id
    if date_posted:
        base_params["f_TPR"] = date_posted
    if easy_apply:
        base_params["f_AL"] = "true"

    consecutive_empty = 0
    page_failures = 0

    while len(jobs) < limit:
        params = {**base_params, "start": start}
        url = GUEST_SEARCH + "?" + urllib.parse.urlencode(params)

        response = _get(url, timeout=12)

        # A refused request is not the end of the results. Retrying the same
        # offset matters: treating a 429 as "no more jobs" is what made whole
        # searches come back empty.
        if response is None or response.status_code >= 400:
            page_failures += 1
            if page_failures >= 3:
                break
            _penalise(2 * page_failures)
            continue

        if not response.text.strip():
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            start += page_size
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.find_all("li")

        if not items:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            start += page_size
            continue

        consecutive_empty = 0
        page_failures = 0
        new_this_page = 0

        for item in items:
            if len(jobs) >= limit:
                break

            job = _parse_job_card(item, display)
            if not job:
                continue

            if job["job_id"] in seen_ids:
                continue
            seen_ids.add(job["job_id"])

            # Only second-guess LinkedIn's own location scoping when the geo
            # lookup failed and the search text may have been interpreted loosely.
            if strict_location and not geo_id and display.lower() not in _GLOBAL_TERMS:
                if not _location_matches(job["location"], display):
                    continue

            jobs.append(job)
            new_this_page += 1

        if new_this_page == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break

        start += page_size

    return jobs


def _parse_job_card(item, fallback_location: str):
    """Extracts one job from a search-result <li>, or None if it isn't a real posting."""
    title_tag = (
        item.find("span", class_="sr-only") or
        item.find("h3", class_="base-search-card__title") or
        item.find("h3")
    )
    company_tag = (
        item.find("h4", class_="base-search-card__subtitle") or
        item.find("a", {"data-tracking-control-name": "public_jobs_jserp-result_job-search-card-subtitle"}) or
        item.find("h4")
    )
    location_tag = (
        item.find("span", class_="job-search-card__location") or
        item.find("span", class_="job-result-card__location")
    )
    link_tag = (
        item.find("a", class_="base-card__full-link") or
        item.find("a", class_="result-card__full-card-link") or
        item.find("a", href=lambda h: h and "/jobs/view/" in h)
    )
    time_tag = item.find("time")

    if not title_tag or not company_tag:
        return None

    title = title_tag.get_text(strip=True)
    company = company_tag.get_text(strip=True)
    if not title or not company:
        return None

    # Skip promotional/ad cards
    title_lower = title.lower()
    if any(title_lower.startswith(p) for p in ["why work at", "working at", "life at", "careers at"]):
        return None
    if "findem" in title_lower:
        return None

    job_url = ""
    if link_tag and link_tag.get("href"):
        job_url = link_tag["href"].split("?")[0]
    if not job_url or "/jobs/" not in job_url:
        return None

    job_id = _extract_job_id(job_url)
    if not job_id:
        return None

    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": location_tag.get_text(strip=True) if location_tag else fallback_location,
        "url": job_url,
        "posted_date": time_tag.get_text(strip=True) if time_tag else "Recently",
    }


def _extract_job_id(job_url: str) -> str:
    match = re.search(r"(\d{8,12})", job_url or "")
    return match.group(1) if match else ""


def _location_matches(job_location: str, requested: str) -> bool:
    parts = [p.strip().lower() for p in requested.replace(",", " ").split() if len(p.strip()) > 3]
    job_loc = (job_location or "").lower()
    if any(p in job_loc for p in parts):
        return True
    return any(t in job_loc for t in ("remote", "emea", "worldwide"))


# ─── Job Detail Enrichment ───

def fetch_job_details(job_ref) -> dict:
    """
    Fetches a posting's full detail page and returns its description plus the
    LinkedIn criteria block (Seniority level / Employment type / Job function /
    Industries) — the same fields LinkedIn's own f_E and f_JT filters run on.
    """
    empty = {
        "description": "", "seniority_level": None, "employment_type": None,
        "job_function": None, "industries": None, "workplace_type": None,
    }

    job_id = job_ref if isinstance(job_ref, str) and job_ref.isdigit() else _extract_job_id(job_ref)
    if not job_id:
        return empty

    with _details_lock:
        cached = _details_cache.get(job_id)
    if cached is not None:
        return dict(cached)

    res = _get(GUEST_JOB.format(job_id=job_id), timeout=12)
    if res is None or res.status_code != 200:
        return empty

    soup = BeautifulSoup(res.text, "html.parser")
    details = dict(empty)

    desc_div = soup.find("div", class_="show-more-less-html__markup")
    if desc_div:
        details["description"] = desc_div.get_text(separator="\n", strip=True)

    for crit in soup.find_all("li", class_="description__job-criteria-item"):
        header = crit.find("h3")
        value = crit.find("span")
        if not header or not value:
            continue
        label = header.get_text(strip=True).lower()
        text = value.get_text(strip=True)
        if "seniority" in label:
            details["seniority_level"] = text
        elif "employment" in label:
            details["employment_type"] = text
        elif "function" in label:
            details["job_function"] = text
        elif "industr" in label:
            details["industries"] = text

    details["workplace_type"] = _detect_workplace_type(soup, details["description"])

    if details["employment_type"] or details["seniority_level"] or details["description"]:
        with _details_lock:
            if len(_details_cache) >= _DETAILS_CACHE_MAX:
                _details_cache.clear()
            _details_cache[job_id] = dict(details)
    return details


def _detect_workplace_type(soup, description: str):
    """
    Best-effort workplace type. LinkedIn does not publish this in the criteria
    block for signed-out requests, so it is inferred from the top-card flavour
    text and the description. Returns None when it genuinely cannot be told.
    """
    flavors = " ".join(f.get_text(" ", strip=True) for f in soup.find_all(class_=re.compile("topcard__flavor")))
    for haystack in (flavors, description or ""):
        text = haystack.lower()
        if re.search(r"\bhybrid\b", text):
            return "Hybrid"
        if re.search(r"\b(fully remote|remote position|remote role|work from home|100% remote|remote)\b", text):
            return "Remote"
        if re.search(r"\b(on-?site|in-office|in office)\b", text):
            return "On-site"
    return None


def fetch_job_description(job_url: str) -> str:
    """Description only — kept for callers that don't need the criteria block."""
    return fetch_job_details(job_url).get("description", "")


def enrich_jobs(jobs: list, max_workers: int = 8) -> list:
    """
    Fills in description + LinkedIn criteria for each job, in parallel.

    LinkedIn throttles this endpoint to roughly 3 requests/second no matter how
    many workers are used, so 8 is simply the point past which extra threads only
    add contention. Budget about a second per three jobs.
    """
    todo = [j for j in jobs if not j.get("_enriched")]
    if not todo:
        return jobs

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_enrich_one, todo))
    return jobs


def _enrich_one(job: dict) -> dict:
    """Adds a posting's description and LinkedIn criteria to it, once."""
    if job.get("_enriched"):
        return job
    details = fetch_job_details(job.get("job_id") or job.get("url", ""))

    # The search card's location often carries the workplace type LinkedIn omits
    # from the detail page, e.g. "Beirut, Lebanon (Remote)".
    if not details.get("workplace_type"):
        card_location = (job.get("location") or "").lower()
        for label, code in (("hybrid", "Hybrid"), ("remote", "Remote"), ("on-site", "On-site")):
            if label in card_location:
                details["workplace_type"] = code
                break

    job.update(details)
    # No criteria and no description means the fetch gave us nothing usable.
    job["_enriched"] = any(
        details.get(k) for k in ("seniority_level", "employment_type", "description")
    )
    return job


# ─── LinkedIn-Parity Filtering ───

# LinkedIn writes this when the employer left seniority blank. It means "unspecified",
# not "a level you didn't ask for" — and it is very common (27 of 60 in a Lebanon
# sample), so treating it as a mismatch silently deletes half the market.
_SENIORITY_UNSET = {"not applicable", "not specified", "n/a", ""}


def _matches_filters(job: dict, wt_codes, jt_codes, exp_codes):
    """
    Returns True (keep), False (contradicts the filter) or None (LinkedIn wouldn't say).

    Job type and seniority come straight from the posting's criteria block — the same
    fields the site's own filters read, and present on ~98% of postings. Work type is
    different: LinkedIn never publishes it to signed-out requests, so it can only ever
    be inferred. An inferred blank must not exclude anything, or selecting a work type
    would quietly discard nearly every result.
    """
    unknown = False

    if jt_codes:
        declared = job.get("employment_type")
        if not declared:
            unknown = True
        elif _EMPLOYMENT_TO_CODE.get(declared.lower()) not in jt_codes:
            return False

    if exp_codes:
        declared = (job.get("seniority_level") or "").strip()
        if not declared or declared.lower() in _SENIORITY_UNSET:
            unknown = True
        elif _SENIORITY_TO_CODE.get(declared.lower()) not in exp_codes:
            return False

    if wt_codes:
        code = _WORKPLACE_TO_CODE.get((job.get("workplace_type") or "").lower())
        if code is None:
            # Undetectable: not grounds for dropping the job, but not grounds for
            # calling it a confirmed match either.
            unknown = True
        elif code not in wt_codes:
            return False

    return None if unknown else True


def apply_linkedin_filters(jobs: list, work_type=None, job_type=None, experience_level=None,
                           max_checks: int = 60, max_workers: int = 8):
    """
    Applies the Work type / Job type / Experience level filters that LinkedIn
    ignores on its signed-out endpoints.

    Each posting is judged on its own LinkedIn criteria — the exact fields the
    site filters on when you tick those boxes while signed in — instead of on
    guesses made from the job title. Several codes within one filter are OR'd and
    the filters are AND'd, matching LinkedIn's behaviour.

    Postings that contradict a filter are dropped. Postings LinkedIn won't describe
    are kept but ranked after the confirmed ones and flagged 'filter_uncertain', so
    the user still sees every real job while nothing unconfirmed is passed off as a
    verified match.

    Returns (jobs, stats), confirmed matches first.
    """
    wt_codes = _as_codes(work_type)
    jt_codes = _as_codes(job_type)
    exp_codes = _as_codes(experience_level)

    stats = {"total": len(jobs), "checked": 0, "matched": 0, "unverified": 0}
    if not (wt_codes or jt_codes or exp_codes) or not jobs:
        return jobs, stats

    candidates = jobs[:max_checks]
    kept, unverified = [], []

    # Lookups are submitted up front so they overlap, but results are consumed in
    # LinkedIn's own result order so its ranking survives.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [(job, pool.submit(_enrich_one, job)) for job in candidates]
        for job, future in futures:
            try:
                future.result()
            except Exception:
                pass
            stats["checked"] += 1

            verdict = _matches_filters(job, wt_codes, jt_codes, exp_codes)
            if verdict is True:
                kept.append(job)
            elif verdict is None:
                job["filter_uncertain"] = True
                unverified.append(job)

    stats["matched"] = len(kept)
    stats["unverified"] = len(unverified)
    stats["dropped"] = stats["checked"] - len(kept) - len(unverified)

    return kept + unverified, stats


def filter_hard_qualifications(jobs: list, keywords: str, job_type: str = None,
                               experience_level: str = None, work_type: str = None) -> list:
    """
    Deprecated — kept so older call sites keep working.

    This used to drop jobs by guessing seniority from words in the title, which is
    why bot results diverged from LinkedIn's: LinkedIn filters on a posting's
    declared criteria, not its title. Use apply_linkedin_filters() instead.
    """
    return jobs
