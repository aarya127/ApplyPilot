#!/usr/bin/env python3
"""Local backend for LLM field mapping and application tracking."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request


ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIR = ROOT / "generated"
DB_PATH = GENERATED_DIR / "applications.sqlite3"
PRIVATE_ENV_PATH = Path(__file__).resolve().parent / "env.private"
NVIDIA_CHAT_COMPLETIONS_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"

app = Flask(__name__)


def load_private_env() -> None:
    if not PRIVATE_ENV_PATH.exists():
        return

    for line in PRIVATE_ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def init_db() -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT,
                status TEXT,
                filled_count INTEGER DEFAULT 0,
                mapped_count INTEGER DEFAULT 0,
                source TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "model": model_name(), "llmConfigured": bool(api_key())})


@app.route("/map-fields", methods=["POST", "OPTIONS"])
def map_fields():
    if request.method == "OPTIONS":
        return ("", 204)

    payload = request.get_json(force=True) or {}
    fields = payload.get("fields", [])
    profile = payload.get("profile", {})
    page = payload.get("page", {})

    if not api_key():
        return jsonify({"mappings": [], "warning": "NVIDIA_API_KEY is not configured"})

    try:
        mappings = call_nvidia_mapper(fields, profile, page)
    except Exception as exc:
        app.logger.exception("Mapper request failed")
        return jsonify({"mappings": policy_mappings(fields, profile), "warning": f"Mapper request failed: {exc}"}), 200

    return jsonify({"mappings": mappings})


@app.route("/track-application", methods=["POST", "OPTIONS"])
def track_application():
    if request.method == "OPTIONS":
        return ("", 204)

    init_db()
    payload = request.get_json(force=True) or {}
    created_at = payload.get("createdAt") or datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO applications
                (url, title, status, filled_count, mapped_count, source, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("url", ""),
                payload.get("title", ""),
                payload.get("status", "filled"),
                int(payload.get("filledCount") or 0),
                int(payload.get("mappedCount") or 0),
                payload.get("source", "chrome_extension"),
                created_at,
                json.dumps(payload, sort_keys=True),
            ),
        )

    return jsonify({"tracked": True, "id": cursor.lastrowid})


@app.route("/applications", methods=["GET"])
def applications():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, url, title, status, filled_count, mapped_count, source, created_at
            FROM applications
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()

    return jsonify({"applications": [dict(row) for row in rows]})


def call_nvidia_mapper(fields: list[dict[str, Any]], profile: dict[str, Any], page: dict[str, Any]) -> list[dict[str, Any]]:
    prompt = build_mapper_prompt(fields, profile, page)
    response = requests.post(
        os.environ.get("NVIDIA_CHAT_COMPLETIONS_URL", NVIDIA_CHAT_COMPLETIONS_URL),
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": model_name(),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are ApplyPilot acting for Aarya Shah on job application forms. "
                        "Return only strict JSON with no markdown or reasoning. "
                        "Choose answers from supplied dropdown, radio, checkbox, and combobox options exactly. "
                        "For optioned fields, infer the intended meaning from the profile and choose the closest supplied option label verbatim. "
                        "For legal eligibility or authorization to work in the country of employment, choose the positive authorized/eligible option. "
                        "Use the profile, resume facts, saved answers, and default policies. "
                        "Do not invent experience. Skip unknown fields."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 3200,
            "response_format": {"type": "json_object"},
        },
        timeout=45,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    data = parse_json_object(content)
    mappings = data.get("mappings", [])

    if not isinstance(mappings, list):
        mappings = []

    valid_mappings = [mapping for mapping in mappings if valid_mapping(mapping)]
    enforced_mappings = enforce_option_values(valid_mappings, fields)
    fallback_mappings = policy_mappings(fields, profile)
    return merge_backend_mappings(enforced_mappings, fallback_mappings)


def build_mapper_prompt(fields: list[dict[str, Any]], profile: dict[str, Any], page: dict[str, Any]) -> str:
    addresses = profile.get("addresses", {})
    minimized_profile = {
        "contact": {
            "firstName": profile.get("firstName"),
            "lastName": profile.get("lastName"),
            "fullName": profile.get("fullName"),
            "email": profile.get("email"),
            "phone": profile.get("phone"),
            "linkedin": profile.get("linkedin"),
            "github": profile.get("github"),
            "portfolio": profile.get("portfolio"),
        },
        "addresses": {
            "canada": safe_location(addresses.get("canada", {}), ["city", "province", "postalCode", "country"]),
            "usa": safe_location(addresses.get("usa", {}), ["city", "state", "zipCode", "country"]),
        },
        "workEligibility": {
            "workAuthorization": profile.get("workAuthorization"),
            "needsSponsorship": profile.get("needsSponsorship"),
            "canadianCitizen": profile.get("canadianCitizen"),
            "usPermanentResident": profile.get("usPermanentResident"),
            "subjectToAgreement": profile.get("subjectToAgreement"),
        },
        "employment": {
            "currentOrPreviousEmployer": profile.get("currentOrPreviousEmployer"),
            "currentOrPreviousJobTitle": profile.get("currentOrPreviousJobTitle"),
            "workExperience": profile.get("workExperience", []),
        },
        "education": {
            "school": profile.get("school"),
            "degree": profile.get("degree"),
            "graduationDate": profile.get("graduationDate"),
        },
        "preferences": {
            "relocation": profile.get("relocation"),
            "salary": profile.get("salary"),
            "targetCountry": page.get("targetCountry"),
        },
        "defaultPolicies": default_answer_policies(profile),
        "demographics": profile.get("demographics", {}),
        "veteranStatus": profile.get("veteranStatus"),
        "resumeFacts": profile.get("resumeFacts", {}),
        "resumeTranscript": resume_transcript(profile),
        "candidateContext": candidate_context(profile),
        "savedAnswers": profile.get("answers", {}),
    }
    serializable_fields = [
        {
            "index": field.get("index"),
            "tag": field.get("tag"),
            "type": field.get("type"),
            "label": field.get("label"),
            "name": field.get("name"),
            "id": field.get("id"),
            "placeholder": field.get("placeholder"),
            "ariaLabel": field.get("ariaLabel"),
            "questionText": field.get("questionText"),
            "surroundingText": field.get("surroundingText"),
            "nearbyText": field.get("nearbyText"),
            "currentValue": field.get("value"),
            "options": field.get("options", []),
        }
        for field in fields
    ]

    return json.dumps(
        {
            "instructions": (
                "Return JSON in this shape: "
                "{\"mappings\":[{\"index\":0,\"value\":\"answer\",\"confidence\":0.0,\"source\":\"llm\"}]}. "
                "Use exact option labels when a field has options. "
                "For dropdown, radio, checkbox, and combobox fields, choose only from the supplied options. "
                "If the best semantic answer is not an exact option, choose the closest supplied option label. "
                "Never return profile wording for an optioned field unless it exactly equals one supplied option. "
                "For Degree, Discipline, Field of Study, Major, and Qualification dropdowns, never answer with a free-text degree or major; use only a supplied option label, or skip if options are missing. "
                "For disability, demographic, veteran, work authorization, sponsorship, relocation, consent, and yes/no fields, compare the meaning of every supplied option and return the single closest option label exactly. "
                "Prefer explicit profile facts and resume facts over inference. "
                "Use savedAnswers only when they clearly match the same current question; ignore generic or low-information saved answers for policy questions. "
                "Act as Aarya; answer eligibility/default-policy questions according to defaultPolicies. "
                "Use resumeTranscript to decide whether the candidate has worked at a named company; if the named company is absent from the transcript and savedAnswers do not say otherwise, answer No. "
                "Use candidateContext as the full compact source of truth for profile facts, work history, education, links, preferences, eligibility, and saved answers. "
                "For relatives, family, spouse, domestic partner, contractors, dealers, affiliates, group/community affiliations, memberships, or company-specific conflict questions, answer No/None of the above by default unless savedAnswers or resumeTranscript explicitly says Yes. "
                "For email subscriptions, newsletters, marketing emails, promotional emails, and job alerts, answer No unless savedAnswers explicitly says Yes. "
                "For Terms and Conditions, Terms of Use, Terms of Service, user agreements, or legal terms acceptance prompts, answer Yes. "
                "For certification questions that ask the candidate to confirm the application is true, correct, or complete, answer Yes. "
                "Do not confuse relocation preference with relocation assistance: being open to relocation does not mean the candidate needs relocation assistance. "
                "For voluntary demographic, disability, veteran, age, or sexual-orientation fields, use explicit profile facts when present; otherwise choose a decline/prefer-not-to-answer option if available. "
                "For previous employer/company questions, answer No when the saved profile does not show employment at that company. "
                "For textarea custom questions, answer in 2-3 concise sentences using only supplied facts. "
                "Skip fields that cannot be answered safely."
            ),
            "page": page,
            "profile": minimized_profile,
            "fields": serializable_fields,
        },
        ensure_ascii=False,
    )


def safe_location(location: Any, keys: list[str]) -> dict[str, Any]:
    if not isinstance(location, dict):
        return {}

    return {key: location.get(key) for key in keys if location.get(key)}


def candidate_context(profile: dict[str, Any]) -> dict[str, Any]:
    """Small enough for every request, but pruned of local file paths/noisy blobs."""
    allowed_keys = [
        "firstName",
        "lastName",
        "fullName",
        "email",
        "phone",
        "linkedin",
        "github",
        "portfolio",
        "addresses",
        "school",
        "degree",
        "graduationDate",
        "workAuthorization",
        "needsSponsorship",
        "canadianCitizen",
        "usPermanentResident",
        "subjectToAgreement",
        "relocation",
        "salary",
        "veteranStatus",
        "militaryService",
        "demographics",
        "answers",
        "workExperience",
        "education",
        "links",
        "resumeFacts",
    ]
    context = {key: profile.get(key) for key in allowed_keys if profile.get(key) not in (None, "", [], {})}

    facts = context.get("resumeFacts")
    if isinstance(facts, dict):
        context["resumeFacts"] = {
            key: value
            for key, value in facts.items()
            if key not in {"sourceFile", "rawTextFile", "localPath", "resumeFile"}
        }

    return prune_large_values(context, max_text=1600, max_items=30)


def prune_large_values(value: Any, max_text: int, max_items: int) -> Any:
    if isinstance(value, dict):
        return {
            str(key): prune_large_values(item, max_text, max_items)
            for key, item in list(value.items())[:max_items]
        }

    if isinstance(value, list):
        return [prune_large_values(item, max_text, max_items) for item in value[:max_items]]

    if isinstance(value, str):
        return value[:max_text]

    return value


def resume_transcript(profile: dict[str, Any]) -> str:
    facts = profile.get("resumeFacts", {})
    if not isinstance(facts, dict):
        return ""

    sections = []
    work_experience = profile.get("workExperience") or facts.get("workExperience")
    if isinstance(work_experience, list) and work_experience:
        sections.append("Structured Work Experience:\n" + "\n".join(json.dumps(item, ensure_ascii=False) for item in work_experience if item))

    education = profile.get("education") or facts.get("education")
    if isinstance(education, list) and education:
        sections.append("Structured Education:\n" + "\n".join(json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item) for item in education if item))

    for label, key in [
        ("Education", "education"),
        ("Experience", "experience"),
        ("Projects", "projects"),
        ("Skills", "skills"),
        ("Certifications", "certifications"),
    ]:
        values = facts.get(key, [])
        if isinstance(values, list) and values:
            sections.append(f"{label}:\n" + "\n".join(str(item) for item in values if str(item).strip()))

    return "\n\n".join(sections)[:12000]


def default_answer_policies(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": "Answer as Aarya Shah using only the supplied profile and resume facts.",
        "minimumAge": profile.get("answers", {}).get("meetsMinimumAge", "Yes"),
        "usWorkAuthorization": (
            "Aarya is a U.S. permanent resident/green card holder and is authorized to work "
            "in the United States for any employer."
        ),
        "canadaWorkAuthorization": "Aarya is a Canadian citizen and is authorized to work in Canada.",
        "needsSponsorship": profile.get("needsSponsorship") or profile.get("answers", {}).get("sponsorship") or "No",
        "subjectToAgreement": profile.get("subjectToAgreement") or profile.get("answers", {}).get("subjectToAgreement") or "No",
        "relativesAtCompany": profile.get("answers", {}).get("relativesAtCompany", "No"),
        "familyAtCompany": profile.get("answers", {}).get("familyAtCompany", "No"),
        "groupAffiliations": profile.get("answers", {}).get("groupAffiliations", "No"),
        "previousCompanyEmployment": "No unless the named company appears in workExperience or resumeTranscript.",
        "contractorDealerAffiliate": "No unless savedAnswers or resumeTranscript explicitly says otherwise.",
        "militaryService": profile.get("militaryService") or profile.get("answers", {}).get("militaryService") or "No",
        "spouseMilitaryService": profile.get("answers", {}).get("spouseMilitaryService", "No"),
        "veteranStatus": profile.get("veteranStatus") or profile.get("answers", {}).get("veteranStatus") or "No",
        "recruitingMessages": profile.get("answers", {}).get("recruitingMessages", "No"),
        "subscribeEmails": profile.get("answers", {}).get("subscribeEmails", "No"),
        "acceptTerms": profile.get("answers", {}).get("acceptTerms", "Yes"),
        "certifyApplicationTruth": profile.get("answers", {}).get("certifyApplicationTruth", "Yes"),
        "relocation": profile.get("relocation") or profile.get("answers", {}).get("relocation") or "Anywhere",
    }


def policy_mappings(fields: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    mappings = []
    transcript = resume_transcript(profile).lower()
    companies = [
        str(item.get("company", "")).lower()
        for item in (profile.get("workExperience") or profile.get("resumeFacts", {}).get("workExperience") or [])
        if isinstance(item, dict)
    ]

    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("index"), int):
            continue

        haystack = field_policy_haystack(field)
        answer = policy_answer_for_field(haystack, field, profile, transcript, companies)
        if answer is None:
            continue

        mappings.append(
            {
                "index": field["index"],
                "value": answer,
                "confidence": 0.78,
                "source": "policy",
            }
        )

    return enforce_option_values(mappings, fields)


def policy_answer_for_field(
    haystack: str,
    field: dict[str, Any],
    profile: dict[str, Any],
    transcript: str,
    companies: list[str],
) -> str | None:
    options = normalized_options(field)
    policies = default_answer_policies(profile)

    if any(term in haystack for term in ["18 years of age", "at least 18", "proof of age", "minimum age"]):
        return best_available_option("Yes", options) or "Yes"

    if has_sponsorship_terms(haystack):
        return best_available_option(policies["needsSponsorship"], options) or policies["needsSponsorship"]

    if is_work_eligibility_question(haystack):
        return best_authorization_option(options) or best_available_option("Yes", options) or "Yes"

    if any(term in haystack for term in ["relocation assistance", "relocation support", "need relocation assistance"]):
        answer = profile.get("answers", {}).get("relocationAssistance") or "No"
        return best_available_option(answer, options) or answer

    if "relocat" in haystack:
        return best_relocation_option(options, policies["relocation"]) or policies["relocation"]

    if ("spouse" in haystack or "domestic partner" in haystack) and ("military" in haystack or "armed forces" in haystack):
        return best_available_option(policies["spouseMilitaryService"], options) or policies["spouseMilitaryService"]

    if "military" in haystack or "armed forces" in haystack or "served" in haystack:
        return best_available_option(policies["militaryService"], options) or policies["militaryService"]

    if "veteran" in haystack:
        return best_available_option(policies["veteranStatus"], options) or policies["veteranStatus"]

    if is_dependent_no_detail_question(haystack):
        return best_available_option("N/A", options) or "N/A"

    if is_family_or_relationship_conflict_question(haystack):
        return best_available_option("No", options) or "No"

    if any(term in haystack for term in ["group", "community", "communities", "affiliation", "affiliated", "membership", "member of", "belong to"]):
        return best_available_option(policies["groupAffiliations"], options) or best_available_option("None of the above", options) or policies["groupAffiliations"]

    if is_company_affiliation_question(haystack):
        return best_available_option("No", options) or "No"

    if is_previous_company_question(haystack):
        question_companies = named_companies_from_question(haystack)
        worked_there = any(
            company and company_in_question(company, haystack, question_companies)
            for company in companies
        )
        return best_available_option("Yes" if worked_there else "No", options) or ("Yes" if worked_there else "No")

    if any(term in haystack for term in ["whatsapp", "sms", "text message", "messaging"]):
        return best_available_option(policies["recruitingMessages"], options) or policies["recruitingMessages"]

    if any(term in haystack for term in ["subscribe", "subscription", "email alert", "job alert", "marketing email", "promotional email", "newsletter", "mailing list"]):
        return best_available_option(policies["subscribeEmails"], options) or policies["subscribeEmails"]

    if any(term in haystack for term in ["terms and conditions", "terms of use", "terms of service", "conditions of use", "user agreement", "legal terms", "accept terms", "agree terms", "consent terms"]):
        return best_available_option(policies["acceptTerms"], options) or policies["acceptTerms"]

    if any(term in haystack for term in ["certify", "certifying", "certification", "true and correct", "true complete", "information provided true", "facts true"]):
        return best_available_option(policies["certifyApplicationTruth"], options) or policies["certifyApplicationTruth"]

    return None


def is_previous_company_question(haystack: str) -> bool:
    return (
        any(term in haystack for term in ["previously employed", "currently employed", "directly employed", "worked for"])
        or (
            any(term in haystack for term in ["previously", "currently", "directly", "ever"])
            and any(term in haystack for term in ["employed", "worked", "paycheck", "w 2"])
        )
    )


def is_work_eligibility_question(haystack: str) -> bool:
    return bool(
        re.search(r"(legally\s+)?(authorized|eligible|permitted|allowed).*(work|employment)", haystack)
        or re.search(r"(work|employment).*(authorized|eligible|authorization|eligibility)", haystack)
        or "work authorization" in haystack
        or "proof of authorization" in haystack
        or "legally eligible" in haystack
    )


def has_sponsorship_terms(haystack: str) -> bool:
    return bool(
        re.search(r"\b(sponsor|sponsorship|visa|work permit)\b", haystack)
        or re.search(r"\b(h\s*1b|f\s*1|opt|cpt|tn|ead)\b", haystack)
    )


def is_dependent_no_detail_question(haystack: str) -> bool:
    asks_for_details = any(
        term in haystack
        for term in [
            "if yes",
            "if applicable",
            "please enter",
            "please provide",
            "please state",
            "state their name",
            "provide their name",
            "name and department",
            "name department",
            "details",
        ]
    )
    conflict_context = is_family_or_relationship_conflict_question(haystack) or is_company_affiliation_question(haystack)
    return asks_for_details and conflict_context


def is_family_or_relationship_conflict_question(haystack: str) -> bool:
    return any(
        term in haystack
        for term in [
            "relative",
            "relatives",
            "family member",
            "family members",
            "spouse",
            "domestic partner",
            "close personal relationship",
            "significant other",
            "parent",
            "sibling",
            "child",
        ]
    )


def is_company_affiliation_question(haystack: str) -> bool:
    return any(
        term in haystack
        for term in [
            "authorized dealer",
            "dealer",
            "contractor",
            "affiliate",
            "subsidiary",
            "business unit",
            "vendor",
            "supplier",
            "partner company",
        ]
    )


def field_policy_haystack(field: dict[str, Any]) -> str:
    label = str(field.get("label") or "")
    pieces = [
        label,
        str(field.get("name") or ""),
        str(field.get("id") or ""),
        str(field.get("placeholder") or ""),
        str(field.get("ariaLabel") or ""),
    ]

    if is_low_information_label(label):
        pieces.append(str(field.get("questionText") or ""))
        pieces.append(str(field.get("surroundingText") or ""))
        pieces.append(str(field.get("nearbyText") or ""))

    return normalize_for_option(" ".join(pieces))


def is_low_information_label(value: Any) -> bool:
    return normalize_for_option(value) in {
        "yes",
        "required yes",
        "yes required",
        "no",
        "required no",
        "no required",
        "yes no",
        "no yes",
        "select one",
        "required select one",
        "select one required",
        "required",
        "true false",
        "false true",
    }


def named_companies_from_question(haystack: str) -> list[str]:
    stop_words = {
        "have",
        "previously",
        "currently",
        "directly",
        "employed",
        "worked",
        "work",
        "with",
        "for",
        "or",
        "and",
        "the",
        "company",
        "subsidiaries",
        "affiliates",
        "received",
        "paycheck",
        "w",
    }
    return [
        token
        for token in haystack.split()
        if len(token) > 3 and token not in stop_words
    ]


def company_in_question(company: str, haystack: str, question_companies: list[str]) -> bool:
    if not company or len(company) < 3:
        return False

    if exact_phrase_in_text(company, haystack):
        return True

    company_tokens = [token for token in company.split() if len(token) > 2]
    if not company_tokens:
        return False

    question_token_set = set(question_companies)
    return all(token in question_token_set for token in company_tokens)


def exact_phrase_in_text(phrase: str, text: str) -> bool:
    return f" {phrase} " in f" {text} "


def best_available_option(answer: str, options: list[dict[str, str]]) -> str | None:
    if not options:
        return None

    return match_option_value(answer, options)


def best_authorization_option(options: list[dict[str, str]]) -> str | None:
    for option in options:
        label = option.get("label") or option.get("value") or ""
        text = normalize_for_option(label)
        if (
            "authorized" in text
            and "work" in text
            and ("united states" in text or "u s" in text or "us" in text)
            and ("any employer" in text or "for any" in text or "without sponsorship" in text)
        ):
            return label

    for option in options:
        label = option.get("label") or option.get("value") or ""
        text = normalize_for_option(label)
        if "authorized" in text and "work" in text and "not authorized" not in text and "require sponsorship" not in text:
            return label

    return None


def best_relocation_option(options: list[dict[str, str]], preferred: str) -> str | None:
    for answer in [preferred, "Anywhere", "Nationwide", "Open to relocation", "Yes"]:
        match = match_option_value(answer, options)
        if match:
            return match

    return None


def merge_backend_mappings(primary: list[dict[str, Any]], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_index = {
        mapping.get("index"): mapping
        for mapping in primary
        if isinstance(mapping.get("index"), int)
    }

    for mapping in fallback:
        index = mapping.get("index")
        if not isinstance(index, int):
            continue

        if index not in by_index or mapping.get("source") == "policy":
            by_index[index] = mapping

    return list(by_index.values())


def enforce_option_values(mappings: list[dict[str, Any]], fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    field_by_index = {
        field.get("index"): field
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("index"), int)
    }
    filtered: list[dict[str, Any]] = []

    for mapping in mappings:
        field = field_by_index.get(mapping.get("index"))
        options = normalized_options(field)

        if not options:
            filtered.append(mapping)
            continue

        value = value_from_options(normalize_mapping_value_for_field(mapping.get("value"), field), options)
        if value is None:
            continue

        filtered.append({**mapping, "value": value})

    return filtered


def normalize_mapping_value_for_field(value: Any, field: dict[str, Any] | None) -> Any:
    if not isinstance(field, dict):
        return value

    haystack = field_policy_haystack(field)
    text = normalize_for_option(value)

    if any(term in haystack for term in ["relocation assistance", "relocation support", "need relocation assistance"]):
        if "relocation assistance" not in text and re.search(r"\b(open|willing|able|can|anywhere|nationwide|relocat)", text):
            return "No"

    return value


def normalized_options(field: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(field, dict) or not isinstance(field.get("options"), list):
        return []

    options = []
    for option in field.get("options", []):
        if not isinstance(option, dict):
            continue

        label = str(option.get("label") or "").strip()
        value = str(option.get("value") or "").strip()
        if label or value:
            options.append({"label": label, "value": value})

    return options


def value_from_options(value: Any, options: list[dict[str, str]]) -> str | list[str] | None:
    if isinstance(value, list):
        selected = [match_option_value(item, options) for item in value]
        selected = [item for item in selected if item is not None]
        return selected or None

    return match_option_value(value, options)


def match_option_value(value: Any, options: list[dict[str, str]]) -> str | None:
    desired = normalize_for_option(value)
    if not desired:
        return None

    constrained_value = yes_no_constrained_value(desired, options)
    if constrained_value is not None:
        return constrained_value

    for option in options:
        label = option.get("label", "")
        option_value = option.get("value", "")
        normalized_label = normalize_for_option(label)
        normalized_value = normalize_for_option(option_value)
        aliases = option_aliases(desired)

        if desired in {normalized_label, normalized_value}:
            return label or option_value

        if any(alias in {normalized_label, normalized_value} for alias in aliases):
            return label or option_value

        if is_united_states_desired(desired):
            if is_united_states_option(normalized_label) or is_united_states_option(normalized_value):
                return label or option_value
            continue

        if any(alias and alias in normalized_label for alias in aliases if len(alias) > 3):
            return label or option_value

    return None


def yes_no_constrained_value(value: str, options: list[dict[str, str]]) -> str | None:
    semantic = semantic_yes_no_value(value)
    if semantic is None:
        return None

    yes_option = None
    no_option = None

    for option in options:
        label = option.get("label", "")
        option_value = option.get("value", "")
        text = normalize_for_option(f"{label} {option_value}")
        if not text or re.search(r"select one|choose|please select", text):
            continue

        if match_simple_yes_no_option(label, option_value, "yes"):
            yes_option = label or option_value
        elif match_simple_yes_no_option(label, option_value, "no"):
            no_option = label or option_value

    if not yes_option or not no_option:
        return None

    return yes_option if semantic == "yes" else no_option


def match_simple_yes_no_option(label: str, value: str, desired: str) -> bool:
    normalized_label = normalize_for_option(label)
    normalized_value = normalize_for_option(value)
    aliases = option_aliases(desired)
    return desired in {normalized_label, normalized_value} or any(
        alias in {normalized_label, normalized_value}
        for alias in aliases
    )


def semantic_yes_no_value(value: str) -> str | None:
    if re.search(r"^(no|false|n|0)$", value) or re.search(r"\b(no|not|never|decline|unable|cannot|won t|would not|do not|don t)\b", value):
        return "no"

    if re.search(r"^(yes|true|y|1)$", value) or re.search(r"\b(open|willing|able|can|agree|consent|authorized|eligible)\b", value):
        return "yes"

    return None


def option_aliases(value: str) -> set[str]:
    aliases = {value}

    if value == "no":
        aliases.update(
            {
                "no i am not",
                "no i do not",
                "no i don t",
                "no i have not",
                "no i have never",
                "no i do not have",
                "no i don t have",
                "no i have never served",
                "i have never served",
                "have never served",
                "have not served",
                "never served",
                "not served",
                "not a protected veteran",
                "i am not a protected veteran",
                "not protected veteran",
                "not a veteran",
                "not hispanic or latino",
                "not hispanic",
                "not latino",
                "none",
                "none of the above",
                "no affiliation",
                "no affiliations",
                "not affiliated",
                "not a member",
            }
        )

    if value == "yes":
        aliases.update({"yes i am", "yes i do", "yes i have"})

    if "authorized" in value and "work" in value:
        aliases.update(
            {
                "i am authorized to work in the united states for any employer",
                "authorized to work in the united states for any employer",
                "legally authorized to work in the united states",
                "authorized to work for any employer",
                "authorized to work",
            }
        )

    if (
        "do not require sponsorship" in value
        or "not require sponsorship" in value
        or "no sponsorship" in value
    ):
        aliases.update(
            {
                "no",
                "i do not require sponsorship",
                "do not require sponsorship",
                "i will not require sponsorship",
                "will not require sponsorship",
                "no sponsorship",
            }
        )

    if value in {"united states", "united states of america", "usa", "u s", "u s a", "us"}:
        aliases.update({"united states", "united states of america", "usa", "u s", "u s a", "us"})

    if value in {"canada 1", "canada +1", "+1", "1"}:
        aliases.update({"canada", "canada 1", "canada +1", "canada plus 1", "+1", "1 canada"})

    if value in {"heterosexual", "heterosexual straight", "straight"}:
        aliases.update({"heterosexual", "heterosexual straight", "heterosexual / straight", "straight"})

    aliases.update(us_state_aliases(value))

    if value == "asian":
        aliases.update({"asian not hispanic or latino", "asian not hispanic"})

    if value == "male":
        aliases.update({"man", "male"})

    return aliases


def us_state_aliases(value: str) -> set[str]:
    states = {
        "al": "alabama",
        "ak": "alaska",
        "az": "arizona",
        "ar": "arkansas",
        "ca": "california",
        "co": "colorado",
        "ct": "connecticut",
        "de": "delaware",
        "fl": "florida",
        "ga": "georgia",
        "hi": "hawaii",
        "id": "idaho",
        "il": "illinois",
        "in": "indiana",
        "ia": "iowa",
        "ks": "kansas",
        "ky": "kentucky",
        "la": "louisiana",
        "me": "maine",
        "md": "maryland",
        "ma": "massachusetts",
        "mi": "michigan",
        "mn": "minnesota",
        "ms": "mississippi",
        "mo": "missouri",
        "mt": "montana",
        "ne": "nebraska",
        "nv": "nevada",
        "nh": "new hampshire",
        "nj": "new jersey",
        "nm": "new mexico",
        "ny": "new york",
        "nc": "north carolina",
        "nd": "north dakota",
        "oh": "ohio",
        "ok": "oklahoma",
        "or": "oregon",
        "pa": "pennsylvania",
        "ri": "rhode island",
        "sc": "south carolina",
        "sd": "south dakota",
        "tn": "tennessee",
        "tx": "texas",
        "ut": "utah",
        "vt": "vermont",
        "va": "virginia",
        "wa": "washington",
        "wv": "west virginia",
        "wi": "wisconsin",
        "wy": "wyoming",
        "dc": "district of columbia",
    }

    if value in states:
        return {states[value]}

    for abbreviation, name in states.items():
        if value == name:
            return {abbreviation}

    return set()


def is_united_states_desired(value: str) -> bool:
    return value in {"united states", "united states of america", "usa", "u s", "u s a", "us"}


def is_united_states_option(value: str) -> bool:
    return value in {"united states", "united states of america", "usa", "u s", "u s a", "us"}


def normalize_for_option(value: Any) -> str:
    return " ".join(
        "".join(char.lower() if char.isalnum() else " " for char in str(value or "")).split()
    )


def parse_json_object(content: str) -> dict[str, Any]:
    if isinstance(content, dict):
        return content

    if isinstance(content, list):
        return {"mappings": content}

    if not isinstance(content, str) or not content.strip():
        return {}

    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        start = content.find("{")
        end = content.rfind("}")

        if start == -1 or end == -1 or end <= start:
            return {}

        try:
            return json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            return {}


def valid_mapping(mapping: Any) -> bool:
    return (
        isinstance(mapping, dict)
        and isinstance(mapping.get("index"), int)
        and mapping.get("value") is not None
        and str(mapping.get("value")).strip() != ""
    )


def api_key() -> str:
    return os.environ.get("NVIDIA_API_KEY", "").strip()


def model_name() -> str:
    return os.environ.get("NVIDIA_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


load_private_env()
init_db()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), debug=True)
