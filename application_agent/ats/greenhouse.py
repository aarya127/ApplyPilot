from __future__ import annotations

from typing import Any

from application_agent.ats.base import BaseAdapter


class GreenhouseAdapter(BaseAdapter):
    name = "greenhouse"

    def fill_current_page(self, page: Any, profile: dict[str, Any]) -> dict[str, Any]:
        direct = {
            "input[name='job_application[first_name]']": profile.get("first_name", ""),
            "input[name='job_application[last_name]']": profile.get("last_name", ""),
            "input[name='job_application[email]']": profile.get("email", ""),
            "input[name='job_application[phone]']": profile.get("phone", ""),
        }
        direct_filled = 0

        for selector, value in direct.items():
            if value and safe_fill(page, selector, value):
                direct_filled += 1

        result = super().fill_current_page(page, profile)
        result["filled"] += direct_filled
        result["directFilled"] = direct_filled
        return result


def safe_fill(page: Any, selector: str, value: str) -> bool:
    locator = page.locator(selector)
    if locator.count() == 0:
        return False

    try:
        locator.first.fill(value, timeout=2_000)
        return True
    except Exception:
        return False

