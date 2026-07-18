const form = document.getElementById("profileForm");
const resetButton = document.getElementById("resetButton");
const exportButton = document.getElementById("exportButton");
const importInput = document.getElementById("importInput");
const saveStatus = document.getElementById("saveStatus");

const sampleProfile = {
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
};

const sampleSettings = {
  backendMapperUrl: "",
  autoMapAmbiguousFields: true,
  autoFillDynamicFields: false,
  autoFillSensitiveFields: false,
  requireReviewBeforeSubmit: true,
  backendBaseUrl: "http://127.0.0.1:8000",
  targetCountry: "canada"
};

let storedProfile = {};
let storedSettings = {};

loadOptions();

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  try {
    const { candidateProfile, settings } = readForm();
    await chrome.storage.local.set({ candidateProfile, settings });
    storedProfile = candidateProfile;
    storedSettings = settings;
    showStatus("Saved.");
  } catch (error) {
    showStatus(error.message);
  }
});

resetButton.addEventListener("click", async () => {
  await chrome.storage.local.set({
    candidateProfile: sampleProfile,
    settings: sampleSettings
  });
  writeForm(sampleProfile, sampleSettings);
  showStatus("Sample restored.");
});

exportButton.addEventListener("click", async () => {
  const { candidateProfile, settings } = readForm();
  downloadJson("application-autofill-profile.json", { candidateProfile, settings });
});

importInput.addEventListener("change", async () => {
  const file = importInput.files?.[0];

  if (!file) {
    return;
  }

  try {
    const parsed = JSON.parse(await file.text());
    const candidateProfile = parsed.candidateProfile || parsed.profile || parsed;
    const settings = parsed.settings || sampleSettings;

    await chrome.storage.local.set({ candidateProfile, settings });
    writeForm(candidateProfile, settings);
    showStatus("Imported.");
  } catch (error) {
    showStatus("Import failed: invalid JSON.");
  } finally {
    importInput.value = "";
  }
});

async function loadOptions() {
  const { candidateProfile, settings } = await chrome.storage.local.get([
    "candidateProfile",
    "settings"
  ]);

  writeForm(candidateProfile || sampleProfile, settings || sampleSettings);
}

function writeForm(profile, settings) {
  profile.answers = preserveApplicationLocationAnswers(profile);
  storedProfile = profile;
  storedSettings = settings;

  for (const [key, value] of Object.entries(profile)) {
    if (key === "answers" || key === "addresses" || key === "demographics" || key === "resumeFacts") {
      continue;
    }

    const input = form.elements[key];
    if (input) {
      input.value = value || "";
    }
  }

  const canada = profile.addresses?.canada || {};
  const usa = profile.addresses?.usa || {};
  const demographics = profile.demographics || {};

  setValue("canadaLine1", canada.line1);
  setValue("canadaCity", canada.city);
  setValue("canadaProvince", canada.province);
  setValue("canadaPostalCode", canada.postalCode);
  setValue("canadaFullAddress", canada.fullAddress);
  setValue("usaLine1", usa.line1);
  setValue("usaCity", usa.city);
  setValue("usaState", usa.state);
  setValue("usaZipCode", usa.zipCode);
  setValue("usaFullAddress", usa.fullAddress);
  setValue("race", demographics.race);
  setValue("ethnicity", demographics.ethnicity);
  setValue("hispanicLatino", demographics.hispanicLatino);
  setValue("gender", demographics.gender);
  setValue("genderIdentity", demographics.genderIdentity);

  form.elements.answersJson.value = JSON.stringify(profile.answers || {}, null, 2);
  form.elements.resumeFactsJson.value = JSON.stringify(profile.resumeFacts || {}, null, 2);
  form.elements.backendMapperUrl.value = settings.backendMapperUrl || "";
  form.elements.backendBaseUrl.value = settings.backendBaseUrl || "";
  form.elements.targetCountry.value = settings.targetCountry || "canada";
  form.elements.autoMapAmbiguousFields.checked = settings.autoMapAmbiguousFields !== false;
  form.elements.autoFillDynamicFields.checked = settings.autoFillDynamicFields === true;
  form.elements.autoFillSensitiveFields.checked = settings.autoFillSensitiveFields === true;
  form.elements.requireReviewBeforeSubmit.checked = settings.requireReviewBeforeSubmit !== false;
}

