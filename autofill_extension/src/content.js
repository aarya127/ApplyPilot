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

  const CONSENT_CONTAINER_SELECTOR = [
    "[id*='onetrust' i]",
    "[class*='onetrust' i]",
    "[id*='ot-sdk' i]",
    "[class*='ot-sdk' i]",
    "[id*='cookie' i]",
    "[class*='cookie-banner' i]",
    "[class*='cookie-consent' i]",
    "[class*='consent' i]",
    "[id*='consent' i]",
    "[aria-label*='cookie' i]",
    "[id*='truste' i]",
    "[class*='truste' i]",
    "[class*='cky-consent' i]",
    "[id*='cky-consent' i]",
    "[id*='usercentrics' i]",
    "[class*='usercentrics' i]",
    "[id*='osano' i]",
    "[class*='osano' i]",
    "[id*='didomi' i]",
    "[class*='didomi' i]",
    "[id*='qc-cmp' i]",
    "[class*='qc-cmp' i]"
  ].join(",");

  // Normalized (lowercase, punctuation stripped) so they can be matched against normalize() output.
  const CONSENT_BANNER_PHRASES = [
    "when you visit any website it may store or retrieve information",
    "we and our partners use cookies",
    "this website uses cookies",
    "we use cookies and similar technologies"
  ];

  const MAX_FIELD_LABEL_LENGTH = 300;

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

    if (message?.type === "GET_PAGE_FIELD_CONTEXT") {
      sendResponse({ ok: true, context: buildPageFieldContext(scanFields()) });
      return false;
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
      manualTasks: buildManualTasks(unmappedFields, plan.profile),
      page: {
        url: location.href,
        title: document.title,
        context: buildPageFieldContext(plan.fields)
      }
    };
  }

  function buildManualTasks(unmappedFields, profile) {
    const willAttachResume = findResumeFileInputs().some((input) => !input.files?.length);
    const tasks = unmappedFields
      .filter((field) => field.needsManualUpload)
      .filter((field) => !(willAttachResume && isResumeContextText(field.label)))
      .map((field) => ({
        index: field.index,
        label: field.label,
        task: "Upload resume manually",
        resumeFileName: profile.resumeFileName || ""
      }));

    if (willAttachResume) {
      tasks.push({
        index: -1,
        label: "Resume/CV",
        task: "Attach resume automatically",
        automatic: true,
        resumeFileName: profile.resumeFileName || ""
      });
    }

    return tasks;
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
    const canonicalMappings = buildCanonicalMappings(fields, profile, settings || {});
    const profileContactMappings = buildProfileContactMappings(fields, profile, settings || {});
    const structuralPolicyMappings = buildStructuralPolicyMappings(fields, profile);
    const localMappings = fields.map((field) => mapField(field, profile, settings || {})).filter(Boolean);
    const repeatableMappings = [
      ...mapRepeatableEmploymentFields(fields, profile),
      ...mapRepeatableEducationFields(fields, profile),
      ...mapRepeatableWebsiteFields(fields, profile)
    ];
    const backendMappings = settings?.autoMapAmbiguousFields === true
      ? await getBackendMappings(fields.filter((field) => !isAiOnlyField(field)), profile, fields)
      : [];
    const mappings = mergeMappings([...canonicalMappings, ...profileContactMappings, ...structuralPolicyMappings, ...localMappings, ...repeatableMappings], backendMappings, fields)
      .concat(buildRawStructuralPolicyMappings(fields, profile))
      .filter((mapping, index, all) => all.findIndex((item) => item.index === mapping.index) === index)
      .filter((mapping) => !isAlreadyCorrectlyFilledMapping(mapping, fields));

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
    mappings = reindexMappingsByIdentity(mappings, fields)
      .map((mapping) => normalizeMappingForField(mapping, fields))
      .filter((mapping) => hasValue(mapping.value));
    let filled = 0;
    const failures = [];
    const attached = [];

    try {
      if (await fillWorkdayTargetCountry(profile, settings || {})) {
        filled += 1;
        await sleep(700);
        fields.length = 0;
        fields.push(...scanFields());
        await enrichDynamicDropdownOptions(fields);
        hydrateFieldsFromPreview(fields);
        mappings = mergeMappings(
          reindexMappingsByIdentity(mappings, fields),
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

        if (isAlreadyCorrectlyFilledElement(element, mapping, field)) {
          continue;
        }

        try {
          const didFill = await fillElement(element, mapping, field);
          if (didFill) {
            confirmFilledElement(element);
            markFilled(element, mapping);
            filled += 1;
          } else if (field.type !== "file" && !isFillSatisfied(element, mapping, field)) {
            // A silent false return with a value to fill means the control rejected it
            // (e.g. no dropdown option matched); surface it instead of dropping it.
            failures.push({
              index: mapping.index,
              label: displayLabelForField(field),
              error: field.tag === "select" || field.options?.length || isOptionLikeField(field)
                ? "no matching option"
                : "fill failed"
            });
          }
        } catch (error) {
          failures.push({ index: mapping.index, label: field.label, error: error.message });
        }
      }

      filled += await fillWorkdayExperienceDateFallback(profile);
      filled += await fillWorkdayEducationDropdownFallback(profile);
      filled += await fillWorkdayHearAboutUsFallback(profile);
      filled += await fillWorkdayAddressFallback(profile, settings || {});

      const resumeAttach = await attachResumeToResumeInputs();
      filled += resumeAttach.attached.length;
      failures.push(...resumeAttach.failures);
      attached.push(...resumeAttach.attached);
    } finally {
      state.isApplying = false;
    }

    const verification = verifyFilledMappings(mappings, fields);

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
      failures,
      attached,
      verification
    };
  }

  function verifyFilledMappings(mappings, fields) {
    const results = [];

    for (const mapping of mappings) {
      const field = resolveFieldForMapping(mapping, fields);
      const element = field?.elementRef?.deref?.();
      if (!field || !element || !isFillable(element)) {
        results.push(verificationRecord(mapping, field, "", "unreadable"));
        continue;
      }

      const actual = getCurrentValue(element);
      const status = mappingValueMatchesField(actual, mapping.value) ? "matched" : "mismatch";
      results.push(verificationRecord(mapping, field, actual, status));
    }

    return {
      matched: results.filter((item) => item.status === "matched").length,
      mismatched: results.filter((item) => item.status === "mismatch").slice(0, 25),
      unreadable: results.filter((item) => item.status === "unreadable").slice(0, 25)
    };
  }

  function verificationRecord(mapping, field, actual, status) {
    return {
      index: mapping.index,
      label: field ? displayLabelForField(field) : mapping.label || `Field ${mapping.index + 1}`,
      fieldKind: mapping.fieldKind || "",
      expected: mapping.value,
      actual,
      status
    };
  }

  function mappingValueMatchesField(actual, expected) {
    return valueMatches(actual, expected) || optionMatches(actual, "", expected);
  }

  const RESUME_FILE_CONTEXT_PATTERN = /resume|\bcv\b|curriculum vitae/i;
  const NON_RESUME_FILE_CONTEXT_PATTERN = /cover\s*letter|transcript|portfolio|\bother\b/i;

  async function attachResumeToResumeInputs() {
    const inputs = findResumeFileInputs();

    if (!inputs.length) {
      return { attached: [], failures: [] };
    }

    // Never replace a file the user already chose.
    const pending = inputs.filter((input) => !input.files?.length);

    if (!pending.length) {
      return { attached: [], failures: [] };
    }

    let payload;
    try {
      payload = await fetchResumeFileFromBackend();
    } catch (error) {
      return { attached: [], failures: [{ label: "Resume/CV", error: error.message }] };
    }

    const file = new File(
      [base64ToUint8Array(payload.bytes)],
      payload.filename || "resume.pdf",
      { type: payload.mimeType || "application/pdf" }
    );
    const attached = [];
    const failures = [];

    for (const input of pending) {
      try {
        const transfer = new DataTransfer();
        transfer.items.add(file);
        input.files = transfer.files;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        attached.push({ label: "Resume/CV", filename: file.name });
      } catch (error) {
        failures.push({ label: "Resume/CV", error: error.message });
      }
    }

    return { attached, failures };
  }

  function findResumeFileInputs() {
    // Resume inputs are often visually hidden behind an "Attach" button (Greenhouse),
    // so unlike scanFields this deliberately does not require visibility.
    return Array.from(document.querySelectorAll("input[type='file']"))
      .filter((input) => !input.disabled)
      .filter(isResumeFileInput);
  }

  function isResumeFileInput(input) {
    // The input's own context (attributes + label) wins over surrounding headings, so a
    // "Cover Letter" input inside a "Resume/CV" section is still skipped.
    for (const text of fileInputContextTexts(input)) {
      if (!text) {
        continue;
      }

      if (NON_RESUME_FILE_CONTEXT_PATTERN.test(text)) {
        return false;
      }

      if (RESUME_FILE_CONTEXT_PATTERN.test(text)) {
        return true;
      }
    }

    return false;
  }

  function isResumeContextText(text) {
    const value = String(text || "");
    return RESUME_FILE_CONTEXT_PATTERN.test(value) && !NON_RESUME_FILE_CONTEXT_PATTERN.test(value);
  }

  function fileInputContextTexts(input) {
    const ownText = compactText([
      input.getAttribute("name") || "",
      input.id || "",
      input.getAttribute("aria-label") || "",
      getLabelText(input)
    ].join(" "));
    const container = input.closest("section, fieldset");
    const containerHeading = container
      ? compactText(container.querySelector("h1, h2, h3, h4, h5, h6, legend, [role='heading']")?.textContent || "")
      : "";
    const headingText = compactText([containerHeading, precedingHeadingText(input)].join(" "));

    return [ownText, headingText];
  }

  function precedingHeadingText(input) {
    const headings = Array.from(document.querySelectorAll("h1, h2, h3, h4, h5, h6, legend, [role='heading']"))
      .filter((heading) => heading.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING);

    const nearest = headings[headings.length - 1];
    return compactText(nearest?.textContent || "");
  }

  async function fetchResumeFileFromBackend() {
    let response;
    try {
      response = await chrome.runtime.sendMessage({ type: "FETCH_RESUME_FILE" });
    } catch (error) {
      throw new Error(`Could not reach the extension background: ${error.message}`);
    }

    if (!response?.ok || !response.bytes) {
      throw new Error(response?.error || "The backend did not return a resume file.");
    }

    return response;
  }

  function base64ToUint8Array(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);

    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }

    return bytes;
  }

  function scanFields() {
    const elements = Array.from(document.querySelectorAll(FIELD_SELECTOR))
      .filter(isFillable)
      .filter((element) => !isJunkFieldElement(element));
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

    // Drop fields whose derived text is actually an open dropdown's option list (e.g. a
    // phone Country combobox whose expanded listbox "244 results found ... Afghanistan+93 ..."
    // leaked into the label). These are scanning artifacts, not askable fields.
    return fields.filter((field) => !isOptionListArtifactField(field));
  }

  function isJunkFieldElement(element) {
    return isInsideConsentUi(element)
      || isStandaloneSearchElement(element)
      || isInsideOpenOptionList(element);
  }

  // An element that is itself an option list (role=listbox/option) or that lives inside an
  // open combobox popup is a transient product of opening a dropdown, not a real form field.
  function isInsideOpenOptionList(element) {
    const role = (element.getAttribute("role") || "").toLowerCase();
    if (role === "listbox" || role === "option") {
      return true;
    }

    const popup = element.closest?.(
      "[role='listbox'], [class*='menu' i], [class*='listbox' i], [class*='options' i]"
    );
    if (!popup || popup === element) {
      return false;
    }

    // Only discard when the popup genuinely holds option nodes, so ordinary fields wrapped in
    // "menu"-styled containers are never dropped.
    const looksLikeOptionList = (popup.getAttribute("role") || "").toLowerCase() === "listbox"
      || Boolean(popup.querySelector?.(DROPDOWN_OPTION_SELECTOR));
    return looksLikeOptionList;
  }

  // Matches labels that are really an open dropdown's option list rather than a question,
  // e.g. "244 results foundNo results found" or a long run of country dialing codes.
  function isOptionListArtifactField(field) {
    const text = compactText([field.label, field.ariaLabel, field.placeholder].join(" "));
    if (!text) {
      return false;
    }

    if (/\bresults? found\b/i.test(text) || /\bno results? found\b/i.test(text)) {
      return true;
    }

    const dialingCodes = text.match(/\+\d{1,4}/g);
    return Boolean(dialingCodes && dialingCodes.length >= 5);
  }

  function isInsideConsentUi(element) {
    if (element.closest?.(CONSENT_CONTAINER_SELECTOR)) {
      return true;
    }

    const dialog = element.closest?.("[role='dialog'], [role='alertdialog'], dialog");
    if (!dialog) {
      return false;
    }

    const dialogText = normalize((dialog.innerText || dialog.textContent || "").slice(0, 1500));
    return CONSENT_BANNER_PHRASES.some((phrase) => dialogText.includes(phrase));
  }

  function isStandaloneSearchElement(element) {
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();

    if (type === "search" || role === "searchbox") {
      return true;
    }

    const accessibleLabel = normalize([
      element.getAttribute("aria-label") || "",
      element.getAttribute("placeholder") || "",
      element.getAttribute("name") || "",
      element.id || ""
    ].filter(Boolean).join(" "));

    return /^search( search)*$/.test(accessibleLabel);
  }

  function capFieldLabel(value) {
    const text = compactText(value);
    return text.length > MAX_FIELD_LABEL_LENGTH ? text.slice(0, MAX_FIELD_LABEL_LENGTH).trimEnd() : text;
  }

  function buildFieldMetadata(element, index) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || element.getAttribute("role") || tag).toLowerCase();
    const options = getOptions(element);
    const rawLabel = getLabelText(element);
    const nearbyText = nearbyTextForField(element, options);
    const questionText = policyQuestionTextForField(element, options);
    const recoveredLabel = needsSimpleLabelRecovery(rawLabel, element)
      ? simpleFieldLabelFor(element) || parentQuestionLabel(element, options) || precedingQuestionLabel(element, options) || questionText || firstPolicyQuestionLine(nearbyText) || rawLabel
      : isLowInformationText(rawLabel)
        ? parentQuestionLabel(element, options) || precedingQuestionLabel(element, options) || questionText || firstPolicyQuestionLine(nearbyText) || rawLabel
      : rawLabel;

    return {
      index,
      tag,
      type,
      name: element.getAttribute("name") || "",
      id: element.id || "",
      label: capFieldLabel(recoveredLabel),
      placeholder: element.getAttribute("placeholder") || "",
      ariaLabel: element.getAttribute("aria-label") || "",
      ariaAutocomplete: element.getAttribute("aria-autocomplete") || "",
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
      label: capFieldLabel(label),
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

  function needsSimpleLabelRecovery(rawLabel, element) {
    const text = normalize([
      rawLabel,
      element.getAttribute("placeholder") || "",
      element.getAttribute("aria-label") || "",
      element.getAttribute("name") || "",
      element.id || ""
    ].join(" "));

    return !compactText(rawLabel)
      || isLowInformationText(rawLabel)
      || /enter name|name as per national identifier|national identifier/.test(text);
  }

  function simpleFieldLabelFor(element) {
    const parentLabel = parentSimpleFieldLabel(element);
    if (parentLabel) {
      return parentLabel;
    }

    return precedingSimpleFieldLabel(element);
  }

  function parentSimpleFieldLabel(element) {
    let current = element.parentElement;
    let depth = 0;

    while (current && current !== document.body && depth < 6) {
      const controlCount = current.querySelectorAll?.("input:not([type='hidden']), textarea, select, button, [role='combobox']").length || 0;
      if (controlCount > 1) {
        current = current.parentElement;
        depth += 1;
        continue;
      }

      const clone = current.cloneNode(true);
      clone.querySelectorAll("input, textarea, select, button, option, [role='radio'], [role='checkbox'], [role='combobox']").forEach((node) => node.remove());
      const lines = (clone.innerText || clone.textContent || "")
        .split(/\n+/)
        .map((line) => compactText(line))
        .filter(Boolean);
      const label = lines.find(isSimpleFieldLabelLine);

      if (label) {
        return cleanSimpleFieldLabel(label);
      }

      current = current.parentElement;
      depth += 1;
    }

    return "";
  }

  function precedingSimpleFieldLabel(element) {
    const targetRect = element.getBoundingClientRect();
    const candidates = Array.from(document.querySelectorAll("label, legend, p, div, span, strong, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel']"))
      .filter(isVisibleElement)
      .filter((node) => node !== element && followsNode(node, element))
      .flatMap((node) => simpleFieldLabelCandidatesNear(node, targetRect, element))
      .sort((left, right) => left.distance - right.distance);

    return candidates[0]?.text || "";
  }

  function simpleFieldLabelCandidatesNear(node, targetRect, targetElement) {
    const rect = node.getBoundingClientRect();
    const text = compactText(node.innerText || node.textContent || "");
    const controlCount = node.querySelectorAll?.("input, textarea, select, button, [role='radio'], [role='checkbox'], [role='combobox']").length || 0;

    if (targetElement && node.contains?.(targetElement) && controlCount > 1) {
      return [];
    }

    if (!text || text.length > 600 || controlCount > 3 || rect.bottom < targetRect.top - 180 || rect.top > targetRect.bottom + 24) {
      return [];
    }

    const distance = rect.bottom <= targetRect.top
      ? targetRect.top - rect.bottom
      : rect.top >= targetRect.bottom
        ? rect.top - targetRect.bottom + 40
        : 0;

    return text
      .split(/\n+/)
      .map((line) => compactText(line))
      .filter(isSimpleFieldLabelLine)
      .map((line) => ({
        text: cleanSimpleFieldLabel(line),
        distance
      }));
  }

  function isSimpleFieldLabelLine(line) {
    const normalized = normalize(line).replace(/\s+/g, " ").trim();
    return /^(legal\s+)?(first|middle|last|preferred|given|family)\s+name\s*:?\*?$/.test(normalized)
      || /^(email|e-mail|phone|phone number|mobile|linkedin|linkedin url|linkedin profile|linked in url|linked in profile|github|github url|github link|git hub link|portfolio|website|personal website|personal site|location|current location|city|state|province|country|postal code|zip code|graduation date|graduation year|grad date)\s*:?\*?$/.test(normalized);
  }

  function cleanSimpleFieldLabel(line) {
    return compactText(String(line || "").replace(/\s*:\s*$/, ""));
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
        const labelledByText = compactText(labelledBy?.innerText || labelledBy?.textContent || "");
        if (labelledByText) {
          pieces.push(labelledByText);
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
      const formGroupControlCount = formGroup?.querySelectorAll?.("input:not([type='hidden']), textarea, select, button, [role='combobox']")?.length || 0;
      if (formGroup?.innerText && formGroupControlCount <= 1) {
        pieces.push(firstMeaningfulLine(formGroup.innerText));
      }
    }

    const precedingSimple = precedingSimpleFieldLabel(element);
    if (precedingSimple && pieces.length === 0) {
      pieces.unshift(precedingSimple);
    }

    return compactText(unique(pieces).join(" "));
  }

  function shouldPreferPrecedingSimpleLabel(currentLabel, precedingLabel) {
    const current = normalize(currentLabel);

    if (!current || current === normalize(precedingLabel)) {
      return true;
    }

    if (isLowInformationText(currentLabel) || /enter name|name as per national identifier|national identifier/.test(current)) {
      return true;
    }

    return current.length > 80 || current.split(" ").length > 8;
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
      || /list/i.test(field.ariaAutocomplete || "")
      || /selectwidget|selectshowall/i.test(field.dataAutomationId || "")
      || field.ariaLabel
      || /(\blocation\b|\bcity\b|\bcountry\b|\bstate\b|\bprovince\b|phone.*code|country.*phone|select one)/i.test([field.label, field.placeholder, field.name, field.id, field.ariaLabel, field.ariaAutocomplete, field.surroundingText].join(" "))
      || /listbox|combobox/i.test([field.type, field.ariaLabel, field.ariaAutocomplete, field.surroundingText].join(" "));
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

      if (!options.length) {
        openDropdownWithPointer(trigger);
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

  // React-select style widgets open on mousedown on the control, not on a synthetic
  // click(), so a full pointer sequence (which bubbles up to the control) is needed to
  // reveal their options when a plain click and ArrowDown did nothing.
  function openDropdownWithPointer(trigger) {
    clickOption(trigger);
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
    const autocomplete = (element.getAttribute("aria-autocomplete") || "").toLowerCase();
    const automationId = (element.getAttribute("data-automation-id") || "").toLowerCase();
    const tag = element.tagName.toLowerCase();
    const className = (element.getAttribute("class") || "").toLowerCase();
    return role === "combobox"
      || popup === "listbox"
      || popup === "true"
      || autocomplete === "list"
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

  const FIELD_KIND = {
    FIRST_NAME: "identity.first_name",
    LAST_NAME: "identity.last_name",
    FULL_NAME: "identity.full_name",
    EMAIL: "contact.email",
    PHONE: "contact.phone",
    LINKEDIN: "links.linkedin",
    GITHUB: "links.github",
    PORTFOLIO: "links.portfolio",
    CURRENT_EMPLOYER: "work.current_or_previous_employer",
    CURRENT_TITLE: "work.current_or_previous_job_title",
    SCHOOL: "education.school",
    DEGREE: "education.degree",
    FIELD_OF_STUDY: "education.field_of_study",
    ADDRESS_LINE1: "address.line1",
    ADDRESS_LINE2: "address.line2",
    CITY: "address.city",
    STATE: "address.state",
    POSTAL: "address.postal_code",
    COUNTRY: "address.country"
  };

  function buildCanonicalMappings(fields, profile, settings) {
    return fields
      .map((field) => {
        if (isScopedRepeatableDetailField(field)) {
          return null;
        }

        const kind = classifyFieldKind(field);
        if (!kind) {
          return null;
        }

        const value = answerForFieldKind(kind, field, profile, settings);
        if (!hasValue(value)) {
          return null;
        }

        const mapping = buildMapping(field, value, "field-kind", 0.97);
        return mapping ? { ...mapping, fieldKind: kind } : null;
      })
      .filter(Boolean);
  }

  function buildProfileContactMappings(fields, profile, settings) {
    return fields
      .map((field) => (
        isScopedRepeatableDetailField(field)
          || isAmbiguousRepeatableLocationDateLabel(primaryFieldHaystack(field))
          || shouldSkipField(primaryFieldHaystack(field))
          ? null
          : mapProfileContactField(field, profile, settings, primaryFieldHaystack(field))
      ))
      .filter(Boolean);
  }

  function buildStructuralPolicyMappings(fields, profile) {
    return fields
      .map((field) => {
        const primary = primaryFieldHaystack(field);
        const rawKey = `${field.name || ""} ${field.id || ""} ${field.autocomplete || ""}`;

        if (/work[\s_-]*authorization/.test(primary) || /work[_-]?authorization/i.test(rawKey)) {
          return buildMapping(field, workAuthorizationAnswer(field, profile), "rule", 0.9);
        }

        if (/canadian[\s_-]*citizen|citizen[\s_-]*of[\s_-]*canada|canada[\s_-]*citizenship/.test(primary) || /canadian[_-]?citizen/i.test(rawKey)) {
          return buildMapping(field, profile.canadianCitizen || profile.answers?.canadianCitizen || "Yes", "rule", 0.9);
        }

        if (/(u[\s._-]*s|united states).*(permanent resident|green card)|us[\s_-]*permanent[\s_-]*resident/.test(primary) || /us[_-]?permanent[_-]?resident/i.test(rawKey)) {
          return buildMapping(field, profile.usPermanentResident || profile.answers?.usPermanentResident || "Yes", "rule", 0.9);
        }

        return null;
      })
      .filter(Boolean);
  }

  function buildRawStructuralPolicyMappings(fields, profile) {
    return fields
      .map((field) => {
        const key = `${field.name || ""} ${field.id || ""} ${field.autocomplete || ""}`;
        let value = "";

        const normalizedKey = normalize(key);

        if ((/work/i.test(key) && /authorization/i.test(key)) || (/work/.test(normalizedKey) && /authorization/.test(normalizedKey))) {
          value = profile.workAuthorization || profile.answers?.workAuthorization || "Yes";
        } else if ((/canadian/i.test(key) && /citizen/i.test(key)) || (/canadian/.test(normalizedKey) && /citizen/.test(normalizedKey))) {
          value = profile.canadianCitizen || profile.answers?.canadianCitizen || "Yes";
        } else if ((/us/i.test(key) && /permanent/i.test(key) && /resident/i.test(key)) || (/us/.test(normalizedKey) && /permanent/.test(normalizedKey) && /resident/.test(normalizedKey))) {
          value = profile.usPermanentResident || profile.answers?.usPermanentResident || "Yes";
        }

        return hasValue(value)
          ? { index: field.index, value: compactText(value), source: "rule", confidence: 0.9, ...mappingIdentityForField(field) }
          : null;
      })
      .filter(Boolean);
  }

  function classifyFieldKind(field) {
    const primary = primaryFieldHaystack(field);
    const full = fullFieldHaystack(field);

    if (isRepeatableEmploymentDetailField(field, primary, full)) {
      return "";
    }

    if (/\bif yes\b|\bif applicable\b|last assigned/.test(primary)) {
      return "";
    }

    if (/\bcookie|provider linkedin|consent to cookies|marketing consent|privacy preferences/.test(primary)) {
      return "";
    }

    if (isWorkOrEducationIdentityField(primary)) {
      if (/employer|company/.test(primary)) {
        return FIELD_KIND.CURRENT_EMPLOYER;
      }
      if (/job title|title|position|role/.test(primary)) {
        return FIELD_KIND.CURRENT_TITLE;
      }
      if (/school|university|college|education/.test(primary)) {
        return FIELD_KIND.SCHOOL;
      }
    }

    if (/(\bmiddle\b.*\bname\b|mname|second last name|second surname|additional last name)/.test(primary)) {
      return "";
    }
    if (/(\bfirst\b.*\bname\b|\bgiven\b.*\bname\b|fname)/.test(primary)) {
      return FIELD_KIND.FIRST_NAME;
    }
    if (/(\blast\b.*\bname\b|\bfamily\b.*\bname\b|lname|surname)/.test(primary)) {
      return FIELD_KIND.LAST_NAME;
    }
    if (/(\bfull\b.*\bname\b|\blegal name\b|^name$|first and last name)/.test(primary)) {
      return FIELD_KIND.FULL_NAME;
    }
    if (isEmailProfileField(primary)) {
      return FIELD_KIND.EMAIL;
    }
    if (isPhoneNumberField(primary)) {
      return FIELD_KIND.PHONE;
    }
    if (isLinkedinProfileField(primary)) {
      return FIELD_KIND.LINKEDIN;
    }
    if (isGithubProfileField(primary)) {
      return FIELD_KIND.GITHUB;
    }
    if (isPortfolioProfileField(primary) || /^website\s*\*?$/.test(primary)) {
      return FIELD_KIND.PORTFOLIO;
    }
    if (isGenericStandaloneUrlField(primary)) {
      return "";
    }

    if (/(school|university|college|institution)/.test(primary) && !/(website|url|link)/.test(full)) {
      return FIELD_KIND.SCHOOL;
    }
    if (/(degree|qualification)/.test(primary)) {
      return FIELD_KIND.DEGREE;
    }
    if (/(field of study|discipline|major|program)/.test(primary)) {
      return FIELD_KIND.FIELD_OF_STUDY;
    }

    if (/(address line 1|address 1|street address|street)/.test(primary)) {
      return FIELD_KIND.ADDRESS_LINE1;
    }
    if (/(address line 2|address 2|apt|apartment|suite|unit)/.test(primary)) {
      return FIELD_KIND.ADDRESS_LINE2;
    }
    if (/(location city|city location|^city\b|\bcity$)/.test(primary)) {
      return FIELD_KIND.CITY;
    }
    if (/(what|which).{0,20}\bu\.?s\.?\s*state\b|state.{0,60}(currently reside|current residence)|currently reside.{0,60}\bstate\b|\bstate\b|\bprovince\b|region/.test(primary)) {
      return FIELD_KIND.STATE;
    }
    if (/(postal code|postcode|zip code|\bzip\b)/.test(primary)) {
      return FIELD_KIND.POSTAL;
    }
    if (/(currently reside|current residence|country.*reside|country region|country\/region|\bcountry\b)/.test(primary) && !/(phone|code)/.test(primary)) {
      return FIELD_KIND.COUNTRY;
    }

    return "";
  }

  function isRepeatableEmploymentDetailField(field, primary, full) {
    if (isWorkOrEducationIdentityField(primary)) {
      return false;
    }

    const isEmploymentDetail = isGenericRepeatableEmploymentDetailLabel(primary);
    const hasEmploymentContext = /(my experience|work experience|employment|work history|professional experience|job history)/.test(full);
    return isEmploymentDetail && (hasEmploymentContext || isStrictRepeatableEmploymentDetailLabel(primary));
  }

  function isGenericRepeatableEmploymentDetailLabel(primary) {
    return /(^|\b)(company|company name|employer|job title|title|position|role|location|from|to|start date|end date|month|year|current role|currently work|description|role description|responsibilities|achievements)\b/.test(primary);
  }

  function isStrictRepeatableEmploymentDetailLabel(primary) {
    return /^(company|company name|employer|job title|title|position|role|location i currently work here|location month|location year|from|to|from month|from year|to month|to year|start date|end date|start date month|start date year|end date month|end date year|month|year|current role|currently work here|i currently work here|description|role description|responsibilities|achievements)\s*\*?$/.test(primary);
  }

  function isGenericStandaloneUrlField(primary) {
    return /^(url|link)\s*\*?$/.test(primary);
  }

  function isGenericRowDetailFieldForScopedMappers(field, primary, full) {
    if (isGenericStandaloneUrlField(primary)) {
      return true;
    }

    if (isRepeatableEmploymentDetailField(field, primary, full)) {
      return true;
    }

    if (isAmbiguousRepeatableLocationDateLabel(primary)) {
      return true;
    }

    if (isProfileContactOrLocationField(primary)) {
      return false;
    }

    return /(location month|location year|from month|from year|to month|to year|start date month|start date year|end date month|end date year|role description|current role|currently work here|i currently work here)/.test(primary)
      || /^(location\s+)?(month|year)\b/.test(primary);
  }

  function isScopedRepeatableDetailField(field) {
    const primary = primaryFieldHaystack(field);
    const element = field.elementRef?.deref?.();
    const sectionHeading = element ? normalize(`${nearestExplicitSectionHeadingText(element)} ${nearestSectionHeadingText(element)}`) : "";
    const name = normalize(field.name || "");

    if (/\[\]$/.test(field.name || "") || /\b(company|title|location|from|to|description|school|degree|discipline|field|url)\s*\[\]/.test(name)) {
      return true;
    }

    if (/(my experience|work experience|employment|work history|professional experience|job history)/.test(sectionHeading)) {
      return isGenericRepeatableEmploymentDetailLabel(primary);
    }

    if (/education|school|university/.test(sectionHeading)) {
      return /(school|university|institution|degree|field of study|discipline|major|qualification|actual or expected|^from\b|^to\b|start.*year|end.*year|graduation)/.test(primary);
    }

    if (/websites?|urls?|links?/.test(sectionHeading)) {
      return /(url|link|website)/.test(primary);
    }

    return false;
  }

  function answerForFieldKind(kind, field, profile, settings) {
    const address = selectAddress(profile, settings, primaryFieldHaystack(field)) || {};
    const education = normalizedEducation(profile)[0] || {};

    switch (kind) {
      case FIELD_KIND.FIRST_NAME:
        return profile.firstName;
      case FIELD_KIND.LAST_NAME:
        return profile.lastName;
      case FIELD_KIND.FULL_NAME:
        return profile.fullName || [profile.firstName, profile.lastName].filter(Boolean).join(" ");
      case FIELD_KIND.EMAIL:
        return profile.email;
      case FIELD_KIND.PHONE:
        return profile.phone;
      case FIELD_KIND.LINKEDIN:
        return profile.linkedin;
      case FIELD_KIND.GITHUB:
        return profile.github;
      case FIELD_KIND.PORTFOLIO:
        return profile.portfolio || profile.website || profile.personalWebsite;
      case FIELD_KIND.CURRENT_EMPLOYER:
        return profile.currentOrPreviousEmployer
          || profile.currentEmployer
          || profile.previousEmployer
          || profile.answers?.currentOrPreviousEmployer
          || firstResumeExperienceValue(profile, ["company", "employer", "organization"]);
      case FIELD_KIND.CURRENT_TITLE:
        return profile.currentOrPreviousJobTitle
          || profile.currentJobTitle
          || profile.previousJobTitle
          || profile.answers?.currentOrPreviousJobTitle
          || firstResumeExperienceValue(profile, ["title", "role", "position"]);
      case FIELD_KIND.SCHOOL:
        return profile.school || education.school;
      case FIELD_KIND.DEGREE:
        return profile.degree || education.degree;
      case FIELD_KIND.FIELD_OF_STUDY:
        return profile.fieldOfStudy || education.fieldOfStudy;
      case FIELD_KIND.ADDRESS_LINE1:
        return address.line1;
      case FIELD_KIND.ADDRESS_LINE2:
        return address.line2;
      case FIELD_KIND.CITY:
        return address.city;
      case FIELD_KIND.STATE:
        return stateNameOrValue(address.state || address.province || "");
      case FIELD_KIND.POSTAL:
        return address.postalCode || address.zipCode;
      case FIELD_KIND.COUNTRY:
        return address.country;
      default:
        return "";
    }
  }

  function primaryFieldHaystack(field) {
    return normalize(
      [
        field.label,
        field.placeholder,
        field.name,
        field.id,
        field.ariaLabel,
        field.autocomplete
      ].join(" ")
    );
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

    if (isLinkedinProfileField(primaryHaystack) || /\blinkedin\b|linked\s*in/.test(primaryHaystack)) {
      return hasValue(profile.linkedin) ? buildMapping(field, profile.linkedin, "rule", 0.92) : null;
    }

    if (isGithubProfileField(primaryHaystack) || /\bgithub\b|git\s*hub/.test(primaryHaystack)) {
      return hasValue(profile.github) ? buildMapping(field, profile.github, "rule", 0.92) : null;
    }

    if (isScopedRepeatableDetailField(field)) {
      return null;
    }

    if (isGenericRowDetailFieldForScopedMappers(field, primaryHaystack, fullHaystack)) {
      return null;
    }

    const dependentNoDetailMapping = mapDependentNoDetailField(field, fullHaystack);
    if (dependentNoDetailMapping) {
      return dependentNoDetailMapping;
    }

    if (
      isPhoneCountryCodeField(primaryHaystack)
      || isGreenhousePhoneCountryField(field, primaryHaystack, phoneContextHaystack)
      || isGreenhouseBarePhoneCountryFallbackField(field, primaryHaystack)
    ) {
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

    if (/work[\s_-]*authorization/.test(primaryHaystack)) {
      return buildMapping(field, workAuthorizationAnswer(field, profile), "rule", 0.9);
    }

    const profileContactMapping = mapProfileContactField(field, profile, settings, primaryHaystack);
    if (profileContactMapping) {
      return profileContactMapping;
    }

    const savedAnswer = findSavedAnswer(field, profile);
    if (hasValue(savedAnswer)) {
      return buildMapping(field, savedAnswer, "saved-answer", 0.95);
    }

    const workQuestionMapping = mapWorkQuestion(field, profile, haystack);
    if (workQuestionMapping) {
      return workQuestionMapping;
    }

    if (
      isDebarmentOrProgramExclusionQuestion(haystack)
      || isGovernmentEmploymentQuestion(haystack)
      || isProfessionalDisciplineQuestion(haystack)
    ) {
      const complianceMapping = mapCommonAtsQuestion(field, profile, haystack);
      if (complianceMapping) {
        return complianceMapping;
      }
    }

    if (isCompanyHistoryQuestion(haystack)) {
      return field.tag === "select"
        ? buildMapping(field, profile.answers?.previouslyEmployedByCompany || "No", "rule", 0.88)
        : null;
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

    const companyQuestionMapping = mapCompanyQuestion(field, profile, haystack);
    if (companyQuestionMapping) {
      return companyQuestionMapping;
    }

    const governmentFormMapping = mapGovernmentSelfIdField(field, profile, primaryHaystack, fullHaystack);
    if (governmentFormMapping) {
      return governmentFormMapping;
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
      [/(linkedin|linked in)/, profile.linkedin],
      [/(github|git hub)/, profile.github],
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

    if (hasSponsorshipTerms(haystack)) {
      return buildMapping(field, profile.needsSponsorship || profile.answers?.sponsorship || "No", "rule", 0.88);
    }

    if (/(within|located).*(50 miles|seattle|boston|washington dc|austin)/.test(haystack)) {
      return buildMapping(field, profile.answers?.withinListedOfficeRadius || "No", "rule", 0.86);
    }

    if (/(overall result|grade point average|\bgpa\b|\bcgpa\b|academic average)/.test(haystack)) {
      const gpa = gpaAnswer(profile);
      return gpa ? buildMapping(field, gpa, "rule", 0.9) : null;
    }

    if (/(review.*linked document|candidate privacy policy|privacy policy|linked document)/.test(haystack)) {
      return buildMapping(field, profile.answers?.reviewedPrivacyPolicy || "Yes", "rule", 0.82);
    }

    if (/(how did you hear about us|how did you hear about this|how did you hear about.*job|source.*application|application source|where did you hear)/.test(haystack)) {
      return buildMapping(field, profile.answers?.applicationSource || "LinkedIn", "rule", 0.86);
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

    return null;
  }

  function mapProfileContactField(field, profile, settings, haystack) {
    if (isAmbiguousRepeatableLocationDateLabel(haystack)) {
      return null;
    }

    if (isBareExperiencePageLocationField(field, haystack)) {
      return null;
    }

    if (isLinkedinProfileField(haystack) || /\blinkedin\b|linked\s*in/.test(haystack)) {
      return hasValue(profile.linkedin) ? buildMapping(field, profile.linkedin, "rule", 0.92) : null;
    }

    if (isGithubProfileField(haystack) || /\bgithub\b|git\s*hub/.test(haystack)) {
      return hasValue(profile.github) ? buildMapping(field, profile.github, "rule", 0.92) : null;
    }

    if (isPortfolioProfileField(haystack) || /\bportfolio\b|personal\s+(website|site)/.test(haystack)) {
      const value = profile.portfolio || profile.website || profile.personalWebsite;
      return hasValue(value) ? buildMapping(field, value, "rule", 0.92) : null;
    }

    if (isPhoneNumberField(haystack)) {
      return hasValue(profile.phone) ? buildMapping(field, profile.phone, "rule", 0.92) : null;
    }

    if (isEmailProfileField(haystack)) {
      return hasValue(profile.email) ? buildMapping(field, profile.email, "rule", 0.92) : null;
    }

    if (isAuthorizedCountriesField(haystack)) {
      return buildMapping(field, profile.answers?.authorizedCountries || "Canada and United States", "rule", 0.9);
    }

    if (/^location\b|location city|city location/.test(haystack)) {
      const address = selectAddress(profile, settings, haystack);
      const applicationLocation = selectApplicationLocation(profile, settings, address || {});
      const value = locationAnswerForField(field, applicationLocation, address || {})
        || profile.location;
      return hasValue(value) ? buildMapping(field, value, "rule", 0.9) : null;
    }

    return null;
  }

  function isLinkedinProfileField(haystack) {
    return /(linkedin|linked in).*(url|profile)?|(url|profile).*(linkedin|linked in)/.test(haystack)
      && !/(cookie|consent|provider)/.test(haystack);
  }

  function isGithubProfileField(haystack) {
    return /(github|git hub).*(url|profile)?|(url|profile).*(github|git hub)/.test(haystack);
  }

  function isPortfolioProfileField(haystack) {
    return /(portfolio|personal website|personal site|website url)/.test(haystack);
  }

  function isEmailProfileField(haystack) {
    return /(email|e-mail)/.test(haystack)
      && !/(linkedin|linked in|github|git hub|phone|location|website|portfolio)/.test(haystack);
  }

  function isAuthorizedCountriesField(haystack) {
    return /\b(in\s+)?(what|which|list|specify|identify|provide).{0,50}\b(country|countries)\b.{0,120}\b(legally\s+)?(permitted|authorized|eligible)\b.{0,80}\bwork\b/.test(haystack)
      || /\b(country|countries)\b.{0,80}\b(legally\s+)?(permitted|authorized|eligible)\b.{0,80}\bwork\b/.test(haystack);
  }

  function isPhoneCountryCodeField(haystack) {
    return /country.*phone.*code|phone.*country.*code|country code|phone\s+country|country\s+phone/.test(haystack);
  }

  function isGreenhouseHost() {
    return /greenhouse\.io|boards\.greenhouse|job-boards\.greenhouse/i.test(location.hostname);
  }

  function isGreenhousePhoneCountryField(field, primaryHaystack, fullHaystack) {
    const label = normalize(field.label || "");
    return (/^country\s*(required)?\s*\*?$|^required\s+country\s*\*?$/.test(primaryHaystack) || /^country\s*\*?$/.test(label))
      && isGreenhouseHost()
      && !/(currently reside|current residence|country.*reside|country region|country\/region|location|city)/.test(primaryHaystack)
      && (!field.options?.length || hasPhoneCountryCodeOptions(field.options));
  }

  function isGreenhousePhoneCountryCodeLikeField(field) {
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
    const fullHaystack = fullFieldHaystack(field);
    return isPhoneCountryCodeField(fullHaystack)
      || isGreenhousePhoneCountryField(field, primaryHaystack, fullHaystack);
  }

  function isGreenhouseBarePhoneCountryFallbackField(field, primaryHaystack) {
    const label = normalize(field.label || "");
    return isGreenhouseHost()
      && (/^country\s*(required)?\s*\*?$|^required\s+country\s*\*?$/.test(primaryHaystack) || /^country\s*\*?$/.test(label))
      && !/(currently reside|current residence|country.*reside|country region|country\/region|location|city)/.test(primaryHaystack)
      && (!field.options?.length || hasPhoneCountryCodeOptions(field.options));
  }

  function isNearPhoneNumberField(field) {
    const element = field.elementRef?.deref?.();
    if (!element) {
      return false;
    }

    const rect = element.getBoundingClientRect();
    return Array.from(document.querySelectorAll("input:not([type='hidden']), textarea, select, [role='combobox'], [aria-haspopup='listbox']"))
      .filter((candidate) => candidate !== element && isVisibleElement(candidate))
      .some((candidate) => {
        const candidateText = normalize([
          getLabelText(candidate),
          candidate.getAttribute("name") || "",
          candidate.id || "",
          candidate.getAttribute("placeholder") || "",
          candidate.getAttribute("aria-label") || ""
        ].join(" "));
        if (!/phone|mobile|telephone/.test(candidateText)) {
          return false;
        }

        const candidateRect = candidate.getBoundingClientRect();
        return Math.abs(candidateRect.top - rect.top) < 120
          || Math.abs(candidateRect.left - rect.right) < 320
          || Math.abs(rect.left - candidateRect.right) < 320;
      });
  }

  function hasPhoneCountryCodeOptions(options = []) {
    return options.some((option) => /\+\s*\d{1,4}|\(\s*\+\s*\d{1,4}\s*\)/.test(`${option.label || ""} ${option.value || ""}`));
  }

  function phoneCountryCodeAnswer(field, profile) {
    const explicit = profile.answers?.phoneCountryCode || profile.phoneCountryCode || "+1";
    const options = field.options || [];
    const canadaOption = options.find((option) => {
      const text = normalize(`${option.label || ""} ${option.value || ""}`);
      return /\bcanada\b/.test(text) && /(^|\s|\()\+?1(\)|\s|$)/.test(text);
    });

    return canadaOption ? (canadaOption.label || canadaOption.value) : normalizePhoneCountryFallback(explicit);
  }

  function normalizePhoneCountryFallback(value) {
    const text = normalize(value);
    if (/canada|\+?1/.test(text)) {
      return "+1";
    }

    return value;
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

    if (isCoreProfileField(field)) {
      return "";
    }

    if (isLowInformationChoiceLabel(field) && !savedAnswerQuestionContext(field)) {
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

  function savedAnswerQuestionContext(field) {
    return firstMeaningfulQuestion([
      field.questionText,
      field.surroundingText,
      field.nearbyText,
      field.placeholder,
      field.name,
      field.id
    ].join("\n"));
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
      field.ariaAutocomplete,
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
      hasSponsorshipTerms,
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
      || /work[\s_-]*authorization|proof of authorization|legally eligible/.test(haystack);
  }

  function hasSponsorshipTerms(haystack) {
    return hasWorkAuthorizationAssistanceTerms(haystack)
      || /\b(sponsor|sponsorship|visa|work permit)\b/.test(haystack)
      || /\b(h-?1b|f-?1|opt|cpt|tn|ead)\b/.test(haystack);
  }

  function hasWorkAuthorizationAssistanceTerms(haystack) {
    return /\b(require|need|request|want|seek|seeking).{0,80}\b(assistance|help|support).{0,80}\b(work authorization|employment authorization|work permit)\b/.test(haystack)
      || /\b(assistance|help|support).{0,80}\b(work authorization|employment authorization|work permit).{0,80}\b(now|future|later)\b/.test(haystack);
  }

  function shouldAskForField(field) {
    const haystack = fieldHaystack(field);

    if (!haystack) {
      return false;
    }

    if (isSearchWidgetField(field)) {
      return false;
    }

    if (isAiOnlyQuestion(haystack)) {
      return true;
    }

    if (shouldSkipField(haystack)) {
      return false;
    }

    if (!hasMeaningfulAskableLabel(field)) {
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

  function isSearchWidgetField(field) {
    if (field.type === "search" || field.type === "searchbox") {
      return true;
    }

    return normalize(cleanDisplayLabel(displayLabelForField(field))) === "search";
  }

  function isGenericPlaceholderLabel(text) {
    return /^((checkbox|radio|input|select|text|field|button|option)\s*)?label$/.test(text)
      || /^(option|choice|item)\s*\d*$/.test(text);
  }

  function hasMeaningfulAskableLabel(field) {
    return [field.label, field.ariaLabel, field.placeholder, field.questionText, field.name, field.id]
      .map((value) => normalize(cleanDisplayLabel(value || "")))
      .some((text) => text && !isGenericPlaceholderLabel(text) && !/^[a-z]{2,}\d{2,}$/.test(text));
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
    if (hasSponsorshipTerms(haystack)) {
      return buildMapping(field, sponsorshipAnswer(field, profile), "rule", 0.9);
    }

    if (isAuthorizedCountriesField(primaryFieldHaystack(field))) {
      return buildMapping(field, profile.answers?.authorizedCountries || "Canada and United States", "rule", 0.9);
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

    if (isRelocationOwnCostQuestion(haystack)) {
      return buildMapping(field, profile.answers?.relocateAtOwnCost || "Yes", "rule", 0.88);
    }

    if (/(served|service).*(u\.?s\.?|united states).*(military|armed forces)|military service/.test(haystack)) {
      return buildMapping(field, profile.answers?.militaryService || profile.militaryService || "No", "rule", 0.88);
    }

    if (/(spouse|domestic partner).*(served|service).*(military|armed forces)/.test(haystack)) {
      return buildMapping(field, profile.answers?.spouseMilitaryService || "No", "rule", 0.88);
    }

    if (/(veteran|protected veteran|armed forces|military service)/.test(haystack)) {
      return buildMapping(field, profile.veteranStatus || profile.answers?.veteranStatus || "No", "rule", 0.88);
    }

    if (isDebarmentOrProgramExclusionQuestion(haystack)) {
      return buildMapping(field, profile.answers?.governmentProgramExclusion || "No", "rule", 0.88);
    }

    if (isGovernmentEmploymentQuestion(haystack)) {
      return buildMapping(field, profile.answers?.priorGovernmentEmployment || "No", "rule", 0.88);
    }

    if (isProfessionalDisciplineQuestion(haystack)) {
      return buildMapping(field, profile.answers?.professionalDiscipline || "No", "rule", 0.88);
    }

    if (isFamilyOrRelationshipConflictQuestion(haystack)) {
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

  function isRelocationOwnCostQuestion(haystack) {
    return /relocat/.test(haystack)
      && /(own cost|own expense|without relocation assistance|no relocation assistance|assistance is not offered|assistance not offered|not offered|at your cost)/.test(haystack)
      && /(willing|able|would you|are you|can you)/.test(haystack);
  }

  function gpaAnswer(profile) {
    const education = normalizedEducation(profile)[0] || {};
    return profile.gpa
      || profile.answers?.gpa
      || profile.answers?.overallResult
      || education.gpa
      || education.overallResult
      || "";
  }

  function isDebarmentOrProgramExclusionQuestion(haystack) {
    return /(excluded|exclusion|debarred|debarment|suspended|ineligible).{0,140}(federal|state|health care|healthcare|medicare|medicaid|government|procurement|program)/.test(haystack)
      || /(federal|state|health care|healthcare|medicare|medicaid|government|procurement|program).{0,140}(excluded|exclusion|debarred|debarment|suspended|ineligible)/.test(haystack);
  }

  function isGovernmentEmploymentQuestion(haystack) {
    return /(employed|employment|worked).{0,120}(federal|state|local government|government entity|civil service|va hospital|military)/.test(haystack)
      || /(federal|state|local government|government entity|civil service|va hospital|military).{0,120}(employed|employment|worked)/.test(haystack);
  }

  function isProfessionalDisciplineQuestion(haystack) {
    return /(disciplinary action|discipline|fines?|citations?|penalties|reprimands?|reprovals?|probation|practice restrictions?|revocation|surrender|suspension).{0,180}(professional license|license|certification|credential)/.test(haystack)
      || /(professional license|license|certification|credential).{0,180}(disciplinary action|discipline|fines?|citations?|penalties|reprimands?|reprovals?|probation|practice restrictions?|revocation|surrender|suspension)/.test(haystack);
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
    if (!options.length) {
      return explicit || "Open to relocation";
    }

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
    if (/(whatsapp|sms|text messages?|messaging)/.test(haystack)
      && /(recruit|hiring|talent acquisition|job opportunit|consent|receive|opt.?in|communicat|follow.?up)/.test(haystack)) {
      return buildMapping(field, profile.answers?.recruitingMessages || "No", "rule", 0.9);
    }

    if (/(subscribe|subscription|email alerts?|job alerts?|marketing emails?|promotional emails?|newsletter|mailing list)/.test(haystack)) {
      return buildMapping(field, profile.answers?.subscribeEmails || "No", "rule", 0.9);
    }

    return null;
  }

  function mapDependentNoDetailField(field, haystack) {
    if (isDependentNoDetailQuestion(haystack)) {
      return buildMapping(field, "N/A", "rule", 0.86);
    }

    return null;
  }

  function isDependentNoDetailQuestion(haystack) {
    const asksForDetails = /(if yes|if applicable|please enter|please provide|please state|state their name|provide their name|name and department|name department|details?)/.test(haystack);
    return asksForDetails && (isFamilyOrRelationshipConflictQuestion(haystack) || isCompanyAffiliationQuestion(haystack));
  }

  function isFamilyOrRelationshipConflictQuestion(haystack) {
    return /(relatives?|family members?|spouse|domestic partner|close personal relationship|significant other|parent|sibling|child)/.test(haystack);
  }

  function isCompanyAffiliationQuestion(haystack) {
    return /(authorized dealer|dealer|contractor|affiliate|subsidiary|business unit|vendor|supplier|partner company)/.test(haystack);
  }

  function mapGovernmentSelfIdField(field, profile, primaryHaystack, fullHaystack) {
    if (!isCc305DisabilityFormContext(contextForGovernmentSelfId(field, fullHaystack))) {
      return null;
    }

    const label = normalize(field.label || "");

    if (/(employee id|employee number|worker id)/.test(primaryHaystack)) {
      return null;
    }

    if (/^name\s*\*?$|^full name\s*\*?$/.test(label) || /^name\s*(required)?$|^full name\s*(required)?$/.test(primaryHaystack)) {
      return hasValue(profile.fullName) ? buildMapping(field, profile.fullName, "rule", 0.9) : null;
    }

    if (/^date\s*\*?$|^today s date\s*\*?$/.test(label) || /^date\s*(required)?$|^today s date\s*(required)?$/.test(primaryHaystack)) {
      return buildMapping(field, todayDateValue(), "rule", 0.9);
    }

    return null;
  }

  function isCc305DisabilityFormContext(haystack) {
    return /(cc-?305|omb control number 1250-0005|voluntary self-identification of disability|self identification of disability|please check one of the boxes below)/.test(haystack);
  }

  function contextForGovernmentSelfId(field, fullHaystack) {
    const element = field.elementRef?.deref?.();
    const section = element?.closest?.("section, fieldset, form, [role='group'], div");
    return normalize([
      fullHaystack,
      section?.innerText || section?.textContent || "",
      element ? nearestSectionHeadingText(element) : "",
      element ? nearestExplicitSectionHeadingText(element) : ""
    ].join(" "));
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

    if (isAmbiguousRepeatableLocationDateLabel(haystack)) {
      return null;
    }

    if (isBareExperiencePageLocationField(field, haystack)) {
      return null;
    }

    if (isEmploymentField(field, haystack)) {
      return null;
    }

    if (/(work remotely|remote location|plan to work remote)/.test(haystack)) {
      return null;
    }

    const applicationLocation = selectApplicationLocation(profile, settings, address);

    if (isGreenhouseApplicationLocationField(field, haystack)) {
      const location = applicationLocation.full
        || [applicationLocation.city, applicationLocation.region].filter(Boolean).join(", ")
        || applicationLocation.city;
      return hasValue(location) ? buildMapping(field, location, "rule", 0.92) : null;
    }

    if (/(current location|location city|city location|current city|where.*city|where.*located|where.*live)/.test(haystack)) {
      const location = locationAnswerForField(field, applicationLocation, address);
      return hasValue(location) ? buildMapping(field, location, "rule", 0.9) : null;
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

  function isBareExperiencePageLocationField(field, haystack) {
    const visibleLabel = normalize(field?.label || displayLabelForField(field) || "");
    if (!/^location\s*\*?$/.test(visibleLabel)) {
      return false;
    }

    const full = fullFieldHaystack(field);
    if (/(location city|city location|current city|where.*city|currently reside|current residence|city.*(state|region|country)|required.*city)/.test(full)) {
      return false;
    }

    const pageText = normalize(document.body?.innerText || "");
    const element = field.elementRef?.deref?.();
    const section = element ? normalize(`${nearestExplicitSectionHeadingText(element)} ${nearestSectionHeadingText(element)}`) : "";

    return /(my experience|work experience|employment|work history|professional experience|job history)/.test(`${section} ${pageText}`);
  }

  function isGreenhouseApplicationLocationField(field, haystack) {
    return /greenhouse\.io|boards\.greenhouse|job-boards\.greenhouse/i.test(location.hostname)
      && /(location city|city location|current city|where.*city)/.test(haystack)
      && !/(phone|country code|country.*phone|currently reside|current residence|country.*reside)/.test(haystack);
  }

  function selectApplicationLocation(profile, settings, address) {
    const answers = profile.answers || {};
    const target = settings?.targetCountry || targetCountryFromAddress(address);
    const cityKey = target === "usa" ? "usaCity" : target === "canada" ? "canadaCity" : "";
    const locationKey = target === "usa" ? "usaLocation" : target === "canada" ? "canadaLocation" : "";
    const full = preferredApplicationLocation(profile, answers, target, locationKey);
    const city = preferredApplicationCity(profile, answers, target, cityKey) || cityFromLocation(full) || address.city || "";
    const region = (full && regionFromLocation(full)) || address.state || address.province || "";

    return { city, region, full };
  }

  function preferredApplicationLocation(profile, answers, target, locationKey) {
    if (locationKey) {
      const explicit = answers[locationKey] || profile[locationKey];
      if (hasValue(explicit)) {
        return explicit;
      }
    }

    if (target === "usa") {
      return answers.usaPreferredLocation
        || profile.usaPreferredLocation
        || answers.usPreferredLocation
        || profile.usPreferredLocation
        || "";
    }

    if (target === "canada") {
      return answers.canadaPreferredLocation
        || profile.canadaPreferredLocation
        || "";
    }

    return profile.applicationLocation || "";
  }

  function preferredApplicationCity(profile, answers, target, cityKey) {
    if (cityKey) {
      const explicit = answers[cityKey] || profile[cityKey];
      if (hasValue(explicit)) {
        return explicit;
      }
    }

    if (target === "usa") {
      return answers.usaPreferredCity
        || profile.usaPreferredCity
        || answers.usPreferredCity
        || profile.usPreferredCity
        || "";
    }

    if (target === "canada") {
      return answers.canadaPreferredCity
        || profile.canadaPreferredCity
        || "";
    }

    return "";
  }

  function targetCountryFromAddress(address) {
    const country = normalize(address?.country || "");
    if (/united states|usa|u s/.test(country)) {
      return "usa";
    }

    if (/canada/.test(country)) {
      return "canada";
    }

    return "";
  }

  function locationAnswerForField(field, applicationLocation, address) {
    const primary = primaryFieldHaystack(field);
    const disambiguated = applicationLocation.full
      || [applicationLocation.city, applicationLocation.region, address.country].filter(Boolean).join(", ");

    if (applicationLocation.full && /^location\b/.test(primary) && !/\bcity\b/.test(primary)) {
      return applicationLocation.full;
    }

    if (locationFieldNeedsDisambiguation(field)) {
      return disambiguated || applicationLocation.city;
    }

    return applicationLocation.city || disambiguated;
  }

  function locationFieldNeedsDisambiguation(field) {
    const haystack = fullFieldHaystack(field);
    return Boolean(field.options?.length)
      || field.tag === "select"
      || field.tag === "button"
      || field.type === "combobox"
      || /listbox|combobox/i.test([field.type, field.ariaLabel, field.ariaAutocomplete, field.surroundingText].join(" "))
      || /selectwidget|selectshowall/i.test(field.dataAutomationId || "")
      || /autocomplete|typeahead|start typing/.test(haystack);
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

    if (/(what|which).{0,20}\bu\.?s\.?\s*state\b|state.{0,60}(currently reside|current residence)|currently reside.{0,60}\bstate\b/.test(haystack)) {
      const state = address.state || address.province;
      return hasValue(state) ? buildMapping(field, stateNameOrValue(state), "rule", 0.9) : null;
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

  function stateNameOrValue(value) {
    const normalized = normalize(value);
    const stateNames = {
      al: "Alabama",
      ak: "Alaska",
      az: "Arizona",
      ar: "Arkansas",
      ca: "California",
      co: "Colorado",
      ct: "Connecticut",
      de: "Delaware",
      fl: "Florida",
      ga: "Georgia",
      hi: "Hawaii",
      id: "Idaho",
      il: "Illinois",
      in: "Indiana",
      ia: "Iowa",
      ks: "Kansas",
      ky: "Kentucky",
      la: "Louisiana",
      me: "Maine",
      md: "Maryland",
      ma: "Massachusetts",
      mi: "Michigan",
      mn: "Minnesota",
      ms: "Mississippi",
      mo: "Missouri",
      mt: "Montana",
      ne: "Nebraska",
      nv: "Nevada",
      nh: "New Hampshire",
      nj: "New Jersey",
      nm: "New Mexico",
      ny: "New York",
      nc: "North Carolina",
      nd: "North Dakota",
      oh: "Ohio",
      ok: "Oklahoma",
      or: "Oregon",
      pa: "Pennsylvania",
      ri: "Rhode Island",
      sc: "South Carolina",
      sd: "South Dakota",
      tn: "Tennessee",
      tx: "Texas",
      ut: "Utah",
      vt: "Vermont",
      va: "Virginia",
      wa: "Washington",
      wv: "West Virginia",
      wi: "Wisconsin",
      wy: "Wyoming",
      dc: "District of Columbia"
    };
    return stateNames[normalized] || value;
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

  function isGreenhouseTypedDropdownFallbackField(field) {
    if (!isGreenhouseHost()) {
      return false;
    }

    const haystack = primaryFieldHaystack(field);
    return isGreenhousePhoneCountryCodeLikeField(field)
      || /(u\.?s\.?\s*state|state.*currently reside|currently reside.*state)/.test(haystack);
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
      const sectionText = normalize(`${employmentSectionText(item)} ${nearestSectionHeadingText(item)}`);
      return /^(add|add another)$/.test(text) && /(employment|experience|work|job)/.test(sectionText);
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
    // A single-entry profile never needs "Add another" when the form already
    // renders an education block with fields — row-count miscounts on
    // Degree-only blocks would otherwise create empty rows on every run.
    if (targetCount <= 1 && educationSectionHasAnyField()) {
      return;
    }

    await ensureRowsForSection(targetCount, countEducationRows, () => findAddButtonForSection(/education|school|university/));
  }

  function educationSectionHasAnyField() {
    return scanFieldsWithoutPreparation().some((field) => {
      const element = field.elementRef?.deref?.();
      if (!element) {
        return false;
      }
      const sectionHeading = normalize(`${nearestExplicitSectionHeadingText(element)} ${nearestSectionHeadingText(element)}`);
      return /\beducation\b/.test(sectionHeading);
    });
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
      const explicitHeading = nearestExplicitSectionHeadingText(item);
      const nearestHeading = nearestSectionHeadingText(item);
      const sectionText = explicitHeading || nearestHeading || sectionTextAround(item);
      return /^(add|add another)$/.test(text) && sectionPattern.test(sectionText);
    });
  }

  function countEducationRows() {
    // Some Greenhouse boards (e.g. Databricks) render education rows with only a Degree
    // dropdown and no School field, which made a school-only count return 0 on every run
    // and re-click "Add another" each time. Count row instances as the max across every
    // per-row field kind so an existing row is always detected and adding stays idempotent.
    return Math.max(
      countFieldsMatchingInSection(/school|university/, isEducationField),
      countEducationRowFields(/\bdegree\b/),
      countEducationRowFields(/\bdiscipline\b|field of study|\bmajor\b/)
    );
  }

  function countEducationRowFields(pattern) {
    return scanFieldsWithoutPreparation()
      .filter((field) => {
        // Match on the field's own label only: sibling fields in the same row often carry
        // "Degree" in their surrounding text, which would double-count a single row.
        const primary = normalize([field.label, field.name, field.id, field.placeholder].join(" "));
        return pattern.test(primary) && isEducationField(field, primary);
      })
      .length;
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
      const fieldName = normalize(field.name || "");
      const baseName = fieldName.replace(/\[\]$/, "");

      if (!isEmploymentField(field, haystack)) {
        continue;
      }

      if (baseName === "company") {
        buckets.company.push(field);
      } else if (["title", "job title"].includes(baseName)) {
        buckets.title.push(field);
      } else if (baseName === "location") {
        buckets.location.push(field);
      } else if (baseName === "from") {
        buckets.startDate.push(field);
      } else if (baseName === "to") {
        buckets.endDate.push(field);
      } else if (baseName === "description") {
        buckets.description.push(field);
      } else if (/^company\b|\bcompany name\b|\bemployer\b/.test(haystack) && !/website|url/.test(haystack)) {
        buckets.company.push(field);
      } else if (/(^|\b)title\b|job title/.test(haystack) || (/\bposition\b/.test(haystack) && /employment|experience|work history/.test(normalize(field.surroundingText)))) {
        buckets.title.push(field);
      } else if (/^from\b|from date|date from/.test(haystack)) {
        buckets.startDate.push(field);
      } else if (/^to\b|to date|date to/.test(haystack)) {
        buckets.endDate.push(field);
      } else if (/(start date.*month|start month)/.test(haystack)) {
        buckets.startMonth.push(field);
      } else if (/(start date.*year|start year)/.test(haystack)) {
        buckets.startYear.push(field);
      } else if (/(end date.*month|end month)/.test(haystack)) {
        buckets.endMonth.push(field);
      } else if (/(end date.*year|end year)/.test(haystack)) {
        buckets.endYear.push(field);
      } else if (/current role|currently work|current position/.test(haystack)) {
        buckets.currentRole.push(field);
      } else if (/role description|description|responsibilities|achievements/.test(haystack)) {
        buckets.description.push(field);
      } else if (/\blocation\b/.test(haystack) && !/\b(month|year|current role|currently work)\b/.test(haystack)) {
        buckets.location.push(field);
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
      } else if (/^from\b|start.*year|begin.*year/.test(primaryHaystack)) {
        buckets.startYear.push(field);
      } else if (/^to\b|actual or expected|expected.*year|end.*year/.test(primaryHaystack) || (/graduation/.test(primaryHaystack) && isRepeatableEducationDateField(field, haystack))) {
        buckets.endYear.push(field);
      }
    }

    const mappings = [];
    education.forEach((item, index) => {
      addRepeatableMapping(mappings, buckets.school[index], item.school, "education");
      addRepeatableMapping(mappings, buckets.degree[index], item.degree, "education");
      addRepeatableMapping(mappings, buckets.field[index], item.fieldOfStudy, "education");
      addRepeatableMapping(mappings, buckets.startYear[index], item.startYear, "education");
      addRepeatableMapping(mappings, buckets.endYear[index], item.endYear, "education");
    });

    return mappings;
  }

  function mapRepeatableWebsiteFields(fields, profile) {
    const links = normalizedProfileLinks(profile);

    if (!links.length) {
      return [];
    }

    const urlFields = fields.filter((field) => {
      const primaryHaystack = normalize([field.label, field.name, field.id, field.placeholder, field.ariaLabel].join(" "));
      const haystack = normalize([primaryHaystack, field.surroundingText].join(" "));
      if (isWorkOrEducationIdentityField(primaryHaystack)) {
        return false;
      }

      return isWebsiteField(field, haystack) && !isProfileContactOrLocationField(primaryHaystack);
    });

    const mappings = [];
    links.forEach((url, index) => {
      addRepeatableMapping(mappings, urlFields[index], url, "website");
    });

    return mappings;
  }

  function addRepeatableMapping(mappings, field, value, source = "experience") {
    if (!field || !hasValue(value)) {
      return;
    }

    const mapping = buildMapping(field, value, source, 0.92);
    if (mapping) {
      mappings.push(mapping);
    }
  }

  function isEmploymentField(field, haystack) {
    const fullText = `${haystack} ${normalize(field.surroundingText)}`;
    const element = field.elementRef?.deref?.();
    const sectionHeading = element ? nearestExplicitSectionHeadingText(element) : "";

    if (/education|school|university/.test(sectionHeading)) {
      return false;
    }

    if (/(school|education|degree|discipline)/.test(fullText)) {
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

    const hasEmploymentContext = employmentContextForField(field, fullText);
    const isGenericEmploymentField = /(\bcompany\b|company name|employer|\btitle\b|job title|current role|currently work|start date|end date|position|\blocation\b|\bfrom\b|\bto\b|role description|responsibilities|achievements)/.test(haystack);

    if (isGenericEmploymentField && !hasEmploymentContext) {
      return false;
    }

    return /(my experience|employment|experience|work history|\bcompany\b|company name|employer|\btitle\b|job title|current role|currently work|start date|end date|position|\blocation\b|\bfrom\b|\bto\b|role description|responsibilities|achievements)/.test(fullText);
  }

  function employmentContextForField(field, text) {
    const element = field.elementRef?.deref?.();
    if (!element) {
      return /(my experience|work experience|employment|work history|professional experience|job history)/.test(text)
        && text.length < 500;
    }

    const explicitHeading = nearestExplicitSectionHeadingText(element);
    if (/(my experience|employment|work experience|work history|professional experience|job history)/.test(explicitHeading)) {
      return true;
    }

    const localText = normalize(element.closest("section, fieldset, [role='group'], [data-automation-id*='formField'], div")?.innerText || "");
    return /(my experience|employment|work experience|work history|professional experience|job history)/.test(localText)
      && localText.length < 500;
  }

  function isEducationField(field, haystack) {
    const element = field.elementRef?.deref?.();
    const sectionHeading = element ? normalize(`${nearestExplicitSectionHeadingText(element)} ${nearestSectionHeadingText(element)}`) : "";
    const primaryHaystack = normalize([field.label, field.name, field.id, field.placeholder, field.ariaLabel].join(" "));

    if (/my experience|work experience|employment|professional experience|job history/.test(sectionHeading)) {
      return false;
    }

    if (/websites?|social network|resume/.test(sectionHeading)) {
      return false;
    }

    if (/start date month|start month|end date month|end month/.test(primaryHaystack)) {
      return false;
    }

    const educationLabel = /(school|university|institution|degree|field of study|discipline|major|qualification|actual or expected|^from\b|^to\b|start.*year|end.*year|graduation)/.test(primaryHaystack);

    if (/education|school|university/.test(sectionHeading)) {
      return educationLabel;
    }

    if (/work experience|employment|company|job title|role description/.test(haystack)) {
      return false;
    }

    return educationLabel || /(education|school|university|degree|field of study|discipline|major|qualification|actual or expected)/.test(haystack);
  }

  function isRepeatableEducationDateField(field, haystack) {
    const name = normalize(field.name || "");
    const element = field.elementRef?.deref?.();
    const sectionHeading = element ? normalize(`${nearestExplicitSectionHeadingText(element)} ${nearestSectionHeadingText(element)}`) : "";
    return /\[\]$/.test(field.name || "")
      || /\b(end|to|graduation).*(year|date)\s*\[\]/.test(name)
      || /education|school|university/.test(sectionHeading)
      || /education\s+\d+/.test(haystack);
  }

  function isWebsiteField(field, haystack) {
    const primaryHaystack = normalize([field.label, field.name, field.id, field.placeholder, field.ariaLabel].join(" "));
    const element = field.elementRef?.deref?.();
    const sectionHeading = element ? normalize(`${nearestExplicitSectionHeadingText(element)} ${nearestSectionHeadingText(element)}`) : "";

    if (
      /social network|linkedin|linked in|github|git hub|facebook|twitter|phone|email|location/.test(primaryHaystack)
      || isWorkOrEducationIdentityField(primaryHaystack)
    ) {
      return false;
    }

    if (/websites?|urls?|links?/.test(sectionHeading)) {
      return /(websites?|urls?|links?|\burl\b)/.test(primaryHaystack);
    }

    if (/work experience|employment|education|school|university|resume|social network/.test(sectionHeading)) {
      return false;
    }

    return /(websites?|urls?|links?|\burl\b)/.test(primaryHaystack)
      || (!primaryHaystack && /(websites?|urls?|links?|\burl\b)/.test(haystack));
  }

  function isWorkOrEducationIdentityField(haystack) {
    return /(current|previous|most recent|last).*(employer|company|school|university|college|education|job title|title|position|role)/.test(haystack)
      || /(employer|company|school|university|college|education|job title|title|position|role).*(current|previous|most recent|last|attended)/.test(haystack)
      || /work experience|employment history|last university attended|current\/previous employer/.test(haystack);
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
      return uniqueEducationEntries(items);
    }

    const resumeEducation = Array.isArray(profile.resumeFacts?.education) ? profile.resumeFacts.education : [];
    const school = compactText(profile.school || resumeEducation[0] || "");
    const fallback = normalizeEducationEntry({}, profile);
    fallback.school = fallback.school || school;
    const merged = mergeEducationEntry(fallback, parsedResumeEducation);

    return hasValue(merged.school) || hasValue(merged.degree) ? [merged] : [];
  }

  function uniqueEducationEntries(items) {
    const seen = new Set();
    const uniqueItems = [];

    for (const item of items) {
      const key = normalize([item.school, item.degree, item.fieldOfStudy].join(" "));
      if (seen.has(key)) {
        continue;
      }

      seen.add(key);
      uniqueItems.push(item);
    }

    return uniqueItems;
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
      profile.portfolio,
      profile.website,
      profile.personalWebsite,
      profile.github,
      profile.linkedin
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
        return /^(education|websites?|my experience|work experience|employment|certifications?|languages?|social network urls?)$/.test(text);
      })
      .filter((heading) => followsNode(heading, element))
      .sort((left, right) => topOfElement(right) - topOfElement(left));

    return normalize(headings[0]?.innerText || headings[0]?.textContent || sectionTextAround(element));
  }

  function nearestExplicitSectionHeadingText(element) {
    const headings = Array.from(document.querySelectorAll("h1, h2, h3, h4, [role='heading'], div, span"))
      .filter(isVisibleElement)
      .filter((item) => {
        const text = normalize(item.innerText || item.textContent || "");
        return /^(education|websites?|my experience|work experience|employment|certifications?|languages?|social network urls?)$/.test(text);
      })
      .filter((heading) => followsNode(heading, element))
      .sort((left, right) => topOfElement(right) - topOfElement(left));

    return normalize(headings[0]?.innerText || headings[0]?.textContent || "");
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

    if (isSensitiveOptionFieldWithoutOptions(field)) {
      return null;
    }

    if (field.options?.length && !hasValue(normalizedValue)) {
      return null;
    }

    if (!field.options?.length && isOptionLikeField(field) && !canAttemptUnoptionedOptionLikeMapping(field, normalizedValue, source)) {
      return null;
    }

    return {
      index: field.index,
      value: normalizedValue,
      source,
      confidence,
      ...mappingIdentityForField(field)
    };
  }

  function mappingIdentityForField(field) {
    return {
      label: field.label || "",
      name: field.name || "",
      id: field.id || "",
      placeholder: field.placeholder || "",
      ariaLabel: field.ariaLabel || "",
      tag: field.tag || "",
      type: field.type || ""
    };
  }

  function canAttemptUnoptionedOptionLikeMapping(field, value, source = "") {
    const haystack = fullFieldHaystack(field);
    const normalizedValue = normalize(value);

    if (isGreenhousePhoneCountryCodeLikeField(field) && /^(\+?1|canada|canada 1|canada \+1)$/.test(normalizedValue)) {
      return true;
    }

    if (isGreenhouseTypedDropdownFallbackField(field) && hasValue(value)) {
      return true;
    }

    if (isSensitiveOptionFieldWithoutOptions(field)) {
      return false;
    }

    if (!/rule|policy|llm|ai|saved/.test(normalize(source))) {
      return false;
    }

    if (!/^(yes|no|n\/a|na|not applicable|none of the above|i do not wish to answer|i prefer not to answer)$/.test(normalizedValue)) {
      return false;
    }

    return isPolicyLikeQuestion(haystack);
  }

  function isSensitiveOptionFieldWithoutOptions(field) {
    if (field.options?.length) {
      return false;
    }

    const primary = primaryFieldHaystack(field);
    return /(gender|race|racial|ethnic|ethnicity|veteran|protected veteran|disability status|have a disability|had one in the past|sexual orientation|orientation)/.test(primary);
  }

  function isPolicyLikeQuestion(haystack) {
    return isAiOnlyQuestion(haystack)
      || isWorkEligibilityQuestion(haystack)
      || /(sponsor|sponsorship|visa|work permit|relative|family member|spouse|domestic partner|contractor|dealer|affiliate|subsidiary|certify|true and correct|terms|privacy policy|subscribe|newsletter|email alert|job alert|minimum age|18 years of age)/.test(haystack);
  }

  function isOptionLikeField(field) {
    const haystack = fullFieldHaystack(field);
    const primary = primaryFieldHaystack(field);
    const controlKind = normalize(`${field.tag || ""} ${field.type || ""}`);

    if (
      /^(input|textarea|contenteditable|textbox)\b/.test(controlKind)
      && !/select|combobox|button|listbox/.test(controlKind)
      && !/select one|select\.\.\.|choose/.test(primary)
    ) {
      return false;
    }

    if (
      isLinkedinProfileField(primary)
      || isGithubProfileField(primary)
      || isPortfolioProfileField(primary)
      || isEmailProfileField(primary)
      || isPhoneNumberField(primary)
      || isWebsiteUrlTextField(field)
    ) {
      return false;
    }

    return field.tag === "select"
      || field.tag === "button"
      || field.type === "combobox"
      || /listbox|combobox/i.test([field.type, field.ariaLabel, field.surroundingText].join(" "))
      || /selectwidget|selectshowall/i.test(field.dataAutomationId || "")
      || /select one|select\.\.\.|choose/.test(haystack)
      || (/(degree|discipline|field of study|major|qualification)/.test(haystack) && /select/.test(haystack));
  }

  function isAmbiguousRepeatableLocationDateLabel(primary) {
    return /^(location\s+)?(month|year)\s*\*?$/.test(primary)
      || /^location\s+(month|year)\b/.test(primary);
  }

  function isWebsiteUrlTextField(field) {
    const primary = primaryFieldHaystack(field);
    const element = field.elementRef?.deref?.();
    const sectionHeading = element ? normalize(`${nearestExplicitSectionHeadingText(element)} ${nearestSectionHeadingText(element)}`) : "";
    const tag = normalize(field.tag || "");
    const type = normalize(field.type || "");

    if (!/^(url|link)\s*\*?$/.test(primary) && !/(website|url|link)/.test(primary)) {
      return false;
    }

    if (/select|combobox|button|listbox/.test(`${tag} ${type}`)) {
      return false;
    }

    return /websites?|urls?|links?/.test(sectionHeading)
      || /^(url|link)\s*\*?$/.test(primary);
  }

  function normalizedMappingValue(field, value) {
    const policyValue = normalizedPolicyValue(field, value);
    if (policyValue !== null) {
      return bestOptionValue(field, policyValue) || policyValue;
    }

    const exactOption = bestOptionValue(field, value);
    if (field.options?.length) {
      const normalizedValue = normalize(value);
      return exactOption || (/^(yes|no)$/.test(normalizedValue) ? compactText(value) : "");
    }

    return value;
  }

  function normalizedPolicyValue(field, value) {
    const haystack = fullFieldHaystack(field);
    const desired = normalize(value);

    if (hasSponsorshipTerms(haystack)) {
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
      || /\b(h-?1b|f-?1|opt|cpt|tn|ead)\b/.test(text)
    ) {
      return "Yes";
    }

    return value;
  }

  // Compact snapshot of every scanned form field, in DOM order, so the model can resolve
  // conditional questions ("If you selected ... in the prior question") against the prior
  // questions and their current answers. Kept small: capped text and at most 60 entries.
  function buildPageFieldContext(fields) {
    const entries = fields.map((field) => ({
      field,
      element: field.elementRef?.deref?.() || field.choiceRefs?.[0]?.deref?.() || null
    }));

    entries.sort((a, b) => {
      if (!a.element || !b.element || a.element === b.element) {
        return 0;
      }

      const position = a.element.compareDocumentPosition(b.element);
      if (position & Node.DOCUMENT_POSITION_FOLLOWING) {
        return -1;
      }
      return position & Node.DOCUMENT_POSITION_PRECEDING ? 1 : 0;
    });

    return entries.slice(0, 60).map(({ field }) => {
      const currentValue = compactText(String(pageContextValueForField(field) ?? "")).slice(0, 120);
      return {
        label: compactText(displayLabelForField(field)).slice(0, 120),
        currentValue,
        answered: Boolean(currentValue) && !isPlaceholderValue(currentValue)
      };
    });
  }

  function pageContextValueForField(field) {
    if (field.choiceRefs?.length) {
      return (field.choiceRefs || [])
        .map((ref) => ref.deref?.())
        .filter((choice) => choice && (choice.checked || choice.getAttribute?.("aria-checked") === "true"))
        .map((choice) => choiceLabel(choice) || choiceValue(choice))
        .filter(Boolean)
        .join("; ");
    }

    const element = field.elementRef?.deref?.();
    return element && isFillable(element) ? getCurrentValue(element) : field.value;
  }

  async function getBackendMappings(fields, profile, allFields = fields) {
    const serializableFields = fields.map(({ elementRef, ...field }) => field);

    try {
      const response = await chrome.runtime.sendMessage({
        type: "MAP_FIELDS_WITH_BACKEND",
        payload: {
          fields: serializableFields,
          profile,
          page: {
            url: location.href,
            title: document.title,
            context: buildPageFieldContext(allFields)
          }
        }
      });

      if (!response?.ok || !Array.isArray(response.payload?.mappings)) {
        return [];
      }

      return response.payload.mappings.map((mapping) => {
        const field = fields.find((item) => item.index === mapping.index);
        return field ? { ...mapping, ...mappingIdentityForField(field) } : mapping;
      });
    } catch (error) {
      return [];
    }
  }

  function mergeMappings(localMappings, backendMappings, fields = []) {
    const byIndex = new Map();

    for (const mapping of localMappings) {
      const existing = byIndex.get(mapping.index);
      const candidate = normalizeMappingForField(mapping, fields);

      if (!hasValue(candidate.value)) {
        continue;
      }

      if (shouldKeepExistingMapping(existing, candidate, fields)) {
        continue;
      }

      if (!existing || mappingPriority(candidate) > mappingPriority(existing) || (
        mappingPriority(candidate) === mappingPriority(existing)
        && Number(candidate.confidence || 0) >= Number(existing.confidence || 0)
      )) {
        byIndex.set(mapping.index, candidate);
      }
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

      if (!existing || mappingPriority(candidate) > mappingPriority(existing) || (
        mappingPriority(candidate) === mappingPriority(existing)
        && Number(candidate.confidence || 0) >= Number(existing.confidence || 0)
      )) {
        byIndex.set(mapping.index, candidate);
      }
    }

    return Array.from(byIndex.values());
  }

  function isAlreadyCorrectlyFilledMapping(mapping, fields) {
    const field = resolveFieldForMapping(mapping, fields);
    const current = field?.value;

    if (!hasValue(current) || isPlaceholderValue(current)) {
      return false;
    }

    if (field?.options?.length) {
      const normalizedCurrent = normalize(current);
      const currentIsSingleOption = field.options.some((option) => {
        const optionText = normalize(option.label || option.value);
        return optionText && optionText === normalizedCurrent;
      });

      if (!currentIsSingleOption) {
        return false;
      }
    }

    if (field && isPhoneCountryCodeField(primaryFieldHaystack(field))) {
      return false;
    }

    return mappingValueMatchesField(current, mapping.value);
  }

  function shouldKeepExistingMapping(existing, candidate, fields) {
    if (!existing || !hasValue(existing.value)) {
      return false;
    }

    const field = fields.find((item) => item.index === existing.index);
    const haystack = field ? fullFieldHaystack(field) : "";
    const existingValue = normalize(existing.value);
    const candidateValue = normalize(candidate.value);

    if (/^(rule|profile-audit|policy-audit)$/.test(normalize(existing.source)) && isProfileContactOrLocationField(haystack)) {
      return true;
    }

    if (
      /(relocation assistance|need relocation assistance|relocation support)/.test(haystack)
      && /^(yes|no)$/.test(existingValue)
      && !/^(yes|no)$/.test(candidateValue)
    ) {
      return true;
    }

    return false;
  }

  function mappingPriority(mapping) {
    const source = normalize(mapping?.source || "");

    if (/^(experience|education|website)$/.test(source)) {
      return 70;
    }

    if (/^(rule|sensitive-rule|saved-answer)$/.test(source)) {
      return 60;
    }

    if (/audit|policy/.test(source)) {
      return 55;
    }

    if (/llm|ai/.test(source)) {
      return 50;
    }

    if (source === "field-kind") {
      return 20;
    }

    return 40;
  }

  function isProfileContactOrLocationField(haystack) {
    return isLinkedinProfileField(haystack)
      || isGithubProfileField(haystack)
      || isPortfolioProfileField(haystack)
      || isPhoneNumberField(haystack)
      || isEmailProfileField(haystack)
      || /^location\b|location city|city location/.test(haystack);
  }

  function normalizeMappingForField(mapping, fields) {
    const field = resolveFieldForMapping(mapping, fields);

    if (!field) {
      return mapping;
    }

    if (isSensitiveOptionFieldWithoutOptions(field)) {
      return {
        ...mapping,
        value: ""
      };
    }

    if (!field.options?.length && isOptionLikeField(field) && !canAttemptUnoptionedOptionLikeMapping(field, mapping.value, mapping.source)) {
      return {
        ...mapping,
        value: ""
      };
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

  function reindexMappingsByIdentity(mappings, fields) {
    return mappings.map((mapping) => {
      if (!mappingHasIdentity(mapping)) {
        return mapping;
      }

      const field = resolveFieldForMapping(mapping, fields);
      return field && field.index !== mapping.index
        ? { ...mapping, index: field.index }
        : mapping;
    });
  }

  function mappingMatchesField(mapping, field) {
    if (!field) {
      return false;
    }

    if (mapping.id && field.id && mapping.id === field.id) {
      return true;
    }

    if (
      mapping.name
      && field.name
      && mapping.name === field.name
      && (!mapping.type || !field.type || mapping.type === field.type || compatibleFieldTypes(mapping.type, field.type))
    ) {
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

  function isAlreadyCorrectlyFilledElement(element, mapping, field) {
    const current = compactText(getCurrentValue(element));
    if (!current || isPlaceholderValue(current)) {
      return false;
    }

    if (field?.options?.length) {
      const normalizedCurrent = normalize(current);
      const currentIsSingleOption = field.options.some((option) => {
        const optionText = normalize(option.label || option.value);
        return optionText && optionText === normalizedCurrent;
      });

      if (!currentIsSingleOption) {
        return false;
      }
    }

    if (field && isPhoneCountryCodeField(primaryFieldHaystack(field))) {
      return false;
    }

    if (mappingValueMatchesField(current, mapping.value)) {
      return true;
    }

    if (!field?.options?.length) {
      return false;
    }

    const currentOption = bestOptionValue(field, current);
    const desiredOption = bestOptionValue(field, mapping.value);
    return Boolean(currentOption && desiredOption && normalize(currentOption) === normalize(desiredOption));
  }

  // After fillElement returns false, decide whether the element already holds the desired
  // state (fine) or the fill genuinely failed (must be reported as a failure).
  function isFillSatisfied(element, mapping, field) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();

    if (type === "checkbox" || role === "checkbox" || (field.type === "checkbox" && field.choiceRefs?.length)) {
      return isCheckboxInDesiredState(element, mapping, field);
    }

    if (type === "radio" || role === "radio" || (field.type === "radio" && field.choiceRefs?.length)) {
      return isRadioGroupInDesiredState(element, mapping, field);
    }

    if (tag === "select") {
      const option = Array.from(element.options)
        .find((item) => optionMatches(item.textContent || "", item.value, mapping.value));
      return Boolean(option) && element.value === option.value;
    }

    return isAlreadyCorrectlyFilledElement(element, mapping, field)
      || mappingValueMatchesField(getCurrentValue(element), mapping.value);
  }

  function isCheckboxInDesiredState(element, mapping, field) {
    const choices = (field.choiceRefs || []).map((ref) => ref.deref?.()).filter(Boolean);
    if (choices.length) {
      const values = Array.isArray(mapping.value)
        ? mapping.value
        : String(mapping.value).split(/\s*[;,]\s*/).filter(Boolean);
      return choices.some((choice) => (
        values.some((item) => optionMatches(choiceLabel(choice), choiceValue(choice), item))
        && (choice.checked || choice.getAttribute?.("aria-checked") === "true")
      ));
    }

    const shouldCheck = /^(true|yes|y|1|agree|checked)$/i.test(String(mapping.value).trim());
    const isChecked = element.getAttribute("role") === "checkbox"
      ? element.getAttribute("aria-checked") === "true"
      : Boolean(element.checked);
    return isChecked === shouldCheck;
  }

  function isRadioGroupInDesiredState(element, mapping, field) {
    const choices = (field.choiceRefs || []).map((ref) => ref.deref?.()).filter(Boolean);
    if (choices.length) {
      return choices.some((choice) => (
        optionMatches(choiceLabel(choice), choiceValue(choice), mapping.value)
        && (choice.checked || choice.getAttribute?.("aria-checked") === "true")
      ));
    }

    const name = element.getAttribute("name");
    const candidates = name
      ? Array.from(document.querySelectorAll(`input[type="radio"][name="${cssEscape(name)}"]`))
      : [element];
    return candidates.some((radio) => (
      optionMatches(getLabelText(radio), radio.value || radio.getAttribute("aria-label") || "", mapping.value)
      && (radio.checked || radio.getAttribute("aria-checked") === "true")
    ));
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
      if (!isPhoneCountryCodeField(primaryFieldHaystack(field)) && (valueMatches(current, mapping.value) || optionMatches(current, "", mapping.value))) {
        return false;
      }

      return fillCombobox(element, mapping.value, field);
    }

    if (field.options?.length && dropdownTrigger(element)) {
      const current = getCurrentValue(element);
      if (!isPhoneCountryCodeField(primaryFieldHaystack(field)) && (valueMatches(current, mapping.value) || optionMatches(current, "", mapping.value))) {
        return false;
      }

      const filled = await fillCombobox(element, mapping.value, field);
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

  async function fillCombobox(element, desiredValue, field = null) {
    const trigger = dropdownTrigger(element) || element;
    const previousValue = getCurrentValue(trigger);
    const mustClickOption = requiresDropdownOptionClick();

    trigger.focus();
    trigger.click();
    await sleep(250);

    if (!visibleOptionElements(trigger).length) {
      openDropdownWithPointer(trigger);
      await sleep(250);
    }

    const initialOptions = visibleOptionElements(trigger);
    const optionValue = optionConstrainedValue(optionsFromElements(initialOptions), desiredValue) || desiredValue;
    const option = await waitForMatchingDropdownOption(trigger, optionValue);

    if (option) {
      clickOption(option);
      dispatchFormEvents(trigger);
      dispatchFormEvents(element);
      return true;
    }

    const searchValue = dropdownSearchValue(optionValue);

    if (trigger.tagName.toLowerCase() !== "button") {
      setEditableText(trigger, searchValue);
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

      if (canConfirmTypedDropdownValue(field, typedOptionValue)) {
        dispatchEnterOrEscape(trigger, "Enter");
        await sleep(250);
        dispatchFormEvents(trigger);
        dispatchFormEvents(element);

        if (dropdownSelectionLooksConfirmed(trigger, previousValue, typedOptionValue)) {
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

    if (canConfirmTypedDropdownValue(field, optionValue)) {
      dispatchKeyboardText(trigger, searchValue);
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

      dispatchEnterOrEscape(trigger, "Enter");
      await sleep(250);
      dispatchFormEvents(trigger);
      dispatchFormEvents(element);

      if (dropdownSelectionLooksConfirmed(trigger, previousValue, typedOptionValue)) {
        return true;
      }
    }

    dispatchFormEvents(trigger);
    dispatchFormEvents(element);
    return false;
  }

  function dispatchKeyboardText(element, text) {
    for (const char of String(text || "")) {
      const eventInit = {
        key: char,
        code: "",
        bubbles: true,
        cancelable: true
      };
      element.dispatchEvent(new KeyboardEvent("keydown", eventInit));
      element.dispatchEvent(new KeyboardEvent("keypress", eventInit));
      element.dispatchEvent(new KeyboardEvent("keyup", eventInit));
    }
  }

  function canConfirmTypedDropdownValue(field, value) {
    if (!field) {
      return false;
    }

    const desired = normalize(value);
    if (isGreenhousePhoneCountryCodeLikeField(field) && /^(\+?1|canada|canada 1|canada \+1)$/.test(desired)) {
      return true;
    }

    return isGreenhouseTypedDropdownFallbackField(field) && hasValue(value);
  }

  function dropdownSelectionLooksConfirmed(trigger, previousValue, desiredValue) {
    const current = getCurrentValue(trigger);

    if (valueMatches(current, previousValue)) {
      return false;
    }

    return optionMatches(current, "", desiredValue) || /\+?\s*1\b|\bcanada\b/i.test(current);
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

    if (/^(yes|true)\b/.test(text) || /^(y|1)$/.test(text)) {
      return "yes";
    }

    if (/^(no|false)\b/.test(text) || /^(n|0)$/.test(text)) {
      return "no";
    }

    if (/\bnot (legally )?(authorized|eligible|permitted|allowed)\b/.test(text)) {
      return "no";
    }

    if (/\b(authorized|eligible|permitted|allowed) to work\b/.test(text)
      || (/\b(authorized|eligible|permitted|allowed)\b/.test(text) && /\bwithout (visa )?sponsorship\b/.test(text))) {
      return "yes";
    }

    if (/\b(no|not|never|decline|unable|cannot|won t|would not|do not|don t)\b/.test(text)) {
      return "no";
    }

    if (/\b(open|willing|able|can|agree|consent|authorized|eligible)\b/.test(text)) {
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

    const educationEntries = normalizedEducation(profile);
    if (!educationEntries.length) {
      return 0;
    }

    // Per-row fill. Repeatable education sections (Greenhouse/Workday) render one
    // School/Degree/Field-of-study control per education block, and prepareRepeatableSections
    // clicks "Add another" once per profile entry. Previously this fallback only located the
    // FIRST control of each kind (document-wide) and filled it from education[0], leaving the
    // rows created for entries 2..N empty. Collect every matching control in document order
    // and fill control[i] from educationEntries[i] so each created row is populated.
    const schoolControls = findAllWorkdayDropdownsByLabel(/^(school or university|school|university|college|institution)\s*\*?$/i);
    const degreeControls = findAllWorkdayDropdownsByLabel(/^degree\s*\*?$/i);
    const fieldControls = findAllWorkdayDropdownsByLabel(/^(field of study|discipline|major)\s*\*?$/i);

    let filled = 0;

    for (let index = 0; index < educationEntries.length; index += 1) {
      const education = educationEntries[index];

      // Top-level profile.school/profile.degree only describe the primary (first) entry.
      const school = education.school || (index === 0 ? profile.school : "");
      const schoolControl = schoolControls[index];
      if (schoolControl && hasValue(school) && !valueMatches(getCurrentValue(schoolControl), school)) {
        filled += await fillCombobox(schoolControl, school) ? 1 : 0;
      }

      const degree = education.degree || (index === 0 ? profile.degree : "");
      const degreeControl = degreeControls[index];
      if (degreeControl && hasValue(degree) && !valueMatches(getCurrentValue(degreeControl), degree)) {
        filled += await fillCombobox(degreeControl, degree) ? 1 : 0;
      }

      const fieldControl = fieldControls[index];
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
    }

    return filled;
  }

  async function fillWorkdayHearAboutUsFallback(profile) {
    if (!/workday/i.test(`${location.hostname} ${document.body?.innerText || ""}`)) {
      return 0;
    }

    const answer = profile.answers?.applicationSource || "LinkedIn";
    const control = findWorkdayDropdownByLabel(/^(how did you hear about us\??|how did you hear about this\??|how did you hear about this job\??|source)\s*\*?$/i);
    if (!control || !hasValue(answer) || optionMatches(getCurrentValue(control), "", answer)) {
      return 0;
    }

    return await fillCombobox(control, answer) ? 1 : 0;
  }

  async function fillWorkdayAddressFallback(profile, settings) {
    if (!/workday/i.test(`${location.hostname} ${document.body?.innerText || ""}`)) {
      return 0;
    }

    const address = selectAddress(profile, settings, "address state postal code");
    if (!address) {
      return 0;
    }

    let filled = 0;
    filled += fillWorkdayTextInputByLabel(/^address line 1\s*\*?$/i, address.line1) ? 1 : 0;
    filled += fillWorkdayTextInputByLabel(/^address line 2\s*\*?$/i, address.line2) ? 1 : 0;
    filled += fillWorkdayTextInputByLabel(/^city\s*\*?$/i, address.city) ? 1 : 0;
    filled += fillWorkdayTextInputByLabel(/^(postal code|zip code|postcode)\s*\*?$/i, address.postalCode || address.zipCode) ? 1 : 0;

    const state = stateNameOrValue(address.state || address.province || "");
    const stateControl = findWorkdayDropdownByLabel(/^(state|province|province or territory|territory)\s*\*?$/i);
    if (stateControl && hasValue(state) && !optionMatches(getCurrentValue(stateControl), "", state)) {
      filled += await fillCombobox(stateControl, state) ? 1 : 0;
    }

    return filled;
  }

  function fillWorkdayTextInputByLabel(pattern, value) {
    if (!hasValue(value)) {
      return false;
    }

    const input = findWorkdayTextInputByLabel(pattern);
    if (!input || valueMatches(getCurrentValue(input), value)) {
      return false;
    }

    setEditableText(input, value);
    confirmFilledElement(input);
    return true;
  }

  function findWorkdayTextInputByLabel(pattern) {
    const labels = Array.from(document.querySelectorAll("label, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel'], span, div"))
      .filter(isVisibleElement)
      .filter((item) => pattern.test(compactText(item.innerText || item.textContent || "")))
      .sort((left, right) => topOfElement(left) - topOfElement(right));

    for (const label of labels) {
      const control = findTextInputNearLabel(label);
      if (control) {
        return control;
      }
    }

    return null;
  }

  function findTextInputNearLabel(label) {
    let container = label;

    for (let depth = 0; container && container !== document.body && depth < 8; depth += 1) {
      const control = Array.from(container.querySelectorAll("input:not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='file']), textarea, [role='textbox'], [contenteditable='true']"))
        .find((item) => isVisibleElement(item) && isFillable(item));

      if (control) {
        return control;
      }

      const siblingControl = findTextInputInNearbySibling(container);
      if (siblingControl) {
        return siblingControl;
      }

      container = container.parentElement;
    }

    return null;
  }

  function findTextInputInNearbySibling(element) {
    let sibling = element.nextElementSibling;
    for (let checked = 0; sibling && checked < 4; checked += 1, sibling = sibling.nextElementSibling) {
      const control = sibling.matches?.("input, textarea, [role='textbox'], [contenteditable='true']")
        ? sibling
        : sibling.querySelector?.("input:not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='file']), textarea, [role='textbox'], [contenteditable='true']");

      if (control && isVisibleElement(control) && isFillable(control)) {
        return control;
      }
    }

    return null;
  }

  function preferredEducationFields(education) {
    const values = [];

    if (education.fieldOfStudy) {
      values.push(education.fieldOfStudy);
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

  // Like findWorkdayDropdownByLabel but returns every distinct matching control in document
  // (top-to-bottom) order. Used to fill repeated education blocks per row instead of only the
  // first one.
  function findAllWorkdayDropdownsByLabel(pattern) {
    const labels = Array.from(document.querySelectorAll("label, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel'], span, div"))
      .filter(isVisibleElement)
      .filter((item) => pattern.test(compactText(item.innerText || item.textContent || "")))
      .sort((left, right) => topOfElement(left) - topOfElement(right));

    const controls = [];
    const seen = new Set();

    for (const label of labels) {
      const control = findDropdownNearLabel(label);
      if (control && !seen.has(control)) {
        seen.add(control);
        controls.push(control);
      }
    }

    return controls;
  }

  function findWorkdayCountryDropdown() {
    const labels = Array.from(document.querySelectorAll("label, [data-automation-id='formLabel'], [data-automation-id='formFieldLabel'], span, div"))
      .filter(isVisibleElement)
      .filter((item) => {
        const text = normalize(item.innerText || item.textContent || "");
        return /^country(\s+(territory|region|or territory|or region))?$/.test(text) && !/phone/.test(text);
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

    if (locationOptionMatches(normalizedLabel, desired) || locationOptionMatches(normalizedValue, desired)) {
      return true;
    }

    if (isUnitedStatesDesired(desired)) {
      return isUnitedStatesOption(normalizedLabel) || isUnitedStatesOption(normalizedValue);
    }

    return aliases.some((alias) => alias.length > 3 && containsNormalizedPhrase(normalizedLabel, alias))
      || (desired.length > 2 && containsNormalizedPhrase(normalizedLabel, desired));
  }

  function locationOptionMatches(optionText, desiredText) {
    if (!optionText || !desiredText || !/[, ]/.test(desiredText)) {
      return false;
    }

    const [rawCity, rawRegion] = String(desiredText)
      .split(",")
      .map((part) => normalize(part));
    if (!rawCity || !rawRegion || rawCity.length < 3) {
      return false;
    }

    const regionName = normalize(stateNameOrValue(rawRegion));
    const regionMatches = optionText.includes(rawRegion) || (regionName && optionText.includes(regionName));
    return optionText.includes(rawCity) && regionMatches;
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
      // Only report a fill when the state actually changed; a checkbox that was already in
      // the desired state must not count as newly filled.
      return current !== shouldCheck;
    }

    const changed = element.checked !== shouldCheck;
    if (changed) {
      element.click();
    }

    element.checked = shouldCheck;
    dispatchFormEvents(element);
    return changed;
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
    // Non-searchable react-selects (Greenhouse renders required Yes/No questions this way)
    // mark their combobox input readOnly. They are still fillable through option clicks,
    // so only plain read-only text inputs are excluded from the scan.
    const isReadOnlyDropdown = Boolean(element.readOnly) && isListboxTrigger(element);

    return !element.disabled
      && (!element.readOnly || isReadOnlyDropdown)
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

    const direct = element.value || element.textContent || "";
    if (!compactText(direct) && (element.getAttribute("role") || "").toLowerCase() === "combobox") {
      // React-select keeps the chosen option in a sibling single-value node, never in the
      // combobox input itself, so read it from the surrounding control.
      const containers = [
        element.closest("[class*='value-container' i]"),
        element.closest("[class*='control' i]"),
        element.closest("[class*='select' i]")
      ].filter(Boolean);

      for (const container of containers) {
        const selected = container.querySelector("[class*='single-value' i], [class*='singlevalue' i]");
        const text = compactText(selected?.textContent || "");
        if (text) {
          return text;
        }
      }
    }

    return direct;
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
