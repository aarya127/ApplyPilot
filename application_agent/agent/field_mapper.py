from __future__ import annotations

import re
from typing import Any

from application_agent.agent.field_kinds import classify_field_kind, resolve_field_kind


def map_field(field: dict[str, Any], profile: dict[str, Any]) -> tuple[Any, str] | None:
    text = field_text(field)
    primary_text = primary_field_text(field)

    if should_skip(text):
        return None

    if re.search(r"subscribe|subscription|email alerts?|job alerts?|marketing emails?|promotional emails?|newsletter|mailing list", text):
        return profile.get("answers", {}).get("subscribeEmails", "No"), "rule"

    if re.search(r"how did you hear about us|how did you hear about this|how did you hear about.*job|source.*application|application source|where did you hear", text):
        return profile.get("answers", {}).get("applicationSource", "LinkedIn"), "rule"

    if re.search(r"terms and conditions|terms of use|terms of service|conditions of use|user agreement|legal terms|accept.*terms|agree.*terms|consent.*terms", text):
        return profile.get("answers", {}).get("acceptTerms", "Yes"), "rule"

    if re.search(r"certify|certifying|certification|true and correct|true.*complete|information.*provided.*true|facts.*true", text):
        return profile.get("answers", {}).get("certifyApplicationTruth", "Yes"), "rule"

    if re.search(r"country.*phone.*code|phone.*country.*code|country code|phone\s+country|country\s+phone", text):
        return profile.get("answers", {}).get("phoneCountryCode") or profile.get("phone_country_code") or "+1", "rule"

    if re.search(r"overall result|grade point average|\bgpa\b|\bcgpa\b|academic average", text):
        gpa = profile.get("gpa") or profile.get("answers", {}).get("gpa")
        if has_value(gpa):
            return gpa, "rule"
        return None

    if is_relocation_own_cost_question(text):
        return profile.get("answers", {}).get("relocateAtOwnCost", "Yes"), "rule"

    field_kind = classify_field_kind(field)
    field_value = resolve_field_kind(field_kind, profile)
    if has_value(field_value):
        return field_value, "field-kind"

    policy_like = is_policy_question(text)
    saved = saved_answer(field, profile)
    if has_value(saved) and not policy_like:
        return saved, "saved"

    primary_rules = [
        (r"\bfirst\b.*\bname\b|\bgiven\b.*\bname\b|fname", profile.get("first_name")),
        (r"\blast\b.*\bname\b|\bfamily\b.*\bname\b|lname|surname", profile.get("last_name")),
        (r"\bfull\b.*\bname\b|\blegal name\b", profile.get("full_name") or full_name(profile)),
        (r"\bemail\b|e-mail", profile.get("email")),
        (r"\bphone\b|mobile|cell|telephone", profile.get("phone")),
        (r"school|university|college", profile.get("school")),
        (r"degree|major|program", profile.get("degree")),
        (r"graduation|grad date|expected completion", profile.get("graduation_date")),
    ]

    for pattern, value in primary_rules:
        if re.search(pattern, primary_text) and has_value(value):
            return value, "rule"

    profile_link = map_profile_link_field(primary_text, profile)
    if profile_link:
        return profile_link, "rule"

    rules = []
    if field.get("tag") != "textarea":
        rules.extend(
            [
                (
                    r"(current|previous|most recent).*(employer|company)|(employer|company).*(current|previous|most recent)",
                    profile.get("current_or_previous_employer"),
                ),
                (
                    r"(current|previous|most recent).*(job title|title|position|role)|(job title|title|position|role).*(current|previous|most recent)",
                    profile.get("current_or_previous_job_title"),
                ),
            ]
        )
    rules += [
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

    if has_sponsorship_terms(text):
        if asks_authorization_without_sponsorship(text):
            return profile.get("work_authorization") or "Yes", "rule"
        return sponsorship_answer(profile.get("needs_sponsorship") or profile.get("answers", {}).get("sponsorship") or "No"), "rule"

    if is_work_eligibility_question(text):
        return profile.get("work_authorization") or "Yes", "rule"

    if (
        re.search(r"\bever\b|\bpreviously\b|\bformerly\b", text)
        and re.search(r"\bemployed\b|\bworked\b", text)
        and is_yes_no_style_question(field, text)
    ):
        return previous_company_answer(text, profile), "rule"

    if re.search(r"relatives?|family member|spouse|domestic partner", text) and re.search(r"employed|work|working|relationship", text):
        return profile.get("answers", {}).get("relativesAtCompany", "No"), "rule"

    if re.search(r"groups?|communities|community|affiliation|affiliated|membership|member of|belong to", text):
        return profile.get("answers", {}).get("groupAffiliations", "No"), "rule"

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


def map_profile_link_field(text: str, profile: dict[str, Any]) -> str | None:
    if is_work_or_education_identity_field(text):
        return None

    if re.search(r"linkedin|linked in", text):
        return profile.get("linkedin")

    if re.search(r"github|git hub", text):
        return profile.get("github")

    if re.search(r"portfolio|website|personal site|\burl\b", text):
        return profile.get("portfolio") or profile.get("website")

    return None


def is_work_or_education_identity_field(text: str) -> bool:
    return bool(
        re.search(r"(current|previous|most recent|last).*(employer|company|school|university|college|education|job title|title|position|role)", text)
        or re.search(r"(employer|company|school|university|college|education|job title|title|position|role).*(current|previous|most recent|last|attended)", text)
        or re.search(r"work experience|employment history|last university attended|current/previous employer", text)
    )


def is_work_eligibility_question(text: str) -> bool:
    return bool(
        re.search(r"(legally\s+)?(authorized|eligible|permitted|allowed).*(work|employment)", text)
        or re.search(r"(work|employment).*(authorized|eligible|authorization|eligibility)", text)
        or "work authorization" in text
        or "proof of authorization" in text
        or "legally eligible" in text
    )


def has_sponsorship_terms(text: str) -> bool:
    return bool(
        has_work_authorization_assistance_terms(text)
        or re.search(r"\b(sponsor|sponsorship|visa|work permit)\b", text)
        or re.search(r"\b(h-?1b|f-?1|opt|cpt|tn|ead)\b", text)
    )


def asks_authorization_without_sponsorship(text: str) -> bool:
    if has_work_authorization_assistance_terms(text):
        return False

    return bool(
        re.search(r"\bwithout\b.{0,60}\b(sponsor|sponsorship|visa|work permit)", text)
        or re.search(r"\bnot\b.{0,20}\b(require|need)\b.{0,40}\b(sponsor|sponsorship|visa|work permit)", text)
    )


def is_yes_no_style_question(field: dict[str, Any], text: str) -> bool:
    labels = " ".join(
        normalize(str(option.get("label") or option.get("value") or ""))
        for option in field.get("options") or []
        if isinstance(option, dict)
    )
    if re.search(r"\byes\b", labels) and re.search(r"\bno\b", labels):
        return True

    return bool(re.match(r"(have|has|were|was|are|do|did|will) you\b", text))


def has_work_authorization_assistance_terms(text: str) -> bool:
    return bool(
        re.search(r"\b(require|need|request|want|seek|seeking).{0,80}\b(assistance|help|support).{0,80}\b(work authorization|employment authorization|work permit)\b", text)
        or re.search(r"\b(assistance|help|support).{0,80}\b(work authorization|employment authorization|work permit).{0,80}\b(now|future|later)\b", text)
    )


def map_address(text: str, profile: dict[str, Any]) -> str | None:
    address = profile.get("address") or {}
    if not address:
        return None

    if re.search(r"(what|which).{0,20}u\.?s\.?\s*state|state.{0,60}(currently reside|current residence)|currently reside.{0,60}state", text):
        state = address.get("state") or address.get("province")
        return state_name_or_value(state) if has_value(state) else None

    rules = [
        (r"address line 1|street address|street", address.get("line1")),
        (r"address line 2|apt|apartment|suite|unit", address.get("line2")),
        (r"\bcity\b|location city", address.get("city")),
        (
            r"state\s*/\s*province|state or province|home state|\bprovince\b|^state\s*\*?$|"
            r"(address|city|zip|postal).{0,80}\bstate\b|\bstate\b.{0,80}(address|city|zip|postal)",
            address.get("state") or address.get("province"),
        ),
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
            r"hispanic|latino|race|ethnic|gender|disability|subscribe|newsletter|email alerts?|"
            r"job alerts?|marketing emails?|terms and conditions|terms of use|terms of service|"
            r"user agreement|legal terms|certify|true and correct|communities|affiliation|membership",
            text,
        )
    )


