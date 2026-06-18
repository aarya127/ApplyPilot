from __future__ import annotations

from datetime import datetime

import app
import extract_jobs


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
        app._newgrad_cache["updated_at"] = datetime(2026, 6, 18, 0, 44, 56)
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
        app._newgrad_cache["updated_at"] = datetime(2026, 6, 18, 0, 44, 56)
        app._newgrad_progress["percent"] = 100
        app._newgrad_progress["message"] = "Loaded 1 jobs"

    response = app.app.test_client().get("/newgrad")
    html = response.data.decode()

    assert response.status_code == 200
    assert 'id="progressPanel"' in html
    assert 'hidden style="display: none;"' in html
