import base64
import importlib.util
import json
import re
from pathlib import Path

import pytest

from autofill_extension.backend import server
from autofill_extension.tools.parse_resume import merge_profile, parse_resume


ROOT = Path(__file__).resolve().parents[1]


def test_resume_merge_preserves_existing_contact_and_project_links():
    parsed = {
        "contact": {
            "name": "Parsed Candidate",
            "email": "parsed@example.com",
            "phone": "555-111-2222",
            "linkedin": "https://linkedin.example/parsed",
            "github": "https://github.example/parsed",
            "portfolio": "https://portfolio.example",
        },
        "resumeFacts": {
            "skills": ["Python"],
            "education": ["Sample University"],
            "experience": ["Sample Company"],
            "projects": ["Sample Project"],
            "sourceFile": "resume.pdf",
            "rawTextFile": "generated/resume.txt",
        },
    }
    existing = {
        "candidateProfile": {
            "firstName": "Existing",
            "email": "existing@example.com",
            "github": "https://github.example/existing",
            "resumeFacts": {
                "projectLinks": {
                    "sample": {
                        "name": "Sample",
                        "url": "https://github.example/existing/sample",
                        "type": "project_repository",
                    }
                }
            },
        },
        "settings": {},
    }

    merged = merge_profile(existing, parsed)
    profile = merged["candidateProfile"]

    assert profile["email"] == "existing@example.com"
    assert profile["github"] == "https://github.example/existing"
    assert profile["resumeFileName"] == "resume.pdf"
    assert profile["resumeFacts"]["skills"] == ["Python"]
    assert profile["resumeFacts"]["projectLinks"]["sample"]["url"] == "https://github.example/existing/sample"


def test_parse_resume_text_extracts_sections():
    text = """
    Sample Candidate
    sample@example.com
    Skills
    Python, SQL, Docker
    Education
    Sample University
    Experience
    Software Engineer
    Projects
    Useful Project
    """

    parsed = parse_resume(text, Path("resume.pdf"), "resume.txt")

    assert parsed["contact"]["email"] == "sample@example.com"
    assert "Python" in parsed["resumeFacts"]["skills"]
    assert parsed["resumeFacts"]["education"] == ["Sample University"]
    assert parsed["resumeFacts"]["experience"] == ["Software Engineer"]
    assert parsed["resumeFacts"]["projects"] == ["Useful Project"]


def test_parse_resume_text_extracts_structured_work_experience():
    text = """
    Sample Candidate
    sample@example.com
    Experience
    Example Labs September 2025 – December 2025
    Machine Learning Engineer Santa Barbara, CA
    Built useful systems
    Improved useful systems by 20%
    Sample University January 2025 – April 2025
    Research Assistant Sample City, ST
    """

    parsed = parse_resume(text, Path("resume.pdf"), "resume.txt")
    work = parsed["resumeFacts"]["workExperience"]

    assert work[0]["company"] == "Example Labs"
    assert work[0]["title"] == "Machine Learning Engineer"
    assert work[0]["location"] == "Santa Barbara, CA"
    assert "Built useful systems" in work[0]["description"]
    assert "Improved useful systems by 20%" in work[0]["description"]
    assert work[0]["startMonth"] == "September"
    assert work[0]["endYear"] == "2025"
    assert work[1]["company"] == "Sample University"


def test_backend_without_api_key_returns_empty_llm_mapping(monkeypatch, tmp_path):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "applications.sqlite3")
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    server._ai_request_times.clear()

    client = server.app.test_client()
    response = client.post("/map-fields", json={"fields": [], "profile": {}, "page": {}})

    assert response.status_code == 200
    assert response.json["mappings"] == []
    assert "NVIDIA_API_KEY" in response.json["warning"]
    assert response.json["aiUsage"]["requestsLastMinute"] == 0


def test_backend_ai_usage_tracks_requests_in_last_minute():
    server._ai_request_times.clear()

    assert server.ai_usage_snapshot(now=1000)["requestsLastMinute"] == 0
    first = server.record_ai_request(now=1000)
    second = server.record_ai_request(now=1001)

    assert first["requestsLastMinute"] == 1
    assert second["requestsLastMinute"] == 2
    assert second["limitPerMinute"] == 40
    assert server.ai_usage_snapshot(now=1030)["requestsLastMinute"] == 2
    assert server.ai_usage_snapshot(now=1062)["requestsLastMinute"] == 0


def test_backend_ai_usage_endpoint_reports_counter():
    server._ai_request_times.clear()
    server.record_ai_request()

    client = server.app.test_client()
    response = client.get("/ai-usage")

    assert response.status_code == 200
    assert response.json["ok"] is True
    assert response.json["aiUsage"]["requestsLastMinute"] == 1
    assert response.json["aiUsage"]["remainingThisMinute"] == 39


def test_backend_mapper_failure_returns_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "applications.sqlite3")
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    monkeypatch.setattr(server, "call_nvidia_mapper", lambda fields, profile, page: (_ for _ in ()).throw(RuntimeError("boom")))

    client = server.app.test_client()
    response = client.post(
        "/map-fields",
        json={
            "fields": [
                {
                    "index": 0,
                    "label": "Are you legally authorized to work in the United States?",
                    "options": [
                        {"label": "I am authorized to work in the United States for any employer"},
                        {"label": "I require sponsorship"},
                    ],
                },
                {
                    "index": 1,
                    "label": "How Did You Hear About Us?",
                    "options": [{"label": "LinkedIn"}, {"label": "Other"}],
                },
            ],
            "profile": {"workAuthorization": "Yes"},
            "page": {},
        },
    )

    assert response.status_code == 200
    # Eligibility is answered authoritatively before the mapper runs; the
    # non-authoritative field falls back to policy when the mapper fails.
    assert response.json["mappings"] == [
        {
            "index": 0,
            "value": "I am authorized to work in the United States for any employer",
            "confidence": 0.95,
            "source": "policy",
        },
        {
            "index": 1,
            "value": "LinkedIn",
            "confidence": 0.78,
            "source": "policy",
        },
    ]
    assert "Mapper request failed" in response.json["warning"]


def test_backend_policy_mapper_answers_general_eligibility_dropdowns():
    fields = [
        {
            "index": 0,
            "label": "Can you meet the requirement that you are at least 18 years of age?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 1,
            "label": "Do you now or will you in the future require sponsorship of a visa?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 2,
            "label": "Do you have any relatives employed by this company?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 3,
            "label": "Are you interested in relocating? If so, where?",
            "options": [{"label": "Local"}, {"label": "Nationwide"}, {"label": "Anywhere"}],
        },
    ]

    assert server.policy_mappings(fields, {}) == [
        {"index": 0, "value": "Yes", "confidence": 0.78, "source": "policy"},
        {"index": 1, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 2, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 3, "value": "Anywhere", "confidence": 0.78, "source": "policy"},
    ]


