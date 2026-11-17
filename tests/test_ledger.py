"""One tracking ledger (roadmap Phase 2 #1): the backend's Track endpoint, the dashboard's
apply queue and the application agent's logger all write to one store."""

import hashlib
import sqlite3

from application_agent.agent.apply_queue import ApplyQueue
from application_agent.agent.ledger import Ledger, normalize_ats_job_id
from application_agent.agent.logger import ApplicationLogger
from autofill_extension.backend import server


def _jobs(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT id, url, status, ats, ats_job_id FROM shortlisted_jobs ORDER BY id").fetchall()


def test_record_upserts_by_url_and_appends_events(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    first = ledger.record("https://example.com/jobs/1", status="filling", source="application_agent", title="Data Engineer")
    second = ledger.record("https://example.com/jobs/1", status="filled", source="chrome_extension", details={"filledCount": 4})

    assert first == second
    assert len(_jobs(tmp_path / "ledger.sqlite3")) == 1
    assert [event["status"] for event in ledger.events(first)] == ["filling", "filled"]
    assert ledger.events(first)[1]["details"] == {"filledCount": 4}


def test_record_matches_on_ats_job_id(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    first = ledger.record("https://boards.greenhouse.io/acme/jobs/123", status="filled", source="chrome_extension")
    second = ledger.record("https://boards.greenhouse.io/acme/jobs/123?gh_src=abc", status="submitted", source="application_agent")

    assert first == second
    assert _jobs(tmp_path / "ledger.sqlite3")[0][2:] == ("submitted", "greenhouse", "123")


def test_normalize_ats_job_id():
    assert normalize_ats_job_id("https://job-boards.greenhouse.io/acme/jobs/4567?gh_src=x") == ("greenhouse", "4567")
    assert normalize_ats_job_id("https://acme.com/careers?gh_jid=4567") == ("greenhouse", "4567")
    uuid = "0b7a4c2e-1111-4222-8333-944455556666"
    assert normalize_ats_job_id(f"https://jobs.lever.co/acme/{uuid}/apply") == ("lever", uuid)
    assert normalize_ats_job_id(f"https://jobs.ashbyhq.com/acme/{uuid}/application") == ("ashby", uuid)
    assert normalize_ats_job_id("https://example.com/jobs/1") == (None, None)


def test_status_moves_forward_only(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    job = ledger.record("https://example.com/jobs/2", status="submitted", source="application_agent")

    ledger.record("https://example.com/jobs/2", status="filled", source="chrome_extension")
    ledger.record("https://example.com/jobs/2", status="weird", source="chrome_extension")

    assert _jobs(tmp_path / "ledger.sqlite3")[0][2] == "submitted"
    assert [event["status"] for event in ledger.events(job)] == ["submitted", "filled", "weird"]


def _legacy_db(path, rows):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, title TEXT, status TEXT,"
            " filled_count INTEGER DEFAULT 0, mapped_count INTEGER DEFAULT 0, source TEXT, created_at TEXT NOT NULL,"
            " payload_json TEXT NOT NULL)"
        )
        for url, title, filled in rows:
            connection.execute(
                "INSERT INTO applications (url, title, status, filled_count, mapped_count, source, created_at, payload_json)"
                " VALUES (?, ?, 'filled', ?, 5, 'chrome_extension', '2026-10-01T00:00:00+00:00', '{}')",
                (url, title, filled),
            )


def test_legacy_backend_rows_copied_once_and_source_untouched(tmp_path):
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy, [("https://example.com/jobs/9", "Old Role", 3), ("https://example.com/jobs/9", "Old Role", 4)])
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()

    Ledger(tmp_path / "ledger.sqlite3", legacy_db=legacy).list_jobs()
    ledger = Ledger(tmp_path / "ledger.sqlite3", legacy_db=legacy)
    jobs = ledger.list_jobs()

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Old Role"
    assert jobs[0]["filled_count"] == 4
    assert len(ledger.events(jobs[0]["id"])) == 2
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_ledger_at_a_custom_path_never_reads_the_real_backend_file(tmp_path, monkeypatch):
    legacy = tmp_path / "real_backend.sqlite3"
    _legacy_db(legacy, [("https://example.com/jobs/10", "Real Role", 1)])
    monkeypatch.setattr("application_agent.agent.ledger.LEGACY_BACKEND_DB", legacy)

    assert Ledger(tmp_path / "other.sqlite3").list_jobs() == []


def test_track_application_twice_upserts_one_job(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "missing.sqlite3")
    client = server.app.test_client()
    body = {"url": "https://example.com/jobs/3", "title": "Example Role", "status": "filled", "filledCount": 2, "mappedCount": 3}

    first = client.post("/track-application", json=body).get_json()
    second = client.post("/track-application", json={**body, "filledCount": 5}).get_json()
    listed = client.get("/applications").get_json()["applications"]

    assert first["tracked"] is True and first["id"] == second["id"]
    assert len(listed) == 1
    assert listed[0]["url"] == "https://example.com/jobs/3"
    assert listed[0]["filled_count"] == 5
    assert listed[0]["mapped_count"] == 3
    assert listed[0]["status"] == "filled"
    assert set(listed[0]) == {"id", "url", "title", "status", "filled_count", "mapped_count", "source", "created_at"}


def test_logger_writes_through_ledger(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ApplicationLogger(path).log(url="https://jobs.lever.co/acme/1", title="Role", ats="lever", status="paused_before_submit", details={"steps": []})

    ledger = Ledger(path)
    [job] = ledger.list_jobs()
    assert job["status"] == "paused"
    assert ledger.events(job["id"])[0]["details"]["agentStatus"] == "paused_before_submit"
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "applications" not in tables


def test_queue_update_status_and_report_append_events(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    queue = ApplyQueue(path)
    job = queue.add_shortlist({"url": "https://example.com/jobs/4", "title": "Queued Role"})

    queue.update_status(job["id"], "queued")
    queue.log_report(job_id=job["id"], url=job["url"], status="submitted", report={"filled": 3})

    statuses = [event["status"] for event in Ledger(path).events(job["id"])]
    assert statuses == ["queued", "submitted"]
    assert queue.list_reports(job["id"])[0]["filled_count"] == 3


def test_extension_tracked_job_shows_on_the_dashboard_queue(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    monkeypatch.setattr(server, "LEDGER_PATH", path)
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "missing.sqlite3")
    server.app.test_client().post("/track-application", json={"url": "https://example.com/jobs/5", "title": "Tracked"})

    assert [(job["url"], job["status"]) for job in ApplyQueue(path).list_shortlist("filled")] == [("https://example.com/jobs/5", "filled")]


# --- Review fixes (2026-11-16 /code-review) ---------------------------------------------------


def test_legacy_rows_without_url_are_skipped_and_import_still_completes(tmp_path):
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy, [("", "No URL", 1), ("https://example.com/jobs/11", "Good Role", 2)])

    first = Ledger(tmp_path / "ledger.sqlite3", legacy_db=legacy).list_jobs()
    second = Ledger(tmp_path / "ledger.sqlite3", legacy_db=legacy).list_jobs()

    assert [job["url"] for job in first] == ["https://example.com/jobs/11"]
    assert second == first


def test_track_application_without_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "ledger.sqlite3")
    response = server.app.test_client().post("/track-application", json={"title": "No URL"})

    assert response.status_code == 400
    assert response.get_json()["tracked"] is False


def test_track_application_keeps_client_created_at(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "ledger.sqlite3")
    body = {"url": "https://example.com/jobs/12", "createdAt": "2026-11-01T10:00:00+00:00"}
    job_id = server.app.test_client().post("/track-application", json=body).get_json()["id"]

    assert Ledger(tmp_path / "ledger.sqlite3").events(job_id)[0]["created_at"] == "2026-11-01T10:00:00+00:00"


def test_queued_agent_run_logs_onto_the_queued_job(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    queued = ApplyQueue(path).add_shortlist({"url": "https://acme.com/careers/42", "title": "Queued"})

    ApplicationLogger(path).log(
        url="https://acme.wd5.myworkdayjobs.com/en-US/careers/job/42/apply",
        job_url="https://acme.com/careers/42",
        title="Queued",
        ats="workday",
        status="paused_before_submit",
    )

    jobs = _jobs(path)
    assert len(jobs) == 1
    [event] = Ledger(path).events(queued["id"])
    assert event["details"]["pageUrl"] == "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/42/apply"


def test_shortlisted_job_gets_its_ats_id_and_matches_a_tracked_variant(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    shortlisted = ApplyQueue(path).add_shortlist({"url": "https://acme.com/careers?gh_jid=123", "title": "Role"})

    tracked = Ledger(path).record("https://boards.greenhouse.io/acme/jobs/123", status="filled", source="chrome_extension")

    assert tracked == shortlisted["id"]
    assert len(_jobs(path)) == 1


def test_shortlisting_a_variant_of_a_tracked_job_does_not_duplicate_it(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    tracked = Ledger(path).record("https://boards.greenhouse.io/acme/jobs/321", status="filled", source="chrome_extension")

    shortlisted = ApplyQueue(path).add_shortlist({"url": "https://acme.com/careers?gh_jid=321", "title": "Role", "company": "Acme"})

    assert shortlisted["id"] == tracked
    assert shortlisted["status"] == "filled"
    assert len(_jobs(path)) == 1


def test_caller_ats_does_not_split_a_known_job(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    first = ledger.record("https://acme.com/careers?gh_jid=555", status="filled", source="chrome_extension")

    second = ledger.record("https://boards.greenhouse.io/acme/jobs/555", status="paused", source="application_agent", ats="generic")

    assert second == first