def relocation_assistance_answer(profile: dict[str, Any]) -> str:
    explicit = normalize(profile.get("answers", {}).get("relocationAssistance") or "")
    leading = re.match(r"(yes|no)\b", explicit)
    if leading:
        return "Yes" if leading.group(1) == "yes" else "No"

    return "Yes" if re.search(r"^(true|1)$|\b(need|require|request|want)\b", explicit) else "No"


def is_relocation_own_cost_question(text: str) -> bool:
    return bool(
        "relocat" in text
        and re.search(r"own cost|own expense|without relocation assistance|no relocation assistance|assistance is not offered|assistance not offered|not offered|at your cost", text)
        and re.search(r"willing|able|would you|are you|can you", text)
    )


def sponsorship_answer(value: Any) -> str:
    text = normalize(str(value or ""))

    if (
        text == "no"
        or re.search(r"(do not|don t|will not|would not|won t|not).*(require|need).*(sponsor|visa|work permit)", text)
        or re.search(r"not require sponsorship|no sponsorship", text)
    ):
        return "No"

    if (
        text == "yes"
        or re.search(r"(require|need).*(sponsor|visa|work permit)", text)
        or re.search(r"h-?1b|f-?1|opt|cpt|tn|ead", text)
    ):
        return "Yes"

    return str(value or "No")


def phrase_in_text(phrase: str, text: str) -> bool:
    phrase = strip_punctuation(phrase)
    text = strip_punctuation(text)
    return bool(phrase) and f" {phrase} " in f" {text} "


def strip_punctuation(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", normalize(value))).strip()


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