def test_backend_policy_distinguishes_relocation_assistance_from_relocation_preference():
    fields = [
        {
            "index": 0,
            "label": "Will you need relocation assistance to work at this role's specified location?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        }
    ]

    assert server.policy_mappings(fields, {"relocation": "Open to relocation"}) == [
        {"index": 0, "value": "No", "confidence": 0.78, "source": "policy"}
    ]


def test_backend_policy_defaults_group_affiliations_to_none_of_the_above():
    fields = [
        {
            "index": 0,
            "label": "Which of the following communities do you belong to?",
            "options": [
                {"label": "Person with disability"},
                {"label": "Veteran"},
                {"label": "Parent"},
                {"label": "None of the above"},
                {"label": "I prefer not to answer"},
            ],
        }
    ]

    assert server.policy_mappings(fields, {}) == [
        {"index": 0, "value": "None of the above", "confidence": 0.78, "source": "policy"}
    ]


def test_backend_policy_answers_certification_yes_and_subscriptions_no():
    fields = [
        {
            "index": 0,
            "label": "By selecting Yes, I certify that my application is true and correct.",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 1,
            "label": "Subscribe to job alerts and marketing emails?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 2,
            "label": "Do you accept the Terms and Conditions?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
    ]

    assert server.policy_mappings(fields, {}) == [
        {"index": 0, "value": "Yes", "confidence": 0.78, "source": "policy"},
        {"index": 1, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 2, "value": "Yes", "confidence": 0.78, "source": "policy"},
    ]


def test_backend_policy_prioritizes_sponsorship_over_authorization_phrase():
    fields = [
        {
            "index": 0,
            "label": (
                "Do you now or will you in the future require sponsorship of a visa "
                "for employment authorization in the United States?"
            ),
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 1,
            "label": "Are you legally authorized to work in the United States?",
            "options": [
                {"label": "I am authorized to work in the United States for any employer"},
                {"label": "I require sponsorship"},
            ],
        },
        {
            "index": 2,
            "label": "Are you legally eligible to work in the country of employment?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
    ]

    assert server.policy_mappings(fields, {"workAuthorization": "Yes"}) == [
        {"index": 0, "value": "No", "confidence": 0.78, "source": "policy"},
        {
            "index": 1,
            "value": "I am authorized to work in the United States for any employer",
            "confidence": 0.78,
            "source": "policy",
        },
        {"index": 2, "value": "Yes", "confidence": 0.78, "source": "policy"},
    ]


def test_backend_policy_treats_work_authorization_assistance_as_sponsorship():
    fields = [
        {
            "index": 0,
            "label": "Will you require our assistance with work authorization now or in the future?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 1,
            "label": "Do you need employer help with employment authorization now or later?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
    ]

    assert server.policy_mappings(fields, {}) == [
        {"index": 0, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 1, "value": "No", "confidence": 0.78, "source": "policy"},
    ]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Are you legally eligible to work in the U.S.?", "Yes"),
        ("Are you authorized to work in the country where this role is based?", "Yes"),
        ("Will you now or in the future require visa sponsorship for employment at Example Energy?", "No"),
        ("Do you now or later need employer sponsorship for a work permit?", "No"),
        (
            "Do you have any close relatives who work at Example Energy? For this purpose, close relatives include spouse, domestic partner, parent, child, or sibling.",
            "No",
        ),
        (
            "Are you related to, or in a close personal relationship with, anyone currently employed by Example Studios?",
            "No",
        ),
        (
            "Please enter the Name and Department of any of your close relatives who work at Example Energy.",
            "N/A",
        ),
        (
            "If yes, please provide the relative's name, department, and relationship.",
            "N/A",
        ),
        (
            "Are you currently, have you ever been, or has the government ever proposed that you be excluded, debarred, suspended or otherwise ineligible from participation in any federal or state health care program or other government procurement programs?",
            "No",
        ),
        (
            "Have you ever been employed by a federal, state or local government entity, including Military, Civil Service, or a VA Hospital?",
            "No",
        ),
        (
            "Have you ever had, or do you anticipate receiving, any disciplinary action taken on your professional license, certification, or credentials?",
            "No",
        ),
        (
            "How Did You Hear About Us?",
            "LinkedIn",
        ),
        (
            "Overall Result (GPA)",
            "3.7 out of 4",
        ),
        (
            "If relocation is required for this opportunity, and relocation assistance is not offered for this position, are you willing to relocate at your own cost?",
            "Yes",
        ),
    ],
)
def test_backend_policy_answers_generic_application_policy_questions(label, expected):
    profile = {"workAuthorization": "Yes", "answers": {"gpa": "3.7 out of 4"}}
    assert server.policy_mappings([{"index": 0, "label": label}], profile) == [
        {"index": 0, "value": expected, "confidence": 0.78, "source": "policy"}
    ]


@pytest.mark.parametrize(
    "label",
    [
        "Are you legally eligible to work in the U.S.?",
        "Are you authorized to work in the country where this role is based?",
        "Overall Result (GPA)",
    ],
)
def test_backend_policy_skips_work_authorization_and_gpa_when_profile_is_silent(label):
    assert server.policy_mappings([{"index": 0, "label": label}], {}) == []


def test_backend_policy_answers_generic_policy_questions_without_visible_options_together():
    fields = [
        {"index": 0, "label": "Are you legally eligible to work in the U.S.?"},
        {"index": 1, "label": "Will you now or in the future require visa sponsorship for employment at Example Energy?"},
        {
            "index": 2,
            "label": (
                "Do you have any close relatives who work at Example Energy? For this purpose, "
                "close relatives include spouse, domestic partner, parent, child, or sibling, and each of their respective spouses or domestic partners."
            ),
        },
        {
            "index": 3,
            "label": (
                "Please enter the Name and Department of any of your close relatives who work at Example Energy? "
                "For this purpose, close relatives include spouse, domestic partner, parent, child, or sibling, and each of their respective spouses or domestic partners"
            ),
        },
    ]

    assert server.policy_mappings(fields, {"workAuthorization": "Yes"}) == [
        {"index": 0, "value": "Yes", "confidence": 0.78, "source": "policy"},
        {"index": 1, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 2, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 3, "value": "N/A", "confidence": 0.78, "source": "policy"},
    ]


def test_backend_policy_answers_generic_voluntary_demographics_from_profile():
    profile = {
        "veteranStatus": "No",
        "disabilityStatus": "No",
        "demographics": {
            "gender": "Male",
            "hispanicLatino": "No",
            "race": "Asian",
            "sexualOrientation": "Heterosexual",
        },
    }
    fields = [
        {"index": 0, "label": "Gender", "options": [{"label": "Male"}, {"label": "Female"}, {"label": "Decline to self-identify"}]},
        {"index": 1, "label": "Are you Hispanic/Latino?", "options": [{"label": "Yes"}, {"label": "No"}]},
        {"index": 2, "label": "Please identify your race", "options": [{"label": "White"}, {"label": "Asian"}, {"label": "Decline to self-identify"}]},
        {"index": 3, "label": "Sexual Orientation", "options": [{"label": "Heterosexual / straight"}, {"label": "I prefer not to answer"}]},
        {
            "index": 4,
            "label": "Disability Status",
            "options": [
                {"label": "Yes, I have a disability, or have had one in the past"},
                {"label": "No, I don't have a disability and have not had one in the past"},
                {"label": "I do not want to answer"},
            ],
        },
        {"index": 5, "label": "Veteran Status", "options": [{"label": "I am not a protected veteran"}, {"label": "I decline to self-identify"}]},
    ]

    assert server.policy_mappings(fields, profile) == [
        {"index": 0, "value": "Male", "confidence": 0.78, "source": "policy"},
        {"index": 1, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 2, "value": "Asian", "confidence": 0.78, "source": "policy"},
        {"index": 3, "value": "Heterosexual / straight", "confidence": 0.78, "source": "policy"},
        {"index": 4, "value": "No, I don't have a disability and have not had one in the past", "confidence": 0.78, "source": "policy"},
        {"index": 5, "value": "I am not a protected veteran", "confidence": 0.78, "source": "policy"},
    ]


def test_backend_policy_matches_long_no_dropdown_options():
    fields = [
        {
            "index": 0,
            "label": "Do you currently or have you ever served in the U.S. Military?",
            "options": [
                {"label": "Yes, I am currently serving or have served in the Armed Forces of the United States"},
                {"label": "No, I have never served in the Armed Forces of the United States"},
            ],
        },
        {
            "index": 1,
            "label": "Do you now or will you in the future require sponsorship of a visa?",
            "options": [
                {"label": "Yes, I require sponsorship"},
                {"label": "No, I do not require sponsorship"},
            ],
        },
    ]

    assert server.policy_mappings(fields, {}) == [
        {
            "index": 0,
            "value": "No, I have never served in the Armed Forces of the United States",
            "confidence": 0.78,
            "source": "policy",
        },
        {
            "index": 1,
            "value": "No, I do not require sponsorship",
            "confidence": 0.78,
            "source": "policy",
        },
    ]


def test_backend_policy_uses_surrounding_text_only_for_low_information_labels():
    profile = {
        "workExperience": [
            {"company": "Example Labs", "title": "Engineer"},
        ]
    }
    fields = [
        {
            "index": 0,
            "label": "Yes",
            "surroundingText": (
                "Have you previously been DIRECTLY employed with Example ParentCo AG or Example Affiliate Inc.? "
                "Yes No"
            ),
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 1,
            "label": "Have you previously been DIRECTLY employed with Example ParentCo AG or Example Affiliate Inc.?",
            "surroundingText": "Resume context Example Labs Engineer",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 2,
            "label": (
                "Have you previously been DIRECTLY employed with Example ParentCo AG or Example Affiliate Inc.? "
                "(i.e. have you received a paycheck or W-2 directly from one of these companies?)."
            ),
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 3,
            "label": "Yes",
            "questionText": (
                "Have you previously been DIRECTLY employed with Example ParentCo AG or Example Affiliate Inc.? "
                "(i.e. have you received a paycheck or W-2 directly from one of these companies?)."
            ),
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 4,
            "label": "Yes Required",
            "questionText": (
                "Have you previously been DIRECTLY employed with Example ParentCo AG or Example Affiliate Inc.? "
                "(i.e. have you received a paycheck or W-2 directly from one of these companies?)."
            ),
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
    ]

    assert server.policy_mappings(fields, profile) == [
        {"index": 0, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 1, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 2, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 3, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 4, "value": "No", "confidence": 0.78, "source": "policy"},
    ]


def test_backend_enforces_ai_answers_are_dropdown_options():
    fields = [
        {
            "index": 0,
            "label": "Veteran Status",
            "options": [
                {"label": "I identify as one or more classifications of protected veteran", "value": "protected"},
                {"label": "I am not a protected veteran", "value": "not_protected"},
            ],
        },
        {
            "index": 1,
            "label": "Open text",
            "options": [],
        },
        {
            "index": 2,
            "label": "Country",
            "options": [
                {"label": "United States Minor Outlying Islands", "value": "UM"},
                {"label": "United States of America", "value": "US"},
            ],
        },
        {
            "index": 3,
            "label": "Sexual Orientation",
            "options": [
                {"label": "Straight", "value": "straight"},
                {"label": "I prefer not to answer", "value": "decline"},
            ],
        },
        {
            "index": 4,
            "label": "Country Phone Code",
            "options": [
                {"label": "United States (+1)", "value": "US"},
                {"label": "Canada (+1)", "value": "CA"},
            ],
        },
        {
            "index": 5,
            "label": "Will you now, or in the future, require sponsorship to work in the United States?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 6,
            "label": "Will you need relocation assistance to work at this role's specified location?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
    ]
    mappings = [
        {"index": 0, "value": "No", "confidence": 0.8, "source": "llm"},
        {"index": 1, "value": "Free text answer", "confidence": 0.8, "source": "llm"},
        {"index": 2, "value": "United States", "confidence": 0.8, "source": "llm"},
        {"index": 3, "value": "straight", "confidence": 0.8, "source": "llm"},
        {"index": 4, "value": "Canada (+1)", "confidence": 0.8, "source": "llm"},
        {"index": 5, "value": "I do not require sponsorship", "confidence": 0.8, "source": "llm"},
        {"index": 6, "value": "Open to relocation", "confidence": 0.8, "source": "llm"},
    ]

    filtered = server.enforce_option_values(mappings, fields)

    assert filtered == [
        {"index": 0, "value": "I am not a protected veteran", "confidence": 0.8, "source": "llm"},
        {"index": 1, "value": "Free text answer", "confidence": 0.8, "source": "llm"},
        {"index": 2, "value": "United States of America", "confidence": 0.8, "source": "llm"},
        {"index": 3, "value": "Straight", "confidence": 0.8, "source": "llm"},
        {"index": 4, "value": "Canada (+1)", "confidence": 0.8, "source": "llm"},
        {"index": 5, "value": "No", "confidence": 0.8, "source": "llm"},
        {"index": 6, "value": "No", "confidence": 0.8, "source": "llm"},
    ]


def test_backend_drops_non_option_wording_for_optioned_fields():
    fields = [
        {
            "index": 0,
            "label": "Do you have a disability?",
            "options": [
                {"label": "Yes, I have a disability, or have a history/record of having a disability"},
                {"label": "No, I don't have a disability, or a history/record of having a disability"},
                {"label": "I don't wish to answer"},
            ],
        },
    ]

    assert server.enforce_option_values(
        [{"index": 0, "value": "No, I do not have a disability and have not had one in the past", "confidence": 0.8, "source": "llm"}],
        fields,
    ) == []
    assert server.enforce_option_values(
        [{"index": 0, "value": "No, I don't have a disability, or a history/record of having a disability", "confidence": 0.8, "source": "llm"}],
        fields,
    ) == [
        {
            "index": 0,
            "value": "No, I don't have a disability, or a history/record of having a disability",
            "confidence": 0.8,
            "source": "llm",
        }
    ]


def test_backend_drops_non_option_education_dropdown_answers():
    fields = [
        {
            "index": 0,
            "label": "Degree",
            "options": [{"label": "Select..."}, {"label": "High School"}, {"label": "Associate Degree"}],
        },
        {
            "index": 1,
            "label": "Discipline",
            "options": [{"label": "Select..."}, {"label": "Computer Science"}, {"label": "Mathematics"}],
        },
    ]

    assert server.enforce_option_values(
        [
            {"index": 0, "value": "Bachelor of Science", "confidence": 0.8, "source": "llm"},
            {"index": 1, "value": "Statistics", "confidence": 0.8, "source": "llm"},
        ],
        fields,
    ) == []


def test_backend_merge_prefers_llm_mappings_over_policy_fallback():
    llm = [{"index": 0, "value": "3.9 out of 4", "confidence": 0.8, "source": "llm"}]
    policy = [
        {"index": 0, "value": "No", "confidence": 0.78, "source": "policy"},
        {"index": 1, "value": "Yes", "confidence": 0.78, "source": "policy"},
    ]

    merged = sorted(server.merge_backend_mappings(llm, policy), key=lambda mapping: mapping["index"])

    assert merged == [
        {"index": 0, "value": "3.9 out of 4", "confidence": 0.8, "source": "llm"},
        {"index": 1, "value": "Yes", "confidence": 0.78, "source": "policy"},
    ]


def test_backend_semantic_yes_no_handles_authorization_polarity():
    assert server.semantic_yes_no_value(server.normalize_for_option("I am authorized to work and do not require sponsorship")) == "yes"
    assert server.semantic_yes_no_value(server.normalize_for_option("Yes, I will require sponsorship")) == "yes"
    assert server.semantic_yes_no_value(server.normalize_for_option("No, I am authorized to work")) == "no"
    assert server.semantic_yes_no_value(server.normalize_for_option("I am not authorized to work in the United States")) == "no"
    assert server.semantic_yes_no_value(server.normalize_for_option("I do not require sponsorship")) == "no"


def test_backend_enforces_authorization_statement_to_yes_option():
    fields = [
        {
            "index": 0,
            "label": "Are you legally authorized to work in the United States without sponsorship?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        }
    ]

    assert server.enforce_option_values(
        [{"index": 0, "value": "I am authorized to work and do not require sponsorship", "confidence": 0.8, "source": "llm"}],
        fields,
    ) == [{"index": 0, "value": "Yes", "confidence": 0.8, "source": "llm"}]


def test_backend_deterministic_audit_ignores_mappings_without_matching_field():
    report = server.deterministic_audit_report(
        [],
        [{"index": 7, "value": "Yes", "source": "llm", "confidence": 0.8}],
        {},
    )

    assert report == {"corrections": [], "decisions": []}


def test_backend_prompt_includes_resume_transcript_for_unknown_questions():
    profile = {
        "workExperience": [{"company": "Example Labs", "title": "Machine Learning Engineer"}],
        "answers": {"relativesAtCompany": "No"},
        "resumeFacts": {
            "experience": ["Example Labs May 2025 - August 2025", "Built ML pipelines"],
            "projects": ["Personal AI agent project"],
            "skills": ["Python", "LLMs"],
        },
    }

    prompt = server.build_mapper_prompt(
        [{"index": 0, "label": "Have you worked for Acme?", "options": [{"label": "Yes"}, {"label": "No"}]}],
        profile,
        {},
    )

    assert "resumeTranscript" in prompt
    assert "candidateContext" in prompt
    assert "Machine Learning Engineer" in prompt
    assert "relativesAtCompany" in prompt
    assert "Example Labs May 2025 - August 2025" in prompt
    assert "Personal AI agent project" in prompt


def test_backend_prompt_requires_first_person_for_custom_answers():
    prompt = server.build_mapper_prompt(
        [{"index": 0, "tag": "textarea", "label": "Please describe your AI experience."}],
        {"firstName": "Aarya", "resumeFacts": {"projects": ["Built multi-agent AI systems"]}},
        {},
    )

    assert "first person" in prompt
    assert "I/my" in prompt
    assert "Never write narrative answers in third person" in prompt


def test_backend_retrieves_relevant_context_for_each_field():
    profile = {
        "workAuthorization": "Yes",
        "needsSponsorship": "No",
        "usPermanentResident": "Yes",
        "resumeFacts": {
            "experience": ["Example Labs May 2025 - August 2025"],
            "projects": ["Built a production AI agent"],
        },
    }
    field = {
        "index": 0,
        "label": "Will you require our assistance with work authorization now or in the future?",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }

    context = server.retrieved_context_for_field(field, profile, {"targetCountry": "usa"})

    assert context["category"] == "sponsorship"
    assert context["defaultPolicies"]["needsSponsorship"] == "No"
    assert context["profileFacts"]["usPermanentResident"] == "Yes"


def test_backend_deterministic_audit_corrects_wrong_policy_answers():
    fields = [
        {
            "index": 0,
            "label": "Will you require our assistance with work authorization now or in the future?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 1,
            "label": "Are you legally eligible to work in the country of employment?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
    ]
    mappings = [
        {"index": 0, "value": "Yes", "source": "autofill", "confidence": 0.9},
        {"index": 1, "value": "No", "source": "autofill", "confidence": 0.9},
    ]

    assert server.deterministic_audit_corrections(fields, mappings, {"workAuthorization": "Yes"}) == [
        {
            "index": 0,
            "value": "No",
            "confidence": 0.9,
            "source": "policy-audit",
            "reason": "Current answer conflicts with stored profile policy.",
        },
        {
            "index": 1,
            "value": "Yes",
            "confidence": 0.9,
            "source": "policy-audit",
            "reason": "Current answer conflicts with stored profile policy.",
        },
    ]


def test_backend_deterministic_audit_report_explains_keep_correct_and_skip():
    fields = [
        {
            "index": 0,
            "label": "Are you legally eligible to work in the country of employment?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {
            "index": 1,
            "label": "Will you require our assistance with work authorization now or in the future?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
        {"index": 2, "label": "Tell us why you want this role."},
    ]
    mappings = [
        {"index": 0, "value": "Yes", "source": "autofill", "confidence": 0.9},
        {"index": 1, "value": "Yes", "source": "autofill", "confidence": 0.9},
        {"index": 2, "value": "I like this role.", "source": "llm", "confidence": 0.8},
    ]

    report = server.deterministic_audit_report(fields, mappings, {"workAuthorization": "Yes"})

    assert report["corrections"] == [
        {
            "index": 1,
            "value": "No",
            "confidence": 0.9,
            "source": "policy-audit",
            "reason": "Current answer conflicts with stored profile policy.",
        }
    ]
    assert report["decisions"] == [
        {
            "index": 0,
            "action": "keep",
            "value": "Yes",
            "confidence": 0.9,
            "source": "deterministic-audit",
            "reason": "Current answer matches stored profile policy.",
            "evidence": "policy",
        },
        {
            "index": 1,
            "action": "correct",
            "value": "No",
            "confidence": 0.9,
            "source": "deterministic-audit",
            "reason": "Current answer conflicts with stored profile policy.",
            "evidence": "policy",
        },
        {
            "index": 2,
            "action": "skip",
            "value": "I like this role.",
            "confidence": 0.5,
            "source": "deterministic-audit",
            "reason": "No deterministic profile or policy fact was available for this field.",
            "evidence": "insufficientContext",
        },
    ]


def test_backend_normalizes_audit_decisions_to_visible_options():
    fields = [
        {
            "index": 0,
            "label": "Do you have a disability?",
            "options": [
                {"label": "Yes, I have a disability, or have a history/record of having a disability"},
                {"label": "No, I don't have a disability, or a history/record of having a disability"},
            ],
        }
    ]

    assert server.normalize_audit_decisions(
        [
            {
                "index": 0,
                "action": "correct",
                "value": "No, I do not have a disability and have not had one in the past",
                "confidence": 0.8,
                "reason": "Profile says no disability.",
                "evidence": "profile",
            }
        ],
        fields,
    ) == [
        {
            "index": 0,
            "action": "skip",
            "value": "",
            "confidence": 0.8,
            "source": "audit",
            "reason": "Suggested answer did not match any visible option.",
            "evidence": "profile",
        }
    ]


def test_backend_deterministic_audit_corrects_contact_and_location_mismatches():
    profile = {
        "email": "candidate@example.com",
        "phone": "555-0100",
        "linkedin": "https://linkedin.example/candidate",
        "portfolio": "https://portfolio.example",
        "website": "https://website.example",
        "usaLocation": "Chicago, IL",
        "answers": {"authorizedCountries": "Canada and United States"},
    }
    fields = [
        {"index": 0, "label": "LinkedIn URL *"},
        {"index": 1, "label": "Phone number *"},
        {"index": 2, "label": "Location *"},
        {"index": 3, "label": "In what country/countries are you legally permitted to work? *"},
    ]
    mappings = [
        {"index": 0, "value": "candidate@example.com", "source": "rule"},
        {"index": 1, "value": "candidate@example.com", "source": "rule"},
        {"index": 2, "value": "https://portfolio.example", "source": "rule"},
        {"index": 3, "value": "Yes", "source": "rule"},
    ]

    assert server.deterministic_audit_corrections(fields, mappings, profile) == [
        {
            "index": 0,
            "value": "https://linkedin.example/candidate",
            "confidence": 0.9,
            "source": "policy-audit",
            "reason": "Current answer conflicts with stored profile policy.",
        },
        {
            "index": 1,
            "value": "555-0100",
            "confidence": 0.9,
            "source": "policy-audit",
            "reason": "Current answer conflicts with stored profile policy.",
        },
        {
            "index": 2,
            "value": "Chicago, IL",
            "confidence": 0.9,
            "source": "policy-audit",
            "reason": "Current answer conflicts with stored profile policy.",
        },
        {
            "index": 3,
            "value": "Canada and United States",
            "confidence": 0.9,
            "source": "policy-audit",
            "reason": "Current answer conflicts with stored profile policy.",
        },
    ]


def test_backend_audit_endpoint_works_without_llm(monkeypatch, tmp_path):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "applications.sqlite3")
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)

    client = server.app.test_client()
    response = client.post(
        "/audit-fields",
        json={
            "fields": [
                {
                    "index": 0,
                    "label": "Do you now or will you in the future require visa sponsorship?",
                    "options": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
            "mappings": [{"index": 0, "value": "Yes", "source": "autofill"}],
            "profile": {},
            "page": {},
        },
    )

    assert response.status_code == 200
    assert response.json["corrections"] == [
        {
            "index": 0,
            "value": "No",
            "confidence": 0.9,
            "source": "policy-audit",
            "reason": "Current answer conflicts with stored profile policy.",
        }
    ]
    assert response.json["decisions"][0]["action"] == "correct"
    assert "deterministic audit" in response.json["warning"]


def test_backend_llm_trace_writer_and_endpoint(monkeypatch, tmp_path):
    trace_path = tmp_path / "llm_trace.private.jsonl"
    monkeypatch.setattr(server, "LLM_TRACE_PATH", trace_path)
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    monkeypatch.delenv("APPLYPILOT_LLM_TRACE", raising=False)

    server.write_llm_trace("mapper.request", {"traceId": "abc", "request": {"messages": [{"role": "user", "content": "prompt"}]}})
    server.write_llm_trace("mapper.response", {"traceId": "abc", "rawContent": '{"mappings":[]}'})

    assert trace_path.exists()
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "mapper.request"

    client = server.app.test_client()
    response = client.get("/llm-traces?limit=1")
    assert response.status_code == 200
    assert response.json["tracePath"] == str(trace_path)
    assert response.json["traces"][0]["event"] == "mapper.response"


def test_backend_tracks_applications(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "applications.sqlite3")
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)

    client = server.app.test_client()
    response = client.post(
        "/track-application",
        json={
            "url": "https://example.com/jobs/1",
            "title": "Example Role",
            "status": "filled",
            "filledCount": 2,
            "mappedCount": 3,
        },
    )

    assert response.status_code == 200
    assert response.json["tracked"] is True

    response = client.get("/applications")
    assert response.status_code == 200
    assert response.json["applications"][0]["url"] == "https://example.com/jobs/1"
    assert response.json["applications"][0]["filled_count"] == 2


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_previews_and_fills_sample_form():
    from playwright.sync_api import sync_playwright

    sample_path = ROOT / "autofill_extension/examples/sample_application.html"
    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "fullName": "Test Candidate",
        "email": "test@example.com",
        "phone": "5550100000",
        "linkedin": "https://linkedin.example/test",
        "github": "https://github.example/test",
        "portfolio": "https://portfolio.example",
        "location": "Test City, TS",
        "school": "Sample University",
        "degree": "Sample Degree",
        "graduationDate": "April 2026",
        "workAuthorization": "Yes",
        "needsSponsorship": "No",
        "canadianCitizen": "Yes",
        "usPermanentResident": "Yes",
        "subjectToAgreement": "No",
        "relocation": "Open to relocation",
        "salary": "Negotiable",
        "addresses": {
            "canada": {
                "line1": "123 Test St",
                "city": "Toronto",
                "province": "ON",
                "postalCode": "A1A1A1",
                "country": "Canada",
                "fullAddress": "123 Test St, Toronto, ON, A1A1A1",
            }
        },
        "answers": {},
        "relocation": "Open to relocation",
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "canada",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.add_init_script(
            f"""
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            """
        )
        page.goto(sample_path.as_uri())
        page.evaluate(
            """() => {
              const form = document.querySelector('form');
              const section = document.createElement('section');
              section.innerHTML = `
                <h2>Regression Fields</h2>
                <label>Legal Middle Name<input name="middleName"></label>
                <label>Second Last Name<input name="secondLastName"></label>
                <label>Address Line 2<input name="address_line_2"></label>
                <label>Country/Region *<input name="country_region" required></label>
                <label>Company Name<input name="companyName"></label>
                <label>If Yes Which Country Were You Last Assigned to?<input name="conditionalCountry"></label>
                <label>I agree to the Alternate Dispute Resolution statement above *<input type="checkbox" name="adrAgree" required></label>
                <label>Consent to cookies from provider LinkedIn<input name="linkedinCookieConsent"></label>
                <section>
                  <h2>Voluntary Self-Identification of Disability</h2>
                  <p>Form CC-305 OMB Control Number 1250-0005</p>
                  <label>Name*<input name="cc305Name"></label>
                  <label>Employee ID (if applicable)<input name="employeeId"></label>
                  <label>Date*<input name="cc305Date" placeholder="MM/DD/YYYY"></label>
                  <p>Please check one of the boxes below:</p>
                </section>
              `;
              form.prepend(section);
            }"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        assert preview["result"]["mapped"] >= 12

        selected = preview["result"]["mappings"]
        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            selected,
        )
        assert fill_response["ok"] is True, fill_response
        assert fill_response["result"]["filled"] >= 10
        assert page.locator("[name='firstName']").input_value() == "Test"
        assert page.locator("[name='cc305Name']").input_value() == "Test Candidate"
        assert page.locator("[name='cc305Date']").input_value().count("/") == 2
        assert page.locator("[name='employeeId']").input_value() == ""
        assert page.locator("[name='email_address']").input_value() == "test@example.com"
        assert page.locator("[name='address_line_1']").input_value() == "123 Test St"
        assert page.locator("[name='work_authorization']").input_value() == "Yes"
        assert page.locator("[name='middleName']").input_value() == ""
        assert page.locator("[name='secondLastName']").input_value() == ""
        assert page.locator("[name='address_line_2']").input_value() == ""
        assert page.locator("[name='country_region']").input_value() == "Canada"
        assert page.locator("[name='companyName']").input_value() == ""
        assert page.locator("[name='conditionalCountry']").input_value() == ""
        assert page.locator("[name='adrAgree']").is_checked() is True
        assert page.locator("[name='linkedinCookieConsent']").input_value() == ""

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_saved_answers_do_not_override_core_identity_fields():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "fullName": "Test Candidate",
        "email": "test@example.com",
        "phone": "5550100000",
        "answers": {
            "custom:first-name": "No",
            "first name": "No",
            "firstName": "No",
            "firstname": "No",
            "custom:last-name": "No",
            "last name": "No",
            "lastName": "No",
            "lastname": "No",
        },
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label>First name *<input name="firstName"></label>
              <label>Last name *<input name="lastName"></label>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )

        assert preview["ok"] is True
        mappings = {mapping["label"]: mapping for mapping in preview["result"]["mappings"]}
        assert mappings["First name *"]["value"] == "Test"
        assert mappings["First name *"]["source"] == "rule"
        assert mappings["Last name *"]["value"] == "Candidate"
        assert mappings["Last name *"]["source"] == "rule"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_answers_compliance_questions_and_uses_saved_question_memory():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "fullName": "Test Candidate",
        "email": "test@example.com",
        "phone": "5550100000",
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label>Are you currently, have you ever been, or has the government ever proposed that you be excluded, debarred, suspended or otherwise ineligible from participation in any federal or state health care program or other government procurement programs (e.g., Medicare, Medicaid)?*
                <select name="programExclusion"><option>Select One</option><option>Yes</option><option>No</option></select>
              </label>
              <label>Have you ever been employed by a federal, state or local government entity (e.g., Military, Civil Service, VA Hospital)?*
                <select name="governmentEmployment"><option>Select One</option><option>Yes</option><option>No</option></select>
              </label>
              <label>Have you ever had, or do you anticipate receiving, any disciplinary action taken on your professional license, certification, or credentials?*
                <select name="licenseDiscipline"><option>Select One</option><option>Yes</option><option>No</option></select>
              </label>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mappings_by_name = {mapping["name"]: mapping for mapping in preview["result"]["mappings"]}
        assert mappings_by_name["programExclusion"]["value"] == "No"
        assert mappings_by_name["governmentEmployment"]["value"] == "No"
        assert mappings_by_name["licenseDiscipline"]["value"] == "No"

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='programExclusion']").input_value() == "No"
        assert page.locator("[name='governmentEmployment']").input_value() == "No"
        assert page.locator("[name='licenseDiscipline']").input_value() == "No"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_saved_answers_can_fill_remembered_company_history_question():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "fullName": "Test Candidate",
        "email": "test@example.com",
        "phone": "5550100000",
        "answers": {
            "custom:have-you-ever-been-employed-by-exampleco": "Yes",
        },
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label>Have you ever been employed by ExampleCo?
                <select name="exampleCo" required><option>Select One</option><option>Yes</option><option>No</option></select>
              </label>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["name"] == "exampleCo")
        assert mapping["value"] == "Yes"
        assert mapping["source"] == "saved-answer"

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='exampleCo']").input_value() == "Yes"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_rejects_ai_free_text_for_hidden_gender_and_degree_dropdowns():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "degree": "Bachelor of Science",
        "answers": {},
        "demographics": {
            "genderIdentity": "Cisgender man",
            "gender": "Male",
        },
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": True,
        "autoMapAmbiguousFields": True,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label id="gender-label">Gender</label>
              <input id="gender" role="combobox" aria-labelledby="gender-label" aria-haspopup="listbox" value="Select...">
              <label id="degree-label">Degree</label>
              <input id="degree" role="combobox" aria-labelledby="degree-label" aria-haspopup="listbox" value="Select...">
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{
                    addListener: (fn) => {{ window.__autofillListener = fn; }}
                  }},
                  sendMessage: async () => ({{
                    ok: true,
                    payload: {{
                      mappings: [
                        {{ index: 0, value: "Cisgender man", confidence: 0.8, source: "llm" }},
                        {{ index: 1, value: "Bachelor of Science", confidence: 0.8, source: "llm" }}
                      ]
                    }}
                  }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        selected = {mapping["label"]: mapping["value"] for mapping in preview["result"]["mappings"]}
        assert "Gender" not in selected
        assert "Degree" not in selected

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("#gender").input_value() == "Select..."
        assert page.locator("#degree").input_value() == "Select..."

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_uses_usa_target_country_for_stripe_style_fields():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Sample",
        "lastName": "Candidate",
        "email": "sample@example.com",
        "phone": "5550100000",
        "workAuthorization": "Yes",
        "needsSponsorship": "No",
        "school": "Sample University",
        "addresses": {
            "canada": {
                "city": "Toronto",
                "province": "ON",
                "postalCode": "A1A1A1",
                "country": "Canada",
                "line1": "123 Maple St",
            },
            "usa": {
                "city": "Chicago",
                "state": "IL",
                "zipCode": "60601",
                "country": "United States",
                "line1": "456 Lake St",
            },
        },
        "answers": {"gpa": "3.7 out of 4"},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.add_init_script(
            f"""
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            """
        )
        page.set_content(
            """
            <form>
              <label>Country*<input name="country"></label>
              <label>Location (City)*<input name="locationCity"></label>
              <label>Please select the country where you currently reside. *<input name="currentlyReside"></label>
              <label>Are you authorized to work in the location(s) you selected in your previous response?*<input name="authorized"></label>
              <label>Will you require ExampleCo to sponsor you for a work permit now or in the future for the location(s) you selected in your previous response? *<input name="sponsorship"></label>
              <label>If this role offers the option to work from a remote location, do you plan to work remotely?*<input name="remote"></label>
              <label>What is the most recent school you attended?<input name="school"></label>
              <label>Overall Result (GPA)*<input name="gpa"></label>
              <label>If relocation is required for this opportunity, and relocation assistance is not offered for this position, are you willing to relocate at your own cost?*<input name="relocateOwnCost"></label>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='country']").input_value() == "United States"
        assert page.locator("[name='locationCity']").input_value() == "Chicago"
        assert page.locator("[name='currentlyReside']").input_value() == "United States"
        assert page.locator("[name='authorized']").input_value() == "Yes"
        assert page.locator("[name='sponsorship']").input_value() == "No"
        assert page.locator("[name='remote']").input_value() == ""
        assert page.locator("[name='school']").input_value() == "Sample University"
        assert page.locator("[name='gpa']").input_value() == "3.7 out of 4"
        assert page.locator("[name='relocateOwnCost']").input_value() == "Yes"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_disambiguates_location_typeahead_with_saved_usa_location():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "addresses": {
            "usa": {
                "city": "Bartlett",
                "state": "IL",
                "zipCode": "60103",
                "country": "United States",
            }
        },
        "answers": {
            "usaLocation": "Chicago, IL",
            "usaCity": "Chicago",
        },
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.route("https://job-boards.greenhouse.io/**", lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="""
            <form>
              <label>Location (City)*<input name="locationCity" aria-autocomplete="list" placeholder="Start typing..."></label>
            </form>
            """
        ))
        page.goto("https://job-boards.greenhouse.io/example/jobs/1")
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )

        assert preview["ok"] is True
        mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Location (City)*")
        assert mapping["value"] == "Chicago, IL"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_fills_greenhouse_location_typeahead_with_chicago_not_bartlett():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "addresses": {
            "usa": {
                "city": "Bartlett",
                "state": "IL",
                "zipCode": "60103",
                "country": "United States",
            }
        },
        "answers": {
            "usaLocation": "Chicago, IL",
            "usaCity": "Chicago",
        },
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.route("https://job-boards.greenhouse.io/**", lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="""
            <form>
              <label>
                Location (City)*
                <input name="locationCity" aria-autocomplete="list" aria-controls="location-options" value="Bartlett, Tennessee, United States" placeholder="Start typing...">
              </label>
              <div id="location-options" role="listbox" hidden>
                <div role="option" data-value="Bartlett, Tennessee, United States">Bartlett, Tennessee, United States</div>
                <div role="option" data-value="Chicago, Illinois, United States">Chicago, Illinois, United States</div>
              </div>
            </form>
            <script>
              const input = document.querySelector('[name="locationCity"]');
              const options = document.querySelector('#location-options');
              input.addEventListener('click', () => { options.hidden = false; });
              input.addEventListener('input', () => { options.hidden = false; });
              for (const option of options.querySelectorAll('[role="option"]')) {
                option.addEventListener('click', () => {
                  input.value = option.getAttribute('data-value');
                  input.setAttribute('data-selected', option.getAttribute('data-value'));
                  options.hidden = true;
                });
              }
            </script>
            """
        ))
        page.goto("https://job-boards.greenhouse.io/example/jobs/1")
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Location (City)*")
        assert mapping["value"] == "Chicago, Illinois, United States"

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='locationCity']").input_value() == "Chicago, Illinois, United States"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_does_not_cross_fill_gem_contact_fields():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Sample",
        "lastName": "Candidate",
        "email": "candidate@example.com",
        "phone": "555-0100",
        "linkedin": "https://linkedin.example/candidate",
        "portfolio": "https://portfolio.example",
        "usaLocation": "Chicago, IL",
        "answers": {"authorizedCountries": "Canada and United States"},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form class="gem-style">
              <div class="field"><div>First name *</div><input name="first_name"></div>
              <div class="field"><div>Last name *</div><input name="last_name"></div>
              <div class="field"><div>Email *</div><input name="email"></div>
              <div class="field"><div>LinkedIn URL *</div><input name="linkedin"></div>
              <div class="field"><div>Phone number *</div><input name="phone"></div>
              <div class="field"><div>Location *</div><input name="location"></div>
              <div class="field"><div>In what country/countries are you legally permitted to work? *</div><input name="authorizedCountries"></div>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )

        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='email']").input_value() == "candidate@example.com"
        assert page.locator("[name='linkedin']").input_value() == "https://linkedin.example/candidate"
        assert page.locator("[name='phone']").input_value() == "555-0100"
        assert page.locator("[name='location']").input_value() == "Chicago, IL"
        assert page.locator("[name='authorizedCountries']").input_value() == "Canada and United States"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_fills_workday_country_dropdown_with_target_country_option():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "phone": "5550100000",
        "addresses": {
            "usa": {
                "line1": "456 Lake St",
                "city": "Chicago",
                "state": "IL",
                "zipCode": "60601",
                "country": "United States",
            },
            "canada": {
                "country": "Canada",
            },
        },
        "answers": {
            "previouslyEmployedByCompany": "No",
        },
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <nav>
              <button type="button">English</button>
              <button type="button">Settings</button>
            </nav>
            <form>
              <div class="wd-field">
                <div>Do you now or have you previously worked for Example Company, Inc. or any of its subsidiaries?*</div>
                <label><input type="radio" name="workedBefore" value="Yes">Yes</label>
                <label><input type="radio" name="workedBefore" value="No">No</label>
              </div>
              <div class="wd-field">
                <label id="country-label">Country / Territory*</label>
                <button id="country" type="button" aria-labelledby="country-label" aria-haspopup="listbox" aria-controls="country-options">Canada</button>
                <div id="country-options" role="listbox" hidden>
                  <div data-automation-id="promptOption" data-automation-label="Canada">Canada</div>
                  <div data-automation-id="promptOption" data-automation-label="United States Minor Outlying Islands">United States Minor Outlying Islands</div>
                  <div data-automation-id="promptOption" data-automation-label="United States of America">United States of America</div>
                </div>
              </div>
              <label>Address Line 1*<input name="addressLine1"></label>
              <label>City*<input name="city"></label>
              <div class="wd-field">
                <label id="state-label">State*</label>
                <button id="state" type="button" aria-labelledby="state-label" aria-haspopup="listbox" aria-controls="state-options">Select One</button>
                <div id="state-options" role="listbox" hidden>
                  <div data-automation-id="promptOption" data-automation-label="California">California</div>
                  <div data-automation-id="promptOption" data-automation-label="Illinois">Illinois</div>
                </div>
              </div>
              <label>Postal Code*<input name="postalCode"></label>
              <div class="wd-field">
                <label id="phone-device-label">Phone Device Type*</label>
                <button id="phone-device" type="button" aria-labelledby="phone-device-label" aria-haspopup="listbox" aria-controls="phone-device-options">Select One</button>
                <div id="phone-device-options" role="listbox" hidden>
                  <div data-automation-id="promptOption" data-automation-label="Home">Home</div>
                  <div data-automation-id="promptOption" data-automation-label="Mobile">Mobile</div>
                </div>
              </div>
              <div class="wd-field">
                <label id="source-label">How Did You Hear About Us?*</label>
                <button id="source" type="button" aria-labelledby="source-label" aria-haspopup="listbox" aria-controls="source-options">0 items selected</button>
                <div id="source-options" role="listbox" hidden>
                  <div data-automation-id="promptOption" data-automation-label="Company Website">Company Website</div>
                  <div data-automation-id="promptOption" data-automation-label="LinkedIn">LinkedIn</div>
                  <div data-automation-id="promptOption" data-automation-label="Indeed">Indeed</div>
                </div>
              </div>
              <div class="wd-field">
                <label id="phone-code-label">Country Phone Code*</label>
                <button id="phone-code" type="button" aria-labelledby="phone-code-label" aria-haspopup="listbox" aria-controls="phone-code-options">Canada (+1)</button>
                <div id="phone-code-options" role="listbox" hidden>
                  <div data-automation-id="promptOption" data-automation-label="Canada (+1)">Canada (+1)</div>
                  <div data-automation-id="promptOption" data-automation-label="United States (+1)">United States (+1)</div>
                </div>
              </div>
              <label>Phone Extension<input name="phoneExtension"></label>
              <label>kpaj6<input name="kpaj6"></label>
            </form>
            <script>
              for (const button of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
                const list = document.getElementById(button.getAttribute('aria-controls'));
                button.addEventListener('click', () => { list.hidden = !list.hidden; });
                button.addEventListener('keydown', (event) => {
                  if (event.key === 'Escape') list.hidden = true;
                });
                list.querySelectorAll('[data-automation-id="promptOption"]').forEach((option) => {
                  option.addEventListener('click', () => {
                    button.textContent = option.getAttribute('data-automation-label');
                    button.setAttribute('data-selected', option.getAttribute('data-automation-label'));
                    list.hidden = true;
                    if (button.id === 'country') {
                      document.querySelector('[name="addressLine1"]').value = '';
                      document.querySelector('[name="city"]').value = '';
                      document.querySelector('[name="postalCode"]').value = '';
                      document.querySelector('#state').textContent = 'Select One';
                      document.querySelector('#state').removeAttribute('data-selected');
                    }
                  });
                });
              }
            </script>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        country_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Country / Territory*")
        address_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Address Line 1*")
        city_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "City*")
        state_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "State*")
        postal_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Postal Code*")
        phone_device_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Phone Device Type*")
        source_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "How Did You Hear About Us?*")
        phone_code_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Country Phone Code*")
        phone_extension_mapping = [mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Phone Extension"]
        unmapped_labels = [field["label"] for field in preview["result"]["unmappedFields"]]
        assert country_mapping["value"] == "United States of America"
        assert address_mapping["value"] == "456 Lake St"
        assert city_mapping["value"] == "Chicago"
        assert state_mapping["value"] == "Illinois"
        assert postal_mapping["value"] == "60601"
        assert phone_device_mapping["value"] == "Mobile"
        assert source_mapping["value"] == "LinkedIn"
        assert phone_code_mapping["value"] == "Canada (+1)"
        assert phone_extension_mapping == []
        assert any("previously worked" in label for label in unmapped_labels)
        assert "English" not in unmapped_labels
        assert "Settings" not in unmapped_labels
        assert "Phone Extension" not in unmapped_labels

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert not page.locator("[name='workedBefore'][value='No']").is_checked()
        assert page.locator("#country").get_attribute("data-selected") == "United States of America"
        assert page.locator("[name='addressLine1']").input_value() == "456 Lake St"
        assert page.locator("[name='city']").input_value() == "Chicago"
        assert page.locator("#state").get_attribute("data-selected") == "Illinois"
        assert page.locator("[name='postalCode']").input_value() == "60601"
        assert page.locator("#phone-device").get_attribute("data-selected") == "Mobile"
        assert page.locator("#source").get_attribute("data-selected") == "LinkedIn"
        assert page.locator("#phone-code").get_attribute("data-selected") == "Canada (+1)"
        assert page.locator("[name='phoneExtension']").input_value() == ""

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_surfaces_unknown_questions_uploads_and_saved_answers():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Sample",
        "lastName": "Candidate",
        "email": "sample@example.com",
        "phone": "5550100000",
        "currentOrPreviousEmployer": "Example Labs",
        "currentOrPreviousJobTitle": "Software Engineer",
        "veteranStatus": "No",
        "resumeFileName": "resume.private.pdf",
        "answers": {
            "custom:do-you-have-experience-with-kubernetes": "Yes",
            "previouslyEmployedByCompany": "No",
            "recruitingMessages": "No",
        },
        "addresses": {},
        "demographics": {
            "race": "Asian",
            "hispanicLatino": "No",
            "gender": "Male",
        },
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": True,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label>Do you have experience with Kubernetes?<input name="k8s" required></label>
              <label>Who is your current or previous employer?<input name="employer" required></label>
              <label>What is your current or previous job title?<input name="jobTitle" required></label>
              <label>Have you ever been employed by ExampleCo or an ExampleCo affiliate?<select name="companyAffiliate" required><option></option><option>Yes</option><option>No</option></select></label>
              <label>Do you opt-in to receive WhatsApp messages from ExampleCo Recruiting?<select name="whatsapp" required><option></option><option>Yes</option><option>No</option></select></label>
              <label>Gender<select name="gender" required><option></option><option>Female</option><option>Male</option><option>I do not wish to answer</option></select></label>
              <label>Are you Hispanic/Latino?<select name="hispanic" required><option></option><option>Yes, I am Hispanic or Latino</option><option>No, I am not Hispanic or Latino</option><option>I do not wish to answer</option></select></label>
              <label>Race<select name="race" required><option></option><option>Black or African American</option><option>Asian (Not Hispanic or Latino)</option><option>I do not wish to answer</option></select></label>
              <label>Are you a protected veteran?<select name="veteran" required><option></option><option>I identify as one or more of the classifications of a protected veteran</option><option>I am not a protected Veteran</option><option>I do not wish to answer</option></select></label>
              <label>Upload Resume<input type="file" name="resume"></label>
              <label>What is your favorite database?<input name="database" required></label>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mappings = preview["result"]["mappings"]
        k8s_mapping = next(mapping for mapping in mappings if "Kubernetes" in mapping["label"])
        employer_mapping = next(mapping for mapping in mappings if "current or previous employer" in mapping["label"])
        title_mapping = next(mapping for mapping in mappings if "current or previous job title" in mapping["label"])
        affiliate_mapping = next(mapping for mapping in mappings if "ExampleCo affiliate" in mapping["label"])
        whatsapp_mapping = next(mapping for mapping in mappings if "WhatsApp messages" in mapping["label"])
        gender_mapping = next(mapping for mapping in mappings if mapping["label"] == "Gender")
        hispanic_mapping = next(mapping for mapping in mappings if "Hispanic/Latino" in mapping["label"])
        race_mapping = next(mapping for mapping in mappings if mapping["label"] == "Race")
        veteran_mapping = next(mapping for mapping in mappings if "protected veteran" in mapping["label"])
        assert k8s_mapping["value"] == "Yes"
        assert employer_mapping["value"] == "Example Labs"
        assert title_mapping["value"] == "Software Engineer"
        assert affiliate_mapping["value"] == "No"
        assert whatsapp_mapping["value"] == "No"
        assert gender_mapping["value"] == "Male"
        assert hispanic_mapping["value"] == "No, I am not Hispanic or Latino"
        assert race_mapping["value"] == "Asian (Not Hispanic or Latino)"
        assert veteran_mapping["value"] == "I am not a protected Veteran"
        assert preview["result"]["manualTasks"][0]["resumeFileName"] == "resume.private.pdf"

        unknown_labels = [field["label"] for field in preview["result"]["unmappedFields"]]
        assert "What is your favorite database?" in unknown_labels

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='k8s']").input_value() == "Yes"
        assert page.locator("[name='employer']").input_value() == "Example Labs"
        assert page.locator("[name='jobTitle']").input_value() == "Software Engineer"
        assert page.locator("[name='companyAffiliate']").input_value() == "No"
        assert page.locator("[name='whatsapp']").input_value() == "No"
        assert page.locator("[name='gender']").input_value() == "Male"
        assert page.locator("[name='hispanic']").input_value() == "No, I am not Hispanic or Latino"
        assert page.locator("[name='race']").input_value() == "Asian (Not Hispanic or Latino)"
        assert page.locator("[name='veteran']").input_value() == "I am not a protected Veteran"
        assert page.locator("[name='database']").input_value() == ""

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_groups_ashby_style_choice_questions():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Sample",
        "lastName": "Candidate",
        "fullName": "Sample Candidate",
        "email": "sample@example.com",
        "phone": "5550100000",
        "linkedin": "https://linkedin.example/sample",
        "workAuthorization": "Yes",
        "needsSponsorship": "No",
        "veteranStatus": "No",
        "answers": {
            "pronouns": "He/Him",
            "withinListedOfficeRadius": "No",
        },
        "resumeFacts": {
            "projects": ["Built an inference optimization project for AI serving"],
            "skills": ["Python", "PyTorch", "AWS"],
        },
        "addresses": {
            "usa": {
                "city": "Chicago",
                "state": "IL",
                "zipCode": "60601",
                "country": "United States",
            }
        },
        "demographics": {
            "race": "Asian",
            "hispanicLatino": "No",
            "gender": "Male",
            "genderIdentity": "Man",
        },
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": True,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label>Name<input name="name" placeholder="Please enter your first and last name"></label>
              <label>What AI projects have you built in your spare time outside of work?<textarea name="aiProjects" required></textarea></label>
              <fieldset>
                <legend>Are you located within ~50 miles of Seattle, WA; Boston, MA; Washington DC; or Austin, TX?</legend>
                <label><input type="radio" name="nearOffice" value="Yes" required>Yes</label>
                <label><input type="radio" name="nearOffice" value="No">No</label>
              </fieldset>
              <label>Location <input name="location" placeholder="REQUIRED: Enter your city, region/state, and country"></label>
              <label>Zip Code / Postal Code<input name="zip"></label>
              <fieldset>
                <legend>Pronouns</legend>
                <label><input type="radio" name="pronouns" value="He/Him" required>He/Him</label>
                <label><input type="radio" name="pronouns" value="She/Her">She/Her</label>
                <label><input type="radio" name="pronouns" value="They/Them">They/Them</label>
              </fieldset>
              <fieldset>
                <legend>Can you provide proof of authorization to work in the country for which job you are applying for?</legend>
                <label><input type="radio" name="authorized" value="Yes" required>Yes</label>
                <label><input type="radio" name="authorized" value="No">No</label>
              </fieldset>
              <fieldset>
                <legend>Will you now or in the future require employer sponsorship to work in the country for which job you are applying for?</legend>
                <label><input type="radio" name="sponsor" value="Yes" required>Yes</label>
                <label><input type="radio" name="sponsor" value="No">No</label>
              </fieldset>
              <fieldset>
                <legend>What is your current age?</legend>
                <label><input type="radio" name="age" value="Under 30" required>Under 30</label>
                <label><input type="radio" name="age" value="I prefer not to answer">I prefer not to answer</label>
              </fieldset>
              <fieldset>
                <legend>What is your gender identity?</legend>
                <label><input type="radio" name="genderIdentity" value="Man" required>Man</label>
                <label><input type="radio" name="genderIdentity" value="Woman">Woman</label>
                <label><input type="radio" name="genderIdentity" value="Another Gender Identity">Another Gender Identity</label>
              </fieldset>
              <fieldset>
                <legend>Race</legend>
                <label><input type="radio" name="race" value="Hispanic or Latino" required>Hispanic or Latino</label>
                <label><input type="radio" name="race" value="White (Not Hispanic or Latino)">White (Not Hispanic or Latino)</label>
                <label><input type="radio" name="race" value="Asian (Not Hispanic or Latino)">Asian (Not Hispanic or Latino)</label>
                <label><input type="radio" name="race" value="Decline to self-identify">Decline to self-identify</label>
              </fieldset>
              <fieldset>
                <legend>Veteran Status</legend>
                <label><input type="radio" name="veteran" value="I identify as one or more of the classifications of protected veteran listed above" required>I identify as one or more of the classifications of protected veteran listed above</label>
                <label><input type="radio" name="veteran" value="I am not a protected veteran">I am not a protected veteran</label>
                <label><input type="radio" name="veteran" value="I decline to self-identify for protected veteran status">I decline to self-identify for protected veteran status</label>
              </fieldset>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        labels = [mapping["label"] for mapping in preview["result"]["mappings"]]
        assert labels.count("Race") == 1
        assert not any(label == "White (Not Hispanic or Latino)" for label in labels)
        assert any(mapping["label"] == "Name" and mapping["value"] == "Sample Candidate" for mapping in preview["result"]["mappings"])
        assert any("AI projects" in mapping["label"] and "inference optimization" in mapping["value"] for mapping in preview["result"]["mappings"])
        assert any(mapping["label"] == "Race" and mapping["value"] == "Asian (Not Hispanic or Latino)" for mapping in preview["result"]["mappings"])
        assert any(mapping["label"] == "Veteran Status" and mapping["value"] == "I am not a protected veteran" for mapping in preview["result"]["mappings"])
        assert any(mapping["label"] == "What is your current age?" and mapping["value"] == "I prefer not to answer" for mapping in preview["result"]["mappings"])

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='name']").input_value() == "Sample Candidate"
        assert page.locator("[name='nearOffice'][value='No']").is_checked()
        assert page.locator("[name='pronouns'][value='He/Him']").is_checked()
        assert page.locator("[name='authorized'][value='Yes']").is_checked()
        assert page.locator("[name='sponsor'][value='No']").is_checked()
        assert page.locator("[name='age'][value='I prefer not to answer']").is_checked()
        assert page.locator("[name='genderIdentity'][value='Man']").is_checked()
        assert page.locator("[name='race'][value='Asian (Not Hispanic or Latino)']").is_checked()
        assert page.locator("[name='veteran'][value='I am not a protected veteran']").is_checked()

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_keeps_ashby_profile_links_and_location_separate():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "fullName": "Aarya Shah",
        "email": "a268shah@uwaterloo.ca",
        "phone": "647-767-8243",
        "linkedin": "https://www.linkedin.com/in/AaryaShah127",
        "github": "https://github.com/aarya127",
        "school": "University of Waterloo",
        "graduationDate": "April 2026",
        "answers": {
            "usaLocation": "Chicago, IL",
        },
        "addresses": {
            "usa": {
                "city": "Chicago",
                "state": "IL",
                "zipCode": "60601",
                "country": "United States",
            }
        },
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <div class="ashby-section">
                <div class="field-row"><div>LinkedIn Profile</div><input name="linkedin" placeholder="Type here..." aria-required="true"></div>
                <div class="field-row"><div>Current Location</div><input name="currentLocation" placeholder="Start typing..." aria-required="true"></div>
                <div class="field-row"><div>Github Link</div><input name="github" placeholder="Type here..." aria-required="true"></div>
                <div class="field-row"><div>School</div><input name="school" aria-required="true"></div>
                <div class="field-row"><div>Graduation Date</div><input name="graduationDate" aria-required="true"></div>
              </div>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mappings_by_name = {mapping["name"]: mapping for mapping in preview["result"]["mappings"]}
        assert mappings_by_name["linkedin"]["value"] == "https://www.linkedin.com/in/AaryaShah127"
        assert mappings_by_name["currentLocation"]["value"] == "Chicago, IL"
        assert mappings_by_name["github"]["value"] == "https://github.com/aarya127"
        assert mappings_by_name["graduationDate"]["value"] == "April 2026"

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='linkedin']").input_value() == "https://www.linkedin.com/in/AaryaShah127"
        assert page.locator("[name='currentLocation']").input_value() == "Chicago, IL"
        assert page.locator("[name='github']").input_value() == "https://github.com/aarya127"
        assert page.locator("[name='graduationDate']").input_value() == "April 2026"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_expands_and_fills_greenhouse_employment_history():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Sample",
        "lastName": "Candidate",
        "fullName": "Sample Candidate",
        "addresses": {
            "usa": {
                "city": "Sample City",
                "state": "ST",
                "zipCode": "60601",
                "country": "United States",
            }
        },
        "workExperience": [
            {
                "company": "Example Labs",
                "title": "Machine Learning Engineer",
                "location": "Toronto, ON",
                "description": "Built production ML services\nImproved model quality",
                "startMonth": "September",
                "startYear": "2025",
                "endMonth": "December",
                "endYear": "2025",
                "currentRole": False,
            },
            {
                "company": "Research Group",
                "title": "Research Assistant",
                "location": "Waterloo, ON",
                "description": "Published efficient transformer research",
                "startMonth": "January",
                "startYear": "2025",
                "endMonth": "April",
                "endYear": "2025",
                "currentRole": False,
            },
        ],
        "currentOrPreviousJobTitle": "Staff Data Engineer",
        "answers": {
            "custom:may-we-contact-your-current-employer": "Old Bad Answer",
            "contactCurrentEmployer": "No",
        },
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label>Zip / postal code<input name="zip" required></label>
              <label>May we contact your current employer?<select name="contactEmployer" required><option></option><option>Yes</option><option>No</option></select></label>
              <label>This position is based in the United States. Do you currently reside in commutable proximity to an ExampleCo office located in San Francisco or are you open to relocating?<input name="commutable" required></label>
              <label>What is your current or previous job title?<input name="jobTitle" required></label>
              <section id="employment">
                <h2>Employment</h2>
                <div class="employment-row">
                  <label>Company<input name="company[]" required></label>
                  <label>Job Title<input name="title[]" required></label>
                  <label>Location<input name="location[]" required></label>
                  <label>From<input name="from[]" required></label>
                  <label>To<input name="to[]" required></label>
                  <label>Role Description<textarea name="description[]" required></textarea></label>
                  <label>I currently work here<input type="checkbox" name="current[]" required></label>
                </div>
                <button id="addEmployment" type="button">Add another</button>
              </section>
            </form>
            <script>
              document.getElementById('addEmployment').addEventListener('click', () => {
                const row = document.querySelector('.employment-row').cloneNode(true);
                row.querySelectorAll('input').forEach((input) => {
                  input.value = '';
                  input.checked = false;
                });
                row.querySelectorAll('select').forEach((select) => { select.value = ''; });
                document.getElementById('employment').insertBefore(row, document.getElementById('addEmployment'));
              });
            </script>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        assert page.locator(".employment-row").count() == 2
        employment_row_mappings = [
            mapping for mapping in preview["result"]["mappings"]
            if mapping["name"] in {"company[]", "title[]", "location[]", "from[]", "to[]", "description[]"}
        ]
        assert employment_row_mappings
        assert all(mapping["source"] == "experience" for mapping in employment_row_mappings)
        assert not any(
            mapping["source"] == "field-kind" and mapping["name"] in {"company[]", "title[]", "location[]", "from[]", "to[]", "description[]"}
            for mapping in preview["result"]["mappings"]
        )

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='zip']").input_value() == "60601"
        assert page.locator("[name='contactEmployer']").input_value() == "No"
        assert page.locator("[name='commutable']").input_value() == "Open to relocation"
        assert page.locator("[name='jobTitle']").input_value() == "Staff Data Engineer"
        assert page.locator("[name='company[]']").nth(0).input_value() == "Example Labs"
        assert page.locator("[name='title[]']").nth(0).input_value() == "Machine Learning Engineer"
        assert page.locator("[name='location[]']").nth(0).input_value() == "Toronto, ON"
        assert page.locator("[name='from[]']").nth(0).input_value() == "9/2025"
        assert page.locator("[name='to[]']").nth(0).input_value() == "12/2025"
        assert page.locator("[name='description[]']").nth(0).input_value() == "Built production ML services\nImproved model quality"
        assert page.locator("[name='company[]']").nth(1).input_value() == "Research Group"
        assert page.locator("[name='location[]']").nth(1).input_value() == "Waterloo, ON"
        assert page.locator("[name='to[]']").nth(1).input_value() == "4/2025"
        assert page.locator("[name='description[]']").nth(1).input_value() == "Published efficient transformer research"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_does_not_field_kind_map_generic_workday_experience_labels_or_refill_correct_values():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "currentOrPreviousEmployer": "Cognixion",
        "currentOrPreviousJobTitle": "Machine Learning Software Engineer",
        "addresses": {
            "usa": {
                "city": "Bartlett",
                "state": "IL",
                "zipCode": "60103",
                "country": "United States",
            }
        },
        "linkedin": "https://www.linkedin.com/in/example",
        "portfolio": "https://portfolio.example",
        "github": "https://github.com/example",
        "education": [
            {
                "school": "University of Waterloo",
                "degree": "Bachelor's Degree",
                "fieldOfStudy": "Statistics",
                "startYear": "2021",
                "endYear": "2026",
            }
        ],
        "workExperience": [
            {
                "company": "Cognixion",
                "title": "Machine Learning Software Engineer",
                "location": "Santa Barbara, CA",
                "description": "Built production ML systems",
                "startMonth": "September",
                "startYear": "2025",
                "endMonth": "December",
                "endYear": "2025",
                "currentRole": False,
            }
        ],
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label>Job Title*<input name="genericTitle"></label>
              <label>Company*<input name="genericCompany"></label>
              <label>Location<input name="genericLocation"></label>
              <label>Location Month<input name="genericMonth"></label>
              <label>Location Year<input name="genericYear"></label>
              <label>Role Description<textarea name="genericDescription"></textarea></label>

              <section id="employment">
                <h2>Work Experience</h2>
                <label>Company<input name="company[]" value="Cognixion"></label>
                <label>Job Title<input name="title[]" value="Machine Learning Software Engineer"></label>
                <label>Location<input name="location[]" value="Santa Barbara, CA"></label>
                <label>From<input name="from[]" value="9/2025"></label>
                <label>To<input name="to[]" value="12/2025"></label>
                <label>Role Description<textarea name="description[]">Built production ML systems</textarea></label>
              </section>
              <section id="education">
                <h2>Education</h2>
                <button id="addEducation" type="button">Add</button>
              </section>
              <section id="websites">
                <h2>Websites</h2>
                <label>URL*<input name="url[]"></label>
                <label>URL*<input name="url[]"></label>
                <label>URL*<input name="url[]"></label>
              </section>
            </form>
            <script>
              document.getElementById('addEducation').addEventListener('click', () => {
                const section = document.getElementById('education');
                const row = document.createElement('div');
                row.className = 'education-row';
                row.innerHTML = `
                  <label>School<input name="school[]"></label>
                  <label>Degree<input name="degree[]"></label>
                  <label>Discipline<input name="discipline[]"></label>
                `;
                section.insertBefore(row, document.getElementById('addEducation'));
              });
            </script>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mappings = preview["result"]["mappings"]
        generic_names = {"genericTitle", "genericCompany", "genericLocation", "genericMonth", "genericYear", "genericDescription"}
        repeatable_names = {"company[]", "title[]", "location[]", "from[]", "to[]", "description[]"}
        url_mappings = [mapping for mapping in mappings if mapping["name"] == "url[]"]

        assert not any(mapping["source"] == "field-kind" and mapping["name"] in generic_names for mapping in mappings)
        assert not any(mapping["name"] in {"genericMonth", "genericYear", "genericDescription"} for mapping in mappings), json.dumps(mappings, indent=2)
        assert not any(mapping["name"] == "genericLocation" and mapping["value"] == "Bartlett" for mapping in mappings), json.dumps(mappings, indent=2)
        assert not any(mapping["name"] in repeatable_names for mapping in mappings)
        assert page.locator(".education-row").count() == 1
        assert url_mappings
        assert all(mapping["value"] != "Cognixion" for mapping in url_mappings)
        assert set(mapping["value"] for mapping in url_mappings) == {
            "https://www.linkedin.com/in/example",
            "https://portfolio.example",
            "https://github.com/example",
        }

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_maps_workday_experience_locations_from_resume_not_home_address():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "addresses": {
            "usa": {
                "city": "Bartlett",
                "state": "IL",
                "zipCode": "60103",
                "country": "United States",
            }
        },
        "workExperience": [
            {
                "company": "Cognixion",
                "title": "Machine Learning Software Engineer",
                "location": "Santa Barbara, CA",
                "startMonth": "September",
                "startYear": "2025",
                "endMonth": "December",
                "endYear": "2025",
                "description": "Built production ML systems",
            },
            {
                "company": "University of Waterloo",
                "title": "AI/ML Research Assistant",
                "location": "Waterloo, ON",
                "startMonth": "August",
                "startYear": "2025",
                "endMonth": "December",
                "endYear": "2025",
                "description": "Published efficient transformer research",
            },
        ],
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <h2>My Experience</h2>
              <section>
                <h3>Work Experience 1</h3>
                <label>Job Title*<input name="title1"></label>
                <label>Company*<input name="company1"></label>
                <label>Location*<input name="location1"></label>
                <label>Location Month<input name="startMonth1"></label>
                <label>Location Year<input name="startYear1"></label>
                <label>Role Description<textarea name="description1"></textarea></label>
              </section>
              <section>
                <h3>Work Experience 2</h3>
                <label>Job Title*<input name="title2"></label>
                <label>Company*<input name="company2"></label>
                <label>Location*<input name="location2"></label>
                <label>Location Month<input name="startMonth2"></label>
                <label>Location Year<input name="startYear2"></label>
                <label>Role Description<textarea name="description2"></textarea></label>
              </section>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )

        assert preview["ok"] is True
        mappings_by_name = {mapping["name"]: mapping for mapping in preview["result"]["mappings"]}
        assert mappings_by_name["location1"]["value"] == "Santa Barbara, CA"
        assert mappings_by_name["location2"]["value"] == "Waterloo, ON"
        assert mappings_by_name["location1"]["source"] == "experience"
        assert not any(mapping["value"] == "Bartlett" for mapping in preview["result"]["mappings"])
        assert "startMonth1" not in mappings_by_name
        assert "startYear1" not in mappings_by_name

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_fills_workday_education_dropdown_fallbacks():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "education": [
            {
                "school": "University of Waterloo",
                "degree": "Bachelor's Degree",
                "fieldOfStudy": "Statistics",
                "startYear": "2021",
                "endYear": "2026",
            }
        ],
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <style>
              [role='option'] { display: block; }
            </style>
            <form>
              <h2>Education</h2>
              <div data-automation-id="formField-school">
                <div data-automation-id="formLabel">School or University*</div>
                <button id="school" type="button" aria-haspopup="listbox">0 items selected</button>
              </div>
              <div data-automation-id="formField-degree">
                <div data-automation-id="formLabel">Degree*</div>
                <button id="degree" type="button" aria-haspopup="listbox">Select One</button>
              </div>
              <div data-automation-id="formField-field">
                <div data-automation-id="formLabel">Field of Study</div>
                <button id="field" type="button" aria-haspopup="listbox">0 items selected</button>
              </div>
              <div role="listbox">
                <div role="option" data-target="school">University of Waterloo</div>
                <div role="option" data-target="degree">Bachelor's Degree</div>
                <div role="option" data-target="degree">Master's Degree</div>
                <div role="option" data-target="field">Statistics</div>
                <div role="option" data-target="field">Computer Science</div>
              </div>
            </form>
            <script>
              document.querySelectorAll('[role="option"]').forEach((option) => {
                option.addEventListener('click', () => {
                  const target = document.getElementById(option.dataset.target);
                  target.textContent = option.textContent;
                });
              });
            </script>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        fill_response = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings: [] }, null, (response) => resolve(response));
            })"""
        )

        assert fill_response["ok"] is True, fill_response
        assert page.locator("#school").inner_text() == "University of Waterloo"
        assert page.locator("#degree").inner_text() == "Bachelor's Degree"
        assert page.locator("#field").inner_text() == "Statistics"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_does_not_use_education_add_button_for_work_experience():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "school": "Sample University",
        "degree": "Bachelor's Degree",
        "fieldOfStudy": "Computer Science",
        "workExperience": [
            {
                "company": f"Company {index}",
                "title": "Engineer",
                "startMonth": "September",
                "startYear": "2025",
                "endMonth": "December",
                "endYear": "2025",
            }
            for index in range(6)
        ],
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
        "targetCountry": "usa",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <section id="education">
                <h2>Education</h2>
                <div class="education-row">
                  <label>School<input name="school[]" required></label>
                  <label>Degree<select name="degree[]" required><option>Select...</option><option>Bachelor's Degree</option></select></label>
                  <label>Discipline<input name="discipline[]" required></label>
                  <label>Start date month<input name="start_date_month[]"></label>
                  <label>Start date year<input name="start_date_year[]"></label>
                  <label>End date month<input name="end_date_month[]"></label>
                  <label>End date year<input name="end_date_year[]"></label>
                </div>
                <button id="addEducation" type="button">Add another</button>
              </section>
            </form>
            <script>
              document.getElementById('addEducation').addEventListener('click', () => {
                const row = document.querySelector('.education-row').cloneNode(true);
                row.querySelectorAll('input').forEach((input) => { input.value = ''; });
                row.querySelectorAll('select').forEach((select) => { select.value = 'Select...'; });
                document.getElementById('education').insertBefore(row, document.getElementById('addEducation'));
              });
            </script>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )

        assert preview["ok"] is True
        assert page.locator(".education-row").count() == 1
        assert page.locator("[name='school[]']").input_value() == ""
        assert page.locator("[name='start_date_month[]']").input_value() == ""
        assert page.locator("[name='start_date_year[]']").input_value() == ""
        assert page.locator("[name='end_date_month[]']").input_value() == ""
        assert page.locator("[name='end_date_year[]']").input_value() == ""

        mapped_labels = [mapping["label"] for mapping in preview["result"]["mappings"]]
        assert "Start date month" not in mapped_labels
        assert "Start date year" not in mapped_labels
        assert "End date month" not in mapped_labels
        assert "End date year" not in mapped_labels

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )

        assert fill_response["ok"] is True, fill_response
        assert page.locator(".education-row").count() == 1
        assert page.locator("[name='school[]']").input_value() == "Sample University"
        assert page.locator("[name='start_date_month[]']").input_value() == ""
        assert page.locator("[name='start_date_year[]']").input_value() == ""
        assert page.locator("[name='end_date_month[]']").input_value() == ""
        assert page.locator("[name='end_date_year[]']").input_value() == ""

        browser.close()


def test_content_script_discovers_custom_dropdown_options_before_mapping():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "veteranStatus": "No",
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": True,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label id="veteran-label">Veteran Status</label>
              <button id="veteran" type="button" aria-labelledby="veteran-label" aria-haspopup="listbox" aria-controls="veteran-options" aria-required="true">Select...</button>
              <div id="veteran-options" role="listbox" hidden>
                <div role="option">I identify as one or more of the classifications of protected veteran listed above</div>
                <div role="option">I am not a protected veteran</div>
                <div role="option">I decline to self-identify for protected veteran status</div>
              </div>
            </form>
            <script>
              const button = document.getElementById('veteran');
              const list = document.getElementById('veteran-options');
              button.addEventListener('click', () => { list.hidden = !list.hidden; });
              button.addEventListener('keydown', (event) => {
                if (event.key === 'Escape') list.hidden = true;
              });
              list.querySelectorAll('[role="option"]').forEach((option) => {
                option.addEventListener('click', () => {
                  button.textContent = option.textContent;
                  button.setAttribute('data-selected', option.textContent);
                  list.hidden = true;
                });
              });
            </script>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Veteran Status")
        assert mapping["value"] == "I am not a protected veteran"
        assert "I am not a protected veteran" in [option["label"] for option in mapping["options"]]

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True
        assert page.locator("#veteran").get_attribute("data-selected") == "I am not a protected veteran"

        browser.close()


def test_content_script_greenhouse_uses_typed_dropdown_fallbacks_when_options_are_hidden():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "phoneCountryCode": "+1",
        "veteranStatus": "No",
        "answers": {
            "disabilityStatus": "No, I do not have a disability and have not had one in the past",
        },
        "addresses": {
            "usa": {
                "state": "IL",
            }
        },
        "demographics": {
            "gender": "Male",
            "race": "Asian",
        },
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": True,
        "targetCountry": "usa",
        "requireReviewBeforeSubmit": True,
    }

    html = """
      <form>
        <label id="country-label">Country*</label>
        <input id="country" role="combobox" aria-labelledby="country-label" aria-haspopup="listbox" value="Select...">
        <label>Phone*<input id="phone" name="phone"></label>

        <label id="state-label">What U.S State do you currently reside in? *</label>
        <input id="state" role="combobox" aria-labelledby="state-label" aria-haspopup="listbox" value="Select...">

        <label id="gender-label">Gender*</label>
        <input id="gender" role="combobox" aria-labelledby="gender-label" aria-haspopup="listbox" value="Select...">

        <label id="race-label">Race/Ethnicity*</label>
        <input id="race" role="combobox" aria-labelledby="race-label" aria-haspopup="listbox" value="Select...">

        <label id="veteran-label">Veteran Status*</label>
        <input id="veteran" role="combobox" aria-labelledby="veteran-label" aria-haspopup="listbox" value="Select...">

        <label id="disability-label">Disability Status*</label>
        <input id="disability" role="combobox" aria-labelledby="disability-label" aria-haspopup="listbox" value="Select...">
      </form>
      <script>
        document.querySelectorAll('[role="combobox"]').forEach((input) => {
          input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
              input.setAttribute('data-selected', input.value);
            }
          });
        });
      </script>
    """

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.route("https://job-boards.greenhouse.io/**", lambda route: route.fulfill(body=html, content_type="text/html"))
        page.goto("https://job-boards.greenhouse.io/embed/job_app?for=pinterest")
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        selected = {mapping["label"]: mapping["value"] for mapping in preview["result"]["mappings"]}
        assert selected["Country*"] == "+1"
        assert selected["What U.S State do you currently reside in? *"] == "Illinois"
        assert "Gender*" not in selected
        assert "Race/Ethnicity*" not in selected
        assert "Veteran Status*" not in selected
        assert "Disability Status*" not in selected
        unresolved = {field["label"]: field["unfilledReason"] for field in preview["result"]["unmappedFields"]}
        assert unresolved["Gender*"] == "Dropdown options were not discoverable, so neither autofill nor AI can safely choose an option yet."
        assert unresolved["Race/Ethnicity*"] == "Dropdown options were not discoverable, so neither autofill nor AI can safely choose an option yet."
        assert unresolved["Veteran Status*"] == "Dropdown options were not discoverable, so neither autofill nor AI can safely choose an option yet."
        assert unresolved["Disability Status*"] == "Dropdown options were not discoverable, so neither autofill nor AI can safely choose an option yet."

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True
        assert page.locator("#country").get_attribute("data-selected") == "+1"
        assert page.locator("#state").get_attribute("data-selected") == "Illinois"
        assert page.locator("#gender").get_attribute("data-selected") is None
        assert page.locator("#race").get_attribute("data-selected") is None
        assert page.locator("#veteran").get_attribute("data-selected") is None
        assert page.locator("#disability").get_attribute("data-selected") is None

        browser.close()


def _fake_nvidia_response(status_code, payload=None):
    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self.ok = status_code < 400
            self.text = json.dumps(payload or {})

        def json(self):
            return payload or {}

        def raise_for_status(self):
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")

    return FakeResponse()


def _reset_key_probe_cache():
    server._key_probe_cache.update({"checkedAt": 0.0, "keyValid": None, "keyError": ""})


def test_backend_health_reports_key_rejected_as_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    monkeypatch.setattr(server.requests, "post", lambda *args, **kwargs: _fake_nvidia_response(403))
    _reset_key_probe_cache()

    client = server.app.test_client()
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json["ok"] is True
    assert response.json["keyConfigured"] is True
    assert response.json["keyValid"] is False
    assert "rejected (403)" in response.json["keyError"]
    assert "test-key" not in response.get_data(as_text=True)
    _reset_key_probe_cache()


def test_backend_health_reports_valid_key_and_caches_probe(monkeypatch, tmp_path):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs.get("json", {}))
        return _fake_nvidia_response(200, {"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    monkeypatch.setattr(server.requests, "post", fake_post)
    _reset_key_probe_cache()

    client = server.app.test_client()
    first = client.get("/health")
    second = client.get("/health")

    assert first.json["keyValid"] is True
    assert first.json["keyError"] == ""
    assert second.json["keyValid"] is True
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 1
    _reset_key_probe_cache()


def test_backend_health_without_key_reports_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    _reset_key_probe_cache()

    client = server.app.test_client()
    response = client.get("/health")

    assert response.json["keyConfigured"] is False
    assert response.json["keyValid"] is False
    assert "not configured" in response.json["keyError"]


def test_backend_mapper_key_rejection_returns_actionable_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "applications.sqlite3")
    monkeypatch.setattr(server, "GENERATED_DIR", tmp_path)
    monkeypatch.setattr(server, "LLM_TRACE_PATH", tmp_path / "llm_trace.private.jsonl")
    monkeypatch.setattr(server.requests, "post", lambda *args, **kwargs: _fake_nvidia_response(403))
    _reset_key_probe_cache()

    client = server.app.test_client()
    response = client.post(
        "/map-fields",
        json={
            "fields": [{"index": 0, "label": "Favorite color", "options": []}],
            "profile": {},
            "page": {},
        },
    )

    assert response.status_code == 200
    assert "API key was rejected (403)" in response.json["warning"]

    traces = (tmp_path / "llm_trace.private.jsonl").read_text(encoding="utf-8").splitlines()
    error_events = [json.loads(line) for line in traces if json.loads(line)["event"] == "mapper.error"]
    assert error_events
    assert "API key was rejected (403)" in error_events[0]["error"]
    assert server._key_probe_cache["keyValid"] is False
    _reset_key_probe_cache()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_scan_skips_cookie_consent_and_search_widgets():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "email": "test@example.com",
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <div id="onetrust-consent-sdk">
              <div class="ot-sdk-container">
                <h2>Your Privacy</h2>
                <p>When you visit any website, it may store or retrieve information on your browser,
                mostly in the form of cookies. This information might be about you, your preferences
                or your device and is mostly used to make the site work as you expect it to.</p>
                <label>Performance Cookies<input type="checkbox" name="ot-performance"></label>
                <label>Targeting Cookies<input type="checkbox" name="ot-targeting"></label>
                <button aria-haspopup="listbox">Confirm My Choices</button>
              </div>
            </div>
            <header>
              <input type="search" name="site-search" placeholder="Search">
              <input type="text" aria-label="Search" name="search">
            </header>
            <div role="dialog">
              <p>When you visit any website, it may store or retrieve information on your browser.</p>
              <label>Strictly Necessary<input type="checkbox" name="dialog-consent"></label>
            </div>
            <form>
              <label>First name *<input name="firstName"></label>
              <label>Last name *<input name="lastName"></label>
              <label>Email<input name="email" type="email"></label>
              <div><label>checkbox label<input type="checkbox"></label></div>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        scan = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'SCAN_FIELDS' }, null, (response) => resolve(response));
            })"""
        )
        assert scan["ok"] is True
        scanned_names = {field["name"] for field in scan["fields"]}
        assert "firstName" in scanned_names
        assert "lastName" in scanned_names
        assert "email" in scanned_names
        assert "ot-performance" not in scanned_names
        assert "ot-targeting" not in scanned_names
        assert "dialog-consent" not in scanned_names
        assert "site-search" not in scanned_names
        assert "search" not in scanned_names
        assert all(len(field["label"]) <= 300 for field in scan["fields"])

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        generic_checkbox = next(
            field for field in preview["result"]["debugFields"]
            if field["rawLabel"].lower() == "checkbox label"
        )
        assert generic_checkbox["shouldAsk"] is False
        unmapped_labels = {field["label"] for field in preview["result"]["unmappedFields"]}
        assert not any("cookie" in label.lower() for label in unmapped_labels)
        assert not any("privacy" in label.lower() for label in unmapped_labels)
        assert "checkbox label" not in {label.lower() for label in unmapped_labels}

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_applies_mappings_by_identity_after_index_drift():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "email": "test@example.com",
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form id="application">
              <label>First name *<input name="firstName"></label>
              <label>Last name *<input name="lastName"></label>
              <label>Email<input name="email" type="email"></label>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mappings = preview["result"]["mappings"]
        assert {mapping["name"] for mapping in mappings} >= {"firstName", "lastName", "email"}

        # Mutate the DOM between preview and fill so that scan indices drift.
        page.evaluate(
            """() => {
              const form = document.getElementById('application');
              const drift = document.createElement('div');
              drift.innerHTML = `
                <label>Nickname<input name="nickname"></label>
                <label>Fax number<input name="fax"></label>
              `;
              form.prepend(drift);
            }"""
        )

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            mappings,
        )
        assert fill_response["ok"] is True
        assert page.locator("[name='firstName']").input_value() == "Test"
        assert page.locator("[name='lastName']").input_value() == "Candidate"
        assert page.locator("[name='email']").input_value() == "test@example.com"
        assert page.locator("[name='nickname']").input_value() == ""
        assert page.locator("[name='fax']").input_value() == ""

        verification = fill_response["result"]["verification"]
        assert verification["matched"] >= 3
        assert verification["mismatched"] == []

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_fills_every_repeated_greenhouse_education_row_without_empty_rows():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "education": [
            {"school": "University of Waterloo", "degree": "Bachelor's Degree"},
            {"school": "University of Toronto", "degree": "Master's Degree"},
        ],
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    # A repeated Greenhouse-style education section: one School + Degree combobox per block,
    # each block's option listbox only visible while that block's control is open, and an
    # "Add another" button that clones a new empty block. The option lists are intentionally
    # long (> 40 entries) so the extension cannot pre-discover them and must fill each block
    # through its per-row dropdown fallback.
    html = """
      <form>
        <h2>Education</h2>
        <div id="education">
          <div class="education-row">
            <div data-automation-id="formField-school">
              <div data-automation-id="formLabel">School or University*</div>
              <button class="school" type="button" aria-haspopup="listbox">Select...</button>
              <div class="listbox school-options" role="listbox" hidden></div>
            </div>
            <div data-automation-id="formField-degree">
              <div data-automation-id="formLabel">Degree*</div>
              <button class="degree" type="button" aria-haspopup="listbox">Select...</button>
              <div class="listbox degree-options" role="listbox" hidden></div>
            </div>
          </div>
          <button id="addEducation" type="button">Add another</button>
        </div>
      </form>
      <script>
        const SCHOOLS = ['University of Waterloo', 'University of Toronto'];
        const DEGREES = ["Bachelor's Degree", "Master's Degree"];
        function padOptions(base) {
          const options = base.slice();
          for (let i = options.length; i < 45; i += 1) {
            options.push('Filler option ' + i);
          }
          return options;
        }
        function fillListbox(container, values) {
          container.innerHTML = '';
          values.forEach((value) => {
            const option = document.createElement('div');
            option.setAttribute('role', 'option');
            option.textContent = value;
            container.appendChild(option);
          });
        }
        function wire(root) {
          fillListbox(root.querySelector('.school-options'), padOptions(SCHOOLS));
          fillListbox(root.querySelector('.degree-options'), padOptions(DEGREES));
          root.querySelectorAll('button[aria-haspopup="listbox"]').forEach((button) => {
            const list = button.parentElement.querySelector('.listbox');
            button.addEventListener('click', () => { list.hidden = !list.hidden; });
            button.addEventListener('keydown', (event) => { if (event.key === 'Escape') list.hidden = true; });
            list.querySelectorAll('[role="option"]').forEach((option) => {
              option.addEventListener('click', () => {
                button.textContent = option.textContent;
                button.setAttribute('data-selected', option.textContent);
                list.hidden = true;
              });
            });
          });
        }
        wire(document.querySelector('.education-row'));
        document.getElementById('addEducation').addEventListener('click', () => {
          const row = document.querySelector('.education-row').cloneNode(true);
          row.querySelectorAll('button').forEach((button) => {
            button.textContent = 'Select...';
            button.removeAttribute('data-selected');
          });
          row.querySelectorAll('.listbox').forEach((list) => { list.hidden = true; });
          document.getElementById('education').insertBefore(row, document.getElementById('addEducation'));
          wire(row);
        });
      </script>
    """

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(html)
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        # One block should have been added, one per education entry.
        assert page.locator(".education-row").count() == 2

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response

        # No added row is left empty: every degree/school control is populated, and each
        # education entry lands in its own separate row.
        assert page.locator(".education-row").count() == 2
        assert page.locator(".education-row").nth(0).locator("button.school").inner_text() == "University of Waterloo"
        assert page.locator(".education-row").nth(0).locator("button.degree").inner_text() == "Bachelor's Degree"
        assert page.locator(".education-row").nth(1).locator("button.school").inner_text() == "University of Toronto"
        assert page.locator(".education-row").nth(1).locator("button.degree").inner_text() == "Master's Degree"

        selected_degrees = page.locator("button.degree[data-selected]").count()
        assert selected_degrees == 2

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_scan_skips_open_dropdown_option_lists():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "email": "test@example.com",
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    # A phone Country combobox that is currently OPEN. Its popup listbox holds a search input
    # and the full country-dialing-code list, and a second field's label was polluted with the
    # same option dump. Neither the injected search input nor the option-list-labelled field
    # should be scanned or sent as askable.
    option_dump = (
        "244 results found No results found Afghanistan +93 Åland Islands +358 "
        "Albania +355 Algeria +213 Andorra +376 Angola +244 Argentina +54"
    )
    html = f"""
      <form>
        <label>First name *<input name="firstName"></label>
        <div class="phone-country select__container">
          <label id="pc-label">Phone country code</label>
          <div class="select__control">
            <input id="pc-combobox" name="phoneCountry" role="combobox" aria-labelledby="pc-label" aria-expanded="true" value="">
          </div>
          <div class="select__menu" role="listbox" id="pc-menu">
            <input id="pc-search" name="phoneCountrySearch" role="textbox" placeholder="Search">
            <div class="select__menu-notice">244 results found</div>
            <div role="option">Afghanistan +93</div>
            <div role="option">Åland Islands +358</div>
            <div role="option">Albania +355</div>
          </div>
        </div>
        <div class="form-group">
          <input name="leakedLabelField">
          <span>{option_dump}</span>
        </div>
      </form>
    """

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(html)
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        scan = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'SCAN_FIELDS' }, null, (response) => resolve(response));
            })"""
        )
        assert scan["ok"] is True
        scanned_names = {field["name"] for field in scan["fields"]}
        assert "firstName" in scanned_names
        # The dropdown's injected search input and the option-list-labelled field are dropped.
        assert "phoneCountrySearch" not in scanned_names
        assert "leakedLabelField" not in scanned_names
        for field in scan["fields"]:
            haystack = " ".join([field.get("label", ""), field.get("ariaLabel", ""), field.get("placeholder", "")]).lower()
            assert "results found" not in haystack
            assert len(re.findall(r"\+\d", haystack)) < 5

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        askable_names = {field["name"] for field in preview["result"]["unmappedFields"]}
        assert "phoneCountrySearch" not in askable_names
        assert "leakedLabelField" not in askable_names
        for field in preview["result"]["unmappedFields"]:
            assert "results found" not in field["label"].lower()

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_does_not_add_education_rows_on_repeat_runs_for_degree_only_blocks():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "education": [
            {"school": "University of Waterloo", "degree": "Bachelor's Degree"},
        ],
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    # Databricks-style Greenhouse education block: the row contains ONLY a Degree dropdown
    # (no School/University field). Counting education rows by school fields alone returned
    # 0 here, so every preview/fill run clicked "Add another" again and piled up empty rows.
    html = """
      <form>
        <h2>Education</h2>
        <div id="education">
          <div class="education-row">
            <div data-automation-id="formField-degree">
              <div data-automation-id="formLabel">Degree*</div>
              <button class="degree" type="button" aria-haspopup="listbox">Select...</button>
              <div class="listbox degree-options" role="listbox" hidden></div>
            </div>
          </div>
          <button id="addEducation" type="button">Add another</button>
        </div>
      </form>
      <script>
        const DEGREES = ["Bachelor's Degree", "Master's Degree"];
        function fillListbox(container, values) {
          container.innerHTML = '';
          values.forEach((value) => {
            const option = document.createElement('div');
            option.setAttribute('role', 'option');
            option.textContent = value;
            container.appendChild(option);
          });
        }
        function wire(root) {
          fillListbox(root.querySelector('.degree-options'), DEGREES);
          root.querySelectorAll('button[aria-haspopup="listbox"]').forEach((button) => {
            const list = button.parentElement.querySelector('.listbox');
            button.addEventListener('click', () => { list.hidden = !list.hidden; });
            button.addEventListener('keydown', (event) => { if (event.key === 'Escape') list.hidden = true; });
            list.querySelectorAll('[role="option"]').forEach((option) => {
              option.addEventListener('click', () => {
                button.textContent = option.textContent;
                button.setAttribute('data-selected', option.textContent);
                list.hidden = true;
              });
            });
          });
        }
        wire(document.querySelector('.education-row'));
        document.getElementById('addEducation').addEventListener('click', () => {
          const row = document.querySelector('.education-row').cloneNode(true);
          row.querySelectorAll('button').forEach((button) => {
            button.textContent = 'Select...';
            button.removeAttribute('data-selected');
          });
          row.querySelectorAll('.listbox').forEach((list) => { list.hidden = true; });
          document.getElementById('education').insertBefore(row, document.getElementById('addEducation'));
          wire(row);
        });
      </script>
    """

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(html)
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        # Two full preview + fill cycles: the single profile entry already has a row on the
        # page, so no run may click "Add another".
        for _ in range(2):
            preview = page.evaluate(
                """() => new Promise((resolve) => {
                  window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
                })"""
            )
            assert preview["ok"] is True
            assert page.locator(".education-row").count() == 1

            fill_response = page.evaluate(
                """(mappings) => new Promise((resolve) => {
                  window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
                })""",
                preview["result"]["mappings"],
            )
            assert fill_response["ok"] is True, fill_response
            assert page.locator(".education-row").count() == 1

        # The lone row still received the degree from the single education entry.
        assert page.locator(".education-row").nth(0).locator("button.degree").inner_text() == "Bachelor's Degree"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_fills_greenhouse_work_authorization_react_select_with_profile_yes():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "workAuthorization": "Yes",
        "needsSponsorship": "No",
        "answers": {"workAuthorization": "Yes", "sponsorship": "No"},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "autoMapAmbiguousFields": False,
        "requireReviewBeforeSubmit": True,
    }

    # Greenhouse renders required Yes/No custom questions as non-searchable react-selects:
    # the combobox input is readOnly, the current value lives in a sibling single-value
    # node, typing is ignored, and the option menu opens only on a real mousedown on the
    # control. This exact widget previously fell through every path (never scanned, never
    # mapped, never asked) and ended the run stuck on "Select...".
    html = """
      <form>
        <div class="select__container">
          <label for="question_wa">Are you legally authorized to work in the country in which you are applying?*</label>
          <div class="select">
            <div class="select__control" id="wa-control">
              <div class="select__value-container">
                <div class="select__placeholder" id="wa-value">Select...</div>
                <div class="select__input-container">
                  <input id="question_wa" class="select__input" role="combobox" aria-expanded="false"
                         aria-haspopup="true" aria-autocomplete="list" autocomplete="off" type="text"
                         value="" readonly inputmode="none">
                </div>
              </div>
            </div>
          </div>
        </div>
      </form>
      <script>
        const OPTIONS = ['Select...', 'Yes', 'No'];
        const control = document.getElementById('wa-control');
        const input = document.getElementById('question_wa');
        let menu = null;
        function openMenu() {
          if (menu) return;
          menu = document.createElement('div');
          menu.className = 'select__menu';
          const list = document.createElement('div');
          list.setAttribute('role', 'listbox');
          OPTIONS.forEach((label) => {
            const option = document.createElement('div');
            option.setAttribute('role', 'option');
            option.className = 'select__option';
            option.textContent = label;
            option.addEventListener('click', () => {
              document.getElementById('wa-value').textContent = label;
              document.getElementById('wa-value').className = 'select__single-value';
              input.setAttribute('data-selected', label);
              closeMenu();
            });
            list.appendChild(option);
          });
          menu.appendChild(list);
          control.parentElement.appendChild(menu);
          input.setAttribute('aria-expanded', 'true');
        }
        function closeMenu() {
          if (!menu) return;
          menu.remove();
          menu = null;
          input.setAttribute('aria-expanded', 'false');
        }
        control.addEventListener('mousedown', () => { menu ? closeMenu() : openMenu(); });
        input.addEventListener('keydown', (event) => {
          if (event.key === 'Escape') closeMenu();
        });
      </script>
    """

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.route("https://job-boards.greenhouse.io/**", lambda route: route.fulfill(body=html, content_type="text/html"))
        page.goto("https://job-boards.greenhouse.io/databricks/jobs/1")
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mappings = preview["result"]["mappings"]
        work_auth = next(
            (m for m in mappings if "legally authorized" in m["label"].lower()),
            None,
        )
        assert work_auth is not None, mappings
        assert work_auth["value"] == "Yes"

        # The preview also carries the compact page context used for conditional questions.
        context = preview["result"]["page"]["context"]
        assert any("legally authorized" in entry["label"].lower() for entry in context)

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            mappings,
        )
        assert fill_response["ok"] is True, fill_response
        assert fill_response["result"]["filled"] >= 1
        assert fill_response["result"]["failures"] == []
        assert page.locator("#wa-value").inner_text() == "Yes"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_reports_unfilled_mapped_fields_as_failures():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {"answers": {}, "demographics": {}}
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    html = """
      <form>
        <label for="fullName">Full name</label>
        <input id="fullName" name="fullName" type="text">
        <label for="color">Favorite color</label>
        <select id="color" name="color">
          <option value="">Select...</option>
          <option value="red">Red</option>
          <option value="green">Green</option>
        </select>
        <label for="agree">I agree to the terms</label>
        <input id="agree" name="agree" type="checkbox" checked>
      </form>
    """

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(html)
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        scan = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'SCAN_FIELDS' }, null, (response) => resolve(response));
            })"""
        )
        assert scan["ok"] is True
        by_name = {field["name"]: field for field in scan["fields"]}

        def mapping_for(name, value):
            field = by_name[name]
            return {
                "index": field["index"],
                "label": field.get("label", ""),
                "name": field.get("name", ""),
                "id": field.get("id", ""),
                "tag": field.get("tag", ""),
                "type": field.get("type", ""),
                "value": value,
                "source": "rule",
                "confidence": 0.9,
            }

        mappings = [
            # Fills normally.
            mapping_for("fullName", "Jane Doe"),
            # A yes/no answer aimed at a select with no matching option: fillSelect returns
            # false and this previously vanished without a trace.
            mapping_for("color", "Yes"),
            # Checkbox already in the desired state: not a failure, but also not newly filled.
            mapping_for("agree", "Yes"),
        ]

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            mappings,
        )
        assert fill_response["ok"] is True, fill_response
        result = fill_response["result"]

        assert result["filled"] == 1
        assert page.locator("#fullName").input_value() == "Jane Doe"
        assert page.locator("#agree").is_checked()

        failures = result["failures"]
        assert len(failures) == 1, failures
        assert "favorite color" in failures[0]["label"].lower()
        assert failures[0]["error"] == "no matching option"

        browser.close()


