from __future__ import annotations

import re


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

    if (
        "oraclecloud.com" in haystack
        or "oracle recruiting" in haystack
        or "oracle hcm" in haystack
        or "oracle cloud" in haystack
    ):
        return "oracle"

    if "taleo.net" in haystack or "careersection" in haystack:
        return "taleo"

    if "icims.com" in haystack or "icims" in haystack:
        return "icims"

    if "smartrecruiters.com" in haystack or "smartrecruiters" in haystack:
        return "smartrecruiters"

    if (
        "successfactors.com" in haystack
        or re.search(r"successfactors\.[a-z.]+", haystack)
        or "jobs.sap.com" in haystack
        or "sap successfactors" in haystack
        or "successfactors" in haystack
    ):
        return "successfactors"

    return "generic"
