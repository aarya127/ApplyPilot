from __future__ import annotations

from typing import Any


FIELD_SELECTOR = (
    "input:not([type='hidden']), textarea, select, [contenteditable='true'], "
    "[role='textbox'], [role='combobox'], [aria-haspopup='listbox'], "
    "[data-automation-id='selectWidget'], [data-automation-id='selectShowAll'], "
    "oj-select-single, oj-combobox-one, .select2-choice, .select2-selection, "
    ".sapMInputBaseInner, [data-sap-ui], [aria-autocomplete='list']"
)

OPTION_SELECTOR = (
    "[role='option'], [data-option], .select2-results__option, [role='menuitemradio'], "
    "[data-automation-id='promptOption'], .select__option, [id*='-option-'], "
    ".oj-listbox-result, .oj-listbox-result-label, .oj-option, [oj-option-id], "
    ".sapMSelectListItem, .sapMLIB, .sapMComboBoxBaseItem, "
    ".ui-menu-item, .ui-menu-item-wrapper, .iCIMS_Dropdown_Option"
)

SKIPPED_FRAME_URL_TERMS = (
    "doubleclick",
    "googletagmanager",
    "google-analytics",
    "googlesyndication",
    "adsystem",
    "facebook",
    "recaptcha",
    "gstatic",
    "youtube",
    "vimeo",
    "bat.bing",
    "cookielaw",
    "onetrust",
    "hotjar",
    "clarity.ms",
)


def scan_fields(page: Any) -> list[dict[str, Any]]:
    fields = scan_container_fields(page, page, frame_url="")

    for frame in scannable_frames(page):
        fields.extend(scan_container_fields(page, frame, frame_url=frame.url))

    for index, field in enumerate(fields):
        field["index"] = index

    return fields


def scan_container_fields(page: Any, container: Any, frame_url: str) -> list[dict[str, Any]]:
    try:
        locators = container.locator(FIELD_SELECTOR)
        count = locators.count()
    except Exception:
        return []

    page_url = safe_page_url(page)
    fields: list[dict[str, Any]] = []

    for index in range(count):
        element = locators.nth(index)

        try:
            if not element.is_visible() or not element.is_enabled():
                continue

            options = options_for(element)
            field = {
                "index": index,
                "tag": element.evaluate("element => element.tagName.toLowerCase()"),
                "type": (element.get_attribute("type") or element.get_attribute("role") or "").lower(),
                "name": element.get_attribute("name") or "",
                "id": element.get_attribute("id") or "",
                "placeholder": element.get_attribute("placeholder") or "",
                "aria_label": element.get_attribute("aria-label") or "",
                "data_automation_id": element.get_attribute("data-automation-id") or "",
                "class": element.get_attribute("class") or "",
                "label": label_for(page, element),
                "question_text": question_text_for(element),
                "surrounding_text": surrounding_text_for(element),
                "options": options,
                "frame_url": frame_url,
                "page_url": page_url,
                "locator": element,
            }
            field["selector"], field["selector_strategy"] = durable_selector(container, field, index)
            if not options and is_dynamic_dropdown_field(field):
                field["options"] = dynamic_options_for(element, container)

            fields.append(field)
        except Exception:
            continue

    return fields


def scannable_frames(page: Any) -> list[Any]:
    try:
        all_frames = list(page.frames)
    except Exception:
        return []

    main_frame = getattr(page, "main_frame", None)
    frames: list[Any] = []

    for frame in all_frames:
        if frame is main_frame:
            continue

        try:
            if frame.is_detached():
                continue
        except Exception:
            pass

        url = str(getattr(frame, "url", "") or "").lower()
        if not url.startswith("http"):
            continue

        if any(term in url for term in SKIPPED_FRAME_URL_TERMS):
            continue

        frames.append(frame)

    return frames


def durable_selector(container: Any, field: dict[str, Any], index: int) -> tuple[str, str]:
    field_id = str(field.get("id") or "")
    if field_id:
        selector = f'[id="{css_attribute_escape(field_id)}"]'
        if selector_is_unique(container, selector):
            return selector, "css"

    name = str(field.get("name") or "")
    if name:
        base = f'[name="{css_attribute_escape(name)}"]'
        tag = str(field.get("tag") or "")
        candidates = [base]
        if tag:
            candidates.append(f"{tag}{base}")

        for selector in candidates:
            if selector_is_unique(container, selector):
                return selector, "css"

    for label_text in [str(field.get("aria_label") or ""), str(field.get("label") or "")]:
        label_text = label_text.strip()
        if not label_text:
            continue

        try:
            if container.get_by_label(label_text, exact=True).count() == 1:
                return label_text, "label"
        except Exception:
            continue

    return str(index), "nth"


def field_frame(page: Any, field: dict[str, Any]) -> Any | None:
    frame_url = str(field.get("frame_url") or "")
    if not frame_url:
        return page

    try:
        frame = page.frame(url=frame_url)
        if frame is not None:
            return frame
    except Exception:
        pass

    try:
        for frame in page.frames:
            if frame.url == frame_url:
                return frame
    except Exception:
        pass

    return None


