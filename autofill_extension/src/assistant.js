const chatLog = document.getElementById("chatLog");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");
const reviewList = document.getElementById("reviewList");
const assistantStatus = document.getElementById("assistantStatus");
const aiStatusPanel = document.getElementById("aiStatusPanel");
const aiStatusText = document.getElementById("aiStatusText");
const aiUsageText = document.getElementById("aiUsageText");
const scanButton = document.getElementById("scanButton");
const previewButton = document.getElementById("previewButton");
const askAiButton = document.getElementById("askAiButton");
const fillSelectedButton = document.getElementById("fillSelectedButton");
const saveAnswersButton = document.getElementById("saveAnswersButton");
const trackButton = document.getElementById("trackButton");
const optionsButton = document.getElementById("optionsButton");
const countryButtons = Array.from(document.querySelectorAll("[data-country]"));

let lastPreview = null;
let lastFillResult = null;
let aiUsageRefreshTimer = null;

initAssistant();

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = chatInput.value.trim();

  if (!text) {
    return;
  }

  chatInput.value = "";
  addMessage("user", text);
  await handleChat(text);
});

countryButtons.forEach((button) => {
  button.addEventListener("click", async () => {
    const country = button.dataset.country;
    await setTargetCountry(country);
    addMessage("user", country === "usa" ? "This is a USA role." : "This is a Canadian role.");
    addMessage("agent", `Got it. I will use your ${country === "usa" ? "USA" : "Canadian"} address and eligibility answers for this page.`);
  });
});

scanButton.addEventListener("click", async () => {
  setBusy("Scanning the current application page...");
  const response = await sendToActiveTab({ type: "SCAN_FIELDS" });

  if (!response?.ok) {
    showError(response?.error || "I could not scan this page.");
    return;
  }

  const frameText = response.frameCount
    ? ` across ${response.accessibleFrameCount}/${response.frameCount} accessible frame(s)`
    : "";
  addMessage("agent", `I found ${response.count} fillable field(s)${frameText}.`);
  assistantStatus.textContent = "Scan complete";
});

previewButton.addEventListener("click", async () => {
  await previewCurrentPage();
});

askAiButton.addEventListener("click", async () => {
  await askAiForMissingAnswers();
});

fillSelectedButton.addEventListener("click", async () => {
  await fillSelectedMappings();
});

saveAnswersButton.addEventListener("click", async () => {
  await saveNewAnswersFromReview();
});

trackButton.addEventListener("click", async () => {
  await trackApplication();
});

optionsButton.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

async function handleChat(text) {
  const normalized = text.toLowerCase();

  if (/\b(usa|u\.s\.|united states|american)\b/.test(normalized)) {
    await setTargetCountry("usa");
    addMessage("agent", "Set this application to USA.");
    return;
  }

  if (/\b(canada|canadian)\b/.test(normalized)) {
    await setTargetCountry("canada");
    addMessage("agent", "Set this application to Canada.");
    return;
  }

  if (/\bscan\b/.test(normalized)) {
    scanButton.click();
    return;
  }

  if (/\bpreview\b/.test(normalized)) {
    await previewCurrentPage();
    return;
  }

  if (/\b(ai|answer missing|missing answers|ask)\b/.test(normalized)) {
    await askAiForMissingAnswers();
    return;
  }

  if (/\b(dropdown|dropdowns|options)\b/.test(normalized) && /\b(debug|test|find|scan|show)\b/.test(normalized)) {
    await showDropdownDebug();
    return;
  }

  if (/\bdebug\b/.test(normalized)) {
    await showDebugFields();
    return;
  }

  if (/\bfill\b/.test(normalized)) {
    await fillSelectedMappings();
    return;
  }

  if (/\bsave\b/.test(normalized)) {
    await saveNewAnswersFromReview();
    return;
  }

  if (/\btrack\b/.test(normalized)) {
    await trackApplication();
    return;
  }

  if (await saveAnswerFromChat(text)) {
    return;
  }

  addMessage("agent", "I can scan, preview, fill selected fields, save missing answers, track the application, or switch USA/Canada. For a missing answer, type something like `question text: answer`.");
}

async function previewCurrentPage() {
  setBusy("Building a review plan...");
  const response = await sendToActiveTab({ type: "PREVIEW_AUTOFILL" });

  if (!response?.ok) {
    showError(response?.error || "I could not build a preview for this page.");
    return;
  }

  lastPreview = response.result;
  lastFillResult = null;
  renderReview(response.result);
}

async function fillSelectedMappings() {
  const selected = getSelectedMappings();

  if (!selected.length) {
    addMessage("agent", "Nothing is selected yet. Preview the page, then keep the fields you want checked.");
    return;
  }

  setBusy("Filling the selected reviewed fields...");
  const response = await sendToActiveTab({
    type: "APPLY_AUTOFILL_MAPPINGS",
    mappings: selected
  });

  if (!response?.ok) {
    showError(response?.error || "I could not fill the selected fields.");
    return;
  }

  lastFillResult = response.result;
  trackButton.disabled = false;
  addMessage("agent", `Filled ${response.result.filled} selected field(s). Please review the page before submitting.`);
  assistantStatus.textContent = "Filled";
}

