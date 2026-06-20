from pathlib import Path

from application_agent.agent.answer_generator import answer_option_question, parse_option_answer
from application_agent.agent.detector import detect_ats
from application_agent.agent.field_kinds import classify_field_kind, resolve_field_kind
from application_agent.agent.field_mapper import map_field
from application_agent.agent.planner import build_fill_plan
from application_agent.agent.profile_loader import normalize_profile
from application_agent.agent.verifier import verify_fill_plan
from application_agent.ats.base import is_final_submit_text, option_matches, requires_dropdown_option_click, value_allowed_by_field_options
from application_agent.ats.router import get_adapter


def test_detect_ats_routes_common_platforms():
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_ats("https://jobs.lever.co/acme/1") == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/acme/1") == "ashby"
    assert detect_ats("https://acme.myworkdayjobs.com/job/1") == "workday"
    assert detect_ats("https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1") == "oracle"
    assert detect_ats("https://oraclecloud.taleo.net/careersection/jobdetail.ftl") == "taleo"
    assert detect_ats("https://careers-company.icims.com/jobs/123/apply") == "icims"
    assert detect_ats("https://jobs.smartrecruiters.com/Acme/123") == "smartrecruiters"
    assert detect_ats("https://career012.successfactors.eu/career?company=acme") == "successfactors"
    assert detect_ats("https://example.com/apply") == "generic"


def test_router_returns_first_class_ats_adapters():
    assert get_adapter("oracle").name == "oracle"
    assert get_adapter("taleo").name == "taleo"
    assert get_adapter("icims").name == "icims"
    assert get_adapter("smartrecruiters").name == "smartrecruiters"
    assert get_adapter("successfactors").name == "successfactors"


def test_custom_ats_dropdowns_require_clicking_actual_options():
    class Page:
        def __init__(self, url: str) -> None:
            self.url = url

    class Locator:
        def __init__(self, url: str) -> None:
            self.page = Page(url)

    for url in [
        "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience",
        "https://oraclecloud.taleo.net/careersection/jobdetail.ftl",
        "https://careers-company.icims.com/jobs/123",
        "https://jobs.smartrecruiters.com/Acme/123",
        "https://career012.successfactors.eu/career",
    ]:
        assert requires_dropdown_option_click({"locator": Locator(url)})


