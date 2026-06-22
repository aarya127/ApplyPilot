from __future__ import annotations

from application_agent.agent.apply_queue import ApplyQueue
from application_agent.agent.preferences import job_matches_preferences


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
