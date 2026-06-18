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
    "oj-select-single",
    "oj-combobox-one",
    ".select2-choice",
    ".select2-selection",
    ".sapMInputBaseInner",
    "[aria-autocomplete='list']",
    "[role='checkbox']",
    "[role='radio']"
  ].join(",");

  const DROPDOWN_OPTION_SELECTOR = [
    "[role='option']",
    "[data-option]",
    ".select2-results__option",
    ".select2-result-label",
    "[role='menuitemradio']",
    "[data-automation-id='promptOption']",
    ".select__option",
    "[id*='-option-']",
    ".oj-listbox-result",
    ".oj-listbox-result-label",
    ".oj-option",
    "[oj-option-id]",
    ".sapMSelectListItem",
    ".sapMLIB",
    ".sapMComboBoxBaseItem",
    ".ui-menu-item",
    ".ui-menu-item-wrapper",
    ".iCIMS_Dropdown_Option"
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

    if (message?.type === "DEBUG_DROPDOWNS") {
      debugDropdowns()
        .then((result) => sendResponse({ ok: true, result }))
        .catch((error) => sendResponse({ ok: false, error: error.message }));
      return true;
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
      .filter((field) => !mappedIndexes.has(field.index) || isAiOnlyField(field))
      .filter(shouldAskForField)
      .map(({ elementRef, ...field }) => ({
        ...field,
        label: displayLabelForField(field),
        answerKey: answerKeyForField(field),
        needsManualUpload: field.type === "file",
        unfilledReason: unfilledReasonForField(field, mappedIndexes)
      }));

    return {
      scanned: plan.fields.length,
      mapped: plan.mappings.length,
      mappings: plan.mappings.map((mapping) => {
        const field = plan.fields.find((item) => item.index === mapping.index);

        return {
          ...mapping,
          label: field?.label || field?.name || field?.id || `Field ${mapping.index + 1}`,
          name: field?.name || "",
          id: field?.id || "",
          placeholder: field?.placeholder || "",
          ariaLabel: field?.ariaLabel || "",
          tag: field?.tag || "",
          type: field?.type || "",
          options: field?.options || []
        };
      }),
      unmappedFields,
      debugFields: plan.fields.map(debugFieldForPreview),
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

  function debugFieldForPreview(field) {
    return {
      index: field.index,
      tag: field.tag,
      type: field.type,
      label: displayLabelForField(field),
      rawLabel: field.label || "",
      value: field.value || "",
      name: field.name || "",
      id: field.id || "",
      ariaLabel: field.ariaLabel || "",
      required: Boolean(field.required),
      questionText: field.questionText || "",
      surroundingText: field.surroundingText || "",
      nearbyText: field.nearbyText || "",
      options: field.options || [],
      haystack: fieldHaystack(field),
      isPolicy: isAiOnlyField(field),
      shouldAsk: shouldAskForField(field)
    };
  }

  function unfilledReasonForField(field, mappedIndexes) {
    if (field.type === "file") {
      return "File inputs require manual browser confirmation.";
    }

    if (mappedIndexes.has(field.index) && isAiOnlyField(field)) {
      return "Autofill has a rule, but this policy question is also available for AI review.";
    }

    if (isOptionLikeField(field) && !field.options?.length) {
      return "Dropdown options were not discoverable, so neither autofill nor AI can safely choose an option yet.";
    }

    if (field.required) {
      return "Required field was not filled by autofill. Ask AI or add a saved answer.";
    }

    return "No saved profile value or safe rule matched this field.";
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
      ? await getBackendMappings(fields.filter((field) => !isAiOnlyField(field)), profile)
      : [];
    const mappings = mergeMappings([...localMappings, ...repeatableMappings], backendMappings, fields);

    return { fields, mappings, profile };
  }

  async function debugDropdowns() {
    const fields = scanFields();
    const dropdowns = [];

    for (const field of fields) {
      if (!isDynamicDropdownField(field) && field.tag !== "select" && !field.options?.length) {
        continue;
      }

      const element = field.elementRef?.deref?.();
      let options = field.options || [];

      if (element && isFillable(element) && !options.length && isDynamicDropdownField(field)) {
        options = await discoverDynamicDropdownOptions(element);
      }

      dropdowns.push({
        index: field.index,
        label: displayLabelForField(field),
        tag: field.tag,
        type: field.type,
        value: field.value || "",
        isDynamic: isDynamicDropdownField(field),
        optionsFound: options.length,
        options: options.slice(0, 25)
      });
    }

    return { count: dropdowns.length, dropdowns };
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
    mappings = mappings
      .map((mapping) => normalizeMappingForField(mapping, fields))
      .filter((mapping) => hasValue(mapping.value));
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
        const field = resolveFieldForMapping(mapping, fields);
        const element = field?.elementRef?.deref?.();

        if (!element || !isFillable(element)) {
          continue;
        }

        if (shouldSkipMappingForExistingCoreField(mapping, field, element)) {
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
      filled += await fillWorkdayEducationDropdownFallback(profile);
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
    const options = getOptions(element);
    const rawLabel = getLabelText(element);
    const nearbyText = nearbyTextForField(element, options);
    const questionText = policyQuestionTextForField(element, options);
    const recoveredLabel = isLowInformationText(rawLabel)
      ? parentQuestionLabel(element, options) || precedingQuestionLabel(element, options) || questionText || firstPolicyQuestionLine(nearbyText) || rawLabel
      : rawLabel;

    return {
      index,
      tag,
      type,
      name: element.getAttribute("name") || "",
      id: element.id || "",
      label: recoveredLabel,
      placeholder: element.getAttribute("placeholder") || "",
      ariaLabel: element.getAttribute("aria-label") || "",
      autocomplete: element.getAttribute("autocomplete") || "",
      dataAutomationId: element.getAttribute("data-automation-id") || "",
      required: isRequiredElement(element, rawLabel),
      value: getCurrentValue(element),
      options,
      surroundingText: getSurroundingText(element),
      questionText,
      nearbyText,
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
    const nearbyText = nearbyTextForField(first, options);
    const questionText = policyQuestionTextForField(first, options);
    const initialLabel = choiceGroupLabel(group.container, options) || getSurroundingText(first) || getLabelText(first);
    const label = isLowInformationText(initialLabel)
      ? parentQuestionLabel(first, options) || precedingQuestionLabel(first, options) || questionText || firstPolicyQuestionLine(nearbyText) || initialLabel
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
      required: isRequiredElement(first, label),
      value: "",
      options,
      surroundingText: compactText(group.container?.innerText || ""),
      questionText,
      nearbyText,
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

  function isRequiredElement(element, label = "") {
    return element.required === true
      || element.getAttribute("aria-required") === "true"
      || /\*/.test(label || "")
      || /\brequired\b/i.test(label || "")
      || /\*/.test(getSurroundingText(element));
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
      const question = lines.find((line) => /[?]|\b(now|previously|ever|worked|employed|subsidiar|affiliate|authorize|authorization|sponsor|sponsorship|require|visa|military|served|spouse|domestic partner|relative|dealer|contractor|relocat|age|proof of age)\b/i.test(line));

      if (question) {
        return question;
      }

      current = current.parentElement;
      depth += 1;
    }

    return "";
  }

  function precedingQuestionLabel(element, options) {
    const targetRect = element.getBoundingClientRect();
    const optionLabels = new Set(options.map((option) => normalize(option.label)).filter(Boolean));
    const candidates = Array.from(document.querySelectorAll("label, legend, h1, h2, h3, h4, p, div, span, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel']"))
      .filter(isVisibleElement)
      .filter((node) => node !== element && followsNode(node, element))
      .flatMap((node) => questionCandidatesNear(node, targetRect, optionLabels))
      .filter((candidate) => candidate.distance >= -16 && candidate.distance <= 520)
      .sort((left, right) => left.distance - right.distance);

    return candidates[0]?.text || "";
  }

  function questionCandidatesNear(node, targetRect, optionLabels) {
    const rect = node.getBoundingClientRect();
    const text = compactText(node.innerText || node.textContent || "");
    const controlCount = node.querySelectorAll?.("input, textarea, select, button, [role='radio'], [role='checkbox'], [role='combobox']").length || 0;

    if (!text || text.length > 1200 || controlCount > 14 || rect.bottom < targetRect.top - 560 || rect.top > targetRect.bottom + 48) {
      return [];
    }

    return text
      .split(/\n+/)
      .map((line) => compactText(line))
      .filter((line) => isPolicyQuestionLine(line, optionLabels))
      .map((line) => ({
        text: line,
        distance: Math.max(0, targetRect.top - rect.bottom)
      }));
  }

  function isPolicyQuestionLine(line, optionLabels = new Set()) {
    const normalized = normalize(line);
    return line.length > 3
      && line.length <= 360
      && !optionLabels.has(normalized)
      && !isLowInformationText(line)
      && !/^\*?\s*(required|select one|yes|no)\s*$/i.test(line)
      && isAiOnlyQuestion(normalized);
  }

  function nearbyTextForField(element, options = []) {
    const targetRect = element.getBoundingClientRect();
    const optionLabels = new Set(options.map((option) => normalize(option.label)).filter(Boolean));
    const candidates = Array.from(document.querySelectorAll("label, legend, h1, h2, h3, h4, p, li, div, span, [role='heading'], [data-automation-id='formLabel'], [data-automation-id='formFieldLabel']"))
      .filter(isVisibleElement)
      .filter((node) => node !== element)
      .flatMap((node) => nearbyTextCandidates(node, targetRect, optionLabels))
      .sort((left, right) => left.score - right.score);

    return unique(candidates.map((candidate) => candidate.text)).slice(0, 14).join(" ");
  }

  function policyQuestionTextForField(element, options = []) {
    const targetRect = element.getBoundingClientRect();
    const optionLabels = new Set(options.map((option) => normalize(option.label)).filter(Boolean));
    const candidates = Array.from(document.querySelectorAll("label, legend, h1, h2, h3, h4, p, li, div, span, [role='heading'], [data-automation-id='formLabel'], [data-automation-id='formFieldLabel']"))
      .filter(isVisibleElement)
      .filter((node) => node !== element)
      .flatMap((node) => nearbyTextCandidates(node, targetRect, optionLabels))
      .filter((candidate) => isAiOnlyQuestion(normalize(candidate.text)))
      .sort((left, right) => left.score - right.score);

    return candidates[0]?.text || "";
  }

  function nearbyTextCandidates(node, targetRect, optionLabels) {
    const rect = node.getBoundingClientRect();
    const text = compactText(node.innerText || node.textContent || "");
    const controlCount = node.querySelectorAll?.("input, textarea, select, button, [role='radio'], [role='checkbox'], [role='combobox']").length || 0;

    if (!text || text.length > 1400 || controlCount > 20 || rect.bottom < targetRect.top - 720 || rect.top > targetRect.bottom + 260) {
      return [];
    }

    const distance = rect.bottom <= targetRect.top
      ? targetRect.top - rect.bottom
      : rect.top >= targetRect.bottom
        ? rect.top - targetRect.bottom + 80
        : 20;

    return text
      .split(/\n+/)
      .map((line) => compactText(line))
      .filter((line) => isUsefulNearbyLine(line, optionLabels))
      .map((line) => ({
        text: line,
        score: distance + (isAiOnlyQuestion(normalize(line)) ? -120 : 0)
      }));
  }

  function isUsefulNearbyLine(line, optionLabels) {
    const normalized = normalize(line);
    return line.length > 2
      && line.length <= 360
      && !optionLabels.has(normalized)
      && !isLowInformationText(line)
      && !/^(english|settings|search for jobs|candidate home|job alerts|back|save and continue|submit)$/i.test(line)
      && (
        /[?]/.test(line)
        || isAiOnlyQuestion(normalized)
        || /(select one|required|\*)/i.test(line)
      );
  }

  function firstPolicyQuestionLine(text) {
    return String(text || "")
      .split(/\s{2,}|\n+/)
      .map((line) => compactText(line))
      .find((line) => isAiOnlyQuestion(normalize(line))) || "";
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
      || field.tag === "oj-select-single"
      || field.tag === "oj-combobox-one"
      || /selectwidget|selectshowall/i.test(field.dataAutomationId || "")
      || field.ariaLabel
      || /(\blocation\b|\bcity\b|\bcountry\b|\bstate\b|\bprovince\b|phone.*code|country.*phone|select one)/i.test([field.label, field.placeholder, field.name, field.id, field.ariaLabel, field.surroundingText].join(" "))
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
      await sleep(250);

      let options = await waitForDropdownOptions(trigger);

      if (!options.length) {
        dispatchEnterOrEscape(trigger, "ArrowDown");
        await sleep(250);
        options = await waitForDropdownOptions(trigger);
      }

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

    return element.querySelector?.("[aria-haspopup='listbox'], [role='combobox'], [role='button'][aria-haspopup], button, .select2-choice, .select2-selection, .sapMInputBaseInner");
  }

  function isListboxTrigger(element) {
    const role = (element.getAttribute("role") || "").toLowerCase();
    const popup = (element.getAttribute("aria-haspopup") || "").toLowerCase();
    const automationId = (element.getAttribute("data-automation-id") || "").toLowerCase();
    const tag = element.tagName.toLowerCase();
    const className = (element.getAttribute("class") || "").toLowerCase();
    return role === "combobox"
      || popup === "listbox"
      || popup === "true"
      || automationId === "selectwidget"
      || automationId === "selectshowall"
      || tag === "oj-select-single"
      || tag === "oj-combobox-one"
      || /select2|sapminput|sapmcombobox|oj-select|oj-combobox/.test(className);
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
      Array.from(container.querySelectorAll(DROPDOWN_OPTION_SELECTOR))
    ));

    return optionNodes
      .filter(isVisibleElement)
      .map((option) => ({
        label: compactText(option.getAttribute("data-automation-label") || option.innerText || option.textContent || option.getAttribute("aria-label") || ""),
        value: compactText(
          option.getAttribute("data-automation-label")
          || option.getAttribute("data-value")
          || option.getAttribute("data-id")
          || option.getAttribute("data-key")
          || option.getAttribute("oj-option-id")
          || option.getAttribute("value")
          || option.getAttribute("aria-label")
          || option.innerText
          || option.textContent
          || ""
        )
      }))
      .filter((option) => option.label || option.value);
  }

  async function waitForDropdownOptions(trigger, attempts = 8) {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const options = collectVisibleDropdownOptions(trigger);
      if (options.length) {
        return options;
      }

      await sleep(125);
    }

    return [];
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
    const haystack = fieldHaystack(field);
    const fullHaystack = fullFieldHaystack(field);
    const phoneContextHaystack = normalize([primaryHaystack, field.surroundingText, field.nearbyText].join(" "));

    if (shouldSkipField(primaryHaystack)) {
      return null;
    }

    if (isPhoneCountryCodeField(primaryHaystack) || isGreenhousePhoneCountryField(field, primaryHaystack, phoneContextHaystack)) {
      return buildMapping(field, phoneCountryCodeAnswer(field, profile), "rule", 0.88);
    }

    if (isPhoneExtensionField(primaryHaystack)) {
      return hasValue(profile.phoneExtension)
        ? buildMapping(field, profile.phoneExtension, "rule", 0.9)
        : null;
    }

    if (isPhoneDeviceTypeField(primaryHaystack)) {
      return buildMapping(field, profile.answers?.phoneDeviceType || profile.phoneDeviceType || "Mobile", "rule", 0.86);
    }

    if (isPhoneNumberField(primaryHaystack)) {
      return buildMapping(field, profile.phone, "rule", 0.9);
    }

    const address = selectAddress(profile, settings, primaryHaystack);

    const agreementMapping = mapAgreementCheckbox(field, primaryHaystack);
    if (agreementMapping) {
      return agreementMapping;
    }

    const workQuestionMapping = mapWorkQuestion(field, profile, haystack);
    if (workQuestionMapping) {
      return workQuestionMapping;
    }

    const commonQuestionMapping = mapCommonAtsQuestion(field, profile, haystack);
    if (commonQuestionMapping) {
      return commonQuestionMapping;
    }

    if (isAiOnlyQuestion(haystack)) {
      return null;
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

    const governmentFormMapping = mapGovernmentSelfIdField(field, profile, primaryHaystack, fullHaystack);
    if (governmentFormMapping) {
      return governmentFormMapping;
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

    const locationMapping = mapLocationField(field, profile, settings, address, primaryHaystack);
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

    if (/(terms and conditions|terms of use|terms of service|conditions of use|user agreement|legal terms|accept.*terms|agree.*terms|consent.*terms)/.test(haystack)) {
      return buildMapping(field, profile.answers?.acceptTerms || "Yes", "rule", 0.9);
    }

    if (/(certify|certifying|certification|true and correct|true.*complete|information.*provided.*true|facts.*true)/.test(haystack)) {
      return buildMapping(field, profile.answers?.certifyApplicationTruth || "Yes", "rule", 0.9);
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

    if (isWorkEligibilityQuestion(haystack)) {
      return buildMapping(
        field,
        workAuthorizationAnswer(field, profile),
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
    return /country.*phone.*code|phone.*country.*code|country code|phone\s+country|country\s+phone/.test(haystack);
  }

  function isGreenhousePhoneCountryField(field, primaryHaystack, fullHaystack) {
    return /^country\s*(required)?\s*\*?$|^required\s+country\s*\*?$/.test(primaryHaystack)
      && (/\bphone\b/.test(fullHaystack) || hasPhoneCountryCodeOptions(field.options))
      && !/(currently reside|current residence|country.*reside|country region|country\/region|location|city)/.test(primaryHaystack)
      && (!field.options?.length || hasPhoneCountryCodeOptions(field.options));
  }

  function hasPhoneCountryCodeOptions(options = []) {
    return options.some((option) => /\+\s*\d{1,4}|\(\s*\+\s*\d{1,4}\s*\)/.test(`${option.label || ""} ${option.value || ""}`));
  }

  function phoneCountryCodeAnswer(field, profile) {
    const explicit = profile.answers?.phoneCountryCode || profile.phoneCountryCode || "Canada (+1)";
    const options = field.options || [];
    const canadaOption = options.find((option) => {
      const text = normalize(`${option.label || ""} ${option.value || ""}`);
      return /\bcanada\b/.test(text) && /(^|\s|\()\+?1(\)|\s|$)/.test(text);
    });

    return canadaOption ? (canadaOption.label || canadaOption.value) : explicit;
  }

  function isPhoneExtensionField(haystack) {
    return /phone.*extension|extension/.test(haystack);
  }

  function isPhoneDeviceTypeField(haystack) {
    return /phone.*device.*type|device.*type.*phone/.test(haystack);
  }

  function isPhoneNumberField(haystack) {
    return /(phone|mobile|cell|telephone)/.test(haystack)
      && !isPhoneCountryCodeField(haystack)
      && !isPhoneExtensionField(haystack)
      && !isPhoneDeviceTypeField(haystack)
      && !/(location|city|country|state|province|postal|zip)/.test(haystack);
  }

  function findSavedAnswer(field, profile) {
    const haystack = fieldHaystack(field);

    if (isLowInformationChoiceLabel(field) || isAiOnlyQuestion(haystack) || isCompanyHistoryQuestion(haystack)) {
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
    return /^(yes|required yes|yes required|no|required no|no required|yes\s*no|no\s*yes|select one|required select one|select one required|required|true false|false true)$/.test(label);
  }

  function isAiOnlyField(field) {
    return isAiOnlyQuestion(fieldHaystack(field));
  }

  function fieldHaystack(field) {
    const label = field.label || "";
    const includeSurroundingText = !compactText(label) || isLowInformationText(label);

    return normalize([
      label,
      field.placeholder,
      field.name,
      field.id,
      field.ariaLabel,
      field.autocomplete,
      includeSurroundingText ? field.questionText : "",
      includeSurroundingText ? field.surroundingText : "",
      includeSurroundingText ? field.nearbyText : ""
    ].join(" "));
  }

  function fullFieldHaystack(field) {
    return normalize([
      field.label,
      field.placeholder,
      field.name,
      field.id,
      field.ariaLabel,
      field.autocomplete,
      field.questionText,
      field.surroundingText,
      field.nearbyText
    ].join(" "));
  }

  function isAiOnlyQuestion(haystack) {
    return [
      /(18 years of age|at least 18|proof of age|minimum age)/,
      isWorkEligibilityQuestion,
      /(sponsor|sponsorship|visa|h-?1b|f-?1|opt|cpt|tn|ead|work permit)/,
      /(now|ever|previously|formerly|current|directly).*(employed|worked|work|contractor|dealer|affiliate|subsidiar|paycheck|w-?2)/,
      /(employed|worked|work|contractor|dealer|affiliate|subsidiar|paycheck|w-?2).*(now|ever|previously|formerly|current|directly)/,
      /(relatives?|family member|spouse|domestic partner).*(employed|work|working|military|armed forces|served|service)/,
      /(military|armed forces|served|service|veteran)/,
      /(interested in relocating|relocation|relocating)/
    ].some((pattern) => (
      typeof pattern === "function" ? pattern(haystack) : pattern.test(haystack)
    ));
  }

  function isWorkEligibilityQuestion(haystack) {
    return /(legally\s+)?(authorized|eligible|permitted|allowed).*(work|employment)/.test(haystack)
      || /(work|employment).*(authorized|eligible|authorization|eligibility)/.test(haystack)
      || /work authorization|proof of authorization|legally eligible/.test(haystack);
  }

  function shouldAskForField(field) {
    const haystack = fieldHaystack(field);

    if (!haystack) {
      return false;
    }

    if (isAiOnlyQuestion(haystack)) {
      return true;
    }

    if (shouldSkipField(haystack)) {
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
      return buildMapping(field, sponsorshipAnswer(field, profile), "rule", 0.9);
    }

    if (isWorkEligibilityQuestion(haystack)) {
      return buildMapping(
        field,
        workAuthorizationAnswer(field, profile),
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

  function sponsorshipAnswer(field, profile) {
    const explicit = profile.needsSponsorship || profile.answers?.sponsorship;
    const desired = explicit || "No";
    const options = field.options || [];
    const preferredOption = options.find((option) => {
      const text = normalize([option.label, option.value].join(" "));
      return /(do not|don t|no).*(require|need).*(sponsor|visa)|not require sponsorship|no sponsorship/.test(text);
    }) || options.find((option) => optionMatches(option.label, option.value, desired));

    if (preferredOption) {
      return preferredOption.label || preferredOption.value;
    }

    return normalizeSponsorshipValue(desired);
  }

  function workAuthorizationAnswer(field, profile) {
    const explicit = profile.workAuthorization || profile.answers?.workAuthorization;
    const options = field.options || [];
    const preferredOption = options.find((option) => {
      const text = normalize([option.label, option.value].join(" "));
      return /authorized.*work.*(united states|u s|usa)/.test(text)
        && /(any employer|for any|without sponsorship|do not require sponsorship|not require sponsorship)/.test(text);
    }) || options.find((option) => {
      const text = normalize([option.label, option.value].join(" "));
      return /i am authorized|legally authorized|authorized.*work/.test(text)
        && !/(not authorized|not eligible|require sponsorship|need sponsorship)/.test(text);
    });

    if (preferredOption) {
      return preferredOption.label || preferredOption.value;
    }

    return explicit || "I am authorized to work in the United States for any employer";
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

  function mapCommonAtsQuestion(field, profile, haystack) {
    if (/(18 years of age|at least 18|proof of age|minimum age)/.test(haystack)) {
      return buildMapping(field, profile.answers?.meetsMinimumAge || "Yes", "rule", 0.88);
    }

    if (/(served|service).*(u\.?s\.?|united states).*(military|armed forces)|military service/.test(haystack)) {
      return buildMapping(field, profile.answers?.militaryService || profile.militaryService || "No", "rule", 0.88);
    }

    if (/(spouse|domestic partner).*(served|service).*(military|armed forces)/.test(haystack)) {
      return buildMapping(field, profile.answers?.spouseMilitaryService || "No", "rule", 0.88);
    }

    if (/(relatives?|family member|spouse|domestic partner).*(employed|work|working)|((employed|work|working).*(relatives?|family member|spouse|domestic partner))/.test(haystack)) {
      return buildMapping(field, profile.answers?.relativesAtCompany || "No", "rule", 0.88);
    }

    if (/(groups?|communities|community|affiliation|affiliated|membership|member of|belong to)/.test(haystack)) {
      return buildMapping(field, profile.answers?.groupAffiliations || "No", "rule", 0.86);
    }

    if (/(authorized dealer|dealer).*(employed|work|working)|((employed|work|working).*(authorized dealer|dealer))/.test(haystack)) {
      return buildMapping(field, profile.answers?.workedForAuthorizedDealer || "No", "rule", 0.88);
    }

    if (/(contractor).*(employed|work|working)|working as a contractor|currently working as a contractor/.test(haystack)) {
      return buildMapping(field, profile.answers?.workedAsContractorForCompany || "No", "rule", 0.88);
    }

    if (/(previously|currently|directly).*(employed|worked|work)|received a paycheck|w-2/.test(haystack)) {
      return buildMapping(field, previousCompanyAnswer(profile, haystack), "rule", 0.88);
    }

    if (/(relocation assistance|need relocation assistance|relocation support)/.test(haystack)) {
      return buildMapping(field, relocationAssistanceAnswer(field, profile), "rule", 0.86);
    }

    if (/(interested in relocating|relocation|relocating)/.test(haystack)) {
      return buildMapping(field, relocationAnswer(field, profile), "rule", 0.86);
    }

    return null;
  }

  function previousCompanyAnswer(profile, haystack) {
    const companies = normalizedWorkExperience(profile)
      .map((item) => normalize(item.company))
      .filter(Boolean);

    return companies.some((company) => company && exactCompanyMention(company, haystack)) ? "Yes" : "No";
  }

  function exactCompanyMention(company, haystack) {
    if (!company || company.length < 3) {
      return false;
    }

    return new RegExp(`(^|\\s)${escapeRegex(company)}(\\s|$)`).test(haystack);
  }

  function relocationAnswer(field, profile) {
    const explicit = profile.relocation || profile.answers?.relocation;
    const options = field.options || [];
    const preferred = options.find((option) => /anywhere/i.test(option.label || option.value || ""))
      || options.find((option) => /nationwide/i.test(option.label || option.value || ""))
      || options.find((option) => /yes/i.test(option.label || option.value || ""))
      || (hasValue(explicit) ? options.find((option) => optionMatches(option.label, option.value, explicit)) : null);

    return preferred ? (preferred.label || preferred.value) : (explicit || "Anywhere");
  }

  function relocationAssistanceAnswer(field, profile) {
    const explicit = profile.answers?.relocationAssistance;
    const options = field.options || [];
    const needsAssistance = /^(yes|true|1)$|need|require|request|want/.test(normalize(explicit || ""));
    const desired = needsAssistance ? "Yes" : "No";
    const preferred = options.find((option) => optionMatches(option.label, option.value, desired))
      || (hasValue(explicit) ? options.find((option) => optionMatches(option.label, option.value, explicit)) : null);

    return preferred ? (preferred.label || preferred.value) : desired;
  }

  function mapCompanyQuestion(field, profile, haystack) {
    if (/(whatsapp|sms|text messages?|messaging).*(recruit|hiring)|recruit.*(whatsapp|sms|text messages?|messaging)/.test(haystack)) {
      return buildMapping(field, profile.answers?.recruitingMessages || "No", "rule", 0.9);
    }

    if (/(subscribe|subscription|email alerts?|job alerts?|marketing emails?|promotional emails?|newsletter|mailing list)/.test(haystack)) {
      return buildMapping(field, profile.answers?.subscribeEmails || "No", "rule", 0.9);
    }

    return null;
  }

  function mapGovernmentSelfIdField(field, profile, primaryHaystack, fullHaystack) {
    if (!isCc305DisabilityFormContext(fullHaystack)) {
      return null;
    }

    if (/(employee id|employee number|worker id)/.test(primaryHaystack)) {
      return null;
    }

    if (/^name\s*(required)?$|^full name\s*(required)?$/.test(primaryHaystack)) {
      return hasValue(profile.fullName) ? buildMapping(field, profile.fullName, "rule", 0.9) : null;
    }

    if (/^date\s*(required)?$|^today s date\s*(required)?$/.test(primaryHaystack)) {
      return buildMapping(field, todayDateValue(), "rule", 0.9);
    }

    return null;
  }

  function isCc305DisabilityFormContext(haystack) {
    return /(cc-?305|omb control number 1250-0005|voluntary self-identification of disability|self identification of disability|please check one of the boxes below)/.test(haystack);
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

  function mapLocationField(field, profile, settings, address, haystack) {
    if (!address) {
      return null;
    }

    if (isEmploymentField(field, haystack)) {
      return null;
    }

    if (/(work remotely|remote location|plan to work remote)/.test(haystack)) {
      return null;
    }

    const applicationLocation = selectApplicationLocation(profile, settings, address);

    if (/(location city|city location|current city|where.*city)/.test(haystack)) {
      return hasValue(applicationLocation.city) ? buildMapping(field, applicationLocation.city, "rule", 0.9) : null;
    }

    if (/(location|required.*city.*(state|region|country)|city.*(state|region).*(country))/.test(haystack)) {
      const location = applicationLocation.full || [applicationLocation.city, applicationLocation.region, address.country].filter(Boolean).join(", ");
      return hasValue(location) ? buildMapping(field, location, "rule", 0.9) : null;
    }

    if (/(currently reside|current residence|country.*reside|country region|country\/region|\bcountry\b)/.test(haystack)) {
      return hasValue(address.country) ? buildMapping(field, address.country, "rule", 0.9) : null;
    }

    return null;
  }

  function selectApplicationLocation(profile, settings, address) {
    const answers = profile.answers || {};
    const target = settings?.targetCountry || "";
    const cityKey = target === "usa" ? "usaCity" : target === "canada" ? "canadaCity" : "";
    const locationKey = target === "usa" ? "usaLocation" : target === "canada" ? "canadaLocation" : "";
    const full = answers[locationKey] || profile[locationKey] || profile.applicationLocation || "";
    const city = answers[cityKey] || profile[cityKey] || cityFromLocation(full) || address.city || "";
    const region = (full && regionFromLocation(full)) || address.state || address.province || "";

    return { city, region, full };
  }

  function cityFromLocation(value) {
    return compactText(String(value || "").split(",")[0] || "");
  }

  function regionFromLocation(value) {
    return compactText(String(value || "").split(",")[1] || "");
  }

  function shouldSkipField(haystack) {
    if (/\bif yes\b|\bif applicable\b|please state their name|please provide.*if yes/.test(haystack)) {
      return true;
    }

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
      [/(sexual orientation|orientation)/, demographics.sexualOrientation || profile.answers?.sexualOrientation],
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
      if (/sexual orientation|orientation/.test(haystack)) {
        return null;
      }

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
      return /^(add|add another)$/.test(text) && sectionPattern.test(nearestSectionHeadingText(item));
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

    const mapping = buildMapping(field, value, "experience", 0.92);
    if (mapping) {
      mappings.push(mapping);
    }
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
    const parsedField = normalizeFieldOfStudy(inMatch ? inMatch[1] : "");

    return {
      school: compactText(profile.school || schoolLine.replace(/\s+(January|February|March|April|May|June|July|August|September|October|November|December)\b.*$/i, "")),
      degree: compactText(profile.degree || parsedDegree),
      fieldOfStudy: compactText(profile.fieldOfStudy || profile.major || profile.discipline || parsedField),
      startYear: yearFromValue(dateMatch[0] || profile.educationStartYear || ""),
      endYear: yearFromValue(dateMatch[1] || profile.graduationDate || profile.graduationYear || "")
    };
  }

  function normalizeDegreeName(value) {
    const text = normalize(value);

    if (/bachelor/.test(text)) {
      return /science|computer science|statistics/.test(text) ? "Bachelor of Science" : "Bachelor's Degree";
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

  function normalizeFieldOfStudy(value) {
    const text = normalize(value);

    if (/computer science/.test(text)) {
      return "Computer Science";
    }

    if (/statistics/.test(text)) {
      return "Statistics";
    }

    return compactText(String(value || "").replace(/&/g, "and"));
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
    const expectedDateFills = experiences.reduce((total, experience) => (
      total + (experienceDateValue(experience, "start") ? 2 : 0) + (!experience.currentRole && experienceDateValue(experience, "end") ? 2 : 0)
    ), 0);

    filled += fillWorkdayExperienceDateInputsByOrder(experiences);

    if (expectedDateFills > 0 && filled >= expectedDateFills) {
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

  function nearestSectionHeadingText(element) {
    const headings = Array.from(document.querySelectorAll("h1, h2, h3, h4, [role='heading'], div, span"))
      .filter(isVisibleElement)
      .filter((item) => {
        const text = normalize(item.innerText || item.textContent || "");
        return /^(education|websites?|work experience|employment|certifications?|languages?|social network urls?)$/.test(text);
      })
      .filter((heading) => followsNode(heading, element))
      .sort((left, right) => topOfElement(right) - topOfElement(left));

    return normalize(headings[0]?.innerText || headings[0]?.textContent || sectionTextAround(element));
  }

  function signatureValue(profile) {
    return [profile.fullName, todayDateValue({ padded: false })].filter(Boolean).join(" ");
  }

  function todayDateValue(options = {}) {
    const date = new Date();
    const month = date.getMonth() + 1;
    const day = date.getDate();
    const padded = options.padded !== false;

    return [
      padded ? String(month).padStart(2, "0") : String(month),
      padded ? String(day).padStart(2, "0") : String(day),
      String(date.getFullYear())
    ].join("/");
  }

  function bestOptionValue(field, value) {
    if (!hasValue(value) || !field.options?.length) {
      return "";
    }

    const constrainedValue = optionConstrainedValue(field.options, value);
    if (constrainedValue) {
      return constrainedValue;
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
    const normalizedValue = normalizedMappingValue(field, value);

    if (field.options?.length && !hasValue(normalizedValue)) {
      return null;
    }

    if (!field.options?.length && isOptionLikeField(field)) {
      return null;
    }

    return {
      index: field.index,
      value: normalizedValue,
      source,
      confidence
    };
  }

  function isOptionLikeField(field) {
    return field.tag === "select"
      || field.tag === "button"
      || field.type === "combobox"
      || /listbox|combobox/i.test([field.type, field.ariaLabel, field.surroundingText].join(" "))
      || /selectwidget|selectshowall/i.test(field.dataAutomationId || "");
  }

  function normalizedMappingValue(field, value) {
    const policyValue = normalizedPolicyValue(field, value);
    if (policyValue !== null) {
      return bestOptionValue(field, policyValue) || policyValue;
    }

    const exactOption = bestOptionValue(field, value);
    if (field.options?.length) {
      return exactOption || "";
    }

    return value;
  }

  function normalizedPolicyValue(field, value) {
    const haystack = fullFieldHaystack(field);
    const desired = normalize(value);

    if (/(sponsor|sponsorship|visa|h-?1b|f-?1|opt|cpt|tn|ead|work permit)/.test(haystack)) {
      return normalizeSponsorshipValue(value);
    }

    if (/(relocation assistance|need relocation assistance|relocation support)/.test(haystack)) {
      return normalizeRelocationAssistanceValue(value);
    }

    if (isWorkEligibilityQuestion(haystack)) {
      if (/(not authorized|not eligible|unauthorized|cannot work)/.test(desired)) {
        return "No";
      }

      if (/(authorized|eligible|legally|yes|any employer|green card|permanent resident|citizen)/.test(desired)) {
        return "Yes";
      }
    }

    return null;
  }

  function normalizeRelocationAssistanceValue(value) {
    const text = normalize(value);

    if (
      text === "yes"
      || /(need|require|request|want).*(relocation assistance|relocation support|assistance|support)/.test(text)
    ) {
      return "Yes";
    }

    if (
      text === "no"
      || /(do not|don t|will not|would not|not).*(need|require|request|want).*(relocation assistance|relocation support|assistance|support)/.test(text)
      || /open to relocation|open to relocate|willing to relocate|anywhere|nationwide/.test(text)
    ) {
      return "No";
    }

    return value;
  }

  function normalizeSponsorshipValue(value) {
    const text = normalize(value);

    if (
      text === "no"
      || /(do not|don t|will not|would not|won t|not).*(require|need).*(sponsor|visa|work permit)/.test(text)
      || /not require sponsorship|no sponsorship/.test(text)
    ) {
      return "No";
    }

    if (
      text === "yes"
      || /(require|need).*(sponsor|visa|work permit)/.test(text)
      || /(h-?1b|f-?1|opt|cpt|tn|ead)/.test(text)
    ) {
      return "Yes";
    }

    return value;
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
      const candidate = normalizeMappingForField(mapping, fields);

      if (!hasValue(candidate.value)) {
        continue;
      }

      if (shouldKeepExistingMapping(existing, candidate, fields)) {
        continue;
      }

      if (!existing || Number(candidate.confidence || 0) >= Number(existing.confidence || 0)) {
        byIndex.set(mapping.index, candidate);
      }
    }

    return Array.from(byIndex.values());
  }

  function shouldKeepExistingMapping(existing, candidate, fields) {
    if (!existing || !hasValue(existing.value)) {
      return false;
    }

    const field = fields.find((item) => item.index === existing.index);
    const haystack = field ? fullFieldHaystack(field) : "";
    const existingValue = normalize(existing.value);
    const candidateValue = normalize(candidate.value);

    if (
      /(relocation assistance|need relocation assistance|relocation support)/.test(haystack)
      && /^(yes|no)$/.test(existingValue)
      && !/^(yes|no)$/.test(candidateValue)
    ) {
      return true;
    }

    return false;
  }

  function normalizeMappingForField(mapping, fields) {
    const field = resolveFieldForMapping(mapping, fields);

    if (!field) {
      return mapping;
    }

    return {
      ...mapping,
      value: normalizedMappingValue(field, mapping.value)
    };
  }

  function resolveFieldForMapping(mapping, fields) {
    const indexed = fields.find((item) => item.index === mapping.index);

    if (indexed && mappingMatchesField(mapping, indexed)) {
      return indexed;
    }

    const exact = fields.find((field) => mappingMatchesField(mapping, field));
    if (exact) {
      return exact;
    }

    return mappingHasIdentity(mapping) ? null : indexed;
  }

  function mappingMatchesField(mapping, field) {
    if (!field) {
      return false;
    }

    if (mapping.id && field.id && mapping.id === field.id) {
      return true;
    }

    if (mapping.name && field.name && mapping.name === field.name) {
      return true;
    }

    const mappingLabel = normalize(cleanDisplayLabel(mapping.label || ""));
    const fieldLabel = normalize(displayLabelForField(field));

    if (mappingLabel && fieldLabel && mappingLabel === fieldLabel) {
      if (mapping.tag && field.tag && mapping.tag !== field.tag) {
        return false;
      }

      if (mapping.type && field.type && mapping.type !== field.type && !compatibleFieldTypes(mapping.type, field.type)) {
        return false;
      }

      return true;
    }

    return false;
  }

  function mappingHasIdentity(mapping) {
    return Boolean(mapping.label || mapping.name || mapping.id || mapping.ariaLabel || mapping.placeholder);
  }

  function compatibleFieldTypes(left, right) {
    const pair = new Set([left, right].map((item) => String(item || "").toLowerCase()));
    return pair.has("text") && (pair.has("input") || pair.has("textbox"));
  }

  function serializeFields(fields) {
    return fields.map(({ elementRef, choiceRefs, ...field }) => field);
  }

  function shouldSkipMappingForExistingCoreField(mapping, field, element) {
    if (!isAiMapping(mapping) || !isCoreProfileField(field)) {
      return false;
    }

    const current = compactText(getCurrentValue(element));
    if (!current || isPlaceholderValue(current)) {
      return false;
    }

    return !valueMatches(current, mapping.value) && !optionMatches(current, "", mapping.value);
  }

  function isAiMapping(mapping) {
    return /llm|ai|policy/.test(normalize(mapping.source || ""));
  }

  function isCoreProfileField(field) {
    const haystack = fullFieldHaystack(field);
    return [
      /\b(first|middle|last|preferred|full|legal)\s+name\b/,
      /\bname\s+(first|middle|last|preferred|full|legal)\b/,
      /^name$/,
      /\bemail\b|\be-mail\b/,
      /\bphone\b|\bmobile\b|\bcell\b|\btelephone\b/,
      /\baddress\b|\bstreet\b|\bcity\b|\bstate\b|\bprovince\b|\bpostal\b|\bzip\b|\bcountry\b/
    ].some((pattern) => pattern.test(haystack));
  }

  function isPlaceholderValue(value) {
    return /^(select one|select|choose|none selected|no selection|mm\/yyyy|yyyy|mm\/dd\/yyyy|type here|\s*)$/i.test(String(value || "").trim());
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
      const current = getCurrentValue(element);
      if (valueMatches(current, mapping.value) || optionMatches(current, "", mapping.value)) {
        return false;
      }

      return fillCombobox(element, mapping.value);
    }

    if (field.options?.length && dropdownTrigger(element)) {
      const current = getCurrentValue(element);
      if (valueMatches(current, mapping.value) || optionMatches(current, "", mapping.value)) {
        return false;
      }

      const filled = await fillCombobox(element, mapping.value);
      if (filled) {
        return true;
      }

      return false;
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
    const choiceOptions = (field.choiceRefs || [])
      .map((ref) => ref.deref())
      .filter(Boolean)
      .map((choice) => ({ label: choiceLabel(choice), value: choiceValue(choice) }));
    const constrainedValue = optionConstrainedValue(choiceOptions, desiredValue);
    const values = Array.isArray(desiredValue)
      ? desiredValue
      : String(constrainedValue || desiredValue).split(/\s*[;,]\s*/).filter(Boolean);
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
    const previousValue = getCurrentValue(trigger);
    const mustClickOption = requiresDropdownOptionClick();

    trigger.focus();
    trigger.click();
    await sleep(250);

    const initialOptions = visibleOptionElements(trigger);
    const optionValue = optionConstrainedValue(optionsFromElements(initialOptions), desiredValue) || desiredValue;
    const option = await waitForMatchingDropdownOption(trigger, optionValue);

    if (option) {
      clickOption(option);
      dispatchFormEvents(trigger);
      dispatchFormEvents(element);
      return true;
    }

    if (trigger.tagName.toLowerCase() !== "button") {
      setEditableText(trigger, dropdownSearchValue(optionValue));
      await sleep(250);

      const typedOptions = visibleOptionElements(trigger);
      const typedOptionValue = optionConstrainedValue(optionsFromElements(typedOptions), desiredValue) || optionValue;
      const typedOption = await waitForMatchingDropdownOption(trigger, typedOptionValue);

      if (typedOption) {
        clickOption(typedOption);
        dispatchFormEvents(trigger);
        dispatchFormEvents(element);
        return true;
      }

      if (!typedOption) {
        dispatchEnterOrEscape(trigger, "ArrowDown");
        await sleep(250);
        const arrowOption = await waitForMatchingDropdownOption(trigger, typedOptionValue);

        if (arrowOption) {
          clickOption(arrowOption);
          dispatchFormEvents(trigger);
          dispatchFormEvents(element);
          return true;
        }
      }

      if (!mustClickOption && optionMatches(getCurrentValue(trigger), "", typedOptionValue)) {
        confirmFilledElement(trigger);
        await sleep(250);
        if (optionMatches(getCurrentValue(trigger), "", typedOptionValue)) {
          dispatchFormEvents(element);
          return true;
        }
      }

      dispatchFormEvents(trigger);
      dispatchFormEvents(element);
      if (!valueMatches(getCurrentValue(trigger), previousValue)) {
        setEditableText(trigger, previousValue);
      }
      return false;
    }

    dispatchFormEvents(trigger);
    dispatchFormEvents(element);
    return false;
  }

  function dropdownSearchValue(desiredValue) {
    return String(desiredValue || "").trim();
  }

  function requiresDropdownOptionClick() {
    return /greenhouse\.io|boards\.greenhouse|job-boards\.greenhouse|oraclecloud\.com|taleo\.net|icims\.com|smartrecruiters\.com|successfactors\.[a-z.]+|jobs\.sap\.com/i.test(location.hostname);
  }

  async function waitForMatchingDropdownOption(trigger, desiredValue, attempts = 10) {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const option = visibleOptionElements(trigger).find((item) => optionMatches(
        item.getAttribute("data-automation-label") || item.textContent || item.getAttribute("aria-label") || "",
        item.getAttribute("data-automation-label") || item.getAttribute("data-value") || item.getAttribute("value") || "",
        desiredValue
      ));

      if (option) {
        return option;
      }

      await sleep(150);
    }

    return null;
  }

  function visibleOptionElements(trigger = null) {
    const containers = [];
    const controls = trigger?.getAttribute?.("aria-controls");
    const owns = trigger?.getAttribute?.("aria-owns");

    for (const id of [controls, owns].filter(Boolean)) {
      const container = document.getElementById(id);
      if (container) {
        containers.push(container);
      }
    }

    containers.push(document);

    return uniqueElements(containers.flatMap((container) => (
      Array.from(container.querySelectorAll(DROPDOWN_OPTION_SELECTOR))
    )))
      .filter(isVisibleElement);
  }

  function uniqueElements(elements) {
    return Array.from(new Set(elements));
  }

  function optionsFromElements(elements) {
    return elements.map((item) => ({
      label: item.getAttribute("data-automation-label") || item.textContent || item.getAttribute("aria-label") || "",
      value: item.getAttribute("data-automation-label") || item.getAttribute("data-value") || item.getAttribute("value") || ""
    }));
  }

  function optionConstrainedValue(options, value) {
    if (!Array.isArray(options) || !options.length) {
      return "";
    }

    const yesNo = yesNoOptions(options);
    if (!yesNo) {
      return "";
    }

    const semantic = semanticYesNoValue(value);
    if (!semantic) {
      return "";
    }

    return semantic === "yes" ? yesNo.yes : yesNo.no;
  }

  function yesNoOptions(options) {
    const usable = options
      .map((option) => ({
        label: compactText(option.label || ""),
        value: compactText(option.value || "")
      }))
      .filter((option) => {
        const text = normalize([option.label, option.value].join(" "));
        return text && !/select one|choose|please select/.test(text);
      });
    const yes = usable.find((option) => optionMatches(option.label, option.value, "Yes"));
    const no = usable.find((option) => optionMatches(option.label, option.value, "No"));

    return yes && no
      ? { yes: yes.label || yes.value, no: no.label || no.value }
      : null;
  }

  function semanticYesNoValue(value) {
    const text = normalize(value);

    if (!text) {
      return "";
    }

    if (/^(no|false|n|0)$/.test(text) || /\b(no|not|never|decline|unable|cannot|won t|would not|do not|don t)\b/.test(text)) {
      return "no";
    }

    if (/^(yes|true|y|1)$/.test(text) || /\b(open|willing|able|can|agree|consent|authorized|eligible)\b/.test(text)) {
      return "yes";
    }

    return "";
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

  async function fillWorkdayEducationDropdownFallback(profile) {
    if (!/education/i.test(document.body?.innerText || "")) {
      return 0;
    }

    const education = normalizedEducation(profile)[0];
    if (!education) {
      return 0;
    }

    let filled = 0;
    const degree = education.degree || "Bachelor of Science";
    const degreeControl = findWorkdayDropdownByLabel(/^degree\s*\*?$/i);

    if (degreeControl && !valueMatches(getCurrentValue(degreeControl), degree)) {
      filled += await fillCombobox(degreeControl, degree) ? 1 : 0;
    }

    const fieldControl = findWorkdayDropdownByLabel(/^(field of study|discipline|major)\s*\*?$/i);
    const fields = preferredEducationFields(education);

    for (const field of fields) {
      if (!fieldControl) {
        break;
      }

      const current = normalize(getCurrentValue(fieldControl));
      if (current && optionMatches(current, "", field)) {
        continue;
      }

      filled += await fillCombobox(fieldControl, field) ? 1 : 0;

      if (!isMultiSelectDropdown(fieldControl)) {
        break;
      }
    }

    return filled;
  }

  function preferredEducationFields(education) {
    const desired = normalize(education.fieldOfStudy || "");
    const values = [];

    if (/computer science/.test(desired)) {
      values.push("Computer Science");
    }

    if (/statistics/.test(desired)) {
      values.push("Statistics");
    }

    if (!values.length && education.fieldOfStudy) {
      values.push(education.fieldOfStudy);
    }

    if (!values.includes("Computer Science")) {
      values.push("Computer Science");
    }

    if (!values.includes("Statistics")) {
      values.push("Statistics");
    }

    return values;
  }

  function isMultiSelectDropdown(control) {
    const text = normalize(getCurrentValue(control) || control.innerText || control.textContent || "");
    const multi = normalize([
      control.getAttribute("aria-label"),
      control.getAttribute("data-automation-id"),
      getSurroundingText(control)
    ].join(" "));

    return /\d+\s+items?\s+selected|multi|multiple/.test(`${text} ${multi}`);
  }

  function findWorkdayDropdownByLabel(pattern) {
    const labels = Array.from(document.querySelectorAll("label, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel'], span, div"))
      .filter(isVisibleElement)
      .filter((item) => pattern.test(compactText(item.innerText || item.textContent || "")))
      .sort((left, right) => topOfElement(left) - topOfElement(right));

    for (const label of labels) {
      const control = findDropdownNearLabel(label);
      if (control) {
        return control;
      }
    }

    return null;
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
        "no i have never",
        "no i do not have",
        "no i don t have",
        "no i have never served",
        "i have never served",
        "have never served",
        "have not served",
        "never served",
        "not served",
        "not a protected veteran",
        "i am not a protected veteran",
        "not protected veteran",
        "not a veteran",
        "not hispanic or latino",
        "not hispanic",
        "not latino",
        "none",
        "none of the above",
        "no affiliation",
        "no affiliations",
        "not affiliated",
        "not a member"
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

    if (/authorized.*work/.test(desired)) {
      [
        "i am authorized to work in the united states for any employer",
        "authorized to work in the united states for any employer",
        "legally authorized to work in the united states",
        "authorized to work for any employer",
        "authorized to work"
      ].forEach((alias) => aliases.add(alias));
    }

    if (/do not require sponsorship|not require sponsorship|no sponsorship/.test(desired)) {
      [
        "no",
        "i do not require sponsorship",
        "do not require sponsorship",
        "i will not require sponsorship",
        "will not require sponsorship",
        "no sponsorship"
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

    if (desired === "canada 1" || desired === "canada +1" || desired === "+1" || desired === "1 item selected canada 1") {
      [
        "canada",
        "canada 1",
        "canada +1",
        "canada plus 1",
        "+1",
        "1 canada",
        "canada country code 1"
      ].forEach((alias) => aliases.add(normalize(alias)));
    }

    if (desired === "heterosexual" || desired === "heterosexual straight" || desired === "straight") {
      [
        "heterosexual",
        "heterosexual straight",
        "heterosexual / straight",
        "straight"
      ].forEach((alias) => aliases.add(normalize(alias)));
    }

    if (/\bbachelor/.test(desired)) {
      [
        "bachelor of science",
        "bachelors of science",
        "bs",
        "bsc",
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
    const keyCodes = {
      Enter: 13,
      Escape: 27,
      ArrowDown: 40,
      ArrowUp: 38,
      Space: 32
    };
    const keyCode = keyCodes[key] || 0;
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
