#!/usr/bin/env python3
"""Parse a resume PDF into the extension's private profile format."""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from pypdf import PdfReader


SECTION_NAMES = [
    "education",
    "skills",
    "technical skills",
    "experience",
    "work experience",
    "professional experience",
    "projects",
    "leadership",
    "awards",
    "certifications",
]


def extract_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    pages = []

    for page in reader.pages:
        pages.append(page.extract_text() or "")

    return normalize_text("\n".join(pages))


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = text.replace("♂phone-square-alt", "")
    text = text.replace("/envel⌢p", "")
    text = text.replace("/h⌢me", "")
    text = text.replace("/linkedin", "")
    text = text.replace("/github", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_resume(text: str, pdf_path: Path, raw_text_file: str = "") -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sections = split_sections(lines)
    contact_blob = "\n".join(lines[:14])

    return {
        "contact": {
            "name": parse_name(lines),
            "email": first_match(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text),
            "phone": first_match(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", text),
            "linkedin": first_url(contact_blob, "linkedin"),
            "github": first_url(contact_blob, "github"),
            "portfolio": first_non_matching_url(contact_blob, ["linkedin", "github"]),
        },
        "resumeFacts": {
            "skills": parse_skills(sections),
            "education": parse_section_items(sections, ["education"]),
            "experience": parse_section_items(
                sections,
                ["experience", "work experience", "professional experience"],
            ),
            "workExperience": parse_work_experience(
                parse_section_items(
                    sections,
                    ["experience", "work experience", "professional experience"],
                )
            ),
            "projects": parse_section_items(sections, ["projects"]),
            "certifications": parse_section_items(sections, ["certifications"]),
            "rawTextFile": raw_text_file,
            "sourceFile": pdf_path.name,
        },
    }


def parse_name(lines: list[str]) -> str:
    if not lines:
        return ""

    for line in lines[:5]:
        if "@" not in line and not re.search(r"https?://|linkedin|github|\d{3}", line, re.I):
            return line

    return ""


def split_sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = "header"
    sections[current] = []
    section_pattern = re.compile(rf"^({'|'.join(re.escape(name) for name in SECTION_NAMES)})$", re.I)

    for line in lines:
        normalized = line.lower().strip(":")

        if section_pattern.match(normalized):
            current = normalized
            sections.setdefault(current, [])
            continue

        sections.setdefault(current, []).append(line)

    return sections


def parse_skills(sections: dict[str, list[str]]) -> list[str]:
    skill_lines = []

    for section_name in ["skills", "technical skills"]:
        skill_lines.extend(sections.get(section_name, []))

    if not skill_lines:
        return []

    blob = " ".join(skill_lines)
    blob = re.sub(r"\b(Languages|Frameworks|Tools|Technologies|Databases|Libraries)\s*:", "", blob, flags=re.I)
    parts = re.split(r"[,;|•]", blob)

    return unique_clean(parts)


def parse_section_items(sections: dict[str, list[str]], names: list[str]) -> list[str]:
    items = []

    for name in names:
        items.extend(sections.get(name, []))

    return unique_clean(items)


def parse_work_experience(lines: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    month_names = (
        "January|February|March|April|May|June|July|August|September|October|November|December"
    )
    date_pattern = re.compile(
        rf"^(?P<company>.+?)\s+(?P<start_month>{month_names})\s+(?P<start_year>\d{{4}})\s+"
        rf"(?:-|–|—|to)\s+(?:(?P<end_month>{month_names})\s+(?P<end_year>\d{{4}})|(?P<present>Present|Current))$",
        re.I,
    )

    for index, line in enumerate(lines):
        match = date_pattern.match(line)
        if not match:
            continue

        title_line = lines[index + 1] if index + 1 < len(lines) else ""
        title, location = split_title_location(title_line)
        company = clean_company(match.group("company"))
        entry = {
            "company": company,
            "title": title,
            "location": location,
            "startMonth": canonical_month(match.group("start_month")),
            "startYear": match.group("start_year"),
            "endMonth": canonical_month(match.group("end_month") or ""),
            "endYear": match.group("end_year") or "",
            "currentRole": bool(match.group("present")),
        }

        entries.append({key: value for key, value in entry.items() if value not in ("", None)})

    return entries


def split_title_location(line: str) -> tuple[str, str]:
    if not line:
        return "", ""

    match = re.match(
        r"^(?P<title>.*?\b(?:Engineer|Assistant|Scientist|Developer|Analyst|Intern|Manager|Architect|Consultant|Specialist)\b)\s+"
        r"(?P<location>[A-Z][A-Za-z .'-]+,\s*[A-Z]{2})$",
        line,
    ) or re.match(r"^(?P<title>.+)\s+(?P<location>[A-Z][A-Za-z .'-]+,\s*[A-Z]{2})$", line)
    if not match:
        return line, ""

    return match.group("title").strip(), match.group("location").strip()


def canonical_month(value: str) -> str:
    return value[:1].upper() + value[1:].lower() if value else ""


def clean_company(value: str) -> str:
    return re.sub(r"\bW aterloo\b", "Waterloo", value).strip()


def first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I)
    return match.group(0).strip() if match else ""


def first_url(text: str, required_fragment: str) -> str:
    for url in extract_urls(text):
        if required_fragment.lower() in url.lower():
            return url

    return ""


def first_non_matching_url(text: str, excluded_fragments: list[str]) -> str:
    for url in extract_urls(text):
        lowered = url.lower()

        if not any(fragment.lower() in lowered for fragment in excluded_fragments):
            return url

    return ""


def extract_urls(text: str) -> list[str]:
    url_pattern = r"(?<![@\w.-])(?:https?://)?(?:www\.)?[\w.-]+\.[a-z]{2,}(?:/[\w./?=&%+#-]*)?"
    return [url.rstrip(".,)") for url in re.findall(url_pattern, text, re.I)]


def unique_clean(values: list[str]) -> list[str]:
    seen = set()
    cleaned = []

    for value in values:
        item = re.sub(r"\s+", " ", str(value)).strip(" -•\t")

        if not item or item.lower() in seen:
            continue

        seen.add(item.lower())
        cleaned.append(item)

    return cleaned


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    return json.loads(path.read_text(encoding="utf-8"))


def merge_profile(existing: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(existing)
    profile = payload.setdefault("candidateProfile", {})
    existing_resume_facts = profile.get("resumeFacts", {})
    contact = parsed["contact"]

    name = contact.get("name") or ""
    if name:
        parts = name.split()
        profile.setdefault("fullName", name)
        profile.setdefault("firstName", parts[0])
        profile.setdefault("lastName", parts[-1] if len(parts) > 1 else "")

    for parsed_key, profile_key in [
        ("email", "email"),
        ("phone", "phone"),
        ("linkedin", "linkedin"),
        ("github", "github"),
        ("portfolio", "portfolio"),
    ]:
        if contact.get(parsed_key) and not profile.get(profile_key):
            profile[profile_key] = contact[parsed_key]

    profile["resumeFileName"] = parsed["resumeFacts"].get("sourceFile", "")
    resume_facts = parsed["resumeFacts"]
    for preserved_key in ["projectLinks", "links", "notes"]:
        if existing_resume_facts.get(preserved_key):
            resume_facts[preserved_key] = existing_resume_facts[preserved_key]

    profile["resumeFacts"] = resume_facts
    if resume_facts.get("workExperience"):
        profile["workExperience"] = resume_facts["workExperience"]
    profile.setdefault("currentOrPreviousEmployer", "")
    profile.setdefault("currentOrPreviousJobTitle", "")
    profile.setdefault("demographics", {})
    profile["demographics"].setdefault("race", "")
    profile["demographics"].setdefault("ethnicity", "")
    profile["demographics"].setdefault("hispanicLatino", "")
    profile["demographics"].setdefault("gender", "")
    profile["demographics"].setdefault("genderIdentity", "")
    profile.setdefault("answers", {})
    profile["answers"].setdefault("previouslyEmployedByCompany", "No")
    profile["answers"].setdefault("recruitingMessages", "No")
    profile["answers"].setdefault("veteranStatus", profile.get("veteranStatus", "No"))

    settings = payload.setdefault("settings", {})
    settings.setdefault("backendBaseUrl", "http://127.0.0.1:8000")
    settings.setdefault("backendMapperUrl", "")
    settings.setdefault("targetCountry", "canada")
    settings.setdefault("autoFillDynamicFields", False)
    settings.setdefault("autoFillSensitiveFields", False)
    settings.setdefault("requireReviewBeforeSubmit", True)

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Resume PDF path")
    parser.add_argument("--profile", type=Path, default=Path("autofill_extension/profile.private.json"))
    parser.add_argument("--out-profile", type=Path, default=Path("autofill_extension/profile.private.json"))
    parser.add_argument("--out-text", type=Path, default=Path("autofill_extension/generated/resume_text.private.txt"))
    parser.add_argument("--out-parsed", type=Path, default=Path("autofill_extension/generated/resume_parsed.private.json"))
    args = parser.parse_args()

    text = extract_text(args.pdf)
    args.out_text.parent.mkdir(parents=True, exist_ok=True)
    args.out_text.write_text(text + "\n", encoding="utf-8")

    parsed = parse_resume(text, args.pdf, str(args.out_text))
    args.out_parsed.parent.mkdir(parents=True, exist_ok=True)
    args.out_parsed.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

    existing = load_json(args.profile)
    merged = merge_profile(existing, parsed)
    args.out_profile.parent.mkdir(parents=True, exist_ok=True)
    args.out_profile.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    facts = parsed["resumeFacts"]
    print(f"Parsed {args.pdf.name}")
    print(f"Text characters: {len(text)}")
    print(f"Skills: {len(facts['skills'])}")
    print(f"Education lines: {len(facts['education'])}")
    print(f"Experience lines: {len(facts['experience'])}")
    print(f"Project lines: {len(facts['projects'])}")


if __name__ == "__main__":
    main()
