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

Debugging:
- Backend LLM requests and responses are logged to:
  `autofill_extension/generated/llm_trace.private.jsonl`.
- The generated folder is gitignored because traces include private profile,
  resume, field, and answer context.
- You can inspect recent traces with:
  `GET http://127.0.0.1:8000/llm-traces?limit=20`.
- Trace events include mapper/auditor request payloads, prompts, visible fields,
  options, raw model output, parsed JSON, enforced option mappings, and merged
  fallback mappings.