def field_locator(page: Any, field: dict[str, Any]) -> Any | None:
    strategy = str(field.get("selector_strategy") or "")
    selector = str(field.get("selector") or "")
    container = field_frame(page, field) if page is not None else None

    if container is None or not strategy:
        return field.get("locator")

    try:
        if strategy == "css":
            return container.locator(selector).first
        if strategy == "label":
            return container.get_by_label(selector, exact=True).first
        if strategy == "nth":
            return container.locator(FIELD_SELECTOR).nth(int(selector))
    except Exception:
        pass

    return field.get("locator")


def selector_is_unique(container: Any, selector: str) -> bool:
    try:
        return container.locator(selector).count() == 1
    except Exception:
        return False


def css_attribute_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def safe_page_url(page: Any) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


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
            const parent = element.closest(
              '.field, .form-group, .question, .oj-form-control, .oj-flex-item, ' +
              '.iCIMS_FormField, .form-field, .sapMInputBase, .sapMFlexItem, li, div'
            );
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


def is_dynamic_dropdown_field(field: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(field.get("label") or ""),
            str(field.get("placeholder") or ""),
            str(field.get("name") or ""),
            str(field.get("id") or ""),
            str(field.get("aria_label") or ""),
            str(field.get("surrounding_text") or ""),
        ]
    ).lower()
    return (
        field.get("type") == "combobox"
        or field.get("tag") == "button"
        or "listbox" in text
        or "combobox" in text
        or field.get("data_automation_id", "").lower() in {"selectwidget", "selectshowall"}
        or any(term in field.get("class", "").lower() for term in ["select2", "sapm", "oj-", "icims"])
        or any(term in text for term in ["select one", "dropdown", "country", "state", "province", "location", "city", "phone code"])
    )


def dynamic_options_for(element: Any, container: Any = None) -> list[dict[str, str]]:
    try:
        page = element.page
    except Exception:
        return []

    container = container if container is not None else page

    try:
        element.click(timeout=1_500)
        page.wait_for_timeout(250)
        options = visible_dropdown_options(container)

        if not options:
            try:
                element.press("ArrowDown", timeout=800)
            except Exception:
                pass
            page.wait_for_timeout(250)
            options = visible_dropdown_options(container)

        try:
            element.press("Escape", timeout=800)
        except Exception:
            pass

        return unique_options(options)
    except Exception:
        try:
            element.press("Escape", timeout=800)
        except Exception:
            pass
        return []


def visible_dropdown_options(page: Any) -> list[dict[str, str]]:
    options = page.locator(OPTION_SELECTOR)
    values: list[dict[str, str]] = []

    for index in range(min(options.count(), 80)):
        option = options.nth(index)
        try:
            if not option.is_visible():
                continue

            label = option.inner_text(timeout=500)
            value = (
                option.get_attribute("data-automation-label")
                or option.get_attribute("data-value")
                or option.get_attribute("data-id")
                or option.get_attribute("data-key")
                or option.get_attribute("oj-option-id")
                or option.get_attribute("value")
                or option.get_attribute("aria-label")
                or ""
            )
            label = " ".join(str(label or "").split())
            value = " ".join(str(value or "").split())
            if label or value:
                values.append({"label": label, "value": value})
        except Exception:
            continue

    return values


def unique_options(options: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []

    for option in options:
        key = f"{option.get('label', '')}\0{option.get('value', '')}".lower()
        if key in seen:
            continue

        seen.add(key)
        unique.append(option)

    return unique


def surrounding_text_for(element: Any) -> str:
    try:
        return element.evaluate(
            """
            element => {
              const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
              const parent = element.closest(
                'label, fieldset, .field, .form-group, .question, .oj-form-control, .oj-flex-item, ' +
                '.iCIMS_FormField, .form-field, .sapMInputBase, .sapMFlexItem, li, div, section'
              );
              return clean(parent ? (parent.innerText || parent.textContent || '') : '');
            }
            """
        )
    except Exception:
        return ""


def question_text_for(element: Any) -> str:
    try:
        return element.evaluate(
            """
            element => {
              const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
              const isQuestion = line => /[?]|\\b(now|previously|ever|worked|employed|subsidiar|affiliate|authorize|authorization|sponsor|sponsorship|require|visa|military|served|spouse|domestic partner|relative|dealer|contractor|relocat|age|proof of age|disability)\\b/i.test(line);
              let node = element;
              for (let depth = 0; node && node !== document.body && depth < 8; depth += 1) {
                const text = clean(node.innerText || node.textContent || '');
                const lines = text.split(/\\n+/).map(clean).filter(line => line.length > 3 && line.length < 360);
                const found = lines.find(isQuestion);
                if (found) return found;
                node = node.parentElement;
              }

              const rect = element.getBoundingClientRect();
              const candidates = Array.from(document.querySelectorAll('label, legend, p, div, span, [role="heading"]'))
                .filter(item => {
                  const itemRect = item.getBoundingClientRect();
                  return itemRect.bottom <= rect.top && rect.top - itemRect.bottom < 520;
                })
                .map(item => clean(item.innerText || item.textContent || ''))
                .flatMap(text => text.split(/\\n+/).map(clean))
                .filter(line => line.length > 3 && line.length < 360 && isQuestion(line));
              return candidates[candidates.length - 1] || '';
            }
            """
        )
    except Exception:
        return ""
