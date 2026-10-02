# ApplyPilot: Path to Full Autonomy

_Last updated: 2026-09-26_

## Where we are today

ApplyPilot fills most required fields on the forms it can reach, but it is not autonomous and its
answers are not safe to send unseen.

- Nothing clicks **Next** or **Submit**.
- Nothing signs in or creates accounts.
- Nothing records an application unless you click **Track**.
- On the forms it did reach, 10 of 15 got at least one wrong or made-up answer.

### Baseline: test run of 2026-09-26

We drove the real extension through Preview → Fill selected → Ask AI → Fill selected on 33 live
postings across 14 applicant tracking systems (ATSs). Nothing was submitted and no accounts were
created. The raw results are in `docs/e2e_runs/2026-09-26/` (gitignored). The counts below apply
the owner's decisions recorded at the end of this document: current location is Chicago, "How did
you hear about us?" is LinkedIn, and employer requests to write without AI are ignored.

| | Result |
|---|---|
| Postings tested | 33, on 14 ATSs |
| Reached an application form | 15 of 33. The rest stopped at a sign-in/account wall (15) or a bot check (3). |
| Required fields filled, on reachable forms | 128 of 135 (95%) |
| Forms with at least one wrong, made-up, or misplaced answer | **10 of 15** |
| Forms that would have been safe to submit unattended | **2 of 15** (Ramp, Databricks). Both still had tool problems outside the answers: Ask AI ran past 10 minutes on Ramp, and cookie switches were ticked on Databricks. |
| Reachable forms with reCAPTCHA or hCaptcha scripts | 11 of 15 |

Coverage is not the problem; correctness is. Details by ATS:

| ATS | Jobs | Reached form | What happened |
|---|---|---|---|
| Greenhouse | 4 (2 embedded) | 4 | 64 of 66 required fields filled. Reddit left the required "I agree" consent empty. Discord got the wrong ethnicity. Instacart (a Canada job) said the candidate lives in Mississauga, Ontario. |
| Lever | 3 | 3 | 29 of 34 required fields filled. Errors: a name field set to "I"; "full legal name" set to the LinkedIn URL; the email address typed into an essay box; a garbage LinkedIn URL; "Current location" never filled. |
| Ashby | 3 | 3 | All required fields filled. OpenAI: the LinkedIn URL went into a date picker, and an arbitration agreement was accepted. Cohere: "Name" set to "I", and location set to Mississauga. Ramp: clean, but Ask AI ran past 10 minutes. |
| Workable | 1 | 1 | The job description asked for the first answer to start with a specific phrase. The tool missed the instruction, then answered "YES" to "Did you start with the exact phrase?". |
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
   AI answer failed for about 3.5 weeks, and nothing raised an alert. **Done 2026-09-27:**
   `env.private` and `DEFAULT_MODEL` now use `nemotron-3-ultra-550b` for structured answers and
   `glm-5.3` for essays (see Model choice). Still to do:
   - `application_agent/agent/answer_generator.py` still defaults to the omni model that used to
     hang.
   - Add a fallback chain: primary, then secondary, on a 404/410, a 5xx or a timeout. **Done
     2026-10-02:**
     - Every model call goes through `post_chat_completion`.
     - It moves down the chain on timeouts, connection errors, HTTP 403/404/408/409/410/429/5xx
       and empty replies, within a 100 s limit.
     - A 401 (bad key) stops at once.
     - Form answers and audits: ultra → glm-5.3 → kimi-k3 → super-120b. Essays: glm-5.3 →
       super-120b, never ultra.
     - Override with `NVIDIA_FALLBACK_MODELS` / `NVIDIA_NARRATIVE_FALLBACK_MODELS` (empty
       disables). Each switch is logged as `model.fallback`, and `/health` lists the chains.
     - Verified live: a retired model (410) fell through to glm-5.3.
   - Make `/health` fail loudly on a 410, and show that in the side panel.
   - The owner chose to stay on the free endpoint, which retires models without notice and returns
     intermittent 403 and 503 errors. On 2026-10-01 a live run lost 8 of 22 model calls to 503s and 2
     to timeouts. Reddit's veteran question became a hand-off because its call failed. A later run
     got 1 of 3 mapper calls and no audit through. These outages are now the largest single cause
     of hand-offs, so the fallback chain is the next fix to make. The fallback chain and alert are therefore required, not
     optional. The model ranking in Model choice gives the order for the chain.
2. **Put a time limit on Ask AI.** On Spotify (Lever) and Ramp (Ashby), Ask AI was still "Working"
   after 10 minutes. Give the per-field loop an overall deadline and hand off whatever is left.
