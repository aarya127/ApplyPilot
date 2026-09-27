# ApplyPilot: Path to Full Autonomy

_Last updated: 2026-09-26_

## Where we are today

ApplyPilot fills most required fields on the forms it can reach, but it is not autonomous and its
answers are not safe to send unseen.

- Nothing clicks **Next** or **Submit**.
- Nothing signs in or creates accounts.
- Nothing records an application unless you click **Track**.
- On the forms it did reach, 9 of 15 got at least one wrong or made-up answer.

### Baseline: test run of 2026-09-26

We drove the real extension through Preview → Fill selected → Ask AI → Fill selected on 33 live
postings across 14 applicant tracking systems (ATSs). Nothing was submitted and no accounts were
created. The raw results are in `docs/e2e_runs/2026-09-26/` (gitignored).

| | Result |
|---|---|
| Postings tested | 33, on 14 ATSs |
| Reached an application form | 15 of 33. The rest stopped at a sign-in/account wall (15) or a bot check (3). |
| Required fields filled, on reachable forms | 128 of 135 (95%) |
| Forms with at least one wrong, made-up, or misplaced answer | **9 of 15** |
| Forms that would have been safe to submit unattended | **1 of 15** (Ramp). Instacart was correct apart from a made-up answer to "How did you hear about us?". |
| Reachable forms with reCAPTCHA or hCaptcha scripts | 11 of 15 |

Coverage is not the problem; correctness is. Details by ATS:

| ATS | Jobs | Reached form | What happened |
|---|---|---|---|
| Greenhouse | 4 (2 embedded) | 4 | 64 of 66 required fields filled. Reddit left the required "I agree" consent empty. Discord got the wrong ethnicity. Three forms got a made-up "LinkedIn" as the answer to "How did you hear about us?". |
| Lever | 3 | 3 | 29 of 34 required fields filled. Errors: a name field set to "I"; "full legal name" set to the LinkedIn URL; the email address typed into an essay box; a garbage LinkedIn URL; "Current location" never filled. |
| Ashby | 3 | 3 | All required fields filled. OpenAI: the LinkedIn URL went into a date picker, and an arbitration agreement was accepted. Cohere: "Name" set to "I". Ramp: clean, but Ask AI ran past 10 minutes. |
| Workable | 1 | 1 | The employer asked for an essay "in your own words"; the tool wrote one with AI and answered "Yes" to "is everything in this application your own". |
| Breezy | 1 | 1 | The phone number was typed into Full Name, Email, Company and Title. |
| Jobvite | 1 | 1 | Contact details filled, résumé not attached. |
| JazzHR | 1 | 1 | Made-up references, a made-up DoD certification, a mixed Canada/US address, and the candidate named as their own referrer. |
| Rippling | 1 | 1 | Everything except the required résumé upload. |
| SmartRecruiters | 3 | 1 of 4 attempts | A "Verification Required: slide right" bot check. The extension filled the check's own feedback form. |
| Workday | 3 | 0 | Step 1 of 6 is Create Account/Sign In. |
| Taleo | 3 | 0 | Sign-in wall. |
| iCIMS | 3 | 0 | Email-first start page with hCaptcha. |
| SuccessFactors | 3 | 0 | Email-first start page on 2 of 3; on Scotiabank the test never got past the job page. On all 3 the tool filled job-alert and cookie widgets instead. |
| Oracle Recruiting Cloud | 3 | 0 | Email plus terms on 2 of 3, where the next step emails a one-time code. On Akamai no fields appeared. |

## What "fully autonomous" means

For a job in the queue, ApplyPilot will, with no human input:

1. open the posting and get to the application form, creating an account or signing in if the ATS
   needs one
2. fill every page with answers that are true and consistent with the profile
3. move through multi-page flows and submit once the form passes the submit gate
4. confirm that the ATS accepted the application
5. record the application, and later its outcome, in one ledger

**Guardrails that stay in place at every phase:**

