from __future__ import annotations

from typing import Any

from application_agent.ats.base import BaseAdapter


class LeverAdapter(BaseAdapter):
    name = "lever"

    def fill_current_page(self, page: Any, profile: dict[str, Any]) -> dict[str, Any]:
        label_values = {
            "Full name": profile.get("full_name") or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
            "Email": profile.get("email", ""),
            "Phone": profile.get("phone", ""),
            "LinkedIn": profile.get("linkedin", ""),
        }
        direct_filled = 0

        for label, value in label_values.items():
            if value and fill_by_label(page, label, value):
                direct_filled += 1

        result = super().fill_current_page(page, profile)
        result["filled"] += direct_filled
        result["directFilled"] = direct_filled
        return result


def fill_by_label(page: Any, label: str, value: str) -> bool:
    locator = page.get_by_label(label)
    if locator.count() == 0:
        return False

    try:
        locator.first.fill(value, timeout=2_000)
        return True
    except Exception:
        return False

