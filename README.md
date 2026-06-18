# ApplyPilot

An application-assist system for tracking software, AI/ML, data, and cybersecurity job opportunities, then helping complete job applications through a Chrome extension and post-apply agent.

The app currently has three tabs:

- **Job Scraper** - pulls live postings from configured company career sites, scores them for technical relevance, and filters by search text and location.
- **New Grad Jobs** - scrapes newgrad-jobs.com categories for early-career roles.
- **Applied Jobs** - searches your Outlook mailbox with Microsoft Graph and groups job-related emails into applications, interviews, and rejections.

This repo also now contains two standalone application-assist tools:

- **Chrome autofill extension** in `autofill_extension/` for in-browser form scanning and review-first filling.
- **Post-apply Playwright agent** in `application_agent/` for taking over after an application page opens and safely filling ATS flows.

---

## Quick Start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install the browser used by Playwright-based scrapers
playwright install chromium

# 4. Start the Flask app
python app.py
```

Open the dashboard at:

```text
http://127.0.0.1:5003
```

---

## Features

### Job Scraper

- Fetches jobs from the configured URLs in `extract_jobs.DEFAULT_URLS`.
- Runs site extractors concurrently and keeps the last cached results visible while refreshes happen in the background.
- Auto-refreshes the page while a fetch is running.
- Normalizes many posted-date formats, including ISO dates, US dates, `Today`, `Yesterday`, and relative dates like `3 days ago`.
- Scores roles for technical relevance using software engineering, data, AI/ML, infrastructure, security, and technical leadership keywords.
- Filters out common non-technical roles such as finance, legal, retail, healthcare, hospitality, and facilities roles.
- Supports UI filters for:
  - Relevant jobs vs. all jobs
  - US / Canada / Remote vs. any location
  - Free-text search across title, location, and source

### New Grad Jobs

- Scrapes newgrad-jobs.com category embeds in parallel.
- Deduplicates jobs by canonical URL.
- Shows title, company, location, salary, posted date, category, and link.
- Supports category filtering and free-text search.
- Uses a separate 30-minute cache so it does not block the main scraper.

### Applied Jobs Tracker

- Uses a Microsoft Graph access token from Graph Explorer to search your Outlook inbox.
- Searches job-related subject lines from January 1, 2026 onward.
- Classifies emails as:
  - `Applied`
  - `Interview`
  - `Rejected`
- Discards job alerts, newsletters, recruiter marketing, GitHub/OAuth noise, and calendar system messages.
- Deduplicates email threads by normalized subject and sender domain.
- Shows summary counts, status tabs, search, sender/company, received time, and Outlook web links.
- Caches results per token for 5 minutes.

Token note: the app does not write your Graph token to disk or logs. The token is kept in the Flask session for the current browser session and Graph Explorer tokens typically expire after about an hour.

---

## Microsoft Graph Setup

To use the **Applied Jobs** tab:

1. Go to [Microsoft Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer).
2. Sign in with the Outlook or university account you want to search.
3. Make sure the token has `Mail.Read` permission.
4. Copy the access token.
5. Paste it into the Applied Jobs page and click **Load emails**.

Use **Clear token** on the page when you want to remove the token from the session.

---

## Standalone Extractor CLI

You can also run the scraper extractors without the Flask UI:

```bash
# Use all default configured career URLs
python extract_jobs.py --limit 20

