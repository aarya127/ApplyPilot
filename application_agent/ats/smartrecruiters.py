from __future__ import annotations

from typing import Any

from application_agent.ats.platform_base import LabelFirstAdapter


class SmartRecruitersAdapter(LabelFirstAdapter):
    name = "smartrecruiters"

    def platform_label_values(self, profile: dict[str, Any]) -> list[tuple[str, str]]:
        address = profile.get("address") or {}
        return [
            (r"first name|given name", profile.get("first_name", "")),
            (r"last name|family name|surname", profile.get("last_name", "")),
            (r"email|e-mail", profile.get("email", "")),
            (r"phone|mobile|telephone", profile.get("phone", "")),
            (r"linkedin", profile.get("linkedin", "")),
            (r"website|portfolio", profile.get("portfolio", "")),
            (r"address", address.get("line1", "")),
            (r"\bcity\b|location", profile.get("location") or address.get("city", "")),
            (r"\bcountry\b", address.get("country", "")),
        ]
