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
        filled = 0
        generated_review_required = 0
        skipped: list[str] = []
        uploads = self.upload_resume(page, profile)
        employment_filled = self.fill_employment_history(page, profile)
        fields = scan_fields(page)

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

            value = value_allowed_by_field_options(field, value)
            if value is None:
                skipped.append(field.get("label") or field.get("name") or f"field-{field.get('index')}")
                continue

            if self.fill_field(field, value):
                filled += 1

        return {
            "ats": self.name,
            "scanned": len(fields),
            "filled": filled,
            "employmentFilled": employment_filled,
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

            if field.get("options") or field_type == "combobox":
                if locator_value_matches(locator, value):
                    return False

                return self.select_dynamic_option(field, value)

            if is_typeahead_field(field):
                if locator_value_matches(locator, value):
                    return False

                if self.select_dynamic_option(field, value):
                    return True

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

    def select_dynamic_option(self, field: dict[str, Any], value: Any) -> bool:
        locator = field["locator"]
        desired = str(value)

        try:
            locator.click(timeout=2_000)
        except Exception:
            return False

        if self.click_visible_option(locator, desired):
            return True

        search_value = dropdown_search_value(field, desired)

        try:
            locator.fill(search_value, timeout=2_000)
        except Exception:
            try:
                locator.press((search_value or desired or "ArrowDown")[:1], timeout=1_000)
            except Exception:
                return False

        try:
            locator.page.wait_for_timeout(350)
        except Exception:
            pass

        return self.click_visible_option(locator, desired)

    def click_visible_option(self, locator: Any, desired: Any) -> bool:
        try:
            page = locator.page
        except Exception:
            return False

        options = page.locator("[role='option'], [data-option], .select2-results__option, [role='menuitemradio'], [data-automation-id='promptOption']")
        desired_text = str(desired)

        for index in range(options.count()):
            option = options.nth(index)
            try:
                if not option.is_visible():
                    continue

                label = option.inner_text(timeout=1_000)
                value = option.get_attribute("data-value") or option.get_attribute("value") or option.get_attribute("aria-label") or ""
                if option_matches(label, value, desired_text):
                    option.click(timeout=2_000)
                    return True
            except Exception:
                continue

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

    def fill_employment_history(self, page: Any, profile: dict[str, Any]) -> int:
        experiences = profile.get("work_experience") or []
        experiences = [item for item in experiences if isinstance(item, dict)]

        if not experiences:
            return 0

        self.ensure_employment_rows(page, len(experiences))
        filled = 0

        for index, experience in enumerate(experiences):
            filled += fill_nth_labeled_control(page, r"company name|employer", index, experience.get("company", ""))
            filled += fill_nth_labeled_control(page, r"^title$|job title|position", index, experience.get("title", ""))
            filled += fill_nth_labeled_control(page, r"start date month|start month", index, experience.get("startMonth", ""))
            filled += fill_nth_labeled_control(page, r"start date year|start year", index, experience.get("startYear", ""))

            if experience.get("currentRole") is True:
                filled += set_nth_checkbox(page, r"current role|currently work|current position", index, True)
            else:
                filled += fill_nth_labeled_control(page, r"end date month|end month", index, experience.get("endMonth", ""))
                filled += fill_nth_labeled_control(page, r"end date year|end year", index, experience.get("endYear", ""))

        return filled

    def ensure_employment_rows(self, page: Any, target_count: int) -> None:
        current_count = max(
            page.get_by_label(re.compile(r"company name|employer", re.I)).count(),
            page.get_by_label(re.compile(r"^title$|job title|position", re.I)).count(),
        )
        clicks_needed = max(target_count - max(current_count, 1), 0)

        for _ in range(min(clicks_needed, 12)):
            button = page.locator("button:has-text('Add another'), a:has-text('Add another')")
            if button.count() == 0:
                return

            try:
                button.last.click(timeout=2_000)
                page.wait_for_timeout(300)
            except Exception:
                return

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
    desired = normalize(desired)
    label_text = normalize(label)
    value_text = normalize(value)
    aliases = answer_aliases(desired)

    if desired in {label_text, value_text}:
        return True

    if any(alias in {label_text, value_text} for alias in aliases):
        return True

    return any(contains_phrase(label_text, alias) for alias in aliases if len(alias) > 2)


def locator_value_matches(locator: Any, desired: Any) -> bool:
    try:
        current = locator.input_value(timeout=500)
    except Exception:
        try:
            current = locator.inner_text(timeout=500)
        except Exception:
            current = ""

    return option_matches(current, "", str(desired))


def is_typeahead_field(field: dict[str, Any]) -> bool:
    text = normalize(
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
    return bool(re.search(r"location|city|country|state|province|phone.*code|country.*phone|select one", text))


def dropdown_search_value(field: dict[str, Any], desired: str) -> str:
    return str(desired or "").strip()


def value_allowed_by_field_options(field: dict[str, Any], value: Any) -> Any | None:
    options = [
        option
        for option in field.get("options", [])
        if str(option.get("label") or option.get("value") or "").strip()
    ]

    if not options:
        return value

    desired = normalize(str(value))
    for option in options:
        label = option.get("label", "")
        option_value = option.get("value", "")
        if option_matches(label, option_value, desired):
            return label or option_value

    return None


def answer_aliases(desired: str) -> list[str]:
    aliases = {desired}

    if desired == "no":
        aliases.update(
            {
                "no i am not",
                "no i do not",
                "no i have not",
                "i do not require sponsorship",
                "do not require sponsorship",
                "will not require sponsorship",
                "no sponsorship",
                "i am not a protected veteran",
                "not a protected veteran",
                "not hispanic or latino",
            }
        )

    if "do not require sponsorship" in desired or "not require sponsorship" in desired or "no sponsorship" in desired:
        aliases.update(
            {
                "no",
                "no i do not",
                "i do not require sponsorship",
                "do not require sponsorship",
                "will not require sponsorship",
                "no sponsorship",
            }
        )

    if desired == "yes":
        aliases.update({"yes i am", "yes i do", "yes i have"})

    if desired == "asian":
        aliases.update({"asian not hispanic or latino"})

    if desired == "male":
        aliases.update({"male", "man"})

    if desired in {"heterosexual", "heterosexual straight", "straight"}:
        aliases.update({"heterosexual", "heterosexual straight", "heterosexual / straight", "straight"})

    if desired in {"canada 1", "canada +1", "+1", "1"}:
        aliases.update({"canada", "canada 1", "canada +1", "canada plus 1", "+1", "1 canada"})

    return list(aliases)


def contains_phrase(text: str, phrase: str) -> bool:
    if " " in phrase:
        return phrase in text

    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def fill_nth_labeled_control(page: Any, label_pattern: str, index: int, value: Any) -> int:
    if value is None or str(value).strip() == "":
        return 0

    locator = page.get_by_label(re.compile(label_pattern, re.I))
    if locator.count() <= index:
        return 0

    field = locator.nth(index)
    text = str(value).strip()

    try:
        tag_name = field.evaluate("element => element.tagName.toLowerCase()")
        if tag_name == "select":
            return 1 if select_by_label_or_value(field, text) else 0

        field.fill(text, timeout=2_000)
        return 1
    except Exception:
        return 0


def select_by_label_or_value(locator: Any, value: str) -> bool:
    desired = normalize(value)
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


def set_nth_checkbox(page: Any, label_pattern: str, index: int, checked: bool) -> int:
    locator = page.get_by_label(re.compile(label_pattern, re.I))
    if locator.count() <= index:
        return 0

    try:
        locator.nth(index).set_checked(checked, timeout=2_000)
        return 1
    except Exception:
        return 0
