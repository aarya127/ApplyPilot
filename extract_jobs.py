import argparse
import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_URLS = [
    "https://careers.micron.com/careers?start=0&pid=41210446&sort_by=timestamp",
    "https://careers.mckesson.com/en/search-jobs?acm=ALL&alrpm=ALL&ascf=[%7B%22key%22:%22custom_fields.JobFunction%22,%22value%22:%22Information+Technology%22%7D]",
    "https://cmegroup.wd1.myworkdayjobs.com/en-US/cme_careers?locations=7a3301e6ea5710f69c0df31964e169fd&locations=7a3301e6ea5710f69c0f7e3613466b20&locations=7a3301e6ea5710f69c0ece8a424a6ab7&locations=7a3301e6ea5710f69c0ea186a0046a8f",
    "https://vrtx.wd501.myworkdayjobs.com/vertex_careers",
    "https://www.capitalonecareers.com/search-jobs/United%20States/234/2/6252001/39x76/-98x5/50/2",
    "https://careers.progressive.com/search/jobs?sort_by=cfm16,desc",
    "https://job-boards.greenhouse.io/ibkr?departments%5B%5D=4027655002&departments%5B%5D=4027657002",
    "https://jobs.intuit.com/search-jobs",
    "https://jobs.bms.com/careers?start=0&pid=137479694501&sort_by=timestamp",
    "https://prologis.wd5.myworkdayjobs.com/Prologis_External_Careers",
    "https://careers.stryker.com/jobs?filter%5Bcategory%5D%5B0%5D=Data%20Analysis&filter%5Bcategory%5D%5B1%5D=Engineering&sort_by=update_date&page_number=1&location_name=united%20states&location_type=1",
    "https://jobs.dell.com/en/search-jobs",
    "https://corningjobs.corning.com/search-jobs?collections=6097279-2023-272212-1&filters=6147437-1%2C6147440-1%2C6147442-1",
    "https://www.applovin.com/en/careers#job-board",
    "https://careers.spglobal.com/jobs?page=1&categories=Information%20Technology&sortBy=posted_date&descending=true&limit=100",
    "https://jobs.paloaltonetworks.com/en/search-jobs",
    "https://careers.qualcomm.com/careers",
    "https://www.lockheedmartinjobs.com/search-jobs",
    "https://careers.honeywell.com/en/sites/Honeywell/jobs?lastSelectedFacet=CATEGORIES&mode=location&selectedCategoriesFacet=300000017425610%3B300000017425634&selectedOrganizationsFacet=300000011497075&sortBy=POSTING_DATES_DESC",
    "https://www.uber.com/ca/en/careers/list/?department=Data%20Science&department=Engineering",
    "https://blackrock.wd1.myworkdayjobs.com/BlackRock_Professional?q=machine%20learning",
    "https://analogdevices.wd1.myworkdayjobs.com/External?q=machine%20learning",
    "https://www.arista.com/en/careers/engineering",
    "https://jobs.boeing.com/search-jobs/machine%20learning/185/1",
    "https://www.schwabjobs.com/search-jobs",
    "https://www.disneycareers.com/en/search-jobs",
    "https://careers.salesforce.com/en/jobs/?search=&country=Canada&country=United+States+of+America&pagesize=20#results",
]


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d[\d,]*)", text)
    return int(match.group(1).replace(",", "")) if match else None


