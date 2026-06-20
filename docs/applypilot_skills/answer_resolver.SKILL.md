# Answer Resolver Skill

Purpose: resolve deterministic answers from the candidate profile after a field
kind is known.

Inputs:
- Canonical field kind.
- Candidate profile.
- Dropdown options, when available.

Outputs:
- Answer value plus source.

Rules:
- Deterministic profile fields do not need AI.
- Contact, address, work, education, and links must come directly from profile facts.
- For dropdowns, constrain answers to the supplied options.

Code:
- Chrome extension: `answerForFieldKind`, `buildCanonicalMappings`.
- Playwright agent: `resolve_field_kind`, `build_fill_plan`.