async function saveNewAnswersFromReview() {
  const answers = collectNewAnswers();

  if (!Object.keys(answers).length) {
    addMessage("agent", "I do not see any new answers to save yet.");
    return;
  }

  const { candidateProfile } = await chrome.storage.local.get("candidateProfile");
  const profile = candidateProfile || {};
  profile.answers = { ...(profile.answers || {}), ...answers };
  await chrome.storage.local.set({ candidateProfile: profile });

  addMessage("agent", `Saved ${Object.keys(answers).length} answer(s). I will remember those for next time.`);
  const response = await sendToActiveTab({ type: "PREVIEW_AUTOFILL" });
  if (response?.ok) {
    lastPreview = response.result;
    renderReview(response.result);
  }
}

async function askAiForMissingAnswers() {
  setAiWorking(true, "Refreshing page before Ask AI...");
  try {
    return await askAiForMissingAnswersImpl();
  } finally {
    setAiWorking(false, "AI idle");
    await refreshAiUsage(true);
  }
}

async function askAiForMissingAnswersImpl() {
  const freshPreview = await sendToActiveTab({ type: "PREVIEW_AUTOFILL" });

  if (!freshPreview?.ok) {
    showError(freshPreview?.error || "I could not refresh the page before asking AI.");
    return;
  }

  lastPreview = freshPreview.result;
  renderReview(lastPreview);

  const { candidateProfile, settings } = await chrome.storage.local.get(["candidateProfile", "settings"]);
  const profile = candidateProfile || {};
  const pageContext = {
    ...(lastPreview?.page || {}),
    targetCountry: settings?.targetCountry || ""
  };
  const auditMappings = await auditCurrentAutofillAnswers(lastPreview, profile, pageContext);
  const missingFields = (lastPreview?.unmappedFields || []).filter(isAiAskableField);
  const skippedOptionFields = (lastPreview?.unmappedFields || []).filter((field) => (
    isOptionLikeField(field) && !(field.options || []).length
  ));

  if (!missingFields.length && !auditMappings.length) {
    addMessage("agent", "I do not see any unanswered fields for AI to handle, and the filled answers passed the audit.");
    if (skippedOptionFields.length) {
      addMessage("agent", `I skipped ${skippedOptionFields.length} dropdown-like field(s) because I could not read their options. Type \`debug dropdowns\` to test option discovery.`);
    }
    return;
  }

  if (skippedOptionFields.length) {
    addMessage("agent", `I will not ask AI to fill ${skippedOptionFields.length} dropdown-like field(s) without options. Type \`debug dropdowns\` to see what I can read.`);
  }

  const answers = {};
  const aiMappings = [...auditMappings];

  if (missingFields.length) {
    setBusy(`Asking AI for ${missingFields.length} unanswered required/askable field(s), one at a time...`);
  }

  for (let index = 0; index < missingFields.length; index += 1) {
    const field = missingFields[index];
    const fieldForModel = toBackendField(field, 0);
    setAiWorking(true, `Asking AI ${index + 1}/${missingFields.length}: ${truncateDebug(field.label || "field")}`);
    const response = await chrome.runtime.sendMessage({
      type: "MAP_FIELDS_WITH_BACKEND",
      payload: {
        fields: [fieldForModel],
        profile,
        page: pageContext
      }
    });
    updateAiUsage(response?.payload?.aiUsage);

    if (!response?.ok) {
      addMessage("agent", `AI could not answer "${field.label}": ${response?.error || "request failed"}`);
      continue;
    }

    const mapping = (response.payload?.mappings || []).find((item) => item.index === 0);
    const value = String(mapping?.value ?? "").trim();

    if (field.answerKey && value) {
      answers[field.answerKey] = value;
      aiMappings.push(toPreviewMapping(fieldForModel, mapping));
    }
  }

  if (!aiMappings.length) {
    showError("AI did not return safe answers or audit corrections for the visible fields.");
    return;
  }

  if (Object.keys(answers).length) {
    profile.answers = { ...(profile.answers || {}), ...answers };
    await chrome.storage.local.set({ candidateProfile: profile });
  }

  lastPreview = mergeAiMappingsIntoPreview(lastPreview, aiMappings);
  renderReview(lastPreview);
  const fillResponse = await sendToActiveTab({
    type: "APPLY_AUTOFILL_MAPPINGS",
    mappings: aiMappings
  });

  if (fillResponse?.ok) {
    lastFillResult = fillResponse.result;
    trackButton.disabled = false;
    addMessage("agent", `Audited, added, and filled ${fillResponse.result.filled} answer(s). Please review them before continuing.`);
    await refreshPreviewAfterAiFill();
    reportRemainingRequiredFields(lastPreview);
  } else {
    addMessage("agent", `Added ${aiMappings.length} AI answer(s) to the review list, but I could not fill them automatically. Keep them checked and click Fill selected.`);
    reportRemainingRequiredFields(lastPreview);
  }
}

