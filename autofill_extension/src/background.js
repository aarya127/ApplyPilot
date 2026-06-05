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
        workAuthorization: "Yes",
        needsSponsorship: "No",
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
          gender: "",
          genderIdentity: ""
        },
        answers: {
          sponsorship: "No",
          workAuthorization: "Yes",
          canadianCitizen: "Yes",
          usPermanentResident: "Yes",
          subjectToAgreement: "No",
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
        autoFillDynamicFields: true,
        autoFillSensitiveFields: false,
        requireReviewBeforeSubmit: true
      }
    });
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "MAP_FIELDS_WITH_BACKEND") {
    return false;
  }

  mapFieldsWithBackend(message.payload)
    .then((payload) => sendResponse({ ok: true, payload }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));

  return true;
});

async function mapFieldsWithBackend(payload) {
  const { settings } = await chrome.storage.local.get("settings");
  const endpoint = settings?.backendMapperUrl?.trim();

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
