from __future__ import annotations

import application_agent.agent.preferences as preferences_module
from application_agent.agent.apply_queue import ApplyQueue
from application_agent.agent.preferences import job_matches_preferences
from application_agent.queued_apply import preference_skip_report, run_queued_job


def test_apply_queue_shortlists_jobs_idempotently_and_logs_reports(tmp_path):
    queue = ApplyQueue(tmp_path / "applications.sqlite3")
    first = queue.add_shortlist(
        {
            "source": "newgrad",
            "title": "ML Engineer",
            "company": "Example",
            "location": "Chicago, IL",
            "url": "https://jobs.example.test/1",
            "category": "AI/ML",
        }
    )
    second = queue.add_shortlist(
        {
            "source": "newgrad",
            "title": "ML Engineer Updated",
            "company": "Example",
            "url": "https://jobs.example.test/1",
            "priority": 3,
        }
    )

    assert first["id"] == second["id"]
    assert second["title"] == "ML Engineer Updated"
    assert second["priority"] == 3
    assert queue.shortlisted_urls() == {"https://jobs.example.test/1"}

    queued = queue.update_status(second["id"], "queued")
    assert queued["status"] == "queued"
    assert len(queue.list_shortlist("queued")) == 1

    report = queue.log_report(
        job_id=second["id"],
        url="https://jobs.example.test/1/apply",
        status="paused_before_submit",
        report={
            "status": "paused_before_submit",
            "filled": 12,
            "skipped": 2,
            "corrected": 1,
            "confidence": 0.86,
        },
    )

    assert report["filled_count"] == 12
    assert report["skipped_count"] == 2
    assert report["corrected_count"] == 1
    assert report["report"]["confidence"] == 0.86
    assert queue.list_reports(job_id=second["id"])[0]["id"] == report["id"]


def test_preferences_filter_jobs_with_blacklists_and_locations():
    preferences = {
        "positions": ["machine learning engineer"],
        "locations": ["chicago", "remote"],
        "remote": True,
        "hybrid": True,
        "onsite": True,
        "companyBlacklist": ["BadCo"],
        "titleBlacklist": ["principal"],
    }

    assert job_matches_preferences(
        {"title": "Machine Learning Engineer", "company": "GoodCo", "location": "Chicago, IL"},
        preferences,
    ) == (True, [])
    assert job_matches_preferences(
        {"title": "Principal Machine Learning Engineer", "company": "GoodCo", "location": "Chicago, IL"},
        preferences,
    ) == (False, ["title_blacklist"])
    assert job_matches_preferences(
        {"title": "Machine Learning Engineer", "company": "BadCo", "location": "Chicago, IL"},
        preferences,
    ) == (False, ["company_blacklist"])
    assert job_matches_preferences(
        {"title": "Backend Engineer", "company": "GoodCo", "location": "Seattle, WA"},
        preferences,
    ) == (False, ["position_not_preferred", "location_not_preferred"])


def test_load_preferences_falls_back_to_example_defaults(tmp_path, monkeypatch):
    private = tmp_path / "preferences.private.json"
    example = tmp_path / "preferences.example.json"
    example.write_text('{"titleBlacklist": ["sales"]}', encoding="utf-8")
    monkeypatch.setattr(preferences_module, "DEFAULT_PREFERENCES", private)
    monkeypatch.setattr(preferences_module, "EXAMPLE_PREFERENCES", example)

    assert preferences_module.load_preferences(private) == {"titleBlacklist": ["sales"]}

    private.write_text('{"titleBlacklist": ["marketing"]}', encoding="utf-8")
    assert preferences_module.load_preferences(private) == {"titleBlacklist": ["marketing"]}

    assert preferences_module.load_preferences(tmp_path / "elsewhere.json") == {}


def test_queued_runner_skips_jobs_that_fail_preferences():
    job = {
        "id": 7,
        "title": "Sales Lead",
        "company": "BadCo",
        "location": "Chicago, IL",
        "url": "https://jobs.example.test/7",
    }
    preferences = {"companyBlacklist": ["BadCo"], "titleBlacklist": ["sales"]}

    report = preference_skip_report(job, preferences)
    assert report["status"] == "skipped"
    assert set(report["skipReasons"]) == {"company_blacklist", "title_blacklist"}
    assert report["job"]["company"] == "BadCo"

    matching_job = {"id": 1, "title": "ML Engineer", "company": "GoodCo", "location": "Remote", "url": "u"}
    assert preference_skip_report(matching_job, preferences) is None
    assert preference_skip_report(job, {}) is None

    class FakeQueue:
        def __init__(self) -> None:
            self.statuses: list[tuple[int, str]] = []
            self.reports: list[tuple[int, str]] = []

        def update_status(self, job_id: int, status: str) -> None:
            self.statuses.append((job_id, status))

        def log_report(self, job_id: int, url: str, status: str, report: dict) -> None:
            self.reports.append((job_id, status))

    queue = FakeQueue()
    result = run_queued_job(page=None, agent=None, queue=queue, job=job, preferences=preferences)

    assert result["status"] == "skipped"
    assert queue.statuses == [(7, "skipped")]
    assert queue.reports == [(7, "skipped")]
