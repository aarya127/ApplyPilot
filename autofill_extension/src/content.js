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
    scanCount: 0,
    isApplying: false,
    dynamicRunCount: 0
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
    await prepareRepeatableSections(profile);
    const fields = scanFields();
    const localMappings = fields.map((field) => mapField(field, profile, settings || {})).filter(Boolean);
    const repeatableMappings = mapRepeatableEmploymentFields(fields, profile);
    const backendMappings = settings?.autoMapAmbiguousFields === true
      ? await getBackendMappings(fields, profile)
      : [];
    const mappings = mergeMappings([...localMappings, ...repeatableMappings], backendMappings);

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
    const { settings } = await chrome.storage.local.get("settings");
    const fields = scanFields();
    let filled = 0;
    const failures = [];

    try {
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
    const label = choiceGroupLabel(group.container, options) || getSurroundingText(first) || getLabelText(first);

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
        ".form-group, .field, .question, .application-field, [data-qa], [data-testid], li, p, div"
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

    const companyQuestionMapping = mapCompanyQuestion(field, profile, primaryHaystack);
    if (companyQuestionMapping) {
      return companyQuestionMapping;
    }

    if (/(may we contact|contact).*(current employer)/.test(haystack)) {
      return buildMapping(field, profile.answers?.contactCurrentEmployer || "No", "rule", 0.82);
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
      [/(linkedin profile|linkedin url|linked in profile|linked in url)/, profile.linkedin],
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

    if (/(ever|previously|formerly).*(employed|worked).*(affiliate|subsidiary|\bby\b|\bfor\b)/.test(haystack)) {
      return buildMapping(field, profile.answers?.previouslyEmployedByCompany || "No", "rule", 0.9);
    }

    return null;
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

    if (experiences.length <= 1) {
      return;
    }

    await ensureEmploymentRows(experiences.length);
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
      countFieldsMatching(/\bcompany name\b/),
      countFieldsMatching(/(^|\b)title\b/),
      countFieldsMatching(/start date.*year/)
    );
  }

  function countFieldsMatching(pattern) {
    return scanFieldsWithoutPreparation()
      .filter((field) => pattern.test(normalize([field.label, field.name, field.id, field.placeholder].join(" "))))
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
      startMonth: [],
      startYear: [],
      endMonth: [],
      endYear: [],
      currentRole: []
    };

    for (const field of fields) {
      const haystack = normalize([field.label, field.name, field.id, field.placeholder].join(" "));

      if (!isEmploymentField(field, haystack)) {
        continue;
      }

      if (/\bcompany name\b|\bemployer\b/.test(haystack)) {
        buckets.company.push(field);
      } else if (/(^|\b)title\b|job title/.test(haystack) || (/\bposition\b/.test(haystack) && /employment|experience|work history/.test(normalize(field.surroundingText)))) {
        buckets.title.push(field);
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
      }
    }

    const mappings = [];
    experiences.forEach((experience, index) => {
      addRepeatableMapping(mappings, buckets.company[index], experience.company);
      addRepeatableMapping(mappings, buckets.title[index], experience.title);
      addRepeatableMapping(mappings, buckets.startMonth[index], experience.startMonth);
      addRepeatableMapping(mappings, buckets.startYear[index], experience.startYear);
      addRepeatableMapping(mappings, buckets.endMonth[index], experience.currentRole ? "" : experience.endMonth);
      addRepeatableMapping(mappings, buckets.endYear[index], experience.currentRole ? "" : experience.endYear);
      addRepeatableMapping(mappings, buckets.currentRole[index], experience.currentRole ? true : false);
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

    if (/(this position is based|commutable|relocat|lyft office|san francisco)/.test(haystack)) {
      return false;
    }

    return /(employment|experience|work history|company name|employer|\btitle\b|job title|current role|start date|end date|position)/.test(
      `${haystack} ${normalize(field.surroundingText)}`
    );
  }

  function normalizedWorkExperience(profile) {
    const raw = profile.workExperience || profile.resumeFacts?.workExperience || [];
    if (Array.isArray(raw) && raw.some((item) => item && typeof item === "object")) {
      return raw
        .filter((item) => item && typeof item === "object")
        .map(normalizeExperienceEntry)
        .filter((item) => hasValue(item.company) || hasValue(item.title));
    }

    return parseExperienceLines(profile.resumeFacts?.experience || []);
  }

  function normalizeExperienceEntry(entry) {
    return {
      company: compactText(entry.company || entry.employer || entry.organization || ""),
      title: compactText(entry.title || entry.role || entry.position || ""),
      startMonth: compactText(entry.startMonth || entry.start_month || ""),
      startYear: compactText(entry.startYear || entry.start_year || ""),
      endMonth: compactText(entry.endMonth || entry.end_month || ""),
      endYear: compactText(entry.endYear || entry.end_year || ""),
      currentRole: entry.currentRole === true || entry.current === true || /present|current/i.test(String(entry.endYear || entry.end || ""))
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

      const { title } = splitTitleLocation(lines[index + 1] || "");
      entries.push({
        company: compactText(match[1]).replace(/\bW aterloo\b/g, "Waterloo"),
        title,
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

  function employmentSectionText(element) {
    return normalize(element.closest("section, fieldset, form, div")?.innerText || "");
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
      setEditableText(element, mapping.value);
      return true;
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
          choice.click();
        }
      } else if (choice.getAttribute("role") === "radio") {
        choice.click();
      } else if (choice.type === "checkbox") {
        if (!choice.checked) {
          choice.click();
        }
        choice.checked = true;
      } else if (choice.type === "radio") {
        choice.click();
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
    element.focus();
    element.click();
    setEditableText(element, desiredValue);
    await sleep(200);

    const option = Array.from(document.querySelectorAll("[role='option'], [data-option], .select2-results__option"))
      .find((item) => optionMatches(
        item.textContent || item.getAttribute("aria-label") || "",
        item.getAttribute("data-value") || "",
        desiredValue
      ));

    if (option) {
      option.click();
    }

    dispatchFormEvents(element);
    return true;
  }

  function fillSelect(select, desiredValue) {
    const options = Array.from(select.options);
    const option = options.find((item) => optionMatches(item.textContent || "", item.value, desiredValue));

    if (!option) {
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

    target.click();
    target.checked = true;
    dispatchFormEvents(target);
    return true;
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

    return aliases.some((alias) => alias.length > 3 && containsNormalizedPhrase(normalizedLabel, alias))
      || (desired.length > 2 && containsNormalizedPhrase(normalizedLabel, desired));
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
        "no i have not",
        "not a protected veteran",
        "i am not a protected veteran",
        "not protected veteran",
        "not a veteran",
        "not hispanic or latino",
        "not hispanic",
        "not latino"
      ].forEach((alias) => aliases.add(alias));
    }

    if (desired === "yes") {
      [
        "yes i am",
        "yes i do",
        "yes i have"
      ].forEach((alias) => aliases.add(alias));
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
