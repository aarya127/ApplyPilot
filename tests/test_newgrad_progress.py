from __future__ import annotations

from datetime import datetime

import app
import extract_jobs
from application_agent.agent.apply_queue import ApplyQueue


def test_extract_newgrad_jobs_reports_progress(monkeypatch):
    monkeypatch.setattr(
        extract_jobs,
        "_NEWGRAD_CATEGORY_EMBEDS",
        {"swe": "https://example.test/swe", "aiml": "https://example.test/aiml"},
    )

    def fake_scrape(category, embed_url, progress_callback=None):
        if progress_callback:
            progress_callback(category, "starting", 0, 1, f"Starting {category}")
            progress_callback(category, "scrolling", 1, 2, f"Scanning {category}")
        return [
            {
                "title": f"{category} role",
                "company": "Example",
                "location": "Remote",
                "posted": "2026-06-18",
                "salary": "",
                "url": f"https://jobs.example.test/{category}",
                "category": category,
            }
        ]

    monkeypatch.setattr(extract_jobs, "_scrape_newgrad_category", fake_scrape)
    events = []

    result = extract_jobs.extract_newgrad_jobs(
        progress_callback=lambda percent, message, detail: events.append((percent, message, detail))
    )

    assert len(result["jobs"]) == 2
    assert events
    assert events[-1][0] == 100
    assert events[-1][2]["phase"] == "complete"


def test_newgrad_status_returns_serializable_progress():
    with app._newgrad_cache_lock:
        app._newgrad_cache_loading = True
        app._newgrad_cache["jobs"] = [{"title": "Example"}]
        app._newgrad_cache["errors"] = []
        app._newgrad_cache["updated_at"] = datetime.now()
        app._newgrad_progress["percent"] = 42
        app._newgrad_progress["message"] = "Scanning Software Engineering"
        app._newgrad_progress["detail"] = {"phase": "scrolling"}
        app._newgrad_progress["started_at"] = datetime(2026, 6, 18, 0, 40, 0)
        app._newgrad_progress["updated_at"] = datetime(2026, 6, 18, 0, 41, 0)

    response = app.app.test_client().get("/newgrad/status")

    assert response.status_code == 200
    assert response.json["loading"] is True
    assert response.json["progress"]["percent"] == 42
    assert response.json["progress"]["started_at"] == "2026-06-18T00:40:00"
    assert response.json["jobCount"] == 1

    with app._newgrad_cache_lock:
        app._newgrad_cache_loading = False


def test_newgrad_progress_panel_hidden_after_load_completes():
    with app._newgrad_cache_lock:
        app._newgrad_cache_loading = False
        app._newgrad_cache["jobs"] = [
            {
                "title": "Example role",
                "company": "Example",
                "location": "Remote",
                "salary": "",
                "posted": "2026-06-18",
                "category": "Software Engineering",
                "url": "https://jobs.example.test/role",
            }
        ]
        app._newgrad_cache["errors"] = []
        app._newgrad_cache["updated_at"] = datetime.now()
        app._newgrad_progress["percent"] = 100
        app._newgrad_progress["message"] = "Loaded 1 jobs"

    response = app.app.test_client().get("/newgrad")
    html = response.data.decode()

    assert response.status_code == 200
    assert 'id="progressPanel"' in html
    assert 'hidden style="display: none;"' in html


def test_shortlist_routes_add_queue_and_render_reports(tmp_path, monkeypatch):
    queue = ApplyQueue(tmp_path / "applications.sqlite3")
    monkeypatch.setattr(app, "apply_queue", queue)
    client = app.app.test_client()

    response = client.post(
        "/shortlist",
        data={
            "source": "newgrad",
            "title": "Backend Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://jobs.example.test/backend",
            "category": "Software Engineering",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    jobs = queue.list_shortlist()
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Backend Engineer"

    status_response = client.post(
        f"/shortlist/{jobs[0]['id']}/status",
        json={"status": "queued"},
    )
    assert status_response.status_code == 200
    assert status_response.json["job"]["status"] == "queued"

    queue.log_report(
        job_id=jobs[0]["id"],
        url=jobs[0]["url"],
        status="paused_before_submit",
        report={"status": "paused_before_submit", "filled": 5, "skipped": 1, "confidence": 0.8},
    )

    page = client.get("/shortlist")
    html = page.data.decode()
    assert page.status_code == 200
    assert "Backend Engineer" in html
    assert "filled 5" in html

    reports = client.get(f"/application-reports?job_id={jobs[0]['id']}")
    assert reports.status_code == 200
    assert reports.json["reports"][0]["status"] == "paused_before_submit"