- **Never fabricate.** An answer the profile or fetched company text can't support goes to the human.
  It is never guessed.
- **Never bypass bot protection.** A visible CAPTCHA or bot challenge pauses the job and notifies
  the human, who solves it in the same browser.
- **Hand-offs are cheap.** A paused job keeps its state. The human answers once, and stable facts are
  saved so the same question never pauses a job twice.
- **Caps and a kill switch.** Daily submission limits per ATS and overall, plus one switch that stops
  all submissions.

## Phases

The phases are ordered so that each one makes the next safe. Automatic submission needs reliable
answers, and it is only useful once every submission is recorded.

### Phase 1: Reliable answers

**Goal:** every form the tool can reach gets correct answers in every required field, the same
answers on every run, and a per-page verdict that says whether the page can be submitted.

Every item below came from the 2026-09-26 test run unless it says otherwise. The items are grouped
by the kind of failure and ordered by how much harm the failure would do once submission is automatic.

**A. Keep the model running**

1. **Handle model retirement.** NVIDIA retired `nvidia/nemotron-3-nano-30b-a3b` on 2026-09-01. Every
   AI answer failed for about 3.5 weeks, and nothing raised an alert. `env.private` now points at
   `nvidia/nemotron-3-super-120b-a12b`, which answers in about 2 s. Still to do:
   - The code defaults are still wrong: `DEFAULT_MODEL` in `autofill_extension/backend/server.py`
     names the retired model, and `application_agent/agent/answer_generator.py` defaults to the omni
     model that used to hang.
   - Add a fallback chain: primary, then secondary, on a 404/410, a 5xx or a timeout.
   - Make `/health` fail loudly on a 410, and show that in the side panel.
   - Consider a paid provider that announces deprecations in advance. The free endpoint also returns
     intermittent 403 and 503 errors.
2. **Put a time limit on Ask AI.** On Spotify (Lever) and Ramp (Ashby), Ask AI was still "Working"
   after 10 minutes. Give the per-field loop an overall deadline and hand off whatever is left.

**B. Never send an answer the profile doesn't support**

