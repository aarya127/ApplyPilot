from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ANSWERS = ROOT / "application_agent/standard_answers.private.json"
PRIVATE_ENV_PATH = ROOT / "autofill_extension/backend/env.private"
NVIDIA_CHAT_COMPLETIONS_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# Same models and env overrides as autofill_extension/backend/server.py. The free endpoint
# retires models without notice and returns 403/503s under load, so each call walks a chain.
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
DEFAULT_NARRATIVE_MODEL = "z-ai/glm-5.3"
DEFAULT_FALLBACK_MODELS = ["z-ai/glm-5.3", "moonshotai/kimi-k3", "nvidia/nemotron-3-super-120b-a12b"]
DEFAULT_NARRATIVE_FALLBACK_MODELS = ["nvidia/nemotron-3-super-120b-a12b"]
CHAIN_DEADLINE_SECONDS = 90

# The local autofill backend (autofill_extension/backend/server.py) carries the policy rules,
# grounding and retries this module lacks. When it is running the agent asks it first. It is
# reached over HTTP only: importing server.py would run init_db and build the Flask app.
BACKEND_URL_ENV = "APPLYPILOT_BACKEND_URL"
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
BACKEND_CONNECT_TIMEOUT = 1.0
BACKEND_READ_TIMEOUT = 190  # the backend caps one /map-fields request at 180 s
DETERMINISTIC_BACKEND_SOURCES = ("policy", "rule", "saved", "profile", "deterministic")
_backend_status: dict[str, bool] = {}


def load_private_env() -> None:
    if os.getenv("NVIDIA_API_KEY"):
        return

    if not PRIVATE_ENV_PATH.exists():
        return

    for line in PRIVATE_ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_standard_answers(path: Path = DEFAULT_ANSWERS) -> dict[str, str]:
    if not path.exists():
        return {}

    return json.loads(path.read_text(encoding="utf-8"))


def model_chain(primary: str, env_key: str, defaults: list[str]) -> list[str]:
    configured = os.environ.get(env_key)
    fallbacks = [item.strip() for item in configured.split(",")] if configured is not None else defaults
    chain: list[str] = []
    for model in [primary, *fallbacks]:
        if model and model not in chain:
            chain.append(model)
    return chain