async function auditCurrentAutofillAnswers(preview, profile, pageContext) {
  const currentMappings = (preview?.mappings || []).filter((mapping) => (
    mapping && mapping.value !== undefined && mapping.value !== null && String(mapping.value).trim()
  ));

  if (!currentMappings.length) {
    return [];
  }

  const fieldsForAudit = currentMappings.map((mapping, index) => toBackendField(fieldForPreviewMapping(preview, mapping), index));
  const auditPayloadMappings = currentMappings.map((mapping, index) => ({
    index,
    value: currentAuditValue(preview, mapping),
    plannedValue: mapping.value,
    source: mapping.source,
    confidence: mapping.confidence
  }));

  setBusy(`Auditing ${currentMappings.length} filled answer(s) before asking AI...`);
  const response = await chrome.runtime.sendMessage({
    type: "AUDIT_FIELDS_WITH_BACKEND",
    payload: {
      fields: fieldsForAudit,
      mappings: auditPayloadMappings,
      profile,
      page: pageContext
    }
  });

  if (!response?.ok) {
    addMessage("agent", `Audit skipped: ${response?.error || "request failed"}`);
    return [];
  }

  updateAiUsage(response.payload?.aiUsage);

  const corrections = response.payload?.corrections || [];
  const auditMappings = [];
  for (const correction of corrections) {
    const field = fieldsForAudit.find((item) => item.index === correction.index);
    const value = String(correction.value ?? "").trim();
    if (field && value) {
      auditMappings.push(toPreviewMapping(field, correction));
    }
  }

  if (auditMappings.length) {
    addMessage("agent", `Audit found ${auditMappings.length} filled answer(s) to correct before continuing.`);
  }

  const issues = response.payload?.issues || [];
  for (const issue of issues.slice(0, 5)) {
    if (issue?.reason) {
      addMessage("agent", `Audit note: ${issue.reason}`);
    }
  }

  const decisions = response.payload?.decisions || [];
  const corrected = decisions.filter((item) => item.action === "correct" || item.action === "fill").length;
  const skipped = decisions.filter((item) => item.action === "skip").length;
  if (decisions.length && (corrected || skipped)) {
    addMessage("agent", `Audit protocol: ${decisions.length} checked, ${corrected} correction/fill decision(s), ${skipped} skipped as unsafe.`);
  }

  return auditMappings;
}

function fieldForPreviewMapping(preview, mapping) {
  const frameId = Number.isInteger(mapping.frameId) ? mapping.frameId : 0;
  const debugField = (preview?.debugFields || []).find((field) => {
    const fieldFrameId = Number.isInteger(field.frameId) ? field.frameId : 0;
    return fieldFrameId === frameId && field.index === mapping.index;
  });

  return {
    ...(debugField || {}),
    ...mapping,
    value: debugField?.value ?? mapping.value,
    options: debugField?.options || mapping.options || []
  };
}

function currentAuditValue(preview, mapping) {
  const field = fieldForPreviewMapping(preview, mapping);
  const currentValue = String(field.value ?? "").trim();

  if (currentValue && !isPlaceholderValue(currentValue)) {
    return currentValue;
  }

  return mapping.value;
}

function toBackendField(field, index) {
  return {
    ...field,
    index,
    originalIndex: Number.isInteger(field.originalIndex) ? field.originalIndex : field.index,
    frameId: field.frameId
  };
}

function toPreviewMapping(field, mapping) {
  const value = String(mapping.value ?? "").trim();
  const originalIndex = Number.isInteger(field.originalIndex) ? field.originalIndex : field.index;

  return {
    index: originalIndex,
    label: field.label,
    name: field.name || "",
    id: field.id || "",
    placeholder: field.placeholder || "",
    ariaLabel: field.ariaLabel || "",
    tag: field.tag || "",
    type: field.type || "",
    options: field.options || [],
    value,
    source: mapping.source || "llm",
    confidence: Number(mapping.confidence || 0.8),
    frameId: field.frameId,
    frameUrl: field.frameUrl || "",
    reviewId: `${Number.isInteger(field.frameId) ? field.frameId : 0}:${originalIndex}`
  };
}

async function refreshPreviewAfterAiFill() {
  const response = await sendToActiveTab({ type: "PREVIEW_AUTOFILL" });
  if (response?.ok) {
    lastPreview = response.result;
    renderReview(lastPreview);
  }
}

function reportRemainingRequiredFields(preview) {
  const required = (preview?.unmappedFields || []).filter((field) => field.required);

  if (!required.length) {
    addMessage("agent", "No remaining required fields were detected in the preview.");
    return;
  }

  addMessage("agent", `Still unresolved required field(s): ${required.length}.`);
  for (const field of required.slice(0, 8)) {
    addMessage("agent", `${field.label}: ${field.unfilledReason || "No safe answer was available."}`);
  }
}

