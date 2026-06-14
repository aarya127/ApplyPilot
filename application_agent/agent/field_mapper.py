from __future__ import annotations

import re
from typing import Any


def map_field(field: dict[str, Any], profile: dict[str, Any]) -> tuple[Any, str] | None:
    text = field_text(field)

    if should_skip(text):
        return None

    policy_like = is_policy_question(text)
    saved = saved_answer(field, profile)
    if has_value(saved) and not policy_like:
        return saved, "saved"

    rules = [
        (r"\bfirst\b.*\bname\b|\bgiven\b.*\bname\b|fname", profile.get("first_name")),
        (r"\blast\b.*\bname\b|\bfamily\b.*\bname\b|lname|surname", profile.get("last_name")),
        (r"\bfull\b.*\bname\b|\blegal name\b", profile.get("full_name") or full_name(profile)),
        (r"email|e-mail", profile.get("email")),
        (r"phone|mobile|cell|telephone", profile.get("phone")),
        (r"linkedin", profile.get("linkedin")),
        (r"github", profile.get("github")),
        (r"portfolio|website|personal site", profile.get("portfolio")),
        (r"school|university|college", profile.get("school")),
        (r"degree|major|program", profile.get("degree")),
        (r"graduation|grad date|expected completion", profile.get("graduation_date")),
        (
            r"(current|previous|most recent).*(employer|company)|(employer|company).*(current|previous|most recent)",
            profile.get("current_or_previous_employer"),
        ),
        (
            r"(current|previous|most recent).*(job title|title|position|role)|(job title|title|position|role).*(current|previous|most recent)",
            profile.get("current_or_previous_job_title"),
        ),
        (r"salary|compensation|pay expectation", profile.get("salary")),
        (r"relocation assistance|need relocation assistance|relocation support", relocation_assistance_answer(profile)),
        (r"relocat", profile.get("relocation")),
        (r"non[- ]?compete|restrictive covenant|subject to.*agreement", profile.get("subject_to_agreement")),
    ]

    for pattern, value in rules:
        if re.search(pattern, text) and has_value(value):
            return value, "rule"

    address_answer = map_address(text, profile)
    if address_answer:
        return address_answer, "rule"

    if re.search(r"sponsor|visa|h-?1b|work permit", text):
        return profile.get("needs_sponsorship") or "No", "rule"

    if re.search(r"authorized|eligible|legally.*work|work authorization", text):
        return profile.get("work_authorization") or "Yes", "rule"

    if re.search(r"ever|previously|formerly", text) and re.search(r"employed|worked", text):
        return previous_company_answer(text, profile), "rule"

    if re.search(r"relatives?|family member|spouse|domestic partner", text) and re.search(r"employed|work|working|relationship", text):
        return profile.get("answers", {}).get("relativesAtCompany", "No"), "rule"

    if re.search(r"authorized dealer|dealer", text):
        return profile.get("answers", {}).get("workedForAuthorizedDealer", "No"), "rule"

    if re.search(r"contractor", text) and re.search(r"work|working|employed", text):
        return profile.get("answers", {}).get("workedAsContractorForCompany", "No"), "rule"

    if re.search(r"whatsapp|sms|text messages?|messaging", text) and re.search(r"recruit|hiring", text):
        return profile.get("answers", {}).get("recruitingMessages", "No"), "rule"

    if re.search(r"veteran|protected veteran|military service", text):
        return profile.get("veteran_status") or "No", "rule"

    if profile.get("auto_fill_sensitive_fields") is True:
        sensitive = map_sensitive(text, profile)
        if sensitive:
            return sensitive, "sensitive"

    if has_value(saved):
        return saved, "saved"

    return None


def field_text(field: dict[str, Any]) -> str:
    return normalize(
        " ".join(
            [
                field.get("label", ""),
                field.get("question_text", ""),
                field.get("name", ""),
                field.get("id", ""),
                field.get("placeholder", ""),
                field.get("aria_label", ""),
                field.get("surrounding_text", ""),
            ]
        )
    )


def map_address(text: str, profile: dict[str, Any]) -> str | None:
    address = profile.get("address") or {}
    if not address:
        return None

    rules = [
        (r"address line 1|street address|street", address.get("line1")),
        (r"address line 2|apt|apartment|suite|unit", address.get("line2")),
        (r"\bcity\b|location city", address.get("city")),
        (r"\bstate\b|\bprovince\b|region", address.get("state") or address.get("province")),
        (r"postal code|postcode|zip code|\bzip\b", address.get("zipCode") or address.get("postalCode")),
        (r"\bcountry\b|currently reside", address.get("country")),
        (r"full address|mailing address|home address", address.get("fullAddress")),
    ]

    for pattern, value in rules:
        if re.search(pattern, text) and has_value(value):
            return value

    if "location" in text and has_value(profile.get("location")):
        return profile["location"]

    return None


def map_sensitive(text: str, profile: dict[str, Any]) -> str | None:
    demographics = profile.get("demographics") or {}
    rules = [
        (r"hispanic|latino|latina|latinx", demographics.get("hispanicLatino")),
        (r"race|racial", demographics.get("race")),
        (r"ethnic|ethnicity", demographics.get("ethnicity")),
        (r"sexual orientation|orientation", demographics.get("sexualOrientation") or profile.get("answers", {}).get("sexualOrientation")),
        (r"gender", demographics.get("gender") or demographics.get("genderIdentity")),
    ]

    for pattern, value in rules:
        if re.search(pattern, text) and has_value(value):
            return value

    return None


def previous_company_answer(text: str, profile: dict[str, Any]) -> str:
    companies = [
        normalize(item.get("company", ""))
        for item in profile.get("work_experience", [])
        if isinstance(item, dict) and item.get("company")
    ]
    return "Yes" if any(company and phrase_in_text(company, text) for company in companies) else "No"


def is_policy_question(text: str) -> bool:
    return bool(
        re.search(
            r"sponsor|visa|work authorization|authorized.*work|previously|formerly|ever.*employed|"
            r"relatives?|family member|contractor|dealer|veteran|military|relocat|sexual orientation|"
            r"hispanic|latino|race|ethnic|gender|disability",
            text,
        )
    )


def relocation_assistance_answer(profile: dict[str, Any]) -> str:
    explicit = profile.get("answers", {}).get("relocationAssistance") or profile.get("relocation") or ""
    return "No" if re.search(r"not interested|^no$|false", normalize(explicit)) else "Yes"


def phrase_in_text(phrase: str, text: str) -> bool:
    return f" {phrase} " in f" {text} "


def saved_answer(field: dict[str, Any], profile: dict[str, Any]) -> str:
    answers = profile.get("answers") or {}
    keys = [answer_key(field), normalize(field.get("label", "")), normalize(field.get("name", ""))]

    for key in keys:
        if has_value(answers.get(key)):
            return answers[key]

    return ""


def answer_key(field: dict[str, Any]) -> str:
    basis = field.get("label") or field.get("name") or field.get("id") or str(field.get("index", ""))
    return f"custom:{normalize(basis).replace(' ', '-')[:80]}"


def full_name(profile: dict[str, Any]) -> str:
    return " ".join(part for part in [profile.get("first_name"), profile.get("last_name")] if part)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def should_skip(text: str) -> bool:
    return any(
        re.search(pattern, text)
        for pattern in [
            r"\bif yes\b",
            r"\bif applicable\b",
            r"please state their name",
            r"please provide.*if yes",
            r"\bcookie",
            r"\btracking",
            r"\badvertis",
            r"\bprivacy preferences\b",
            r"\bprovider linkedin\b",
        ]
    )