def structured_model_chain() -> list[str]:
    primary = os.getenv("NVIDIA_MODEL", "").strip() or DEFAULT_MODEL
    return model_chain(primary, "NVIDIA_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)


def narrative_model_chain() -> list[str]:
    structured = os.getenv("NVIDIA_MODEL", "").strip() or DEFAULT_MODEL
    configured = os.environ.get("NVIDIA_NARRATIVE_MODEL")
    primary = DEFAULT_NARRATIVE_MODEL if configured is None else (configured.strip() or structured)
    if primary == structured:
        return structured_model_chain()
    return model_chain(primary, "NVIDIA_NARRATIVE_FALLBACK_MODELS", DEFAULT_NARRATIVE_FALLBACK_MODELS)


def chat_completion(api_key: str, body: dict[str, Any], models: list[str], timeout: float = 30) -> str:
    """Return the first non-empty reply from the model chain, or "" if every model
    fails. A 401 means the key is bad, so the rest of the chain is skipped."""
    deadline = time.monotonic() + CHAIN_DEADLINE_SECONDS
    for model in models:
        remaining = deadline - time.monotonic()
        if remaining < 5:
            break
        try:
            response = requests.post(
                NVIDIA_CHAT_COMPLETIONS_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                # Reasoning models otherwise spend the token budget on chain-of-thought.
                json={**body, "model": model, "chat_template_kwargs": {"thinking": False}},
                timeout=min(timeout, remaining),
            )
        except requests.RequestException:
            continue
        if response.status_code == 401:
            return ""
        if not 200 <= response.status_code < 300:
            continue
        try:
            content = strip_reasoning(response.json()["choices"][0]["message"].get("content"))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            continue
        if content:
            return content
    return ""


def backend_url() -> str:
    return os.environ.get(BACKEND_URL_ENV, DEFAULT_BACKEND_URL).strip().rstrip("/")


def reset_backend_cache() -> None:
    _backend_status.clear()


def backend_available() -> bool:
    """Probe the backend's /health once per process (1 s connect limit)."""
    url = backend_url()
    if not url:
        return False
    if url not in _backend_status:
        try:
            response = requests.get(f"{url}/health", timeout=BACKEND_CONNECT_TIMEOUT)
            _backend_status[url] = response.status_code == 200
        except requests.RequestException:
            _backend_status[url] = False
    return _backend_status[url]


def backend_answer(
    question: str,
    profile: dict[str, Any],
    *,
    options: list[str] | None = None,
    field: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """One-field /map-fields call. Returns (answer, source), or None when the backend is
    down, errors, or has no answer. Model-written answers keep the review flag."""
    if not question or not backend_available():
        return None

    field = field or {}
    payload_field: dict[str, Any] = {
        "index": 0,
        "label": question,
        "questionText": question,
        "tag": field.get("tag") or ("select" if options else "textarea"),
        "type": field.get("type") or ("select" if options else "textarea"),
        "name": field.get("name") or "",
        "placeholder": field.get("placeholder") or "",
    }
    # No "required" key: the agent's scanner can't tell, and the backend answers fields
    # without the flag (it only skips fields marked optional).
    if options:
        payload_field["options"] = [{"label": option, "value": option} for option in options]
    raw_profile = profile.get("_raw") if isinstance(profile.get("_raw"), dict) else {
        key: value for key, value in profile.items() if not str(key).startswith("_")
    }
    page_url = str(field.get("frame_url") or field.get("page_url") or "")

    try:
        response = requests.post(
            f"{backend_url()}/map-fields",
            json={"fields": [payload_field], "profile": raw_profile, "page": {"url": page_url, "title": ""}},
            timeout=(BACKEND_CONNECT_TIMEOUT, BACKEND_READ_TIMEOUT),
        )
        if response.status_code != 200:
            return None
        mappings = response.json().get("mappings") or []
    except (requests.RequestException, ValueError):
        return None

    mapping = next((item for item in mappings if isinstance(item, dict) and item.get("index") == 0), None)
    value = str((mapping or {}).get("value") or "").strip()
    if not value:
        return None
    source = str(mapping.get("source") or "")
    return value, "backend_policy" if source.startswith(DETERMINISTIC_BACKEND_SOURCES) else "generated_review_required"


def answer_question(
    question: str,
    profile: dict[str, Any],
    standard_answers: dict[str, str] | None = None,
    field: dict[str, Any] | None = None,
) -> tuple[str, str]:
    standard_answers = standard_answers or {}
    normalized = question.lower().strip()

    for saved_question, saved_answer in standard_answers.items():
        saved_normalized = saved_question.lower().strip()
        if saved_normalized in normalized or normalized in saved_normalized:
            return saved_answer, "saved"

    from_backend = backend_answer(question, profile, field=field)
    if from_backend:
        return from_backend

    generated = generate_answer_with_llm(question, profile)
    if generated:
        return generated, "generated_review_required"

    return "", "needs_manual_answer"


def answer_option_question(
    question: str,
    options: list[dict[str, str]],
    profile: dict[str, Any],
    standard_answers: dict[str, str] | None = None,
    field: dict[str, Any] | None = None,
) -> tuple[str, str]:
    labels = [option_label(option) for option in options if option_label(option)]
    if not question or not labels:
        return "", "needs_manual_answer"

    standard_answers = standard_answers or {}
    normalized = question.lower().strip()

    for saved_question, saved_answer in standard_answers.items():
        saved_normalized = saved_question.lower().strip()
        if saved_normalized in normalized or normalized in saved_normalized:
            matched = exact_option(saved_answer, labels)
            if matched:
                return matched, "saved"

    from_backend = backend_answer(question, profile, options=labels, field=field)
    if from_backend and exact_option(from_backend[0], labels):
        return exact_option(from_backend[0], labels), from_backend[1]

    generated = generate_option_answer_with_llm(question, labels, profile)
    matched = exact_option(generated, labels)
    if matched:
        return matched, "generated_review_required"

    return "", "needs_manual_answer"


def generate_option_answer_with_llm(question: str, options: list[str], profile: dict[str, Any]) -> str:
    load_private_env()
    api_key = os.getenv("NVIDIA_API_KEY", "")
    if not api_key:
        return ""

    prompt = {
        "instructions": (
            "Choose exactly one option label for this job application field. "
            "Return JSON only in this shape: {\"answer\":\"exact option label\"}. "
            "The answer must be one of the supplied option labels verbatim. "
            "Use only provided profile facts; do not invent experience."
        ),
        "question": question,
        "options": options,
        "profile": safe_profile_for_prompt(profile),
    }

    content = chat_completion(
        api_key,
        {
            "messages": [
                {"role": "system", "content": "You choose exact dropdown options for truthful job applications."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=True)},
            ],
            "temperature": 0,
            "max_tokens": 512,
        },
        structured_model_chain(),
    )
    return parse_option_answer(content)


def generate_answer_with_llm(question: str, profile: dict[str, Any]) -> str:
    load_private_env()
    api_key = os.getenv("NVIDIA_API_KEY", "")
    if not api_key:
        return ""

    prompt = (
        "Draft a truthful 2-3 sentence job application answer. "
        "Do not invent experience. Use only the provided profile facts.\n\n"
        f"Question: {question}\n"
        f"Profile: {json.dumps(safe_profile_for_prompt(profile), ensure_ascii=True)}"
    )

    return chat_completion(
        api_key,
        {
            "messages": [
                {"role": "system", "content": "You help draft concise, truthful job application answers."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 768,
        },
        narrative_model_chain(),
    )


def strip_reasoning(content: Any) -> str:
    text = re.sub(r"<think>.*?</think>", "", str(content or ""), flags=re.DOTALL)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


def parse_option_answer(content: Any) -> str:
    if not isinstance(content, str):
        return ""

    text = content.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return text.strip().strip('"')
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return ""

    if isinstance(data, dict):
        return str(data.get("answer") or data.get("value") or "").strip()

    if isinstance(data, str):
        return data.strip()

    return ""


def exact_option(answer: str, options: list[str]) -> str:
    normalized = " ".join(str(answer or "").lower().split())
    for option in options:
        if " ".join(option.lower().split()) == normalized:
            return option

    return ""


def option_label(option: dict[str, str]) -> str:
    return str(option.get("label") or option.get("value") or "").strip()


def safe_profile_for_prompt(profile: dict[str, Any]) -> dict[str, Any]:
    raw = profile.get("_raw") or {}
    return {
        "education": raw.get("resumeFacts", {}).get("education", []),
        "experience": raw.get("resumeFacts", {}).get("experience", []),
        "projects": raw.get("resumeFacts", {}).get("projects", []),
        "skills": raw.get("resumeFacts", {}).get("skills", []),
        "degree": profile.get("degree", ""),
        "school": profile.get("school", ""),
    }
