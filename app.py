import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

import urllib.request
import urllib.parse
import json
from flask import Flask, render_template, request, session

from extract_jobs import DEFAULT_URLS, extract_jobs, extract_newgrad_jobs

app = Flask(__name__)
app.secret_key = "hr-system-applied-tracker-key"
CACHE_TTL = timedelta(minutes=5)
# ---------------------------------------------------------------------------
# Relevance scoring
# Each pattern tuple: (compiled_regex, score_weight)
# A job title + source text is scored; >= 3 = relevant.
# Strong matches (exact tech role names) score +4 each hit.
# Weak signals (e.g. standalone word "data") score +1.
# Firm non-tech terms score -6, which overrides any weak positive.
# ---------------------------------------------------------------------------
_STRONG_KEYWORDS = "|".join([
    # --- Machine Learning / AI ---
    r"machine learning", r"deep learning", r"reinforcement learning",
    r"artificial intelligence", r"ai/ml", r"mlops", r"llmops",
    r"large language model", r"llm", r"generative ai", r"gen ai",
    r"computer vision", r"natural language processing", r"nlp",
    r"speech recognition", r"recommendation system", r"feature engineering",
    r"model training", r"model evaluation", r"applied scientist",
    r"research scientist", r"ml engineer", r"ai engineer", r"ai researcher",
    r"ai infrastructure", r"foundation model", r"rag",
    r"prompt engineer", r"fine.?tun",
    # --- Data Science / Analytics ---
    r"data scientist", r"data science",
    r"data analyst", r"senior analyst",
    r"analytics engineer", r"quantitative analyst", r"quant analyst",
    r"business intelligence", r"bi engineer", r"bi developer",
    r"reporting analyst", r"insights analyst",
    r"statistical model", r"statistician",
    r"a/b test", r"experimentation engineer",
    # --- Data Engineering / Architecture ---
    r"data engineer", r"data engineering",
    r"data architect", r"data platform",
    r"etl", r"elt", r"pipeline engineer",
    r"lakehouse", r"data lake", r"data warehouse", r"data mesh",
    r"streaming engineer", r"kafka engineer",
    r"spark engineer", r"dbt", r"airflow",
    r"database engineer", r"database administrator", r"dba",
    # --- Software Engineering ---
    r"software engineer", r"software developer", r"software engineering",
    r"software architect", r"principal engineer",
    r"full[ -]?stack", r"front[ -]?end", r"back[ -]?end",
    r"mobile engineer", r"ios engineer", r"android engineer",
    r"embedded engineer", r"embedded software",
    r"firmware engineer",
    r"api engineer", r"sdk engineer",
    r"staff engineer", r"distinguished engineer",
    # --- Infrastructure / Platform / DevOps ---
    r"platform engineer", r"infrastructure engineer",
    r"site reliability engineer", r"\bsre\b",
    r"devops", r"devsecops", r"cloud engineer",
    r"cloud architect", r"solutions architect",
    r"kubernetes", r"\bk8s\b", r"docker",
    r"ci/cd", r"build engineer", r"release engineer",
    r"distributed systems", r"systems engineer",
    r"storage engineer", r"network engineer", r"network architect",
    r"gpu infrastructure", r"hpc engineer",
    # --- Security / Compliance Engineering ---
    r"security engineer", r"application security", r"appsec",
    r"cybersecurity", r"cyber security", r"information security",
    r"devsecops", r"penetration test", r"pentest",
    r"identity engineer", r"iam engineer", r"zero trust",
    r"threat intelligence",
    # --- Technical Leadership / Management ---
    r"engineering manager", r"principal scientist",
    r"director of engineering", r"vp of engineering",
    r"technical program manager", r"technical project manager",
    r"it systems", r"it engineer", r"it architect",
    # --- Product / UX Engineering ---
    r"product engineer", r"growth engineer",
    r"ux engineer", r"ui engineer",
])

TECH_INCLUDE_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(rf"\b({_STRONG_KEYWORDS})\b", re.IGNORECASE), 4),
    # Weaker lone-word signals — still need a strong match unless stacked
    (re.compile(r"\b(ai|ml|data|software|platform|cloud|automation|infrastructure|python|sql|scala|spark|golang|rust|java|typescript|kubernetes|devops|analytics|modeling|algorithm)\b", re.IGNORECASE), 1),
]

