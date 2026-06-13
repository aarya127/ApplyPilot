#!/usr/bin/env python3
"""Local backend for LLM field mapping and application tracking."""

from __future__ import annotations

import json
import os
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
        return jsonify({"mappings": [], "warning": f"Mapper request failed: {exc}"}), 200

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
                        "You map job application form fields to candidate answers. "
                        "Return only strict JSON. Do not invent experience. "
                        "Use resumeFacts and resumeTranscript for custom questions. Skip unknown fields."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1600,
        },
        timeout=45,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    data = parse_json_object(content)
    mappings = data.get("mappings", [])

    if not isinstance(mappings, list):
        return []

    valid_mappings = [mapping for mapping in mappings if valid_mapping(mapping)]
    return enforce_option_values(valid_mappings, fields)


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
        "demographics": profile.get("demographics", {}),
        "veteranStatus": profile.get("veteranStatus"),
        "resumeFacts": profile.get("resumeFacts", {}),
        "resumeTranscript": resume_transcript(profile),
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
                "Prefer savedAnswers and explicit profile facts over inference. "
                "Use resumeTranscript to decide whether the candidate has worked at a named company; if the named company is absent from the transcript and savedAnswers do not say otherwise, answer No. "
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


def resume_transcript(profile: dict[str, Any]) -> str:
    facts = profile.get("resumeFacts", {})
    if not isinstance(facts, dict):
        return ""

    sections = []
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

        value = value_from_options(mapping.get("value"), options)
        if value is None:
            continue

        filtered.append({**mapping, "value": value})

    return filtered


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


def option_aliases(value: str) -> set[str]:
    aliases = {value}

    if value == "no":
        aliases.update(
            {
                "no i am not",
                "no i do not",
                "no i don t",
                "no i have not",
                "no i do not have",
                "not a protected veteran",
                "i am not a protected veteran",
                "not hispanic or latino",
                "not hispanic",
                "not latino",
            }
        )

    if value == "yes":
        aliases.update({"yes i am", "yes i do", "yes i have"})

    if value in {"united states", "united states of america", "usa", "u s", "u s a", "us"}:
        aliases.update({"united states", "united states of america", "usa", "u s", "u s a", "us"})

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
