#!/usr/bin/env python3
"""Local backend for LLM field mapping and application tracking."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request


ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIR = ROOT / "generated"
DB_PATH = GENERATED_DIR / "applications.sqlite3"
LLM_TRACE_PATH = GENERATED_DIR / "llm_trace.private.jsonl"
PRIVATE_ENV_PATH = Path(__file__).resolve().parent / "env.private"
NVIDIA_CHAT_COMPLETIONS_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
AI_RATE_LIMIT_PER_MINUTE = 40
KEY_PROBE_TTL_SECONDS = 600
_ai_request_times: deque[float] = deque()
_ai_request_lock = threading.Lock()
_key_probe_lock = threading.Lock()
_key_probe_cache: dict[str, Any] = {"checkedAt": 0.0, "keyValid": None, "keyError": ""}

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


def prune_ai_request_times(now: float) -> None:
    cutoff = now - 60
    while _ai_request_times and _ai_request_times[0] < cutoff:
        _ai_request_times.popleft()


def ai_usage_snapshot(now: float | None = None) -> dict[str, int]:
    current_time = now if now is not None else time.time()
    with _ai_request_lock:
        prune_ai_request_times(current_time)
        requests_last_minute = len(_ai_request_times)

    return {
        "requestsLastMinute": requests_last_minute,
        "limitPerMinute": AI_RATE_LIMIT_PER_MINUTE,
        "remainingThisMinute": max(AI_RATE_LIMIT_PER_MINUTE - requests_last_minute, 0),
    }


def record_ai_request(now: float | None = None) -> dict[str, int]:
    current_time = now if now is not None else time.time()
    with _ai_request_lock:
        prune_ai_request_times(current_time)
        _ai_request_times.append(current_time)
        requests_last_minute = len(_ai_request_times)

    return {
        "requestsLastMinute": requests_last_minute,
        "limitPerMinute": AI_RATE_LIMIT_PER_MINUTE,
        "remainingThisMinute": max(AI_RATE_LIMIT_PER_MINUTE - requests_last_minute, 0),
    }


def cors_origin_allowed(origin: str) -> bool:
    if not origin:
        return False

    if origin.startswith("chrome-extension://"):
        return True

    return bool(re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", origin))


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if cors_origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def nvidia_key_rejected_message(status_code: int) -> str:
    return (
        f"NVIDIA API key was rejected ({status_code}). "
        "Update NVIDIA_API_KEY in backend/env.private and restart the backend."
    )


def note_api_key_rejected(status_code: int) -> None:
    with _key_probe_lock:
        _key_probe_cache.update({
            "checkedAt": time.time(),
            "keyValid": False,
            "keyError": nvidia_key_rejected_message(status_code),
        })


def raise_for_nvidia_status(response, trace_event: str, trace_id: str) -> None:
    if response.status_code in (401, 403):
        message = nvidia_key_rejected_message(response.status_code)
        note_api_key_rejected(response.status_code)
        write_llm_trace(trace_event, {
            "traceId": trace_id,
            "statusCode": response.status_code,
            "error": message,
        })
        raise RuntimeError(message)

    response.raise_for_status()


def probe_api_key_validity() -> dict[str, Any]:
    if not api_key():
        return {
            "keyConfigured": False,
            "keyValid": False,
            "keyError": "NVIDIA_API_KEY is not configured",
        }

    now = time.time()
    with _key_probe_lock:
        if _key_probe_cache["checkedAt"] and now - _key_probe_cache["checkedAt"] < KEY_PROBE_TTL_SECONDS:
            return {
                "keyConfigured": True,
                "keyValid": _key_probe_cache["keyValid"],
                "keyError": _key_probe_cache["keyError"],
            }

    key_valid: bool | None = None
    key_error = ""
    try:
        response = requests.post(
            os.environ.get("NVIDIA_CHAT_COMPLETIONS_URL", NVIDIA_CHAT_COMPLETIONS_URL),
            headers={
                "Authorization": f"Bearer {api_key()}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name(),
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            timeout=15,
        )
        if response.status_code in (401, 403):
            key_valid = False
            key_error = nvidia_key_rejected_message(response.status_code)
        elif response.ok:
            key_valid = True
        else:
            key_error = f"NVIDIA API returned status {response.status_code} during the key check"
    except requests.RequestException as exc:
        key_error = f"Could not reach the NVIDIA API to validate the key: {exc.__class__.__name__}"

    with _key_probe_lock:
        _key_probe_cache.update({"checkedAt": time.time(), "keyValid": key_valid, "keyError": key_error})

    return {"keyConfigured": True, "keyValid": key_valid, "keyError": key_error}


@app.route("/health", methods=["GET"])
def health():
    key_status = probe_api_key_validity()
    return jsonify({
        "ok": True,
        "model": model_name(),
        "llmConfigured": key_status["keyConfigured"],
        "keyConfigured": key_status["keyConfigured"],
        "keyValid": key_status["keyValid"],
        "keyError": key_status["keyError"],
        "aiUsage": ai_usage_snapshot(),
    })


@app.route("/ai-usage", methods=["GET"])
def ai_usage():
    return jsonify({"ok": True, "aiUsage": ai_usage_snapshot()})


@app.route("/map-fields", methods=["POST", "OPTIONS"])
def map_fields():
    if request.method == "OPTIONS":
        return ("", 204)

    payload = request.get_json(force=True) or {}
    fields = payload.get("fields", [])
    profile = payload.get("profile", {})
    page = payload.get("page", {})

    if not api_key():
        return jsonify({
            "mappings": [],
            "warning": "NVIDIA_API_KEY is not configured",
            "aiUsage": ai_usage_snapshot(),
        })

    try:
        mappings = call_nvidia_mapper(fields, profile, page)
    except Exception as exc:
        app.logger.exception("Mapper request failed")
        return jsonify({
            "mappings": policy_mappings(fields, profile),
            "warning": f"Mapper request failed: {exc}",
            "aiUsage": ai_usage_snapshot(),
        }), 200

    return jsonify({"mappings": mappings, "aiUsage": ai_usage_snapshot()})


@app.route("/audit-fields", methods=["POST", "OPTIONS"])
def audit_fields():
    if request.method == "OPTIONS":
        return ("", 204)

    payload = request.get_json(force=True) or {}
    fields = payload.get("fields", [])
    mappings = payload.get("mappings", [])
    profile = payload.get("profile", {})
    page = payload.get("page", {})

    deterministic_report = deterministic_audit_report(fields, mappings, profile)
    corrections = deterministic_report["corrections"]
    decisions = deterministic_report["decisions"]
    issues: list[dict[str, Any]] = []
    warning = None

    if api_key():
        try:
            audit = call_nvidia_auditor(fields, mappings, profile, page)
            corrections = merge_audit_corrections(corrections, audit.get("corrections", []))
            decisions = merge_audit_decisions(decisions, audit.get("decisions", []))
            issues = audit.get("issues", []) if isinstance(audit.get("issues"), list) else []
        except Exception as exc:
            app.logger.exception("Audit request failed")
            warning = f"Audit request failed: {exc}"
    else:
        warning = "NVIDIA_API_KEY is not configured; used deterministic audit only"

    return jsonify({
        "corrections": corrections,
        "decisions": decisions,
        "issues": issues,
        "warning": warning,
        "aiUsage": ai_usage_snapshot(),
    })


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


@app.route("/llm-traces", methods=["GET"])
def llm_traces():
    limit = min(max(int(request.args.get("limit", "20") or 20), 1), 200)
    if not LLM_TRACE_PATH.exists():
        return jsonify({"tracePath": str(LLM_TRACE_PATH), "traces": []})

    lines = LLM_TRACE_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    traces = []
    for line in lines:
        try:
            traces.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return jsonify({"tracePath": str(LLM_TRACE_PATH), "traces": traces})


def profile_display_name(profile: dict[str, Any]) -> str:
    if not isinstance(profile, dict):
        return "the candidate"

    full_name = str(profile.get("fullName") or "").strip()
    if full_name:
        return full_name

    parts = [str(profile.get(key) or "").strip() for key in ("firstName", "lastName")]
    name = " ".join(part for part in parts if part)
    return name or "the candidate"


def call_nvidia_mapper(fields: list[dict[str, Any]], profile: dict[str, Any], page: dict[str, Any]) -> list[dict[str, Any]]:
    prompt = build_mapper_prompt(fields, profile, page)
    display_name = profile_display_name(profile)
    trace_id = new_trace_id()
    request_json = {
        "model": model_name(),
        "messages": [
            {
                "role": "system",
                "content": (
                    f"You are ApplyPilot acting for {display_name} on job application forms. "
                    "Return only strict JSON with no markdown or reasoning. "
                    "For each field, produce the most accurate truthful answer using the supplied profile, "
                    "resume facts, saved answers, default policies, retrieved field context, and visible options. "
                    "Choose answers from supplied dropdown, radio, checkbox, and combobox options exactly. "
                    "For optioned fields, infer the intended meaning from the profile and choose the closest supplied option label verbatim. "
                    "For legal eligibility or authorization to work in the country of employment, choose the positive authorized/eligible option. "
                    "Use the profile, resume facts, saved answers, and default policies. "
                    "For narrative textarea/free-text custom answers, write as the candidate in first person using I/my; "
                    f"never write in third person as {display_name} or he/she/they. "
                    "Do not invent experience. Skip unknown fields."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 3200,
        "response_format": {"type": "json_object"},
    }
    write_llm_trace(
        "mapper.request",
        {
            "traceId": trace_id,
            "page": page,
            "fieldCount": len(fields),
            "fields": fields,
            "request": request_json,
        },
    )

    record_ai_request()
    response = requests.post(
        os.environ.get("NVIDIA_CHAT_COMPLETIONS_URL", NVIDIA_CHAT_COMPLETIONS_URL),
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
        },
        json=request_json,
        timeout=45,
    )
    raise_for_nvidia_status(response, "mapper.error", trace_id)
    content = response.json()["choices"][0]["message"]["content"]
    data = parse_json_object(content)
    mappings = data.get("mappings", [])

    if not isinstance(mappings, list):
        mappings = []

    valid_mappings = [mapping for mapping in mappings if valid_mapping(mapping)]
    enforced_mappings = enforce_option_values(valid_mappings, fields)
    fallback_mappings = policy_mappings(fields, profile)
    merged = merge_backend_mappings(enforced_mappings, fallback_mappings)
    write_llm_trace(
        "mapper.response",
        {
            "traceId": trace_id,
            "statusCode": response.status_code,
            "rawContent": content,
            "parsed": data,
            "enforcedMappings": enforced_mappings,
            "fallbackMappings": fallback_mappings,
            "mergedMappings": merged,
        },
    )
    return merged


def call_nvidia_auditor(
    fields: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    profile: dict[str, Any],
    page: dict[str, Any],
) -> dict[str, Any]:
    prompt = build_audit_prompt(fields, mappings, profile, page)
    display_name = profile_display_name(profile)
    trace_id = new_trace_id()
    request_json = {
        "model": model_name(),
        "messages": [
            {
                "role": "system",
                "content": (
                    f"You are ApplyPilot auditing answers on a job application for {display_name}. "
                    "Return only strict JSON with no markdown. "
                    "Follow the audit protocol exactly: keep correct answers, correct wrong answers, fill safe unanswered required questions, "
                    "and skip anything unsafe with a reason. "
                    "The goal is the most accurate truthful answer for each question, not a generic positive answer. "
                    "Do not overwrite correct answers. "
                    "Only propose a correction when the current answer conflicts with supplied profile facts, "
                    "retrieved context, default policies, or visible options. "
                    "If a field has options, the correction value must be exactly one supplied option label. "
                    "For narrative textarea/free-text answers, write as the candidate in first person using I/my; "
                    f"never write in third person as {display_name} or he/she/they. "
                    "Never change name, email, phone, address, resume, experience, education, or link fields unless "
                    "the retrieved profile facts explicitly show the visible value is wrong."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.05,
        "max_tokens": 3200,
        "response_format": {"type": "json_object"},
    }
    write_llm_trace(
        "auditor.request",
        {
            "traceId": trace_id,
            "page": page,
            "fieldCount": len(fields),
            "mappingCount": len(mappings),
            "fields": fields,
            "mappings": mappings,
            "request": request_json,
        },
    )

    record_ai_request()
    response = requests.post(
        os.environ.get("NVIDIA_CHAT_COMPLETIONS_URL", NVIDIA_CHAT_COMPLETIONS_URL),
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
        },
        json=request_json,
        timeout=45,
    )
    raise_for_nvidia_status(response, "auditor.error", trace_id)
    content = response.json()["choices"][0]["message"]["content"]
    data = parse_json_object(content)
    corrections = data.get("corrections", [])
    decisions = data.get("decisions", [])
    issues = data.get("issues", [])

    if not isinstance(corrections, list):
        corrections = []
    if not isinstance(decisions, list):
        decisions = []
    if not isinstance(issues, list):
        issues = []

    valid_corrections = [mapping for mapping in corrections if valid_mapping(mapping)]
    result = {
        "corrections": enforce_option_values(valid_corrections, fields),
        "decisions": normalize_audit_decisions(decisions, fields),
        "issues": [issue for issue in issues if isinstance(issue, dict)],
    }
    write_llm_trace(
        "auditor.response",
        {
            "traceId": trace_id,
            "statusCode": response.status_code,
            "rawContent": content,
            "parsed": data,
            "result": result,
        },
    )
    return result


def build_mapper_prompt(fields: list[dict[str, Any]], profile: dict[str, Any], page: dict[str, Any]) -> str:
    addresses = profile.get("addresses", {})
    display_name = profile_display_name(profile)
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
            "gpa": profile.get("gpa") or profile.get("answers", {}).get("gpa") or "",
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
    serializable_fields = [serialize_field_for_model(field, profile, page) for field in fields]

    return json.dumps(
        {
            "instructions": (
                "Return JSON in this shape: "
                "{\"mappings\":[{\"index\":0,\"value\":\"answer\",\"confidence\":0.0,\"source\":\"llm\"}]}. "
                "Return the most accurate truthful answer for each question. "
                "When context is insufficient, skip the field instead of guessing. "
                "Use exact option labels when a field has options. "
                "For dropdown, radio, checkbox, and combobox fields, choose only from the supplied options. "
                "If a dropdown, radio, checkbox, or combobox field has no supplied options, skip it unless it is a typed location/state field or phone-country-code field. "
                "If the best semantic answer is not an exact option, choose the closest supplied option label. "
                "Never return profile wording for an optioned field unless it exactly equals one supplied option. "
                "For gender fields, never answer Cisgender man unless that exact option is supplied; choose the closest supplied option such as Male/Man when present, otherwise skip. "
                "For Degree, Discipline, Field of Study, Major, and Qualification dropdowns, never answer with a free-text degree or major; use only a supplied option label, or skip if options are missing. "
                "For disability, demographic, veteran, work authorization, sponsorship, relocation, consent, and yes/no fields, compare the meaning of every supplied option and return the single closest option label exactly. "
                "Prefer explicit profile facts and resume facts over inference. "
                "Use savedAnswers only when they clearly match the same current question; ignore generic or low-information saved answers for policy questions. "
                f"Act as {display_name}; answer eligibility/default-policy questions according to defaultPolicies. "
                "Use resumeTranscript to decide whether the candidate has worked at a named company; if the named company is absent from the transcript and savedAnswers do not say otherwise, answer No. "
                "Use candidateContext as the full compact source of truth for profile facts, work history, education, links, preferences, eligibility, and saved answers. "
                "For relatives, family, spouse, domestic partner, contractors, dealers, affiliates, group/community affiliations, memberships, or company-specific conflict questions, answer No/None of the above by default unless savedAnswers or resumeTranscript explicitly says Yes. "
                "For email subscriptions, newsletters, marketing emails, promotional emails, and job alerts, answer No unless savedAnswers explicitly says Yes. "
                "For Terms and Conditions, Terms of Use, Terms of Service, user agreements, or legal terms acceptance prompts, answer Yes. "
                "For certification questions that ask the candidate to confirm the application is true, correct, or complete, answer Yes. "
                "Do not confuse relocation preference with relocation assistance: being open to relocation does not mean the candidate needs relocation assistance. "
                "If asked whether the candidate is willing to relocate at their own cost when relocation assistance is not offered, answer Yes. "
                "For voluntary demographic, disability, veteran, age, or sexual-orientation fields, use explicit profile facts when present; otherwise choose a decline/prefer-not-to-answer option if available. "
                "For previous employer/company questions, answer No when the saved profile does not show employment at that company. "
                "For textarea/free-text custom questions, answer in 2-3 concise sentences using only supplied facts. "
                "Write custom narrative answers in first person as the candidate using I/my. "
                f"Never write narrative answers in third person as {display_name} or he/she/they. "
                "Skip fields that cannot be answered safely."
            ),
            "page": page,
            "profile": minimized_profile,
            "fields": serializable_fields,
        },
        ensure_ascii=False,
    )


def build_audit_prompt(
    fields: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    profile: dict[str, Any],
    page: dict[str, Any],
) -> str:
    field_by_index = {
        field.get("index"): field
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("index"), int)
    }
    mapped_answers = []

    for mapping in mappings:
        if not isinstance(mapping, dict) or not isinstance(mapping.get("index"), int):
            continue

        field = field_by_index.get(mapping.get("index"), {"index": mapping.get("index")})
        mapped_answers.append(
            {
                "index": mapping.get("index"),
                "currentAnswer": mapping.get("value"),
                "source": mapping.get("source"),
                "confidence": mapping.get("confidence"),
                "field": serialize_field_for_model(field, profile, page),
            }
        )

    return json.dumps(
        {
            "instructions": (
                "Return JSON in this exact shape: "
                "{\"decisions\":[{\"index\":0,\"action\":\"keep|correct|fill|skip\",\"value\":\"answer or empty\","
                "\"confidence\":0.0,\"reason\":\"short reason\",\"evidence\":\"profile|resume|policy|savedAnswer|options|insufficientContext\"}],"
                "\"corrections\":[{\"index\":0,\"value\":\"corrected answer\",\"confidence\":0.0,\"source\":\"audit\",\"reason\":\"short reason\"}],"
                "\"issues\":[{\"index\":0,\"severity\":\"warning\",\"reason\":\"short reason\"}]}. "
                "Audit every currentAnswer against retrievedContext, candidateContext, defaultPolicies, and visible options. "
                "For each answer, decide exactly one action: keep when accurate, correct when wrong, fill when blank and safely answerable, "
                "or skip when unsafe. "
                "The answer must be the most accurate truthful answer for the specific question. "
                "Do not guess, do not choose an optimistic answer, and do not prefer Yes unless profile/policy facts support Yes. "
                "Only include corrections for actions correct or fill. "
                "If options are supplied, decision.value and correction.value must be one exact option label from field.options. "
                "For unanswered required fields, correct/fill only when a safe exact option or precise text answer is supported; otherwise skip and add an issue. "
                "Do not change identity/contact/address/education/experience/link fields unless the supplied profile fact is explicit and the current value is wrong. "
                "Answer work authorization/eligibility questions only from the work authorization facts in defaultPolicies and candidateContext; "
                "if the profile does not state work authorization, skip the field instead of assuming an answer. "
                "Visa sponsorship, employer work-authorization assistance, relocation assistance, relatives at company, "
                "contractor/dealer/affiliate status, military service, veteran protected status, subscriptions, and marketing messages default to No. "
                "Terms/conditions acceptance and certification that the application is true/correct default to Yes. "
                "For textarea/free-text custom answers, write in first person as the candidate using I/my, never third person."
            ),
            "page": page,
            "candidateContext": candidate_context(profile),
            "defaultPolicies": default_answer_policies(profile),
            "answersToAudit": mapped_answers,
        },
        ensure_ascii=False,
    )


def serialize_field_for_model(field: dict[str, Any], profile: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    return {
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
        "required": field.get("required"),
        "options": field.get("options", []),
        "retrievedContext": retrieved_context_for_field(field, profile, page),
    }


def retrieved_context_for_field(field: dict[str, Any], profile: dict[str, Any], page: dict[str, Any] | None = None) -> dict[str, Any]:
    haystack = field_policy_haystack(field)
    category = field_context_category(haystack)
    return prune_large_values(
        {
            "category": category,
            "profileFacts": relevant_profile_facts(category, profile, page or {}),
            "defaultPolicies": relevant_default_policies(category, profile),
            "savedAnswers": relevant_saved_answers(field, profile),
            "resumeFacts": relevant_resume_facts(field, profile),
        },
        max_text=1200,
        max_items=12,
    )


def field_context_category(haystack: str) -> str:
    if is_gpa_question(haystack):
        return "education"
    if has_sponsorship_terms(haystack):
        return "sponsorship"
    if is_work_eligibility_question(haystack):
        return "work_authorization"
    if any(term in haystack for term in ["relocation assistance", "relocation support", "need relocation assistance"]):
        return "relocation_assistance"
    if "relocat" in haystack:
        return "relocation_preference"
    if is_family_or_relationship_conflict_question(haystack):
        return "family_or_relationship"
    if is_company_affiliation_question(haystack) or is_previous_company_question(haystack):
        return "company_affiliation"
    if any(term in haystack for term in ["military", "armed forces", "served", "veteran"]):
        return "military_veteran"
    if any(term in haystack for term in ["gender", "hispanic", "latino", "race", "ethnicity", "sexual orientation", "disability"]):
        return "demographics"
    if any(term in haystack for term in ["certify", "true and correct", "terms", "conditions", "privacy policy", "consent"]):
        return "consent_certification"
    if any(term in haystack for term in ["school", "university", "degree", "field of study", "discipline", "major"]):
        return "education"
    if any(term in haystack for term in ["company", "job title", "employment", "work experience", "role description"]):
        return "work_experience"
    if any(term in haystack for term in ["project", "built", "experience with", "tell us about", "describe"]):
        return "custom_question"
    return "general"


def relevant_profile_facts(category: str, profile: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    base = {
        "fullName": profile.get("fullName"),
        "targetCountry": page.get("targetCountry"),
    }
    if category in {"sponsorship", "work_authorization"}:
        base.update(
            {
                "workAuthorization": profile.get("workAuthorization"),
                "needsSponsorship": profile.get("needsSponsorship"),
                "canadianCitizen": profile.get("canadianCitizen"),
                "usPermanentResident": profile.get("usPermanentResident"),
            }
        )
    if category in {"relocation_assistance", "relocation_preference"}:
        base.update(
            {
                "relocation": profile.get("relocation"),
                "relocationAssistance": profile.get("answers", {}).get("relocationAssistance"),
                "relocateAtOwnCost": profile.get("answers", {}).get("relocateAtOwnCost") or "Yes",
            }
        )
    if category in {"family_or_relationship", "company_affiliation", "military_veteran"}:
        base.update(
            {
                "workExperience": profile.get("workExperience") or profile.get("resumeFacts", {}).get("workExperience"),
                "militaryService": profile.get("militaryService"),
                "veteranStatus": profile.get("veteranStatus"),
            }
        )
    if category == "demographics":
        base.update({"demographics": profile.get("demographics"), "veteranStatus": profile.get("veteranStatus"), "disabilityStatus": profile.get("disabilityStatus")})
    if category in {"education", "work_experience", "custom_question"}:
        base.update(
            {
                "education": profile.get("education"),
                "school": profile.get("school"),
                "degree": profile.get("degree"),
                "gpa": profile.get("gpa") or profile.get("answers", {}).get("gpa") or "",
                "workExperience": profile.get("workExperience") or profile.get("resumeFacts", {}).get("workExperience"),
                "links": profile.get("links"),
                "resumeFacts": profile.get("resumeFacts"),
            }
        )

    return {key: value for key, value in base.items() if value not in (None, "", [], {})}


def relevant_default_policies(category: str, profile: dict[str, Any]) -> dict[str, Any]:
    policies = default_answer_policies(profile)
    keys_by_category = {
        "sponsorship": ["needsSponsorship"],
        "work_authorization": ["usWorkAuthorization", "canadaWorkAuthorization"],
        "relocation_assistance": ["relocation"],
        "relocation_preference": ["relocation"],
        "family_or_relationship": ["relativesAtCompany", "familyAtCompany"],
        "company_affiliation": ["previousCompanyEmployment", "contractorDealerAffiliate"],
        "military_veteran": ["militaryService", "spouseMilitaryService", "veteranStatus"],
        "demographics": ["veteranStatus"],
        "consent_certification": ["acceptTerms", "certifyApplicationTruth", "subscribeEmails", "recruitingMessages"],
    }
    keys = keys_by_category.get(category, list(policies))
    return {key: policies[key] for key in keys if key in policies}


def relevant_saved_answers(field: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    answers = profile.get("answers", {})
    if not isinstance(answers, dict):
        return {}

    field_tokens = context_tokens(field_policy_haystack(field))
    ranked = []
    for key, value in answers.items():
        key_tokens = context_tokens(key)
        if not key_tokens:
            continue
        score = len(field_tokens & key_tokens)
        if score:
            ranked.append((score, str(key), value))

    ranked.sort(reverse=True, key=lambda item: item[0])
    return {key: value for _, key, value in ranked[:6]}


def relevant_resume_facts(field: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    transcript = resume_transcript(profile)
    if not transcript:
        return []

    tokens = context_tokens(field_policy_haystack(field))
    lines = [line.strip() for line in transcript.splitlines() if line.strip()]
    ranked = []
    for line in lines:
        line_tokens = context_tokens(line)
        score = len(tokens & line_tokens)
        if score:
            ranked.append((score, line))

    ranked.sort(reverse=True, key=lambda item: item[0])
    return [line for _, line in ranked[:10]]


def context_tokens(value: Any) -> set[str]:
    stop_words = {
        "the",
        "and",
        "or",
        "for",
        "with",
        "you",
        "your",
        "are",
        "have",
        "has",
        "will",
        "now",
        "future",
        "this",
        "that",
        "from",
        "into",
        "one",
        "select",
        "required",
    }
    return {
        token
        for token in normalize_for_option(value).split()
        if len(token) > 2 and token not in stop_words
    }


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
        "gpa",
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


def stated_work_authorization(profile: dict[str, Any]) -> str:
    answers = profile.get("answers", {}) if isinstance(profile.get("answers"), dict) else {}
    authorization = (
        profile.get("workAuthorization")
        or profile.get("workEligibility")
        or answers.get("workAuthorization")
    )
    return str(authorization).strip() if authorization not in (None, "", [], {}) else ""


def work_authorization_policy(profile: dict[str, Any], country: str, status_key: str) -> str:
    facts = []
    status = profile.get(status_key)
    if status not in (None, ""):
        facts.append(f"{status_key}: {status}")

    authorization = stated_work_authorization(profile)
    if authorization:
        facts.append(f"workAuthorization: {authorization}")

    if not facts:
        return (
            f"Work authorization for {country} is not specified in profile; "
            "do not assert authorization and leave the field for user review."
        )

    return f"Answer work authorization questions for {country} strictly from these profile facts: {'; '.join(facts)}."


def default_answer_policies(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": (
            f"Answer as {profile_display_name(profile)} using only the supplied profile and resume facts. "
            "Use first person for narrative answers."
        ),
        "minimumAge": profile.get("answers", {}).get("meetsMinimumAge", "Yes"),
        "usWorkAuthorization": work_authorization_policy(profile, "United States", "usPermanentResident"),
        "canadaWorkAuthorization": work_authorization_policy(profile, "Canada", "canadianCitizen"),
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
        "relocateAtOwnCost": profile.get("answers", {}).get("relocateAtOwnCost", "Yes"),
        "gpa": profile.get("gpa") or profile.get("answers", {}).get("gpa") or "",
    }


def deterministic_audit_corrections(
    fields: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    return deterministic_audit_report(fields, mappings, profile)["corrections"]


def deterministic_audit_report(
    fields: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    profile: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    field_by_index = {
        field.get("index"): field
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("index"), int)
    }
    policy_by_index = {
        mapping.get("index"): mapping
        for mapping in policy_mappings(fields, profile)
        if isinstance(mapping.get("index"), int)
    }
    corrections: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for mapping in mappings:
        if not isinstance(mapping, dict) or not isinstance(mapping.get("index"), int):
            continue

        index = mapping["index"]
        field = field_by_index.get(index)

        if not field:
            continue

        policy = policy_by_index.get(index) or deterministic_profile_mapping(field, profile)

        current_value = mapping.get("value")

        if not policy:
            decisions.append(
                {
                    "index": index,
                    "action": "skip",
                    "value": current_value,
                    "confidence": 0.5,
                    "source": "deterministic-audit",
                    "reason": "No deterministic profile or policy fact was available for this field.",
                    "evidence": "insufficientContext",
                }
            )
            continue

        policy_value = policy.get("value")
        if values_equivalent_for_field(current_value, policy_value, field):
            decisions.append(
                {
                    "index": index,
                    "action": "keep",
                    "value": current_value,
                    "confidence": 0.9,
                    "source": "deterministic-audit",
                    "reason": "Current answer matches stored profile policy.",
                    "evidence": "policy",
                }
            )
            continue

        reason = "Current answer conflicts with stored profile policy."
        decisions.append(
            {
                "index": index,
                "action": "correct",
                "value": policy_value,
                "confidence": 0.9,
                "source": "deterministic-audit",
                "reason": reason,
                "evidence": policy.get("source") or "policy",
            }
        )
        corrections.append(
            {
                "index": index,
                "value": policy_value,
                "confidence": 0.9,
                "source": "policy-audit",
                "reason": reason,
            }
        )

    return {
        "corrections": enforce_option_values(corrections, fields),
        "decisions": normalize_audit_decisions(decisions, fields),
    }


def normalize_audit_decisions(decisions: list[Any], fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    field_by_index = {
        field.get("index"): field
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("index"), int)
    }
    normalized: list[dict[str, Any]] = []

    for decision in decisions:
        if not isinstance(decision, dict) or not isinstance(decision.get("index"), int):
            continue

        action = str(decision.get("action") or "").strip().lower()
        if action not in {"keep", "correct", "fill", "skip"}:
            continue

        index = decision["index"]
        field = field_by_index.get(index)
        value = decision.get("value")

        if action in {"correct", "fill"}:
            options = normalized_options(field)
            if options:
                option_value = value_from_options(value, options)
                if option_value is None:
                    action = "skip"
                    value = ""
                    reason = "Suggested answer did not match any visible option."
                else:
                    value = option_value
                    reason = str(decision.get("reason") or "").strip()
            else:
                reason = str(decision.get("reason") or "").strip()
        else:
            reason = str(decision.get("reason") or "").strip()

        normalized.append(
            {
                "index": index,
                "action": action,
                "value": value,
                "confidence": safe_float(decision.get("confidence"), 0.0),
                "source": str(decision.get("source") or "audit"),
                "reason": reason or "Audited answer.",
                "evidence": str(decision.get("evidence") or ""),
            }
        )

    return normalized


def deterministic_profile_mapping(field: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
    value = deterministic_profile_answer(field, profile)
    if value is None or str(value).strip() == "":
        return None

    return {
        "index": field["index"],
        "value": value,
        "confidence": 0.9,
        "source": "profile-audit",
    }


def deterministic_profile_answer(field: dict[str, Any], profile: dict[str, Any]) -> str | None:
    haystack = field_policy_haystack(field)
    answers = profile.get("answers", {}) if isinstance(profile.get("answers"), dict) else {}

    if is_linkedin_field(haystack):
        return profile.get("linkedin") or ""

    if is_github_field(haystack):
        return profile.get("github") or ""

    if is_portfolio_field(haystack):
        return profile.get("portfolio") or profile.get("website") or ""

    if is_phone_number_profile_field(haystack):
        return profile.get("phone") or ""

    if is_email_profile_field(haystack):
        return profile.get("email") or ""

    if is_application_location_profile_field(haystack):
        location = (
            answers.get("usaLocation")
            or profile.get("usaLocation")
            or profile.get("usaPreferredLocation")
            or answers.get("canadaLocation")
            or profile.get("canadaLocation")
            or profile.get("applicationLocation")
            or profile.get("location")
            or ""
        )
        return location

    if is_work_authorized_countries_field(haystack):
        return profile.get("answers", {}).get("authorizedCountries") or None

    return None


def is_linkedin_field(haystack: str) -> bool:
    return bool(re.search(r"\blinked\s*in\b|\blinkedin\b", haystack)) and not any(
        term in haystack for term in ["cookie", "consent", "provider"]
    )


def is_github_field(haystack: str) -> bool:
    return "github" in haystack or "git hub" in haystack


def is_portfolio_field(haystack: str) -> bool:
    return any(term in haystack for term in ["portfolio", "personal website", "personal site", "website url"])


def is_phone_number_profile_field(haystack: str) -> bool:
    return any(term in haystack for term in ["phone number", "mobile", "cell", "telephone"]) and not any(
        term in haystack for term in ["phone code", "country code", "extension", "device type"]
    )


def is_email_profile_field(haystack: str) -> bool:
    return bool(re.search(r"\bemail\b|e mail", haystack)) and not is_linkedin_field(haystack)


def is_application_location_profile_field(haystack: str) -> bool:
    return bool(re.search(r"^location\b|location city|city location", haystack)) and not any(
        term in haystack for term in ["phone", "country code", "currently reside", "current residence"]
    )


def is_work_authorized_countries_field(haystack: str) -> bool:
    return bool(
        re.search(r"\b(in\s+)?(what|which|list|specify|identify|provide).{0,50}\b(country|countries)\b.{0,120}\b(legally\s+)?(permitted|authorized|eligible)\b.{0,80}\bwork\b", haystack)
        or re.search(r"\b(country|countries)\b.{0,80}\b(legally\s+)?(permitted|authorized|eligible)\b.{0,80}\bwork\b", haystack)
    )


def values_equivalent_for_field(current_value: Any, desired_value: Any, field: dict[str, Any]) -> bool:
    options = normalized_options(field)
    if options:
        current_option = value_from_options(current_value, options)
        desired_option = value_from_options(desired_value, options)
        return current_option is not None and desired_option is not None and current_option == desired_option

    return normalize_for_option(current_value) == normalize_for_option(desired_value)


def merge_audit_corrections(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_index = {
        mapping.get("index"): mapping
        for mapping in primary
        if isinstance(mapping.get("index"), int)
    }

    for mapping in secondary:
        index = mapping.get("index")
        if isinstance(index, int) and index not in by_index:
            by_index[index] = mapping

    return list(by_index.values())


def merge_audit_decisions(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_index = {
        decision.get("index"): decision
        for decision in primary
        if isinstance(decision.get("index"), int)
    }

    for decision in secondary:
        index = decision.get("index") if isinstance(decision, dict) else None
        if not isinstance(index, int):
            continue

        existing = by_index.get(index)
        if not existing or audit_decision_priority(decision) >= audit_decision_priority(existing):
            by_index[index] = decision

    return list(by_index.values())


def audit_decision_priority(decision: dict[str, Any]) -> int:
    action = str(decision.get("action") or "").strip().lower()
    return {"skip": 0, "keep": 1, "fill": 2, "correct": 3}.get(action, 0)


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

    if is_work_authorized_countries_field(haystack):
        answer = profile.get("answers", {}).get("authorizedCountries")
        if not answer:
            return None
        return best_available_option(answer, options) or answer

    if is_work_eligibility_question(haystack):
        authorization = stated_work_authorization(profile)
        if not authorization:
            return None
        if semantic_yes_no_value(normalize_for_option(authorization)) == "no":
            return best_available_option("No", options) or "No"
        return best_authorization_option(options) or best_available_option(authorization, options) or authorization

    if is_gpa_question(haystack):
        answer = profile.get("gpa") or profile.get("answers", {}).get("gpa")
        if not answer:
            return None
        return best_available_option(answer, options) or answer

    if is_relocation_own_cost_question(haystack):
        answer = profile.get("answers", {}).get("relocateAtOwnCost") or "Yes"
        return best_available_option(answer, options) or answer

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

    if is_debarment_or_program_exclusion_question(haystack):
        answer = profile.get("answers", {}).get("governmentProgramExclusion") or "No"
        return best_available_option(answer, options) or answer

    if is_government_employment_question(haystack):
        answer = profile.get("answers", {}).get("priorGovernmentEmployment") or "No"
        return best_available_option(answer, options) or answer

    if is_professional_discipline_question(haystack):
        answer = profile.get("answers", {}).get("professionalDiscipline") or "No"
        return best_available_option(answer, options) or answer

    demographic_answer = demographic_policy_answer(haystack, field, profile)
    if demographic_answer is not None:
        return demographic_answer

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

    if re.search(r"how did you hear about us|how did you hear about this|how did you hear about.*job|source.*application|application source|where did you hear", haystack):
        answer = profile.get("answers", {}).get("applicationSource") or "LinkedIn"
        return best_available_option(answer, options) or answer

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


def is_gpa_question(haystack: str) -> bool:
    return bool(re.search(r"overall result|grade point average|\bgpa\b|\bcgpa\b|academic average", haystack))


def is_relocation_own_cost_question(haystack: str) -> bool:
    return bool(
        "relocat" in haystack
        and re.search(r"own cost|own expense|without relocation assistance|no relocation assistance|assistance is not offered|assistance not offered|not offered|at your cost", haystack)
        and re.search(r"willing|able|would you|are you|can you", haystack)
    )


def is_debarment_or_program_exclusion_question(haystack: str) -> bool:
    return bool(
        re.search(r"(excluded|exclusion|debarred|debarment|suspended|ineligible).{0,140}(federal|state|health care|healthcare|medicare|medicaid|government|procurement|program)", haystack)
        or re.search(r"(federal|state|health care|healthcare|medicare|medicaid|government|procurement|program).{0,140}(excluded|exclusion|debarred|debarment|suspended|ineligible)", haystack)
    )


def is_government_employment_question(haystack: str) -> bool:
    return bool(
        re.search(r"(employed|employment|worked).{0,120}(federal|state|local government|government entity|civil service|va hospital|military)", haystack)
        or re.search(r"(federal|state|local government|government entity|civil service|va hospital|military).{0,120}(employed|employment|worked)", haystack)
    )


def is_professional_discipline_question(haystack: str) -> bool:
    return bool(
        re.search(r"(disciplinary action|discipline|fines?|citations?|penalties|reprimands?|reprovals?|probation|practice restrictions?|revocation|surrender|suspension).{0,180}(professional license|license|certification|credential)", haystack)
        or re.search(r"(professional license|license|certification|credential).{0,180}(disciplinary action|discipline|fines?|citations?|penalties|reprimands?|reprovals?|probation|practice restrictions?|revocation|surrender|suspension)", haystack)
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
        has_work_authorization_assistance_terms(haystack)
        or
        re.search(r"\b(sponsor|sponsorship|visa|work permit)\b", haystack)
        or re.search(r"\b(h\s*1b|f\s*1|opt|cpt|tn|ead)\b", haystack)
    )


def has_work_authorization_assistance_terms(haystack: str) -> bool:
    return bool(
        re.search(r"\b(require|need|request|want|seek|seeking).{0,80}\b(assistance|help|support).{0,80}\b(work authorization|employment authorization|work permit)\b", haystack)
        or re.search(r"\b(assistance|help|support).{0,80}\b(work authorization|employment authorization|work permit).{0,80}\b(now|future|later)\b", haystack)
    )


def demographic_policy_answer(haystack: str, field: dict[str, Any], profile: dict[str, Any]) -> str | None:
    demographics = profile.get("demographics") or {}
    options = normalized_options(field)

    if any(term in haystack for term in ["hispanic", "latino", "latina", "latinx"]):
        answer = demographics.get("hispanicLatino")
        return option_or_value(answer, options)

    if any(term in haystack for term in ["race", "racial", "ethnic", "ethnicity"]):
        answer = demographics.get("race") or demographics.get("ethnicity")
        return option_or_value(answer, options)

    if "sexual orientation" in haystack or "orientation" in haystack:
        answer = demographics.get("sexualOrientation") or profile.get("answers", {}).get("sexualOrientation")
        return option_or_value(answer, options)

    if "gender identity" in haystack or "gender" in haystack:
        answer = demographics.get("genderIdentity") or demographics.get("gender")
        return option_or_value(answer, options)

    if "disability" in haystack:
        answer = profile.get("answers", {}).get("disabilityStatus") or profile.get("disabilityStatus")
        return option_or_value(answer, options)

    return None


def option_or_value(answer: Any, options: list[dict[str, str]]) -> str | None:
    if answer in (None, "", [], {}):
        return None

    return best_available_option(str(answer), options) or str(answer)


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

        if index not in by_index:
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
    if re.search(r"^(yes|true)\b", value) or re.search(r"^(y|1)$", value):
        return "yes"

    if re.search(r"^(no|false)\b", value) or re.search(r"^(n|0)$", value):
        return "no"

    if re.search(r"\bnot (legally )?(authorized|eligible|permitted|allowed)\b", value):
        return "no"

    if re.search(r"\b(authorized|eligible|permitted|allowed) to work\b", value) or (
        re.search(r"\b(authorized|eligible|permitted|allowed)\b", value)
        and re.search(r"\bwithout (visa )?sponsorship\b", value)
    ):
        return "yes"

    if re.search(r"\b(no|not|never|decline|unable|cannot|won t|would not|do not|don t)\b", value):
        return "no"

    if re.search(r"\b(open|willing|able|can|agree|consent|authorized|eligible)\b", value):
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


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def new_trace_id() -> str:
    return uuid.uuid4().hex


def write_llm_trace(event: str, payload: dict[str, Any]) -> None:
    if os.environ.get("APPLYPILOT_LLM_TRACE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    with LLM_TRACE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")


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
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