TECH_EXCLUDE_PATTERN = re.compile(
    "|".join([
        r"\bsales associate\b", r"\bculinary\b", r"\bdishwasher\b", r"\bsteward\b",
        r"\bpastry\b", r"\brestaurant\b", r"\bchef\b", r"\bcashier\b",
        r"\bretail\b", r"\bstore manager\b", r"\bstore associate\b",
        r"\bparalegal\b", r"\blegal counsel\b", r"\battorney\b", r"\bcounsel\b",
        r"\bcorporate communications\b", r"\bpublic relations\b", r"\bpr manager\b",
        r"\bmarketing manager\b", r"\bbrand manager\b", r"\bcontent strategist\b",
        r"\bevent coordinator\b", r"\bworkplace experience\b",
        r"\bfinancial analyst\b", r"\bfinancial advisor\b", r"\bportfolio manager\b",
        r"\btax\b", r"\baccountant\b", r"\baccounting\b", r"\baudit\b", r"\bpayroll\b",
        r"\bhuman resources\b", r"\bhr business partner\b", r"\brecruiter\b",
        r"\btalent acquisition\b", r"\blearning.*development\b",
        r"\bnurse\b", r"\bphysician\b", r"\bmedical assistant\b", r"\bpharmacist\b",
        r"\bdentist\b", r"\btherapist\b", r"\bclinical\b",
        r"\bmechanic\b", r"\bmanufacturing technician\b", r"\bproduction operator\b",
        r"\bassembly technician\b", r"\bquality inspector\b",
        r"\bfacilities\b", r"\bcustodian\b", r"\bjanitorial\b",
        r"\bsecurity officer\b", r"\bsecurity guard\b",
        r"\bsupply chain\b", r"\bprocurement\b", r"\blogistics\b",
        r"\bbox office\b", r"\bguestroom\b", r"\bfront desk\b", r"\bconcierge\b",
        r"\bhousekeeping\b",
    ]),
    re.IGNORECASE,
)
_cache: Dict[str, Any] = {
    "jobs": [],
    "errors": [],
    "updated_at": None,
    "urls": DEFAULT_URLS,
}
_cache_lock = threading.Lock()
_cache_loading = False


def parse_posted_date(posted: str) -> Optional[datetime]:
    if not posted:
        return None

    text = posted.strip()
    text = text.replace("Date Posted:", "").replace("Posted:", "").strip()
    text = text.replace("Posted", "").replace("Date Posted", "").strip()

    now = datetime.now()
    relative_match = None
    if "today" in text.lower():
        return now
    if "yesterday" in text.lower():
        return now - timedelta(days=1)

    # Handle "a day ago", "an hour ago", "a month ago" etc. by substituting "1"
    text = re.sub(r'\ba\b(?=\s+(second|minute|hour|day|week|month|year))', '1', text, flags=re.IGNORECASE)
    text = re.sub(r'\ban\b(?=\s+(second|minute|hour|day|week|month|year))', '1', text, flags=re.IGNORECASE)

    relative_match = None
    for pattern in [
        (r"(\d+)\s+days?\s+ago", lambda value: now - timedelta(days=int(value))),
        (r"(\d+)\s+hours?\s+ago", lambda value: now - timedelta(hours=int(value))),
        (r"(\d+)\s+hrs?\s+ago", lambda value: now - timedelta(hours=int(value))),
        (r"(\d+)\s+minutes?\s+ago", lambda value: now - timedelta(minutes=int(value))),
        (r"(\d+)\s+weeks?\s+ago", lambda value: now - timedelta(weeks=int(value))),
        (r"(\d+)\s+months?\s+ago", lambda value: now - timedelta(days=int(value) * 30)),
        (r"(\d+)\s+years?\s+ago", lambda value: now - timedelta(days=int(value) * 365)),
    ]:
        try:
            match = re.search(pattern[0], text, re.IGNORECASE)
            if match:
                return pattern[1](match.group(1))
        except Exception:
            continue

    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y"]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def source_name(url: str) -> str:
    hostname = urlparse(url).hostname or "unknown"
    return hostname.replace("www.", "")


def normalize_job_text(*parts: str) -> str:
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip().lower()


def score_job_relevance(title: str, *, source: str = "", source_url: str = "") -> int:
    text = normalize_job_text(title, source, source_url)
    score = 0
    for pattern, weight in TECH_INCLUDE_PATTERNS:
        if pattern.search(text):
            score += weight
    if TECH_EXCLUDE_PATTERN.search(text):
        score -= 6
    return score


def is_relevant_job(title: str, *, source: str = "", source_url: str = "") -> bool:
    return score_job_relevance(title, source=source, source_url=source_url) >= 3


