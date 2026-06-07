from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Profile not found at {path}. Create/import autofill_extension/profile.private.json first."
        )

    payload = json.loads(path.read_text(encoding="utf-8"))
    profile = payload.get("candidateProfile") or payload.get("profile") or payload
    settings = payload.get("settings", {})
    normalized = normalize_profile(profile, settings)
    normalized["_raw"] = profile
    normalized["_settings"] = settings
    return normalized


def normalize_profile(profile: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    address = select_address(profile, settings)
    answers = profile.get("answers") or {}
    demographics = profile.get("demographics") or {}

    return {
        "first_name": profile.get("firstName", ""),
        "last_name": profile.get("lastName", ""),
        "full_name": profile.get("fullName", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "linkedin": profile.get("linkedin", ""),
        "github": profile.get("github", ""),
        "portfolio": profile.get("portfolio", ""),
        "school": profile.get("school", ""),
        "degree": profile.get("degree", ""),
        "graduation_date": profile.get("graduationDate", ""),
        "current_or_previous_employer": profile.get("currentOrPreviousEmployer", ""),
        "current_or_previous_job_title": profile.get("currentOrPreviousJobTitle", ""),
        "work_authorization": profile.get("workAuthorization") or answers.get("workAuthorization") or "Yes",
        "needs_sponsorship": profile.get("needsSponsorship") or answers.get("sponsorship") or "No",
        "veteran_status": profile.get("veteranStatus") or answers.get("veteranStatus") or "No",
        "subject_to_agreement": profile.get("subjectToAgreement") or answers.get("subjectToAgreement") or "No",
        "salary": profile.get("salary") or answers.get("salary") or "Negotiable",
        "relocation": profile.get("relocation") or answers.get("relocation") or "",
        "address": address,
        "location": profile.get("location") or format_location(address),
        "resume_path": resume_path(profile),
        "work_experience": profile.get("workExperience") or profile.get("resumeFacts", {}).get("workExperience", []),
        "answers": answers,
        "demographics": demographics,
        "auto_fill_sensitive_fields": settings.get("autoFillSensitiveFields") is True,
    }


def select_address(profile: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    addresses = profile.get("addresses") or {}
    target = settings.get("targetCountry") or "canada"

    if target == "usa":
        return addresses.get("usa") or addresses.get("canada") or {}

    return addresses.get("canada") or addresses.get("usa") or {}


def format_location(address: dict[str, Any]) -> str:
    city = address.get("city", "")
    region = address.get("state") or address.get("province") or ""
    return ", ".join(part for part in [city, region] if part)


def resume_path(profile: dict[str, Any]) -> str:
    filename = profile.get("resumeFileName") or ""
    if not filename:
        return ""

    root = Path(__file__).resolve().parents[2]
    candidate = root / "autofill_extension/resumes" / filename
    return str(candidate) if candidate.exists() else ""
