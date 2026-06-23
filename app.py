import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import urllib.request
import urllib.parse
import json
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from application_agent.agent.apply_queue import ApplyQueue
from extract_jobs import extract_newgrad_jobs

app = Flask(__name__)
app.secret_key = "hr-system-applied-tracker-key"
apply_queue = ApplyQueue()
SHORTLIST_STATUSES = ["shortlisted", "queued", "running", "paused", "submitted", "failed", "skipped"]
@app.route("/")
def index() -> str:
    return redirect(url_for("applied"))



# ---------------------------------------------------------------------------
# New-grad tab — separate cache so it doesn't block the main dashboard fetch
# ---------------------------------------------------------------------------
_newgrad_cache: Dict[str, Any] = {"jobs": [], "errors": [], "updated_at": None}
_newgrad_cache_lock = threading.Lock()
_newgrad_cache_loading = False
_newgrad_progress: Dict[str, Any] = {
    "percent": 0,
    "message": "Idle",
    "detail": {},
    "started_at": None,
    "updated_at": None,
}
NEWGRAD_CACHE_TTL = timedelta(minutes=30)   # Airtable doesn't change as fast


def _set_newgrad_progress(percent: int, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    with _newgrad_cache_lock:
        _newgrad_progress["percent"] = max(0, min(int(percent), 100))
        _newgrad_progress["message"] = message
        _newgrad_progress["detail"] = detail or {}
        _newgrad_progress["updated_at"] = datetime.now()


def _newgrad_progress_snapshot() -> Dict[str, Any]:
    progress = dict(_newgrad_progress)
    for key in ["started_at", "updated_at"]:
        value = progress.get(key)
        if isinstance(value, datetime):
            progress[key] = value.isoformat()
    return progress


def _run_newgrad_fetch() -> None:
    global _newgrad_cache_loading
    try:
        _set_newgrad_progress(1, "Starting new-grad job refresh")
        result = extract_newgrad_jobs(progress_callback=_set_newgrad_progress)
        with _newgrad_cache_lock:
            _newgrad_cache["jobs"] = result["jobs"]
            _newgrad_cache["errors"] = result["errors"]
            _newgrad_cache["updated_at"] = datetime.now()
            _newgrad_progress["percent"] = 100
            _newgrad_progress["message"] = f"Loaded {len(result['jobs'])} jobs"
            _newgrad_progress["detail"] = {"phase": "complete"}
            _newgrad_progress["updated_at"] = datetime.now()
    except Exception as exc:
        with _newgrad_cache_lock:
            _newgrad_cache["errors"] = [str(exc)]
            _newgrad_progress["message"] = f"Refresh failed: {exc}"
            _newgrad_progress["detail"] = {"phase": "error"}
            _newgrad_progress["updated_at"] = datetime.now()
    finally:
        with _newgrad_cache_lock:
            _newgrad_cache_loading = False


def start_newgrad_fetch() -> None:
    global _newgrad_cache_loading
    with _newgrad_cache_lock:
        if _newgrad_cache_loading:
            return
        _newgrad_cache_loading = True
        _newgrad_progress["percent"] = 0
        _newgrad_progress["message"] = "Queued new-grad job refresh"
        _newgrad_progress["detail"] = {"phase": "queued"}
        _newgrad_progress["started_at"] = datetime.now()
        _newgrad_progress["updated_at"] = datetime.now()
    threading.Thread(target=_run_newgrad_fetch, daemon=True).start()


@app.route("/newgrad/status")
def newgrad_status():
    with _newgrad_cache_lock:
        return jsonify(
            {
                "loading": _newgrad_cache_loading,
                "progress": _newgrad_progress_snapshot(),
                "jobCount": len(_newgrad_cache["jobs"]),
                "errorCount": len(_newgrad_cache["errors"]),
                "updatedAt": _newgrad_cache["updated_at"].isoformat() if _newgrad_cache["updated_at"] else None,
            }
        )


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
        progress = _newgrad_progress_snapshot()

    triggered_fetch = False
    if (refresh or not jobs or expired) and not loading:
        start_newgrad_fetch()
        triggered_fetch = True
    elif loading:
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
    shortlisted_urls = apply_queue.shortlisted_urls()

    return render_template(
        "newgrad.html",
        jobs=filtered_jobs,
        errors=errors,
        updated_at=updated_at,
        loading=loading or triggered_fetch,
        progress=progress,
        total_job_count=len(jobs),
        all_categories=all_categories,
        category_filter=category_filter,
        search_query=search_query,
        shortlisted_urls=shortlisted_urls,
    )


@app.route("/shortlist")
def shortlist() -> str:
    status_filter = request.args.get("status", "all").strip().lower()
    jobs = apply_queue.list_shortlist(status_filter)
    reports = apply_queue.list_reports(limit=100)
    latest_report_by_job: dict[int, dict[str, Any]] = {}
    for report in reports:
        job_id = report.get("job_id")
        if isinstance(job_id, int) and job_id not in latest_report_by_job:
            latest_report_by_job[job_id] = report

    counts = {"all": len(apply_queue.list_shortlist("all"))}
    counts.update({status: len(apply_queue.list_shortlist(status)) for status in SHORTLIST_STATUSES})

    return render_template(
        "shortlist.html",
        jobs=jobs,
        counts=counts,
        status_filter=status_filter,
        statuses=SHORTLIST_STATUSES,
        latest_report_by_job=latest_report_by_job,
    )


@app.route("/shortlist", methods=["POST"])
def add_to_shortlist():
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    try:
        job = apply_queue.add_shortlist(payload or {})
    except ValueError as exc:
        if request.is_json:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return redirect(url_for("newgrad"))

    if request.is_json:
        return jsonify({"ok": True, "job": job})

    return redirect(request.referrer or url_for("shortlist"))


@app.route("/shortlist/<int:job_id>/status", methods=["POST"])
def update_shortlist_status(job_id: int):
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    status = (payload or {}).get("status", "")
    try:
        job = apply_queue.update_status(job_id, status)
    except ValueError as exc:
        if request.is_json:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return redirect(request.referrer or url_for("shortlist"))

    if request.is_json:
        return jsonify({"ok": bool(job), "job": job})

    return redirect(request.referrer or url_for("shortlist"))


@app.route("/application-reports")
def application_reports():
    job_id = request.args.get("job_id", "").strip()
    parsed_job_id = int(job_id) if job_id.isdigit() else None
    return jsonify({"reports": apply_queue.list_reports(job_id=parsed_job_id)})


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


_CHUNK_DAYS = 3  # small ranges avoid Graph page caps hiding older mailbox mail
_MAX_GRAPH_WORKERS = 6

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
    """$search-based fetch, mailbox-wide, relevance-ranked, up to 3 pages."""
    encoded_q = urllib.parse.quote(query)
    path: Optional[str] = (
        f"/me/messages"
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
    """$filter fetch for a specific mailbox-wide date range, up to 10 pages."""
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    filter_val  = urllib.parse.quote(f"receivedDateTime ge {start_str} and receivedDateTime lt {end_str}")
    orderby_val = urllib.parse.quote("receivedDateTime desc")
    path: Optional[str] = (
        f"/me/messages"
        f"?$filter={filter_val}"
        f"&$orderby={orderby_val}"
        f"&$select=id,subject,receivedDateTime,from,webLink"
        f"&$top=100"
    )
    messages: list[dict] = []
    pages = 0
    while path and pages < 10:
        data = _graph_request(access_token, path)
        messages.extend(data.get("value", []))
        pages += 1
        next_link: str = data.get("@odata.nextLink", "")
        if next_link and next_link.startswith(_GRAPH_BASE):
            path = next_link[len(_GRAPH_BASE):]
        else:
            path = None
    return messages


def _applied_start_date(now: Optional[datetime] = None) -> datetime:
    """Return midnight UTC on Jan 1 of the current year."""
    current = now or datetime.utcnow()
    return datetime(current.year, 1, 1)


def _date_chunks(start_dt: datetime, end_dt: datetime, chunk_days: int) -> list[tuple[datetime, datetime]]:
    """Split a date window into (start, end) pairs of chunk_days each."""
    chunks = []
    end = end_dt
    while end > start_dt:
        start = max(end - timedelta(days=chunk_days), start_dt)
        chunks.append((start, end))
        end = start
    return chunks


# Per-token result cache: token_hash -> (results, expires_at)
_applied_cache: Dict[str, Any] = {}
_applied_cache_lock = threading.Lock()
_APPLIED_CACHE_TTL = timedelta(minutes=5)


def fetch_applied_jobs(access_token: str) -> list[dict[str, Any]]:
    """Search the user's mailbox for job-related emails, categorized by type.

    Runs targeted $search queries plus mailbox-wide date-filtered fetches from
    Jan 1 of the current year. Results are cached per token for 5 minutes.
    """
    import hashlib
    token_key = hashlib.sha256(access_token.encode()).hexdigest()
    now = datetime.now()

    with _applied_cache_lock:
        cached = _applied_cache.get(token_key)
        if cached and cached["expires_at"] > now:
            return cached["results"]

    all_messages: list[dict] = []
    now_utc = datetime.utcnow()
    cutoff_dt = _applied_start_date(now_utc)
    chunks = _date_chunks(cutoff_dt, now_utc, _CHUNK_DAYS)

    # Run enough requests concurrently to stay responsive without making Graph
    # throttle older chunks and leave the result set looking artificially cut off.
    n_workers = min(_MAX_GRAPH_WORKERS, len(_GRAPH_SEARCHES) + len(chunks))
    failed_chunks: list[tuple[datetime, datetime]] = []
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(_graph_search_messages, access_token, q): ("search", None)
            for q in _GRAPH_SEARCHES
        }
        futures.update({
            executor.submit(_graph_fetch_date_chunk, access_token, start, end): ("chunk", (start, end))
            for start, end in chunks
        })
        for future in as_completed(futures):
            kind, date_range = futures[future]
            try:
                all_messages.extend(future.result())
            except RuntimeError:
                if kind == "chunk" and date_range is not None:
                    failed_chunks.append(date_range)

    for start, end in failed_chunks:
        all_messages.extend(_graph_fetch_date_chunk(access_token, start, end))

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
