const chatLog = document.getElementById("chatLog");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");
const reviewList = document.getElementById("reviewList");
const assistantStatus = document.getElementById("assistantStatus");
const scanButton = document.getElementById("scanButton");
const previewButton = document.getElementById("previewButton");
const fillSelectedButton = document.getElementById("fillSelectedButton");
const saveAnswersButton = document.getElementById("saveAnswersButton");
const trackButton = document.getElementById("trackButton");
const optionsButton = document.getElementById("optionsButton");
const countryButtons = Array.from(document.querySelectorAll("[data-country]"));

let lastPreview = null;
let lastFillResult = null;

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
      manualTasks,
      page: {
        url: tab.url || "",
        title: tab.title || ""
      }
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
  saveAnswersButton.disabled = !(preview.unmappedFields || []).some((field) => !field.needsManualUpload);
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
    addMessage("agent", "Some questions are not in your profile yet. Answer them below once and I will remember them.");
  }

  for (const field of askableFields) {
    const item = document.createElement("label");
    item.className = "assistant-card question-item";
    const body = document.createElement("span");
    body.className = "review-body";

    const label = document.createElement("strong");
    label.textContent = field.label;

    const input = buildAnswerInput(field);
    body.append(label, input);
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