def test_normalize_profile_uses_target_address_and_resume_path(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    resume_dir = root / "autofill_extension/resumes"
    monkeypatch.setattr(Path, "exists", lambda self: str(self).endswith("resume.pdf"))

    profile = normalize_profile(
        {
            "firstName": "Test",
            "lastName": "Candidate",
            "resumeFileName": "resume.pdf",
            "workExperience": [
                {
                    "company": "Example Labs",
                    "title": "Software Engineer",
                    "startMonth": "January",
                    "startYear": "2025",
                    "endMonth": "April",
                    "endYear": "2025",
                }
            ],
            "addresses": {
                "usa": {
                    "line1": "1 Test Way",
                    "city": "Chicago",
                    "state": "IL",
                    "zipCode": "60601",
                    "country": "United States",
                }
            },
            "answers": {"sponsorship": "No"},
        },
        {"targetCountry": "usa"},
    )

    assert profile["first_name"] == "Test"
    assert profile["address"]["country"] == "United States"
    assert profile["location"] == "Chicago, IL"
    assert profile["resume_path"] == str(resume_dir / "resume.pdf")
    assert profile["work_experience"][0]["company"] == "Example Labs"

    profile_with_location_override = normalize_profile(
        {
            "firstName": "Test",
            "lastName": "Candidate",
            "addresses": {
                "usa": {
                    "city": "Bartlett",
                    "state": "IL",
                    "country": "United States",
                }
            },
            "answers": {"usaLocation": "Chicago, IL"},
        },
        {"targetCountry": "usa"},
    )
    assert profile_with_location_override["location"] == "Chicago, IL"


def test_field_mapper_handles_greenhouse_style_questions():
    profile = {
        "first_name": "Test",
        "last_name": "Candidate",
        "email": "test@example.com",
        "current_or_previous_employer": "Example Labs",
        "current_or_previous_job_title": "Software Engineer",
        "needs_sponsorship": "No",
        "work_authorization": "Yes",
        "veteran_status": "No",
        "address": {
            "state": "IL",
        },
        "answers": {
            "previouslyEmployedByCompany": "No",
            "recruitingMessages": "No",
        },
        "demographics": {
            "race": "Asian",
            "hispanicLatino": "No",
            "gender": "Male",
            "sexualOrientation": "Straight",
        },
        "auto_fill_sensitive_fields": True,
    }

    assert map_field({"label": "First Name"}, profile) == ("Test", "field-kind")
    assert map_field({"label": "Who is your current or previous employer?"}, profile) == ("Example Labs", "field-kind")
    assert map_field({"label": "What is your current or previous job title?"}, profile) == ("Software Engineer", "field-kind")
    assert map_field({"label": "Have you ever been employed by ExampleCo or an ExampleCo affiliate?"}, profile) == ("No", "rule")
    profile["needs_sponsorship"] = "I do not require sponsorship"
    assert map_field({"label": "Will you now, or in the future, require sponsorship to work in the United States?"}, profile) == ("No", "rule")
    assert map_field({"label": "Will you require our assistance with work authorization now or in the future?"}, profile) == ("No", "rule")
    assert map_field({"label": "Are you legally eligible to work in the country of employment?"}, profile) == ("Yes", "rule")
    assert map_field({"question_text": "Have you previously been DIRECTLY employed with Example ParentCo AG or Example Affiliate Inc.?"}, profile) == ("No", "rule")
    assert map_field({"label": "Will you need relocation assistance to work at this role's specified location?"}, profile) == ("No", "rule")
    assert map_field({"label": "Do you opt-in to receive WhatsApp messages from ExampleCo Recruiting?"}, profile) == ("No", "rule")
    assert map_field({"label": "Are you Hispanic/Latino?"}, profile) == ("No", "sensitive")
    assert map_field({"label": "Race"}, profile) == ("Asian", "sensitive")
    assert map_field({"label": "Gender"}, profile) == ("Male", "sensitive")
    assert map_field({"label": "Sexual Orientation"}, profile) == ("Straight", "sensitive")
    assert map_field({"label": "Country Phone Code"}, profile) == ("+1", "rule")
    assert map_field({"label": "What U.S State do you currently reside in?"}, profile) == ("Illinois", "field-kind")
    assert map_field({"label": "If yes, please state their name and job title"}, profile) is None
    assert map_field({"label": "Subscribe to job alerts and marketing emails?"}, profile) == ("No", "rule")
    assert map_field({"label": "Do you accept the Terms and Conditions?"}, profile) == ("Yes", "rule")
    assert map_field({"label": "By selecting Yes, I certify that this application is true and correct."}, profile) == ("Yes", "rule")


def test_option_matching_handles_long_dropdown_labels_without_male_female_collision():
    assert option_matches("I am not a protected Veteran", "", "no")
    assert option_matches("No, I am not Hispanic or Latino", "", "no")
    assert option_matches("Asian (Not Hispanic or Latino)", "", "asian")
    assert option_matches("Male", "", "male")
    assert not option_matches("Female", "", "male")
    assert option_matches("Straight", "", "straight")
    assert option_matches("Canada (+1)", "", "Canada (+1)")
    assert option_matches("No", "", "I do not require sponsorship")


def test_field_mapper_does_not_cross_fill_links_from_surrounding_text():
    profile = {
        "linkedin": "https://linkedin.example/candidate",
        "github": "https://github.example/candidate",
        "portfolio": "https://portfolio.example",
        "school": "University of Waterloo",
        "current_or_previous_employer": "Example Labs",
    }

    assert map_field(
        {
            "label": "Work Experience: Current/Previous Employer",
            "surrounding_text": "LinkedIn Profile Website URL GitHub",
        },
        profile,
    ) == ("Example Labs", "field-kind")
    assert map_field(
        {
            "label": "Education: Last University Attended",
            "surrounding_text": "LinkedIn Profile Website URL GitHub",
        },
        profile,
    ) == ("University of Waterloo", "field-kind")
    assert map_field(
        {
            "label": "Website",
            "surrounding_text": "Education Work Experience",
        },
        profile,
    ) == ("https://portfolio.example", "field-kind")


def test_field_kind_classifier_resolves_core_profile_fields_before_heuristics():
    profile = {
        "linkedin": "https://linkedin.example/candidate",
        "email": "candidate@example.com",
        "address": {"state": "IL"},
    }
    linkedin_field = {"label": "LinkedIn Profile", "surrounding_text": "Website Email Phone"}
    state_field = {"label": "What U.S State do you currently reside in?"}

    assert classify_field_kind(linkedin_field) == "links.linkedin"
    assert resolve_field_kind(classify_field_kind(linkedin_field), profile) == "https://linkedin.example/candidate"
    assert classify_field_kind(state_field) == "address.state"
    assert resolve_field_kind(classify_field_kind(state_field), profile) == "Illinois"


def test_fill_plan_constrains_options_and_reports_skipped_fields():
    fields = [
        {"index": 0, "label": "First Name"},
        {
            "index": 1,
            "label": "Are you a protected veteran?",
            "options": [{"label": "I am not a protected veteran"}, {"label": "I decline to self-identify"}],
        },
        {"index": 2, "label": "Unmapped custom field"},
    ]
    profile = {"first_name": "Aarya", "veteran_status": "No"}
    plan = build_fill_plan(fields, profile, constrain_value=value_allowed_by_field_options)

    assert [(item["field"]["index"], item["value"], item["source"]) for item in plan["items"]] == [
        (0, "Aarya", "field-kind"),
        (1, "I am not a protected veteran", "rule"),
    ]
    assert plan["skipped"] == [{"index": 2, "label": "Unmapped custom field", "reason": "no_mapping"}]


def test_verifier_reports_mismatched_filled_values():
    class Locator:
        def __init__(self, value: str) -> None:
            self.value = value

        def input_value(self, timeout: int = 500) -> str:
            return self.value

    plan_items = [
        {"field": {"index": 0, "label": "Email", "locator": Locator("candidate@example.com")}, "value": "candidate@example.com", "fieldKind": "contact.email"},
        {"field": {"index": 1, "label": "LinkedIn", "locator": Locator("https://portfolio.example")}, "value": "https://linkedin.example", "fieldKind": "links.linkedin"},
    ]
    verification = verify_fill_plan(plan_items)

    assert verification["matched"] == 1
    assert verification["mismatched"][0]["label"] == "LinkedIn"
    assert verification["mismatched"][0]["fieldKind"] == "links.linkedin"


def test_agent_only_allows_values_from_field_options():
    field = {
        "label": "Veteran Status",
        "options": [
            {"label": "I identify as one or more classifications of protected veteran", "value": "protected"},
            {"label": "I am not a protected veteran", "value": "not_protected"},
        ],
    }

    assert value_allowed_by_field_options(field, "No") == "I am not a protected veteran"
    assert value_allowed_by_field_options(field, "Some unrelated answer") is None
    assert value_allowed_by_field_options({"label": "Why us?", "options": []}, "Free text") == "Free text"
    assert value_allowed_by_field_options(
        {"label": "Relocation Assistance", "options": [{"label": "Yes"}, {"label": "No"}]},
        "Open to relocation",
    ) == "No"
    disability_field = {
        "label": "Do you have a disability?",
        "options": [
            {"label": "Yes, I have a disability, or have a history/record of having a disability"},
            {"label": "No, I don't have a disability, or a history/record of having a disability"},
            {"label": "I don't wish to answer"},
        ],
    }
    assert value_allowed_by_field_options(disability_field, "No, I do not have a disability and have not had one in the past") is None
    assert (
        value_allowed_by_field_options(disability_field, "No, I don't have a disability, or a history/record of having a disability")
        == "No, I don't have a disability, or a history/record of having a disability"
    )


def test_final_submit_detection_is_conservative():
    assert is_final_submit_text("Submit Application")
    assert is_final_submit_text("Complete Application")
    assert not is_final_submit_text("Save and Continue")


def test_option_answer_generation_only_accepts_exact_dropdown_options(monkeypatch):
    options = [{"label": "Yes"}, {"label": "No"}, {"label": "I prefer not to answer"}]

    monkeypatch.setattr(
        "application_agent.agent.answer_generator.generate_option_answer_with_llm",
        lambda question, labels, profile: "Nope",
    )
    assert answer_option_question("Are you affiliated with any group?", options, {}) == ("", "needs_manual_answer")

    monkeypatch.setattr(
        "application_agent.agent.answer_generator.generate_option_answer_with_llm",
        lambda question, labels, profile: "No",
    )
    assert answer_option_question("Are you affiliated with any group?", options, {}) == ("No", "generated_review_required")


def test_option_answer_parser_accepts_json_only_shape():
    assert parse_option_answer('{"answer":"No"}') == "No"
    assert parse_option_answer('```json\n{"answer":"Yes"}\n```') == "Yes"
