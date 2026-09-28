"""Stage 2 of the model evaluation (see docs/ROADMAP_FULL_AUTONOMY.md, "Model choice").

Usage (from the repo root, with the NVIDIA key in autofill_extension/backend/env.private):
  .venv/bin/python docs/model_eval/model_screen.py                 # stage 1: which models answer at all
  .venv/bin/python docs/model_eval/model_eval.py <model> [<model>...]  # stage 2: graded replay
  .venv/bin/python docs/model_eval/essay_eval.py <model>           # essay grounding, one model per run
The CASES indexes refer to lines of docs/e2e_runs/model_eval/requests.jsonl (2026-09-26 run).

Replay REAL production mapper/auditor requests from the 2026-09-26 run
against candidate models (only the model id changes) and grade the raw answers
against the candidate's true facts. Critical checks = invented facts, wrong
demographics, name/identity corruption, values in the wrong field."""
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from model_screen import call  # same endpoint/key/params helper

DATA = Path(__file__).resolve().parents[1] / "e2e_runs" / "model_eval"  # gitignored: prompts contain profile data
REQS = (DATA / "requests.jsonl").read_text().splitlines()
LI = "linkedin.com/in/aaryashah127"
NOT_I = ("null_or_contains", "aarya")  # name fields must never become "I"

# (field index, check, critical?)
CASES = {
    "map_g2ops": (19, "mapper", [
        (1, ("eq", "aarya"), False), (2, ("eq", "shah"), False), (3, ("contains", "a268shah"), False),
        (5, ("contains", "224 ariana"), False),
        (11, ("null_or_not_contains", ["aarya", "shah"]), True),
        (13, ("prefix", "college - bachelor"), False), (14, ("contains", LI), False),
        (16, ("eq", "no"), False), (18, ("oneof_prefix", ["i have never", "no"]), False),
        (19, ("eq", "none"), False), (22, ("eq", "no"), False), (24, ("eq", "yes"), False),
        (27, ("eq", "no"), True), (28, ("contains", "negotiable"), False), (29, ("contains", "cognixion"), False),
        (31, ("null",), True), (32, ("null",), True), (34, ("null",), True), (35, ("null",), True),
        (37, ("null",), True), (38, ("null",), True), (40, ("null",), True), (41, ("null",), True),
        (43, ("nonnull",), False),
    ]),
    "map_discord": (6, "mapper", [
        (0, ("eq", "aarya"), False), (1, ("eq", "shah"), False), (5, ("any_contains", ["chicago", "bartlett"]), False),
        (8, ("null_or_contains", "waterloo"), True), (9, ("eq", "bachelor's degree"), False),
        (10, ("any_contains", ["statist", "math", "computer"]), False), (12, ("contains", LI), False),
        (13, ("contains", "aarya127.github.io"), False),
        (17, ("eq", "yes"), False), (18, ("eq", "male"), True), (19, ("eq", "asian"), True),
        (21, ("prefix", "no, i do not"), False), (22, ("eq", "man"), False),
        (23, ("eq", "south asian"), True), (24, ("eq", "no"), False),
    ]),
    "map_reddit": (26, "mapper", [
        (9, ("contains", LI), False), (11, ("contains", "cognixion"), False),
        (2, NOT_I, True), (14, ("prefix", "i agree"), False), (15, ("eq", "male"), True), (16, ("eq", "no"), False),
        (17, ("eq", "heterosexual"), True), (18, ("prefix", "no, i do not"), False),
        (20, ("contains", "south asian"), True),
    ]),
    "map_instacart": (36, "mapper", [
        (8, ("eq", "no"), False), (11, ("contains", LI), False),
        (13, ("eq", "yes"), False), (17, ("eq", "no"), False), (18, ("eq", "man"), True), (19, ("eq", "no"), False),
        (20, ("prefix", "asian or asian-american"), True), (21, ("eq", "yes"), False),
        (22, ("prefix", "no, i don't have"), False),
    ]),
    "map_openai": (37, "mapper", [
        (8, ("contains", "aarya shah"), True), (9, NOT_I, True), (10, ("contains", "a268shah"), False),
        (12, ("contains", "767"), False), (15, ("contains", LI), True),
    ]),
    "map_shieldai": (62, "mapper", [
        (2, ("prefix", "a lawful permanent resident"), True),
        (12, ("contains", "aarya shah"), True), (13, ("contains", "a268shah"), False),
        (30, ("contains", "software engineer"), False), (36, NOT_I, True),
    ]),
    "map_spotify": (53, "mapper", [
        (6, ("eq", "yes"), False), (15, ("contains", "aarya shah"), True), (16, ("contains", "a268shah"), False),
        (17, ("contains", "767"), False), (19, ("contains", "cognixion"), False),
    ]),
    "map_cohere": (61, "mapper", [
        (1, ("contains", "aarya shah"), True), (2, ("contains", "a268shah"), False), (5, ("contains", LI), False),
    ]),
    "map_givzey": (9, "mapper", [
        (2, ("contains", "767-8243"), True),
    ]),
    "map_databricks": (57, "mapper", [
        (0, ("contains", "none of the above"), True), (1, ("prefix", "not applicable"), False),
        (2, ("eq", "aarya"), False), (3, ("eq", "shah"), False), (4, NOT_I, True), (5, ("contains", "a268shah"), False),
        (8, ("any_contains", ["chicago", "bartlett"]), False), (11, ("contains", LI), False), (12, ("contains", "aarya127.github.io"), False),
    ]),
    "audit_g2ops": (23, "auditor", [
        (4, ("contains", "224 ariana"), False),
        (8, ("null_or_not_contains", ["aarya", "shah"]), True),
        (9, ("prefix", "i am a u.s. citizen/perman"), False), (12, ("eq", "yes"), False), (18, ("eq", "yes"), False),
        (22, ("null_or_zero",), True), (23, ("eq", "no"), True),
        (26, ("null",), True), (27, ("null",), True), (29, ("null",), True),
        (31, ("null",), True), (32, ("null",), True), (34, ("null",), True),
        (25, ("contains", "cognixion"), False),
    ]),
    "audit_instacart": (52, "auditor", [
        (19, ("prefix", "asian or asian-american"), True), (17, ("eq", "man"), True), (20, ("eq", "yes"), False),
        (7, ("eq", "no"), False),
    ]),
    "audit_reddit": (32, "auditor", [
        (17, ("eq", "heterosexual"), True), (20, ("contains", "south asian"), True), (15, ("eq", "male"), True),
        (16, ("eq", "no"), False),
    ]),
    "audit_discord": (11, "auditor", [
        (23, ("eq", "south asian"), True), (19, ("eq", "asian"), True), (18, ("eq", "male"), True),
        (22, ("eq", "man"), False), (24, ("eq", "no"), False),
    ]),
}
ESSAYS = {"map_discord": [11], "map_cohere": [6], "map_givzey": [3]}
LONG_CONTEXT = ("long_palantir", 38)  # ~137k-token production prompt: success/latency only


