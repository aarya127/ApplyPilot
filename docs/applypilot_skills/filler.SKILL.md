# Filler Skill

Purpose: apply an approved fill plan to the live ATS page.

Inputs:
- Fill plan items with field, field kind, answer, and source.

Outputs:
- Count of filled fields and per-field failures.

Rules:
- Dispatch input/change/blur events after filling.
- For custom dropdowns, click the option when possible.
- Never click final submit.
- Keep final submission as a manual user action.

Code:
- Chrome extension: `applyMappings`, `fillElement`, `fillCombobox`.
- Playwright agent: `application_agent/ats/base.py`.