async function showDebugFields() {
  if (!lastPreview) {
    await previewCurrentPage();
  }

  const fields = (lastPreview?.debugFields || []).filter(isDebugRelevantField).slice(0, 16);

  if (!fields.length) {
    addMessage("agent", "Debug: I do not see policy-like fields in the current preview.");
    return;
  }

  for (const field of fields) {
    addMessage("agent", debugFieldSummary(field));
  }
}

async function showDropdownDebug() {
  setBusy("Testing dropdown option discovery...");
  const response = await sendToActiveTab({ type: "DEBUG_DROPDOWNS" });

  if (!response?.ok) {
    showError(response?.error || "I could not test dropdowns on this page.");
    return;
  }

  const dropdowns = response.result?.dropdowns || [];
  addMessage("agent", `Dropdown debug: found ${dropdowns.length} dropdown-like field(s).`);

  if (!dropdowns.length) {
    assistantStatus.textContent = "No dropdowns";
    return;
  }

  for (const dropdown of dropdowns.slice(0, 20)) {
    addMessage("agent", dropdownDebugSummary(dropdown));
  }

  assistantStatus.textContent = "Dropdown debug complete";
}

function dropdownDebugSummary(dropdown) {
  const options = (dropdown.options || [])
    .map((option) => option.label || option.value)
    .filter(Boolean)
    .slice(0, 12)
    .join(" | ");

  return [
    `Dropdown #${dropdown.index}`,
    `label: ${truncateDebug(dropdown.label || "")}`,
    `tag/type: ${truncateDebug([dropdown.tag, dropdown.type].filter(Boolean).join("/"))}`,
    `value: ${truncateDebug(dropdown.value || "")}`,
    `dynamic=${Boolean(dropdown.isDynamic)} options=${dropdown.optionsFound || 0}`,
    `options: ${truncateDebug(options)}`
  ].join("\n");
}

function isDebugRelevantField(field) {
  const haystack = [
    field.label,
    field.rawLabel,
    field.value,
    field.questionText,
    field.surroundingText,
    field.nearbyText,
    field.haystack
  ].map((item) => String(item || "").toLowerCase()).join(" ");

  return field.isPolicy
    || /(deutsche|telekom|softbank|t-mobile|t mobile|sponsor|visa|authorized|military|relative|contractor|dealer|relocat|select one|\byes\b|\bno\b)/.test(haystack);
}

function debugFieldSummary(field) {
  const options = (field.options || [])
    .map((option) => option.label || option.value)
    .filter(Boolean)
    .slice(0, 8)
    .join(" | ");

  return [
    `Debug field #${field.index}`,
    `tag/type: ${truncateDebug([field.tag, field.type].filter(Boolean).join("/"))}`,
    `label: ${truncateDebug(field.label || field.rawLabel || "")}`,
    `value: ${truncateDebug(field.value || "")}`,
    `question: ${truncateDebug(field.questionText || "")}`,
    `nearby: ${truncateDebug(field.nearbyText || "")}`,
    `options: ${truncateDebug(options)}`,
    `policy=${Boolean(field.isPolicy)} ask=${Boolean(field.shouldAsk)}`
  ].join("\n");
}

function truncateDebug(value) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > 260 ? `${text.slice(0, 257)}...` : text;
}

function isAiAskableField(field) {
  if (!field?.answerKey || field.needsManualUpload) {
    return false;
  }

  if (isOptionLikeField(field) && !(field.options || []).length) {
    return false;
  }

  const label = String(field.label || "").trim().toLowerCase();
  if (!label || /^(english|settings|phone extension)$/i.test(label)) {
    return false;
  }

  const haystack = aiFieldHaystack(field);
  if (isCoreProfileField(haystack)) {
    return false;
  }

  const value = String(field.value || "").trim();

  if (isAiPolicyQuestion(haystack)) {
    return true;
  }

  if (isStructuredAutofillField(haystack)) {
    return false;
  }

  if (!value) {
    return true;
  }

  return /^(select one|select|choose|none selected|no selection)$/i.test(value);
}

function isOptionLikeField(field) {
  return field.tag === "select"
    || field.tag === "button"
    || field.type === "combobox"
    || /listbox|combobox/i.test([field.type, field.ariaLabel, field.surroundingText].join(" "))
    || /selectwidget|selectshowall/i.test(field.dataAutomationId || "");
}

function aiFieldHaystack(field) {
  const label = String(field.label || "").trim();
  const includeSurroundingText = !label || isLowInformationText(label);
  return [
    label,
    field.name,
    field.id,
    field.placeholder,
    field.ariaLabel,
    includeSurroundingText ? field.questionText : "",
    includeSurroundingText ? field.surroundingText : "",
    includeSurroundingText ? field.nearbyText : ""
  ].map((item) => String(item || "").toLowerCase()).join(" ");
}

