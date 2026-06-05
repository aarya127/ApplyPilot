(function () {
  const FIELD_SELECTOR = [
    "input:not([type='hidden']):not([disabled])",
    "textarea:not([disabled])",
    "select:not([disabled])",
    "[contenteditable='true']",
    "[role='textbox']",
    "[role='combobox']",
    "[role='checkbox']",
    "[role='radio']"
  ].join(",");

  const state = {
    observer: null,
    lastFilledAt: 0,
    filledCount: 0,
    scanCount: 0
  };

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === "SCAN_FIELDS") {
      const fields = scanFields();
      sendResponse({ ok: true, fields: serializeFields(fields), count: fields.length });
      return false;
    }

    if (message?.type === "AUTOFILL_PAGE") {
      autofillPage()
        .then((result) => sendResponse({ ok: true, result }))
        .catch((error) => sendResponse({ ok: false, error: error.message }));
      return true;
    }

    if (message?.type === "GET_AUTOFILL_STATUS") {
      sendResponse({ ok: true, state });
      return false;
    }

    return false;
  });

  async function autofillPage() {
    const { candidateProfile, settings } = await chrome.storage.local.get([
      "candidateProfile",
      "settings"
    ]);
    const profile = candidateProfile || {};
    const fields = scanFields();
    const localMappings = fields.map((field) => mapField(field, profile, settings || {})).filter(Boolean);
    const backendMappings = await getBackendMappings(fields, profile);
    const mappings = mergeMappings(localMappings, backendMappings);

    let filled = 0;
    const failures = [];

    for (const mapping of mappings) {
      const field = fields.find((item) => item.index === mapping.index);
      const element = field?.elementRef?.deref?.();

      if (!element || !isFillable(element)) {
        continue;
      }

      try {
        const didFill = fillElement(element, mapping, field);
        if (didFill) {
          markFilled(element, mapping);
          filled += 1;
        }
      } catch (error) {
        failures.push({ index: mapping.index, label: field.label, error: error.message });
      }
    }

    state.lastFilledAt = Date.now();
    state.filledCount = filled;
    state.scanCount = fields.length;

    if (settings?.autoFillDynamicFields !== false) {
      startObserver();
    }

    return {
      scanned: fields.length,
      mapped: mappings.length,
      filled,
      failures
    };
  }

  function scanFields() {
    return Array.from(document.querySelectorAll(FIELD_SELECTOR))
      .filter(isFillable)
      .map((element, index) => buildFieldMetadata(element, index));
  }

  function buildFieldMetadata(element, index) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || element.getAttribute("role") || tag).toLowerCase();
    const label = getLabelText(element);
    const options = getOptions(element);

    return {
      index,
      tag,
      type,
      name: element.getAttribute("name") || "",
      id: element.id || "",
      label,
      placeholder: element.getAttribute("placeholder") || "",
      ariaLabel: element.getAttribute("aria-label") || "",
      autocomplete: element.getAttribute("autocomplete") || "",
      value: getCurrentValue(element),
      options,
      surroundingText: getSurroundingText(element),
      elementRef: new WeakRef(element)
    };
  }

  function getLabelText(element) {
    const pieces = [];
    const ariaLabelledBy = element.getAttribute("aria-labelledby");
    const explicitId = element.id;

    if (element.getAttribute("aria-label")) {
      pieces.push(element.getAttribute("aria-label"));
    }

    if (ariaLabelledBy) {
      for (const id of ariaLabelledBy.split(/\s+/)) {
        const labelledBy = document.getElementById(id);
        if (labelledBy?.innerText) {
          pieces.push(labelledBy.innerText);
        }
      }
    }

    if (explicitId) {
      const label = document.querySelector(`label[for="${cssEscape(explicitId)}"]`);
      if (label?.innerText) {
        pieces.push(label.innerText);
      }
    }

    const wrappingLabel = element.closest("label");
    if (wrappingLabel?.innerText) {
      pieces.push(wrappingLabel.innerText);
    }

    const formGroup = element.closest(
      ".form-group, .field, .question, .application-field, [data-qa], [data-testid], li, p, div"
    );
    if (formGroup?.innerText) {
      pieces.push(firstMeaningfulLine(formGroup.innerText));
    }

    return compactText(unique(pieces).join(" "));
  }

  function getSurroundingText(element) {
    const parent = element.closest("label, .form-group, .field, .question, li, div, section");
    return compactText(parent?.innerText || "");
  }

  function getOptions(element) {
    if (element.tagName.toLowerCase() === "select") {
      return Array.from(element.options).map((option) => ({
        label: compactText(option.textContent || ""),
        value: option.value
      }));
    }

    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();

    if (type === "radio" && element.name) {
      return Array.from(document.querySelectorAll(`input[type="radio"][name="${cssEscape(element.name)}"]`))
        .map((radio) => ({
          label: getLabelText(radio) || radio.value,
          value: radio.value
        }));
    }

    if (role === "radio" || role === "checkbox") {
      const group = element.closest("[role='radiogroup'], fieldset, .question, .field, div");
      return Array.from(group?.querySelectorAll("[role='radio'], [role='checkbox']") || []).map((item) => ({
        label: getLabelText(item) || item.innerText || item.getAttribute("aria-label") || "",
        value: item.getAttribute("data-value") || item.getAttribute("aria-label") || item.innerText || ""
      }));
    }

    return [];
  }

  function mapField(field, profile, settings) {
    const haystack = normalize(
      [
        field.label,
        field.placeholder,
        field.name,
        field.id,
        field.ariaLabel,
        field.autocomplete,
        field.surroundingText
      ].join(" ")
    );

    const addressMapping = mapAddressField(field, profile, haystack);
    if (addressMapping) {
      return addressMapping;
    }

    const directRules = [
      [/(\bfirst\b.*\bname\b|\bgiven\b.*\bname\b|fname)/, profile.firstName],
      [/(\blast\b.*\bname\b|\bfamily\b.*\bname\b|lname|surname)/, profile.lastName],
      [/(\bfull\b.*\bname\b|\blegal\b.*\bname\b|\bname\b)/, profile.fullName],
      [/(email|e-mail)/, profile.email],
      [/(phone|mobile|cell|telephone)/, profile.phone],
      [/(linkedin|linked in)/, profile.linkedin],
      [/(github|git hub)/, profile.github],
      [/(portfolio|personal website|website|url)/, profile.portfolio],
      [/(current location|location)/, profile.location],
      [/(school|university|college|institution)/, profile.school],
      [/(degree|program|major)/, profile.degree],
      [/(graduation|grad date|expected completion)/, profile.graduationDate],
      [/(salary|compensation|pay expectation)/, profile.salary || profile.answers?.salary],
      [/(relocat)/, profile.relocation || profile.answers?.relocation],
      [/(non[- ]?compete|restrictive covenant|employment agreement|subject to.*agreement)/,
        profile.subjectToAgreement || profile.answers?.subjectToAgreement]
    ];

    for (const [pattern, value] of directRules) {
      if (pattern.test(haystack) && hasValue(value)) {
        return buildMapping(field, value, "rule", 0.9);
      }
    }

    if (/(sponsor|visa|h-?1b|work permit)/.test(haystack)) {
      return buildMapping(field, profile.needsSponsorship || profile.answers?.sponsorship || "No", "rule", 0.88);
    }

    if (/(canadian citizen|citizen of canada|canada citizenship)/.test(haystack)) {
      return buildMapping(field, profile.canadianCitizen || profile.answers?.canadianCitizen || "Yes", "rule", 0.9);
    }

    if (/(u\.?s\.?|united states).*(green card|permanent resident|lawful permanent resident)/.test(haystack)) {
      return buildMapping(field, profile.usPermanentResident || profile.answers?.usPermanentResident || "Yes", "rule", 0.9);
    }

    if (/(authorized|eligible|legally).*(work|employment)|work authorization/.test(haystack)) {
      return buildMapping(
        field,
        profile.workAuthorization || profile.answers?.workAuthorization || "Yes",
        "rule",
        0.88
      );
    }

    if (settings.autoFillSensitiveFields === true) {
      const demographicMapping = mapSensitiveField(field, profile, haystack);
      if (demographicMapping) {
        return demographicMapping;
      }
    }

    return null;
  }

  function mapAddressField(field, profile, haystack) {
    const address = selectAddress(profile, haystack);

    if (!address) {
      return null;
    }

    const rules = [
      [/(address line 1|address 1|street address|street|mailing address)/, address.line1],
      [/(address line 2|address 2|apt|apartment|suite|unit)/, address.line2],
      [/\bcity\b/, address.city],
      [/(province|state|region)/, address.province || address.state],
      [/(postal code|postcode|zip code|\bzip\b)/, address.postalCode || address.zipCode],
      [/\bcountry\b/, address.country],
      [/\baddress\b/, address.fullAddress]
    ];

    for (const [pattern, value] of rules) {
      if (pattern.test(haystack) && hasValue(value)) {
        return buildMapping(field, value, "rule", 0.9);
      }
    }

    return null;
  }

  function selectAddress(profile, haystack) {
    if (/(\bcanada\b|\bcanadian\b|\bprovince\b|postal code|postcode)/.test(haystack)) {
      return profile.addresses?.canada;
    }

    if (/(\bu\.?s\.?\b|\busa\b|\bunited states\b|\bstate\b|zip code|\bzip\b)/.test(haystack)) {
      return profile.addresses?.usa;
    }

    return profile.addresses?.canada || profile.addresses?.usa || null;
  }

  function mapSensitiveField(field, profile, haystack) {
    const demographics = profile.demographics || {};
    const rules = [
      [/(race|racial)/, demographics.race],
      [/(ethnic|ethnicity)/, demographics.ethnicity],
      [/(gender identity|cisgender|transgender)/, demographics.genderIdentity || demographics.gender],
      [/\bgender\b/, demographics.gender || demographics.genderIdentity]
    ];

    for (const [pattern, value] of rules) {
      if (pattern.test(haystack) && hasValue(value)) {
        return buildMapping(field, value, "sensitive-rule", 0.82);
      }
    }

    return null;
  }

  function buildMapping(field, value, source, confidence) {
    return {
      index: field.index,
      value,
      source,
      confidence
    };
  }

  async function getBackendMappings(fields, profile) {
    const serializableFields = fields.map(({ elementRef, ...field }) => field);

    try {
      const response = await chrome.runtime.sendMessage({
        type: "MAP_FIELDS_WITH_BACKEND",
        payload: {
          fields: serializableFields,
          profile,
          page: {
            url: location.href,
            title: document.title
          }
        }
      });

      if (!response?.ok || !Array.isArray(response.payload?.mappings)) {
        return [];
      }

      return response.payload.mappings;
    } catch (error) {
      return [];
    }
  }

  function mergeMappings(localMappings, backendMappings) {
    const byIndex = new Map();

    for (const mapping of localMappings) {
      byIndex.set(mapping.index, mapping);
    }

    for (const mapping of backendMappings) {
      const existing = byIndex.get(mapping.index);
      if (!existing || Number(mapping.confidence || 0) >= Number(existing.confidence || 0)) {
        byIndex.set(mapping.index, mapping);
      }
    }

    return Array.from(byIndex.values());
  }

  function serializeFields(fields) {
    return fields.map(({ elementRef, ...field }) => field);
  }

  function fillElement(element, mapping, field) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();

    if (tag === "select") {
      return fillSelect(element, mapping.value);
    }

    if (type === "radio" || role === "radio") {
      return fillRadio(element, mapping.value, field);
    }

    if (type === "checkbox" || role === "checkbox") {
      return fillCheckbox(element, mapping.value);
    }

    if (element.isContentEditable || role === "textbox" || role === "combobox") {
      setEditableText(element, mapping.value);
      return true;
    }

    setNativeValue(element, String(mapping.value));
    dispatchFormEvents(element);
    return true;
  }

  function fillSelect(select, desiredValue) {
    const desired = normalize(String(desiredValue));
    const options = Array.from(select.options);
    const option = options.find((item) => normalize(item.value) === desired)
      || options.find((item) => normalize(item.textContent || "") === desired)
      || options.find((item) => normalize(item.textContent || "").includes(desired));

    if (!option) {
      return false;
    }

    select.value = option.value;
    dispatchFormEvents(select);
    return true;
  }

  function fillRadio(element, desiredValue, field) {
    const desired = normalize(String(desiredValue));
    const name = element.getAttribute("name");
    const candidates = name
      ? Array.from(document.querySelectorAll(`input[type="radio"][name="${cssEscape(name)}"]`))
      : [element];

    const match = candidates.find((radio) => {
      const label = normalize(getLabelText(radio));
      const value = normalize(radio.value || radio.getAttribute("aria-label") || "");
      return value === desired || label === desired || label.includes(desired) || desired.includes(label);
    });

    const fallback = field.options?.find((option) => normalize(option.label) === desired);
    const target = match || (fallback ? candidates.find((radio) => radio.value === fallback.value) : null);

    if (!target) {
      return false;
    }

    target.click();
    target.checked = true;
    dispatchFormEvents(target);
    return true;
  }

  function fillCheckbox(element, desiredValue) {
    const shouldCheck = /^(true|yes|y|1|agree|checked)$/i.test(String(desiredValue).trim());

    if (element.getAttribute("role") === "checkbox") {
      const current = element.getAttribute("aria-checked") === "true";
      if (current !== shouldCheck) {
        element.click();
      }
      return true;
    }

    if (element.checked !== shouldCheck) {
      element.click();
    }

    element.checked = shouldCheck;
    dispatchFormEvents(element);
    return true;
  }

  function setEditableText(element, value) {
    element.focus();
    if (element.isContentEditable) {
      element.textContent = String(value);
    } else {
      setNativeValue(element, String(value));
    }
    dispatchFormEvents(element);
  }

  function setNativeValue(element, value) {
    const prototype = Object.getPrototypeOf(element);
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");

    if (descriptor?.set) {
      descriptor.set.call(element, value);
    } else {
      element.value = value;
    }
  }

  function dispatchFormEvents(element) {
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function startObserver() {
    if (state.observer) {
      return;
    }

    let timeoutId = null;
    state.observer = new MutationObserver(() => {
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(() => {
        if (Date.now() - state.lastFilledAt > 800) {
          autofillPage().catch(() => {});
        }
      }, 350);
    });

    state.observer.observe(document.body, {
      childList: true,
      subtree: true
    });
  }

  function markFilled(element, mapping) {
    element.dataset.applicationAutofill = mapping.source || "rule";
    element.style.outline = "2px solid rgba(22, 163, 74, 0.55)";
    element.style.outlineOffset = "2px";
  }

  function isFillable(element) {
    const style = window.getComputedStyle(element);
    const type = (element.getAttribute("type") || "").toLowerCase();

    return !element.disabled
      && !element.readOnly
      && type !== "hidden"
      && type !== "submit"
      && type !== "button"
      && type !== "reset"
      && style.display !== "none"
      && style.visibility !== "hidden";
  }

  function getCurrentValue(element) {
    if (element.getAttribute("role") === "checkbox") {
      return element.getAttribute("aria-checked") || "";
    }

    if ("checked" in element && (element.type === "checkbox" || element.type === "radio")) {
      return element.checked ? element.value : "";
    }

    return element.value || element.textContent || "";
  }

  function hasValue(value) {
    return value !== undefined && value !== null && String(value).trim() !== "";
  }

  function firstMeaningfulLine(text) {
    return text
      .split(/\n+/)
      .map((line) => compactText(line))
      .find((line) => line.length > 1) || "";
  }

  function unique(values) {
    return Array.from(new Set(values.filter(Boolean).map((value) => String(value).trim())));
  }

  function normalize(value) {
    return compactText(value)
      .toLowerCase()
      .replace(/[^\w\s@.+-]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function compactText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function cssEscape(value) {
    if (window.CSS?.escape) {
      return window.CSS.escape(value);
    }

    return String(value).replace(/["\\]/g, "\\$&");
  }
})();
