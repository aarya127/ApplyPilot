import importlib.util
import json
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

    client = server.app.test_client()
    response = client.post("/map-fields", json={"fields": [], "profile": {}, "page": {}})

    assert response.status_code == 200
    assert response.json["mappings"] == []
    assert "NVIDIA_API_KEY" in response.json["warning"]


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
                }
            ],
            "profile": {},
            "page": {},
        },
    )

    assert response.status_code == 200
    assert response.json["mappings"] == [
        {
            "index": 0,
            "value": "I am authorized to work in the United States for any employer",
            "confidence": 0.78,
            "source": "policy",
        }
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

    assert server.policy_mappings(fields, {}) == [
        {"index": 0, "value": "No", "confidence": 0.78, "source": "policy"},
        {
            "index": 1,
            "value": "I am authorized to work in the United States for any employer",
            "confidence": 0.78,
            "source": "policy",
        },
        {"index": 2, "value": "Yes", "confidence": 0.78, "source": "policy"},
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
    ],
)
def test_backend_policy_answers_generic_application_policy_questions(label, expected):
    assert server.policy_mappings([{"index": 0, "label": label}], {}) == [
        {"index": 0, "value": expected, "confidence": 0.78, "source": "policy"}
    ]


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

    assert server.policy_mappings(fields, {}) == [
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
                <label>Country/Region<input name="country_region"></label>
                <label>Company Name<input name="companyName"></label>
                <label>If Yes Which Country Were You Last Assigned to?<input name="conditionalCountry"></label>
                <label>I agree to the Alternate Dispute Resolution statement above<input type="checkbox" name="adrAgree"></label>
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
        page.set_content(
            """
            <form>
              <label>Location (City)*<input name="locationCity" aria-autocomplete="list" placeholder="Start typing..."></label>
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
        mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Location (City)*")
        assert mapping["value"] == "Chicago, IL"

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
                <label id="country-label">Country*</label>
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
        country_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Country*")
        address_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Address Line 1*")
        city_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "City*")
        state_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "State*")
        postal_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Postal Code*")
        phone_device_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Phone Device Type*")
        phone_code_mapping = next(mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Country Phone Code*")
        phone_extension_mapping = [mapping for mapping in preview["result"]["mappings"] if mapping["label"] == "Phone Extension"]
        unmapped_labels = [field["label"] for field in preview["result"]["unmappedFields"]]
        assert country_mapping["value"] == "United States of America"
        assert address_mapping["value"] == "456 Lake St"
        assert city_mapping["value"] == "Chicago"
        assert state_mapping["value"] == "Illinois"
        assert postal_mapping["value"] == "60601"
        assert phone_device_mapping["value"] == "Mobile"
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
              <label>Do you have experience with Kubernetes?<input name="k8s"></label>
              <label>Who is your current or previous employer?<input name="employer"></label>
              <label>What is your current or previous job title?<input name="jobTitle"></label>
              <label>Have you ever been employed by ExampleCo or an ExampleCo affiliate?<select name="companyAffiliate"><option></option><option>Yes</option><option>No</option></select></label>
              <label>Do you opt-in to receive WhatsApp messages from ExampleCo Recruiting?<select name="whatsapp"><option></option><option>Yes</option><option>No</option></select></label>
              <label>Gender<select name="gender"><option></option><option>Female</option><option>Male</option><option>I do not wish to answer</option></select></label>
              <label>Are you Hispanic/Latino?<select name="hispanic"><option></option><option>Yes, I am Hispanic or Latino</option><option>No, I am not Hispanic or Latino</option><option>I do not wish to answer</option></select></label>
              <label>Race<select name="race"><option></option><option>Black or African American</option><option>Asian (Not Hispanic or Latino)</option><option>I do not wish to answer</option></select></label>
              <label>Are you a protected veteran?<select name="veteran"><option></option><option>I identify as one or more of the classifications of a protected veteran</option><option>I am not a protected Veteran</option><option>I do not wish to answer</option></select></label>
              <label>Upload Resume<input type="file" name="resume"></label>
              <label>What is your favorite database?<input name="database"></label>
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
              <label>What AI projects have you built in your spare time outside of work?<textarea name="aiProjects"></textarea></label>
              <fieldset>
                <legend>Are you located within ~50 miles of Seattle, WA; Boston, MA; Washington DC; or Austin, TX?</legend>
                <label><input type="radio" name="nearOffice" value="Yes">Yes</label>
                <label><input type="radio" name="nearOffice" value="No">No</label>
              </fieldset>
              <label>Location <input name="location" placeholder="REQUIRED: Enter your city, region/state, and country"></label>
              <label>Zip Code / Postal Code<input name="zip"></label>
              <fieldset>
                <legend>Pronouns</legend>
                <label><input type="radio" name="pronouns" value="He/Him">He/Him</label>
                <label><input type="radio" name="pronouns" value="She/Her">She/Her</label>
                <label><input type="radio" name="pronouns" value="They/Them">They/Them</label>
              </fieldset>
              <fieldset>
                <legend>Can you provide proof of authorization to work in the country for which job you are applying for?</legend>
                <label><input type="radio" name="authorized" value="Yes">Yes</label>
                <label><input type="radio" name="authorized" value="No">No</label>
              </fieldset>
              <fieldset>
                <legend>Will you now or in the future require employer sponsorship to work in the country for which job you are applying for?</legend>
                <label><input type="radio" name="sponsor" value="Yes">Yes</label>
                <label><input type="radio" name="sponsor" value="No">No</label>
              </fieldset>
              <fieldset>
                <legend>What is your current age?</legend>
                <label><input type="radio" name="age" value="Under 30">Under 30</label>
                <label><input type="radio" name="age" value="I prefer not to answer">I prefer not to answer</label>
              </fieldset>
              <fieldset>
                <legend>What is your gender identity?</legend>
                <label><input type="radio" name="genderIdentity" value="Man">Man</label>
                <label><input type="radio" name="genderIdentity" value="Woman">Woman</label>
                <label><input type="radio" name="genderIdentity" value="Another Gender Identity">Another Gender Identity</label>
              </fieldset>
              <fieldset>
                <legend>Race</legend>
                <label><input type="radio" name="race" value="Hispanic or Latino">Hispanic or Latino</label>
                <label><input type="radio" name="race" value="White (Not Hispanic or Latino)">White (Not Hispanic or Latino)</label>
                <label><input type="radio" name="race" value="Asian (Not Hispanic or Latino)">Asian (Not Hispanic or Latino)</label>
                <label><input type="radio" name="race" value="Decline to self-identify">Decline to self-identify</label>
              </fieldset>
              <fieldset>
                <legend>Veteran Status</legend>
                <label><input type="radio" name="veteran" value="I identify as one or more of the classifications of protected veteran listed above">I identify as one or more of the classifications of protected veteran listed above</label>
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
        assert any(mapping["label"] == "Race" and mapping["value"] == "Asian" for mapping in preview["result"]["mappings"])
        assert any(mapping["label"] == "Veteran Status" and mapping["value"] == "No" for mapping in preview["result"]["mappings"])
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
              <label>Zip / postal code<input name="zip"></label>
              <label>May we contact your current employer?<select name="contactEmployer"><option></option><option>Yes</option><option>No</option></select></label>
              <label>This position is based in the United States. Do you currently reside in commutable proximity to an ExampleCo office located in San Francisco or are you open to relocating?<input name="commutable"></label>
              <label>What is your current or previous job title?<input name="jobTitle"></label>
              <section id="employment">
                <h2>Employment</h2>
                <div class="employment-row">
                  <label>Company<input name="company[]"></label>
                  <label>Job Title<input name="title[]"></label>
                  <label>Location<input name="location[]"></label>
                  <label>From<input name="from[]"></label>
                  <label>To<input name="to[]"></label>
                  <label>Role Description<textarea name="description[]"></textarea></label>
                  <label>I currently work here<input type="checkbox" name="current[]"></label>
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
                  <label>School<input name="school[]"></label>
                  <label>Degree<select name="degree[]"><option>Select...</option><option>Bachelor's Degree</option></select></label>
                  <label>Discipline<input name="discipline[]"></label>
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
              <button id="veteran" type="button" aria-labelledby="veteran-label" aria-haspopup="listbox" aria-controls="veteran-options">Select...</button>
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
