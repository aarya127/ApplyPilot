# Application Autofill Prototype

This is a standalone Chrome extension prototype for job application autofill. It is separate from the Flask HR dashboard.

## What It Does

- Stores a candidate profile in Chrome local storage.
- Scans visible `input`, `textarea`, `select`, radio, checkbox, and simple ARIA form controls.
- Extracts field metadata such as label, placeholder, name, id, options, and nearby text.
- Maps common application fields to profile values using local rules.
- Fills fields and dispatches `input`, `change`, and `blur` events so React-style forms usually notice the changes.
- Watches for dynamically added form fields with `MutationObserver`.
- Includes an optional backend mapper URL setting for a future AI field-mapping service.

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
