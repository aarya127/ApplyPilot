const result = document.getElementById("result");
const reviewList = document.getElementById("reviewList");
const profileStatus = document.getElementById("profileStatus");
const autofillButton = document.getElementById("autofillButton");
const previewButton = document.getElementById("previewButton");
const scanButton = document.getElementById("scanButton");
const saveAnswersButton = document.getElementById("saveAnswersButton");
const fillSelectedButton = document.getElementById("fillSelectedButton");
const trackButton = document.getElementById("trackButton");
const optionsButton = document.getElementById("optionsButton");

let lastPreview = null;
let lastFillResult = null;

initPopup();

previewButton.addEventListener("click", async () => {
  setBusy("Building autofill preview...");
  const response = await sendToActiveTab({ type: "PREVIEW_AUTOFILL" });

  if (!response?.ok) {
    showError(response?.error || "Could not preview this page.");
    return;
  }

  lastPreview = response.result;
  lastFillResult = null;
  renderReview(response.result);
});

autofillButton.addEventListener("click", async () => {
  setBusy("Filling current page...");
  const response = await sendToActiveTab({ type: "AUTOFILL_PAGE" });

  if (!response?.ok) {
    showError(response?.error || "Could not autofill this page.");
    return;
  }

  const { scanned, mapped, filled, failures } = response.result;
  result.textContent = `Scanned ${scanned} fields, mapped ${mapped}, filled ${filled}.`;

  if (failures?.length) {
    result.textContent += ` ${failures.length} field(s) need review.`;
  }

  lastFillResult = response.result;
  trackButton.disabled = false;
});

fillSelectedButton.addEventListener("click", async () => {
  const selected = getSelectedMappings();

  if (!selected.length) {
    result.textContent = "No reviewed fields selected.";
    return;
  }

  setBusy("Filling selected fields...");
  const response = await sendToActiveTab({
    type: "APPLY_AUTOFILL_MAPPINGS",
    mappings: selected
  });

  if (!response?.ok) {
    showError(response?.error || "Could not fill selected fields.");
    return;
  }

  const { filled, failures } = response.result;
  result.textContent = `Filled ${filled} selected field(s).`;

  if (failures?.length) {
    result.textContent += ` ${failures.length} field(s) need review.`;
  }

  lastFillResult = response.result;
  trackButton.disabled = false;
});

saveAnswersButton.addEventListener("click", async () => {
  const answers = collectNewAnswers();

  if (!Object.keys(answers).length) {
    result.textContent = "No new answers entered.";
    return;
  }

  const { candidateProfile } = await chrome.storage.local.get("candidateProfile");
  const profile = candidateProfile || {};
  profile.answers = { ...(profile.answers || {}), ...answers };
  await chrome.storage.local.set({ candidateProfile: profile });

  result.textContent = `Saved ${Object.keys(answers).length} answer(s). Refreshing preview...`;
  const response = await sendToActiveTab({ type: "PREVIEW_AUTOFILL" });

  if (response?.ok) {
    lastPreview = response.result;
    renderReview(response.result);
  }
});

scanButton.addEventListener("click", async () => {
  setBusy("Scanning page...");
  const response = await sendToActiveTab({ type: "SCAN_FIELDS" });

  if (!response?.ok) {
    showError(response?.error || "Could not scan this page.");
    return;
  }

  const frameText = response.frameCount
    ? ` across ${response.accessibleFrameCount}/${response.frameCount} accessible frame(s)`
    : "";
  result.textContent = `Found ${response.count} fillable field(s)${frameText}.`;
});

optionsButton.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

trackButton.addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const selected = getSelectedMappings();
  const payload = {
    url: tab?.url || lastPreview?.page?.url || "",
    title: tab?.title || lastPreview?.page?.title || "",
    status: "filled",
    filledCount: lastFillResult?.filled || 0,
    mappedCount: selected.length || lastPreview?.mapped || 0,
    source: "chrome_extension",
    createdAt: new Date().toISOString()
  };

  const response = await chrome.runtime.sendMessage({
    type: "TRACK_APPLICATION",
    payload
  });

  if (!response?.ok) {
    showError(response?.error || "Could not track this application.");
    return;
  }

  result.textContent = "Application tracked locally.";
});

async function initPopup() {
  const { candidateProfile } = await chrome.storage.local.get("candidateProfile");
  const hasProfile = Boolean(candidateProfile?.email || candidateProfile?.firstName);
  profileStatus.textContent = hasProfile ? "Profile saved" : "Needs profile";
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

  if (message.type === "AUTOFILL_PAGE") {
    return aggregateFillResponses(successful, frames.length);
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
  }

  const unmappedFields = [];
  const manualTasks = [];
  for (const { frame, response } of successful) {
    const result = response.result || {};
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

function accessErrorMessage(url = "") {
  if (/^(chrome|edge|about|devtools|chrome-extension):/i.test(url)) {
    return "Chrome blocks extensions on browser/internal pages. Open a normal https page.";
  }

  if (/^file:/i.test(url)) {
    return "Enable Allow access to file URLs for this extension, then reload the page.";
  }

  return "Could not inject the autofill script. Reload the extension and refresh this page.";
}

function setBusy(message) {
  result.textContent = message;
}

function showError(message) {
  result.textContent = message;
}

function renderReview(preview) {
  reviewList.replaceChildren();
  fillSelectedButton.disabled = preview.mappings.length === 0;
  saveAnswersButton.disabled = !(preview.unmappedFields || []).some((field) => !field.needsManualUpload);
  trackButton.disabled = true;
  const frameText = preview.frameCount
    ? ` across ${preview.accessibleFrameCount}/${preview.frameCount} accessible frame(s)`
    : "";
  result.textContent = `Scanned ${preview.scanned} fields, mapped ${preview.mapped}${frameText}. Review before filling.`;

  for (const mapping of preview.mappings) {
    const item = document.createElement("label");
    item.className = "review-item";

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
    value.textContent = `${String(mapping.value).slice(0, 120)} (${mapping.source}, ${Math.round((mapping.confidence || 0) * 100)}%)`;

    body.append(label, value);
    item.append(checkbox, body);
    reviewList.append(item);
  }

  for (const task of preview.manualTasks || []) {
    const item = document.createElement("div");
    item.className = "review-item manual-task";
    const body = document.createElement("span");
    body.className = "review-body";
    const label = document.createElement("strong");
    label.textContent = task.label || "Resume upload";
    const value = document.createElement("span");
    value.className = "review-value";
    value.textContent = task.resumeFileName
      ? `Manual task: upload ${task.resumeFileName}`
      : "Manual task: upload your resume";
    body.append(label, value);
    item.append(body);
    reviewList.append(item);
  }

  const askableFields = (preview.unmappedFields || []).filter((field) => !field.needsManualUpload);
  if (askableFields.length) {
    const heading = document.createElement("div");
    heading.className = "review-heading";
    heading.textContent = "Missing answers";
    reviewList.append(heading);
  }

  for (const field of askableFields) {
    const item = document.createElement("label");
    item.className = "review-item question-item";
    const body = document.createElement("span");
    body.className = "review-body";

    const label = document.createElement("strong");
    label.textContent = field.label;

    const input = buildAnswerInput(field);
    body.append(label, input);
    item.append(body);
    reviewList.append(item);
  }
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
    input.type = field.type === "checkbox" ? "text" : "text";
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