RESUME_UPLOAD_PAGE = """
<form>
  <div>
    <label for="resumeUpload">Resume/CV *</label>
    <button type="button">Attach</button>
    <input id="resumeUpload" type="file" style="display:none">
  </div>
  <div>
    <label for="coverLetterUpload">Cover Letter</label>
    <button type="button">Attach</button>
    <input id="coverLetterUpload" type="file" style="display:none">
  </div>
</form>
"""


def install_chrome_mock_with_resume(page, profile, settings, resume_response):
    page.evaluate(
        f"""() => {{
          const profile = {json.dumps(profile)};
          const settings = {json.dumps(settings)};
          const resumeResponse = {json.dumps(resume_response)};
          window.__autofillListener = null;
          window.__resumeFetchCount = 0;
          window.chrome = {{
            runtime: {{
              onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
              sendMessage: async (message) => {{
                if (message?.type === 'FETCH_RESUME_FILE') {{
                  window.__resumeFetchCount += 1;
                  return resumeResponse;
                }}
                return {{ ok: true, payload: {{ mappings: [] }} }};
              }}
            }},
            storage: {{
              local: {{
                get: async () => ({{ candidateProfile: profile, settings }})
              }}
            }}
          }};
        }}"""
    )


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_attaches_resume_to_resume_input_but_not_cover_letter():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    resume_b64 = base64.b64encode(b"%PDF-1.4 sample resume bytes").decode("ascii")
    profile = {"firstName": "Test", "lastName": "Candidate", "resumeFileName": "My Resume.pdf", "answers": {}, "demographics": {}}
    settings = {"autoFillDynamicFields": False, "autoFillSensitiveFields": False, "requireReviewBeforeSubmit": True}

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(RESUME_UPLOAD_PAGE)
        install_chrome_mock_with_resume(
            page,
            profile,
            settings,
            {"ok": True, "filename": "My Resume.pdf", "mimeType": "application/pdf", "bytes": resume_b64},
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        automatic_tasks = [task for task in preview["result"]["manualTasks"] if task.get("automatic")]
        assert len(automatic_tasks) == 1
        assert automatic_tasks[0]["label"] == "Resume/CV"
        assert automatic_tasks[0]["resumeFileName"] == "My Resume.pdf"

        fill_response = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings: [] }, null, (response) => resolve(response));
            })"""
        )
        assert fill_response["ok"] is True, fill_response
        result = fill_response["result"]
        assert result["filled"] == 1
        assert result["attached"] == [{"label": "Resume/CV", "filename": "My Resume.pdf"}]
        assert result["failures"] == []

        resume_files = page.evaluate(
            "() => Array.from(document.getElementById('resumeUpload').files).map((file) => file.name)"
        )
        assert resume_files == ["My Resume.pdf"]
        assert page.evaluate("() => document.getElementById('coverLetterUpload').files.length") == 0
        assert page.evaluate("() => window.__resumeFetchCount") == 1
        resume_events = page.evaluate(
            """() => {
              const input = document.getElementById('resumeUpload');
              return { size: input.files[0].size, type: input.files[0].type };
            }"""
        )
        assert resume_events["size"] == len(b"%PDF-1.4 sample resume bytes")
        assert resume_events["type"] == "application/pdf"

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_does_not_replace_user_chosen_resume_file():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    resume_b64 = base64.b64encode(b"%PDF-1.4 sample resume bytes").decode("ascii")
    profile = {"firstName": "Test", "lastName": "Candidate", "resumeFileName": "My Resume.pdf", "answers": {}, "demographics": {}}
    settings = {"autoFillDynamicFields": False, "autoFillSensitiveFields": False, "requireReviewBeforeSubmit": True}

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(RESUME_UPLOAD_PAGE)
        install_chrome_mock_with_resume(
            page,
            profile,
            settings,
            {"ok": True, "filename": "My Resume.pdf", "mimeType": "application/pdf", "bytes": resume_b64},
        )
        page.evaluate(
            """() => {
              const transfer = new DataTransfer();
              transfer.items.add(new File(['user chosen'], 'user-resume.pdf', { type: 'application/pdf' }));
              document.getElementById('resumeUpload').files = transfer.files;
            }"""
        )
        page.add_script_tag(path=str(content_script_path))

        fill_response = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings: [] }, null, (response) => resolve(response));
            })"""
        )
        assert fill_response["ok"] is True, fill_response
        result = fill_response["result"]
        assert result["filled"] == 0
        assert result["attached"] == []
        assert result["failures"] == []

        resume_files = page.evaluate(
            "() => Array.from(document.getElementById('resumeUpload').files).map((file) => file.name)"
        )
        assert resume_files == ["user-resume.pdf"]
        assert page.evaluate("() => window.__resumeFetchCount") == 0

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_reports_resume_fetch_failure_only_when_resume_input_exists():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {"firstName": "Test", "lastName": "Candidate", "answers": {}, "demographics": {}}
    settings = {"autoFillDynamicFields": False, "autoFillSensitiveFields": False, "requireReviewBeforeSubmit": True}
    missing_resume = {"ok": False, "error": "No resume file configured. Set RESUME_FILE_PATH in backend/env.private."}

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(RESUME_UPLOAD_PAGE)
        install_chrome_mock_with_resume(page, profile, settings, missing_resume)
        page.add_script_tag(path=str(content_script_path))

        fill_response = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings: [] }, null, (response) => resolve(response));
            })"""
        )
        assert fill_response["ok"] is True, fill_response
        result = fill_response["result"]
        assert result["filled"] == 0
        assert result["attached"] == []
        assert result["failures"] == [{"label": "Resume/CV", "error": missing_resume["error"]}]
        assert page.evaluate("() => document.getElementById('resumeUpload').files.length") == 0

        no_upload_page = browser.new_page()
        no_upload_page.set_content("<form><label>Full name<input name='fullName'></label></form>")
        install_chrome_mock_with_resume(no_upload_page, profile, settings, missing_resume)
        no_upload_page.add_script_tag(path=str(content_script_path))

        fill_response = no_upload_page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings: [] }, null, (response) => resolve(response));
            })"""
        )
        assert fill_response["ok"] is True, fill_response
        assert fill_response["result"]["failures"] == []
        assert fill_response["result"]["attached"] == []
        assert no_upload_page.evaluate("() => window.__resumeFetchCount") == 0

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_page_field_context_reflects_answers_filled_mid_loop():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {"firstName": "Test", "lastName": "Candidate", "answers": {}, "demographics": {}}
    settings = {"autoFillDynamicFields": False, "autoFillSensitiveFields": False, "requireReviewBeforeSubmit": True}

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <form>
              <fieldset>
                <legend>Do you have relatives employed by this company?</legend>
                <label><input type="radio" name="relatives" value="Yes">Yes</label>
                <label><input type="radio" name="relatives" value="No">No</label>
              </fieldset>
              <label>If yes, which relative?<input name="whichRelative"></label>
            </form>
            """
        )
        install_chrome_mock_with_resume(page, profile, settings, {"ok": False, "error": "unused"})
        page.add_script_tag(path=str(content_script_path))

        def get_context():
            response = page.evaluate(
                """() => new Promise((resolve) => {
                  window.__autofillListener({ type: 'GET_PAGE_FIELD_CONTEXT' }, null, (response) => resolve(response));
                })"""
            )
            assert response["ok"] is True
            return response["context"]

        def relatives_entry(context):
            matches = [entry for entry in context if "relatives" in entry["label"].lower()]
            assert matches, context
            return matches[0]

        before = relatives_entry(get_context())
        assert before["answered"] is False
        assert before["currentValue"] == ""

        scan = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'SCAN_FIELDS' }, null, (response) => resolve(response));
            })"""
        )
        group = next(field for field in scan["fields"] if field["name"] == "relatives")
        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            [
                {
                    "index": group["index"],
                    "label": group.get("label", ""),
                    "name": group.get("name", ""),
                    "id": group.get("id", ""),
                    "tag": group.get("tag", ""),
                    "type": group.get("type", ""),
                    "value": "No",
                    "source": "rule",
                    "confidence": 0.9,
                }
            ],
        )
        assert fill_response["ok"] is True, fill_response
        assert fill_response["result"]["filled"] >= 1
        assert page.locator("input[name='relatives'][value='No']").is_checked()

        after = relatives_entry(get_context())
        assert after["answered"] is True
        assert "No" in after["currentValue"]

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_fills_only_required_fields_plus_contact_basics():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "fullName": "Test Candidate",
        "email": "test@example.com",
        "phone": "5550100000",
        "answers": {
            "custom:notice-period": "2 weeks",
            "custom:best-time-to-call": "Morning",
            "custom:favorite-snack": "Apples",
        },
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        # Contact basics carry no required markers on purpose: they must still fill.
        # "Notice period *" is required via a trailing asterisk only (no attribute).
        page.set_content(
            """
            <form>
              <label>First name<input name="firstName"></label>
              <label>Last name<input name="lastName"></label>
              <label>Email<input name="email" type="email"></label>
              <label>Mobile phone<input name="phone" type="tel"></label>
              <label>Notice period *<input name="noticePeriod"></label>
              <label>Why do you want to work here? *<textarea name="why"></textarea></label>
              <label>Best time to call<select name="bestTime"><option value=""></option><option>Morning</option><option>Evening</option></select></label>
              <label>Favorite snack<input name="snack"></label>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async () => ({{ ok: true, payload: {{ mappings: [] }} }})
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }})
                  }}
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))

        preview = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'PREVIEW_AUTOFILL' }, null, (response) => resolve(response));
            })"""
        )
        assert preview["ok"] is True
        mappings_by_name = {mapping["name"]: mapping for mapping in preview["result"]["mappings"]}

        # Contact basics still fill despite the missing required markers.
        assert mappings_by_name["firstName"]["value"] == "Test"
        assert mappings_by_name["lastName"]["value"] == "Candidate"
        assert mappings_by_name["email"]["value"] == "test@example.com"
        assert mappings_by_name["phone"]["value"] == "5550100000"
        # Required (asterisk-marked) field fills from saved answers.
        assert mappings_by_name["noticePeriod"]["value"] == "2 weeks"
        # Optional fields are never mapped, even when a saved answer exists.
        assert "bestTime" not in mappings_by_name
        assert "snack" not in mappings_by_name

        # Optional fields are not asked either; they are surfaced as skipped-optional.
        unmapped_labels = [field["label"] for field in preview["result"]["unmappedFields"]]
        assert any("Why do you want to work here?" in label for label in unmapped_labels)
        assert not any("Best time to call" in label for label in unmapped_labels)
        assert not any("Favorite snack" in label for label in unmapped_labels)
        skipped_labels = [field["label"] for field in preview["result"]["skippedOptionalFields"]]
        assert any("Best time to call" in label for label in skipped_labels)
        assert any("Favorite snack" in label for label in skipped_labels)

        fill_response = page.evaluate(
            """(mappings) => new Promise((resolve) => {
              window.__autofillListener({ type: 'APPLY_AUTOFILL_MAPPINGS', mappings }, null, (response) => resolve(response));
            })""",
            preview["result"]["mappings"],
        )
        assert fill_response["ok"] is True, fill_response
        assert page.locator("[name='firstName']").input_value() == "Test"
        assert page.locator("[name='noticePeriod']").input_value() == "2 weeks"
        assert page.locator("[name='bestTime']").input_value() == ""
        assert page.locator("[name='snack']").input_value() == ""

        browser.close()


