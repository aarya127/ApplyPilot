# Application Completion Agent

This is a separate Playwright-based post-apply agent. It does not search for jobs. You open/click Apply, then the agent attaches to the current browser page and completes the application flow as far as it can safely go.

The agent is meant to be reusable by any candidate. It reads the same private profile used by the Chrome extension:

```text
autofill_extension/profile.private.json
```

Create that file by copying the tracked template:

```bash
cp autofill_extension/profile.example.json autofill_extension/profile.private.json
```

Then replace the sample values with your own contact info, addresses, work authorization, work history, education, links, resume filename, saved answers, and optional self-identification answers.

## Safety Rules

- It can fill fields, upload the ignored local resume file, and click safe Next/Continue buttons.
- It stops before final Submit/Apply/Complete buttons.
- It logs progress locally in ignored SQLite storage.
- It reads your existing ignored `autofill_extension/profile.private.json` profile.
- It does not bypass CAPTCHA, invent experience, or submit without review.

## Personal Files

Keep user-specific data in ignored paths:

```text
autofill_extension/profile.private.json
autofill_extension/resumes/
autofill_extension/generated/
autofill_extension/backend/env.private
application_agent/browser_profile/
application_agent/data/
application_agent/files/
application_agent/*.private.json
```

Put your resume in:

```text
autofill_extension/resumes/
```

Then set `candidateProfile.resumeFileName` in `profile.private.json` to the exact filename.

## AI Backend

The agent can use the same local NVIDIA-backed mapper as the Chrome extension.

Create:

```text
autofill_extension/backend/env.private
```

Example:

```text
NVIDIA_API_KEY=your-key-here
NVIDIA_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
PORT=8000
```

Start the backend in one terminal:

```bash
python autofill_extension/backend/server.py
```

The backend is used for ambiguous fields and custom questions. It receives a structured list of visible fields and options, then returns mappings. Option fields are constrained to the actual dropdown/radio/checkbox labels whenever those labels are available.

## Run The Agent

In another terminal:

```bash
python -m application_agent.main
```

The first run creates an ignored persistent browser profile:

```text
application_agent/browser_profile/
```

Application logs are stored in:

```text
application_agent/data/applications.sqlite3
```

Workflow:

1. Start the backend if you want AI assistance.
2. Run `python -m application_agent.main`.
3. In the opened browser, log into any ATS account if needed.
4. Open your job board or application link.
5. Click Apply manually.
6. Return to the terminal and press Enter when prompted.
7. The agent detects the ATS, scans the page, fills known fields, uploads your resume when possible, and handles safe Next/Continue buttons.
8. Review everything before submitting. The agent should stop before final Submit.

## Current Adapters

- Greenhouse: direct known selectors plus generic scanner fallback.
- Lever: label-based known fields plus generic scanner fallback.
- Ashby: generic scanner fallback.
- Workday: generic scanner fallback only. Workday-specific custom widgets still need deeper adapter work.
- Generic: scans visible inputs, textareas, selects, contenteditable controls, and basic ARIA text/combobox controls.

## Private Standard Answers

Optional reusable custom answers can live in ignored JSON:

```text
application_agent/standard_answers.private.json
```

Example:

```json
{
  "Do you have experience with Kubernetes?": "Yes, I have used Kubernetes for deploying and operating containerized services."
}
```

Use this for answers you want reused exactly. For questions that vary by role or company, leave them out so the AI mapper can draft or choose an answer from the current field context.

## How It Shares The Extension Profile

`application_agent.agent.profile_loader` normalizes the extension profile into the snake_case shape used by the Playwright agent. These fields are especially important:

- `firstName`, `lastName`, `fullName`, `email`, `phone`
- `linkedin`, `github`, `portfolio`, `links`
- `addresses.canada`, `addresses.usa`
- `school`, `degree`, `fieldOfStudy`, `graduationDate`
- `workExperience`
- `resumeFileName`
- `workAuthorization`, `needsSponsorship`, `veteranStatus`, `subjectToAgreement`
- `answers`
- `demographics`
- `settings.targetCountry`

If the profile is missing a value, the agent either skips the field, asks AI when enabled, or pauses for review depending on the field type and adapter.