function isLowInformationText(value) {
  return /^(yes|required yes|yes required|no|required no|no required|yes\s*no|no\s*yes|select one|required select one|select one required|required|true false|false true)$/i.test(String(value || "").trim());
}

function isPlaceholderValue(value) {
  return /^(select one|select|choose|none selected|no selection|mm\/yyyy|yyyy|mm\/dd\/yyyy|type here|\s*)$/i.test(String(value || "").trim());
}

function isAiPolicyQuestion(haystack) {
  return [
    /(18 years of age|at least 18|proof of age|minimum age)/,
    /(authorized|eligible|legally|authorization).*(work|employment)|work authorization|proof of authorization/,
    /(sponsor|sponsorship|visa|h-?1b|f-?1|opt|cpt|tn|ead|work permit)/,
    /(now|ever|previously|formerly|current|directly).*(employed|worked|work|contractor|dealer|affiliate|subsidiar|paycheck|w-?2)/,
    /(employed|worked|work|contractor|dealer|affiliate|subsidiar|paycheck|w-?2).*(now|ever|previously|formerly|current|directly)/,
    /(relatives?|family member|spouse|domestic partner).*(employed|work|working|military|armed forces|served|service)/,
    /(military|armed forces|served|service|veteran)/,
    /(interested in relocating|relocation|relocating)/
  ].some((pattern) => pattern.test(haystack));
}

function isStructuredAutofillField(haystack) {
  return [
    /\b(first|middle|last|preferred|full|legal)\s+name\b/,
    /\bname\s+(first|middle|last|preferred|full|legal)\b/,
    /^name$/,
    /\bemail\b|\be-mail\b/,
    /\bphone\b|\bmobile\b|\bcell\b|\btelephone\b/,
    /\baddress\b|\bstreet\b|\bcity\b|\bstate\b|\bprovince\b|\bpostal\b|\bzip\b|\bcountry\b/,
    /\bwork experience\b/,
    /\bemployment\s+(history|section|row)\b/,
    /\beducation\b/,
    /\bschool\b/,
    /\bdegree\b/,
    /\bfield of study\b/,
    /\bresume\b/,
    /\bcv\b/,
    /\bwebsites?\b/,
    /\bsocial network\b/,
    /\blinkedin\b/,
    /\bfacebook\b/,
    /\btwitter\b/,
    /\bcompany name\b/,
    /\bjob title\b/,
    /\bwork location\b/,
    /^from\b/,
    /^to\b/,
    /\bcurrent value is\b/,
    /\bmm\s*\/?\s*yyyy\b/,
    /\brole description\b/,
    /\bcertifications?\b/,
    /\blanguages?\b/
  ].some((pattern) => pattern.test(haystack));
}

function isCoreProfileField(haystack) {
  return [
    /\b(first|middle|last|preferred|full|legal)\s+name\b/,
    /\bname\s+(first|middle|last|preferred|full|legal)\b/,
    /^name$/,
    /\bemail\b|\be-mail\b/,
    /\bphone\b|\bmobile\b|\bcell\b|\btelephone\b/,
    /\baddress\b|\bstreet\b|\bcity\b|\bstate\b|\bprovince\b|\bpostal\b|\bzip\b|\bcountry\b/
  ].some((pattern) => pattern.test(haystack));
}

function mergeAiMappingsIntoPreview(preview, mappings) {
  if (!preview || !mappings.length) {
    return preview;
  }

  const mappingKey = (mapping) => `${Number.isInteger(mapping.frameId) ? mapping.frameId : 0}:${mapping.index}`;
  const answeredKeys = new Set(mappings.map(mappingKey));
  const existing = new Map((preview.mappings || []).map((mapping) => [mappingKey(mapping), mapping]));

  for (const mapping of mappings) {
    existing.set(mappingKey(mapping), mapping);
  }

  return {
    ...preview,
    mapped: existing.size,
    mappings: Array.from(existing.values()),
    unmappedFields: (preview.unmappedFields || []).filter((field) => {
      const key = `${Number.isInteger(field.frameId) ? field.frameId : 0}:${field.index}`;
      return !answeredKeys.has(key);
    })
  };
}

async function trackApplication() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const selected = getSelectedMappings();
  const payload = {
    url: tab?.url || lastPreview?.page?.url || "",
    title: tab?.title || lastPreview?.page?.title || "",
    status: "filled",
    filledCount: lastFillResult?.filled || 0,
    mappedCount: selected.length || lastPreview?.mapped || 0,
    source: "chrome_extension_assistant",
    createdAt: new Date().toISOString()
  };

  const response = await chrome.runtime.sendMessage({
    type: "TRACK_APPLICATION",
    payload
  });

  if (!response?.ok) {
    showError(response?.error || "I could not track this application.");
    return;
  }

  addMessage("agent", "Tracked this application locally.");
}

