from __future__ import annotations

import re
from typing import Any


IDENTITY_FIRST_NAME = "identity.first_name"
IDENTITY_LAST_NAME = "identity.last_name"
IDENTITY_FULL_NAME = "identity.full_name"
CONTACT_EMAIL = "contact.email"
CONTACT_PHONE = "contact.phone"
LINK_LINKEDIN = "links.linkedin"
LINK_GITHUB = "links.github"
LINK_PORTFOLIO = "links.portfolio"
WORK_CURRENT_EMPLOYER = "work.current_or_previous_employer"
WORK_CURRENT_TITLE = "work.current_or_previous_job_title"
EDUCATION_SCHOOL = "education.school"
EDUCATION_DEGREE = "education.degree"
EDUCATION_FIELD = "education.field_of_study"
ADDRESS_LINE1 = "address.line1"
ADDRESS_LINE2 = "address.line2"
ADDRESS_CITY = "address.city"
ADDRESS_STATE = "address.state"
ADDRESS_POSTAL = "address.postal_code"
ADDRESS_COUNTRY = "address.country"


CORE_PROFILE_KINDS = {
    IDENTITY_FIRST_NAME,
    IDENTITY_LAST_NAME,
    IDENTITY_FULL_NAME,
    CONTACT_EMAIL,
    CONTACT_PHONE,
    LINK_LINKEDIN,
    LINK_GITHUB,
    LINK_PORTFOLIO,
    WORK_CURRENT_EMPLOYER,
    WORK_CURRENT_TITLE,
    EDUCATION_SCHOOL,
    EDUCATION_DEGREE,
    EDUCATION_FIELD,
    ADDRESS_LINE1,
    ADDRESS_LINE2,
    ADDRESS_CITY,
    ADDRESS_STATE,
    ADDRESS_POSTAL,
    ADDRESS_COUNTRY,
}


def classify_field_kind(field: dict[str, Any]) -> str:
    primary = primary_field_text(field)
    full = field_text(field)

    if is_work_or_education_identity_field(primary):
        if re.search(r"employer|company", primary):
            return WORK_CURRENT_EMPLOYER
        if re.search(r"job title|title|position|role", primary):
            return WORK_CURRENT_TITLE
        if re.search(r"school|university|college|education", primary):
            return EDUCATION_SCHOOL

    if re.search(r"\bfirst\b.*\bname\b|\bgiven\b.*\bname\b|fname", primary):
        return IDENTITY_FIRST_NAME
    if re.search(r"\blast\b.*\bname\b|\bfamily\b.*\bname\b|lname|surname", primary):
        return IDENTITY_LAST_NAME
    if re.search(r"\bfull\b.*\bname\b|\blegal name\b|^name$", primary):
        return IDENTITY_FULL_NAME
    if re.search(r"\bemail\b|e-mail", primary) and not re.search(r"linkedin|linked in|github|website|portfolio", primary):
        return CONTACT_EMAIL
    if re.search(r"\bphone\b|mobile|cell|telephone", primary) and not re.search(r"country|code|extension|device|location", primary):
        return CONTACT_PHONE
    if re.search(r"linkedin|linked in", primary):
        return LINK_LINKEDIN
    if re.search(r"github|git hub", primary):
        return LINK_GITHUB
    if re.search(r"portfolio|personal website|personal site|website url|^website$", primary):
        return LINK_PORTFOLIO

    if re.search(r"school|university|college|institution", primary) and not re.search(r"website|url|link", full):
        return EDUCATION_SCHOOL
    if re.search(r"degree|qualification", primary):
        return EDUCATION_DEGREE
    if re.search(r"field of study|discipline|major|program", primary):
        return EDUCATION_FIELD

    if re.search(r"address line 1|address 1|street address|street", primary):
        return ADDRESS_LINE1
    if re.search(r"address line 2|address 2|apt|apartment|suite|unit", primary):
        return ADDRESS_LINE2
    if re.search(r"location city|city location|^city\b|\bcity$", primary):
        return ADDRESS_CITY
    if re.search(r"(what|which).{0,20}u\.?s\.?\s*state|state.{0,60}(currently reside|current residence)|currently reside.{0,60}state|\bstate\b|\bprovince\b|region", primary):
        return ADDRESS_STATE
    if re.search(r"postal code|postcode|zip code|\bzip\b", primary):
        return ADDRESS_POSTAL
    if re.search(r"\bcountry\b|currently reside", primary) and not re.search(r"phone|code", primary):
        return ADDRESS_COUNTRY

    if re.search(r"(current|previous|most recent).*(employer|company)|(employer|company).*(current|previous|most recent)", full):
        return WORK_CURRENT_EMPLOYER
    if re.search(r"(current|previous|most recent).*(job title|title|position|role)|(job title|title|position|role).*(current|previous|most recent)", full):
        return WORK_CURRENT_TITLE

    return ""


