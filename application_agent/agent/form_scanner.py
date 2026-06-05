from __future__ import annotations

from typing import Any


FIELD_SELECTOR = "input:not([type='hidden']), textarea, select, [contenteditable='true'], [role='textbox'], [role='combobox']"


def scan_fields(page: Any) -> list[dict[str, Any]]:
    locators = page.locator(FIELD_SELECTOR)
    fields: list[dict[str, Any]] = []

    for index in range(locators.count()):
        element = locators.nth(index)

        try:
            if not element.is_visible() or not element.is_enabled():
                continue

            fields.append(
                {
                    "index": index,
                    "tag": element.evaluate("element => element.tagName.toLowerCase()"),
                    "type": (element.get_attribute("type") or element.get_attribute("role") or "").lower(),
                    "name": element.get_attribute("name") or "",
                    "id": element.get_attribute("id") or "",
                    "placeholder": element.get_attribute("placeholder") or "",
                    "aria_label": element.get_attribute("aria-label") or "",
                    "label": label_for(page, element),
                    "options": options_for(element),
                    "locator": element,
                }
            )
        except Exception:
            continue

    return fields


def label_for(page: Any, element: Any) -> str:
    return element.evaluate(
        """
        element => {
          const pieces = [];
          const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
          const withoutControls = label => {
            if (!label) return '';
            const clone = label.cloneNode(true);
            clone.querySelectorAll('input, textarea, select, button, option').forEach(node => node.remove());
            return clean(clone.innerText || clone.textContent || '');
          };

          if (element.getAttribute('aria-label')) pieces.push(element.getAttribute('aria-label'));

          const ariaLabelledBy = element.getAttribute('aria-labelledby');
          if (ariaLabelledBy) {
            for (const id of ariaLabelledBy.split(/\\s+/)) {
              const node = document.getElementById(id);
              if (node) pieces.push(clean(node.innerText || node.textContent || ''));
            }
          }

          if (element.id) {
            const label = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
            const text = withoutControls(label);
            if (text) pieces.push(text);
          }

          const wrapping = element.closest('label');
          const wrappingText = withoutControls(wrapping);
          if (wrappingText) pieces.push(wrappingText);

          if (!pieces.length) {
            const parent = element.closest('.field, .form-group, .question, li, div');
            if (parent) pieces.push(clean(parent.innerText || parent.textContent || '').split('\\n')[0]);
          }

          return clean([...new Set(pieces.filter(Boolean))].join(' '));
        }
        """
    )


def options_for(element: Any) -> list[dict[str, str]]:
    try:
        return element.evaluate(
            """
            element => {
              if (element.tagName.toLowerCase() !== 'select') return [];
              return Array.from(element.options).map(option => ({
                label: (option.textContent || '').replace(/\\s+/g, ' ').trim(),
                value: option.value || ''
              }));
            }
            """
        )
    except Exception:
        return []