async function saveAnswerFromChat(text) {
  if (!lastPreview?.unmappedFields?.length) {
    return false;
  }

  const [questionPart, ...answerParts] = text.split(":");
  const answer = answerParts.join(":").trim();
  const askable = lastPreview.unmappedFields.filter((field) => !field.needsManualUpload);
  let field = null;

  if (answer) {
    const question = questionPart.trim().toLowerCase();
    field = askable.find((item) => item.label.toLowerCase().includes(question) || question.includes(item.label.toLowerCase()));
  } else if (askable.length === 1) {
    field = askable[0];
  }

  const value = answer || text.trim();
  if (!field || !value) {
    return false;
  }

  const { candidateProfile } = await chrome.storage.local.get("candidateProfile");
  const profile = candidateProfile || {};
  profile.answers = { ...(profile.answers || {}), [field.answerKey]: value };
  await chrome.storage.local.set({ candidateProfile: profile });
  addMessage("agent", `Saved an answer for "${field.label}". Previewing again.`);
  await previewCurrentPage();
  return true;
}

async function initAssistant() {
  const { candidateProfile, settings } = await chrome.storage.local.get(["candidateProfile", "settings"]);
  const hasProfile = Boolean(candidateProfile?.email || candidateProfile?.firstName);
  const targetCountry = settings?.targetCountry || "canada";

  assistantStatus.textContent = hasProfile ? "Profile loaded" : "Profile needed";
  updateCountryButtons(targetCountry);
  await refreshAiUsage(true);
  if (!aiUsageRefreshTimer) {
    aiUsageRefreshTimer = setInterval(() => refreshAiUsage(true), 15000);
  }
  addMessage("agent", "I can help fill this application step by step. Is this role in the USA or Canada?");
  addMessage("agent", "Choose a country first, then preview. I will stop before final submission.");
}

async function setTargetCountry(targetCountry) {
  const { settings } = await chrome.storage.local.get("settings");
  const nextSettings = { ...(settings || {}), targetCountry };
  await chrome.storage.local.set({ settings: nextSettings });
  updateCountryButtons(targetCountry);
}

function updateCountryButtons(targetCountry) {
  countryButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.country === targetCountry);
  });
}

async function sendToActiveTab(message) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  if (!tab?.id) {
    return { ok: false, error: "No active tab found." };
  }

  const injected = await injectContentScript(tab.id);
  if (!injected) {
    return {
      ok: false,
      error: accessErrorMessage(tab.url)
    };
  }

  return sendFrameAwareMessage(tab, message);
}

async function injectContentScript(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      files: ["src/content.js"]
    });
    return true;
  } catch (error) {
    return false;
  }
}

async function sendFrameAwareMessage(tab, message) {
  const frames = await getTabFrames(tab.id);

  if (message.type === "APPLY_AUTOFILL_MAPPINGS") {
    return sendApplyMappings(tab.id, frames, message.mappings || []);
  }

  const responses = await Promise.all(
    frames.map(async (frame) => {
      try {
        const response = await chrome.tabs.sendMessage(tab.id, message, { frameId: frame.frameId });
        return response?.ok ? { frame, response } : null;
      } catch (error) {
        return null;
      }
    })
  );
  const successful = responses.filter(Boolean);

  if (message.type === "SCAN_FIELDS") {
    return aggregateScanResponses(successful, frames.length);
  }

  if (message.type === "PREVIEW_AUTOFILL") {
    return aggregatePreviewResponses(successful, tab, frames.length);
  }

  if (message.type === "DEBUG_DROPDOWNS") {
    return aggregateDropdownDebugResponses(successful, frames.length);
  }

  return successful[0]?.response || { ok: false, error: accessErrorMessage(tab.url) };
}

async function getTabFrames(tabId) {
  try {
    const frames = await chrome.webNavigation.getAllFrames({ tabId });
    return frames?.length ? frames : [{ frameId: 0, url: "" }];
  } catch (error) {
    return [{ frameId: 0, url: "" }];
  }
}

function aggregateScanResponses(successful, frameCount) {
  const fields = [];
  let count = 0;

  for (const { frame, response } of successful) {
    const frameFields = response.fields || [];
    count += Number(response.count || frameFields.length || 0);
    fields.push(...frameFields.map((field) => ({ ...field, frameId: frame.frameId })));
  }

  return { ok: true, count, fields, frameCount, accessibleFrameCount: successful.length };
}

