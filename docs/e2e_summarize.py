"""Summarize a docs/e2e_batch.py run: one row per job, plus (with --detail) every value
the page ended up with, for checking answers against the profile by hand.

Usage:
  .venv/bin/python docs/e2e_summarize.py docs/e2e_runs/<name> [--detail]
"""
import json
import sys
from pathlib import Path


def outcome(record):
    blockers = record.get("blockers") or {}
    scanned = ((record.get("summary") or {}).get("firstScan") or {}).get("scanned") or 0
    if record.get("fatal"):
        return "harness-error"
    if blockers.get("closedText"):
        return "closed?"
    if blockers.get("botChallenge"):
        return "bot-challenge"
    if blockers.get("password") or (blockers.get("signInText") and scanned == 0):
        return "login-wall"
    if scanned == 0:
        return "no-fields-seen"
    return "form"


def captcha(record):
    blockers = record.get("blockers") or {}
    kinds = set()
    for frame in blockers.get("captchaFrames") or []:
        src = frame["src"]
        kinds.add("hcaptcha" if "hcaptcha" in src else "turnstile" if "turnstile" in src or "cloudflare" in src else "recaptcha")
    return ",".join(sorted(kinds)) or ("recaptcha-badge" if blockers.get("recaptchaBadge") else "-")


def required_groups(fields):
    """Required fields as answered/unanswered groups: a radio or checkbox group counts
    once, answered if any option in it is checked."""
    groups = {}
    for field in fields:
        if not field.get("required"):
            continue
        grouped = field["kind"] in ("input:radio", "input:checkbox")
        key = field["label"].split(" :: ")[0] if grouped else f"{field['label']}|{field['kind']}"
        answered = bool(field["value"]) and field["value"] not in ("Select...", "Select ...")
        groups[key] = groups.get(key, False) or answered
    return groups


def main():
    run = Path(sys.argv[1])
    detail = "--detail" in sys.argv
    records = [json.loads(path.read_text()) for path in sorted(run.glob("[0-9][0-9]_*.json"))]

    print(f"{'#':>2} {'board':15} {'company':22} {'outcome':15} {'scan':>4} {'reqFilled':>9} {'mism':>4} {'secs':>5} captcha")
    for record in records:
        summary = record.get("summary") or {}
        groups = required_groups(record.get("finalPageFields") or [])
        print(f"{record['slug'][:2]:>2} {record['job']['board']:15} {record['job']['company'][:22]:22} {outcome(record):15} "
              f"{((summary.get('firstScan') or {}).get('scanned') or 0):>4} {sum(groups.values()):>4}/{len(groups):<4} "
              f"{len(summary.get('mismatches') or []):>4} {record.get('durationS', 0):>5.0f} {captcha(record)}")

    if not detail:
        return
    for record in records:
        if outcome(record) != "form":
            continue
        summary = record["summary"]
        print(f"\n===== {record['slug']} | {record['job']['title']} | {record.get('applicationUrl', '')[:90]}")
        for step in record["steps"]:
            print(f"  step {step['name']}: {step.get('status', '')} {step.get('skipped', '')} {step.get('durationS', '')}s")
        for key, title in [("needsAnswerCards", "NEEDS ANSWER"), ("mismatches", "MISMATCH"),
                           ("readyUnfilled", "READY-BUT-UNFILLED"), ("errorMessages", "ERRORS")]:
            if summary.get(key):
                print(f"  {title}:", [item[:160] for item in summary[key]])
        for field in record.get("finalPageFields") or []:
            if field["kind"] in ("input:radio", "input:checkbox") and not field["value"]:
                continue
            flag = "REQ" if field.get("required") else "   "
            print(f"   {flag} {field['label'][:85]:85} => {field['value'][:110]}")


if __name__ == "__main__":
    main()