3. **The audit pass invents answers.** On G2 Ops (JazzHR) it filled two references ("Globys" and
   "RBC", both "Former Manager", each with the candidate's own email and phone), while tagging its own
   evidence as `insufficientContext`. Rule: in `server.py`, drop every audit `fill` whose evidence is
   `insufficientContext` or missing.
4. **Credentials and experience that don't exist.** The G2 Ops run answered "Yes" to holding a DoD IAT
   Level II certification and "3" years of DoD systems experience. The August Point72 run ticked
   "Certifications: Other". The profile lists no certifications. Answer questions about
   certifications, clearances, licenses and years of experience in a specific area only from
   profile facts. With no fact, answer "No" if the question allows it, otherwise hand off.
5. **The candidate as their own referrer.** "Who referred you to this position?" got "Aarya Shah".
   Referral questions stay blank or "N/A" unless the profile names a referrer.
6. **Demographics must match the profile exactly.** Four separate failures:
   - **Substring bug:** `best_available_option("Asian", options)` returns **"White / Caucasian"**,
     because "Caucasian" contains "asian". On Instacart this came from a fixed `policy` rule, not the
     AI. The scanner had also missed the real "Asian or Asian-American" option.
   - Discord got "Southeast Asian", and Reddit tried "East Asian". The profile says South Asian.
   - Reddit declined sexual orientation even though the profile answers it.
   - The mapper treated a multi-select's "Remove East Asian" chip button as an answer option.

   Fix: match options on whole words, and map demographics deterministically from
   `profile.demographics` with no AI step. If the profile's value isn't among the options, hand off.
   Never fall back to a fuzzy match.
7. **Where the candidate is right now.** Discord's "Are you currently located in the US?" got "Yes",
   and G2 Ops' "Where are you currently located?" got "Chicago". But `profile.location` is Waterloo,
   ON; Chicago is the *preferred* US location. Add an explicit `currentLocation` / `currentCountry`
   fact and use it for "currently located" questions.
8. **Mixed addresses.** G2 Ops got the Canadian street (895 Sombrero Way) and postal code (L5W1T1)
   together with a US city and state (Bartlett, IL). Fill the address as one unit from a single
   country's address record.
9. **Employer rules about AI-written answers.** Hugging Face asked for answers written "yourself, in
   your own words" and included a trap question about a phrase from the job description. The tool
   wrote the essay, answered "YES" to the phrase question even though the essay didn't start with it,
   and answered "YES" to "everything in this application is true and your own". Detect "in your own
   words / write it yourself / do not use AI" and hand those jobs off. Never answer authorship
   attestations automatically. The job description itself isn't in the page context sent to the
   model, so instructions written there are invisible today.
10. **The required-only policy leaks, and "how did you hear" gets made up.**
    - Optional fields got filled: AI text in Point72's "Note to Hiring Manager" and Givzey's cover
      letter, and Discord's optional gender-identity and ethnicity questions.
    - "How did you hear about this job?" got "LinkedIn" on Discord, Instacart and Databricks, and
      "LinkedIn Post" on Shield AI. All four are made up.

    Enforce required-only in the backend too, not only in the panel. Answer "how did you hear" from
    the job's real source.
11. **Check values the ATS pre-filled.** Lever's résumé parser pre-filled LinkedIn as
    `http://linkedin/aarya` and "Other website" as `http://aarya127.com`. The profile has
    `linkedin.com/in/AaryaShah127` and `aarya127.github.io`, and the tool never corrected them.
    The audit has to compare every non-empty field with the profile, not only the empty ones.

**C. Put each answer in the right field**

12. **Name fields rewritten to "I".** Cohere's required "Name" and Palantir's "Preferred Name" ended
    up as "I". The first-person rewrite in `server.py` (around line 2659) treats every text input
    without options as an essay (`textarea or not normalized_options(field)`), so "Aarya" becomes "I".
    Limit it to `is_narrative_question_field`, and never touch name or identity fields.
13. **Container fields.** On Givzey (Breezy) a container labelled "Personal Details Full Name* Email
    Address* Phone Number…" was mapped to the phone number, and the fill typed `647-767-8243` into Full
    Name, Email, Company, Title and Summary. On every Greenhouse form, a phantom field labelled
    "First Name*Last Name*Email*Phone…" stays listed as unresolved, which would block a submit gate
    forever.
    - Reject any field whose label joins the labels of three or more other fields.
    - Make a fill touch exactly one control.
14. **Values in the wrong field.** Three cases:
    - OpenAI (Ashby): the LinkedIn URL went into a required date picker.
    - Shield AI (Lever): "Please state your full legal name" got the LinkedIn URL.
    - Palantir (Lever): the backend-experience essay got the email address.

    This is the known index-drift problem. Check the value's type against the field before typing: a
    URL or email never goes into a name, date or essay field. Re-verify the field's identity right
    before filling.
15. **CAPTCHA widgets get filled.** On SmartRecruiters (Experian, Bosch) the extension scanned the
    reCAPTCHA challenge frame and ticked its "Reason for contacting us" help form. Skip any
    `recaptcha|hcaptcha|turnstile|challenges.cloudflare` frame entirely and report it as a blocker.
16. **Cookie centres and site widgets get filled.** On SuccessFactors job pages and Databricks, the
    tool ticked OneTrust cookie switches, including "Consent to all Advertising Cookies". It also set a
    job-alert frequency and filled search boxes. Extend the junk-field filter to OneTrust
    `ot-group-id-*` switches, cookie toggles and job-alert widgets.
17. **Dropdown options.** Three failures:
    - JazzHR stores options as numbers ("0", "60"), so the required citizenship question ended on
      "No answer". Select and verify dropdowns by the visible option text.
    - Palantir's university question got the "Click Here (If you encounter an issue…)" help entry.
      Exclude help and instruction entries from the options.
    - Instacart's option list was missing the right answer (item 6).
18. **Typeahead fields.** Lever's "Current location" typeahead ("No location found") was never
    filled on any of the 3 Lever jobs; it was required on 2. Type a short query (city only), wait for
    suggestions, then pick one. The August School typeahead bug belongs here too (see below).
19. **Multi-select and consent fills don't stick.** On Reddit, the required "I agree" consent, the
    ethnicity multi-select, gender and sexual orientation all stayed "ready to fill" after two fill
    passes.

**D. Résumé**

20. **Résumé upload is missed on some layouts.** The résumé wasn't attached on Rippling (where
    "Résumé*" is required), on Jobvite ("File" inputs), or on SmartRecruiters. JazzHR reported both
    "Attached" and "File inputs require manual browser confirmation". Detect drop zones ("Drop or
    select", "Choose a file") and generic "File" inputs inside résumé sections.

**E. Know what page you're on**

21. **Wait for the form.** SmartRecruiters draws its form well after page load. ServiceNow scanned 0
    fields at 5 s even though the form finished rendering later. Scan once the visible field count has
    stopped changing (`docs/e2e_batch.py` now does this).
22. **Refuse to fill pages that aren't applications.** The tool filled the email box on iCIMS,
    Oracle and SuccessFactors "enter your email to start" pages, plus job-alert widgets. Classify each
    page first (application form, sign-in wall, email-first start, CAPTCHA, job description) and fill
    only application forms.

**F. A trustworthy verdict per page**

23. **Clean up verification noise.** Workable mismatches compare "question text + answer" with the
    answer ("Expected '…identity? YES' but the page shows 'YES'"). Verification has to be exact before
    a submit gate can rely on it.
24. **Give each page a verdict.**
    - *Submit-ready:* every required field is filled and verified, and no factual field relies on an
      unconfirmed AI guess.
    - *Needs human:* the list of questions to hand off.
    - *Blocked:* the reason, such as a CAPTCHA or sign-in wall.

    Phase 3's submit gate consumes this verdict.

**Better than in August:** with the new model, School was correctly "University of Waterloo" on
Discord, where the August Point72 run picked "Aarhus University" from the first page of an
alphabetical dropdown. The code path is unchanged, so keep a school typeahead in the regression set.

**Exit criteria (on a fixed set of about 20 reachable forms, 3 runs in a row):**

- 0 answers that the profile, the résumé or fetched company text doesn't support
- 0 values in the wrong field, and 0 fills inside CAPTCHA, cookie, search or job-alert widgets
- 0 demographic, address or location answers that disagree with the profile
- at least 95% of required fields filled and verified; every other required field surfaced as a
  hand-off
- identical answers across the 3 runs for every factual field

### Phase 2: Automatic tracking

**Goal:** one ledger that records every application automatically, from the first fill through
submission to the employer's reply.

Today applications are recorded in three places that don't talk to each other:

- the extension backend's `applications.sqlite3` (written only when you click **Track**)
- the dashboard queue (`application_agent/agent/apply_queue.py`)
- the Outlook tracker in `app.py`, which classifies emails but doesn't link them to applications

Work items:

1. **One ledger.** Merge the backend's `applications` table and the dashboard's apply queue into one
   store. Each row holds the job URL, a normalized ATS job ID, company, title, ATS, and status history
   (`queued → filling → filled → submitted → confirmed → interview / rejected`).
2. **Record without a button.** Write a row on first fill and update it on every step. Remove the
   Track button.
3. **Snapshot what was sent.** Store the final value of every field, the résumé version, and
   screenshots of the filled form and the confirmation page. This is the audit trail if a bad answer
   ever gets out.
4. **Duplicate guard.** Refuse to start a job whose ATS job ID is already in the ledger, and warn on
   same company plus similar title within 30 days.
5. **Link emails to applications.** Match confirmation, interview and rejection emails to ledger rows
   by company and ATS sender domain. Replace the pasted Graph Explorer token, which expires after about
   an hour, with an MSAL login that has a refresh token. Phase 4 needs the same login to read
   verification codes.

**Exit criteria:** every run of `docs/e2e_batch.py` produces ledger rows without a click, and at
least 90% of confirmation emails from a week of real applications match their row automatically.

### Phase 3: Submit step

**Goal:** submit an application when the page is provably complete, then confirm the ATS accepted it.

Work items:

1. **Submit gate.** Build on the Phase 1 verdict. Submit only when every required field is filled
   and verified on the page, there are no verification mismatches, and no factual field relies on an
   unconfirmed AI guess.
2. **Multi-page navigation.** Click **Next / Continue / Save and continue** after each page passes
   the gate, then scan and fill again. Workday, SmartRecruiters, iCIMS and SuccessFactors are all
   multi-page. Today only `application_agent/agent/runner.py` has a page loop, and it uses the
   weaker answer engine (see Phase 5).
3. **Detect the outcome after submit.** Tell apart a confirmation page (per-ATS URL and text
   patterns), validation errors (fix and retry, at most twice), a CAPTCHA challenge (pause and
   notify) and an unknown result (pause and notify).
4. **Supervised rollout.**
   - Mode A: the tool fills everything and sends "ready to submit"; one click approves.
   - Mode B: auto-submit for ATSs whose last 20 supervised submissions needed no corrections.
5. **Bot protection.** 11 of the 15 reachable forms load reCAPTCHA or hCaptcha. Greenhouse and
   Ashby use invisible reCAPTCHA Enterprise, which scores the browser. Submit from the user's real
   Chrome profile with the real extension, not from a fresh automated Chromium, so the score reflects
   a normal browser. Never solve challenges programmatically.

**Exit criteria:** 20 supervised submissions per supported ATS with zero corrections, and
confirmation detection correct on 100% of them.

### Phase 4: Accounts and logins

**Goal:** get past the account wall that enterprise ATSs put in front of the form.

What the test run hit, before reaching any application question:

| ATS | Jobs | What stands in front of the form |
|---|---|---|
| Workday | 3/3 | Step 1 of 6 is "Create Account/Sign In", with sign-in by Google, LinkedIn or email. Accounts are per employer, because each company is a separate Workday tenant. |
| Taleo | 3/3 | Sign-in or create-account page |
| iCIMS | 3/3 | An email-first start page ("enter your email" plus a privacy checkbox) with **hCaptcha** |
| SuccessFactors | 2/3 | "Enter email to start application process", which leads to account creation. On the third (Scotiabank) the test never got past the job page. |
| Oracle Recruiting Cloud | 2/3 | Email plus terms checkbox. The next step emails a one-time code. On the third (Akamai) no fields appeared. |
| SmartRecruiters | 3/3 | No account needed, but a "Verification Required: slide right" bot check blocked 3 of 4 attempts from the automated browser |

Work items:

1. **Credential vault.** Store one generated password per ATS tenant in the macOS Keychain, keyed by
   the tenant host (for example `nvidia.wd5.myworkdayjobs.com`). Never reuse a password across tenants.
2. **Email access.** Read verification links and one-time codes from the inbox. This uses the MSAL
   login from Phase 2. Only read messages from the ATS sender that arrived after the request.
3. **Flows, in order of volume:**
   - Workday: create an account, verify the email, sign in, then use "Autofill with Resume" once
     signed in.
   - iCIMS: email-first start, then log in or create an account.
   - SuccessFactors: email-first start, then create an account.
   - Oracle: email plus terms, then the one-time code.
   - Taleo: log in or create an account.
4. **Consent policy.** Creating an account means accepting terms and privacy policies. The OpenAI
   (Ashby) run also ticked an **arbitration agreement**, and Oracle ticked "I agree with the terms
   and conditions". Decide which consents the tool may give without asking. Everything else becomes a
   hand-off.
5. **Bot checks at sign-in** (iCIMS hCaptcha, the SmartRecruiters slider) hand off to the human.
   Using the user's real Chrome profile instead of a fresh automated browser should cut how often they
   appear.
6. **Resume a paused application.** After sign-in, detect "continue your application" and resume at
   the right step instead of starting over.

**Exit criteria:** on 3 Workday tenants and one tenant each of iCIMS, Taleo, SuccessFactors and
Oracle, the tool reaches the first application-question page with nobody touching it, apart from
CAPTCHA hand-offs.

### Phase 5: Automate the workflow

**Goal:** a queue that runs by itself, where the human only answers hand-offs.

Work items:

1. **One engine.** `application_agent` has the page loop and queue but uses its own
   `answer_generator.py`. That module doesn't use the backend's policy answers, audit pass, focused
   retries or grounding, and its default model has been retired. Retire it and drive the real
   extension from Playwright instead. `docs/e2e_batch.py` already does this and is the starting point
   for the orchestrator.
2. **State machine per job.**
   `open → entry → login → fill page → gate → next or submit → confirm → record`. Each state
   records where it stopped and why, so a paused job resumes at the same step.
3. **Job intake.** The live-site scrapers are no longer used. The public ATS APIs used to build the
   test set (Greenhouse, Lever, Ashby, SmartRecruiters and Workday job listings) give structured,
   stable listings. Filter them with the existing `application_agent/agent/preferences.py`, then
   check the Phase 2 duplicate guard.
4. **Hand-off channel.** Push a notification for each paused job ("CAPTCHA on X", "need your answer
   to Y"), plus a daily summary.
5. **Operations.**
   - Run the backend as a `launchd` service.
   - Add a health check that alerts when the model returns 404/410 or times out.
   - Reload the extension and refresh open job tabs automatically after code changes.
   - Enforce the daily caps and kill switch from the guardrails.

**Exit criteria:** runs unattended for a week, and the human touches only paused hand-offs, each in
under 2 minutes.

## Decisions needed from the owner

These block parts of Phase 1 and Phase 4, and only the owner can make them:

1. **Consents.** Which may the tool give without asking: privacy policy, terms of use, SMS or
   marketing opt-ins, arbitration agreements?
2. **Optional demographic questions.** Answer them from the profile, decline, or skip them? Today
   they are sometimes answered and sometimes declined.
3. **Current location.** Which is true right now: Waterloo, ON or Chicago, IL? The answer decides the
   "are you currently located in the US?" questions.
4. **Employers who ask for answers written without AI.** Skip those jobs, or hand the essays to you?
5. **"How did you hear about us?"** What should the default answer be?
6. **Model provider.** Stay on the free NVIDIA endpoint, which retired our model without warning and
   returns intermittent 403/503 errors, or pay for a provider with deprecation notices?

## How we measure progress

`docs/e2e_batch.py` is the regression suite for every phase. It drives the real extension through
Preview → Fill → Ask AI on a list of live postings and never clicks Submit or Next. It writes, per
job:

- the landing outcome (form, login wall, no fields seen, closed)
- CAPTCHA and bot-check signals
- every field value as read directly from the page DOM, independent of the extension's own scanner
- verification mismatches and unresolved required fields

```bash
.venv/bin/python autofill_extension/backend/server.py &            # backend on :8000
.venv/bin/python docs/e2e_batch.py docs/e2e_runs/<date>/jobs.json \
    --out docs/e2e_runs/<date> [--boards greenhouse,lever] [--resume]
```

Run output goes to `docs/e2e_runs/`, which is gitignored because screenshots contain profile data.
Postings close within weeks, so refresh `jobs.json` before each run. The DOM-read `required` flag
misses fields that ATSs mark as required only through CSS (Ashby does this), so treat the
required-field counts as a lower bound.