ASSISTANT_PANEL_HTML = """
<div id="assistant-panel" style="display:none">
  <div id="chatLog"></div>
  <form id="chatForm"><input id="chatInput"></form>
  <div id="reviewList"></div>
  <span id="assistantStatus"></span>
  <div id="aiStatusPanel"></div>
  <span id="aiStatusText"></span>
  <span id="aiUsageText"></span>
  <button id="scanButton"></button>
  <button id="previewButton"></button>
  <button id="askAiButton"></button>
  <button id="fillSelectedButton"></button>
  <button id="saveAnswersButton"></button>
  <button id="trackButton"></button>
  <button id="uploadResumeButton"></button>
  <button id="optionsButton"></button>
</div>
"""


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_assistant_one_shot_audit_fixes_and_fills_required_fields_only():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    assistant_script_path = ROOT / "autofill_extension/src/assistant.js"
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            ASSISTANT_PANEL_HTML
            + """
            <form>
              <label>Preferred start date *<input name="startDate" value="Never" required></label>
              <label>Team preference *<select name="teamPreference" required><option value=""></option><option>Platform</option><option>Product</option></select></label>
              <label>Favorite snack<input name="snack"></label>
            </form>
            """
        )
        page.evaluate(
            f"""() => {{
              const profile = {json.dumps(profile)};
              const settings = {json.dumps(settings)};
              window.__autofillListener = null;
              window.__auditPayloads = [];
              window.__mapFieldsPayloads = [];
              const respond = (message) => {{
                if (message?.type === 'AUDIT_FIELDS_WITH_BACKEND') {{
                  window.__auditPayloads.push(message.payload);
                  const decisions = (message.payload.fields || []).map((field) => {{
                    if (/preferred start date/i.test(field.label || '')) {{
                      return {{ index: field.index, action: 'correct', value: 'March 2026', confidence: 0.9, source: 'audit', reason: 'Wrong answer' }};
                    }}
                    if (/team preference/i.test(field.label || '')) {{
                      return {{ index: field.index, action: 'fill', value: 'Platform', confidence: 0.9, source: 'audit', reason: 'Required and empty' }};
                    }}
                    return {{ index: field.index, action: 'keep', value: '', confidence: 0.9, source: 'audit', reason: 'Looks fine' }};
                  }});
                  return {{ ok: true, payload: {{ corrections: [], decisions, issues: [] }} }};
                }}
                if (message?.type === 'MAP_FIELDS_WITH_BACKEND') {{
                  window.__mapFieldsPayloads.push(message.payload);
                  return {{ ok: true, payload: {{ mappings: [] }} }};
                }}
                if (message?.type === 'GET_AI_USAGE') {{
                  return {{ ok: true, payload: {{ aiUsage: {{ requestsLastMinute: 0, limitPerMinute: 40, remainingThisMinute: 40 }} }} }};
                }}
                if (message?.type === 'FETCH_RESUME_FILE') {{
                  return {{ ok: false, error: 'No resume in this test.' }};
                }}
                return {{ ok: true, payload: {{}} }};
              }};
              window.chrome = {{
                runtime: {{
                  onMessage: {{ addListener: (fn) => {{ window.__autofillListener = fn; }} }},
                  sendMessage: async (message) => respond(message),
                  openOptionsPage: () => {{}}
                }},
                storage: {{
                  local: {{
                    get: async () => ({{ candidateProfile: profile, settings }}),
                    set: async () => {{}}
                  }}
                }},
                tabs: {{
                  query: async () => [{{ id: 1, url: 'https://example.com/apply', title: 'Apply' }}],
                  sendMessage: (tabId, message, options) => new Promise((resolve) => {{
                    window.__autofillListener(message, null, resolve);
                  }})
                }},
                scripting: {{
                  executeScript: async () => []
                }},
                webNavigation: {{
                  getAllFrames: async () => [{{ frameId: 0, url: '' }}]
                }}
              }};
            }}"""
        )
        page.add_script_tag(path=str(content_script_path))
        page.add_script_tag(path=str(assistant_script_path))

        page.evaluate("() => document.getElementById('askAiButton').click()")
        page.wait_for_function(
            """() => document.getElementById('aiStatusText').textContent === 'AI idle'
              && window.__auditPayloads.length > 0"""
        )

        # ONE audit request, carrying only required fields (filled and empty) plus a
        # fresh page context snapshot.
        audit_payloads = page.evaluate("() => window.__auditPayloads")
        assert len(audit_payloads) == 1
        payload = audit_payloads[0]
        labels = [field["label"] for field in payload["fields"]]
        assert any("Preferred start date" in label for label in labels)
        assert any("Team preference" in label for label in labels)
        assert not any("Favorite snack" in label for label in labels), labels
        assert isinstance(payload["page"]["context"], list) and payload["page"]["context"]
        assert all("currentValue" in entry for entry in payload["page"]["context"])
        mappings_by_index = {mapping["index"]: mapping for mapping in payload["mappings"]}
        start_field = next(field for field in payload["fields"] if "Preferred start date" in field["label"])
        team_field = next(field for field in payload["fields"] if "Team preference" in field["label"])
        assert mappings_by_index[start_field["index"]]["value"] == "Never"
        assert mappings_by_index[team_field["index"]]["value"] == ""

        # "correct" replaced the wrong value and "fill" answered the empty required field.
        assert page.locator("[name='startDate']").input_value() == "March 2026"
        assert page.locator("[name='teamPreference']").input_value() == "Platform"
        assert page.locator("[name='snack']").input_value() == ""

        # Everything required was handled by the one-shot audit, so the per-field
        # Ask-AI loop had nothing left to do.
        assert page.evaluate("() => window.__mapFieldsPayloads") == []
        chat_text = page.evaluate("() => document.getElementById('chatLog').textContent")
        assert "AI corrections" in chat_text

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_runs_workday_resume_autofill_before_field_fills():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    resume_b64 = base64.b64encode(b"%PDF-1.4 sample resume bytes").decode("ascii")
    profile = {
        "firstName": "Test",
        "lastName": "Candidate",
        "email": "test@example.com",
        "resumeFileName": "My Resume.pdf",
        "answers": {},
        "demographics": {},
    }
    settings = {
        "autoFillDynamicFields": False,
        "autoFillSensitiveFields": False,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(
            """
            <div data-automation-id="quickApplyUpload">
              <button type="button" data-automation-id="autofillWithResume">Autofill with Resume</button>
              <input id="resumeUpload" type="file" aria-label="Resume/CV upload" style="display:none">
            </div>
            <form id="app-form">
              <label>Email Address<input name="email" type="email"></label>
              <label>Legal first name *<input name="firstName" required></label>
            </form>
            <script>
              window.__order = [];
              // Simulate the ATS parsing the attached resume: a new field appears.
              document.getElementById('resumeUpload').addEventListener('change', () => {
                window.__order.push('resume-attached');
                const label = document.createElement('label');
                label.append('Parsed by resume *');
                const input = document.createElement('input');
                input.name = 'parsedByResume';
                input.required = true;
                label.appendChild(input);
                document.getElementById('app-form').appendChild(label);
              });
              document.querySelectorAll('#app-form input').forEach((element) => {
                element.addEventListener('input', () => window.__order.push('fill:' + element.name));
              });
            </script>
            """
        )
        install_chrome_mock_with_resume(
            page,
            profile,
            settings,
            {"ok": True, "filename": "My Resume.pdf", "mimeType": "application/pdf", "bytes": resume_b64},
        )
        page.add_script_tag(path=str(content_script_path))

        fill_response = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'AUTOFILL_PAGE' }, null, (response) => resolve(response));
            })"""
        )
        assert fill_response["ok"] is True, fill_response
        result = fill_response["result"]

        order = page.evaluate("() => window.__order")
        # The resume attach ran FIRST, before any field mapping was applied.
        assert order and order[0] == "resume-attached", order
        assert "fill:email" in order and order.index("fill:email") > 0
        assert "fill:firstName" in order and order.index("fill:firstName") > 0

        # The scan happened after the ATS parse, so it saw the parsed field too.
        assert result["scanned"] == 3
        assert {"label": "Resume/CV", "filename": "My Resume.pdf"} in result["attached"]
        assert page.evaluate("() => window.__resumeFetchCount") == 1

        resume_files = page.evaluate(
            "() => Array.from(document.getElementById('resumeUpload').files).map((file) => file.name)"
        )
        assert resume_files == ["My Resume.pdf"]
        assert page.locator("[name='email']").input_value() == "test@example.com"
        assert page.locator("[name='firstName']").input_value() == "Test"

        browser.close()


# Models a real Greenhouse resume field: the actual <input type=file> is NOT in the DOM
# until the "Attach" button is clicked, and the section also offers Dropbox / Google Drive
# / Enter manually alternatives we must not click. A separate Cover Letter section has its
# own "Attach" that must be left alone.
GREENHOUSE_REVEAL_PAGE = """
<form>
  <fieldset id="resumeField">
    <legend>Resume/CV</legend>
    <button type="button" id="attachResumeBtn">Attach</button>
    <button type="button">Dropbox</button>
    <button type="button">Google Drive</button>
    <button type="button">Enter manually</button>
  </fieldset>
  <fieldset id="coverField">
    <legend>Cover Letter</legend>
    <button type="button" id="attachCoverBtn">Attach</button>
    <button type="button">Enter manually</button>
  </fieldset>