# Scrape a specific supported career URL
python extract_jobs.py "https://jobs.intuit.com/search-jobs" --limit 10
```

The CLI prints each source, total jobs found when available, and the first matching job cards.

---

## Application Assist Tools

The Chrome extension and Playwright completion agent are separate from the Flask dashboard.

They are designed to be reusable. Each user should keep their own profile, resume, API key, browser session, and application logs in gitignored local files.

### Set Up A Personal Application Profile

Copy the sample profile:

```bash
cp autofill_extension/profile.example.json autofill_extension/profile.private.json
```

Then edit the ignored private copy with your own information:

- contact details, links, school, degree, and expected graduation date
- Canada and/or USA addresses
- work authorization, sponsorship, veteran, relocation, salary, and consent defaults
- work experience, education, projects, and websites
- optional demographic answers, only if you want the tool to fill them
- `resumeFileName`, matching a file in `autofill_extension/resumes/`

Private files are ignored by git:

```text
autofill_extension/profile.private.json
autofill_extension/resumes/
autofill_extension/generated/
autofill_extension/backend/env.private
application_agent/browser_profile/
application_agent/data/
application_agent/files/
application_agent/*.private.json
```

### Configure The AI Backend

Create:

```text
autofill_extension/backend/env.private
```

Use your own NVIDIA API key:

```text
NVIDIA_API_KEY=your-key-here
NVIDIA_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
PORT=8000
```

Start the local mapper/tracking backend:

```bash
python autofill_extension/backend/server.py
```

The backend lets the extension and agent ask one structured AI request for visible missing fields, constrained to the actual dropdown/radio options when those options are available.

### Use The Chrome Extension

Load the extension from:

```text
autofill_extension/
```

Then:

1. Open `chrome://extensions`.
2. Enable Developer Mode.
3. Click **Load unpacked** and choose `autofill_extension/`.
4. Open the extension options page.
5. Import `autofill_extension/profile.private.json`.
6. Set backend base URL to `http://127.0.0.1:8000`.
7. Save, open an application page, preview, fill, and review before submitting.

### Use The Post-Apply Agent

Run the Playwright agent with:

```bash
python -m application_agent.main
```

The agent uses the same ignored private extension profile at:

```text
autofill_extension/profile.private.json
```

It opens a persistent Playwright browser, waits for you to click Apply, detects Greenhouse/Lever/Ashby/Workday/generic pages, fills what it can, uploads the ignored local resume when a file input allows it, logs to ignored SQLite storage, and pauses before final submission.

The intended workflow is:

```text
Open/click Apply
  -> detect ATS
  -> scan visible fields and dropdown options
  -> fill profile-backed fields
  -> ask AI for visible missing fields when needed
  -> choose only supplied options for dropdown/radio fields
  -> upload resume when allowed
  -> click safe Next/Continue steps
  -> stop before final Submit
```

See:

```text
autofill_extension/README.md
application_agent/README.md
```

---

## Supported Sources

The default source list includes career pages for companies such as Micron, McKesson, CME Group, Vertex, Capital One, Progressive, Interactive Brokers, Intuit, Bristol Myers Squibb, Prologis, Stryker, Dell, Corning, AppLovin, S&P Global, Palo Alto Networks, Qualcomm, Lockheed Martin, Honeywell, Uber, BlackRock, Analog Devices, Arista, Boeing, Schwab, Disney, and Salesforce.

`extract_jobs.py` has dedicated extractors for several platforms and sites, including:

- Workday / myworkdayjobs
- Greenhouse
- TalentBrew-style boards
- Oracle HCM-style boards
- Salesforce careers
- newgrad-jobs.com embeds
- Site-specific pages that require custom parsing or Playwright rendering

External career sites change markup frequently, so extraction errors are captured and shown in the UI instead of crashing the dashboard.

---

## Project Layout

```text
hr_system/
├── app.py                  Flask routes, caches, filters, and email classification
├── extract_jobs.py         Career-site and new-grad scraper implementations
├── requirements.txt        Runtime dependencies
├── requirements-dev.txt    Test dependencies
├── pytest.ini              Pytest configuration
├── conftest.py             Test setup
├── templates/
│   ├── index.html          Main job scraper dashboard
│   ├── newgrad.html        New-grad jobs tab
│   └── applied.html        Outlook applied-jobs tracker
└── tests/
    └── test_extractors.py  Date parsing, relevance filters, and extractor tests
```

---

## Testing

```bash
pip install -r requirements-dev.txt

# Fast/unit-oriented tests
pytest tests/ -v -m "not slow"

# Full suite, including live external scraper checks
pytest tests/ -v
```

Some integration tests call live career sites and can fail when a site is down, rate-limited, geoblocked, or changes its HTML.

---

## Development Notes

- Main dashboard cache TTL: 5 minutes.
- New-grad cache TTL: 30 minutes.
- Applied-email cache TTL: 5 minutes per access token.
- Flask runs locally on `127.0.0.1:5003` in debug mode when started with `python app.py`.
- Add new company sources by adding a URL to `DEFAULT_URLS` and either reusing an existing hostname extractor or adding a new extractor in `extract_jobs.py`.
