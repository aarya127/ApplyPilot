from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ANSWERS = ROOT / "application_agent/standard_answers.private.json"


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


def generate_answer_with_llm(question: str, profile: dict[str, Any]) -> str:
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
            "max_tokens": 220,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


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

