from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from application_agent.agent.answer_generator import answer_question
from application_agent.agent.field_mapper import map_field, normalize
from application_agent.agent.form_scanner import scan_fields


FINAL_SUBMIT_TEXT = [
    "submit application",
    "submit",
    "send application",
    "complete application",
    "apply",
]

NEXT_TEXT = [
    "next",
    "continue",
    "save and continue",
    "review",
]


class BaseAdapter:
    name = "generic"

    def fill_current_page(self, page: Any, profile: dict[str, Any]) -> dict[str, Any]:
        fields = scan_fields(page)
        filled = 0
        generated_review_required = 0
        skipped: list[str] = []
        uploads = self.upload_resume(page, profile)

        for field in fields:
            mapping = map_field(field, profile)
            if not mapping:
                custom_mapping = self.custom_text_answer(field, profile)
                if custom_mapping:
                    mapping = custom_mapping
                else:
                    skipped.append(field.get("label") or field.get("name") or f"field-{field.get('index')}")
                    continue

            value, source = mapping
            if source == "generated_review_required":
                generated_review_required += 1

            if self.fill_field(field, value):
                filled += 1

        return {
            "ats": self.name,
            "scanned": len(fields),
            "filled": filled,
            "generatedReviewRequired": generated_review_required,
            "uploads": uploads,
            "unmapped": skipped[:25],
        }

    def custom_text_answer(self, field: dict[str, Any], profile: dict[str, Any]) -> tuple[str, str] | None:
        if field.get("tag") != "textarea":
            return None

        question = field.get("label") or field.get("placeholder") or field.get("name") or ""
        if not question:
            return None

        answer, source = answer_question(question, profile, profile.get("_standard_answers", {}))
        if not answer:
            return None

        return answer, source

    def fill_field(self, field: dict[str, Any], value: Any) -> bool:
        locator = field["locator"]
        tag = field.get("tag", "")
        field_type = field.get("type", "")

        try:
            if field_type == "file":
                return False

            if tag == "select":
                return self.select_option(locator, value)

            if field_type in {"checkbox", "radio"}:
                return self.click_choice(locator, value)

            locator.fill(str(value), timeout=2_000)
            return True
        except Exception:
            return False

    def select_option(self, locator: Any, value: Any) -> bool:
        desired = normalize(str(value))
        options = locator.evaluate(
            """
            element => Array.from(element.options || []).map(option => ({
              value: option.value,
              label: (option.textContent || '').replace(/\\s+/g, ' ').trim()
            }))
            """
        )

        for option in options:
            if option_matches(option.get("label", ""), option.get("value", ""), desired):
                locator.select_option(option.get("value", ""), timeout=2_000)
                return True

        return False

    def click_choice(self, locator: Any, value: Any) -> bool:
        desired = normalize(str(value))
        if locator.get_attribute("type") == "checkbox":
            should_check = desired in {"true", "yes", "y", "1", "agree"}
            locator.set_checked(should_check, timeout=2_000)
            return True

        locator.check(timeout=2_000)
        return True

    def upload_resume(self, page: Any, profile: dict[str, Any]) -> int:
        resume = profile.get("resume_path", "")
        if not resume or not Path(resume).exists():
            return 0

        file_inputs = page.locator("input[type='file']")
        uploaded = 0

        for index in range(file_inputs.count()):
            item = file_inputs.nth(index)
            try:
                item.set_input_files(resume, timeout=3_000)
                uploaded += 1
            except Exception:
                continue

        return uploaded

    def find_safe_next_button(self, page: Any) -> Any | None:
        buttons = page.locator("button, input[type='button'], input[type='submit'], a")

        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                if not button.is_visible() or not button.is_enabled():
                    continue

                text = button_text(button)
                if is_final_submit_text(text):
                    return None

                if any(label in normalize(text) for label in NEXT_TEXT):
                    return button
            except Exception:
                continue

        return None

    def is_final_submit_page(self, page: Any) -> bool:
        buttons = page.locator("button, input[type='submit']")

        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                if button.is_visible() and is_final_submit_text(button_text(button)):
                    return True
            except Exception:
                continue

        return False


def button_text(button: Any) -> str:
    return (
        button.inner_text(timeout=1_000)
        or button.get_attribute("value")
        or button.get_attribute("aria-label")
        or ""
    )


def is_final_submit_text(text: str) -> bool:
    normalized = normalize(text)
    return any(label == normalized or label in normalized for label in FINAL_SUBMIT_TEXT)


def option_matches(label: str, value: str, desired: str) -> bool:
    label_text = normalize(label)
    value_text = normalize(value)
    aliases = answer_aliases(desired)

    if desired in {label_text, value_text}:
        return True

    if any(alias in {label_text, value_text} for alias in aliases):
        return True

    return any(contains_phrase(label_text, alias) for alias in aliases if len(alias) > 2)


def answer_aliases(desired: str) -> list[str]:
    aliases = {desired}

    if desired == "no":
        aliases.update(
            {
                "no i am not",
                "no i do not",
                "no i have not",
                "i am not a protected veteran",
                "not a protected veteran",
                "not hispanic or latino",
            }
        )

    if desired == "yes":
        aliases.update({"yes i am", "yes i do", "yes i have"})

    if desired == "asian":
        aliases.update({"asian not hispanic or latino"})

    if desired == "male":
        aliases.update({"male", "man"})

    return list(aliases)


def contains_phrase(text: str, phrase: str) -> bool:
    if " " in phrase:
        return phrase in text

    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None
