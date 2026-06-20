# ATS Adapters Skill

Purpose: handle platform-specific DOM quirks after the generic plan is built.

Examples:
- Greenhouse: click dropdown options and handle hidden option lists.
- Workday: refill dependent address fields after country changes.
- Lever: label-based fields and native file upload.
- Oracle/Taleo/iCIMS/SmartRecruiters/SuccessFactors: custom dropdown selectors.

Rules:
- Keep adapter logic narrow and platform-specific.
- Do not hardcode company-specific answers.
- Adapter fallbacks should repair DOM behavior, not change candidate facts.

Code:
- Chrome extension: Workday/Greenhouse fallbacks in `content.js`.
- Playwright agent: `application_agent/ats/*.py`.