def resolve_field_kind(kind: str, profile: dict[str, Any]) -> Any:
    address = profile.get("address") or {}
    answers = profile.get("answers") or {}

    if kind == IDENTITY_FIRST_NAME:
        return profile.get("first_name")
    if kind == IDENTITY_LAST_NAME:
        return profile.get("last_name")
    if kind == IDENTITY_FULL_NAME:
        return profile.get("full_name") or full_name(profile)
    if kind == CONTACT_EMAIL:
        return profile.get("email")
    if kind == CONTACT_PHONE:
        return profile.get("phone")
    if kind == LINK_LINKEDIN:
        return profile.get("linkedin")
    if kind == LINK_GITHUB:
        return profile.get("github")
    if kind == LINK_PORTFOLIO:
        return profile.get("portfolio") or profile.get("website")
    if kind == WORK_CURRENT_EMPLOYER:
        return profile.get("current_or_previous_employer") or answers.get("currentOrPreviousEmployer") or first_experience_value(profile, "company")
    if kind == WORK_CURRENT_TITLE:
        return profile.get("current_or_previous_job_title") or answers.get("currentOrPreviousJobTitle") or first_experience_value(profile, "title")
    if kind == EDUCATION_SCHOOL:
        return profile.get("school") or first_education_value(profile, "school")
    if kind == EDUCATION_DEGREE:
        return profile.get("degree") or first_education_value(profile, "degree")
    if kind == EDUCATION_FIELD:
        return profile.get("field_of_study") or profile.get("fieldOfStudy") or first_education_value(profile, "fieldOfStudy")
    if kind == ADDRESS_LINE1:
        return address.get("line1")
    if kind == ADDRESS_LINE2:
        return address.get("line2")
    if kind == ADDRESS_CITY:
        return address.get("city")
    if kind == ADDRESS_STATE:
        return state_name_or_value(address.get("state") or address.get("province"))
    if kind == ADDRESS_POSTAL:
        return address.get("zipCode") or address.get("postalCode")
    if kind == ADDRESS_COUNTRY:
        return address.get("country")

    return None


def primary_field_text(field: dict[str, Any]) -> str:
    return normalize(
        " ".join(
            [
                field.get("label", ""),
                field.get("question_text", ""),
                field.get("name", ""),
                field.get("id", ""),
                field.get("placeholder", ""),
                field.get("aria_label", ""),
            ]
        )
    )


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


def is_work_or_education_identity_field(text: str) -> bool:
    return bool(
        re.search(r"(current|previous|most recent|last).*(employer|company|school|university|college|education|job title|title|position|role)", text)
        or re.search(r"(employer|company|school|university|college|education|job title|title|position|role).*(current|previous|most recent|last|attended)", text)
        or re.search(r"work experience|employment history|last university attended|current/previous employer", text)
    )


def first_experience_value(profile: dict[str, Any], key: str) -> str:
    for item in profile.get("work_experience", []):
        if isinstance(item, dict) and item.get(key):
            return str(item[key])
    return ""


def first_education_value(profile: dict[str, Any], key: str) -> str:
    for item in profile.get("education", []):
        if isinstance(item, dict) and item.get(key):
            return str(item[key])
    return ""


def state_name_or_value(value: Any) -> str:
    states = {
        "al": "Alabama",
        "ak": "Alaska",
        "az": "Arizona",
        "ar": "Arkansas",
        "ca": "California",
        "co": "Colorado",
        "ct": "Connecticut",
        "de": "Delaware",
        "fl": "Florida",
        "ga": "Georgia",
        "hi": "Hawaii",
        "id": "Idaho",
        "il": "Illinois",
        "in": "Indiana",
        "ia": "Iowa",
        "ks": "Kansas",
        "ky": "Kentucky",
        "la": "Louisiana",
        "me": "Maine",
        "md": "Maryland",
        "ma": "Massachusetts",
        "mi": "Michigan",
        "mn": "Minnesota",
        "ms": "Mississippi",
        "mo": "Missouri",
        "mt": "Montana",
        "ne": "Nebraska",
        "nv": "Nevada",
        "nh": "New Hampshire",
        "nj": "New Jersey",
        "nm": "New Mexico",
        "ny": "New York",
        "nc": "North Carolina",
        "nd": "North Dakota",
        "oh": "Ohio",
        "ok": "Oklahoma",
        "or": "Oregon",
        "pa": "Pennsylvania",
        "ri": "Rhode Island",
        "sc": "South Carolina",
        "sd": "South Dakota",
        "tn": "Tennessee",
        "tx": "Texas",
        "ut": "Utah",
        "vt": "Vermont",
        "va": "Virginia",
        "wa": "Washington",
        "wv": "West Virginia",
        "wi": "Wisconsin",
        "wy": "Wyoming",
        "dc": "District of Columbia",
    }
    text = normalize(str(value or ""))
    return states.get(text, str(value or ""))


def full_name(profile: dict[str, Any]) -> str:
    return " ".join(part for part in [profile.get("first_name"), profile.get("last_name")] if part)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()
