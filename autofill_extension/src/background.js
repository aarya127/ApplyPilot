chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.local.get(["candidateProfile", "settings"]);

  if (!existing.candidateProfile) {
    await chrome.storage.local.set({
      candidateProfile: {
        firstName: "Sample",
        lastName: "Candidate",
        fullName: "Sample Candidate",
        email: "candidate@example.com",
        phone: "5550100000",
        linkedin: "",
        github: "",
        portfolio: "",
        location: "Sample City, ST",
        addresses: {
          canada: {
            line1: "",
            line2: "",
            city: "",
            province: "",
            postalCode: "",
            country: "Canada",
            fullAddress: ""
          },
          usa: {
            line1: "",
            line2: "",
            city: "",
            state: "",
            zipCode: "",
            country: "United States",
            fullAddress: ""
          }
        },
        school: "Sample University",
        degree: "",
        graduationDate: "",
        currentOrPreviousEmployer: "",
        currentOrPreviousJobTitle: "",
        workAuthorization: "Yes",
        needsSponsorship: "No",
        veteranStatus: "No",
        canadianCitizen: "Yes",
        usPermanentResident: "Yes",
        subjectToAgreement: "No",
        relocation: "Open to relocation",
        salary: "Negotiable",
        resumeFileName: "",
        resumeFacts: {
          skills: [],
          education: [],
          experience: [],
          projects: [],
          rawTextFile: ""
        },
        demographics: {
          race: "",
          ethnicity: "",
          hispanicLatino: "",
          gender: "",
          genderIdentity: ""
        },
        answers: {
          sponsorship: "No",
          veteranStatus: "No",
          workAuthorization: "Yes",
          canadianCitizen: "Yes",
          usPermanentResident: "Yes",
          subjectToAgreement: "No",
          previouslyEmployedByCompany: "No",
          recruitingMessages: "No",
          relocation: "Open to relocation",
          salary: "Negotiable"
        }
      }
    });
  }

  if (!existing.settings) {
    await chrome.storage.local.set({
      settings: {
        backendMapperUrl: "",
        autoFillDynamicFields: false,
        autoFillSensitiveFields: false,
        requireReviewBeforeSubmit: true,
        backendBaseUrl: "http://127.0.0.1:8000",
        targetCountry: "canada"
      }
    });
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "MAP_FIELDS_WITH_BACKEND") {
    mapFieldsWithBackend(message.payload)
      .then((payload) => sendResponse({ ok: true, payload }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));

    return true;
  }

  if (message?.type === "TRACK_APPLICATION") {
    trackApplication(message.payload)
      .then((payload) => sendResponse({ ok: true, payload }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));

    return true;
  }

  return false;
});

async function mapFieldsWithBackend(payload) {
  const { settings } = await chrome.storage.local.get("settings");
  const endpoint = settings?.backendMapperUrl?.trim()
    || joinUrl(settings?.backendBaseUrl, "/map-fields");

  if (!endpoint) {
    return { mappings: [] };
  }

  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    throw new Error(`Mapper failed with ${response.status}`);
  }

  return response.json();
}

async function trackApplication(payload) {
  const { settings } = await chrome.storage.local.get("settings");
  const endpoint = joinUrl(settings?.backendBaseUrl, "/track-application");

  if (!endpoint) {
    return { tracked: false };
  }

  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    throw new Error(`Tracker failed with ${response.status}`);
  }

  return response.json();
}

function joinUrl(baseUrl, path) {
  const base = String(baseUrl || "").trim().replace(/\/+$/, "");

  if (!base) {
    return "";
  }

  return `${base}${path}`;
}