def request_html(url: str) -> str:
    try:
        import requests
    except ImportError as exc:
        raise ImportError(
            "Requests is required for McKesson scraping. "
            "Install it with: pip install requests"
        ) from exc

    response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def extract_mckesson_jobs(url: str) -> dict[str, Any]:
    html_text = request_html(url)
    jobs = []
    # McKesson (Phenom/TalentBrew) structure:
    # <a class="search-results__job-title-link" href="...">Title</a>
    # <span class="search-results__job-location">Location</span>
    # <span class="search-results__job-date-posted">04/13/2026</span>
    for match in re.finditer(
        r'<a[^>]+class="search-results__job-title-link"[^>]+href="(?P<href>[^"]+)"[^>]*>'
        r'(?P<title>[^<]+)</a>\s*'
        r'<span[^>]+class="search-results__job-location"[^>]*>(?P<location>[^<]+)</span>'
        r'(?:\s*<span[^>]+class="search-results__job-date-posted"[^>]*>(?P<posted>[^<]+)</span>)?',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        title = html.unescape(match.group("title")).strip()
        location = html.unescape(match.group("location")).strip()
        href = match.group("href").strip()
        posted = html.unescape(match.group("posted") or "").strip()
        full_url = urljoin(url, href)
        jobs.append({"title": title, "location": location, "posted": posted, "url": full_url})
    return {"total_jobs": len(jobs), "jobs": jobs}

def extract_spglobal_jobs(url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            'Playwright is required for S&P Global scraping. '
            'Install it with: pip install playwright && playwright install chromium'
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=REQUEST_HEADERS['User-Agent'],
            extra_http_headers={'Accept-Language': REQUEST_HEADERS['Accept-Language']},
        )
        page = context.new_page()
        page.goto(url, wait_until='domcontentloaded', timeout=120000)
        page.wait_for_selector('a.job-title-link', timeout=30000)

        jobs = []
        locator = page.locator('mat-expansion-panel')
        for i in range(locator.count()):
            item = locator.nth(i)
            title = item.locator('a.job-title-link').inner_text().strip()
            href = item.locator('a.job-title-link').get_attribute('href') or ''
            apply_href = item.locator('a.apply-button').get_attribute('href') or ''
            full_url = apply_href if apply_href.startswith('http') else urljoin(url, apply_href or href)

            location = ''
            location_locator = item.locator('span.location.label-value')
            if location_locator.count() > 0:
                location = location_locator.first.inner_text().strip()
                location = re.sub(r'\s+', ' ', location)

            posted = ''
            posted_locator = item.locator('span.label-value.posted_date')
            if posted_locator.count() > 0:
                posted = posted_locator.first.inner_text().strip()

            jobs.append(
                {
                    'title': title,
                    'location': location,
                    'posted': posted,
                    'url': full_url,
                }
            )

        total_jobs = locator.count()
        browser.close()

    return {'total_jobs': total_jobs, 'jobs': jobs}


def extract_paloalto_jobs(url: str) -> dict[str, Any]:
    html_text = request_html(url)
    jobs = []

    for match in re.finditer(
        r'<a[^>]+class=["\']section29__search-results-link["\'][^>]+href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<body>.*?)</a>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        href = match.group('href').strip()
        body = match.group('body')

        title_match = re.search(r'<h2[^>]*>(?P<title>[^<]+)</h2>', body, re.IGNORECASE)
        location_match = re.search(
            r'<span[^>]+class=["\'][^"\']*section29__result-location[^"\']*["\'][^>]*>(?P<location>[^<]+)</span>',
            body,
            re.IGNORECASE,
        )
        posted_match = re.search(r'Posted\s+([^<\n]+)', body, re.IGNORECASE)

        title = html.unescape(title_match.group('title').strip()) if title_match else '(no title)'
        location = html.unescape(location_match.group('location').strip()) if location_match else ''
        posted = posted_match.group(0).strip() if posted_match else ''

        jobs.append(
            {
                'title': title,
                'location': re.sub(r'\s+', ' ', location).strip(),
                'posted': posted,
                'url': urljoin(url, href),
            }
        )

    return {'total_jobs': len(jobs), 'jobs': jobs}


def extract_qualcomm_jobs(url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            'Playwright is required for Qualcomm scraping. '
            'Install it with: pip install playwright && playwright install chromium'
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=REQUEST_HEADERS['User-Agent'],
            extra_http_headers={'Accept-Language': REQUEST_HEADERS['Accept-Language']},
        )
        page = context.new_page()
        page.goto(url, wait_until='domcontentloaded', timeout=120000)
        page.wait_for_selector('a[href^="/careers/job/"]', timeout=30000)

        jobs = []
        locator = page.locator('a[href^="/careers/job/"]')
        for i in range(locator.count()):
            item = locator.nth(i)
            href = item.get_attribute('href') or ''
            text = item.inner_text().strip()
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            title = lines[0] if lines else '(no title)'
            location = lines[1] if len(lines) > 1 else ''
            posted = ''
            if len(lines) > 2 and lines[-1].lower().startswith('posted'):
                posted = lines[-1]

            jobs.append(
                {
                    'title': html.unescape(title),
                    'location': re.sub(r'\s+', ' ', html.unescape(location)).strip(),
                    'posted': posted,
                    'url': urljoin(url, href),
                }
            )

        total_jobs = locator.count()
        browser.close()

    return {'total_jobs': total_jobs, 'jobs': jobs}


def extract_lockheed_jobs(url: str) -> dict[str, Any]:
    """Lockheed Martin TalentBrew site — paginates via ?p=N (15 jobs/page, ~3500+ total)."""
    import requests as req

    parsed = urlparse(url)
    base_url = f"https://{parsed.hostname}"
    clean_url = f"{base_url}{parsed.path}"
    headers = {**REQUEST_HEADERS, "Accept": "text/html"}

    all_jobs: list[dict] = []
    total_jobs: int | None = None
    total_pages: int | None = None
    records_per_page = 15
    seen_urls: set[str] = set()
    page = 1

    while True:
        page_url = clean_url if page == 1 else f"{clean_url}?p={page}"
        resp = req.get(page_url, headers=headers, timeout=30)
        resp.raise_for_status()
        text = resp.text

        if total_jobs is None:
            total_m = re.search(r'data-total-job-results="(\d+)"', text, re.I)
            total_jobs = int(total_m.group(1)) if total_m else None
            pages_m = re.search(r'data-total-pages="(\d+)"', text, re.I)
            total_pages = int(pages_m.group(1)) if pages_m else None
            rpp_m = re.search(r'data-records-per-page="(\d+)"', text, re.I)
            if rpp_m:
                records_per_page = int(rpp_m.group(1))

        page_jobs: list[dict] = []
        for m in re.finditer(
            r'<a[^>]+href="(/job/[^"]+)"[^>]*data-job-id="[^"]+"[^>]*>'
            r'\s*<span[^>]*class="[^"]*job-title[^"]*"[^>]*>([^<]+)</span>'
            r'(?:\s*<span[^>]*class="[^"]*job-location[^"]*"[^>]*>([^<]*)</span>)?'
            r'(?:\s*<span[^>]*class="[^"]*job-date-posted[^"]*"[^>]*>([^<]*)</span>)?',
            text,
            re.I | re.S,
        ):
            href = m.group(1)
            full_url = f"{base_url}{href}"
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            page_jobs.append({
                "title": html.unescape(m.group(2).strip()),
                "location": html.unescape((m.group(3) or "").strip()),
                "posted": html.unescape((m.group(4) or "").strip()),
                "url": full_url,
            })

        all_jobs.extend(page_jobs)

        if total_pages and page >= total_pages:
            break
        if len(page_jobs) == 0:
            break
        page += 1
        if page > 300:  # safety cap (~4500 jobs)
            break

    return {"total_jobs": total_jobs or len(all_jobs), "jobs": all_jobs}


def extract_intuit_jobs(url: str) -> dict[str, Any]:
    html_text = request_html(url)
    jobs = []
    for match in re.finditer(
        r'<a(?=[^>]*\bclass=["\"][^"\"]*\bsr-item\b[^"\"]*["\"])(?=[^>]*\bhref=["\"](?P<href>[^"\"]+)["\"])[^>]*>(?P<body>.*?)</a>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        href = match.group("href").strip()
        body = match.group("body")

        title_match = re.search(r'<h2[^>]*>(?P<title>[^<]+)</h2>', body, re.IGNORECASE)
        if not title_match:
            title_match = re.search(r'data-title=["\"](?P<title>[^"\"]+)["\"]', body, re.IGNORECASE)

        location_match = re.search(
            r'<span[^>]+class=["\"][^"\"]*\bjob-location\b[^"\"]*["\"][^>]*>(?P<location>[^<]+)</span>',
            body,
            re.IGNORECASE,
        )

        title = html.unescape(title_match.group("title").strip()) if title_match else "(no title)"
        location = html.unescape(location_match.group("location").strip()) if location_match else ""
        jobs.append(
            {
                "title": title,
                "location": location,
                "posted": "",
                "url": urljoin(url, href),
            }
        )

    return {"total_jobs": len(jobs), "jobs": jobs}


def extract_dell_jobs(url: str) -> dict[str, Any]:
    html_text = request_html(url)
    jobs = []
    for match in re.finditer(
        r'<a(?=[^>]*\bdata-job-id=["\"][^"\"]+["\"])(?=[^>]*\bhref=["\"](?P<href>[^"\"]+)["\"])[^>]*>(?P<body>.*?)</a>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        href = match.group("href").strip()
        body = match.group("body")

        title_match = re.search(r'<h2[^>]*>(?P<title>[^<]+)</h2>', body, re.IGNORECASE)
        location_match = re.search(
            r'<span[^>]+class=["\"][^"\"]*\bjob-info\b[^"\"]*\bjob-location\b[^"\"]*["\"][^>]*>(?P<location>.*?)</span>',
            body,
            re.IGNORECASE | re.DOTALL,
        )

        title = html.unescape(title_match.group("title").strip()) if title_match else "(no title)"
        raw_location = location_match.group("location") if location_match else ""
        location = re.sub(r"\s+", " ", html.unescape(re.sub(r'<[^>]+>', '', raw_location)).strip())

        jobs.append(
            {
                "title": title,
                "location": location,
                "posted": "",
                "url": urljoin(url, href),
            }
        )

    return {"total_jobs": len(jobs), "jobs": jobs}


def extract_stryker_jobs(url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            "Playwright is required for Stryker scraping. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=REQUEST_HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": REQUEST_HEADERS["Accept-Language"]},
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("a.results-list__item-title--link", timeout=15000)
        except Exception:
            pass

        accept_buttons = page.locator('button:has-text("Accept All Cookies")')
        if accept_buttons.count() > 0:
            accept_buttons.first.click()
            page.wait_for_timeout(500)

        jobs = []
        locator = page.locator("a.results-list__item-title--link")
        for i in range(locator.count()):
            item = locator.nth(i)
            title = item.inner_text().strip()
            href = item.get_attribute("href") or ""
            full_url = href if href.startswith("http") else urljoin(url, href)

            location = ""
            parent_item = item.locator('xpath=ancestor::li[1]')
            location_locator = parent_item.locator("span.results-list__item-street--label")
            if location_locator.count() > 0:
                location = location_locator.first.inner_text().strip()

            jobs.append(
                {
                    "title": title,
                    "location": location,
                    "posted": "",
                    "url": full_url,
                }
            )

        total_jobs = len(jobs)
        browser.close()

    return {"total_jobs": total_jobs, "jobs": jobs}


def extract_bms_jobs(url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            "Playwright is required for BMS scraping. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=REQUEST_HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": REQUEST_HEADERS["Accept-Language"]},
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector('a[aria-label^="View job:"][href^="/careers/job/"]', timeout=15000)
        except Exception:
            pass

        accept_buttons = page.locator('button:has-text("Accept All Cookies")')
        if accept_buttons.count() > 0:
            accept_buttons.first.click()
            page.wait_for_timeout(500)

        jobs = []
        locator = page.locator('a[aria-label^="View job:"][href^="/careers/job/"]')
        for i in range(locator.count()):
            item = locator.nth(i)
            href = item.get_attribute("href") or ""
            aria_label = item.get_attribute("aria-label") or ""
            title = re.sub(r"^View job:\s*", "", aria_label).strip()

            text = item.inner_text().strip()
            posted = ""
            posted_match = re.search(
                r"(Posted\s+(?:Today|Yesterday|\d+\s+days?\s+ago))$",
                text,
                re.IGNORECASE,
            )
            if posted_match:
                posted = posted_match.group(1).strip()

            location = text
            if posted_match:
                location = text[: posted_match.start()].strip()
            if title and location.startswith(title):
                location = location[len(title) :].strip()
            location = re.sub(r"\bR\d+\s*$", "", location).strip()

            jobs.append(
                {
                    "title": title or "(no title)",
                    "location": location,
                    "posted": posted,
                    "url": urljoin(url, href),
                }
            )

        total_jobs = len(jobs)
        browser.close()

    return {"total_jobs": total_jobs, "jobs": jobs}


def extract_workday_jobs(url: str) -> dict[str, Any]:
    """Fetch all jobs from a Workday myworkdayjobs.com board via the JSON REST API."""
    import requests as req

    parsed = urlparse(url)
    hostname = parsed.hostname  # e.g. "cmegroup.wd1.myworkdayjobs.com"
    tenant = hostname.split(".")[0]  # e.g. "cmegroup"

    # Last non-empty path segment is the job board name (strips locale like "en-US")
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    board = path_parts[-1] if path_parts else ""

    qs = parse_qs(parsed.query)
    search_text = qs.get("q", [""])[0]
    location_ids = qs.get("locations", [])

    api_url = f"https://{hostname}/wday/cxs/{tenant}/{board}/jobs"
    headers = {
        **REQUEST_HEADERS,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    limit = 20
    offset = 0
    total_jobs: int | None = None
    all_jobs: list[dict] = []

    while True:
        body: dict[str, Any] = {
            "appliedFacets": {"locations": location_ids} if location_ids else {},
            "limit": limit,
            "offset": offset,
            "searchText": search_text,
        }
        resp = req.post(api_url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if total_jobs is None:
            total_jobs = data.get("total", 0)

        postings = data.get("jobPostings") or []
        if not postings:
            break

        for job in postings:
            path = job.get("externalPath", "")
            job_url = f"https://{hostname}{path}" if path else ""
            all_jobs.append({
                "title": job.get("title", ""),
                "location": job.get("locationsText", ""),
                "posted": job.get("postedOn") or "",
                "url": job_url,
            })

        offset += limit
        if offset >= (total_jobs or 0):
            break

    return {"total_jobs": total_jobs, "jobs": all_jobs}


def extract_micron_jobs(url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            "Playwright is required for rendering the Micron careers page. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=REQUEST_HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": REQUEST_HEADERS["Accept-Language"]},
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)

        body_text = page.inner_text("body")
        count_match = re.search(r"(\d+)\s+jobs", body_text)
        total_jobs = int(count_match.group(1)) if count_match else None

        jobs = []
        locator = page.locator('a[id^="job-card-"]')
        for i in range(locator.count()):
            element = locator.nth(i)
            href = element.get_attribute("href") or ""
            full_url = href if href.startswith("http") else f"https://careers.micron.com{href}"
            text = element.inner_text().strip()
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if len(lines) >= 3 and lines[-1].startswith("Posted"):
                title = " ".join(lines[:-2]).strip() if len(lines) > 3 else lines[0]
                location = lines[-2]
                posted = lines[-1]
            elif len(lines) == 3:
                title, location, posted = lines
            else:
                title = lines[0] if lines else "(no title)"
                location = lines[1] if len(lines) > 1 else "(no location)"
                posted = lines[2] if len(lines) > 2 else "(no posted date)"

            jobs.append(
                {
                    "title": title,
                    "location": location,
                    "posted": posted,
                    "url": full_url,
                }
            )

        browser.close()

    return {"total_jobs": total_jobs, "jobs": jobs}


def extract_capitalone_jobs(url: str) -> dict[str, Any]:
    html_text = request_html(url)
    jobs: dict[str, Any] = {}
    for match in re.finditer(
        r'<a[^>]+href=["\'](?P<href>[^"\']+)["\'][^>]*data-job-id=["\'][^"\']+["\'][^>]*>(?P<body>.*?)</a>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        href = match.group('href').strip()
        body = match.group('body')
        full_url = urljoin(url, href)

        title_match = re.search(r'<h2[^>]*>\s*(?P<title>[^<]+)\s*</h2>', body, re.IGNORECASE)
        title = html.unescape(title_match.group('title').strip()) if title_match else (
            re.sub(r'<[^>]+>', '', body).strip() or '(no title)'
        )

        location_match = re.search(
            r'<span[^>]+class=["\'][^"\']*job-location[^"\']*["\'][^>]*>(?P<location>[^<]+)</span>',
            body, re.IGNORECASE,
        )
        location = html.unescape(location_match.group('location').strip()) if location_match else ''

        posted_match = re.search(
            r'<span[^>]+class=["\'][^"\']*job-date-posted[^"\']*["\'][^>]*>(?P<posted>[^<]+)</span>',
            body, re.IGNORECASE,
        )
        posted = html.unescape(posted_match.group('posted').strip()) if posted_match else ''

        if full_url not in jobs:
            jobs[full_url] = {
                'title': title,
                'location': re.sub(r'\s+', ' ', location).strip(),
                'posted': posted,
                'url': full_url,
            }
    return {'total_jobs': len(jobs), 'jobs': list(jobs.values())}


def extract_progressive_jobs(url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            'Playwright is required for Progressive scraping. '
            'Install it with: pip install playwright && playwright install chromium'
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=REQUEST_HEADERS['User-Agent'],
            extra_http_headers={'Accept-Language': REQUEST_HEADERS['Accept-Language']},
        )
        page = context.new_page()
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        try:
            page.wait_for_selector('a.search-results__job-title-link', timeout=20000)
        except Exception:
            pass
        html_text = page.content()
        browser.close()

    jobs = []
    for match in re.finditer(
        r'<a[^>]+class=["\']search-results__job-title-link["\'][^>]+href=["\'](?P<href>[^"\']+)["\'][^>]*>'
        r'(?P<title>[^<]+)</a>\s*'
        r'<span[^>]+class=["\']search-results__job-location["\'][^>]*>(?P<location>[^<]+)</span>'
        r'(?:\s*<span[^>]+class=["\']search-results__job-date-posted["\'][^>]*>(?P<posted>[^<]+)</span>)?',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        title = html.unescape(match.group('title')).strip()
        location = html.unescape(match.group('location')).strip()
        href = match.group('href').strip()
        posted = html.unescape(match.group('posted') or '').strip()
        full_url = urljoin(url, href)
        jobs.append({'title': title, 'location': location, 'posted': posted, 'url': full_url})
    return {'total_jobs': len(jobs), 'jobs': jobs}


def extract_greenhouse_jobs(url: str) -> dict[str, Any]:
    html_text = request_html(url)
    jobs = []
    for match in re.finditer(
        r'<tr[^>]+class=["\']job-post["\'][^>]*>.*?'
        r'<a[^>]+href=["\'](?P<href>[^"\']+)["\'][^>]*>.*?'
        r'<p[^>]+class=["\'][^"\']*body--medium[^"\']*["\'][^>]*>(?P<title>[^<]+)</p>.*?'
        r'(?:<p[^>]+class=["\'][^"\']*body--metadata[^"\']*["\'][^>]*>(?P<location>[^<]+)</p>)?',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        href = match.group('href').strip()
        title = html.unescape(match.group('title')).strip()
        location = html.unescape(match.group('location') or '').strip()
        jobs.append({'title': title, 'location': location, 'posted': '', 'url': href})
    return {'total_jobs': len(jobs), 'jobs': jobs}


def extract_corning_jobs(url: str) -> dict[str, Any]:
    base_url = 'https://corningjobs.corning.com'
    html_text = request_html(url)
    jobs = []
    for row in re.finditer(
        r'<tr[^>]+class=["\']data-row["\'][^>]*>(.*?)</tr>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        row_html = row.group(1)
        title_match = re.search(
            r'<a[^>]+class=["\'][^"\']*jobTitle-link[^"\']*["\'][^>]+href=["\'](?P<href>[^"\']+)["\'][^>]*>'
            r'(?P<title>[^<]+)</a>',
            row_html, re.IGNORECASE,
        )
        if not title_match:
            continue
        title = html.unescape(title_match.group('title')).strip()
        href = title_match.group('href').strip()
        full_url = href if href.startswith('http') else urljoin(base_url, href)

        location_match = re.search(
            r'<td[^>]+class=["\']colLocation[^"\']*["\'][^>]*>.*?'
            r'<span[^>]+class=["\'][^"\']*jobLocation[^"\']*["\'][^>]*>(?P<location>[^<]+)</span>',
            row_html, re.IGNORECASE | re.DOTALL,
        )
        location = ''
        if location_match:
            location = html.unescape(location_match.group('location')).strip()
        else:
            loc_match2 = re.search(
                r'<span[^>]+class=["\'][^"\']*jobLocation[^"\']*["\'][^>]*>\s*'
                r'<span[^>]+class=["\'][^"\']*jobLocation[^"\']*["\'][^>]*>(?P<location>[^<]+)</span>',
                row_html, re.IGNORECASE | re.DOTALL,
            )
            if loc_match2:
                location = html.unescape(loc_match2.group('location')).strip()

        posted_match = re.search(
            r'<td[^>]+class=["\']colDate[^"\']*["\'][^>]*>.*?'
            r'<span[^>]+class=["\'][^"\']*jobDate[^"\']*["\'][^>]*>(?P<posted>[^<]+)</span>',
            row_html, re.IGNORECASE | re.DOTALL,
        )
        if not posted_match:
            posted_match = re.search(
                r'<span[^>]+class=["\'][^"\']*jobDate[^"\']*["\'][^>]*>(?P<posted>[^<]+)</span>',
                row_html, re.IGNORECASE | re.DOTALL,
            )
        posted = html.unescape(posted_match.group('posted')).strip() if posted_match else ''

        jobs.append({
            'title': title,
            'location': re.sub(r'\s+', ' ', location).strip(),
            'posted': re.sub(r'\s+', ' ', posted).strip(),
            'url': full_url,
        })
    return {'total_jobs': len(jobs), 'jobs': jobs}


def extract_honeywell_jobs(url: str) -> dict[str, Any]:
    import ast as _ast
    try:
        import requests as _req
    except ImportError as exc:
        raise ImportError("requests is required") from exc

    from urllib.parse import parse_qs

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    categories = qs.get("selectedCategoriesFacet", [""])[0]
    orgs = qs.get("selectedOrganizationsFacet", [""])[0]
    last_facet = qs.get("lastSelectedFacet", ["CATEGORIES"])[0]
    sort_by = qs.get("sortBy", ["POSTING_DATES_DESC"])[0]

    # Fetch the careers page HTML to extract Oracle pod URL + site number from CX_CONFIG
    session = _req.Session()
    session.headers.update(REQUEST_HEADERS)
    html_resp = session.get(url, timeout=30)
    html_resp.raise_for_status()
    html_text = html_resp.text

    api_base_match = re.search(r"apiBaseUrl:\s*'([^']+)'", html_text)
    site_number_match = re.search(r"siteNumber:\s*'([^']+)'", html_text)
    if not api_base_match:
        raise ValueError("Could not find Oracle HCM apiBaseUrl on Honeywell careers page")
    api_base = api_base_match.group(1).rstrip("/")
    site_number = site_number_match.group(1) if site_number_match else "CX_1"

    session.headers["Accept"] = "application/json"
    session.headers["Origin"] = "https://careers.honeywell.com"
    session.headers["Referer"] = "https://careers.honeywell.com/"

    expand = (
        "requisitionList.workLocation,requisitionList.otherWorkLocations,"
        "requisitionList.secondaryLocations,flexFieldsFacet.values,"
        "requisitionList.requisitionFlexFields"
    )
    facets_list = "LOCATIONS%3BWORK_LOCATIONS%3BWORKPLACE_TYPES%3BTITLES%3BCATEGORIES%3BORGANIZATIONS%3BPOSTING_DATES%3BFLEX_FIELDS"
    limit = 100
    offset = 0
    total = None
    jobs = []

    while True:
        finder_parts = [
            f"siteNumber={site_number}",
            f"facetsList={facets_list}",
            f"limit={limit}",
            f"offset={offset}",
            f"lastSelectedFacet={last_facet}",
            f"sortBy={sort_by}",
        ]
        if categories:
            finder_parts.append(f"selectedCategoriesFacet={categories}")
        if orgs:
            finder_parts.append(f"selectedOrganizationsFacet={orgs}")
        finder = ",".join(finder_parts)

        api_url = (
            f"{api_base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand={expand}&finder=findReqs;{finder}"
        )
        resp = session.get(api_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("items", [])
        if not items:
            break
        search = items[0]
        if total is None:
            total = int(search.get("TotalJobsCount", 0) or 0)

        req_list = search.get("requisitionList", [])
        if isinstance(req_list, str):
            req_list = _ast.literal_eval(req_list)
        if not req_list:
            break

        for req in req_list:
            job_id = req.get("Id", "")
            title = (req.get("Title") or "").strip()
            posted = (req.get("PostedDate") or "")[:10]
            location = (req.get("PrimaryLocation") or "").strip()
            jobs.append({
                "title": title,
                "location": location,
                "posted": posted,
                "url": f"https://careers.honeywell.com/en/sites/Honeywell/job/{job_id}",
            })

        offset += limit
        if total is not None and offset >= total:
            break

    return {"total_jobs": total, "jobs": jobs}


def extract_uber_jobs(url: str) -> dict[str, Any]:
    from urllib.parse import parse_qs
    try:
        import requests as _req
    except ImportError as exc:
        raise ImportError("requests is required") from exc

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    departments = qs.get("department", ["Engineering"])

    session = _req.Session()
    session.headers.update(REQUEST_HEADERS)
    session.headers["x-csrf-token"] = "x"
    session.headers["Content-Type"] = "application/json"

    resp = session.post(
        "https://www.uber.com/api/loadSearchJobsResults?page=1",
        json={"params": {"department": departments}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    results = data.get("data", {}).get("results", [])
    jobs = []
    for item in results:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        loc = item.get("location") or {}
        location = ", ".join(filter(None, [
            loc.get("city", ""),
            loc.get("region", ""),
            loc.get("countryName", ""),
        ]))
        posted = (item.get("creationDate") or "")[:10]
        job_id = item.get("id", "")
        jobs.append({
            "title": title,
            "location": location,
            "posted": posted,
            "url": f"https://www.uber.com/global/en/careers/list/{job_id}/",
        })

    return {"total_jobs": len(jobs), "jobs": jobs}


def extract_boeing_jobs(url: str) -> dict[str, Any]:
    """TalentBrew-based Boeing careers site — server-rendered HTML, paginated."""
    import requests as req
    from urllib.parse import unquote

    parsed = urlparse(url)
    # Path format: /search-jobs/{keyword}/{org_id}/{page}
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    # parts[0]='search-jobs', parts[1]=keyword, parts[2]=org_id, parts[3]=page
    keyword = unquote(parts[1]) if len(parts) > 1 else ""
    org_id = parts[2] if len(parts) > 2 else "185"
    base = f"https://{parsed.hostname}/search-jobs/{parsed.path.strip('/').split('/')[1]}/{org_id}"

    headers = {**REQUEST_HEADERS, "Accept": "text/html"}
    all_jobs: list[dict] = []
    total_jobs: int | None = None
    page = 1

    while True:
        page_url = f"{base}/{page}"
        resp = req.get(page_url, headers=headers, timeout=30)
        resp.raise_for_status()
        text = resp.text

        if total_jobs is None:
            tm = re.search(r'["\'>](\d+)\s+results\s+found', text, re.I)
            if tm:
                total_jobs = int(tm.group(1).replace(",", ""))

        titles = re.findall(
            r'<span class="search-results__job-title">([^<]+)</span>', text, re.I
        )
        if not titles:
            break

        links = re.findall(
            r'<a class="search-results__job-link" href="([^"]+)"', text, re.I
        )
        locations = re.findall(
            r'<span class="search-results__job-info location">([^<]+)</span>', text, re.I
        )
        dates = re.findall(
            r'<span class="search-results__job-info date">([^<]+)</span>', text, re.I
        )

        for i, title in enumerate(titles):
            href = links[i] if i < len(links) else ""
            all_jobs.append({
                "title": html.unescape(title.strip()),
                "location": html.unescape(locations[i].strip()) if i < len(locations) else "",
                "posted": dates[i].strip() if i < len(dates) else "",
                "url": f"https://{parsed.hostname}{href}" if href else "",
            })

        page += 1
        if total_jobs and len(all_jobs) >= total_jobs:
            break
        if page > 50:  # safety cap
            break

    return {"total_jobs": total_jobs or len(all_jobs), "jobs": all_jobs}


def extract_schwab_jobs(url: str) -> dict[str, Any]:
    """TalentBrew-based Schwab careers site — paginates via ?p=N (20 jobs/page)."""
    import requests as req

    parsed = urlparse(url)
    base_url = f"https://{parsed.hostname}"
    clean_url = f"{base_url}{parsed.path}"  # drop any existing query string
    headers = {**REQUEST_HEADERS, "Accept": "text/html"}

    all_jobs: list[dict] = []
    total_jobs: int | None = None
    records_per_page = 20
    page = 1

    while True:
        page_url = clean_url if page == 1 else f"{clean_url}?p={page}"
        resp = req.get(page_url, headers=headers, timeout=30)
        resp.raise_for_status()
        text = resp.text

        if total_jobs is None:
            total_m = re.search(r'data-total-job-results="(\d+)"', text, re.I)
            total_jobs = int(total_m.group(1)) if total_m else None
            rpp_m = re.search(r'data-records-per-page="(\d+)"', text, re.I)
            if rpp_m:
                records_per_page = int(rpp_m.group(1))

        page_jobs: list[dict] = []
        for m in re.finditer(
            r'<a\s+href="(/job/[^"]+)"[^>]*data-job-id="[^"]+"[^>]*>'
            r'.*?<h2[^>]*>([^<]+)</h2>'
            r'(?:.*?<span class="(?:job-)?location">([^<]*)</span>)?',
            text,
            re.I | re.S,
        ):
            href, title = m.group(1), m.group(2)
            location = m.group(3) or ""
            page_jobs.append({
                "title": html.unescape(title.strip()),
                "location": html.unescape(location.strip()),
                "posted": "",
                "url": f"{base_url}{href}",
            })

        all_jobs.extend(page_jobs)

        if len(page_jobs) < records_per_page:
            break
        if total_jobs and len(all_jobs) >= total_jobs:
            break
        page += 1
        if page > 50:  # safety cap
            break

    return {"total_jobs": total_jobs or len(all_jobs), "jobs": all_jobs}


def extract_disney_jobs(url: str) -> dict[str, Any]:
    """TalentBrew-based Disney careers site — paginates via ?p=N (10 jobs/page)."""
    import requests as req

    parsed = urlparse(url)
    base_url = f"https://{parsed.hostname}"
    clean_url = f"{base_url}{parsed.path}"  # drop any existing query string
    headers = {**REQUEST_HEADERS, "Accept": "text/html"}

    all_jobs: list[dict] = []
    total_jobs: int | None = None
    total_pages: int | None = None
    records_per_page = 10
    page = 1

    while True:
        page_url = clean_url if page == 1 else f"{clean_url}?p={page}"
        r = req.get(page_url, headers=headers, timeout=30)
        r.raise_for_status()
        text = r.text

        if total_jobs is None:
            total_m = re.search(r'data-total-job-results="(\d+)"', text, re.I)
            total_jobs = int(total_m.group(1)) if total_m else None
            pages_m = re.search(r'data-total-pages="(\d+)"', text, re.I)
            total_pages = int(pages_m.group(1)) if pages_m else None
            rpp_m = re.search(r'data-records-per-page="(\d+)"', text, re.I)
            if rpp_m:
                records_per_page = int(rpp_m.group(1))

        page_jobs: list[dict] = []
        for m in re.finditer(
            r'<a\s+href="(/en/job/[^"]+)"[^>]*data-job-id="[^"]+"[^>]*>'
            r'.*?<h2[^>]*>([^<]+)</h2>'
            r'(?:.*?<span class="job-location">([^<]*)</span>)?',
            text,
            re.I | re.S,
        ):
            href, title = m.group(1), m.group(2)
            location = m.group(3) or ""
            page_jobs.append({
                "title": html.unescape(title.strip()),
                "location": html.unescape(location.strip()),
                "posted": "",
                "url": f"{base_url}{href}",
            })

        all_jobs.extend(page_jobs)

        if total_pages and page >= total_pages:
            break
        if len(page_jobs) < records_per_page:
            break
        page += 1
        if page > 100:  # safety cap
            break

    return {"total_jobs": total_jobs or len(all_jobs), "jobs": all_jobs}


def extract_salesforce_jobs(url: str) -> dict[str, Any]:
    """Salesforce careers site — server-rendered HTML with card.card-job, paginated."""
    import requests as req
    from urllib.parse import parse_qs

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    countries = qs.get("country", [])
    search = qs.get("search", [""])[0]
    try:
        pagesize = int(qs.get("pagesize", ["20"])[0])
    except (ValueError, IndexError):
        pagesize = 20

    base_url = f"https://{parsed.hostname}"
    headers = {**REQUEST_HEADERS, "Accept": "text/html"}

    def build_url(page: int) -> str:
        params = f"search={search}&pagesize={pagesize}&page={page}"
        for c in countries:
            params += f"&country={c}"
        return f"{base_url}{parsed.path}?{params}"

    all_jobs: list[dict] = []
    page = 1

    while True:
        resp = req.get(build_url(page), headers=headers, timeout=30)
        resp.raise_for_status()
        text = resp.text

        # Each card: <div class="card card-job">...<h3 class="card-title"><a href="/en/jobs/...">title</a>
        #   ...<ul class="list-inline locations"><li>loc1</li></ul>
        card_blocks = re.findall(
            r'<div class="card card-job">(.*?)</div>\s*</div>',
            text,
            re.I | re.S,
        )
        if not card_blocks:
            break

        for block in card_blocks:
            title_m = re.search(
                r'<a[^>]+href="(/en/jobs/[^"]+)"[^>]*>([^<]+)</a>', block, re.I
            )
            if not title_m:
                continue
            href, title = title_m.group(1), title_m.group(2)
            locs = re.findall(r'<li class="list-inline-item">([^<]+)</li>', block, re.I)
            location = ", ".join(l.strip() for l in locs if l.strip())
            all_jobs.append({
                "title": html.unescape(title.strip()),
                "location": html.unescape(location),
                "posted": "",
                "url": f"{base_url}{href}",
            })

        if len(card_blocks) < pagesize:
            break
        page += 1
        if page > 100:  # safety cap
            break

    return {"total_jobs": len(all_jobs), "jobs": all_jobs}


def extract_arista_jobs(url: str) -> dict[str, Any]:
    """Arista careers SPA — currently blocked by Fastly CDN (HTTP 406 for all clients).

    Attempts a plain HTTP request first; returns empty if the site is inaccessible.
    If Arista's CDN policy ever changes, the regex below will parse /en/careers/ job links.
    """
    import requests as req

    parsed = urlparse(url)
    base_url = f"https://{parsed.hostname}"

    try:
        r = req.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)
        html_text = r.text if r.status_code == 200 else ""
    except Exception:
        html_text = ""

    if not html_text:
        return {"total_jobs": 0, "jobs": []}

    # If the page becomes accessible, parse /en/careers/{path} job links
    jobs: list[dict] = []
    nav_hrefs = {"/en/careers", "/en/careers/engineering", "/en/careers/operations",
                 "/en/careers/sales-marketing", "/en/careers/business-support"}
    for m in re.finditer(
        r'<a[^>]+href="(/en/careers/[^"#]+)"[^>]*>([^<]{5,120})</a>',
        html_text,
        re.I,
    ):
        href, text_val = m.group(1), m.group(2).strip()
        if href.rstrip("/") in nav_hrefs or len(text_val) < 5:
            continue
        jobs.append({
            "title": html.unescape(text_val),
            "location": "",
            "posted": "",
            "url": f"{base_url}{href}",
        })

    seen: set[str] = set()
    unique_jobs = [j for j in jobs if not (j["url"] in seen or seen.add(j["url"]))]  # type: ignore[func-returns-value]
    return {"total_jobs": len(unique_jobs), "jobs": unique_jobs}


def extract_applovin_jobs(url: str) -> dict[str, Any]:
    import json as _json
    api_url = 'https://boards-api.greenhouse.io/v1/boards/applovin/jobs'
    response_text = request_html(api_url)
    data = _json.loads(response_text)
    jobs = []
    for job in data.get('jobs', []):
        title = job.get('title', '').strip()
        location = (job.get('location') or {}).get('name', '').strip()
        absolute_url = job.get('absolute_url', '').strip()
        updated_at = job.get('updated_at', '')[:10]
        jobs.append({
            'title': title,
            'location': location,
            'posted': updated_at,
            'url': absolute_url,
        })
    return {'total_jobs': len(jobs), 'jobs': jobs}


_NEWGRAD_CATEGORY_EMBEDS: dict[str, str] = {
    "swe":  "https://airtable.com/embed/appjDG7vmPOm1pO7S/shr763VHjlzPBDCgN",
    "aiml": "https://airtable.com/embed/appoxNzAIRReFCzZV/shrmDBF1vNPtzNjzl",
    "de":   "https://airtable.com/embed/appqYfRGKpLQ8UsdH/shrFnvW20reJCEkYZ",
    "cs":   "https://airtable.com/embed/app5K4hbJeNczKe80/shrmicWx3O72527KW",
}

_NEWGRAD_CATEGORY_LABELS: dict[str, str] = {
    "swe":  "Software Engineering",
    "aiml": "AI / ML",
    "de":   "Data Engineering",
    "cs":   "Cybersecurity",
}


def _scrape_newgrad_category(category: str, embed_url: str, progress_callback: Any = None) -> list[dict[str, Any]]:
    """Render one Airtable embed page and extract all job rows via virtual-scroll traversal.

    Column indexes are read dynamically from the header row because different embed views
    use different column orderings (e.g. DE has Company at col 4, Location at col 8,
    while SWE/AIML/CS have Location at col 4, Company at col 5).

    The grid uses virtual scrolling: only ~25–30 rows are in the DOM at any time.
    We scroll the .antiscroll-inner container in steps and harvest rows at each position.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            "Playwright is required for newgrad-jobs.com scraping. "
            "Install with: pip install playwright && playwright install chromium"
        ) from exc

    label = _NEWGRAD_CATEGORY_LABELS.get(category, category)
    if progress_callback:
        progress_callback(category, "starting", 0, 1, f"Starting {label}")

    # keyed by rowindex to avoid duplicates at step boundaries
    rows_by_index: dict[int, dict] = {}

    # JS template — col indexes are substituted after reading the header
    _JS_EXTRACT_VISIBLE_TMPL = """(colMap) => {
        const riSet = new Set();
        document.querySelectorAll('[data-rowindex]').forEach(el => {
            const ri = el.getAttribute('data-rowindex');
            if (ri !== null && ri !== '' && !isNaN(Number(ri))) riSet.add(Number(ri));
        });
        const results = [];
        for (const ri of riSet) {
            const cell = ci => {
                const el = document.querySelector(
                    `[data-rowindex="${ri}"][data-columnindex="${ci}"]`
                );
                return el ? (el.innerText || '').trim() : '';
            };
            const applyCell = document.querySelector(
                `[data-rowindex="${ri}"][data-columnindex="2"]`
            );
            const linkEl = applyCell
                ? applyCell.querySelector('a[href*="jobright.ai"]')
                : null;
            const href = linkEl ? linkEl.href : '';
            if (!href) continue;
            const workModel = cell(3);
            const loc = cell(colMap.location);
            results.push({
                ri,
                href,
                title:    cell(0),
                posted:   cell(1),
                location: workModel + (loc ? ' \u2013 ' + loc : ''),
                company:  cell(colMap.company),
                salary:   cell(colMap.salary),
            });
        }
        return results;
    }"""

    _JS_READ_HEADERS = """() => {
        // Header cells have a data-columnindex but no data-rowindex
        const map = {};
        document.querySelectorAll('[data-columnindex]').forEach(el => {
            if (el.getAttribute('data-rowindex') !== null) return;
            const ci = el.getAttribute('data-columnindex');
            if (ci === null || map[ci]) return;
            const txt = (el.innerText || '').trim().toLowerCase();
            if (txt) map[ci] = txt;
        });
        return map;
    }"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=REQUEST_HEADERS["User-Agent"],
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()
        if progress_callback:
            progress_callback(category, "loading", 0, 1, f"Loading {label}")
        # Use "commit" so goto returns as soon as the response is received rather
        # than waiting for DOMContentLoaded (which Airtable's SPA fires late).
        # The actual readiness gate is wait_for_selector below.
        for attempt in range(3):
            try:
                page.goto(embed_url, wait_until="commit", timeout=60000)
                break
            except Exception:
                if attempt == 2:
                    raise
        try:
            page.wait_for_selector('a[href*="jobright.ai/jobs"]', timeout=45000)
        except Exception:
            pass

        # Read header row to build field→index map
        raw_headers: dict[str, str] = page.evaluate(_JS_READ_HEADERS)
        # raw_headers: {"0": "position title", "1": "date", "3": "work model", ...}
        name_to_col: dict[str, str] = {v: k for k, v in raw_headers.items()}
        col_map = {
            "location": int(name_to_col.get("location", "4")),
            "company":  int(name_to_col.get("company",  "5")),
            "salary":   int(name_to_col.get("salary",   "6")),
        }

        # Get total scroll height of the grid container
        scroll_height: int = page.evaluate("""() => {
            const c = document.querySelector('.antiscroll-inner');
            return c ? c.scrollHeight : 0;
        }""")

        scroll_pos = 0
        scroll_step = 600   # px per step — keeps ~15 new rows visible each step
        max_iterations = 300
        total_steps = max(1, min(max_iterations, (scroll_height // scroll_step) + 2))

        for iteration in range(max_iterations):
            page.evaluate(f"""() => {{
                const c = document.querySelector('.antiscroll-inner');
                if (c) c.scrollTop = {scroll_pos};
            }}""")
            page.wait_for_timeout(300)

            visible: list[dict] = page.evaluate(_JS_EXTRACT_VISIBLE_TMPL, col_map)
            for row in visible:
                ri = row.get("ri")
                if ri is not None and ri not in rows_by_index and row.get("href"):
                    rows_by_index[ri] = row

            if progress_callback:
                progress_callback(
                    category,
                    "scrolling",
                    min(iteration + 1, total_steps),
                    total_steps,
                    f"Scanning {label}: {len(rows_by_index)} rows found",
                )

            scroll_pos += scroll_step
            if scroll_pos > scroll_height + scroll_step:
                break

        browser.close()

    jobs: list[dict[str, Any]] = []
    for ri in sorted(rows_by_index):
        row = rows_by_index[ri]
        jobs.append({
            "title":    row.get("title") or "(no title)",
            "company":  row.get("company") or "",
            "location": row.get("location") or "",
            "posted":   row.get("posted") or "",
            "salary":   row.get("salary") or "",
            "url":      (row.get("href") or "").strip(),
            "category": label,
        })
    return jobs


def extract_newgrad_jobs(progress_callback: Any = None) -> dict[str, Any]:
    """Scrape all four newgrad-jobs.com categories and deduplicate by job URL."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_jobs: list[dict[str, Any]] = []
    errors: list[str] = []
    categories = dict(_NEWGRAD_CATEGORY_EMBEDS)
    category_progress = {cat: 0.0 for cat in categories}

    def report_category_progress(category: str, phase: str, current: int, total: int, message: str = "") -> None:
        total = max(total, 1)
        if phase == "starting":
            fraction = 0.02
        elif phase == "loading":
            fraction = 0.08
        elif phase == "complete":
            fraction = 1.0
        else:
            fraction = min(max(current / total, 0.0), 0.98)

        category_progress[category] = max(category_progress.get(category, 0.0), fraction)
        if progress_callback:
            percent = int((sum(category_progress.values()) / max(len(category_progress), 1)) * 95)
            progress_callback(
                max(1, min(percent, 95)),
                message or f"Fetching {_NEWGRAD_CATEGORY_LABELS.get(category, category)}",
                {
                    "category": category,
                    "phase": phase,
                    "current": current,
                    "total": total,
                    "categories": category_progress.copy(),
                },
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_scrape_newgrad_category, cat, url, report_category_progress): cat
            for cat, url in categories.items()
        }
        for future in as_completed(futures):
            cat = futures[future]
            try:
                all_jobs.extend(future.result())
                report_category_progress(
                    cat,
                    "complete",
                    1,
                    1,
                    f"Finished {_NEWGRAD_CATEGORY_LABELS.get(cat, cat)}",
                )
            except Exception as exc:
                errors.append(f"{cat}: {exc}")
                report_category_progress(
                    cat,
                    "complete",
                    1,
                    1,
                    f"Skipped {_NEWGRAD_CATEGORY_LABELS.get(cat, cat)} after an error",
                )

    # Deduplicate by canonical job URL (strip UTM params)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for job in all_jobs:
        canonical = re.split(r"[?#]", job["url"])[0].rstrip("/")
        if canonical not in seen:
            seen.add(canonical)
            unique.append(job)

    if progress_callback:
        progress_callback(100, f"Loaded {len(unique)} jobs", {"phase": "complete"})

    return {"jobs": unique, "errors": errors}


def extract_jobs(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if "careers.micron.com" in hostname:
        return extract_micron_jobs(url)
    if "careers.mckesson.com" in hostname:
        return extract_mckesson_jobs(url)
    if "jobs.intuit.com" in hostname:
        return extract_intuit_jobs(url)
    if "jobs.bms.com" in hostname:
        return extract_bms_jobs(url)
    if "careers.stryker.com" in hostname:
        return extract_stryker_jobs(url)
    if "jobs.dell.com" in hostname:
        return extract_dell_jobs(url)
    if "myworkdayjobs.com" in hostname:
        return extract_workday_jobs(url)
    if "capitalonecareers.com" in hostname:
        return extract_capitalone_jobs(url)
    if "careers.progressive.com" in hostname:
        return extract_progressive_jobs(url)
    if "greenhouse.io" in hostname:
        return extract_greenhouse_jobs(url)
    if "corningjobs.corning.com" in hostname:
        return extract_corning_jobs(url)
    if "applovin.com" in hostname:
        return extract_applovin_jobs(url)
    if "careers.spglobal.com" in hostname:
        return extract_spglobal_jobs(url)
    if "jobs.paloaltonetworks.com" in hostname:
        return extract_paloalto_jobs(url)
    if "careers.qualcomm.com" in hostname:
        return extract_qualcomm_jobs(url)
    if "lockheedmartinjobs.com" in hostname:
        return extract_lockheed_jobs(url)
    if "careers.honeywell.com" in hostname:
        return extract_honeywell_jobs(url)
    if "uber.com" in hostname:
        return extract_uber_jobs(url)
    if "jobs.boeing.com" in hostname:
        return extract_boeing_jobs(url)
    if "schwabjobs.com" in hostname:
        return extract_schwab_jobs(url)
    if "disneycareers.com" in hostname:
        return extract_disney_jobs(url)
    if "careers.salesforce.com" in hostname:
        return extract_salesforce_jobs(url)
    if "arista.com" in hostname:
        return extract_arista_jobs(url)

    raise ValueError(f"Unsupported URL host: {hostname}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch career listings from Micron, McKesson, Intuit, BMS, Prologis, Stryker, Dell, Corning, AppLovin, S&P Global, Palo Alto Networks, Qualcomm, Lockheed Martin, or Workday-based sites and print titles, location, posted date, and URL."
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="One or more careers page URLs to fetch. If omitted, the built-in default list will be used.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of jobs to print per URL.",
    )
    args = parser.parse_args()

    urls = args.urls if args.urls else DEFAULT_URLS
    for url in urls:
        print(f"=== {url} ===")
        try:
            data = extract_jobs(url)
        except Exception as exc:
            print(f"Error fetching jobs from {url}: {exc}\n")
            continue

        total_jobs = data["total_jobs"]
        jobs = data["jobs"]

        if total_jobs is not None:
            print(f"Total jobs found on page: {total_jobs}")
        print(f"Extracted {len(jobs)} job cards; showing up to {args.limit}:\n")

        for job in jobs[: args.limit]:
            print(f"- {job['title']}")
            print(f"  Location: {job['location']}")
            if job["posted"]:
                print(f"  {job['posted']}")
            print(f"  URL: {job['url']}\n")


if __name__ == "__main__":
    main()