_LOCATION_US = re.compile(
    r"\b(united states|usa|us|u\.s\.a?|america|alabama|alaska|arizona|arkansas|california|colorado|"
    r"connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    r"louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|"
    r"nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|"
    r"ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    r"\bdc\b|district of columbia|san francisco|seattle|austin|boston|chicago|dallas|denver|"
    r"los angeles|new york city|nyc|atlanta|miami|portland|phoenix|san diego|san jose|raleigh|"
    r"minneapolis|detroit|charlotte|nashville|houston|philadelphia|pittsburgh|salt lake)\b",
    re.IGNORECASE,
)
_LOCATION_CA = re.compile(
    r"\b(canada|canadian|ontario|quebec|british columbia|alberta|saskatchewan|manitoba|"
    r"nova scotia|new brunswick|newfoundland|prince edward island|northwest territories|nunavut|yukon|"
    r"toronto|montreal|vancouver|calgary|edmonton|ottawa|winnipeg|hamilton|kitchener|waterloo|"
    r"richmond hill|markham|mississauga)\b",
    re.IGNORECASE,
)
_LOCATION_REMOTE = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.IGNORECASE)
_LOCATION_MULTIPLE = re.compile(r"\b(multiple|various|several)\b", re.IGNORECASE)


def location_allowed(location: str, location_filter: str) -> bool:
    """Return True when the job's location matches the active location filter."""
    if location_filter == "any":
        return True
    loc = (location or "").strip()
    if not loc:
        # Unknown location — keep it so we don't accidentally hide legit remote roles
        return True
    if _LOCATION_MULTIPLE.search(loc):
        return True
    if _LOCATION_REMOTE.search(loc):
        return True
    if _LOCATION_US.search(loc):
        return True
    if _LOCATION_CA.search(loc):
        return True
    return False


def job_matches_filters(
    job: Dict[str, Any],
    *,
    filter_mode: str,
    search_query: str,
    location_filter: str = "na_remote",
) -> bool:
    if filter_mode == "relevant" and not job.get("is_relevant", True):
        return False

    if location_filter != "any" and not location_allowed(job.get("location", ""), location_filter):
        return False

    if search_query:
        haystack = normalize_job_text(
            job.get("title", ""),
            job.get("location", ""),
            job.get("source", ""),
        )
        for token in normalize_job_text(search_query).split():
            if token not in haystack:
                return False

    return True


def fetch_jobs(urls: List[str]) -> Dict[str, Any]:
    jobs: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for url in urls:
        try:
            data = extract_jobs(url)
            source = source_name(url)
            for job in data.get("jobs", []):
                posted_text = job.get("posted", "") or ""
                job_date = parse_posted_date(posted_text)
                jobs.append(
                    {
                        "title": job.get("title", "(no title)"),
                        "location": job.get("location", ""),
                        "posted": posted_text,
                        "url": job.get("url", ""),
                        "source": source,
                        "posted_date": job_date,
                        "source_url": url,
                        "relevance_score": score_job_relevance(
                            job.get("title", ""),
                            source=source,
                            source_url=url,
                        ),
                        "is_relevant": is_relevant_job(
                            job.get("title", ""),
                            source=source,
                            source_url=url,
                        ),
                    }
                )
        except Exception as exc:
            errors.append({"url": url, "message": str(exc)})

    jobs.sort(key=lambda item: (item["posted_date"] or datetime.min), reverse=True)
    return {"jobs": jobs, "errors": errors}


def _fetch_one(url: str) -> tuple[str, list, str | None]:
    """Fetch jobs for a single URL. Returns (url, new_jobs, error_message)."""
    source = source_name(url)
    try:
        data = extract_jobs(url)
        new_jobs = []
        for job in data.get("jobs", []):
            posted_text = job.get("posted", "") or ""
            job_date = parse_posted_date(posted_text)
            posted_normalized = job_date.strftime("%Y-%m-%d") if job_date else posted_text
            relevance_score = score_job_relevance(
                job.get("title", ""),
                source=source,
                source_url=url,
            )
            new_jobs.append({
                "title": job.get("title", "(no title)"),
                "location": job.get("location", ""),
                "posted": posted_normalized,
                "url": job.get("url", ""),
                "source": source,
                "posted_date": job_date,
                "source_url": url,
                "relevance_score": relevance_score,
                "is_relevant": relevance_score >= 3,
            })
        return url, new_jobs, None
    except Exception as exc:
        return url, [], str(exc)


