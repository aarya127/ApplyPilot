(function () {
  if (window.__applicationAutofillContentLoaded) {
    return;
  }

  window.__applicationAutofillContentLoaded = true;

  const FIELD_SELECTOR = [
    "input:not([type='hidden']):not([disabled])",
    "textarea:not([disabled])",
    "select:not([disabled])",
    "[contenteditable='true']",
    "[role='textbox']",
    "[role='combobox']",
    "button[aria-haspopup='listbox']:not([disabled])",
    "[aria-haspopup='listbox']:not([disabled])",
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

    if (message?.type === "PREVIEW_AUTOFILL") {
      previewAutofill()
        .then((result) => sendResponse({ ok: true, result }))
        .catch((error) => sendResponse({ ok: false, error: error.message }));
      return true;
    }

    if (message?.type === "APPLY_AUTOFILL_MAPPINGS") {
      applyMappings(message.mappings || [])
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
    const plan = await buildAutofillPlan();
    const result = await applyMappings(plan.mappings);

    return {
      ...result,
      scanned: plan.fields.length,
      mapped: plan.mappings.length
    };
  }

  async function previewAutofill() {
    const plan = await buildAutofillPlan();
    const mappedIndexes = new Set(plan.mappings.map((mapping) => mapping.index));
    const unmappedFields = plan.fields
      .filter((field) => !mappedIndexes.has(field.index))
      .filter(shouldAskForField)
      .map(({ elementRef, ...field }) => ({
        ...field,
        label: field.label || field.name || field.id || `Field ${field.index + 1}`,
        answerKey: answerKeyForField(field),
        needsManualUpload: field.type === "file"
      }));

    return {
      scanned: plan.fields.length,
      mapped: plan.mappings.length,
      mappings: plan.mappings.map((mapping) => {
        const field = plan.fields.find((item) => item.index === mapping.index);

        return {
          ...mapping,
          label: field?.label || field?.name || field?.id || `Field ${mapping.index + 1}`,
          tag: field?.tag || "",
          type: field?.type || "",
          options: field?.options || []
        };
      }),
      unmappedFields,
      manualTasks: unmappedFields
        .filter((field) => field.needsManualUpload)
        .map((field) => ({
          index: field.index,
          label: field.label,
          task: "Upload resume manually",
          resumeFileName: plan.profile.resumeFileName || ""
        })),
      page: {
        url: location.href,
        title: document.title
      }
    };
  }

  async function buildAutofillPlan() {
    const { candidateProfile, settings } = await chrome.storage.local.get([
      "candidateProfile",
      "settings"
    ]);
    const profile = candidateProfile || {};
    const fields = scanFields();
    const localMappings = fields.map((field) => mapField(field, profile, settings || {})).filter(Boolean);
    const backendMappings = await getBackendMappings(fields, profile);
    const mappings = mergeMappings(localMappings, backendMappings);

    return { fields, mappings, profile };
  }

  async function applyMappings(mappings) {
    const { settings } = await chrome.storage.local.get("settings");
    const fields = scanFields();
    let filled = 0;
    const failures = [];

    for (const mapping of mappings) {
      const field = fields.find((item) => item.index === mapping.index);
      const element = field?.elementRef?.deref?.();

      if (!element || !isFillable(element)) {
        continue;
      }

      try {
        const didFill = await fillElement(element, mapping, field);
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
      answerKey: "",
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

    if (pieces.length === 0) {
      const formGroup = element.closest(
        ".form-group, .field, .question, .application-field, [data-qa], [data-testid], li, p, div"
      );
      if (formGroup?.innerText) {
        pieces.push(firstMeaningfulLine(formGroup.innerText));
      }
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
    const primaryHaystack = normalize(
      [
        field.label,
        field.placeholder,
        field.name,
        field.id,
        field.ariaLabel,
        field.autocomplete
      ].join(" ")
    );
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

    if (shouldSkipField(primaryHaystack)) {
      return null;
    }

    const savedAnswer = findSavedAnswer(field, profile);
    if (hasValue(savedAnswer)) {
      return buildMapping(field, savedAnswer, "saved-answer", 0.95);
    }

    const address = selectAddress(profile, settings, primaryHaystack);

    const agreementMapping = mapAgreementCheckbox(field, primaryHaystack);
    if (agreementMapping) {
      return agreementMapping;
    }

    const workQuestionMapping = mapWorkQuestion(field, profile, primaryHaystack);
    if (workQuestionMapping) {
      return workQuestionMapping;
    }

    const locationMapping = mapLocationField(field, address, primaryHaystack);
    if (locationMapping) {
      return locationMapping;
    }

    if (/(\bmiddle\b.*\bname\b|mname)/.test(primaryHaystack)) {
      return hasValue(profile.middleName) ? buildMapping(field, profile.middleName, "rule", 0.88) : null;
    }

    if (/(second last name|second surname|additional last name)/.test(primaryHaystack)) {
      return hasValue(profile.secondLastName) ? buildMapping(field, profile.secondLastName, "rule", 0.88) : null;
    }

    const directRules = [
      [/(\bfirst\b.*\bname\b|\bgiven\b.*\bname\b|fname)/, profile.firstName],
      [/(\blast\b.*\bname\b|\bfamily\b.*\bname\b|lname|surname)/, profile.lastName],
      [/(\bfull\b.*\bname\b|\blegal name\b|\bname as it appears\b)/, profile.fullName],
      [/(email|e-mail)/, profile.email],
      [/(phone|mobile|cell|telephone)/, profile.phone],
      [/(linkedin profile|linkedin url|linked in profile|linked in url)/, profile.linkedin],
      [/(github profile|github url|git hub profile|git hub url)/, profile.github],
      [/(portfolio|personal website|website url|personal site)/, profile.portfolio],
      [/(school|university|college|institution)/, profile.school],
      [/(degree|program|major)/, profile.degree],
      [/(graduation|grad date|expected completion)/, profile.graduationDate],
      [/(salary|compensation|pay expectation)/, profile.salary || profile.answers?.salary],
      [/(relocat)/, profile.relocation || profile.answers?.relocation],
      [/(non[- ]?compete|restrictive covenant|employment agreement|subject to.*agreement)/,
        profile.subjectToAgreement || profile.answers?.subjectToAgreement]
    ];

    for (const [pattern, value] of directRules) {
      if (pattern.test(primaryHaystack) && hasValue(value)) {
        return buildMapping(field, value, "rule", 0.9);
      }
    }

    const addressMapping = mapAddressField(field, profile, settings, primaryHaystack);
    if (addressMapping) {
      return addressMapping;
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

    if (/(veteran|protected veteran|armed forces|military service)/.test(haystack)) {
      return buildMapping(field, profile.veteranStatus || profile.answers?.veteranStatus || "No", "rule", 0.9);
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

  function findSavedAnswer(field, profile) {
    const answers = profile.answers || {};
    const keys = [
      answerKeyForField(field),
      normalize(field.label),
      normalize(field.name),
      normalize(field.id)
    ].filter(Boolean);

    for (const key of keys) {
      if (hasValue(answers[key])) {
        return answers[key];
      }
    }

    return "";
  }

  function shouldAskForField(field) {
    const haystack = normalize([
      field.label,
      field.placeholder,
      field.name,
      field.id,
      field.ariaLabel
    ].join(" "));

    if (!haystack || shouldSkipField(haystack)) {
      return false;
    }

    if (field.type === "file") {
      return true;
    }

    if (field.tag === "input" || field.tag === "textarea" || field.tag === "select") {
      return true;
    }

    return field.type === "radio"
      || field.type === "checkbox"
      || field.type === "combobox"
      || field.ariaLabel
      || field.tag === "button";
  }

  function answerKeyForField(field) {
    const basis = field.label || field.name || field.id || field.placeholder || `field-${field.index}`;
    return `custom:${normalize(basis).replace(/\s+/g, "-").slice(0, 80)}`;
  }

  function mapWorkQuestion(field, profile, haystack) {
    if (/(sponsor|visa|h-?1b|work permit)/.test(haystack)) {
      return buildMapping(field, profile.needsSponsorship || profile.answers?.sponsorship || "No", "rule", 0.9);
    }

    if (/(authorized|eligible|legally).*(work|employment)|work authorization/.test(haystack)) {
      return buildMapping(
        field,
        profile.workAuthorization || profile.answers?.workAuthorization || "Yes",
        "rule",
        0.9
      );
    }

    if (/(work remotely|remote location|plan to work remote)/.test(haystack)) {
      const remoteAnswer = profile.answers?.remoteWork || profile.remoteWork;
      return hasValue(remoteAnswer) ? buildMapping(field, remoteAnswer, "rule", 0.82) : null;
    }

    return null;
  }

  function mapLocationField(field, address, haystack) {
    if (!address) {
      return null;
    }

    if (/(location city|city location|current city|where.*city)/.test(haystack)) {
      return hasValue(address.city) ? buildMapping(field, address.city, "rule", 0.9) : null;
    }

    if (/(currently reside|current residence|country.*reside|country region|country\/region|\bcountry\b)/.test(haystack)) {
      return hasValue(address.country) ? buildMapping(field, address.country, "rule", 0.9) : null;
    }

    return null;
  }

  function shouldSkipField(haystack) {
    return [
      /\bcompany name\b/,
      /\bemployer name\b/,
      /\borganization name\b/,
      /\bif yes\b/,
      /\bif applicable\b/,
      /\blast assigned\b/,
      /\bcookie/,
      /\btracking/,
      /\badvertis/,
      /\bprovider linkedin\b/,
      /\bconsent to cookies\b/,
      /\bmarketing consent\b/,
      /\bprivacy preferences\b/
    ].some((pattern) => pattern.test(haystack));
  }

  function mapAgreementCheckbox(field, haystack) {
    if (!isCheckboxField(field)) {
      return null;
    }

    if (/(^|\b)(i agree|agree to|acknowledge|certify|i certify|i understand)\b/.test(haystack)) {
      return buildMapping(field, true, "rule", 0.86);
    }

    return null;
  }

  function mapAddressField(field, profile, settings, haystack) {
    const address = selectAddress(profile, settings, haystack);

    if (!address) {
      return null;
    }

    if (/(address line 2|address 2|apt|apartment|suite|unit)/.test(haystack)) {
      return hasValue(address.line2) ? buildMapping(field, address.line2, "rule", 0.9) : null;
    }

    const rules = [
      [/(address line 1|address 1|street address|street|mailing address)/, address.line1],
      [/\bcity\b/, address.city],
      [/\bcountry\b/, address.country],
      [/\b(province|state|region)\b/, address.province || address.state],
      [/(postal code|postcode|zip code|\bzip\b)/, address.postalCode || address.zipCode],
      [/(full address|mailing address|home address|residential address)/, address.fullAddress]
    ];

    for (const [pattern, value] of rules) {
      if (pattern.test(haystack) && hasValue(value)) {
        return buildMapping(field, value, "rule", 0.9);
      }
    }

    return null;
  }

  function selectAddress(profile, settings, haystack) {
    if (/(\bcanada\b|\bcanadian\b|\bprovince\b|postal code|postcode)/.test(haystack)) {
      return profile.addresses?.canada;
    }

    if (/(\bu\.?s\.?\b|\busa\b|\bunited states\b|\bstate\b|zip code|\bzip\b)/.test(haystack)) {
      return profile.addresses?.usa;
    }

    if (settings?.targetCountry === "usa") {
      return profile.addresses?.usa || profile.addresses?.canada || null;
    }

    if (settings?.targetCountry === "canada") {
      return profile.addresses?.canada || profile.addresses?.usa || null;
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

  function isCheckboxField(field) {
    return field.type === "checkbox";
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

  async function fillElement(element, mapping, field) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    const hasListboxPopup = element.getAttribute("aria-haspopup") === "listbox";

    if (type === "file") {
      return false;
    }

    if (tag === "select") {
      return fillSelect(element, mapping.value);
    }

    if (type === "radio" || role === "radio") {
      return fillRadio(element, mapping.value, field);
    }

    if (type === "checkbox" || role === "checkbox") {
      return fillCheckbox(element, mapping.value);
    }

    if (role === "combobox" || hasListboxPopup) {
      return fillCombobox(element, mapping.value);
    }

    if (element.isContentEditable || role === "textbox") {
      setEditableText(element, mapping.value);
      return true;
    }

    setNativeValue(element, String(mapping.value));
    dispatchFormEvents(element);
    return true;
  }

  async function fillCombobox(element, desiredValue) {
    element.focus();
    element.click();
    setEditableText(element, desiredValue);
    await sleep(200);

    const desired = normalize(String(desiredValue));
    const option = Array.from(document.querySelectorAll("[role='option'], [data-option], .select2-results__option"))
      .find((item) => normalize(item.textContent || item.getAttribute("aria-label") || "").includes(desired));

    if (option) {
      option.click();
    }

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

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
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
