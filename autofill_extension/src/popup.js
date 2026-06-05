const result = document.getElementById("result");
const profileStatus = document.getElementById("profileStatus");
const autofillButton = document.getElementById("autofillButton");
const scanButton = document.getElementById("scanButton");
const optionsButton = document.getElementById("optionsButton");

initPopup();

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
});

scanButton.addEventListener("click", async () => {
  setBusy("Scanning page...");
  const response = await sendToActiveTab({ type: "SCAN_FIELDS" });

  if (!response?.ok) {
    showError(response?.error || "Could not scan this page.");
    return;
  }

  result.textContent = `Found ${response.count} fillable field(s).`;
});

optionsButton.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
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

  try {
    return await chrome.tabs.sendMessage(tab.id, message);
  } catch (error) {
    return {
      ok: false,
      error: "This page cannot be accessed by the extension. Try a normal https page."
    };
  }
}

function setBusy(message) {
  result.textContent = message;
}

function showError(message) {
  result.textContent = message;
}