def _run_fetch() -> None:
    global _cache_loading
    try:
        urls = list(_cache["urls"])
        all_new_jobs: List[Dict[str, Any]] = []
        all_errors: List[Dict[str, str]] = []

        # Run all scrapers concurrently; old cached jobs stay visible until done
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_one, url): url for url in urls}
            for future in as_completed(futures):
                url, new_jobs, error = future.result()
                if error:
                    all_errors.append({"url": url, "message": error})
                else:
                    all_new_jobs.extend(new_jobs)

        all_new_jobs.sort(
            key=lambda item: (item["posted_date"] or datetime.min),
            reverse=True,
        )

        # Atomic swap — replaces stale data all at once
        with _cache_lock:
            _cache["jobs"] = all_new_jobs
            _cache["errors"] = all_errors
            _cache["updated_at"] = datetime.now()
    finally:
        with _cache_lock:
            _cache_loading = False


def start_fetch_jobs() -> None:
    global _cache_loading
    with _cache_lock:
        if _cache_loading:
            return
        _cache_loading = True

    thread = threading.Thread(target=_run_fetch, daemon=True)
    thread.start()


@app.route("/")
def index() -> str:
    refresh = request.args.get("refresh", "0") == "1"
    filter_mode = request.args.get("filter", "relevant").strip().lower()
    if filter_mode not in {"all", "relevant"}:
        filter_mode = "relevant"
    location_filter = request.args.get("loc", "na_remote").strip().lower()
    if location_filter not in {"na_remote", "any"}:
        location_filter = "na_remote"
    search_query = request.args.get("q", "").strip()
    now = datetime.now()
    with _cache_lock:
        updated_at = _cache["updated_at"]
        expired = not updated_at or now - updated_at > CACHE_TTL
        loading = _cache_loading
        jobs = list(_cache["jobs"])
        errors = list(_cache["errors"])
        urls = list(_cache["urls"])

    triggered_fetch = False
    if refresh or not jobs or expired:
        start_fetch_jobs()
        triggered_fetch = True

    filtered_jobs = [
        job for job in jobs
        if job_matches_filters(
            job,
            filter_mode=filter_mode,
            search_query=search_query,
            location_filter=location_filter,
        )
    ]

    return render_template(
        "index.html",
        jobs=filtered_jobs,
        errors=errors,
        updated_at=updated_at,
        urls=urls,
        cache_ttl_minutes=int(CACHE_TTL.total_seconds() / 60),
        loading=_cache_loading or triggered_fetch,
        total_job_count=len(jobs),
        relevant_job_count=sum(1 for job in jobs if job.get("is_relevant", True)),
        filter_mode=filter_mode,
        location_filter=location_filter,
        search_query=search_query,
    )



# ---------------------------------------------------------------------------
# New-grad tab — separate cache so it doesn't block the main dashboard fetch
# ---------------------------------------------------------------------------
_newgrad_cache: Dict[str, Any] = {"jobs": [], "errors": [], "updated_at": None}
_newgrad_cache_lock = threading.Lock()
_newgrad_cache_loading = False
NEWGRAD_CACHE_TTL = timedelta(minutes=30)   # Airtable doesn't change as fast


def _run_newgrad_fetch() -> None:
    global _newgrad_cache_loading
    try:
        result = extract_newgrad_jobs()
        with _newgrad_cache_lock:
            _newgrad_cache["jobs"] = result["jobs"]
            _newgrad_cache["errors"] = result["errors"]
            _newgrad_cache["updated_at"] = datetime.now()
    except Exception as exc:
        with _newgrad_cache_lock:
            _newgrad_cache["errors"] = [str(exc)]
    finally:
        with _newgrad_cache_lock:
            _newgrad_cache_loading = False


def start_newgrad_fetch() -> None:
    global _newgrad_cache_loading
    with _newgrad_cache_lock:
        if _newgrad_cache_loading:
            return
        _newgrad_cache_loading = True
    threading.Thread(target=_run_newgrad_fetch, daemon=True).start()


