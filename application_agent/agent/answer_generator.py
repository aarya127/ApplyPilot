from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ANSWERS = ROOT / "application_agent/standard_answers.private.json"
PRIVATE_ENV_PATH = ROOT / "autofill_extension/backend/env.private"


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


def answer_question(question: str, profile: dict[str, Any], standard_answers: dict[str, str] | None = None) -> tuple[str, str]:
    standard_answers = standard_answers or {}
    normalized = question.lower().strip()

    for saved_question, saved_answer in standard_answers.items():
        saved_normalized = saved_question.lower().strip()
        if saved_normalized in normalized or normalized in saved_normalized:
            return saved_answer, "saved"

    generated = generate_answer_with_llm(question, profile)
    if generated:
        return generated, "generated_review_required"

    return "", "needs_manual_answer"


def answer_option_question(
    question: str,
    options: list[dict[str, str]],
    profile: dict[str, Any],
    standard_answers: dict[str, str] | None = None,
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

    model = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
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

    try:
        response = requests.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You choose exact dropdown options for truthful job applications."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=True)},
                ],
                "temperature": 0,
                "max_tokens": 512,
            },
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return ""

    return parse_option_answer(strip_reasoning(content))


def generate_answer_with_llm(question: str, profile: dict[str, Any]) -> str:
    load_private_env()
    api_key = os.getenv("NVIDIA_API_KEY", "")
    if not api_key:
        return ""

    model = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
    prompt = (
        "Draft a truthful 2-3 sentence job application answer. "
        "Do not invent experience. Use only the provided profile facts.\n\n"
        f"Question: {question}\n"
        f"Profile: {json.dumps(safe_profile_for_prompt(profile), ensure_ascii=True)}"
    )

    try:
        response = requests.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You help draft concise, truthful job application answers."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 768,
            },
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return ""

    return strip_reasoning(content)


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
