"""Stage 1: screen every chat model on the NVIDIA endpoint with a REAL production
mapper request (Cohere Ashby form, ~10k tokens) using the backend's exact params."""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parents[1] / "e2e_runs" / "model_eval"  # gitignored: prompts contain profile data
REPO = Path(__file__).resolve().parents[2]
URL = "https://integrate.api.nvidia.com/v1/chat/completions"

CANDIDATES = [
    "01-ai/yi-large", "ai21labs/jamba-1.5-large-instruct", "databricks/dbrx-instruct",
    "deepseek-ai/deepseek-v4.1-flash", "google/gemma-3-12b-it", "google/gemma-4-31b-it",
    "ibm/granite-3.0-8b-instruct", "meta/llama-3.2-90b-vision-instruct", "meta/muse-glimmer-30b",
    "microsoft/phi-3.5-moe-instruct", "mistralai/mistral-large", "mistralai/mistral-large-2-instruct",
    "mistralai/mistral-nemotron", "mistralai/mixtral-8x22b-v0.1", "moonshotai/kimi-k3",
    "nv-mistralai/mistral-nemo-12b-instruct", "nvidia/llama-3.1-nemotron-51b-instruct",
    "nvidia/llama-3.1-nemotron-70b-instruct", "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "nvidia/llama3-chatqa-1.5-70b", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning-30b-a3b", "nvidia/nemotron-4-340b-instruct",
    "nvidia/nemotron-nano-3-30b-a3b", "nvidia/ising-calibration-1.5-31b", "openai/gpt-oss-20b",
    "poolside/laguna-xs-2.1", "writer/palmyra-creative-122b", "z-ai/glm-5.3", "z-ai/glm-5.3-flash",
    "zyphra/zamba2-7b-instruct", "mistralai/mistral-7b-instruct-v0.3",
]


def api_key():
    for line in (REPO / "autofill_extension/backend/env.private").read_text().splitlines():
        if line.startswith("NVIDIA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")


KEY = api_key()


def load_request(index):
    lines = (DATA / "requests.jsonl").read_text().splitlines()
    return json.loads(lines[index])["request"]


def call(model, body, timeout=90):
    body = {**body, "model": model}
    started = time.time()
    try:
        r = requests.post(URL, headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}, json=body, timeout=timeout)
    except Exception as e:
        return {"status": type(e).__name__, "secs": round(time.time() - started, 1)}
    out = {"status": r.status_code, "secs": round(time.time() - started, 1)}
    if r.ok:
        try:
            choice = r.json()["choices"][0]
            msg = choice["message"]
            content = msg.get("content") or msg.get("reasoning_content") or ""
            out["content"] = content
            out["finish"] = choice.get("finish_reason")
            text = content.strip()
            if text.startswith("```"):
                text = text.strip("`").split("\n", 1)[-1]
            start, end = text.find("{"), text.rfind("}")
            parsed = json.loads(text[start:end + 1]) if start >= 0 else None
            out["json"] = isinstance(parsed, dict)
            out["hasMappings"] = isinstance(parsed, dict) and isinstance(parsed.get("mappings"), list)
            out["usage"] = r.json().get("usage")
        except Exception as e:
            out["json"] = False
            out["parseError"] = str(e)[:120]
    else:
        out["error"] = r.text[:200]
    return out


def screen(model):
    body = load_request(61)  # Cohere mapper, production params
    results = []
    for attempt in range(2):
        res = call(model, body)
        if res["status"] == 400 and attempt == 0:
            # record whether the model only rejects our extra params
            stripped = {k: v for k, v in body.items() if k not in ("chat_template_kwargs", "response_format")}
            res["retryWithoutParams"] = call(model, stripped)
        results.append(res)
    return model, results


if __name__ == "__main__":
    models = sys.argv[1:] or CANDIDATES
    with ThreadPoolExecutor(6) as ex:
        out = dict(ex.map(screen, models))
    (DATA / "screen_results.json").write_text(json.dumps(out, indent=1))
    for model, results in out.items():
        summary = [f"{r['status']}/{r['secs']}s/json={r.get('json')}/map={r.get('hasMappings')}" for r in results]
        extra = results[0].get("retryWithoutParams")
        if extra:
            summary.append(f"noParams:{extra['status']}/json={extra.get('json')}")
        print(f"{model:50} {'  '.join(summary)}")
