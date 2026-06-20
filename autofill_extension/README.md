# ApplyPilot Chrome Autofill Extension

This is a standalone Chrome extension for job application autofill. It is separate from the Flask HR dashboard and can be configured by any user with their own local profile, resume, and AI API key.

## What It Does

- Stores a candidate profile in Chrome local storage.
- Scans visible `input`, `textarea`, `select`, radio, checkbox, and simple ARIA form controls.
- Extracts field metadata such as label, placeholder, name, id, options, and nearby text.
- Maps common application fields to profile values using local rules.
- Previews mapped fields in the popup so you can review before filling.
- Provides a right-side assistant panel that walks through country selection, preview, filling, missing answers, and tracking.
- Shows unknown questions in the popup, lets you answer them once, and saves those answers for future applications.
- Detects resume upload fields and highlights them as manual tasks.
- Fills fields and dispatches `input`, `change`, and `blur` events so React-style forms usually notice the changes.
- Handles native dropdowns and basic ARIA/listbox-style custom dropdowns.
- Watches for dynamically added form fields with `MutationObserver`.
- Includes a local backend for NVIDIA-hosted LLM field mapping and SQLite application tracking.

Dynamic refilling is off by default because some React/Greenhouse pages rerender after each input event. Turn it on only when a page loads additional steps after the first fill.

## Internal Workflow

The extension uses the same plan-first workflow as the Playwright agent:

```text
scan fields
  -> classify canonical field kinds
  -> discover dropdown options
  -> resolve deterministic profile answers
  -> ask AI only for unresolved/ambiguous fields
  -> preview the fill plan
  -> fill selected fields
  -> verify actual page values
```

Canonical field kinds include values like `contact.email`, `links.linkedin`,
`work.current_or_previous_employer`, and `education.school`. The point is to
decide what a field is before choosing an answer. This prevents surrounding
text from leaking values into the wrong field.

Subprocess documentation lives in:

```text
docs/applypilot_skills/
```

## Load It In Chrome

1. Open `chrome://extensions`.
2. Turn on Developer Mode.
3. Click **Load unpacked**.
4. Select the `autofill_extension` folder.
5. Open the extension options page.
6. Import or enter your private profile.
7. Visit a job application page and click **Autofill page** from the extension popup.

## Assistant Side Panel

The extension also includes a right-side chat-style assistant.

1. Open a job application page.
2. Click the extension icon.
3. Click **Open assistant chat**.
4. Choose **USA** or **Canada** so the extension uses the correct saved address and eligibility context.
5. Click **Preview**.
6. Review checked fields, answer any missing questions, and save answers you want remembered.
7. Click **Fill selected**.
8. Review the actual ATS page before submitting.

The assistant uses the same scanner, mapper, saved answers, and backend as the popup. It is a more guided interface, not a separate profile store.

## Set Up Your Own Private Profile

Personal addresses, work eligibility, and voluntary demographic fields should live outside committed source files. This repo ignores:

```text
autofill_extension/profile.private.json
autofill_extension/*.private.json
autofill_extension/resumes/
autofill_extension/generated/
```

Start from the tracked fake template:

```bash
cp autofill_extension/profile.example.json autofill_extension/profile.private.json
```

Then edit `autofill_extension/profile.private.json` with your own information. Keep the file local; do not commit it.

Profile shape:

```json
{
  "candidateProfile": {
    "firstName": "Your",
    "lastName": "Name",
    "fullName": "Your Name",
    "email": "you@example.com",
    "phone": "5550100000",
    "linkedin": "https://www.linkedin.com/in/your-profile",
    "github": "https://github.com/your-handle",
    "portfolio": "https://your-site.example",
    "addresses": {
      "canada": {
        "line1": "",
        "city": "",
        "province": "",
        "postalCode": "",
        "country": "Canada",
        "fullAddress": ""
      },
      "usa": {
        "line1": "",
        "city": "",
        "state": "",
        "zipCode": "",
        "country": "United States",
        "fullAddress": ""
      }
    },
    "school": "",
    "degree": "",
    "fieldOfStudy": "",
    "graduationDate": "",
    "workAuthorization": "Yes",
    "needsSponsorship": "No",
    "veteranStatus": "No",
    "resumeFileName": "resume.private.pdf",
    "workExperience": [],
    "education": [],
    "links": [],
    "demographics": {},
    "answers": {
      "sponsorship": "No",
      "workAuthorization": "Yes",
      "relocationAssistance": "No",
      "subscribeEmails": "No",
      "acceptTerms": "Yes"
    }
  },
  "settings": {
    "backendBaseUrl": "http://127.0.0.1:8000",
    "autoFillSensitiveFields": false,
    "targetCountry": "usa"
  }
}
```

Recommended profile fields:

