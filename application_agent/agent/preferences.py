from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PREFERENCES = ROOT / "application_agent/preferences.private.json"


def load_preferences(path: Path = DEFAULT_PREFERENCES) -> dict[str, Any]:
    if not path.exists():
        return {}

    return json.loads(path.read_text(encoding="utf-8"))


def job_matches_preferences(job: dict[str, Any], preferences: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    title = normalize(job.get("title"))
    company = normalize(job.get("company"))
    location = normalize(job.get("location"))

    if contains_any(company, preferences.get("companyBlacklist", [])):
        reasons.append("company_blacklist")

    if contains_any(title, preferences.get("titleBlacklist", [])):
        reasons.append("title_blacklist")

    positions = [normalize(item) for item in preferences.get("positions", []) if normalize(item)]
    if positions and not any(position in title for position in positions):
        reasons.append("position_not_preferred")

    locations = [normalize(item) for item in preferences.get("locations", []) if normalize(item)]
    if locations and not any(preferred in location for preferred in locations):
        reasons.append("location_not_preferred")

    if preferences.get("remote") is False and "remote" in location:
        reasons.append("remote_excluded")
    if preferences.get("hybrid") is False and "hybrid" in location:
        reasons.append("hybrid_excluded")
    if preferences.get("onsite") is False and location and "remote" not in location and "hybrid" not in location:
        reasons.append("onsite_excluded")

    return not reasons, reasons


def contains_any(value: str, candidates: list[Any]) -> bool:
    return any(normalize(candidate) and normalize(candidate) in value for candidate in candidates)


def normalize(value: Any) -> str:
    return " ".join(str(value or "").lower().split())
