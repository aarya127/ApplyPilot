from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


HOSTNAME_RULES: list[tuple[str, str]] = [
    (r"(^|\.)greenhouse\.io$", "greenhouse"),
    (r"(^|\.)lever\.co$", "lever"),
    (r"(^|\.)ashbyhq\.com$", "ashby"),
    (r"(^|\.)myworkdayjobs\.com$", "workday"),
    (r"(^|\.)myworkdaysite\.com$", "workday"),
    (r"(^|\.)workday\.com$", "workday"),
    (r"(^|\.)taleo\.net$", "taleo"),
    (r"(^|\.)oraclecloud\.com$", "oracle"),
    (r"(^|\.)icims\.com$", "icims"),
    (r"(^|\.)smartrecruiters\.com$", "smartrecruiters"),
    (r"(^|\.)successfactors\.[a-z]{2,6}$", "successfactors"),
    (r"(^|\.)sapsf\.(com|eu|cn)$", "successfactors"),
    (r"^jobs\.sap\.com$", "successfactors"),
]

PATH_RULES: list[tuple[str, str]] = [
    (r"/careersection/", "taleo"),
    (r"/hcmui/candidateexperience", "oracle"),
]

DOM_MARKER_RULES: list[tuple[str, str]] = [
    ("#grnhse_iframe, #grnhse_app, iframe[src*='greenhouse.io']", "greenhouse"),
    ("iframe[src*='jobs.lever.co']", "lever"),
    ("#ashby_embed, iframe[src*='ashbyhq.com']", "ashby"),
    (
        "iframe[src*='myworkdayjobs.com'], [data-automation-id='applyFlowPage'], "
        "[data-automation-id='jobPostingPage']",
        "workday",
    ),
    ("iframe[src*='taleo.net'], form[action*='careersection']", "taleo"),
    ("#icims_content_iframe, iframe[src*='icims.com']", "icims"),
    ("iframe[src*='smartrecruiters.com'], meta[name='generator'][content*='SmartRecruiters']", "smartrecruiters"),
    ("iframe[src*='successfactors'], script#sap-ui-bootstrap", "successfactors"),
    ("iframe[src*='oraclecloud.com']", "oracle"),
]


def detect_ats(url: str, page: Any = None) -> str:
    detected = detect_ats_from_url(url)
    if detected != "generic":
        return detected

    if page is not None and hasattr(page, "locator"):
        detected = detect_ats_from_dom(page)
        if detected:
            return detected

    return "generic"


def detect_ats_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    for pattern, ats in HOSTNAME_RULES:
        if hostname and re.search(pattern, hostname):
            return ats

    for pattern, ats in PATH_RULES:
        if path and re.search(pattern, path):
            return ats

    return "generic"


def detect_ats_from_dom(page: Any) -> str:
    for selector, ats in DOM_MARKER_RULES:
        try:
            if page.locator(selector).count() > 0:
                return ats
        except Exception:
            continue

    return ""
