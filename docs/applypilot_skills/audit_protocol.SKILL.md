# Audit Protocol Skill

Purpose: review autofill and AI answers before they are applied, then explain
what was kept, corrected, filled, or skipped.

Inputs:

- Visible field metadata, including label, current value, required flag, and
  discovered options.
- Proposed mappings from deterministic autofill and AI.
- Candidate profile, resume facts, saved answers, and default policies.
- Page context such as target country and ATS URL.

Outputs:

- `decisions`: one entry per audited answer.
- `corrections`: only answers that should be applied to fix or fill a field.
- `issues`: required/unsafe fields that could not be answered safely.

Decision actions:

- `keep`: the current answer matches profile, policy, or saved-answer context.
- `correct`: the current answer conflicts with known facts.
- `fill`: the field is blank and the answer is safe and specific.
- `skip`: the field cannot be answered safely, options are missing, or context is
  insufficient.

Rules:

- The goal is the most accurate truthful answer for each question.
- Do not overwrite correct identity, contact, address, resume, experience,
  education, or link fields.
- If a field has options, `value` must be one exact discovered option label.
- If options are not discoverable for a dropdown-like field, skip and log why.
- For ambiguous questions, prefer profile/resume/saved-answer facts over model
  inference.
- Default policies may answer common eligibility questions, but must remain
  generic and not company-specific.
- Never hide failures: skipped and mismatched fields must be surfaced to the
  assistant UI or trace logs.

Code anchors:

- Chrome extension backend: `/audit-fields`, `build_audit_prompt`,
  `deterministic_audit_report`, `normalize_audit_decisions`.
- Side panel: `auditCurrentAutofillAnswers`.
- Trace log: `autofill_extension/generated/llm_trace.private.jsonl`.