@app.route("/newgrad")
def newgrad() -> str:
    refresh = request.args.get("refresh", "0") == "1"
    search_query = request.args.get("q", "").strip()
    category_filter = request.args.get("cat", "all").strip().lower()
    now = datetime.now()

    with _newgrad_cache_lock:
        updated_at = _newgrad_cache["updated_at"]
        expired = not updated_at or now - updated_at > NEWGRAD_CACHE_TTL
        loading = _newgrad_cache_loading
        jobs = list(_newgrad_cache["jobs"])
        errors = list(_newgrad_cache["errors"])

    triggered_fetch = False
    if refresh or not jobs or expired:
        start_newgrad_fetch()
        triggered_fetch = True

    # Collect distinct category labels for the filter dropdown
    all_categories = sorted({job.get("category", "") for job in jobs if job.get("category")})

    # Apply filters
    filtered_jobs = jobs
    if category_filter != "all":
        filtered_jobs = [j for j in filtered_jobs if j.get("category", "").lower() == category_filter]
    if search_query:
        sq = search_query.lower()
        filtered_jobs = [
            j for j in filtered_jobs
            if sq in (j.get("title", "") + " " + j.get("company", "") + " " + j.get("location", "")).lower()
        ]

    # Sort newest first — posted is YYYY-MM-DD so lexicographic sort works
    filtered_jobs.sort(key=lambda j: j.get("posted") or "", reverse=True)

    return render_template(
        "newgrad.html",
        jobs=filtered_jobs,
        errors=errors,
        updated_at=updated_at,
        loading=_newgrad_cache_loading or triggered_fetch,
        total_job_count=len(jobs),
        all_categories=all_categories,
        category_filter=category_filter,
        search_query=search_query,
    )


# ---------------------------------------------------------------------------
# Applied jobs tracker — reads from Microsoft Graph (user's mailbox)
# ---------------------------------------------------------------------------

# Subject-line patterns for classifying incoming emails
_REJECTION_SUBJECTS = re.compile(
    # Explicit negative language
    r"unfortunately|we\s+regret|we\s+are\s+sorry|we\s+appreciate\s+your\s+(interest|time)|"
    r"not\s+moving\s+forward|not\s+selected|not\s+a\s+(match|fit)|"
    r"will\s+not\s+be\s+moving|did\s+not\s+select|decided\s+not\s+to\s+(move|proceed|continue)|"
    r"no\s+longer\s+(consider|moving|pursuing)|"
    # Pursuing / choosing other candidates
    r"other\s+candidates|pursuing\s+other|chosen\s+(another|other|not\s+to)|"
    r"moving\s+forward\s+with\s+(other|another|different)|"
    r"selected\s+(other|another|a\s+different)|"
    # Position / role related
    r"position\s+has\s+been\s+(filled|closed|cancelled|canceled)|"
    r"(closing|closed|cancell?ed|pausing|putting\s+on\s+hold)\s+(the\s+)?(position|role|opening|search|req)|"
    r"role\s+has\s+been\s+(filled|closed|cancelled)|"
    # Skills/qualifications framing (still a no)
    r"although\s+(your|we)|impressed\s+with\s+your|skills\s+were\s+impressive|"
    r"while\s+your\s+(background|experience|qualifications?|skills?)|"
    r"after\s+(careful|thorough|much)\s+(consideration|review|deliberation)|"
    r"after\s+reviewing\s+your|having\s+reviewed\s+your|"
    # Unable to offer
    r"unable\s+to\s+(offer|extend|move|proceed)|cannot\s+offer|"
    r"not\s+able\s+to\s+(offer|move|proceed|extend)|"
    r"not\s+to\s+move\s+forward\s+with|decided\s+not\s+to\s+move\s+forward|"
    r"not\s+moving\s+forward\s+with\s+your|"
    # Application update / status language
    r"application\s+(update|status|decision|outcome)|update\s+on\s+your\s+application|"
    r"regarding\s+your\s+application|your\s+application\s+status|"
    # Wished them well framing
    r"wish\s+you\s+(all\s+the\s+)?best|best\s+of\s+luck\s+in\s+your|future\s+(success|endeavors?|opportunities?)|"
    # Thank you for your interest (standalone rejection — not 'in a role/position')
    r"thank\s+you\s+for\s+your\s+interest(?!\s+in\s+(a\s+)?(role|position|job|opportunity|working))",
    re.IGNORECASE,
)
_INTERVIEW_SUBJECTS = re.compile(
    r"\binterview\b|phone\s+screen|video\s+(call|interview)|"
    r"coding\s+(challenge|assessment|test)|take[\s-]?home|"
    r"technical\s+(assessment|screen|interview|round|challenge)|"

    r"schedule\s+(a\s+)?(call|meeting|time)|meet\s+with|"
    r"hiring\s+manager|assessment\s+invitation|skills\s+assessment",
    re.IGNORECASE,
)
_APPLICATION_SUBJECTS = re.compile(
    r"thank\s+you\s+for\s+(applying|your\s+application)|"
    r"thank\s+you\s+for\s+your\s+interest\s+in\s+(a\s+)?(role|position|job|opportunity)|"
    r"application\s+(received|submitted|confirmed|complete|on\s+file|confirmation)|"
    r"we\s+received\s+your\s+application|successfully\s+applied|"
    r"your\s+application\s+(to|for|has\s+been|was\s+received)|"
    r"submission\s+confirmed|applied\s+to\b|you.ve\s+applied|"
    r"application\s+for\s+the\s+position",
    re.IGNORECASE,
)
# Broad signal: subject mentions apply/application — used as a fallback when
# no stronger pattern matches, to catch generic ATS confirmation emails.
_APPLICATION_SIGNAL = re.compile(
    r"\b(appl(y|ied|ication|ications?)|your\s+application|application\s+for|for\s+the\s+(role|position|job))\b",
    re.IGNORECASE,
)
_ATS_SENDERS = re.compile(
    r"greenhouse\.io|lever\.co|workday\.com|icims\.com|jobvite\.com"
    r"|smartrecruiters\.com|taleo\.net|successfactors\.com|myworkdayjobs\.com"
    r"|linkedin\.com|indeed\.com|ziprecruiter\.com|glassdoor\.com"
    r"|careers?@|recruiting@|talent@|no.?reply.*career|noreply.*job",
    re.IGNORECASE,
)

