"""Batch E2E: drive the ApplyPilot side panel (preview -> fill selected -> ask AI ->
refill) across many live job postings, using the real extension code + profile.

Never clicks a page's submit / next / sign-in buttons. The only page clicks are
cookie-banner dismissals and explicit entry buttons ("Apply", "Apply Manually",
"I'm interested") that open the application form.

Usage:
  .venv/bin/python docs/e2e_batch.py jobs.json --out docs/e2e_runs/<name> [--boards greenhouse,lever] [--limit N]

jobs.json is a list of {"board", "company", "title", "country", "url"} objects.
"""
import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "autofill_extension"

ENTRY_PATTERN = re.compile(
    r"^(apply( now| online| manually| here| to (this )?(job|position|role))?"
    r"|apply for (this|the) (job|position|role)( online)?"
    r"|i[’']?m interested|start( your| an)? application)$",
    re.I,
)
UNSAFE_CLICK_PATTERN = re.compile(r"submit|send|next|continue|sign|log ?in|register|create", re.I)
COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept All')",
    "button:has-text('Accept all')",
    "button:has-text('Accept Cookies')",
    "button:has-text('Allow all')",
    "button:has-text('Accept')",
]

REVIEW_STATE_JS = """() => {
  const cards = [];
  let section = '';
  for (const el of document.getElementById('reviewList').children) {
    if (el.classList.contains('review-heading')) { section = el.textContent.trim(); continue; }
    const label = el.querySelector('strong')?.textContent?.trim() || '';
    const value = el.querySelector('.review-value')?.textContent?.trim() || '';
    const checked = el.querySelector("input[type='checkbox']")?.checked ?? null;
    cards.push({ section, label, value, checked });
  }
  return {
    status: document.getElementById('assistantStatus').textContent.trim(),
    aiStatus: document.getElementById('aiStatusText')?.textContent?.trim() || '',
    fillDisabled: document.getElementById('fillSelectedButton').disabled,
    askAiDisabled: document.getElementById('askAiButton').disabled,
    cards
  };
}"""

CHAT_JS = """() => Array.from(document.querySelectorAll('.chat-message'))
  .map(m => (m.classList.contains('user') ? 'USER: ' : 'AGENT: ') + m.textContent.trim())"""

# Ground truth read straight from the page DOM (independent of the extension's own
# scanner), so accuracy review does not trust the tool under test.
PAGE_FIELDS_JS = """() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && (r.width > 0 || r.height > 0 || el.type === 'file');
  };
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && clean(l.textContent)) return clean(l.textContent);
    }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ');
      if (clean(t)) return clean(t);
    }
    const wrapLabel = el.closest('label');
    if (wrapLabel && clean(wrapLabel.textContent)) return clean(wrapLabel.textContent);
    const fs = el.closest('fieldset')?.querySelector('legend');
    if (fs && clean(fs.textContent)) return clean(fs.textContent);
    const q = el.closest('.field, .input-wrapper, [class*=question], [class*=Question], [data-automation-id^=formField]')?.querySelector('label, legend, [class*=label]');
    if (q && clean(q.textContent)) return clean(q.textContent);
    return el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '(unlabeled)';
  };
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (!visible(el) && type !== 'file' && type !== 'radio' && type !== 'checkbox') continue;
    if (el.getAttribute('aria-hidden') === 'true' && el.tabIndex === -1) continue;
    let value = el.value || '';
    if (el.tagName === 'SELECT') value = el.selectedOptions[0]?.textContent?.trim() || '';
    const control = el.closest('[class*="control"]') || el.closest('[class*="select"]');
    if (!value && control) {
      const sv = control.querySelectorAll('[class*="single-value"], [class*="singleValue"], [class*="multi-value__label"], [class*="multiValue"]');
      if (sv.length) value = Array.from(sv).map(n => clean(n.textContent)).join(' | ');
    }
    if (type === 'checkbox' || type === 'radio') value = el.checked ? `checked(${clean(labelFor(el)) || el.value})` : '';
    if (type === 'file') value = el.files?.length ? Array.from(el.files).map(f => f.name).join(',') : '';
    const group = (type === 'radio' || type === 'checkbox')
      ? clean(el.closest('fieldset')?.querySelector('legend')?.textContent || el.closest('[role=group],[role=radiogroup]')?.getAttribute('aria-label') || el.name)
      : '';
    const label = group ? `${group} :: ${labelFor(el)}` : labelFor(el);
    const key = label + '|' + type + '|' + (el.id || el.name || '');
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      label: label.slice(0, 200),
      kind: el.tagName.toLowerCase() + (type ? ':' + type : ''),
      value: String(value).slice(0, 300),
      required: el.required || el.getAttribute('aria-required') === 'true',
    });
  }
  return out;
}"""

