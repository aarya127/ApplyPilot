from __future__ import annotations

from typing import Any

from application_agent.ats.platform_base import LabelFirstAdapter


class ICIMSAdapter(LabelFirstAdapter):
    name = "icims"

    def platform_label_values(self, profile: dict[str, Any]) -> list[tuple[str, str]]:
        address = profile.get("address") or {}
        return [
            (r"first name|given name", profile.get("first_name", "")),
            (r"last name|family name|surname", profile.get("last_name", "")),
            (r"email|e-mail", profile.get("email", "")),
            (r"phone|mobile|telephone", profile.get("phone", "")),
            (r"linkedin", profile.get("linkedin", "")),
            (r"address line 1|street address", address.get("line1", "")),
            (r"\bcity\b", address.get("city", "")),
            (r"\bstate\b|\bprovince\b", address.get("state") or address.get("province") or ""),
            (r"postal code|zip code|\bzip\b", address.get("zipCode") or address.get("postalCode") or ""),
            (r"\bcountry\b", address.get("country", "")),
        ]
