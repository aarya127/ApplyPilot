from __future__ import annotations

from typing import Any, Callable

from application_agent.agent.field_kinds import classify_field_kind
from application_agent.agent.field_mapper import map_field


MappingFn = Callable[[dict[str, Any], dict[str, Any]], tuple[Any, str] | None]
ConstraintFn = Callable[[dict[str, Any], Any], Any | None]


def build_fill_plan(
    fields: list[dict[str, Any]],
    profile: dict[str, Any],
    option_answerer: MappingFn | None = None,
    text_answerer: MappingFn | None = None,
    constrain_value: ConstraintFn | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for field in fields:
        mapping = map_field(field, profile)
        if not mapping and option_answerer:
            mapping = option_answerer(field, profile)
        if not mapping and text_answerer:
            mapping = text_answerer(field, profile)

        if not mapping:
            skipped.append(skip_record(field, "no_mapping"))
            continue

        value, source = mapping
        constrained = constrain_value(field, value) if constrain_value else value
        if constrained is None:
            skipped.append(skip_record(field, "value_not_in_options"))
            continue

        items.append(
            {
                "field": field,
                "fieldKind": classify_field_kind(field),
                "value": constrained,
                "source": source,
            }
        )

    return {"items": items, "skipped": skipped}


def skip_record(field: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "index": field.get("index"),
        "label": field.get("label") or field.get("name") or f"field-{field.get('index')}",
        "reason": reason,
    }
