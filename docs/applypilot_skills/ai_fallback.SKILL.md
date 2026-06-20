# AI Fallback Skill

Purpose: answer unresolved required questions using candidate context and visible
options.

Inputs:
- Unresolved field.
- Candidate profile facts.
- Resume transcript/facts.
- Default policies.
- Dropdown options, if any.

Outputs:
- Structured answer, confidence, and reason.

Rules:
- Do not overwrite deterministic profile fields.
- For optioned fields, return one exact supplied option.
- Do not invent experience or credentials.
- Use saved answers before generating new text.

Code:
- Backend: `autofill_extension/backend/server.py`.
- Playwright agent: `application_agent/agent/answer_generator.py`.