3. **Shrink the prompts.** Mapper prompts run from 24k to 548k characters. Palantir's is about 137k
   tokens, and only 3 of the 10 working models returned answers for it. Send each field's retrieved
   context instead of the whole profile and page for every field, and split large forms into
   batches.

**B. Never send an answer the profile doesn't support**

4. **The audit pass invents answers.** On G2 Ops (JazzHR) it filled two references ("Globys" and
   "RBC", both "Former Manager", each with the candidate's own email and phone), while tagging its own
   evidence as `insufficientContext`. Rule: in `server.py`, drop every audit `fill` whose evidence is
   `insufficientContext` or missing.
   **Fixed 2026-10-01:**
   - `/audit-fields` drops any model fill or correction with no evidence, along with the model's
     matching entry in `corrections`.
   - Professional-reference fields (reference names, relationship, how long known, and the
     reference's phone or email) never reach the model, the audit or the extension's contact
     rules. They always go to the human unless there is a saved answer.
   - A generic "Phone Number" or "Email" counts as a reference field when it sits within three
     fields of a reference field. Its nearby text is often just the next label or the job
     description.

   Live result on G2 Ops: all 10 reference fields were left for the human, and the candidate's own
   email and phone at the top of the form were still filled.
5. **Credentials and experience that don't exist.** The G2 Ops run answered "Yes" to holding a DoD IAT
   Level II certification and "3" years of DoD systems experience. The August Point72 run ticked
   "Certifications: Other". The profile lists no certifications. Answer questions about
   certifications, clearances, licenses and years of experience in a specific area only from
   profile facts. With no fact, answer "No" if the question allows it, otherwise hand off.
   **Fixed 2026-10-01, in both the backend and the extension:**
   - Certification and license questions get "No" when the profile lists none. Driving licences
     and education credentials are excluded.
   - The extension's attestation rule no longer treats the noun "certification" as "I certify this
     is true"; that rule had answered the IAT question "Yes".
   - Security-clearance eligibility is "No" for a US permanent resident, since clearances require
     citizenship, and "do you hold a clearance" is "None".
   - Defense-specific years-of-experience questions get "0" when the profile has no defense work.
     The fixed years rule had answered "3" (total experience).

   **Still open:** other domain-specific years questions still get total experience.
6. **The candidate as their own referrer.** "Who referred you to this position?" got "Aarya Shah".
   Referral questions stay blank or "N/A" unless the profile names a referrer. **Fixed
   2026-10-01:** the extension's name rule had matched "Enter their first and last name". Referrer
   questions now get `answers.referrer` if it is set, otherwise "N/A" for free text and "No" for
   yes/no questions, on both sides.
7. **Demographics must match the profile exactly.** Four separate failures:
   - **Substring bug:** `best_available_option("Asian", options)` returns **"White / Caucasian"**,
     because "Caucasian" contains "asian". On Instacart this came from a fixed `policy` rule, not the
     AI. The scanner had also missed the real "Asian or Asian-American" option.
   - Discord got "Southeast Asian", and Reddit tried "East Asian". The profile says South Asian.
   - Reddit declined sexual orientation even though the profile answers it.
   - The mapper treated a multi-select's "Remove East Asian" chip button as an answer option.

   Fix: match options on whole words, and map demographics deterministically from
   `profile.demographics` with no AI step. If the profile's value isn't among the options, hand off.
   Never fall back to a fuzzy match.
   **Fixed 2026-10-01, in both the backend and the extension:**
   - Partial matches must be whole words and the only match.
   - Exact matches always win.
   - Ethnicity is tried before race.
   - A dropdown or option list never receives a raw profile value that isn't one of its options.
   - Checkbox groups tick one best option per answer. "Asian" had ticked East *and* Southeast
     Asian.

   Live result on Reddit: gender Male, orientation Heterosexual, ethnicity South Asian. The day
   before it was "I don't wish to answer" and East Asian.
   **Still open:** "Remove South Asian"-style chip buttons are still scanned as fields and show up
   as phantom verification mismatches.
8. **Where the candidate lives right now.** The owner confirmed it is Chicago. On the two Canada jobs,
   the tool used the Canadian address instead: Instacart got "(CAN) Ontario" for "Which state or
   province do you currently live in?" and Mississauga as the city, and Cohere got Mississauga. The
   profile's own `location` field still says Waterloo, ON.
   - Add `currentLocation: "Chicago, IL"` and `currentCountry: "United States"`.
   - Use them for every "currently located / live / based" question, whatever the job's country.
   - Keep the Canadian address only for questions that ask for a Canadian mailing address.

   **Fixed 2026-10-01:**
   - The profile now has `currentLocation` / `currentCity` / `currentState` / `currentCountry`, and
     `location` is Chicago.
   - The mapper prompt carries `currentLocation`, with a rule to use it whatever the job's country.
   - In the extension, the address, location and Workday country fills use the current country's
     address.

   Live result: Cohere's location went from Mississauga to "Chicago, Illinois, United States". The
   owner must re-import `profile.private.json` (Options → Import JSON) for the extension to see the
   new keys.
9. **Mixed addresses.** G2 Ops got the Canadian street (895 Sombrero Way) and postal code (L5W1T1)
   together with a US city and state (Bartlett, IL). Fill the address as one unit from a single
   country's address record.
10. **Questions no profile can answer.** "Tell us something about yourself that we wouldn't find on
    your résumé" made every tested model invent a hobby or credential (a yoga instructor, restoring
    vintage bicycles, hiking). Hand these off once and reuse the saved answer.
11. **Follow instructions written in the job description.** The owner decided to ignore employer
    requests to write without AI, so the tool keeps writing essays and answering authorship
    attestations. One factual problem remains: Hugging Face's job description asked for the first
    answer to start with a specific phrase. The tool didn't, then answered "YES" to "Did you start
    with the exact phrase we asked for?". The job description isn't in the page context sent to the
    model, so its instructions are invisible today. Fetch the description with the page, follow its
    format instructions, and answer checks about them truthfully.
12. **The required-only policy leaks, and "how did you hear" isn't consistent.**
    - Optional fields got filled: AI text in Point72's "Note to Hiring Manager" and Givzey's cover
      letter, and Discord's optional gender-identity and ethnicity questions.
    - The owner's answer to "How did you hear about this job?" is **LinkedIn**. Today it comes from
      the AI, and in the model test one model answered "Greenhouse job board". Make it a
      deterministic rule: pick the option closest to "LinkedIn" ("LinkedIn", "LinkedIn Post",
      "LinkedIn Sponsored Job/Ad"), or type "LinkedIn" into free-text fields.

    Enforce required-only in the backend too, not only in the panel.
13. **Check values the ATS pre-filled.** Lever's résumé parser pre-filled LinkedIn as
    `http://linkedin/aarya` and "Other website" as `http://aarya127.com`. The profile has
    `linkedin.com/in/AaryaShah127` and `aarya127.github.io`, and the tool never corrected them.
    The audit has to compare every non-empty field with the profile, not only the empty ones.

**C. Put each answer in the right field**

14. **Name fields rewritten to "I".** Cohere's required "Name" and Palantir's "Preferred Name" ended
    up as "I". The first-person rewrite in `server.py` treated every text input without options as
    an essay, so "Aarya" became "I". **Fixed 2026-09-27:** the rewrite now uses
    `is_narrative_question_field`, and a regression test covers Name and Preferred Name.
15. **Container fields.** On Givzey (Breezy) a container labelled "Personal Details Full Name* Email
    Address* Phone Number…" was mapped to the phone number, and the fill typed `647-767-8243` into Full
    Name, Email, Company, Title and Summary. On every Greenhouse form, a phantom field labelled
    "First Name*Last Name*Email*Phone…" stays listed as unresolved, which would block a submit gate
    forever.
    - Reject any field whose label joins the labels of three or more other fields.
    - Make a fill touch exactly one control.
16. **Values in the wrong field.** Three cases:
    - OpenAI (Ashby): the LinkedIn URL went into a required date picker.
    - Shield AI (Lever): "Please state your full legal name" got the LinkedIn URL.
    - Palantir (Lever): the backend-experience essay got the email address.

    This is the known index-drift problem. Check the value's type against the field before typing: a
    URL or email never goes into a name, date or essay field. Re-verify the field's identity right
    before filling.
    A fourth case came from a rule, not drift: G2 Ops' "What **program**ming languages are you
    most familiar with?" got "Statistics" because the field-of-study and degree rules matched
    "program" inside "programming". **Fixed 2026-10-01:** they now match "program" only as a
    whole word.
17. **CAPTCHA widgets get filled.** On SmartRecruiters (Experian, Bosch) the extension scanned the
    reCAPTCHA challenge frame and ticked its "Reason for contacting us" help form. Skip any
    `recaptcha|hcaptcha|turnstile|challenges.cloudflare` frame entirely and report it as a blocker.
18. **Cookie centres and site widgets get filled.** On SuccessFactors job pages and Databricks, the
    tool ticked OneTrust cookie switches, including "Consent to all Advertising Cookies". It also set a
    job-alert frequency and filled search boxes. Extend the junk-field filter to OneTrust
    `ot-group-id-*` switches, cookie toggles and job-alert widgets.
19. **Dropdown options.** Three failures:
    - JazzHR stores options as numbers ("0", "60"), so the required citizenship question ended on
      "No answer". Select and verify dropdowns by the visible option text.
    - Palantir's university question got the "Click Here (If you encounter an issue…)" help entry.
      Exclude help and instruction entries from the options.
    - **The selected answer is missing from the option list.** On filled react-select dropdowns
      the scanner drops the chosen option: Reddit's audit got gender, orientation and ethnicity
      lists without "Male", "Heterosexual" or "South Asian", and Instacart's race list lacked
      "Asian or Asian-American". The audit then can't confirm a correct answer and may "correct"
      it to a wrong one. Include the selected option in every option list.
      **Fixed 2026-10-01:**
      - The extension reads react-select multi-select chips. Before, those fields read as blank.
      - Discovered options always include the currently selected value(s).
20. **Typeahead fields.** Lever's "Current location" typeahead ("No location found") was never
    filled on any of the 3 Lever jobs; it was required on 2. Type a short query (city only), wait for
    suggestions, then pick one. The August School typeahead bug belongs here too (see below).
21. **Multi-select and consent fills don't stick.** On Reddit, the required "I agree" consent, the
    ethnicity multi-select, gender and sexual orientation all stayed "ready to fill" after two fill
    passes.

**D. Résumé**

22. **Résumé upload is missed on some layouts.** The résumé wasn't attached on Rippling (where
    "Résumé*" is required), on Jobvite ("File" inputs), or on SmartRecruiters. JazzHR reported both
    "Attached" and "File inputs require manual browser confirmation". Detect drop zones ("Drop or
    select", "Choose a file") and generic "File" inputs inside résumé sections.

**E. Know what page you're on**

23. **Wait for the form.** SmartRecruiters draws its form well after page load. ServiceNow scanned 0
    fields at 5 s even though the form finished rendering later. Scan once the visible field count has
    stopped changing (`docs/e2e_batch.py` now does this).
24. **Refuse to fill pages that aren't applications.** The tool filled the email box on iCIMS,
    Oracle and SuccessFactors "enter your email to start" pages, plus job-alert widgets. Classify each
    page first (application form, sign-in wall, email-first start, CAPTCHA, job description) and fill
    only application forms.

**F. A trustworthy verdict per page**

25. **Clean up verification noise.** Workable mismatches compare "question text + answer" with the
    answer ("Expected '…identity? YES' but the page shows 'YES'"). Verification has to be exact before
    a submit gate can rely on it.
26. **Give each page a verdict.**
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

## Owner decisions

Decided on 2026-09-26:

- **Current location:** Chicago, IL (Phase 1 item 8).
- **Employer requests to write without AI:** ignore them and apply normally (Phase 1 item 11).
- **"How did you hear about us?":** LinkedIn (Phase 1 item 12).
- **Model provider:** stay on the free NVIDIA endpoint and use the best model it offers (see Model
  choice).

Still open. These block parts of Phase 1 and Phase 4:

1. **Consents.** Which may the tool give without asking: privacy policy, terms of use, SMS or
   marketing opt-ins, arbitration agreements?
2. **Optional demographic questions.** Answer them from the profile, decline, or skip them? Today
   they are sometimes answered and sometimes declined.

## Model choice (free NVIDIA endpoint)

**Decision (2026-09-27):**

- Structured answers and the audit pass: `nvidia/nemotron-3-ultra-550b-a55b` (`NVIDIA_MODEL`).
- Free-text essays: `z-ai/glm-5.3` (`NVIDIA_NARRATIVE_MODEL`).

No single model was best at both. The backend now sends essays to the narrative model. That covers
essays the bulk mapper wrote, focused retries and the first-person rewrite. If the narrative model
declines an essay, the question goes to the human instead of keeping the mapper's text.

**How it was tested.** 82 models are listed, 34 of them chat models; 24 of those returned 404,
timed out or errored for this key. The 10 that responded reliably were run on 14 real prompts from
the 2026-09-26 run (10 mapper, 4 audit) with only the model ID changed. Each prompt ran twice.
There were about 110 graded checks against the candidate's true facts, 48 of them critical
(invented facts, wrong demographics, name corruption, values in the wrong field). Separately, 6
essay questions went through the backend's real essay path, twice each, and every claim was
checked against the résumé.

| Model | Mapper accuracy | Mapper critical | Audit critical | Essays with an invented claim | Median / slowest call | 137k-token prompt |
|---|---|---|---|---|---|---|
| **nemotron-3-ultra-550b** | **97.6%** | **62/62** | 32/34 | 6 of 12 | **6.7 s / 29 s** | ✅ |
| **glm-5.3** | 95.9% | 61/62 | 32/34 | **1 of 12** | 11.8 s / 80 s | ❌ |
| gemma-4-31b | 94.7% | 62/62 | **34/34** | not tested | 29 s / 107 s (10 calls over the 45 s timeout) | ❌ |
| kimi-k3 | 95.3% | 60/62 | 23/25 (3 calls failed) | not tested | 17 s / 85 s | ✅ |
| gpt-oss-20b | 97.1% | 60/62 | 21/25 | 2 of 3 (early sample) | 31 s / 58 s | ❌ |
| nemotron-3-super-120b (used 09-26) | 94.7% | 59/62 | **24/34** (invented references, kept the fake certification) | 3 of 12 | 5.6 s / 38 s | ❌ |
| nemotron-3.5-lightning-30b | 83.9% | 55/60 | 28/34 | not tested | 36 s / 103 s | ❌ |
| ising-calibration-1.5-31b | 80.6% | 38/56 (invented reference answers) | 21/25 | not tested | 21 s / 89 s | ❌ |
| nemotron-3-nano-omni-30b | 60.3% | 42/54 | 22/29 | not tested | 11 s / 53 s | ❌ |
| mistral-nemotron | HTTP 500 on almost every call | | | | | |

Ultra's essay inventions included "I'm a certified yoga instructor who teaches weekend community
classes", mentoring that never happened, RBC "pipelines on Azure" (RBC was Snowflake/Kafka), and
Ramp company claims written from memory when only the job title could be fetched. glm-5.3's single
invention was "mentored peers", when asked about mentoring.

**Live check (2026-09-27, the same 9 forms re-run with the new setup):**

- **Better:**
  - G2 Ops used the correct US address instead of the mixed one.
  - G2 Ops' reference names were handed off instead of invented ("Globys" and "RBC" before).
  - Reddit's required "I agree" consent and OpenAI's "I confirm I have read the above" were
    filled.
  - Discord's essay came from Discord's own job page (`grounded=True`) instead of generic text.
  - Cohere's name stayed "Aarya Shah" (from the rewrite fix).
- **Unchanged, because they're code bugs:**
  - the LinkedIn URL in OpenAI's date picker
  - Givzey's phone number in every field
  - Shield AI's legal name set to the LinkedIn URL
  - Lever's pre-filled garbage LinkedIn URL
  - Mississauga on the Canada jobs
  - Discord's optional ethnicity flipping to "Southeast Asian", and Reddit's multi-select adding
    "East Asian" (both caused by the selected answer missing from the option list)
- **Still wrong, from the model in some contexts:** later per-field Ask AI calls on G2 Ops invented
  reference details ("Professional Reference", "2 years", the candidate's own contact details) and
  the self-referral. The audit *did* correct the IAT certification to "No", but the page kept
  "Yes". Clearance eligibility became "Yes"; super had answered "No".
- **Cost:** sending essays to glm-5.3 adds latency under load. On Palantir one essay took 40 s and
  another timed out, and that essay was handed off. The 150 s preview limit was also exceeded,
  mostly by a 2-minute scan in the extension before the backend was called.

**Fallback order** (Phase 1 item 1):

- Structured answers: ultra-550b → glm-5.3 → kimi-k3 → super-120b.
- Essays: glm-5.3 → super-120b.

Re-run the evaluation whenever the endpoint retires a model.

**What the model can't fix.** Every model scored well on the mapper, and most final-page errors on
09-26 came from code, not the model:

- the "Caucasian" substring match
- the name rewrite to "I" (fixed 2026-09-27)
- container fields and index drift
- option lists missing the selected answer

Two failures showed up with every model, so they need deterministic rules:

- Every top model kept an invented "3 years of DoD experience" during the audit (Phase 1 item 5).
- Every model invented something for "tell us something not on your résumé". Hand those questions
  off, or answer them from a saved answer.

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
.venv/bin/python docs/e2e_summarize.py docs/e2e_runs/<date> --detail   # per-job table + final values
```

Run output goes to `docs/e2e_runs/`, which is gitignored because screenshots contain profile data.
Postings close within weeks, so refresh `jobs.json` before each run. The DOM-read `required` flag
misses fields that ATSs mark as required only through CSS (Ashby does this), so treat the
required-field counts as a lower bound.