FILLABLE_COUNT_JS = """() => Array.from(document.querySelectorAll(
  "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]), textarea, select, [role=combobox]"
)).filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }).length"""

BLOCKER_JS = """() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 30 && r.height > 20; };
  const text = (document.body?.innerText || '').slice(0, 20000);
  return {
    password: Array.from(document.querySelectorAll('input[type=password]')).some(vis),
    captchaFrames: Array.from(document.querySelectorAll('iframe')).map(f => ({ src: f.src || '', visible: vis(f) }))
      .filter(f => /recaptcha|hcaptcha|turnstile|challenges\\.cloudflare|arkoselabs|funcaptcha/i.test(f.src))
      .map(f => ({ src: f.src.replace(/\\?.*$/, '').slice(0, 90), visible: f.visible })),
    recaptchaBadge: !!document.querySelector('.grecaptcha-badge'),
    signInText: /\\b(sign in|log in|create (an )?account|already have an account)\\b/i.test(text),
    closedText: /(no longer (available|accepting|open)|job (is )?not found|position (has been )?(filled|closed)|page not found|this job has expired|job posting (has )?closed|could not find the job)/i.test(text),
    botChallenge: /just a moment|verify you are (a )?human|checking your browser|access denied/i.test(document.title + ' ' + text.slice(0, 500)),
  };
}"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_for(fn, timeout_s, poll=1.0):
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def all_frames_eval(page, script):
    results = []
    for frame in page.frames:
        try:
            results.append((frame, frame.evaluate(script)))
        except Exception:
            continue
    return results


def page_fields(page):
    fields = []
    for frame, frame_fields in all_frames_eval(page, PAGE_FIELDS_JS):
        prefix = "" if frame == page.main_frame else "[iframe] "
        for f in frame_fields:
            f["label"] = prefix + f["label"]
            fields.append(f)
    return fields


def fillable_count(page):
    return sum(count for _, count in all_frames_eval(page, FILLABLE_COUNT_JS))


def detect_blockers(page):
    merged = {"password": False, "captchaFrames": [], "recaptchaBadge": False, "signInText": False, "closedText": False, "botChallenge": False}
    for _, b in all_frames_eval(page, BLOCKER_JS):
        for key in ("password", "recaptchaBadge", "signInText", "closedText", "botChallenge"):
            merged[key] = merged[key] or bool(b.get(key))
        merged["captchaFrames"].extend(b.get("captchaFrames") or [])
    return merged


def dismiss_cookies(page):
    for sel in COOKIE_SELECTORS:
        try:
            button = page.locator(sel).first
            if button.is_visible(timeout=800):
                button.click(timeout=2000)
                time.sleep(0.8)
                return sel
        except Exception:
            continue
    return None


def find_entry_button(page, clicked=()):
    # Workday's Apply opens a modal while the original Apply stays visible behind it,
    # so prefer "Apply Manually"-style choices and never re-click a text we already used.
    matches = []
    for frame in page.frames:
        try:
            candidates = frame.locator("a, button, input[type=button], [role=button]")
            for index in range(min(candidates.count(), 400)):
                el = candidates.nth(index)
                try:
                    if not el.is_visible():
                        continue
                    text = (el.inner_text(timeout=300) or el.get_attribute("value") or el.get_attribute("aria-label") or "")
                    text = re.sub(r"\s+", " ", text).strip().strip("→›>").strip()
                    if ENTRY_PATTERN.match(text) and not UNSAFE_CLICK_PATTERN.search(text):
                        matches.append((el, text))
                except Exception:
                    continue
        except Exception:
            continue
    fresh = [m for m in matches if m[1].lower() not in clicked]
    preferred = [m for m in fresh if re.search(r"manually|start", m[1], re.I)]
    return (preferred or fresh or [(None, None)])[0]


def open_application(ctx, page, job, record):
    """Click through entry buttons until a form (or a wall) shows. Returns the page
    that holds the application, which may be a newly opened tab."""
    for _ in range(3):
        if fillable_count(page) >= 4:
            break
        button, text = find_entry_button(page, {e["text"].lower() for e in record["entry"]})
        if not button:
            break
        before = set(ctx.pages)
        try:
            button.click(timeout=5000)
        except Exception as e:
            record["entry"].append({"text": text, "error": type(e).__name__})
            break
        record["entry"].append({"text": text})
        time.sleep(5)
        opened = [p for p in ctx.pages if p not in before]
        if opened:
            page = opened[-1]
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
            time.sleep(3)
        dismiss_cookies(page)
    wait_for_form_settled(page)
    return page


def wait_for_form_settled(page, max_s=25, quiet_s=4):
    """SPA forms (SmartRecruiters, Workday) render well after domcontentloaded; wait
    until the visible field count stops changing before scanning."""
    last, last_change, start = -1, time.time(), time.time()
    while time.time() - start < max_s:
        count = fillable_count(page)
        if count != last:
            last, last_change = count, time.time()
        elif count > 0 and time.time() - last_change >= quiet_s:
            return count
        time.sleep(1)
    return last


def job_tab_id(sw, page_url):
    return sw.evaluate(
        """async (url) => {
          const tabs = await chrome.tabs.query({});
          const exact = tabs.find(t => t.url === url || t.pendingUrl === url);
          if (exact) return exact.id;
          const origin = new URL(url).origin;
          const same = tabs.filter(t => (t.url || '').startsWith(origin));
          return same.length ? same[same.length - 1].id : null;
        }""",
        page_url,
    )


def open_panel(ctx, ext_id, tab_id):
    panel = ctx.new_page()
    panel.goto(f"chrome-extension://{ext_id}/src/assistant.html", wait_until="domcontentloaded", timeout=20000)
    time.sleep(1.5)
    # The panel normally rides along with the active tab; here it lives in its own
    # tab, so route its active-tab lookup at the job tab.
    panel.evaluate(
        """(tabId) => {
          const real = chrome.tabs.query.bind(chrome.tabs);
          chrome.tabs.query = async (info) => {
            if (info && info.active && info.currentWindow) return [await chrome.tabs.get(tabId)];
            return real(info);
          };
        }""",
        tab_id,
    )
    return panel


def panel_state(panel):
    return panel.evaluate(REVIEW_STATE_JS)


def run_panel_step(panel, name, button_id, timeout_s, record, wait_ai=False):
    state = panel_state(panel)
    disabled_key = {"fillSelectedButton": "fillDisabled", "askAiButton": "askAiDisabled"}.get(button_id)
    if disabled_key and state[disabled_key]:
        record["steps"].append({"name": name, "skipped": "button disabled"})
        return False

    chat_before = len(panel.evaluate(CHAT_JS))
    started = time.time()
    panel.evaluate("(id) => document.getElementById(id).click()", button_id)
    time.sleep(2)

    def done():
        s = panel_state(panel)
        if wait_ai:
            return s["aiStatus"] == "AI idle" and s["status"] != "Working"
        return s["status"] != "Working"

    finished = wait_for(done, timeout_s, poll=2.0)
    time.sleep(2)
    state = panel_state(panel)
    record["steps"].append({
        "name": name,
        "finished": finished,
        "durationS": round(time.time() - started, 1),
        "status": state["status"],
        "chat": panel.evaluate(CHAT_JS)[chat_before:],
    })
    return finished


def summarize(record, state, chat):
    joined = "\n".join(chat)
    scanned = re.findall(r"I scanned (\d+) field\(s\) and mapped (\d+)", joined)
    unresolved = re.findall(r"Still unresolved required field\(s\): (\d+)", joined)
    cards = state["cards"]
    ai_answers = [
        {"label": c["label"][:140], "value": c["value"][:200]}
        for c in cards
        if re.search(r"\((llm|grounded-llm|ai|audit)[^)]*\)", c["value"])
    ]
    record["summary"] = {
        "firstScan": {"scanned": int(scanned[0][0]), "mapped": int(scanned[0][1])} if scanned else None,
        "unresolvedRequired": int(unresolved[-1]) if unresolved else None,
        "needsAnswerCards": [c["label"][:140] for c in cards if c["section"].startswith("I need your answer")],
        "mismatches": [f'{c["label"][:100]}: {c["value"][:160]}' for c in cards if c["section"] == "Verification" and "Expected" in c["value"]],
        "readyUnfilled": [c["label"][:140] for c in cards if c["section"] == "Ready to fill" and c["checked"]],
        "aiAnswerCards": ai_answers,
        "errorMessages": [m for m in chat if re.search(r"error|failed|could not|couldn't|timed out|unavailable", m, re.I)][:15],
    }
    fields = record.get("finalPageFields") or []
    record["summary"]["requiredEmptyOnPage"] = [f["label"] for f in fields if f.get("required") and not f.get("value")]


def run_job(ctx, sw, ext_id, job, out_dir, index):
    slug = f"{index:02d}_{job['board']}_{re.sub(r'[^a-z0-9]+', '-', job.get('company', 'x').lower())[:30]}"
    record = {"slug": slug, "job": job, "entry": [], "steps": [], "blockers": None}
    started = time.time()
    page = ctx.new_page()
    for extra in [p for p in ctx.pages if p != page]:
        try:
            extra.close()
        except Exception:
            pass

    panel = None
    try:
        response = page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
        record["httpStatus"] = response.status if response else None
        time.sleep(5)
        dismiss_cookies(page)
        page = open_application(ctx, page, job, record)
        record["applicationUrl"] = page.url
        record["blockers"] = detect_blockers(page)
        record["fillableBefore"] = fillable_count(page)
        blockers = record["blockers"]
        log(f"  landed on {page.url[:100]} fillable={record['fillableBefore']} blockers="
            f"{ {k: v for k, v in blockers.items() if v} }")

        tab_id = job_tab_id(sw, page.url)
        panel = open_panel(ctx, ext_id, tab_id)
        panel.evaluate("(c) => document.querySelector(`[data-country=\"${c}\"]`)?.click()", job.get("country") or "usa")
        time.sleep(1)

        run_panel_step(panel, "preview", "previewButton", 150, record)
        walled = blockers["password"] or blockers["botChallenge"] or blockers["closedText"]
        if not walled:
            run_panel_step(panel, "fill", "fillSelectedButton", 150, record)
            run_panel_step(panel, "askai", "askAiButton", 600, record, wait_ai=True)
            if panel_state(panel)["cards"] and any(
                c["section"] == "Ready to fill" and c["checked"] for c in panel_state(panel)["cards"]
            ):
                run_panel_step(panel, "refill", "fillSelectedButton", 150, record)
        else:
            record["steps"].append({"name": "fill", "skipped": "blocked (login wall / bot challenge / closed)"})

        state = panel_state(panel)
        chat = panel.evaluate(CHAT_JS)
        record["finalCards"] = state["cards"]
        record["finalChat"] = chat
        record["finalPageFields"] = page_fields(page)
        summarize(record, state, chat)
        try:
            page.screenshot(path=str(out_dir / f"{slug}_page.png"), full_page=True, timeout=30000)
            panel.screenshot(path=str(out_dir / f"{slug}_panel.png"), full_page=True, timeout=30000)
        except Exception:
            pass
    except Exception as e:
        record["fatal"] = f"{type(e).__name__}: {e}"[:500]
        record["traceback"] = traceback.format_exc()[-2000:]
        log(f"  FATAL {record['fatal'][:200]}")
    record["durationS"] = round(time.time() - started, 1)
    (out_dir / f"{slug}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
    return record


def launch(p, profile_dir):
    return p.chromium.launch_persistent_context(
        str(profile_dir),
        headless=False,
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--window-size=1440,1100"],
    )


def seed_extension(ctx, candidate, settings):
    sw = None
    deadline = time.time() + 20
    while time.time() < deadline and not sw:
        sws = [w for w in ctx.service_workers if w.url.startswith("chrome-extension://")]
        sw = sws[0] if sws else None
        time.sleep(0.5)
    sw = sw or ctx.wait_for_event("serviceworker", timeout=20000)
    ext_id = sw.url.split("/")[2]
    time.sleep(2)
    # onInstalled writes sample defaults asynchronously; re-seed until read-back matches.
    for _ in range(10):
        sw.evaluate("(data) => chrome.storage.local.set(data)", {"candidateProfile": candidate, "settings": settings})
        time.sleep(1)
        check = sw.evaluate("() => chrome.storage.local.get(['candidateProfile'])")
        if check["candidateProfile"].get("email") == candidate["email"]:
            return sw, ext_id
    raise RuntimeError("could not seed candidateProfile")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("jobs", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--boards", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="skip jobs that already have a result file")
    args = parser.parse_args()

    jobs = json.loads(args.jobs.read_text())
    indexed = list(enumerate(jobs))
    if args.boards:
        wanted = {b.strip() for b in args.boards.split(",")}
        indexed = [(i, j) for i, j in indexed if j["board"] in wanted]
    if args.limit:
        indexed = indexed[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    if args.resume:
        done = {f.name.split("_", 1)[0] for f in args.out.glob("[0-9][0-9]_*.json")}
        indexed = [(i, j) for i, j in indexed if f"{i:02d}" not in done]

    profile_data = json.loads((EXT / "profile.private.json").read_text())
    candidate = profile_data["candidateProfile"]
    settings = {
        **profile_data.get("settings", {}),
        "backendBaseUrl": "http://127.0.0.1:8000",
        "targetCountry": "usa",
        "autoMapAmbiguousFields": True,
        "requireReviewBeforeSubmit": True,
    }

    with sync_playwright() as p:
        ctx = launch(p, args.out / f"chrome-profile-{args.boards.replace(',', '_') or 'all'}")
        try:
            sw, ext_id = seed_extension(ctx, candidate, settings)
            log(f"extension {ext_id} seeded; {len(indexed)} job(s)")
            for index, job in indexed:
                log(f"[{index}] {job['board']} | {job.get('company')} | {job.get('title')}")
                record = run_job(ctx, sw, ext_id, job, args.out, index)
                s = record.get("summary") or {}
                log(f"  done in {record['durationS']}s unresolved={s.get('unresolvedRequired')} "
                    f"requiredEmpty={len(s.get('requiredEmptyOnPage') or [])} mismatches={len(s.get('mismatches') or [])}")
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
