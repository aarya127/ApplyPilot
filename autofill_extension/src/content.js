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
    "[role='button'][aria-haspopup='listbox']:not([aria-disabled='true'])",
    "button[aria-haspopup='listbox']:not([disabled])",
    "[aria-haspopup='listbox']:not([disabled])",
    "[data-automation-id='selectWidget']:not([aria-disabled='true'])",
    "[data-automation-id='selectShowAll']:not([aria-disabled='true'])",
    "[role='checkbox']",
    "[role='radio']"
  ].join(",");

  const state = {
    observer: null,
    lastFilledAt: 0,
    filledCount: 0,
    scanCount: 0,
    isApplying: false,
    dynamicRunCount: 0,
    lastPreviewFields: []
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
    state.dynamicRunCount = 0;
    return runAutofillPage();
  }

  async function runAutofillPage() {
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
        label: displayLabelForField(field),
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
    await prepareRepeatableSections(profile);
    const fields = scanFields();
    await enrichDynamicDropdownOptions(fields);
    state.lastPreviewFields = fields.map(({ elementRef, choiceRefs, ...field }) => field);
    const localMappings = fields.map((field) => mapField(field, profile, settings || {})).filter(Boolean);
    const repeatableMappings = [
      ...mapRepeatableEmploymentFields(fields, profile),
      ...mapRepeatableEducationFields(fields, profile),
      ...mapRepeatableWebsiteFields(fields, profile)
    ];
    const backendMappings = settings?.autoMapAmbiguousFields === true
      ? await getBackendMappings(fields, profile)
      : [];
    const mappings = mergeMappings([...localMappings, ...repeatableMappings], backendMappings, fields);

    return { fields, mappings, profile };
  }

  async function applyMappings(mappings) {
    if (state.isApplying) {
      return {
        scanned: 0,
        mapped: mappings.length,
        filled: 0,
        failures: []
      };
    }

    state.isApplying = true;
    const { candidateProfile, settings } = await chrome.storage.local.get([
      "candidateProfile",
      "settings"
    ]);
    const profile = candidateProfile || {};
    const fields = scanFields();
    hydrateFieldsFromPreview(fields);
    let filled = 0;
    const failures = [];

    try {
      if (await fillWorkdayTargetCountry(profile, settings || {})) {
        filled += 1;
        await sleep(700);
        fields.length = 0;
        fields.push(...scanFields());
        await enrichDynamicDropdownOptions(fields);
        hydrateFieldsFromPreview(fields);
        mappings = mergeMappings(
          mappings,
          fields.map((field) => mapField(field, profile, settings || {})).filter(Boolean),
          fields
        );
      }

      for (const mapping of mappings) {
        const field = fields.find((item) => item.index === mapping.index);
        const element = field?.elementRef?.deref?.();

        if (!element || !isFillable(element)) {
          continue;
        }

        try {
          const didFill = await fillElement(element, mapping, field);
          if (didFill) {
            confirmFilledElement(element);
            markFilled(element, mapping);
            filled += 1;
          }
        } catch (error) {
          failures.push({ index: mapping.index, label: field.label, error: error.message });
        }
      }

      filled += await fillWorkdayExperienceDateFallback(profile);
    } finally {
      state.isApplying = false;
    }

    state.lastFilledAt = Date.now();
    state.filledCount = filled;
    state.scanCount = fields.length;

    if (settings?.autoFillDynamicFields === true) {
      startObserver();
    } else {
      stopObserver();
    }

    return {
      scanned: fields.length,
      mapped: mappings.length,
      filled,
      failures
    };
  }

  function scanFields() {
    const elements = Array.from(document.querySelectorAll(FIELD_SELECTOR)).filter(isFillable);
    const choiceGroups = buildChoiceGroups(elements);
    const groupedElements = new Set(choiceGroups.flatMap((group) => group.elements));
    const fields = [];
    let index = 0;

    for (const group of choiceGroups) {
      fields.push(buildChoiceGroupMetadata(group, index));
      index += 1;
    }

    for (const element of elements) {
      if (groupedElements.has(element)) {
        continue;
      }

      fields.push(buildFieldMetadata(element, index));
      index += 1;
    }

    return fields;
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
      dataAutomationId: element.getAttribute("data-automation-id") || "",
      value: getCurrentValue(element),
      options,
      surroundingText: getSurroundingText(element),
      answerKey: "",
      elementRef: new WeakRef(element)
    };
  }

  function buildChoiceGroups(elements) {
    const groups = new Map();

    for (const element of elements) {
      const type = (element.getAttribute("type") || "").toLowerCase();
      const role = (element.getAttribute("role") || "").toLowerCase();

      if (!isChoiceControl(element)) {
        continue;
      }

      if (type === "checkbox" && isStandaloneCheckbox(element)) {
        continue;
      }

      const group = choiceGroupFor(element);
      if (!group) {
        continue;
      }

      const existing = groups.get(group.key) || {
        key: group.key,
        container: group.container,
        elements: [],
        mode: type === "checkbox" || role === "checkbox" ? "checkbox" : "radio"
      };
      existing.elements.push(element);
      groups.set(group.key, existing);
    }

    return Array.from(groups.values()).filter((group) => group.elements.length > 1);
  }

  function buildChoiceGroupMetadata(group, index) {
    const first = group.elements[0];
    const options = group.elements.map((element) => ({
      label: choiceLabel(element),
      value: choiceValue(element)
    }));
    const initialLabel = choiceGroupLabel(group.container, options) || getSurroundingText(first) || getLabelText(first);
    const label = isLowInformationText(initialLabel)
      ? parentQuestionLabel(first, options) || initialLabel
      : initialLabel;

    return {
      index,
      tag: "choice-group",
      type: group.mode,
      name: first.getAttribute("name") || "",
      id: first.id || "",
      label,
      placeholder: "",
      ariaLabel: group.container?.getAttribute?.("aria-label") || first.getAttribute("aria-label") || "",
      autocomplete: "",
      value: "",
      options,
      surroundingText: compactText(group.container?.innerText || ""),
      answerKey: "",
      elementRef: new WeakRef(first),
      choiceRefs: group.elements.map((element) => new WeakRef(element))
    };
  }

  function isChoiceControl(element) {
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    return type === "radio" || type === "checkbox" || role === "radio" || role === "checkbox";
  }

  function isStandaloneCheckbox(element) {
    const haystack = normalize([getLabelText(element), getSurroundingText(element)].join(" "));
    return /(^|\b)(i agree|agree to|acknowledge|certify|i certify|i understand)\b/.test(haystack);
  }

  function choiceGroupFor(element) {
    const type = (element.getAttribute("type") || "").toLowerCase();
    const name = element.getAttribute("name");

    if (type === "radio" && name) {
      const radios = Array.from(document.querySelectorAll(`input[type="radio"][name="${cssEscape(name)}"]`))
        .filter(isFillable);
      const container = commonChoiceContainer(radios) || element.closest("fieldset, [role='radiogroup'], [role='group']");
      return { key: `radio:${name}`, container: container || element.parentElement };
    }

    const explicit = element.closest("fieldset, [role='radiogroup'], [role='group']");
    if (explicit) {
      return { key: elementGroupKey(explicit), container: explicit };
    }

    const container = nearestChoiceContainer(element);
    return container ? { key: elementGroupKey(container), container } : null;
  }

  function nearestChoiceContainer(element) {
    let current = element.parentElement;
    let depth = 0;

    while (current && current !== document.body && depth < 7) {
      const choices = Array.from(current.querySelectorAll("input[type='radio'], input[type='checkbox'], [role='radio'], [role='checkbox']"))
        .filter(isFillable);

      if (choices.length > 1 && choices.length <= 20) {
        return current;
      }

      current = current.parentElement;
      depth += 1;
    }

    return null;
  }

  function commonChoiceContainer(elements) {
    if (!elements.length) {
      return null;
    }

    let current = elements[0].parentElement;
    while (current && current !== document.body) {
      if (elements.every((element) => current.contains(element))) {
        return current;
      }
      current = current.parentElement;
    }

    return null;
  }

  function elementGroupKey(element) {
    if (!element.dataset.applicationAutofillGroupId) {
      element.dataset.applicationAutofillGroupId = `group-${Math.random().toString(36).slice(2)}`;
    }

    return element.dataset.applicationAutofillGroupId;
  }

  function choiceLabel(element) {
    return compactText(
      getLabelText(element)
      || element.getAttribute("aria-label")
      || element.innerText
      || element.textContent
      || element.value
      || ""
    );
  }

  function choiceValue(element) {
    return compactText(
      element.value
      || element.getAttribute("data-value")
      || element.getAttribute("aria-label")
      || element.innerText
      || element.textContent
      || ""
    );
  }

  function choiceGroupLabel(container, options) {
    if (!container) {
      return "";
    }

    const clone = container.cloneNode(true);
    clone.querySelectorAll("label").forEach((label) => {
      if (label.querySelector("input, [role='radio'], [role='checkbox']")) {
        label.remove();
      }
    });
    clone.querySelectorAll("input, textarea, select, button, option, [role='radio'], [role='checkbox']").forEach((node) => node.remove());
    const optionLabels = new Set(options.map((option) => normalize(option.label)).filter(Boolean));
    const lines = (clone.innerText || clone.textContent || "")
      .split(/\n+/)
      .map((line) => compactText(line))
      .filter((line) => line.length > 1)
      .filter((line) => !optionLabels.has(normalize(line)));

    return lines[0] || "";
  }

  function parentQuestionLabel(element, options) {
    const optionLabels = new Set(options.map((option) => normalize(option.label)).filter(Boolean));
    let current = element.parentElement;
    let depth = 0;

    while (current && current !== document.body && depth < 8) {
      const clone = current.cloneNode(true);
      clone.querySelectorAll("input, textarea, select, button, option, [role='radio'], [role='checkbox']").forEach((node) => node.remove());
      const lines = (clone.innerText || clone.textContent || "")
        .split(/\n+/)
        .map((line) => compactText(line))
        .filter((line) => line.length > 1)
        .filter((line) => !optionLabels.has(normalize(line)))
        .filter((line) => !/^\*?\s*(required|select one)\s*$/i.test(line));
      const question = lines.find((line) => /[?]|\b(now|previously|ever|worked|employed|subsidiar|affiliate|authorize|sponsor|require)\b/i.test(line));

      if (question) {
        return question;
      }

      current = current.parentElement;
      depth += 1;
    }

    return "";
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
      const text = labelTextWithoutControls(label);
      if (text) {
        pieces.push(text);
      }
    }

    const wrappingLabel = element.closest("label");
    const wrappingLabelText = labelTextWithoutControls(wrappingLabel);
    if (wrappingLabelText) {
      pieces.push(wrappingLabelText);
    }

    if (pieces.length === 0) {
      const formGroup = element.closest(
        "[data-automation-id^='formField-'], .form-group, .field, .question, .application-field, [data-qa], [data-testid], li, p, div"
      );
      if (formGroup?.innerText) {
        pieces.push(firstMeaningfulLine(formGroup.innerText));
      }
    }

    return compactText(unique(pieces).join(" "));
  }

  function labelTextWithoutControls(label) {
    if (!label) {
      return "";
    }

    const clone = label.cloneNode(true);
    clone.querySelectorAll("input, textarea, select, button, option, [role='option']").forEach((node) => node.remove());
    return compactText(clone.innerText || clone.textContent || "");
  }

  function getSurroundingText(element) {
    const parent = element.closest("label, [data-automation-id^='formField-'], .form-group, .field, .question, li, div, section");
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

  async function enrichDynamicDropdownOptions(fields) {
    for (const field of fields) {
      if (field.options?.length || !isDynamicDropdownField(field)) {
        continue;
      }

      const element = field.elementRef?.deref?.();
      if (!element || !isFillable(element)) {
        continue;
      }

      const options = await discoverDynamicDropdownOptions(element);
      if (options.length && options.length <= 40) {
        field.options = options;
      }
    }
  }

  function isDynamicDropdownField(field) {
    return field.type === "combobox"
      || field.tag === "button"
      || /selectwidget|selectshowall/i.test(field.dataAutomationId || "")
      || field.ariaLabel
      || /listbox|combobox/i.test([field.type, field.ariaLabel, field.surroundingText].join(" "));
  }

  async function discoverDynamicDropdownOptions(element) {
    const trigger = dropdownTrigger(element);

    if (!trigger) {
      return [];
    }

    const active = document.activeElement;

    try {
      trigger.focus();
      trigger.click();
      await sleep(180);

      const options = collectVisibleDropdownOptions(trigger);
      closeDynamicDropdown(trigger);

      if (active && active !== trigger && typeof active.focus === "function") {
        active.focus();
      }

      return uniqueOptions(options);
    } catch (error) {
      closeDynamicDropdown(trigger);
      return [];
    }
  }

  function dropdownTrigger(element) {
    if (isListboxTrigger(element)) {
      return element;
    }

    return element.querySelector?.("[aria-haspopup='listbox'], [role='combobox'], [role='button'][aria-haspopup], button");
  }

  function isListboxTrigger(element) {
    const role = (element.getAttribute("role") || "").toLowerCase();
    const popup = (element.getAttribute("aria-haspopup") || "").toLowerCase();
    const automationId = (element.getAttribute("data-automation-id") || "").toLowerCase();
    return role === "combobox"
      || popup === "listbox"
      || popup === "true"
      || automationId === "selectwidget"
      || automationId === "selectshowall";
  }

  function collectVisibleDropdownOptions(trigger) {
    const containers = [];
    const controls = trigger.getAttribute("aria-controls");
    const owns = trigger.getAttribute("aria-owns");

    for (const id of [controls, owns].filter(Boolean)) {
      const container = document.getElementById(id);
      if (container) {
        containers.push(container);
      }
    }

    containers.push(document);

    const optionNodes = containers.flatMap((container) => (
      Array.from(container.querySelectorAll("[role='option'], [data-option], .select2-results__option, [role='menuitemradio'], [data-automation-id='promptOption']"))
    ));

    return optionNodes
      .filter(isVisibleElement)
      .map((option) => ({
        label: compactText(option.getAttribute("data-automation-label") || option.innerText || option.textContent || option.getAttribute("aria-label") || ""),
        value: compactText(option.getAttribute("data-automation-label") || option.getAttribute("data-value") || option.getAttribute("value") || option.getAttribute("aria-label") || option.innerText || option.textContent || "")
      }))
      .filter((option) => option.label || option.value);
  }

  function closeDynamicDropdown(element) {
    dispatchEnterOrEscape(element, "Escape");
    element.blur?.();
  }

  function uniqueOptions(options) {
    const seen = new Set();
    const result = [];

    for (const option of options) {
      const key = normalize([option.label, option.value].join(" "));
      if (!key || seen.has(key)) {
        continue;
      }

      seen.add(key);
      result.push(option);
    }

    return result;
  }

  function hydrateFieldsFromPreview(fields) {
    const previewFields = Array.isArray(state.lastPreviewFields) ? state.lastPreviewFields : [];

    for (const field of fields) {
      if (field.options?.length) {
        continue;
      }

      const match = previewFields.find((item) => (
        item.index === field.index
        || sameFieldIdentity(item, field)
      ));

      if (match?.options?.length) {
        field.options = match.options;
      }
    }
  }

  function sameFieldIdentity(left, right) {
    const leftKey = normalize([
      left?.label,
      left?.name,
      left?.id,
      left?.ariaLabel
    ].join(" "));
    const rightKey = normalize([
      right?.label,
      right?.name,
      right?.id,
      right?.ariaLabel
    ].join(" "));

    return Boolean(leftKey && rightKey && leftKey === rightKey);
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

    if (isPhoneCountryCodeField(primaryHaystack)) {
      return buildMapping(field, profile.answers?.phoneCountryCode || profile.phoneCountryCode || "Canada (+1)", "rule", 0.88);
    }

    if (isPhoneExtensionField(primaryHaystack)) {
      return hasValue(profile.phoneExtension)
        ? buildMapping(field, profile.phoneExtension, "rule", 0.9)
        : null;
    }

    if (isPhoneDeviceTypeField(primaryHaystack)) {
      return buildMapping(field, profile.answers?.phoneDeviceType || profile.phoneDeviceType || "Mobile", "rule", 0.86);
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

    if (/(may we contact|contact).*(current employer)/.test(haystack)) {
      return buildMapping(field, profile.answers?.contactCurrentEmployer || "No", "rule", 0.9);
    }

    if (isCompanyHistoryQuestion(haystack)) {
      return null;
    }

    const companyQuestionMapping = mapCompanyQuestion(field, profile, haystack);
    if (companyQuestionMapping) {
      return companyQuestionMapping;
    }

    const savedAnswer = findSavedAnswer(field, profile);
    if (hasValue(savedAnswer)) {
      return buildMapping(field, savedAnswer, "saved-answer", 0.95);
    }

    if (/(essential functions|reasonable accommodation)/.test(primaryHaystack) && !/describe|need for|documentation/.test(primaryHaystack)) {
      return buildMapping(field, profile.answers?.canPerformEssentialFunctions || "Yes", "rule", 0.84);
    }

    if (/(commutable proximity|lyft office|open to relocating|open to relocate|san francisco)/.test(primaryHaystack)) {
      return buildMapping(field, profile.relocation || profile.answers?.relocation || "Open to relocation", "rule", 0.84);
    }

    const knownCustomMapping = mapKnownCustomQuestion(field, profile, primaryHaystack);
    if (knownCustomMapping) {
      return knownCustomMapping;
    }

    const workHistoryMapping = mapWorkHistoryField(field, profile, primaryHaystack);
    if (workHistoryMapping) {
      return workHistoryMapping;
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
      [/(\bfull\b.*\bname\b|\blegal name\b|\bname as it appears\b|^name$|first and last name)/, profile.fullName],
      [/(\bfirst\b.*\bname\b|\bgiven\b.*\bname\b|fname)/, profile.firstName],
      [/(\blast\b.*\bname\b|\bfamily\b.*\bname\b|lname|surname)/, profile.lastName],
      [/(email|e-mail)/, profile.email],
      [/(phone|mobile|cell|telephone)/, profile.phone],
      [/(linkedin profile|linkedin url|linked in profile|linked in url|^linkedin$)/, profile.linkedin],
      [/(github profile|github url|git hub profile|git hub url)/, profile.github],
      [/(portfolio|personal website|website url|personal site)/, profile.portfolio],
      [/(school|university|college|institution)/, profile.school],
      [/(degree|program|major)/, profile.degree],
      [/(graduation|grad date|expected completion)/, profile.graduationDate],
      [/(salary|compensation|pay expectation)/, profile.salary || profile.answers?.salary],
      [/(relocat)/, profile.relocation || profile.answers?.relocation],
      [/(pronouns?|address you correctly)/, profile.answers?.pronouns],
      [/(electronic signature|full name.*today|signature.*date)/, signatureValue(profile)],
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

    if (/(within|located).*(50 miles|seattle|boston|washington dc|austin)/.test(haystack)) {
      return buildMapping(field, profile.answers?.withinListedOfficeRadius || "No", "rule", 0.86);
    }

    if (/(review.*linked document|candidate privacy policy|privacy policy|linked document)/.test(haystack)) {
      return buildMapping(field, profile.answers?.reviewedPrivacyPolicy || "Yes", "rule", 0.82);
    }

    if (/(relevant employment|military service).*(above|add another|employment link)/.test(haystack)) {
      return buildMapping(field, profile.answers?.enteredRelevantEmployment || "Yes", "rule", 0.86);
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

    if (/(authorized|eligible|legally|authorization).*(work|employment)|work authorization|proof of authorization/.test(haystack)) {
      return buildMapping(
        field,
        profile.workAuthorization || profile.answers?.workAuthorization || "Yes",
        "rule",
        0.88
      );
    }

    if (settings.autoFillSensitiveFields === true) {
      if (/(disability status|have a disability|had one in the past|self-identification of disability)/.test(primaryHaystack)) {
        const disabilityAnswer = profile.answers?.disabilityStatus || profile.disabilityStatus;
        if (hasValue(disabilityAnswer)) {
          return buildMapping(field, disabilityAnswer, "sensitive-rule", 0.82);
        }
      }

      const demographicMapping = mapSensitiveField(field, profile, primaryHaystack);
      if (demographicMapping) {
        return demographicMapping;
      }

      const voluntaryFallback = mapVoluntarySensitiveFallback(field, primaryHaystack);
      if (voluntaryFallback) {
        return voluntaryFallback;
      }
    }

    return null;
  }

  function isPhoneCountryCodeField(haystack) {
    return /country.*phone.*code|phone.*country.*code|country code/.test(haystack);
  }

  function isPhoneExtensionField(haystack) {
    return /phone.*extension|extension/.test(haystack);
  }

  function isPhoneDeviceTypeField(haystack) {
    return /phone.*device.*type|device.*type.*phone/.test(haystack);
  }

  function findSavedAnswer(field, profile) {
    const haystack = normalize([
      field.label,
      field.placeholder,
      field.name,
      field.id,
      field.ariaLabel,
      field.surroundingText
    ].join(" "));

    if (isLowInformationChoiceLabel(field) || isCompanyHistoryQuestion(haystack)) {
      return "";
    }

    const answers = profile.answers || {};
    const keys = [
      answerKeyForField(field),
      normalize(field.label),
      normalize(field.name),
      normalize(field.id)
    ].filter(Boolean);

    for (const key of keys) {
      if (hasValue(answers[key])) {
        if (field.options?.length && !bestOptionValue(field, answers[key])) {
          continue;
        }

        return answers[key];
      }
    }

    return "";
  }

  function isLowInformationChoiceLabel(field) {
    return isLowInformationText(field.label || "");
  }

  function isLowInformationText(value) {
    const label = normalize(value || "");
    return /^(yes\s*no|no\s*yes|select one|required|true false|false true)$/.test(label);
  }

  function shouldAskForField(field) {
    const haystack = normalize([
      field.label,
      field.placeholder,
      field.name,
      field.id,
      field.ariaLabel,
      field.surroundingText
    ].join(" "));

    if (!haystack || shouldSkipField(haystack)) {
      return false;
    }

    if (isPhoneExtensionField(haystack)) {
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

  function displayLabelForField(field) {
    const label = field.label || "";

    if (isLowInformationText(label) && field.surroundingText) {
      const fallback = firstMeaningfulQuestion(field.surroundingText);
      if (fallback) {
        return cleanDisplayLabel(fallback);
      }
    }

    return cleanDisplayLabel(label || field.name || field.id || `Field ${field.index + 1}`);
  }

  function cleanDisplayLabel(value) {
    return compactText(String(value || "")
      .replace(/\*?\s*(yes\s*no|no\s*yes)\s*$/i, "")
      .replace(/\s+required\s+/i, " "));
  }

  function firstMeaningfulQuestion(text) {
    return String(text || "")
      .split(/\n+/)
      .map((line) => compactText(line))
      .find((line) => (
        line.length > 1
        && !isLowInformationText(line)
        && /[?]|\b(now|previously|ever|worked|employed|subsidiar|affiliate|authorize|sponsor|require)\b/i.test(line)
      )) || "";
  }

  function answerKeyForField(field) {
    const basis = displayLabelForField(field) || field.name || field.id || field.placeholder || `field-${field.index}`;
    return `custom:${normalize(basis).replace(/\s+/g, "-").slice(0, 80)}`;
  }

  function mapWorkQuestion(field, profile, haystack) {
    if (/(sponsor|visa|h-?1b|work permit)/.test(haystack)) {
      return buildMapping(field, profile.needsSponsorship || profile.answers?.sponsorship || "No", "rule", 0.9);
    }

    if (/(authorized|eligible|legally|authorization).*(work|employment)|work authorization|proof of authorization/.test(haystack)) {
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

  function mapKnownCustomQuestion(field, profile, haystack) {
    if (/(ai projects?|machine learning projects?|built).*?(spare time|outside of work|personal)/.test(haystack)) {
      const saved = profile.answers?.aiProjectsOutsideWork;
      const generated = summarizeProjectFacts(profile);
      return hasValue(saved || generated)
        ? buildMapping(field, saved || generated, "profile-summary", 0.82)
        : null;
    }

    return null;
  }

  function mapCompanyQuestion(field, profile, haystack) {
    if (/(whatsapp|sms|text messages?|messaging).*(recruit|hiring)|recruit.*(whatsapp|sms|text messages?|messaging)/.test(haystack)) {
      return buildMapping(field, profile.answers?.recruitingMessages || "No", "rule", 0.9);
    }

    return null;
  }

  function isCompanyHistoryQuestion(haystack) {
    return (
      /(now|ever|previously|formerly|current).*(employed|worked|work).*(affiliate|subsidiar(?:y|ies)|\bby\b|\bfor\b)/.test(haystack)
      || /(employed|worked|work).*(affiliate|subsidiar(?:y|ies)|\bby\b|\bfor\b).*(now|ever|previously|formerly|current)/.test(haystack)
    );
  }

  function mapWorkHistoryField(field, profile, haystack) {
    if (/(current|previous|most recent).*(employer|company)|((employer|company).*(current|previous|most recent))/.test(haystack)) {
      const employer = profile.currentOrPreviousEmployer
        || profile.currentEmployer
        || profile.previousEmployer
        || profile.answers?.currentOrPreviousEmployer
        || profile.answers?.currentEmployer
        || profile.answers?.previousEmployer
        || firstResumeExperienceValue(profile, ["company", "employer", "organization"]);

      return hasValue(employer) ? buildMapping(field, employer, "rule", 0.87) : null;
    }

    if (/(current|previous|most recent).*(job title|title|position|role)|((job title|title|position|role).*(current|previous|most recent))/.test(haystack)) {
      const title = profile.currentOrPreviousJobTitle
        || profile.currentJobTitle
        || profile.previousJobTitle
        || profile.answers?.currentOrPreviousJobTitle
        || profile.answers?.currentJobTitle
        || profile.answers?.previousJobTitle
        || firstResumeExperienceValue(profile, ["title", "role", "position"]);

      return hasValue(title) ? buildMapping(field, title, "rule", 0.87) : null;
    }

    return null;
  }

  function mapLocationField(field, address, haystack) {
    if (!address) {
      return null;
    }

    if (isEmploymentField(field, haystack)) {
      return null;
    }

    if (/(work remotely|remote location|plan to work remote)/.test(haystack)) {
      return null;
    }

    if (/(location city|city location|current city|where.*city)/.test(haystack)) {
      return hasValue(address.city) ? buildMapping(field, address.city, "rule", 0.9) : null;
    }

    if (/(location|required.*city.*(state|region|country)|city.*(state|region).*(country))/.test(haystack)) {
      const region = address.state || address.province || "";
      const location = [address.city, region, address.country].filter(Boolean).join(", ");
      return hasValue(location) ? buildMapping(field, location, "rule", 0.9) : null;
    }

    if (/(currently reside|current residence|country.*reside|country region|country\/region|\bcountry\b)/.test(haystack)) {
      return hasValue(address.country) ? buildMapping(field, address.country, "rule", 0.9) : null;
    }

    return null;
  }

  function shouldSkipField(haystack) {
    if (/(current|previous|most recent).*(employer|company|job title|title|position|role)/.test(haystack)) {
      return false;
    }

    return [
      /^english$/,
      /^settings$/,
      /^[a-z]{2,}\d{2,}$/,
      /^search for jobs$/,
      /^candidate home$/,
      /^job alerts$/,
      /\bi have a preferred name\b/,
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
    if (isEmploymentField(field, haystack)) {
      return null;
    }

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
    if (settings?.targetCountry === "usa") {
      return profile.addresses?.usa || profile.addresses?.canada || null;
    }

    if (settings?.targetCountry === "canada") {
      return profile.addresses?.canada || profile.addresses?.usa || null;
    }

    if (settings?.targetCountry === "usa" && /(zip code|\bzip\b|postal code|postcode)/.test(haystack)) {
      return profile.addresses?.usa || profile.addresses?.canada || null;
    }

    if (settings?.targetCountry === "canada" && /(postal code|postcode)/.test(haystack) && !/(zip code|\bzip\b)/.test(haystack)) {
      return profile.addresses?.canada || profile.addresses?.usa || null;
    }

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

    if (/transgender/.test(haystack)) {
      const answer = /cisgender/.test(normalize(demographics.genderIdentity || "")) ? "No" : demographics.transgender;
      return hasValue(answer) ? buildMapping(field, answer, "sensitive-rule", 0.82) : null;
    }

    const rules = [
      [/(hispanic|latino|latina|latinx)/, demographics.hispanicLatino],
      [/(race|racial)/, demographics.race],
      [/(ethnic|ethnicity)/, demographics.ethnicity || demographics.race],
      [/(gender identity|cisgender)/, bestOptionValue(field, demographics.genderIdentity || demographics.gender) || demographics.gender || demographics.genderIdentity],
      [/\bgender\b/, demographics.gender || demographics.genderIdentity]
    ];

    for (const [pattern, value] of rules) {
      if (pattern.test(haystack) && hasValue(value)) {
        return buildMapping(field, value, "sensitive-rule", 0.82);
      }
    }

    return null;
  }

  function mapVoluntarySensitiveFallback(field, haystack) {
    const options = field.options || [];

    if (!options.length) {
      return null;
    }

    if (/(age|sexual orientation|disability|communities|transgender)/.test(haystack)) {
      const preferred = options.find((option) => /prefer not|do not want|decline/i.test(option.label || option.value || ""));
      return preferred
        ? buildMapping(field, preferred.label || preferred.value, "voluntary-fallback", 0.7)
        : null;
    }

    return null;
  }

  async function prepareRepeatableSections(profile) {
    const experiences = normalizedWorkExperience(profile);
    const education = normalizedEducation(profile);
    const links = normalizedProfileLinks(profile);

    if (experiences.length > 1) {
      await ensureEmploymentRows(experiences.length);
    }

    if (education.length) {
      await ensureEducationRows(education.length);
    }

    if (links.length) {
      await ensureWebsiteRows(links.length);
    }
  }

  async function ensureEmploymentRows(targetCount) {
    const maxClicks = Math.min(Math.max(targetCount - countEmploymentRows(), 0), 12);

    for (let index = 0; index < maxClicks; index += 1) {
      const button = findAddAnotherEmploymentButton();
      if (!button) {
        return;
      }

      button.click();
      await sleep(250);

      if (countEmploymentRows() >= targetCount) {
        return;
      }
    }
  }

  function findAddAnotherEmploymentButton() {
    const candidates = Array.from(document.querySelectorAll("button, a, [role='button'], input[type='button']"));
    return candidates.find((item) => {
      const text = normalize(item.innerText || item.textContent || item.value || item.getAttribute("aria-label") || "");
      return /add another/.test(text) && /(employment|experience|work|job)/.test(employmentSectionText(item));
    }) || candidates.find((item) => {
      const text = normalize(item.innerText || item.textContent || item.value || item.getAttribute("aria-label") || "");
      return /^add another$/.test(text);
    });
  }

  function countEmploymentRows() {
    return Math.max(
      countFieldsMatching(/\bcompany\b|\bcompany name\b/),
      countFieldsMatching(/(^|\b)title\b|job title/),
      countFieldsMatching(/start date.*year|^from\b|\bfrom\b/)
    );
  }

  async function ensureEducationRows(targetCount) {
    await ensureRowsForSection(targetCount, countEducationRows, () => findAddButtonForSection(/education|school|university/));
  }

  async function ensureWebsiteRows(targetCount) {
    await ensureRowsForSection(targetCount, countWebsiteRows, () => findAddButtonForSection(/websites?|urls?|links?/));
  }

  async function ensureRowsForSection(targetCount, countRows, findButton) {
    const maxClicks = Math.min(Math.max(targetCount - countRows(), 0), 12);

    for (let index = 0; index < maxClicks; index += 1) {
      const button = findButton();
      if (!button) {
        return;
      }

      button.click();
      await sleep(250);

      if (countRows() >= targetCount) {
        return;
      }
    }
  }

  function findAddButtonForSection(sectionPattern) {
    const candidates = Array.from(document.querySelectorAll("button, a, [role='button'], input[type='button']"));
    return candidates.find((item) => {
      const text = normalize(item.innerText || item.textContent || item.value || item.getAttribute("aria-label") || "");
      return /^(add|add another)$/.test(text) && sectionPattern.test(sectionTextAround(item));
    });
  }

  function countEducationRows() {
    return countFieldsMatchingInSection(/school|university/, isEducationField);
  }

  function countWebsiteRows() {
    return countFieldsMatchingInSection(/\burl\b|website|link/, isWebsiteField);
  }

  function countFieldsMatchingInSection(pattern, predicate) {
    return scanFieldsWithoutPreparation()
      .filter((field) => {
        const haystack = normalize([field.label, field.name, field.id, field.placeholder, field.surroundingText].join(" "));
        return pattern.test(haystack) && predicate(field, haystack);
      })
      .length;
  }

  function countFieldsMatching(pattern) {
    return scanFieldsWithoutPreparation()
      .filter((field) => {
        const haystack = normalize([field.label, field.name, field.id, field.placeholder].join(" "));
        return pattern.test(haystack) && isEmploymentField(field, haystack);
      })
      .length;
  }

  function scanFieldsWithoutPreparation() {
    const elements = Array.from(document.querySelectorAll(FIELD_SELECTOR)).filter(isFillable);
    const choiceGroups = buildChoiceGroups(elements);
    const groupedElements = new Set(choiceGroups.flatMap((group) => group.elements));
    const fields = [];
    let index = 0;

    for (const group of choiceGroups) {
      fields.push(buildChoiceGroupMetadata(group, index));
      index += 1;
    }

    for (const element of elements) {
      if (groupedElements.has(element)) {
        continue;
      }

      fields.push(buildFieldMetadata(element, index));
      index += 1;
    }

    return fields;
  }

  function mapRepeatableEmploymentFields(fields, profile) {
    const experiences = normalizedWorkExperience(profile);

    if (!experiences.length) {
      return [];
    }

    const buckets = {
      company: [],
      title: [],
      location: [],
      startDate: [],
      endDate: [],
      startMonth: [],
      startYear: [],
      endMonth: [],
      endYear: [],
      currentRole: [],
      description: []
    };

    for (const field of fields) {
      const haystack = normalize([field.label, field.name, field.id, field.placeholder].join(" "));

      if (!isEmploymentField(field, haystack)) {
        continue;
      }

      if (/^company\b|\bcompany name\b|\bemployer\b/.test(haystack) && !/website|url/.test(haystack)) {
        buckets.company.push(field);
      } else if (/(^|\b)title\b|job title/.test(haystack) || (/\bposition\b/.test(haystack) && /employment|experience|work history/.test(normalize(field.surroundingText)))) {
        buckets.title.push(field);
      } else if (/\blocation\b/.test(haystack)) {
        buckets.location.push(field);
      } else if (/^from\b|from date|date from/.test(haystack)) {
        buckets.startDate.push(field);
      } else if (/^to\b|to date|date to/.test(haystack)) {
        buckets.endDate.push(field);
      } else if (/start date.*month|start month/.test(haystack)) {
        buckets.startMonth.push(field);
      } else if (/start date.*year|start year/.test(haystack)) {
        buckets.startYear.push(field);
      } else if (/end date.*month|end month/.test(haystack)) {
        buckets.endMonth.push(field);
      } else if (/end date.*year|end year/.test(haystack)) {
        buckets.endYear.push(field);
      } else if (/current role|currently work|current position/.test(haystack)) {
        buckets.currentRole.push(field);
      } else if (/role description|description|responsibilities|achievements/.test(haystack)) {
        buckets.description.push(field);
      }
    }

    const mappings = [];
    experiences.forEach((experience, index) => {
      addRepeatableMapping(mappings, buckets.company[index], experience.company);
      addRepeatableMapping(mappings, buckets.title[index], experience.title);
      addRepeatableMapping(mappings, buckets.location[index], experience.location);
      addRepeatableMapping(mappings, buckets.startDate[index], experienceDateValue(experience, "start"));
      addRepeatableMapping(mappings, buckets.endDate[index], experience.currentRole ? "" : experienceDateValue(experience, "end"));
      addRepeatableMapping(mappings, buckets.startMonth[index], experience.startMonth);
      addRepeatableMapping(mappings, buckets.startYear[index], experience.startYear);
      addRepeatableMapping(mappings, buckets.endMonth[index], experience.currentRole ? "" : experience.endMonth);
      addRepeatableMapping(mappings, buckets.endYear[index], experience.currentRole ? "" : experience.endYear);
      addRepeatableMapping(mappings, buckets.currentRole[index], experience.currentRole ? true : false);
      addRepeatableMapping(mappings, buckets.description[index], experience.description);
    });

    return mappings;
  }

  function mapRepeatableEducationFields(fields, profile) {
    const education = normalizedEducation(profile);

    if (!education.length) {
      return [];
    }

    const buckets = {
      school: [],
      degree: [],
      field: [],
      startYear: [],
      endYear: []
    };

    for (const field of fields) {
      const primaryHaystack = normalize([field.label, field.name, field.id, field.placeholder].join(" "));
      const haystack = normalize([field.label, field.name, field.id, field.placeholder, field.surroundingText].join(" "));

      if (!isEducationField(field, haystack)) {
        continue;
      }

      if (/school|university|institution/.test(primaryHaystack)) {
        buckets.school.push(field);
      } else if (/degree|qualification/.test(primaryHaystack)) {
        buckets.degree.push(field);
      } else if (/field of study|discipline|major|program/.test(primaryHaystack)) {
        buckets.field.push(field);
      } else if (/^from\b|start|begin/.test(primaryHaystack)) {
        buckets.startYear.push(field);
      } else if (/^to\b|actual or expected|expected|end|graduation/.test(primaryHaystack)) {
        buckets.endYear.push(field);
      }
    }

    const mappings = [];
    education.forEach((item, index) => {
      addRepeatableMapping(mappings, buckets.school[index], item.school);
      addRepeatableMapping(mappings, buckets.degree[index], item.degree);
      addRepeatableMapping(mappings, buckets.field[index], item.fieldOfStudy);
      addRepeatableMapping(mappings, buckets.startYear[index], item.startYear);
      addRepeatableMapping(mappings, buckets.endYear[index], item.endYear);
    });

    return mappings;
  }

  function mapRepeatableWebsiteFields(fields, profile) {
    const links = normalizedProfileLinks(profile);

    if (!links.length) {
      return [];
    }

    const urlFields = fields.filter((field) => {
      const haystack = normalize([field.label, field.name, field.id, field.placeholder, field.surroundingText].join(" "));
      return isWebsiteField(field, haystack);
    });

    const mappings = [];
    links.forEach((url, index) => {
      addRepeatableMapping(mappings, urlFields[index], url);
    });

    return mappings;
  }

  function addRepeatableMapping(mappings, field, value) {
    if (!field || !hasValue(value)) {
      return;
    }

    mappings.push(buildMapping(field, value, "experience", 0.92));
  }

  function isEmploymentField(field, haystack) {
    if (/(school|education|degree|discipline)/.test(haystack)) {
      return false;
    }

    if (/(may we contact|contact).*(current employer)/.test(haystack)) {
      return false;
    }

    if (/(current|previous|most recent).*(employer|company|job title|title)|((employer|company|job title|title).*(current|previous|most recent))/.test(haystack)) {
      return false;
    }

    if (/(this position is based|commutable|relocat|lyft office|san francisco)/.test(haystack)) {
      return false;
    }

    return /(employment|experience|work history|\bcompany\b|company name|employer|\btitle\b|job title|current role|currently work|start date|end date|position|\blocation\b|\bfrom\b|\bto\b|role description|responsibilities|achievements)/.test(
      `${haystack} ${normalize(field.surroundingText)}`
    );
  }

  function isEducationField(field, haystack) {
    if (/work experience|employment|company|job title|role description|resume|websites?|social network/.test(haystack)) {
      return false;
    }

    return /(education|school|university|degree|field of study|discipline|major|qualification|actual or expected)/.test(
      `${haystack} ${normalize(field.surroundingText)}`
    );
  }

  function isWebsiteField(field, haystack) {
    if (/social network|^linkedin$|facebook|twitter/.test(haystack)) {
      return false;
    }

    return /(websites?|urls?|links?|\burl\b)/.test(`${haystack} ${normalize(field.surroundingText)}`)
      && !/(linkedin profile|social network|^linkedin$)/.test(haystack);
  }

  function normalizedWorkExperience(profile) {
    const raw = profile.workExperience || profile.resumeFacts?.workExperience || [];
    const parsedFromResumeLines = parseExperienceLines(profile.resumeFacts?.experience || []);

    if (Array.isArray(raw) && raw.some((item) => item && typeof item === "object")) {
      return raw
        .filter((item) => item && typeof item === "object")
        .map((item, index) => mergeExperienceEntry(normalizeExperienceEntry(item), parsedFromResumeLines[index]))
        .filter((item) => hasValue(item.company) || hasValue(item.title));
    }

    return parsedFromResumeLines;
  }

  function normalizedEducation(profile) {
    const raw = Array.isArray(profile.education) ? profile.education : [];
    const parsedResumeEducation = parseResumeEducation(profile);
    const items = raw
      .map((item) => mergeEducationEntry(normalizeEducationEntry(item, profile), parsedResumeEducation))
      .filter((item) => hasValue(item.school) || hasValue(item.degree));

    if (items.length) {
      return items;
    }

    const resumeEducation = Array.isArray(profile.resumeFacts?.education) ? profile.resumeFacts.education : [];
    const school = compactText(profile.school || resumeEducation[0] || "");
    const fallback = normalizeEducationEntry({}, profile);
    fallback.school = fallback.school || school;
    const merged = mergeEducationEntry(fallback, parsedResumeEducation);

    return hasValue(merged.school) || hasValue(merged.degree) ? [merged] : [];
  }

  function normalizeEducationEntry(entry, profile) {
    const value = typeof entry === "string" ? { school: entry } : (entry || {});
    const graduationYear = yearFromValue(
      value.endYear
      || value.graduationYear
      || value.graduationDate
      || profile.graduationYear
      || profile.graduationDate
    );

    return {
      school: compactText(value.school || value.university || value.institution || profile.school || ""),
      degree: compactText(value.degree || value.qualification || profile.degree || ""),
      fieldOfStudy: compactText(value.fieldOfStudy || value.field || value.discipline || value.major || profile.fieldOfStudy || profile.discipline || profile.major || ""),
      startYear: yearFromValue(value.startYear || value.from || profile.educationStartYear || ""),
      endYear: graduationYear
    };
  }

  function mergeEducationEntry(entry, fallback) {
    if (!fallback) {
      return entry;
    }

    return {
      ...entry,
      school: entry.school || fallback.school || "",
      degree: entry.degree || fallback.degree || "",
      fieldOfStudy: entry.fieldOfStudy || fallback.fieldOfStudy || "",
      startYear: entry.startYear || fallback.startYear || "",
      endYear: entry.endYear || fallback.endYear || ""
    };
  }

  function parseResumeEducation(profile) {
    const lines = Array.isArray(profile.resumeFacts?.education) ? profile.resumeFacts.education : [];
    const joined = lines.join(" ");
    const degreeLine = lines.find((line) => /bachelor|master|phd|doctor|associate|diploma|certificate/i.test(line)) || "";
    const schoolLine = lines.find((line) => /university|college|school/i.test(line)) || "";
    const dateMatch = joined.match(/\b(19|20)\d{2}\b/g) || [];
    const degreeMatch = degreeLine.match(/\b(Bachelor|Master|Doctor|Associate|PhD)[^,|]*/i);
    const inMatch = degreeLine.match(/\bin\s+(.+?)(?:\s+[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}|$)/i);
    const parsedDegree = degreeMatch ? normalizeDegreeName(degreeMatch[0]) : "";

    return {
      school: compactText(profile.school || schoolLine.replace(/\s+(January|February|March|April|May|June|July|August|September|October|November|December)\b.*$/i, "")),
      degree: compactText(profile.degree || parsedDegree),
      fieldOfStudy: compactText(profile.fieldOfStudy || profile.major || profile.discipline || (inMatch ? inMatch[1].replace(/&/g, "and") : "")),
      startYear: yearFromValue(dateMatch[0] || profile.educationStartYear || ""),
      endYear: yearFromValue(dateMatch[1] || profile.graduationDate || profile.graduationYear || "")
    };
  }

  function normalizeDegreeName(value) {
    const text = normalize(value);

    if (/bachelor/.test(text)) {
      return "Bachelor's Degree";
    }

    if (/master/.test(text)) {
      return "Master's Degree";
    }

    if (/doctor|phd/.test(text)) {
      return "Doctorate";
    }

    if (/associate/.test(text)) {
      return "Associate Degree";
    }

    return compactText(value);
  }

  function normalizedProfileLinks(profile) {
    const links = [
      profile.linkedin,
      profile.portfolio,
      profile.github
    ];

    const projectLinks = profile.resumeFacts?.projectLinks;
    if (projectLinks && typeof projectLinks === "object") {
      Object.values(projectLinks).forEach((item) => {
        if (typeof item === "string") {
          links.push(item);
        } else if (item?.url) {
          links.push(item.url);
        }
      });
    }

    const explicitLinks = profile.links || profile.websites || profile.urls;
    if (Array.isArray(explicitLinks)) {
      explicitLinks.forEach((item) => {
        if (typeof item === "string") {
          links.push(item);
        } else if (item?.url) {
          links.push(item.url);
        }
      });
    }

    return unique(links)
      .filter((url) => /^https?:\/\//i.test(url))
      .slice(0, 12);
  }

  function yearFromValue(value) {
    const match = String(value || "").match(/\b(19|20)\d{2}\b/);
    return match ? match[0] : "";
  }

  function normalizeExperienceEntry(entry) {
    return {
      company: compactText(entry.company || entry.employer || entry.organization || ""),
      title: compactText(entry.title || entry.role || entry.position || ""),
      location: compactText(entry.location || entry.city || ""),
      description: normalizeDescription(entry.description || entry.summary || entry.responsibilities || entry.bullets || ""),
      startMonth: compactText(entry.startMonth || entry.start_month || ""),
      startYear: compactText(entry.startYear || entry.start_year || ""),
      endMonth: compactText(entry.endMonth || entry.end_month || ""),
      endYear: compactText(entry.endYear || entry.end_year || ""),
      currentRole: entry.currentRole === true || entry.current === true || /present|current/i.test(String(entry.endYear || entry.end || ""))
    };
  }

  function mergeExperienceEntry(entry, fallback = {}) {
    if (!fallback) {
      return entry;
    }

    return {
      ...entry,
      company: entry.company || fallback.company || "",
      title: entry.title || fallback.title || "",
      location: entry.location || fallback.location || "",
      description: entry.description || fallback.description || "",
      startMonth: entry.startMonth || fallback.startMonth || "",
      startYear: entry.startYear || fallback.startYear || "",
      endMonth: entry.endMonth || fallback.endMonth || "",
      endYear: entry.endYear || fallback.endYear || "",
      currentRole: entry.currentRole || fallback.currentRole || false
    };
  }

  function parseExperienceLines(lines) {
    if (!Array.isArray(lines)) {
      return [];
    }

    const datePattern = new RegExp(
      "^(.*?)\\s+(January|February|March|April|May|June|July|August|September|October|November|December)\\s+(\\d{4})\\s+(?:-|–|—|to)\\s+(?:(January|February|March|April|May|June|July|August|September|October|November|December)\\s+(\\d{4})|(Present|Current))$",
      "i"
    );
    const entries = [];

    lines.forEach((line, index) => {
      const match = String(line || "").match(datePattern);
      if (!match) {
        return;
      }

      const { title, location: experienceLocation } = splitTitleLocation(lines[index + 1] || "");
      const descriptionLines = [];

      for (let cursor = index + 2; cursor < lines.length; cursor += 1) {
        if (datePattern.test(String(lines[cursor] || ""))) {
          break;
        }

        descriptionLines.push(lines[cursor]);
      }

      entries.push({
        company: compactText(match[1]).replace(/\bW aterloo\b/g, "Waterloo"),
        title,
        location: experienceLocation,
        description: normalizeDescription(descriptionLines),
        startMonth: canonicalMonth(match[2]),
        startYear: match[3],
        endMonth: canonicalMonth(match[4] || ""),
        endYear: match[5] || "",
        currentRole: Boolean(match[6])
      });
    });

    return entries;
  }

  function splitTitleLocation(value) {
    const text = compactText(value);
    const match = text.match(/^(.*?\b(?:Engineer|Assistant|Scientist|Developer|Analyst|Intern|Manager|Architect|Consultant|Specialist)\b)\s+([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})$/)
      || text.match(/^(.+)\s+([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})$/);
    return {
      title: match ? compactText(match[1]) : text,
      location: match ? compactText(match[2]) : ""
    };
  }

  function canonicalMonth(value) {
    return value ? value.slice(0, 1).toUpperCase() + value.slice(1).toLowerCase() : "";
  }

  function monthNumber(value) {
    const months = {
      january: 1,
      february: 2,
      march: 3,
      april: 4,
      may: 5,
      june: 6,
      july: 7,
      august: 8,
      september: 9,
      october: 10,
      november: 11,
      december: 12
    };

    return months[normalize(value)] || "";
  }

  function experienceDateValue(experience, side) {
    const month = side === "start" ? experience.startMonth : experience.endMonth;
    const year = side === "start" ? experience.startYear : experience.endYear;
    const numericMonth = monthNumber(month);

    return numericMonth && year ? `${numericMonth}/${year}` : "";
  }

  async function fillWorkdayExperienceDateFallback(profile) {
    const experiences = normalizedWorkExperience(profile);
    if (!experiences.length || !/work experience/i.test(document.body?.innerText || "")) {
      return 0;
    }

    const fromLabels = workdayDateLabelNodes("from");
    const toLabels = workdayDateLabelNodes("to");
    const rows = workdayExperienceRows();
    let filled = 0;

    filled += fillWorkdayExperienceDateInputsByOrder(experiences);

    if (filled > 0) {
      return filled;
    }

    if (fromLabels.length || toLabels.length) {
      experiences.forEach((experience, index) => {
        const startDate = experienceDateValue(experience, "start");
        const endDate = experience.currentRole ? "" : experienceDateValue(experience, "end");

        if (startDate && fromLabels[index]) {
          filled += fillWorkdayDateFromLabel(fromLabels[index], startDate) ? 1 : 0;
        }

        if (endDate && toLabels[index]) {
          filled += fillWorkdayDateFromLabel(toLabels[index], endDate) ? 1 : 0;
        }
      });

      return filled;
    }

    rows.slice(0, experiences.length).forEach((row, index) => {
      const experience = experiences[index];
      const startDate = experienceDateValue(experience, "start");
      const endDate = experience.currentRole ? "" : experienceDateValue(experience, "end");

      if (startDate) {
        filled += fillWorkdayDateInRow(row, "from", startDate) ? 1 : 0;
      }

      if (endDate) {
        filled += fillWorkdayDateInRow(row, "to", endDate) ? 1 : 0;
      }
    });

    return filled;
  }

  function fillWorkdayExperienceDateInputsByOrder(experiences) {
    const controls = workdayExperienceDateInputs();
    if (controls.length < 4) {
      return 0;
    }

    let filled = 0;

    experiences.forEach((experience, index) => {
      const offset = index * 4;
      const startMonth = monthNumber(experience.startMonth);
      const startYear = experience.startYear;
      const endMonth = experience.currentRole ? "" : monthNumber(experience.endMonth);
      const endYear = experience.currentRole ? "" : experience.endYear;

      if (startMonth && startYear) {
        filled += fillDateControl(controls[offset], String(startMonth)) ? 1 : 0;
        filled += fillDateControl(controls[offset + 1], String(startYear)) ? 1 : 0;
      }

      if (endMonth && endYear) {
        filled += fillDateControl(controls[offset + 2], String(endMonth)) ? 1 : 0;
        filled += fillDateControl(controls[offset + 3], String(endYear)) ? 1 : 0;
      }
    });

    return filled;
  }

  function workdayExperienceDateInputs() {
    const range = workdaySectionRange("work experience", "education");
    const selector = [
      "input[data-automation-id*='dateSectionMonth']",
      "input[data-automation-id*='dateSectionYear']",
      "input[aria-label*='Month' i]",
      "input[aria-label*='Year' i]",
      "input[placeholder='MM']",
      "input[placeholder='YYYY']"
    ].join(",");

    return Array.from(document.querySelectorAll(selector))
      .filter(isFillable)
      .filter((element) => elementInRange(element, range))
      .filter((element) => {
        const haystack = normalize([
          element.getAttribute("data-automation-id"),
          element.getAttribute("aria-label"),
          element.placeholder,
          getLabelText(element),
          getSurroundingText(element)
        ].join(" "));

        return /(month|year|mm|yyyy|date section)/.test(haystack)
          && !/(education|school|degree|field of study|websites?|social network)/.test(haystack);
      })
      .sort((left, right) => documentOrder(left, right));
  }

  function workdaySectionRange(startText, endText) {
    const start = firstVisibleTextElement(startText);
    const end = Array.from(document.querySelectorAll("h1, h2, h3, h4, div, span"))
      .filter(isVisibleElement)
      .find((element) => (
        normalize(compactText(element.innerText || element.textContent || "")) === normalize(endText)
        && (!start || followsNode(start, element))
      )) || null;

    return { start, end };
  }

  function firstVisibleTextElement(text) {
    return Array.from(document.querySelectorAll("h1, h2, h3, h4, div, span"))
      .filter(isVisibleElement)
      .find((element) => normalize(compactText(element.innerText || element.textContent || "")) === normalize(text)) || null;
  }

  function elementInRange(element, range) {
    if (range.start && !followsNode(range.start, element)) {
      return false;
    }

    if (range.end && !followsNode(element, range.end)) {
      return false;
    }

    return true;
  }

  function documentOrder(left, right) {
    if (left === right) {
      return 0;
    }

    return followsNode(left, right) ? -1 : 1;
  }

  function workdayDateLabelNodes(label) {
    const pattern = label === "from" ? /^from\s*\*?$/i : /^to\s*\*?$/i;
    return Array.from(document.querySelectorAll("label, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel'], div, span"))
      .filter(isVisibleElement)
      .filter((element) => pattern.test(compactText(element.innerText || element.textContent || "")))
      .filter((element) => isLeafTextNode(element, pattern))
      .filter((element) => /work experience/i.test(sectionTextForElement(element)))
      .sort((left, right) => topOfElement(left) - topOfElement(right));
  }

  function isLeafTextNode(element, pattern) {
    return !Array.from(element.children || []).some((child) => (
      isVisibleElement(child)
      && pattern.test(compactText(child.innerText || child.textContent || ""))
    ));
  }

  function sectionTextForElement(element) {
    return element.closest("[data-automation-id*='workExperience'], [data-automation-id*='experience'], section, fieldset, form, main, body")?.innerText || "";
  }

  function topOfElement(element) {
    return element.getBoundingClientRect().top + window.scrollY;
  }

  function fillWorkdayDateFromLabel(labelNode, desiredDate) {
    const container = closestDateContainer(labelNode, document.body);
    const orderedControls = dateControlsAfterLabel(labelNode);
    const controls = orderedControls.length
      ? orderedControls
      : dateControlsNear(container, container || document.body);

    if (!controls.length) {
      return false;
    }

    const [month, year] = desiredDate.split("/");

    if (controls.length >= 2 && month && year) {
      const changedMonth = fillDateControl(controls[0], month);
      const changedYear = fillDateControl(controls[1], year);
      return changedMonth || changedYear;
    }

    return fillDateControl(controls[0], desiredDate);
  }

  function dateControlsAfterLabel(labelNode) {
    const allControls = Array.from(document.querySelectorAll("input:not([type='hidden']), [role='textbox'], [contenteditable='true']"))
      .filter(isFillable)
      .filter((element) => !/button|checkbox|radio|file/i.test(element.getAttribute("type") || ""));
    const nextLabel = nextWorkdayDateLabel(labelNode);

    return allControls
      .filter((element) => followsNode(labelNode, element))
      .filter((element) => !nextLabel || followsNode(element, nextLabel))
      .filter((element) => {
        const text = normalize([getLabelText(element), getSurroundingText(element), element.placeholder, element.getAttribute("aria-label")].join(" "));
        return !/(company|job title|location|role description|school|degree|url|linkedin|facebook|twitter)/.test(text);
      })
      .slice(0, 2);
  }

  function nextWorkdayDateLabel(labelNode) {
    return Array.from(document.querySelectorAll("label, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel'], div, span"))
      .filter(isVisibleElement)
      .filter((element) => element !== labelNode)
      .filter((element) => /^(from|to)\s*\*?$/i.test(compactText(element.innerText || element.textContent || "")))
      .filter((element) => followsNode(labelNode, element))[0] || null;
  }

  function followsNode(left, right) {
    return Boolean(left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING);
  }

  function workdayExperienceRows() {
    const headings = Array.from(document.querySelectorAll("h2, h3, h4, div, span"))
      .filter(isVisibleElement)
      .filter((element) => /^work experience\s+\d+$/i.test(compactText(element.innerText || element.textContent || "")));
    const rows = [];

    for (const heading of headings) {
      const row = closestUsefulRow(heading);
      if (row && !rows.includes(row)) {
        rows.push(row);
      }
    }

    if (rows.length) {
      return rows;
    }

    return Array.from(document.querySelectorAll(".employment-row, [data-automation-id*='workExperience'], [data-automation-id*='experience']"))
      .filter(isVisibleElement);
  }

  function closestUsefulRow(element) {
    let current = element.parentElement;
    let best = current;

    for (let depth = 0; current && current !== document.body && depth < 8; depth += 1) {
      const text = normalize(current.innerText || current.textContent || "");
      const controlCount = current.querySelectorAll("input, textarea, [role='textbox'], [contenteditable='true']").length;

      if (controlCount >= 4 && /job title|company|location|from|to|role description/.test(text)) {
        best = current;
        break;
      }

      current = current.parentElement;
    }

    return best;
  }

  function fillWorkdayDateInRow(row, label, desiredDate) {
    const labelNode = findDateLabelInRow(row, label);
    if (!labelNode) {
      return false;
    }

    const container = closestDateContainer(labelNode, row);
    const controls = dateControlsNear(container, row);
    if (!controls.length) {
      return false;
    }

    const [month, year] = desiredDate.split("/");

    if (controls.length >= 2 && month && year) {
      const changedMonth = fillDateControl(controls[0], month);
      const changedYear = fillDateControl(controls[1], year);
      return changedMonth || changedYear;
    }

    return fillDateControl(controls[0], desiredDate);
  }

  function findDateLabelInRow(row, label) {
    const pattern = label === "from" ? /^from\b/i : /^to\b/i;
    return Array.from(row.querySelectorAll("label, div, span"))
      .filter(isVisibleElement)
      .filter((element) => pattern.test(compactText(element.innerText || element.textContent || "")))
      .sort((left, right) => (left.contains(right) ? 1 : right.contains(left) ? -1 : 0))[0] || null;
  }

  function closestDateContainer(labelNode, row) {
    let current = labelNode;

    for (let depth = 0; current && current !== row && depth < 5; depth += 1) {
      const controls = current.querySelectorAll("input:not([type='hidden']), [role='textbox'], [contenteditable='true']");
      if (controls.length) {
        return current;
      }

      current = current.parentElement;
    }

    return labelNode.parentElement || row;
  }

  function dateControlsNear(container, row) {
    const controls = Array.from(container.querySelectorAll("input:not([type='hidden']), [role='textbox'], [contenteditable='true']"))
      .filter(isFillable)
      .filter((element) => !/button|checkbox|radio|file/i.test(element.getAttribute("type") || ""));

    if (controls.length) {
      return controls;
    }

    const allControls = Array.from(row.querySelectorAll("input:not([type='hidden']), [role='textbox'], [contenteditable='true']"))
      .filter(isFillable)
      .filter((element) => !/button|checkbox|radio|file/i.test(element.getAttribute("type") || ""));
    const rowText = normalize(container.innerText || container.textContent || "");
    const dateLike = allControls.filter((element) => {
      const text = normalize([getLabelText(element), getSurroundingText(element), element.placeholder].join(" "));
      return /current value|mm yyyy|from|to|date/.test(text) || /from|to/.test(rowText);
    });

    return dateLike.slice(0, 2);
  }

  function fillDateControl(element, desiredValue) {
    if (!element || valueMatches(getCurrentValue(element), desiredValue)) {
      return false;
    }

    if (element.isContentEditable || element.getAttribute("role") === "textbox") {
      setEditableText(element, desiredValue);
    } else {
      setNativeValue(element, String(desiredValue));
    }

    dispatchFormEvents(element);
    confirmFilledElement(element);
    return true;
  }

  function normalizeDescription(value) {
    const lines = Array.isArray(value) ? value : String(value || "").split(/\n+/);
    return lines
      .map((line) => compactText(line))
      .filter(Boolean)
      .join("\n");
  }

  function employmentSectionText(element) {
    return normalize(element.closest("section, fieldset, form, div")?.innerText || "");
  }

  function sectionTextAround(element) {
    let current = element;

    for (let depth = 0; current && current !== document.body && depth < 7; depth += 1) {
      const text = normalize(current.innerText || current.textContent || "");
      if (text && !/^(add|add another)$/.test(text)) {
        return text;
      }
      current = current.parentElement;
    }

    return "";
  }

  function signatureValue(profile) {
    const date = new Date();
    const formatted = `${date.getMonth() + 1}/${date.getDate()}/${date.getFullYear()}`;
    return [profile.fullName, formatted].filter(Boolean).join(" ");
  }

  function bestOptionValue(field, value) {
    if (!hasValue(value) || !field.options?.length) {
      return "";
    }

    const match = field.options.find((option) => optionMatches(option.label, option.value, value));
    return match ? (match.label || match.value) : "";
  }

  function summarizeProjectFacts(profile) {
    const facts = profile.resumeFacts || {};
    const projects = Array.isArray(facts.projects) ? facts.projects.slice(0, 3) : [];
    const projectLinks = facts.projectLinks && typeof facts.projectLinks === "object"
      ? Object.values(facts.projectLinks).slice(0, 3).map((item) => [item.name, item.url].filter(Boolean).join(": "))
      : [];
    const skills = Array.isArray(facts.skills) ? facts.skills.slice(0, 12).join(", ") : "";
    const items = [...projects, ...projectLinks].filter(Boolean);

    if (!items.length && !skills) {
      return "";
    }

    const projectText = items.length ? `In my spare time, I have built AI projects including ${items.join("; ")}.` : "I have built AI and machine learning projects outside of work.";
    const skillsText = skills ? ` These projects use skills such as ${skills}.` : "";
    return `${projectText}${skillsText}`.slice(0, 700);
  }

  function firstResumeExperienceValue(profile, preferredKeys) {
    const experience = profile.resumeFacts?.experience;
    if (!Array.isArray(experience)) {
      return "";
    }

    const first = experience.find((item) => item && (typeof item === "object" || typeof item === "string"));

    if (!first) {
      return "";
    }

    if (typeof first === "string") {
      return "";
    }

    for (const key of preferredKeys) {
      if (hasValue(first[key])) {
        return first[key];
      }
    }

    const values = Object.values(first).filter((value) => typeof value === "string" && value.trim());
    return values.length === 1 ? values[0] : "";
  }

  function isCheckboxField(field) {
    return field.type === "checkbox";
  }

  function buildMapping(field, value, source, confidence) {
    return {
      index: field.index,
      value: normalizedMappingValue(field, value),
      source,
      confidence
    };
  }

  function normalizedMappingValue(field, value) {
    const exactOption = bestOptionValue(field, value);
    return exactOption || value;
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

  function mergeMappings(localMappings, backendMappings, fields = []) {
    const byIndex = new Map();

    for (const mapping of localMappings) {
      byIndex.set(mapping.index, normalizeMappingForField(mapping, fields));
    }

    for (const mapping of backendMappings) {
      const existing = byIndex.get(mapping.index);
      if (!existing || Number(mapping.confidence || 0) >= Number(existing.confidence || 0)) {
        byIndex.set(mapping.index, normalizeMappingForField(mapping, fields));
      }
    }

    return Array.from(byIndex.values());
  }

  function normalizeMappingForField(mapping, fields) {
    const field = fields.find((item) => item.index === mapping.index);

    if (!field) {
      return mapping;
    }

    return {
      ...mapping,
      value: normalizedMappingValue(field, mapping.value)
    };
  }

  function serializeFields(fields) {
    return fields.map(({ elementRef, choiceRefs, ...field }) => field);
  }

  async function fillElement(element, mapping, field) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    const hasListboxPopup = element.getAttribute("aria-haspopup") === "listbox";

    if (field.type === "radio" && field.choiceRefs?.length) {
      return fillChoiceGroup(field, mapping.value, false);
    }

    if (field.type === "checkbox" && field.choiceRefs?.length) {
      return fillChoiceGroup(field, mapping.value, true);
    }

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
      if (valueMatches(getCurrentValue(element), mapping.value)) {
        return false;
      }
      setEditableText(element, mapping.value);
      return true;
    }

    if (valueMatches(getCurrentValue(element), mapping.value)) {
      return false;
    }

    setNativeValue(element, String(mapping.value));
    dispatchFormEvents(element);
    return true;
  }

  function fillChoiceGroup(field, desiredValue, allowMultiple) {
    const values = Array.isArray(desiredValue)
      ? desiredValue
      : String(desiredValue).split(/\s*[;,]\s*/).filter(Boolean);
    const choices = field.choiceRefs.map((ref) => ref.deref()).filter(Boolean);
    let clicked = 0;

    for (const choice of choices) {
      const label = choiceLabel(choice);
      const value = choiceValue(choice);
      const shouldChoose = values.some((item) => optionMatches(label, value, item));

      if (!shouldChoose) {
        continue;
      }

      if (choice.getAttribute("role") === "checkbox") {
        const current = choice.getAttribute("aria-checked") === "true";
        if (!current) {
          clickOption(choice);
        }
      } else if (choice.getAttribute("role") === "radio") {
        if (choice.getAttribute("aria-checked") !== "true") {
          clickOption(choice);
        }
      } else if (choice.type === "checkbox") {
        if (!choice.checked) {
          clickOption(choice);
        }
        choice.checked = true;
      } else if (choice.type === "radio") {
        if (!choice.checked) {
          clickOption(choice);
        }
        choice.checked = true;
      }

      dispatchFormEvents(choice);
      clicked += 1;

      if (!allowMultiple) {
        break;
      }
    }

    return clicked > 0;
  }

  async function fillCombobox(element, desiredValue) {
    const trigger = dropdownTrigger(element) || element;
    trigger.focus();
    trigger.click();
    await sleep(200);

    const option = Array.from(document.querySelectorAll("[role='option'], [data-option], .select2-results__option, [role='menuitemradio'], [data-automation-id='promptOption']"))
      .filter(isVisibleElement)
      .find((item) => optionMatches(
        item.getAttribute("data-automation-label") || item.textContent || item.getAttribute("aria-label") || "",
        item.getAttribute("data-automation-label") || item.getAttribute("data-value") || item.getAttribute("value") || "",
        desiredValue
      ));

    if (option) {
      clickOption(option);
      dispatchFormEvents(trigger);
      dispatchFormEvents(element);
      return true;
    }

    if (trigger.tagName.toLowerCase() !== "button") {
      setEditableText(trigger, desiredValue);
      await sleep(200);

      const typedOption = Array.from(document.querySelectorAll("[role='option'], [data-option], .select2-results__option, [role='menuitemradio'], [data-automation-id='promptOption']"))
        .filter(isVisibleElement)
        .find((item) => optionMatches(
          item.getAttribute("data-automation-label") || item.textContent || item.getAttribute("aria-label") || "",
          item.getAttribute("data-automation-label") || item.getAttribute("data-value") || item.getAttribute("value") || "",
          desiredValue
        ));

      if (typedOption) {
        clickOption(typedOption);
        dispatchFormEvents(trigger);
        dispatchFormEvents(element);
        return true;
      }
    }

    dispatchFormEvents(trigger);
    dispatchFormEvents(element);
    return trigger.tagName.toLowerCase() !== "button";
  }

  function clickOption(option) {
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      option.dispatchEvent(new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        view: window
      }));
    }
  }

  function fillSelect(select, desiredValue) {
    const options = Array.from(select.options);
    const option = options.find((item) => optionMatches(item.textContent || "", item.value, desiredValue));

    if (!option) {
      return false;
    }

    if (select.value === option.value) {
      return false;
    }

    select.value = option.value;
    dispatchFormEvents(select);
    return true;
  }

  function fillRadio(element, desiredValue, field) {
    const name = element.getAttribute("name");
    const candidates = name
      ? Array.from(document.querySelectorAll(`input[type="radio"][name="${cssEscape(name)}"]`))
      : [element];

    const match = candidates.find((radio) => {
      return optionMatches(getLabelText(radio), radio.value || radio.getAttribute("aria-label") || "", desiredValue);
    });

    const fallback = field.options?.find((option) => optionMatches(option.label, option.value, desiredValue));
    const target = match || (fallback ? candidates.find((radio) => radio.value === fallback.value) : null);

    if (!target) {
      return false;
    }

    if (target.checked || target.getAttribute("aria-checked") === "true") {
      return false;
    }

    clickOption(target);
    target.checked = true;
    dispatchFormEvents(target);
    return true;
  }

  async function fillWorkdayTargetCountry(profile, settings) {
    const targetCountry = settings?.targetCountry || "";
    const address = selectAddress(profile, settings, "country");
    const desired = targetCountry === "usa"
      ? (address?.country || "United States")
      : targetCountry === "canada"
        ? (address?.country || "Canada")
        : "";

    if (!desired) {
      return false;
    }

    const control = findWorkdayCountryDropdown();
    if (!control) {
      return false;
    }

    const current = normalize(getCurrentValue(control));
    if (optionMatches(current, "", desired)) {
      return false;
    }

    return fillCombobox(control, desired);
  }

  function findWorkdayCountryDropdown() {
    const labels = Array.from(document.querySelectorAll("label, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel'], span, div"))
      .filter(isVisibleElement)
      .filter((item) => {
        const text = normalize(item.innerText || item.textContent || "");
        return /^country\s*\*?$/.test(text) && !/phone/.test(text);
      });

    for (const label of labels) {
      const control = findDropdownNearLabel(label);
      if (control) {
        return control;
      }
    }

    return null;
  }

  function findDropdownNearLabel(label) {
    let container = label;

    for (let depth = 0; container && container !== document.body && depth < 8; depth += 1) {
      const control = Array.from(container.querySelectorAll("[aria-haspopup='listbox'], [role='combobox'], [data-automation-id='selectWidget'], [data-automation-id='selectShowAll'], button"))
        .find((item) => isVisibleElement(item) && dropdownTrigger(item) && !/phone/.test(normalize(getLabelText(item) || getSurroundingText(item))));

      if (control) {
        return control;
      }

      container = container.parentElement;
    }

    return null;
  }

  function optionMatches(label, value, desiredValue) {
    const desired = normalize(String(desiredValue));
    const normalizedLabel = normalize(label || "");
    const normalizedValue = normalize(value || "");
    const aliases = answerAliases(desired);

    if (!desired) {
      return false;
    }

    if (normalizedValue === desired || normalizedLabel === desired) {
      return true;
    }

    if (aliases.some((alias) => normalizedValue === alias || normalizedLabel === alias)) {
      return true;
    }

    if (isUnitedStatesDesired(desired)) {
      return isUnitedStatesOption(normalizedLabel) || isUnitedStatesOption(normalizedValue);
    }

    return aliases.some((alias) => alias.length > 3 && containsNormalizedPhrase(normalizedLabel, alias))
      || (desired.length > 2 && containsNormalizedPhrase(normalizedLabel, desired));
  }

  function isUnitedStatesDesired(value) {
    return [
      "united states",
      "united states of america",
      "usa",
      "u s",
      "u s a",
      "us"
    ].includes(value);
  }

  function isUnitedStatesOption(value) {
    return [
      "united states",
      "united states of america",
      "usa",
      "u s",
      "u s a",
      "us"
    ].includes(value);
  }

  function containsNormalizedPhrase(text, phrase) {
    if (phrase.includes(" ")) {
      return text.includes(phrase);
    }

    return new RegExp(`\\b${escapeRegex(phrase)}\\b`).test(text);
  }

  function escapeRegex(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function answerAliases(desired) {
    const aliases = new Set([desired]);

    if (desired === "no") {
      [
        "no i am not",
        "no i do not",
        "no i don t",
        "no i have not",
        "no i do not have",
        "no i don t have",
        "not a protected veteran",
        "i am not a protected veteran",
        "not protected veteran",
        "not a veteran",
        "not hispanic or latino",
        "not hispanic",
        "not latino"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "open to relocation" || desired === "open to relocate") {
      [
        "willing to relocate",
        "i am willing to relocate",
        "i am willing to relocate before starting employment",
        "open to relocating",
        "open to relocate"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "no disability" || desired === "no disabilities") {
      [
        "no",
        "no i do not have a disability",
        "no i don t have a disability",
        "no i do not have a disability and have not had one in the past",
        "no i don t have a disability and have not had one in the past"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "not a protected veteran" || desired === "i am not a protected veteran") {
      [
        "no",
        "not protected veteran",
        "i am not a protected veteran"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "yes") {
      [
        "yes i am",
        "yes i do",
        "yes i have"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "united states" || desired === "usa" || desired === "u s" || desired === "u s a") {
      [
        "united states",
        "united states of america",
        "usa",
        "u s a",
        "us"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "united states of america") {
      [
        "united states",
        "usa",
        "u s a",
        "us"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "canada") {
      aliases.add("canada");
    }

    if (/\bbachelor/.test(desired)) {
      [
        "bachelor degree",
        "bachelors degree",
        "bachelor s degree",
        "undergraduate degree"
      ].forEach((alias) => aliases.add(alias));
    }

    if (/\bmaster/.test(desired)) {
      [
        "master degree",
        "masters degree",
        "master s degree",
        "graduate degree"
      ].forEach((alias) => aliases.add(alias));
    }

    for (const alias of usStateAliases(desired)) {
      aliases.add(alias);
    }

    if (desired === "asian") {
      [
        "asian not hispanic or latino",
        "asian not hispanic",
        "asian"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "male") {
      [
        "man",
        "male"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "cisgender man") {
      [
        "man",
        "male"
      ].forEach((alias) => aliases.add(alias));
    }

    return Array.from(aliases);
  }

  function usStateAliases(value) {
    const states = {
      al: "alabama",
      ak: "alaska",
      az: "arizona",
      ar: "arkansas",
      ca: "california",
      co: "colorado",
      ct: "connecticut",
      de: "delaware",
      fl: "florida",
      ga: "georgia",
      hi: "hawaii",
      id: "idaho",
      il: "illinois",
      in: "indiana",
      ia: "iowa",
      ks: "kansas",
      ky: "kentucky",
      la: "louisiana",
      me: "maine",
      md: "maryland",
      ma: "massachusetts",
      mi: "michigan",
      mn: "minnesota",
      ms: "mississippi",
      mo: "missouri",
      mt: "montana",
      ne: "nebraska",
      nv: "nevada",
      nh: "new hampshire",
      nj: "new jersey",
      nm: "new mexico",
      ny: "new york",
      nc: "north carolina",
      nd: "north dakota",
      oh: "ohio",
      ok: "oklahoma",
      or: "oregon",
      pa: "pennsylvania",
      ri: "rhode island",
      sc: "south carolina",
      sd: "south dakota",
      tn: "tennessee",
      tx: "texas",
      ut: "utah",
      vt: "vermont",
      va: "virginia",
      wa: "washington",
      wv: "west virginia",
      wi: "wisconsin",
      wy: "wyoming",
      dc: "district of columbia"
    };

    if (states[value]) {
      return [states[value]];
    }

    const abbreviation = Object.entries(states).find(([, name]) => name === value)?.[0];
    return abbreviation ? [abbreviation] : [];
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

  function confirmFilledElement(element) {
    dispatchEnterOrEscape(element, "Enter");
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.blur?.();
  }

  function dispatchEnterOrEscape(element, key) {
    const keyCode = key === "Enter" ? 13 : 27;
    for (const type of ["keydown", "keypress", "keyup"]) {
      element.dispatchEvent(new KeyboardEvent(type, {
        key,
        code: key,
        keyCode,
        which: keyCode,
        bubbles: true,
        cancelable: true
      }));
    }
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
      if (state.isApplying || state.dynamicRunCount >= 1) {
        return;
      }

      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(() => {
        if (!state.isApplying && Date.now() - state.lastFilledAt > 2500 && state.dynamicRunCount < 1) {
          state.dynamicRunCount += 1;
          runAutofillPage().catch(() => {});
        }
      }, 1200);
    });

    state.observer.observe(document.body, {
      childList: true,
      subtree: true
    });
  }

  function stopObserver() {
    if (!state.observer) {
      return;
    }

    state.observer.disconnect();
    state.observer = null;
  }

  function markFilled(element, mapping) {
    element.dataset.applicationAutofill = mapping.source || "rule";
    element.style.outline = "2px solid rgba(22, 163, 74, 0.55)";
    element.style.outlineOffset = "2px";
  }

  function isFillable(element) {
    const style = window.getComputedStyle(element);
    const type = (element.getAttribute("type") || "").toLowerCase();
    const isDropdownButton = type === "button" && Boolean(dropdownTrigger(element));

    return !element.disabled
      && !element.readOnly
      && type !== "hidden"
      && type !== "submit"
      && (type !== "button" || isDropdownButton)
      && type !== "reset"
      && style.display !== "none"
      && style.visibility !== "hidden";
  }

  function isVisibleElement(element) {
    if (!element || element.hidden || element.getAttribute("aria-hidden") === "true") {
      return false;
    }

    let current = element;
    while (current && current !== document.body) {
      if (current.hidden || current.getAttribute?.("aria-hidden") === "true") {
        return false;
      }

      const style = window.getComputedStyle(current);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
        return false;
      }

      current = current.parentElement;
    }

    const rect = element.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0 || Boolean(compactText(element.innerText || element.textContent || ""));
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

  function valueMatches(current, desired) {
    const currentText = String(current ?? "").trim();
    const desiredText = String(desired ?? "").trim();

    if (!desiredText) {
      return !currentText;
    }

    if (currentText === desiredText) {
      return true;
    }

    return normalizeDateValue(currentText) === normalizeDateValue(desiredText)
      && normalizeDateValue(desiredText) !== "";
  }

  function normalizeDateValue(value) {
    const match = String(value || "").trim().match(/^0?(\d{1,2})\s*\/\s*(\d{4})$/);
    return match ? `${Number(match[1])}/${match[2]}` : "";
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
