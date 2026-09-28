"""Essay grounding test through the backend's real narrative path
(server.compact_narrative_answer). Usage: essay_eval.py <model>  (one model per process,
because the backend reads NVIDIA_MODEL from the environment)."""
import json
import os
import re
import sys
import time
from pathlib import Path

MODEL = sys.argv[1]
os.environ["NVIDIA_MODEL"] = MODEL
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "autofill_extension/backend"))
import server  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "e2e_runs" / "model_eval"  # gitignored: prompts contain profile data
profile = json.loads((REPO / "autofill_extension/profile.private.json").read_text())["candidateProfile"]
resume = (REPO / "autofill_extension/generated/resume_text.private.txt").read_text().lower()

RAMP = "https://jobs.ashbyhq.com/ramp/bca0346c-b843-4795-96df-6091f51e421b/application"
QUESTIONS = [
    ("challenge", "Describe a challenging technical problem you solved and how you approached it.", None),
    ("ml_prod", "Tell us about your experience building production machine learning systems.", None),
    ("distributed", "What is your experience with distributed systems or large-scale backend services?", None),
    ("not_on_resume", "Tell us something about yourself that we wouldn't find on your resume.", None),
    ("leadership", "Describe a time you led a project or mentored others.", None),
    ("why_ramp", "Why are you interested in this role at Ramp?", RAMP),
]
# Claims that do not appear anywhere in the resume; any hit is an invented fact.
INVENTED = ["mentor", "open-source", "open source", "competitive program", "codeforces", "leetcode", "kaggle",
            "hackathon", "hiking", "volunteer", "content moderation", "content-moderation", "managed a team",
            "led a team", "team lead", "phd", "master's"]


def invented_numbers(text):
    return [n for n in re.findall(r"\d[\d,.]*\s?(?:%|ms|x|k|\+)?", text) if n.strip(" ,.") and n.strip(" ,.").split()[0].rstrip("%+xk") not in resume]


results = []
for key, question, url in QUESTIONS:
    field = {"index": 0, "label": question, "questionText": question, "tag": "textarea", "type": "textarea", "required": True}
    page = {"url": url or "https://job-boards.greenhouse.io/example/jobs/1", "title": "Software Engineer", "context": []}
    for rep in range(2):
        started = time.time()
        try:
            answer = server.compact_narrative_answer(field, profile, page)
            error = None
        except Exception as e:  # noqa: BLE001
            answer, error = None, f"{type(e).__name__}: {e}"[:200]
        text = (answer or "").lower()
        results.append({
            "q": key, "rep": rep, "secs": round(time.time() - started, 1), "answer": answer, "error": error,
            "inventedTerms": [t for t in INVENTED if t in text and t not in resume],
            "inventedNumbers": invented_numbers(text),
            "mentionsName": bool(re.search(r"\baarya\b|\bshah\b", text)),
        })
out = DATA / "essays" / f"{MODEL.replace('/', '__')}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(results, indent=1))
print(MODEL, "done", sum(1 for r in results if r["answer"]), "answers")