</form>
<script>
  window.__resumeAttachClicks = 0;
  window.__coverAttachClicks = 0;
  document.getElementById('attachResumeBtn').addEventListener('click', () => {
    window.__resumeAttachClicks += 1;
    if (document.getElementById('revealedResume')) { return; }
    const input = document.createElement('input');
    input.type = 'file';
    input.id = 'revealedResume';
    input.setAttribute('aria-label', 'Resume/CV');
    document.getElementById('resumeField').appendChild(input);
  });
  document.getElementById('attachCoverBtn').addEventListener('click', () => {
    window.__coverAttachClicks += 1;
    const input = document.createElement('input');
    input.type = 'file';
    input.id = 'revealedCover';
    input.setAttribute('aria-label', 'Cover Letter');
    document.getElementById('coverField').appendChild(input);
  });
</script>
"""


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_content_script_reveals_greenhouse_resume_input_then_attaches():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    resume_b64 = base64.b64encode(b"%PDF-1.4 sample resume bytes").decode("ascii")
    profile = {"firstName": "Test", "lastName": "Candidate", "resumeFileName": "My Resume.pdf", "answers": {}, "demographics": {}}
    settings = {"autoFillDynamicFields": False, "autoFillSensitiveFields": False, "requireReviewBeforeSubmit": True}

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        page = browser.new_page()
        page.set_content(GREENHOUSE_REVEAL_PAGE)
        install_chrome_mock_with_resume(
            page,
            profile,
            settings,
            {"ok": True, "filename": "My Resume.pdf", "mimeType": "application/pdf", "bytes": resume_b64},
        )
        page.add_script_tag(path=str(content_script_path))

        # No resume file input exists yet: it only appears after the reveal click.
        assert page.evaluate("() => document.querySelectorAll('input[type=file]').length") == 0

        response = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'ATTACH_RESUME_NOW' }, null, (response) => resolve(response));
            })"""
        )
        assert response["ok"] is True, response
        result = response["result"]
        assert result["attached"] == [{"label": "Resume/CV", "filename": "My Resume.pdf"}]
        assert result["failures"] == []
        assert result["resumeInputPresent"] is True

        # The revealed resume input holds exactly the fetched résumé.
        resume_files = page.evaluate(
            "() => Array.from(document.getElementById('revealedResume').files).map((file) => file.name)"
        )
        assert resume_files == ["My Resume.pdf"]
        assert page.evaluate("() => window.__resumeFetchCount") == 1

        # Only the résumé "Attach" was clicked, never the cover letter's, and never Dropbox
        # / Google Drive / Enter manually (there is still no cover letter input).
        assert page.evaluate("() => window.__resumeAttachClicks") == 1
        assert page.evaluate("() => window.__coverAttachClicks") == 0
        assert page.evaluate("() => document.getElementById('revealedCover')") is None

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_attach_resume_now_reports_attached_result_and_clear_failure():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    resume_b64 = base64.b64encode(b"%PDF-1.4 sample resume bytes").decode("ascii")
    profile = {"firstName": "Test", "lastName": "Candidate", "resumeFileName": "My Resume.pdf", "answers": {}, "demographics": {}}
    settings = {"autoFillDynamicFields": False, "autoFillSensitiveFields": False, "requireReviewBeforeSubmit": True}

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        # A page with an ordinary (present) resume input: the handler returns the attached
        # result directly.
        page = browser.new_page()
        page.set_content(RESUME_UPLOAD_PAGE)
        install_chrome_mock_with_resume(
            page,
            profile,
            settings,
            {"ok": True, "filename": "My Resume.pdf", "mimeType": "application/pdf", "bytes": resume_b64},
        )
        page.add_script_tag(path=str(content_script_path))

        response = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'ATTACH_RESUME_NOW' }, null, (response) => resolve(response));
            })"""
        )
        assert response["ok"] is True, response
        assert response["result"]["attached"] == [{"label": "Resume/CV", "filename": "My Resume.pdf"}]
        assert response["result"]["failures"] == []
        assert response["result"]["resumeInputPresent"] is True

        # A page with no résumé control at all: a clear, exception-free failure signal.
        no_control_page = browser.new_page()
        no_control_page.set_content("<form><label>Full name<input name='fullName'></label></form>")
        install_chrome_mock_with_resume(
            no_control_page,
            profile,
            settings,
            {"ok": True, "filename": "My Resume.pdf", "mimeType": "application/pdf", "bytes": resume_b64},
        )
        no_control_page.add_script_tag(path=str(content_script_path))

        response = no_control_page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'ATTACH_RESUME_NOW' }, null, (response) => resolve(response));
            })"""
        )
        assert response["ok"] is True, response
        assert response["result"]["attached"] == []
        assert response["result"]["failures"] == []
        assert response["result"]["resumeInputPresent"] is False
        assert no_control_page.evaluate("() => window.__resumeFetchCount") == 0

        browser.close()


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright is not installed")
def test_attach_resume_now_uses_existing_visible_input_without_reveal():
    from playwright.sync_api import sync_playwright

    content_script_path = ROOT / "autofill_extension/src/content.js"
    resume_b64 = base64.b64encode(b"%PDF-1.4 sample resume bytes").decode("ascii")
    profile = {"firstName": "Test", "lastName": "Candidate", "resumeFileName": "My Resume.pdf", "answers": {}, "demographics": {}}
    settings = {"autoFillDynamicFields": False, "autoFillSensitiveFields": False, "requireReviewBeforeSubmit": True}

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium could not launch in this environment: {exc}")

        # A resume input that is already present in the DOM (Workday / hidden inputs).
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <fieldset id="resumeField">
                <legend>Resume/CV</legend>
                <button type="button" id="attachBtn">Attach</button>
                <input id="visibleResume" type="file" aria-label="Resume/CV">
              </fieldset>
            </form>
            <script>
              window.__attachClicks = 0;
              document.getElementById('attachBtn').addEventListener('click', () => {
                window.__attachClicks += 1;
              });
            </script>
            """
        )
        install_chrome_mock_with_resume(
            page,
            profile,
            settings,
            {"ok": True, "filename": "My Resume.pdf", "mimeType": "application/pdf", "bytes": resume_b64},
        )
        page.add_script_tag(path=str(content_script_path))

        response = page.evaluate(
            """() => new Promise((resolve) => {
              window.__autofillListener({ type: 'ATTACH_RESUME_NOW' }, null, (response) => resolve(response));
            })"""
        )
        assert response["ok"] is True, response
        assert response["result"]["attached"] == [{"label": "Resume/CV", "filename": "My Resume.pdf"}]

        resume_files = page.evaluate(
            "() => Array.from(document.getElementById('visibleResume').files).map((file) => file.name)"
        )
        assert resume_files == ["My Resume.pdf"]
        assert page.evaluate("() => window.__resumeFetchCount") == 1
        # The existing input was used directly; the reveal "Attach" was never clicked.
        assert page.evaluate("() => window.__attachClicks") == 0

        browser.close()