def norm(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return " | ".join(norm(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else ""
    return str(value).strip().lower()


def parse(content):
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def final_values(kind, data, request_event):
    values = {}
    if kind == "mapper":
        for m in (data or {}).get("mappings") or []:
            if isinstance(m, dict) and "index" in m:
                values[m["index"]] = m.get("value")
        return values
    current = {m["index"]: m.get("value") for m in request_event.get("mappings") or []}
    values = dict(current)
    for d in (data or {}).get("decisions") or []:
        if isinstance(d, dict) and d.get("action") in ("fill", "correct") and "index" in d:
            values[d["index"]] = d.get("value")
    for c in (data or {}).get("corrections") or []:
        if isinstance(c, dict) and "index" in c:
            values[c["index"]] = c.get("value")
    return values


def check(rule, value):
    v = norm(value)
    kind = rule[0]
    if kind == "eq":
        return v == rule[1]
    if kind == "prefix":
        return v.startswith(rule[1])
    if kind == "contains":
        return rule[1] in v
    if kind == "any_contains":
        return any(t in v for t in rule[1])
    if kind == "oneof_prefix":
        return any(v.startswith(t) for t in rule[1])
    if kind == "null":
        return v in ("", "null", "none", "n/a")
    if kind == "nonnull":
        return v != ""
    if kind == "null_or_contains":
        return v == "" or rule[1] in v
    if kind == "null_or_not_contains":
        return v == "" or not any(t in v for t in rule[1])
    if kind == "null_or_zero":
        return v in ("", "0", "none", "n/a", "0 years")
    raise ValueError(kind)


def run_model(model, reps=2, only=None, previous=None):
    out = previous or {"model": model, "cases": {}, "calls": []}
    if previous:
        out["calls"] = [c for c in out["calls"] if c["case"] not in only]
    for name, (req_index, kind, checks) in CASES.items():
        if only is not None and name not in only:
            continue
        event = json.loads(REQS[req_index])
        runs = []
        for _ in range(reps):
            res = call(model, event["request"], timeout=120)
            if res["status"] in (429, 503, "ReadTimeout", "ConnectionError"):
                time.sleep(5)
                res = call(model, event["request"], timeout=120)
            data = parse(res.get("content")) if res["status"] == 200 else None
            values = final_values(kind, data, event) if data is not None else None
            graded = []
            for field_index, rule, critical in checks:
                value = values.get(field_index) if values is not None else None
                graded.append({"field": field_index, "critical": critical, "value": norm(value)[:160],
                               "pass": values is not None and check(rule, value)})
            essays = {i: norm(values.get(i))[:700] for i in ESSAYS.get(name, [])} if values else {}
            runs.append({"status": res["status"], "secs": res["secs"], "parsed": data is not None, "finish": res.get("finish"),
                         "graded": graded, "essays": essays})
            out["calls"].append({"case": name, "status": res["status"], "secs": res["secs"], "parsed": data is not None, "finish": res.get("finish")})
        out["cases"][name] = runs
    if only is not None:
        (DATA / "eval" / f"{model.replace('/', '__')}.json").write_text(json.dumps(out, indent=1))
        return out
    name, req_index = LONG_CONTEXT
    res = call(model, json.loads(REQS[req_index])["request"], timeout=180)
    data = parse(res.get("content")) if res["status"] == 200 else None
    out["long"] = {"status": res["status"], "secs": res["secs"],
                   "mappings": len((data or {}).get("mappings") or []) if data else 0}
    (DATA / "eval" / f"{model.replace('/', '__')}.json").write_text(json.dumps(out, indent=1))
    return out


def summarize(out):
    total = passed = crit_total = crit_pass = 0
    consistent = comparable = 0
    for runs in out["cases"].values():
        for run in runs:
            for g in run["graded"]:
                total += 1
                passed += g["pass"]
                if g["critical"]:
                    crit_total += 1
                    crit_pass += g["pass"]
        if len(runs) == 2 and all(r["parsed"] for r in runs):
            for a, b in zip(runs[0]["graded"], runs[1]["graded"]):
                comparable += 1
                consistent += a["value"] == b["value"]
    secs = [c["secs"] for c in out["calls"] if c["status"] == 200]
    ok = sum(1 for c in out["calls"] if c["status"] == 200 and c["parsed"])
    return {
        "model": out["model"],
        "score": round(passed / total, 3) if total else 0,
        "critical": f"{crit_pass}/{crit_total}",
        "criticalFail": crit_total - crit_pass,
        "okCalls": f"{ok}/{len(out['calls'])}",
        "p50s": round(statistics.median(secs), 1) if secs else None,
        "maxs": max(secs) if secs else None,
        "over45s": sum(1 for s in secs if s > 45),
        "consistency": round(consistent / comparable, 3) if comparable else None,
        "long": out["long"],
    }


if __name__ == "__main__":
    (DATA / "eval").mkdir(exist_ok=True)
    models = sys.argv[1:]
    if models and models[0] == "--rerun-unparsed":
        def rerun(path):
            prev = json.loads(path.read_text())
            only = {c["case"] for c in prev["calls"] if c["status"] == 200 and not c["parsed"]}
            return run_model(prev["model"], only=only, previous=prev) if only else prev
        paths = [p for p in (DATA / "eval").glob("*__*.json") if "mistral-nemotron" not in p.name]
        with ThreadPoolExecutor(6) as ex:
            results = list(ex.map(rerun, paths))
    else:
        with ThreadPoolExecutor(6) as ex:
            results = list(ex.map(run_model, models))
    rows = sorted((summarize(r) for r in results), key=lambda r: (r["criticalFail"], -r["score"]))
    (DATA / "eval" / "summary.json").write_text(json.dumps(rows, indent=1))
    for r in rows:
        print(json.dumps(r))
