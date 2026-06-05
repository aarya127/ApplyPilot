# Application Autofill Prototype

This is a standalone Chrome extension prototype for job application autofill. It is separate from the Flask HR dashboard.

## What It Does

- Stores a candidate profile in Chrome local storage.
- Scans visible `input`, `textarea`, `select`, radio, checkbox, and simple ARIA form controls.
- Extracts field metadata such as label, placeholder, name, id, options, and nearby text.
- Maps common application fields to profile values using local rules.
- Previews mapped fields in the popup so you can review before filling.
- Shows unknown questions in the popup, lets you answer them once, and saves those answers for future applications.
- Detects resume upload fields and highlights them as manual tasks.
- Fills fields and dispatches `input`, `change`, and `blur` events so React-style forms usually notice the changes.
- Handles native dropdowns and basic ARIA/listbox-style custom dropdowns.
- Watches for dynamically added form fields with `MutationObserver`.
- Includes a local backend for NVIDIA-hosted LLM field mapping and SQLite application tracking.

Dynamic refilling is off by default because some React/Greenhouse pages rerender after each input event. Turn it on only when a page loads additional steps after the first fill.

## Load It In Chrome

1. Open `chrome://extensions`.
2. Turn on Developer Mode.
3. Click **Load unpacked**.
4. Select the `autofill_extension` folder.
5. Open the extension options page and save your profile.
6. Visit a job application page and click **Autofill page** from the extension popup.

## Private Profile Import

Personal addresses, work eligibility, and voluntary demographic fields should live outside committed source files. This repo ignores:

```text
autofill_extension/profile.private.json
autofill_extension/*.private.json
autofill_extension/resumes/
autofill_extension/generated/
```

To load a private profile:

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

## Current Build Status

1. Manifest V3 extension: working.
2. Content script form scanner: working for ordinary page DOM fields.
3. Label/name/id/placeholder/options extraction: working for native controls and simple ARIA controls.
4. Local profile storage: working via `chrome.storage.local`; backend profile sync is not built.
5. Rule matching: working for common contact, address, links, work authorization, sponsorship, relocation, salary, agreement, and optional demographic fields.
6. LLM mapping: implemented through the local backend; requires `NVIDIA_API_KEY`.
7. DOM filling plus `input`/`change`/`blur` events: working for native inputs, textareas, selects, radios, checkboxes, simple contenteditable controls, and basic ARIA/listbox dropdowns. Dynamic refilling has a re-entry guard and is off by default.
8. ATS-specific adapters: generic Greenhouse-style form handling is partially covered; deeper Workday/Lever/Ashby adapters are not built yet.
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
