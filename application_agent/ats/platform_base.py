from __future__ import annotations

import re
from typing import Any

from application_agent.ats.base import BaseAdapter, locator_value_matches


class LabelFirstAdapter(BaseAdapter):
    """Adapter base for ATSes whose accessible labels are better than their DOM shape."""

    def fill_current_page(self, page: Any, profile: dict[str, Any]) -> dict[str, Any]:
        direct_filled = self.fill_platform_labels(page, profile)
        result = super().fill_current_page(page, profile)
        result["filled"] += direct_filled
        result["directFilled"] = direct_filled
        return result

    def platform_label_values(self, profile: dict[str, Any]) -> list[tuple[str, str]]:
        return []

    def fill_platform_labels(self, page: Any, profile: dict[str, Any]) -> int:
        filled = 0

        for pattern, value in self.platform_label_values(profile):
            if not value:
                continue

            filled += self.fill_first_labeled_control(page, pattern, value)

        return filled

    def fill_first_labeled_control(self, page: Any, label_pattern: str, value: str) -> int:
        locator = page.get_by_label(re.compile(label_pattern, re.I))

        for index in range(min(locator.count(), 8)):
            field = locator.nth(index)

            try:
                if not field.is_visible() or not field.is_enabled():
                    continue

                if locator_value_matches(field, value):
                    return 0

                tag = field.evaluate("element => element.tagName.toLowerCase()")
                field_type = (field.get_attribute("type") or field.get_attribute("role") or "").lower()

                if field_type in {"checkbox", "radio", "file"}:
                    continue

                if tag == "select":
                    return 1 if self.select_option(field, value) else 0

                if field_type == "combobox" or field.get_attribute("aria-haspopup") == "listbox":
                    pseudo_field = {
                        "locator": field,
                        "type": "combobox",
                        "tag": tag,
                        "label": label_pattern,
                        "options": [],
                    }
                    return 1 if self.select_dynamic_option(page, pseudo_field, value) else 0

                field.fill(str(value), timeout=2_000)
                return 1
            except Exception:
                continue

        return 0
