from __future__ import annotations

from typing import Any

from application_agent.agent.detector import detect_ats
from application_agent.agent.answer_generator import load_standard_answers
from application_agent.agent.logger import ApplicationLogger
from application_agent.ats.router import get_adapter


class ApplicationAgent:
    def __init__(self, profile: dict[str, Any], logger: ApplicationLogger | None = None, max_steps: int = 10) -> None:
        self.profile = profile
        self.profile["_standard_answers"] = load_standard_answers()
        self.logger = logger or ApplicationLogger()
        self.max_steps = max_steps

    def complete_current_application(self, page: Any) -> dict[str, Any]:
        page_text = safe_body_text(page)
        ats = detect_ats(page.url, page_text)
        adapter = get_adapter(ats)
        status = "started"
        details: dict[str, Any] = {"steps": []}

        for step in range(self.max_steps):
            result = adapter.fill_current_page(page, self.profile)
            details["steps"].append(result)

            if adapter.is_final_submit_page(page):
                status = "paused_before_submit"
                break

            next_button = adapter.find_safe_next_button(page)
            if not next_button:
                status = "paused_for_review"
                break

            next_button.click()
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            page.wait_for_timeout(750)
        else:
            status = "max_steps_reached"

        report = {
            "url": page.url,
            "title": safe_title(page),
            "ats": ats,
            "status": status,
            "filled": sum(int(step.get("filled") or 0) for step in details["steps"]),
            "skipped": sum(len(step.get("unmapped") or []) for step in details["steps"]),
            "corrected": 0,
            "confidence": report_confidence(details),
            "details": details,
        }
        self.logger.log(url=page.url, title=report["title"], ats=ats, status=status, details=details)
        return report


def safe_body_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return ""


def safe_title(page: Any) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def report_confidence(details: dict[str, Any]) -> float:
    matched = 0
    mismatched = 0
    unreadable = 0
    skipped = 0

    for step in details.get("steps", []):
        verification = step.get("verification") or {}
        matched += int(verification.get("matched") or 0)
        mismatched += len(verification.get("mismatched") or [])
        unreadable += len(verification.get("unreadable") or [])
        skipped += len(step.get("unmapped") or [])

    total = matched + mismatched + unreadable + skipped
    if total <= 0:
        return 0.0

    return round(max(0.0, matched / total), 3)
