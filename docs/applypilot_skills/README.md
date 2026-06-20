# ApplyPilot Workflow Skills

These files document the subprocesses that make up the auto-application workflow.
They are intentionally small and operational: each file explains one responsibility,
its inputs, outputs, safety rules, and where to look in the code.

Recommended workflow:

1. Scan the page into structured fields.
2. Classify each field into a canonical field kind.
3. Discover dropdown/radio/checkbox options before choosing answers.
4. Resolve deterministic answers from the profile.
5. Ask AI only for unresolved or ambiguous fields.
6. Fill from the approved plan.
7. Verify the page after filling.
8. Log skipped, failed, and mismatched fields.

Skills:

- `scanner.SKILL.md`
- `field_classifier.SKILL.md`
- `option_discovery.SKILL.md`
- `answer_resolver.SKILL.md`
- `ai_fallback.SKILL.md`
- `filler.SKILL.md`
- `verifier.SKILL.md`
- `ats_adapters.SKILL.md`
