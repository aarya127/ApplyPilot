# Scanner Skill

Purpose: convert the current ATS page into a structured list of visible fields.

Inputs:
- Current browser page or content-script DOM.

Outputs:
- Field records with label, tag, type, name, id, placeholder, ARIA label,
  current value, required flag, surrounding text, and raw options when available.

Rules:
- Do not answer or fill fields during scanning.
- Keep nearby text as context, not as the field identity.
- Group radio/checkbox choices into one logical field.

Code:
- Chrome extension: `autofill_extension/src/content.js` `scanFields`.
- Playwright agent: `application_agent/agent/form_scanner.py`.