function aggregatePreviewResponses(successful, tab, frameCount) {
  let scanned = 0;
  const mappings = [];
  const unmappedFields = [];
  const manualTasks = [];
  const debugFields = [];

  for (const { frame, response } of successful) {
    const result = response.result || {};
    scanned += Number(result.scanned || 0);

    for (const mapping of result.mappings || []) {
      mappings.push({
        ...mapping,
        frameId: frame.frameId,
        frameUrl: frame.url || "",
        reviewId: `${frame.frameId}:${mapping.index}`
      });
    }

    for (const field of result.unmappedFields || []) {
      unmappedFields.push({
        ...field,
        frameId: frame.frameId,
        frameUrl: frame.url || "",
        reviewId: `${frame.frameId}:question:${field.index}`
      });
    }

    for (const field of result.debugFields || []) {
      debugFields.push({
        ...field,
        frameId: frame.frameId,
        frameUrl: frame.url || ""
      });
    }

    for (const task of result.manualTasks || []) {
      manualTasks.push({
        ...task,
        frameId: frame.frameId,
        frameUrl: frame.url || ""
      });
    }
  }

  return {
    ok: true,
    result: {
      scanned,
      mapped: mappings.length,
      frameCount,
      accessibleFrameCount: successful.length,
      mappings,
      unmappedFields,
      debugFields,
      manualTasks,
      page: {
        url: tab.url || "",
        title: tab.title || ""
      }
    }
  };
}

function aggregateDropdownDebugResponses(successful, frameCount) {
  const dropdowns = [];

  for (const { frame, response } of successful) {
    const result = response.result || {};
    for (const dropdown of result.dropdowns || []) {
      dropdowns.push({
        ...dropdown,
        frameId: frame.frameId,
        frameUrl: frame.url || ""
      });
    }
  }

  return {
    ok: true,
    result: {
      count: dropdowns.length,
      frameCount,
      accessibleFrameCount: successful.length,
      dropdowns
    }
  };
}

async function sendApplyMappings(tabId, frames, mappings) {
  const byFrame = new Map();

  for (const mapping of mappings) {
    const frameId = Number.isInteger(mapping.frameId) ? mapping.frameId : 0;
    const { frameId: _frameId, frameUrl: _frameUrl, reviewId: _reviewId, ...cleanMapping } = mapping;
    const existing = byFrame.get(frameId) || [];
    existing.push(cleanMapping);
    byFrame.set(frameId, existing);
  }

  const responses = await Promise.all(
    Array.from(byFrame.entries()).map(async ([frameId, frameMappings]) => {
      if (!frames.some((frame) => frame.frameId === frameId)) {
        return null;
      }

      try {
        const response = await chrome.tabs.sendMessage(
          tabId,
          { type: "APPLY_AUTOFILL_MAPPINGS", mappings: frameMappings },
          { frameId }
        );
        return response?.ok ? { response } : null;
      } catch (error) {
        return null;
      }
    })
  );

  return aggregateFillResponses(responses.filter(Boolean), frames.length);
}

function aggregateFillResponses(successful, frameCount) {
  const result = {
    scanned: 0,
    mapped: 0,
    filled: 0,
    frameCount,
    accessibleFrameCount: successful.length,
    failures: []
  };

  for (const { response } of successful) {
    const frameResult = response.result || {};
    result.scanned += Number(frameResult.scanned || 0);
    result.mapped += Number(frameResult.mapped || 0);
    result.filled += Number(frameResult.filled || 0);
    result.failures.push(...(frameResult.failures || []));
  }

  return { ok: true, result };
}

function renderReview(preview) {
  reviewList.replaceChildren();
  fillSelectedButton.disabled = preview.mappings.length === 0;
  fillSelectedButton.classList.toggle("is-primary-action", preview.mappings.length > 0);
  const hasAskableFields = (preview.unmappedFields || []).some((field) => !field.needsManualUpload);
  askAiButton.disabled = !hasAskableFields;
  saveAnswersButton.disabled = !hasAskableFields;
  trackButton.disabled = true;

  const frameText = preview.frameCount
    ? ` across ${preview.accessibleFrameCount}/${preview.frameCount} accessible frame(s)`
    : "";
  addMessage("agent", `I scanned ${preview.scanned} field(s) and mapped ${preview.mapped}${frameText}. Review the checked cards, then fill selected.`);

  appendReviewHeading("Ready to fill");
  for (const mapping of preview.mappings) {
    const item = document.createElement("label");
    item.className = "assistant-card";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkbox.dataset.reviewId = mapping.reviewId || `0:${mapping.index}`;

    const body = document.createElement("span");
    body.className = "review-body";

    const label = document.createElement("strong");
    label.textContent = mapping.label;

    const value = document.createElement("span");
    value.className = "review-value";
    value.textContent = `${String(mapping.value).slice(0, 140)} (${mapping.source}, ${Math.round((mapping.confidence || 0) * 100)}%)`;

    body.append(label, value);
    item.append(checkbox, body);
    reviewList.append(item);
  }

  if (preview.manualTasks?.length) {
    appendReviewHeading("Manual tasks");
  }

  for (const task of preview.manualTasks || []) {
    const item = document.createElement("div");
    item.className = "assistant-card manual-task";
    const body = document.createElement("span");
    body.className = "review-body";
    const label = document.createElement("strong");
    label.textContent = task.label || "Resume upload";
    const value = document.createElement("span");
    value.className = "review-value";
    value.textContent = task.resumeFileName
      ? `Upload ${task.resumeFileName} manually if the page still needs it.`
      : "Upload your resume manually if the page still needs it.";
    body.append(label, value);
    item.append(body);
    reviewList.append(item);
  }

  const askableFields = (preview.unmappedFields || []).filter((field) => !field.needsManualUpload);
  if (askableFields.length) {
    appendReviewHeading("I need your answer");
    addMessage("agent", "Some questions are not in your profile yet. You can answer them below, or click Ask AI to answer all visible missing fields in one request.");
  }

  for (const field of askableFields) {
    const item = document.createElement("label");
    item.className = "assistant-card question-item";
    const body = document.createElement("span");
    body.className = "review-body";

    const label = document.createElement("strong");
    label.textContent = field.label;

    const input = buildAnswerInput(field);
    body.append(label);

    if (field.required || field.unfilledReason) {
      const reason = document.createElement("span");
      reason.className = "review-value";
      reason.textContent = `${field.required ? "Required. " : ""}${field.unfilledReason || "Not filled yet."}`;
      body.append(reason);
    }

    body.append(input);
    item.append(body);
    reviewList.append(item);
  }

  assistantStatus.textContent = "Preview ready";
}

