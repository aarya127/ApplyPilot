# Verifier Skill

Purpose: re-read the page after filling and report mismatches.

Inputs:
- Fill plan items.
- Current page fields.

Outputs:
- Matched count.
- Mismatched fields.
- Unreadable fields.

Rules:
- Verification should not mutate the page.
- Mismatches should be shown to the user or passed to AI review.
- Contact/link/address mismatches are high priority because they are costly.

Code:
- Chrome extension: `verifyFilledMappings`.
- Playwright agent: `application_agent/agent/verifier.py`.
