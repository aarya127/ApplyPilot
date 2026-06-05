# Application Completion Agent

This is a separate Playwright-based post-apply agent. It does not search for jobs. You open/click Apply, then the agent attaches to the current browser page and completes the application flow as far as it can safely go.

## Safety Rules

- It can fill fields, upload the ignored local resume file, and click safe Next/Continue buttons.
- It stops before final Submit/Apply/Complete buttons.
- It logs progress locally in ignored SQLite storage.
- It reads your existing ignored `autofill_extension/profile.private.json` profile.
- It does not bypass CAPTCHA, invent experience, or submit without review.

## Run

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