function appendReviewHeading(text) {
  const heading = document.createElement("div");
  heading.className = "review-heading";
  heading.textContent = text;
  reviewList.append(heading);
}

function buildAnswerInput(field) {
  let input;

  if (field.options?.length) {
    input = document.createElement("select");
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Select answer";
    input.append(empty);
    for (const option of field.options) {
      const item = document.createElement("option");
      item.value = option.label || option.value;
      item.textContent = option.label || option.value;
      input.append(item);
    }
  } else if (field.tag === "textarea") {
    input = document.createElement("textarea");
    input.rows = 3;
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Type answer to remember";
  }

  input.className = "answer-input";
  input.dataset.answerKey = field.answerKey;
  input.dataset.label = field.label;
  return input;
}

function collectNewAnswers() {
  const answers = {};

  for (const input of reviewList.querySelectorAll(".answer-input")) {
    const value = input.value.trim();
    const key = input.dataset.answerKey;
    if (key && value) {
      answers[key] = value;
    }
  }

  return answers;
}

function getSelectedMappings() {
  if (!lastPreview?.mappings) {
    return [];
  }

  const selectedIndexes = new Set(
    Array.from(reviewList.querySelectorAll("input[type='checkbox']:checked"))
      .map((checkbox) => checkbox.dataset.reviewId)
  );

  return lastPreview.mappings.filter((mapping) => {
    const reviewId = mapping.reviewId || `0:${mapping.index}`;
    return selectedIndexes.has(reviewId);
  });
}

function addMessage(author, text) {
  const message = document.createElement("div");
  message.className = `chat-message ${author}`;
  message.textContent = text;
  chatLog.append(message);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function setAiWorking(isWorking, message = "") {
  aiStatusPanel?.classList.toggle("is-working", isWorking);
  if (aiStatusText) {
    aiStatusText.textContent = message || (isWorking ? "AI working..." : "AI idle");
  }
  askAiButton.disabled = isWorking || !lastPreview?.unmappedFields?.some((field) => !field.needsManualUpload);
}

async function refreshAiUsage(silent = false) {
  try {
    const response = await chrome.runtime.sendMessage({ type: "GET_AI_USAGE" });
    if (response?.ok) {
      updateAiUsage(response.payload?.aiUsage);
      return;
    }

    if (!silent) {
      addMessage("agent", response?.error || "Could not read AI request usage.");
    }
  } catch (error) {
    if (!silent) {
      addMessage("agent", `Could not read AI request usage: ${error.message}`);
    }
  }
}

function updateAiUsage(usage) {
  if (!usage || !aiUsageText) {
    return;
  }

  const used = Number(usage.requestsLastMinute || 0);
  const limit = Number(usage.limitPerMinute || 40);
  const remaining = Number.isFinite(Number(usage.remainingThisMinute))
    ? Number(usage.remainingThisMinute)
    : Math.max(limit - used, 0);
  aiUsageText.textContent = `${used}/${limit} req/min (${remaining} left)`;
  aiStatusPanel?.classList.toggle("is-near-limit", limit > 0 && used >= Math.ceil(limit * 0.8));
}

function setBusy(message) {
  assistantStatus.textContent = "Working";
  addMessage("agent", message);
}

function showError(message) {
  assistantStatus.textContent = "Needs attention";
  addMessage("agent", message);
}

function accessErrorMessage(url = "") {
  if (/^(chrome|edge|about|devtools|chrome-extension):/i.test(url)) {
    return "Chrome blocks extensions on browser/internal pages. Open a normal https page.";
  }

  if (/^file:/i.test(url)) {
    return "Enable Allow access to file URLs for this extension, then reload the page.";
  }

  return "Could not inject the autofill script. Reload the extension and refresh this page.";
}