# Subjects that look like job-board marketing/alerts — discard before classifying
_DISCARD_SUBJECTS = re.compile(
    r"new\s+job\s+(alert|match|recommendation|opening|posting|opportunit)|"
    r"jobs?\s+(you\s+might|matching|near|alert|recommendation|update|digest|weekly|daily)|"
    r"recommended\s+jobs?|suggested\s+jobs?|top\s+jobs?|trending\s+jobs?|"
    r"jobs?\s+based\s+on|similar\s+jobs?|\d+\s+new\s+jobs?|"
    r"job\s+(digest|newsletter|roundup|listing|update)|unsubscribe|"
    r"your\s+job\s+(search|alert|digest|feed)|salary\s+(report|insight|alert)|"
    r"profile\s+(view|update)|recruiter\s+(view|found\s+you|is\s+interested)|"
    r"^\[github\]|oauth\s+application|has\s+been\s+added\s+to\s+your\s+account|"
    r"third.party\s+(oauth|github|app)",
    re.IGNORECASE,
)
# Calendar system noise — invitations, acceptances, reminders, cancellations
_CALENDAR_SUBJECTS = re.compile(
    r"^(invitation|accepted|declined|tentative|canceled\s+event|cancelled\s+event|reminder|updated\s+invitation):\s",
    re.IGNORECASE,
)


def _graph_request(access_token: str, path: str) -> dict:
    """Make a GET request to Microsoft Graph and return parsed JSON."""
    url = f"https://graph.microsoft.com/v1.0{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Graph API {exc.code}: {body[:300]}") from exc


