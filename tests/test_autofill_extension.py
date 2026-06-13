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
    Sample University January 2025 – April 2025
    Research Assistant Sample City, ST
    """

    parsed = parse_resume(text, Path("resume.pdf"), "resume.txt")
    work = parsed["resumeFacts"]["workExperience"]

    assert work[0]["company"] == "Example Labs"
    assert work[0]["title"] == "Machine Learning Engineer"
    assert work[0]["location"] == "Santa Barbara, CA"
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
            "label": "Consent",
            "options": [
                {"label": "Yes", "value": "yes"},
                {"label": "No", "value": "no"},
            ],
        },
    ]
    mappings = [
        {"index": 0, "value": "No", "confidence": 0.8, "source": "llm"},
        {"index": 1, "value": "Free text answer", "confidence": 0.8, "source": "llm"},
        {"index": 2, "value": "Maybe", "confidence": 0.8, "source": "llm"},
    ]

    filtered = server.enforce_option_values(mappings, fields)

    assert filtered == [
        {"index": 0, "value": "I am not a protected veteran", "confidence": 0.8, "source": "llm"},
        {"index": 1, "value": "Free text answer", "confidence": 0.8, "source": "llm"},
    ]


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
              <label>Will you require Stripe to sponsor you for a work permit now or in the future for the location(s) you selected in in your previous response? *<input name="sponsorship"></label>
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
              <label>Have you ever been employed by Stripe or a Stripe affiliate?<select name="stripeAffiliate"><option></option><option>Yes</option><option>No</option></select></label>
              <label>Do you opt-in to receive WhatsApp messages from Stripe Recruiting?<select name="whatsapp"><option></option><option>Yes</option><option>No</option></select></label>
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
        stripe_mapping = next(mapping for mapping in mappings if "Stripe affiliate" in mapping["label"])
        whatsapp_mapping = next(mapping for mapping in mappings if "WhatsApp messages" in mapping["label"])
        gender_mapping = next(mapping for mapping in mappings if mapping["label"] == "Gender")
        hispanic_mapping = next(mapping for mapping in mappings if "Hispanic/Latino" in mapping["label"])
        race_mapping = next(mapping for mapping in mappings if mapping["label"] == "Race")
        veteran_mapping = next(mapping for mapping in mappings if "protected veteran" in mapping["label"])
        assert k8s_mapping["value"] == "Yes"
        assert employer_mapping["value"] == "Example Labs"
        assert title_mapping["value"] == "Software Engineer"
        assert stripe_mapping["value"] == "No"
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
        assert page.locator("[name='stripeAffiliate']").input_value() == "No"
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
                "startMonth": "September",
                "startYear": "2025",
                "endMonth": "December",
                "endYear": "2025",
                "currentRole": False,
            },
            {
                "company": "Research Group",
                "title": "Research Assistant",
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
              <label>This position is based in the United States. Do you currently reside in commutable proximity to a Lyft Office located in San Francisco or are you open to relocating?<input name="commutable"></label>
              <label>What is your current or previous job title?<input name="jobTitle"></label>
              <section id="employment">
                <h2>Employment</h2>
                <div class="employment-row">
                  <label>Company name<input name="company[]"></label>
                  <label>Title<input name="title[]"></label>
                  <label>Start date month<select name="startMonth[]"><option></option><option>January</option><option>April</option><option>September</option></select></label>
                  <label>Start date year<input name="startYear[]"></label>
                  <label>End date month<select name="endMonth[]"><option></option><option>April</option><option>December</option></select></label>
                  <label>End date year<input name="endYear[]"></label>
                  <label>Current role<input type="checkbox" name="current[]"></label>
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
        assert page.locator("[name='startMonth[]']").nth(0).input_value() == "September"
        assert page.locator("[name='endYear[]']").nth(1).input_value() == "2025"
        assert page.locator("[name='company[]']").nth(1).input_value() == "Research Group"

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
