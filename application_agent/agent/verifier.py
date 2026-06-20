from __future__ import annotations

from typing import Any

from application_agent.agent.field_mapper import normalize


def verify_fill_plan(items: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    for item in items:
        field = item.get("field") or {}
        expected = str(item.get("value") or "")
        actual = current_field_value(field)
        status = "matched" if values_match(actual, expected) else "mismatch"

        if actual is None:
            status = "unreadable"

        results.append(
            {
                "index": field.get("index"),
                "label": field.get("label") or field.get("name") or f"field-{field.get('index')}",
                "fieldKind": item.get("fieldKind", ""),
                "expected": expected,
                "actual": actual or "",
                "status": status,
            }
        )

    return {
        "matched": sum(1 for item in results if item["status"] == "matched"),
        "mismatched": [item for item in results if item["status"] == "mismatch"],
        "unreadable": [item for item in results if item["status"] == "unreadable"],
        "results": results,
    }


def current_field_value(field: dict[str, Any]) -> str | None:
    locator = field.get("locator")
    if not locator:
        return None

    try:
        return locator.input_value(timeout=500)
    except Exception:
        try:
            return locator.inner_text(timeout=500)
        except Exception:
            return None


def values_match(actual: str | None, expected: str) -> bool:
    if actual is None:
        return False

    return normalize(actual) == normalize(expected) or normalize(expected) in normalize(actual)