_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes for thread deduplication."""
    s = subject.strip()
    while True:
        stripped = re.sub(r'^(Re|Fwd|FW|RE|FWD|AW):\s*', '', s, flags=re.IGNORECASE).strip()
        if stripped == s:
            break
        s = stripped
    return s.lower()


def _classify_email(subject: str, sender_addr: str) -> Optional[str]:
    """Return 'rejected', 'interview', 'applied', or None (discard)."""
    # Discard calendar system emails (invitations, reminders, acceptances)
    if _CALENDAR_SUBJECTS.search(subject):
        return None
    # Discard job alerts, newsletters, and marketing emails
    if _DISCARD_SUBJECTS.search(subject):
        return None
    # Rejection takes highest priority — a rejection is never an "application"
    if _REJECTION_SUBJECTS.search(subject):
        return "rejected"
    if _INTERVIEW_SUBJECTS.search(subject):
        return "interview"
    if _APPLICATION_SUBJECTS.search(subject):
        return "applied"
    # Fallback: if "apply/application" appears in the subject, use it as a
    # weaker signal and re-check the stronger patterns before defaulting to applied.
    if _APPLICATION_SIGNAL.search(subject):
        # Could still be a rejection or interview phrased differently
        if _REJECTION_SUBJECTS.search(subject):
            return "rejected"
        if _INTERVIEW_SUBJECTS.search(subject):
            return "interview"
        return "applied"
    return None


_LOOKBACK_DAYS = (datetime.utcnow() - datetime(2026, 1, 1)).days  # from Jan 1 2026
_CHUNK_DAYS  = 90      # each parallel date-range fetch covers 90 days

# Targeted KQL phrases — each runs as a separate parallel $search
_GRAPH_SEARCHES = [
    '"thank you for applying" OR "application received" OR "application submitted" OR "successfully applied"',
    '"your application" OR "application confirmation" OR "you\'ve applied" OR "submission confirmed"',
    '"interview" OR "phone screen" OR "technical assessment" OR "coding challenge"',
    '"unfortunately" OR "not moving forward" OR "not selected" OR "we regret" OR "other candidates"',
    '"position has been filled" OR "pursuing other" OR "unable to offer" OR "after careful consideration" OR "not to move forward"',
    '"although your" OR "while your background" OR "closing the position" OR "role has been filled" OR "best of luck" OR "thank you for your interest"',
    '"application update" OR "update on your application" OR "your application status" OR "application decision"',
]


def _graph_search_messages(access_token: str, query: str) -> list[dict]:
    """$search-based fetch — inbox only, relevance-ranked, up to 3 pages (300 results)."""
    encoded_q = urllib.parse.quote(query)
    path: Optional[str] = (
        f"/me/mailFolders/inbox/messages"
        f"?$search={encoded_q}"
        f"&$top=100"
        f"&$select=id,subject,receivedDateTime,from,webLink"
    )
    messages: list[dict] = []
    pages = 0
    while path and pages < 3:
        data = _graph_request(access_token, path)
        messages.extend(data.get("value", []))
        pages += 1
        next_link: str = data.get("@odata.nextLink", "")
        if next_link and next_link.startswith(_GRAPH_BASE):
            path = next_link[len(_GRAPH_BASE):]
        else:
            path = None
    return messages


def _graph_fetch_date_chunk(access_token: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """$filter fetch for a specific date range from inbox, up to 5 pages (500 emails)."""
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    filter_val  = urllib.parse.quote(f"receivedDateTime ge {start_str} and receivedDateTime lt {end_str}")
    orderby_val = urllib.parse.quote("receivedDateTime desc")
    path: Optional[str] = (
        f"/me/mailFolders/inbox/messages"
        f"?$filter={filter_val}"
        f"&$orderby={orderby_val}"
        f"&$select=id,subject,receivedDateTime,from,webLink"
        f"&$top=100"
    )
    messages: list[dict] = []
    pages = 0
    while path and pages < 5:
        data = _graph_request(access_token, path)
        messages.extend(data.get("value", []))
        pages += 1
        next_link: str = data.get("@odata.nextLink", "")
        if next_link and next_link.startswith(_GRAPH_BASE):
            path = next_link[len(_GRAPH_BASE):]
        else:
            path = None
    return messages


def _date_chunks(lookback_days: int, chunk_days: int) -> list[tuple[datetime, datetime]]:
    """Split the lookback window into (start, end) pairs of chunk_days each."""
    now = datetime.utcnow()
    chunks = []
    end = now
    while end > now - timedelta(days=lookback_days):
        start = max(end - timedelta(days=chunk_days), now - timedelta(days=lookback_days))
        chunks.append((start, end))
        end = start
    return chunks


# Per-token result cache: token_hash -> (results, expires_at)
_applied_cache: Dict[str, Any] = {}
_applied_cache_lock = threading.Lock()
_APPLIED_CACHE_TTL = timedelta(minutes=5)


def fetch_applied_jobs(access_token: str) -> list[dict[str, Any]]:
    """Search the user's mailbox for job-related emails, categorized by type.

    Runs 4 targeted $search queries + 1 date-filtered recent fetch all in
    parallel. $search covers 2 years by relevance; the recent $filter
    guarantees nothing from the last 60 days is missed. Results are
    cached per token for 5 minutes.
    """
    import hashlib
    token_key = hashlib.sha256(access_token.encode()).hexdigest()
    now = datetime.now()

    with _applied_cache_lock:
        cached = _applied_cache.get(token_key)
        if cached and cached["expires_at"] > now:
            return cached["results"]

    all_messages: list[dict] = []
    cutoff_dt = datetime.utcnow() - timedelta(days=_LOOKBACK_DAYS)
    chunks = _date_chunks(_LOOKBACK_DAYS, _CHUNK_DAYS)

    # keyword $searches + chunked date-range $filter fetches, all in parallel
    n_workers = len(_GRAPH_SEARCHES) + len(chunks)
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        search_futures = [
            executor.submit(_graph_search_messages, access_token, q)
            for q in _GRAPH_SEARCHES
        ]
        chunk_futures = [
            executor.submit(_graph_fetch_date_chunk, access_token, start, end)
            for start, end in chunks
        ]
        for f in as_completed(search_futures + chunk_futures):
            try:
                all_messages.extend(f.result())
            except RuntimeError:
                pass

    seen_ids: set[str] = set()
    results: list[dict[str, Any]] = []
    for msg in all_messages:
        msg_id = msg.get("id", "")
        if msg_id in seen_ids:
            continue
        subject = msg.get("subject", "") or ""
        sender_addr = (msg.get("from", {}).get("emailAddress", {}).get("address") or "")
        sender_name = (msg.get("from", {}).get("emailAddress", {}).get("name") or "")
        category = _classify_email(subject, sender_addr)
        if category is None:
            continue
        # Drop messages older than the lookback window (from $search results)
        received_raw = msg.get("receivedDateTime", "")
        try:
            received_dt = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
            if received_dt.replace(tzinfo=None) < cutoff_dt:
                continue
            received = received_dt.strftime("%Y-%m-%d %H:%M")
            received_sort = received_dt.isoformat()
        except Exception:
            received = received_raw[:10]
            received_sort = received_raw
        seen_ids.add(msg_id)
        results.append({
            "subject": subject,
            "company": sender_name,
            "sender": sender_addr,
            "received": received,
            "received_sort": received_sort,
            "link": msg.get("webLink", ""),
            "category": category,
        })

    results.sort(key=lambda m: m.get("received_sort", ""), reverse=True)

    # Deduplicate email threads: same normalized subject + sender domain → keep most recent
    seen_thread: dict[tuple, bool] = {}
    deduped: list[dict[str, Any]] = []
    for r in results:
        sender = r["sender"].lower()
        domain = sender.split("@")[-1] if "@" in sender else sender
        key = (_normalize_subject(r["subject"]), domain)
        if key not in seen_thread:
            seen_thread[key] = True
            deduped.append(r)
    results = deduped

    with _applied_cache_lock:
        _applied_cache[token_key] = {"results": results, "expires_at": now + _APPLIED_CACHE_TTL}

    return results


@app.route("/applied", methods=["GET", "POST"])
def applied() -> str:
    error: str = ""
    jobs: list[dict] = []
    search_query = request.args.get("q", "").strip()
    category_filter = request.args.get("cat", "all")
    token_submitted = ""

    if request.method == "POST":
        token_submitted = (request.form.get("access_token") or "").strip()
        if token_submitted:
            # Store only in server-side session (never echoed back to client)
            session["graph_token"] = token_submitted

    access_token: str = session.get("graph_token", "")

    if request.args.get("clear_token"):
        session.pop("graph_token", None)
        access_token = ""
        # Also evict the cache for this token
        import hashlib
        if access_token:
            token_key = hashlib.sha256(access_token.encode()).hexdigest()
            with _applied_cache_lock:
                _applied_cache.pop(token_key, None)

    if request.args.get("refresh"):
        stored = session.get("graph_token", "")
        if stored:
            import hashlib
            token_key = hashlib.sha256(stored.encode()).hexdigest()
            with _applied_cache_lock:
                _applied_cache.pop(token_key, None)

    if access_token:
        try:
            jobs = fetch_applied_jobs(access_token)
        except RuntimeError as exc:
            error = str(exc)
            if "401" in error or "InvalidAuthenticationToken" in error:
                session.pop("graph_token", None)
                error = "Access token expired or invalid. Please paste a new one."

    counts = {
        "all": len(jobs),
        "applied": sum(1 for j in jobs if j.get("category") == "applied"),
        "interview": sum(1 for j in jobs if j.get("category") == "interview"),
        "rejected": sum(1 for j in jobs if j.get("category") == "rejected"),
    }

    if category_filter != "all":
        jobs = [j for j in jobs if j.get("category") == category_filter]

    if search_query and jobs:
        sq = search_query.lower()
        jobs = [j for j in jobs if sq in (j["subject"] + " " + j["company"]).lower()]

    return render_template(
        "applied.html",
        jobs=jobs,
        error=error,
        has_token=bool(access_token),
        search_query=search_query,
        category_filter=category_filter,
        counts=counts,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5003, debug=True)