function readForm() {
  let answers;
  let resumeFacts;

  try {
    answers = JSON.parse(form.elements.answersJson.value || "{}");
  } catch (error) {
    throw new Error("Saved answers must be valid JSON.");
  }

  try {
    resumeFacts = JSON.parse(form.elements.resumeFactsJson.value || "{}");
  } catch (error) {
    throw new Error("Resume facts must be valid JSON.");
  }

  const candidateProfile = {
    ...storedProfile,
    firstName: valueOf("firstName"),
    lastName: valueOf("lastName"),
    fullName: valueOf("fullName"),
    email: valueOf("email"),
    phone: valueOf("phone"),
    linkedin: valueOf("linkedin"),
    github: valueOf("github"),
    portfolio: valueOf("portfolio"),
    location: valueOf("location"),
    addresses: {
      canada: {
        line1: valueOf("canadaLine1"),
        line2: "",
        city: valueOf("canadaCity"),
        province: valueOf("canadaProvince"),
        postalCode: valueOf("canadaPostalCode"),
        country: "Canada",
        fullAddress: valueOf("canadaFullAddress")
      },
      usa: {
        line1: valueOf("usaLine1"),
        line2: "",
        city: valueOf("usaCity"),
        state: valueOf("usaState"),
        zipCode: valueOf("usaZipCode"),
        country: "United States",
        fullAddress: valueOf("usaFullAddress")
      }
    },
    school: valueOf("school"),
    degree: valueOf("degree"),
    graduationDate: valueOf("graduationDate"),
    currentOrPreviousEmployer: valueOf("currentOrPreviousEmployer"),
    currentOrPreviousJobTitle: valueOf("currentOrPreviousJobTitle"),
    workAuthorization: valueOf("workAuthorization"),
    needsSponsorship: valueOf("needsSponsorship"),
    veteranStatus: valueOf("veteranStatus"),
    canadianCitizen: valueOf("canadianCitizen"),
    usPermanentResident: valueOf("usPermanentResident"),
    subjectToAgreement: valueOf("subjectToAgreement"),
    relocation: valueOf("relocation"),
    usaLocation: answers.usaLocation || answers.usaPreferredLocation || "",
    usaCity: answers.usaCity || answers.usaPreferredCity || "",
    usaPreferredLocation: answers.usaPreferredLocation || answers.usaLocation || "",
    usaPreferredCity: answers.usaPreferredCity || answers.usaCity || "",
    canadaLocation: answers.canadaLocation || answers.canadaPreferredLocation || "",
    canadaCity: answers.canadaCity || answers.canadaPreferredCity || "",
    canadaPreferredLocation: answers.canadaPreferredLocation || answers.canadaLocation || "",
    canadaPreferredCity: answers.canadaPreferredCity || answers.canadaCity || "",
    salary: valueOf("salary"),
    resumeFileName: valueOf("resumeFileName"),
    resumeFacts,
    demographics: {
      race: valueOf("race"),
      ethnicity: valueOf("ethnicity"),
      hispanicLatino: valueOf("hispanicLatino"),
      gender: valueOf("gender"),
      genderIdentity: valueOf("genderIdentity")
    },
    answers
  };

  const settings = {
    ...storedSettings,
    backendMapperUrl: valueOf("backendMapperUrl"),
    autoMapAmbiguousFields: form.elements.autoMapAmbiguousFields.checked,
    autoFillDynamicFields: form.elements.autoFillDynamicFields.checked,
    autoFillSensitiveFields: form.elements.autoFillSensitiveFields.checked,
    requireReviewBeforeSubmit: form.elements.requireReviewBeforeSubmit.checked,
    backendBaseUrl: valueOf("backendBaseUrl"),
    targetCountry: valueOf("targetCountry") || "canada"
  };

  return { candidateProfile, settings };
}

function preserveApplicationLocationAnswers(profile) {
  const answers = { ...(profile.answers || {}) };

  for (const key of [
    "usaLocation",
    "usaCity",
    "usaPreferredLocation",
    "usaPreferredCity",
    "canadaLocation",
    "canadaCity",
    "canadaPreferredLocation",
    "canadaPreferredCity"
  ]) {
    if (!answers[key] && profile[key]) {
      answers[key] = profile[key];
    }
  }

  return answers;
}

function valueOf(name) {
  return form.elements[name]?.value?.trim() || "";
}

function setValue(name, value) {
  if (form.elements[name]) {
    form.elements[name].value = value || "";
  }
}

function downloadJson(fileName, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
}

function showStatus(message) {
  saveStatus.textContent = message;
  window.setTimeout(() => {
    if (saveStatus.textContent === message) {
      saveStatus.textContent = "";
    }
  }, 2500);
}
