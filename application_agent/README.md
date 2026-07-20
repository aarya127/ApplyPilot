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

## Install Playwright Browser

The agent and queued runner use Playwright. Installing the Python package is not
enough; each machine also needs the Chromium browser binary:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

If you see:

```text
BrowserType.launch: Executable doesn't exist at .../ms-playwright/.../headless_shell
Looks like Playwright was just installed or updated.
Please run: playwright install
```

run this from the same activated virtual environment you use to start
ApplyPilot:

```bash
python -m playwright install chromium
```

Prefer `python -m playwright ...` over bare `playwright ...` so the browser is
installed for the same Python interpreter used by `python app.py` and
`python -m application_agent.main`.

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

The agent itself also calls the NVIDIA API directly for generated answers. It picks up the key from either place:

- an exported `NVIDIA_API_KEY` environment variable, or
- `autofill_extension/backend/env.private`, which the agent loads automatically when `NVIDIA_API_KEY` is not already set in the environment.

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

## Run Queued Shortlisted Jobs

The Flask dashboard can now store shortlisted jobs in:

```text
application_agent/data/applications.sqlite3
```

Use `/newgrad` to shortlist jobs, open `/shortlist`, and mark selected rows as
`queued`. Then run:

```bash
python -m application_agent.queued_apply --limit 3
```

The queued runner:

1. loads queued jobs from local SQLite storage
2. opens each job URL in the persistent Playwright browser
3. clicks a visible Apply/Apply Now/Start Application control when available
4. runs the ATS completion agent
5. writes a structured report back to `application_reports`
6. marks the job as paused, failed, or submitted

Current default behavior is still review-first. The runner does not final-submit
applications without a future confidence-gated setting.

## Preferences And Blacklists

Copy the tracked example:

```bash
cp application_agent/preferences.example.json application_agent/preferences.private.json
```

The private preferences file borrows the useful configuration ideas from
bulk-apply systems:

- target positions
- preferred locations
- remote/hybrid/onsite switches
- company blacklist
- title blacklist
- apply-once-per-company
- future automation confidence gates

The long-run target is:

```text
shortlist jobs in dashboard
  -> queue selected jobs
  -> run queued ApplyPilot agent
  -> fill each ATS flow
  -> verify and audit every filled answer
  -> send/store a report
  -> submit only when confidence gates pass
```

## Internal Workflow

ApplyPilot now uses a plan-first pipeline:

```text
scan fields
  -> classify field kinds
  -> discover options
  -> resolve profile answers
  -> ask AI only for unresolved fields
  -> build fill plan
  -> fill the DOM
  -> verify filled values
  -> report skipped/mismatched fields
```

The key improvement is the canonical field-kind layer. The agent first decides
what a field is, such as `links.linkedin`, `education.school`, or
`work.current_or_previous_employer`, before choosing an answer. This prevents
nearby labels like "Website" from leaking into unrelated fields like employer
or university.

Process documentation lives in:

```text
docs/applypilot_skills/
```

Start with `docs/applypilot_skills/README.md`, then read the skill file for the
subprocess you are debugging.

## Current Adapters

- Greenhouse: direct known selectors plus generic scanner fallback.
- Lever: label-based known fields plus generic scanner fallback.
- Ashby: generic scanner fallback.
- Workday: generic scanner fallback only. Workday-specific custom widgets still need deeper adapter work.
- Oracle Cloud / Oracle Recruiting / Oracle HCM: first-class detection, label-first common-field filling, expanded Oracle JET dropdown option scanning, and generic scanner fallback.
- Taleo: first-class detection, label-first common-field filling, Select2/jQuery-style dropdown option scanning, and generic scanner fallback.
- iCIMS: first-class detection, label-first common-field filling, Select2/iCIMS dropdown option scanning, and generic scanner fallback.
- SmartRecruiters: first-class detection, label-first common-field filling, custom dropdown option click handling, and generic scanner fallback.
- SuccessFactors: first-class detection, label-first common-field filling, SAP UI5 dropdown option scanning, and generic scanner fallback.
- Generic: scans visible inputs, textareas, selects, contenteditable controls, and basic ARIA text/combobox controls.

For Greenhouse, Oracle/Taleo, iCIMS, SmartRecruiters, and SuccessFactors, option-style fields are treated as click-required controls. The agent should choose an actual visible option instead of relying on typed text that the ATS may erase.

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
