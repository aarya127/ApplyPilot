from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from application_agent.agent.apply_queue import ApplyQueue
from application_agent.agent.profile_loader import load_profile
from application_agent.agent.runner import ApplicationAgent


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "autofill_extension/profile.private.json"
DEFAULT_BROWSER_PROFILE = ROOT / "application_agent/browser_profile"


def main() -> None:
    args = parse_args()
    profile = load_profile(args.profile)
    queue = ApplyQueue(args.db)
    jobs = queue.list_shortlist("queued")[: args.limit]

    if not jobs:
        print("No queued jobs found. Add jobs from /newgrad or /shortlist first.")
        return

    agent = ApplicationAgent(profile)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(args.browser_profile),
            headless=args.headless,
            args=["--start-maximized"],
        )
        try:
            page = context.pages[-1] if context.pages else context.new_page()

            for job in jobs:
                run_queued_job(page, agent, queue, job)
        finally:
            context.close()


def run_queued_job(page: Any, agent: ApplicationAgent, queue: ApplyQueue, job: dict[str, Any]) -> dict[str, Any]:
    queue.update_status(int(job["id"]), "running")
    report: dict[str, Any] | None = None
    status_recorded = False

    try:
        page.goto(job["url"], wait_until="domcontentloaded", timeout=45_000)
        click_apply_if_available(page)
        report = agent.complete_current_application(page)
        report["job"] = {
            "id": job.get("id"),
            "title": job.get("title"),
            "company": job.get("company"),
            "sourceUrl": job.get("url"),
        }
        queue.log_report(job_id=int(job["id"]), url=page.url, status=report["status"], report=report)
        queue.update_status(int(job["id"]), status_for_report(report))
        status_recorded = True
        print(f"{job.get('company') or 'Unknown'} - {job.get('title') or 'Untitled'}: {report['status']}")
        return report
    except Exception as exc:
        report = {
            "url": job.get("url", ""),
            "title": job.get("title", ""),
            "ats": "unknown",
            "status": "failed",
            "filled": 0,
            "skipped": 0,
            "corrected": 0,
            "confidence": 0.0,
            "error": str(exc),
        }
        queue.log_report(job_id=int(job["id"]), url=job.get("url", ""), status="failed", report=report)
        queue.update_status(int(job["id"]), "failed")
        status_recorded = True
        print(f"{job.get('company') or 'Unknown'} - {job.get('title') or 'Untitled'}: failed ({exc})")
        return report
    finally:
        if not status_recorded:
            queue.update_status(int(job["id"]), "paused" if report else "failed")


def click_apply_if_available(page: Any) -> bool:
    patterns = [
        re.compile(r"^apply$", re.I),
        re.compile(r"apply now", re.I),
        re.compile(r"start application", re.I),
        re.compile(r"submit application", re.I),
    ]

    for role in ["link", "button"]:
        for pattern in patterns:
            try:
                locator = page.get_by_role(role, name=pattern).first
                if locator.count() and locator.is_visible(timeout=1_000):
                    locator.click(timeout=3_000)
                    wait_after_apply_click(page)
                    return True
            except Exception:
                continue

    return False


def wait_after_apply_click(page: Any) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    try:
        page.wait_for_timeout(750)
    except Exception:
        pass


def status_for_report(report: dict[str, Any]) -> str:
    status = str(report.get("status") or "")
    if status in {"paused_before_submit", "paused_for_review", "max_steps_reached"}:
        return "paused"
    if status == "submitted":
        return "submitted"
    if status == "failed":
        return "failed"
    return "paused"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ApplyPilot over queued shortlisted jobs.")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--db", type=Path, default=ApplyQueue().db_path)
    parser.add_argument("--browser-profile", type=Path, default=DEFAULT_BROWSER_PROFILE)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
