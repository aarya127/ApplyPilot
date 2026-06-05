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

    mappings = call_nvidia_mapper(fields, profile, page)
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
                        "Use resumeFacts for custom questions. Skip unknown fields."
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

    return [mapping for mapping in mappings if valid_mapping(mapping)]


def build_mapper_prompt(fields: list[dict[str, Any]], profile: dict[str, Any], page: dict[str, Any]) -> str:
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
        "workEligibility": {
            "workAuthorization": profile.get("workAuthorization"),
            "needsSponsorship": profile.get("needsSponsorship"),
            "canadianCitizen": profile.get("canadianCitizen"),
            "usPermanentResident": profile.get("usPermanentResident"),
            "subjectToAgreement": profile.get("subjectToAgreement"),
        },
        "resumeFacts": profile.get("resumeFacts", {}),
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
                "For textarea custom questions, answer in 2-3 concise sentences using only supplied facts."
            ),
            "page": page,
            "profile": minimized_profile,
            "fields": serializable_fields,
        },
        ensure_ascii=False,
    )


def parse_json_object(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")

        if start == -1 or end == -1 or end <= start:
            return {}

        return json.loads(content[start:end + 1])


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
