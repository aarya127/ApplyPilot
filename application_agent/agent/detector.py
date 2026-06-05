from __future__ import annotations


def detect_ats(url: str, page_text: str = "") -> str:
    haystack = f"{url} {page_text}".lower()

    if "greenhouse.io" in haystack or "boards.greenhouse" in haystack:
        return "greenhouse"

    if "lever.co" in haystack or "jobs.lever.co" in haystack:
        return "lever"

    if "ashbyhq.com" in haystack or "ashby" in haystack:
        return "ashby"

    if "myworkdayjobs.com" in haystack or "workday" in haystack:
        return "workday"

    return "generic"