- `addresses.canada` and `addresses.usa`: used when the assistant asks which country the role is based in.
- `workExperience`: used for repeatable employment sections.
- `education`: used for repeatable education sections.
- `links`: used for websites/profile links.
- `resumeFileName`: must match a file in `autofill_extension/resumes/`.
- `answers`: reusable answers and defaults for yes/no, authorization, sponsorship, relocation, subscriptions, terms, and custom questions.
- `demographics`: optional self-identification answers. These are only filled when **Fill sensitive optional fields** is enabled.

To load a private profile into Chrome:

1. Open the extension options page.
2. Click **Import JSON**.
3. Choose `autofill_extension/profile.private.json`.
4. Click **Save profile**.

Sensitive optional fields such as race, ethnicity, gender, and gender identity only autofill when **Fill sensitive optional fields** is enabled.

## Resume Parsing

Install the parser dependency:

```bash
python -m pip install -r requirements-dev.txt
```

Place a resume PDF in the ignored local folder:

```text
autofill_extension/resumes/
```

Parse and merge it into the private extension profile:

```bash
python autofill_extension/tools/parse_resume.py autofill_extension/resumes/resume.private.pdf
```

The parser writes ignored artifacts:

```text
autofill_extension/generated/resume_text.private.txt
autofill_extension/generated/resume_parsed.private.json
autofill_extension/profile.private.json
```

Then import `autofill_extension/profile.private.json` from the extension options page.

## Local LLM And Tracking Backend

The backend uses NVIDIA's OpenAI-compatible chat-completions API at:

```text
https://integrate.api.nvidia.com/v1/chat/completions
```

Create an ignored private env file:

```text
autofill_extension/backend/env.private
```

Use this shape:

```text
NVIDIA_API_KEY=your-key-here
NVIDIA_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
PORT=8000
```

Do not place real API keys in tracked files. Keep the key only in `env.private`, which is ignored by git.

Start the backend:

```bash
python autofill_extension/backend/server.py
```

Then set the options page backend base URL to:

```text
http://127.0.0.1:8000
```

Backend endpoints:

- `GET /health`
- `POST /map-fields`
- `POST /track-application`
- `GET /applications`

Application tracking is written to ignored SQLite storage:

```text
autofill_extension/generated/applications.sqlite3
```

The AI mapper receives the visible fields, labels, nearby text, page context, your profile, resume facts, and visible dropdown/radio options. For option-based controls, it should return one of the supplied option labels exactly. The extension also enforces this locally before filling.

## Current Build Status

1. Manifest V3 extension: working.
2. Content script form scanner: working for ordinary page DOM fields.
3. Label/name/id/placeholder/options extraction: working for native controls and simple ARIA controls.
4. Local profile storage: working via `chrome.storage.local`; backend profile sync is not built.
5. Rule matching: working for common contact, address, links, work authorization, sponsorship, relocation, salary, agreement, and optional demographic fields.
6. LLM mapping: implemented through the local backend; requires `NVIDIA_API_KEY`.
7. DOM filling plus `input`/`change`/`blur` events: working for native inputs, textareas, selects, radios, checkboxes, simple contenteditable controls, and ARIA/listbox dropdowns. Dynamic refilling has a re-entry guard and is off by default.
8. ATS-specific handling: Greenhouse, Workday-style controls, Oracle/JET, Taleo/Select2, iCIMS, SmartRecruiters, and SuccessFactors/SAP UI5 dropdowns have expanded option scanning and click-required selection behavior. Platform coverage still varies by company implementation.
9. Review UI before fill: working in the popup preview flow.
10. Backend application tracking: working locally with SQLite; no cloud sync/auth yet.

Resume uploads are detected but remain manual because browsers do not allow extensions to silently set arbitrary local files into file inputs.

## Try The Sample Form

Open:

```text
autofill_extension/examples/sample_application.html
```

Then click the extension and choose **Autofill page**.

Chrome blocks extension content scripts on `file://` pages unless you enable **Allow access to file URLs** for this extension on `chrome://extensions`. You can also serve the repo locally and open the sample over `http://127.0.0.1`.

## Backend Mapper Contract

The extension can later call a backend endpoint from `src/background.js`. The intended request shape is:

```json
{
  "fields": [
    {
      "index": 0,
      "tag": "input",
      "type": "text",
      "label": "First Name",
      "name": "firstName",
      "id": "candidate-first-name",
      "placeholder": "",
      "options": []
    }
  ],
  "profile": {
    "firstName": "Sample",
    "lastName": "Candidate"
  },
  "page": {
    "url": "https://example.greenhouse.io/jobs/123",
    "title": "Software Engineer Intern"
  }
}
```

Expected response:

```json
{
  "mappings": [
    {
      "index": 0,
      "value": "Sample",
      "confidence": 0.98,
      "source": "ai"
    }
  ]
}
```

For now, the local rule mapper is the primary path.
