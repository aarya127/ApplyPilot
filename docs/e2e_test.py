"""E2E test: drive the ApplyPilot side panel (preview -> fill selected -> ask ai)
against the real Point72 Greenhouse posting, using the real extension code + profile."""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path("/Users/aaryas127/Documents/GitHub/hr_system")
EXT = REPO / "autofill_extension"
SCRATCH = Path(__file__).parent
JOB_URL = "https://job-boards.greenhouse.io/point72/jobs/8651402002?gh_jid=8651402002&jobCode=PIT-0013638&location=null"

RESULTS = {"steps": []}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def save_results():
    (SCRATCH / "e2e_results.json").write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False))


def chat_log(assistant):
    return assistant.evaluate(
        "() => Array.from(document.querySelectorAll('.chat-message')).map(m => (m.classList.contains('user') ? 'USER: ' : 'AGENT: ') + m.textContent.trim())"
    )


def review_state(assistant):
    return assistant.evaluate(
        """() => {
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
    )


def page_field_values(job):
    return job.evaluate(
        """() => {
        const out = [];
        const seen = new Set();
        const labelFor = (el) => {
          if (el.id) {
            const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (l) return l.textContent.replace(/\\s+/g, ' ').trim();
          }
          const wrap = el.closest('div');
          const lab = el.closest('.field, .input-wrapper, [class*=question]')?.querySelector('label');
          if (lab) return lab.textContent.replace(/\\s+/g, ' ').trim();
          return el.getAttribute('aria-label') || el.name || el.id || '(unlabeled)';
        };
        for (const el of document.querySelectorAll('input, textarea, select')) {
          if (el.type === 'hidden' || !el.offsetParent) continue;
          let value = el.value || '';
          let kind = el.tagName.toLowerCase() + (el.type ? ':' + el.type : '');
          if (el.tagName === 'SELECT') value = el.selectedOptions[0]?.textContent?.trim() || '';
          // react-select renders the chosen value outside the input
          const control = el.closest('[class*="control"]') || el.closest('[class*="select"]');
          if (!value && control) {
            const sv = control.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value"]');
            if (sv) value = sv.textContent.trim();
          }
          if (el.type === 'checkbox' || el.type === 'radio') value = el.checked ? `checked(${el.value})` : '';
          if (el.type === 'file') value = el.files?.length ? Array.from(el.files).map(f => f.name).join(',') : '';
          const label = labelFor(el);
          const key = label + '|' + kind + '|' + (el.id || el.name || '');
          if (seen.has(key)) continue;
          seen.add(key);
          out.push({ label, kind, value, required: el.required || el.getAttribute('aria-required') === 'true' });
        }
        return out;
      }"""
    )


def wait_for(fn, desc, timeout_s, poll=1.0):
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            if fn():
                return True
        except Exception as e:
            log(f"  poll error ({desc}): {e}")
        time.sleep(poll)
    log(f"  TIMEOUT waiting for {desc} after {timeout_s}s")
    return False


def snapshot(name, assistant, job):
    state = {
        "name": name,
        "chat": chat_log(assistant),
        "review": review_state(assistant),
        "pageFields": page_field_values(job),
    }
    RESULTS["steps"].append(state)
    save_results()
    job.screenshot(path=str(SCRATCH / f"{name}_page.png"), full_page=True)
    assistant.screenshot(path=str(SCRATCH / f"{name}_panel.png"), full_page=True)
    log(f"snapshot saved: {name}")
    return state


def main():
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
        ctx = p.chromium.launch_persistent_context(
            str(SCRATCH / "chrome-profile"),
            headless=False,
            args=[
                f"--disable-extensions-except={EXT}",
                f"--load-extension={EXT}",
                "--window-size=1440,1100",
            ],
        )
        try:
            # extension service worker -> extension id
            sw = None
            deadline = time.time() + 20
            while time.time() < deadline:
                sws = [w for w in ctx.service_workers if w.url.startswith("chrome-extension://")]
                if sws:
                    sw = sws[0]
                    break
                time.sleep(0.5)
            if not sw:
                sw = ctx.wait_for_event("serviceworker", timeout=20000)
            ext_id = sw.url.split("/")[2]
            log(f"extension id: {ext_id}")
            time.sleep(2)  # let onInstalled defaults land first, then overwrite

            sw.evaluate(
                "(data) => chrome.storage.local.set(data)",
                {"candidateProfile": candidate, "settings": settings},
            )
            check = sw.evaluate("() => chrome.storage.local.get(['candidateProfile','settings'])")
            log(f"storage seeded: profile email={check['candidateProfile'].get('email')}, country={check['settings'].get('targetCountry')}")

            job = ctx.pages[0] if ctx.pages else ctx.new_page()
            job.goto(JOB_URL, wait_until="domcontentloaded", timeout=60000)
            log(f"job page loaded: {job.title()}")
            time.sleep(4)
            # best-effort cookie banner dismissal
            for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept')"]:
                try:
                    b = job.locator(sel).first
                    if b.is_visible(timeout=1500):
                        b.click()
                        log(f"dismissed cookie banner via {sel}")
                        time.sleep(1)
                        break
                except Exception:
                    pass

            assistant = ctx.new_page()
            assistant.goto(f"chrome-extension://{ext_id}/src/assistant.html")
            time.sleep(1.5)
            # Route the panel's active-tab lookup at the job tab (side panel normally
            # rides along with it; here the panel lives in its own tab).
            assistant.evaluate(
                """() => {
                const real = chrome.tabs.query.bind(chrome.tabs);
                chrome.tabs.query = async (info) => {
                  if (info && info.active && info.currentWindow) {
                    const tabs = await real({ url: '*://job-boards.greenhouse.io/*' });
                    if (tabs.length) return tabs;
                  }
                  return real(info);
                };
              }"""
            )
            log("assistant panel open, tabs.query patched")

            # -------- STEP 1: PREVIEW --------
            log("STEP 1: clicking Preview")
            assistant.evaluate("() => document.getElementById('previewButton').click()")
            ok = wait_for(
                lambda: review_state(assistant)["status"] in ("Preview ready", "Needs attention"),
                "preview to finish", 120,
            )
            snapshot("1_preview", assistant, job)
            if not ok:
                log("preview did not finish; aborting")
                return

            # -------- STEP 2: FILL SELECTED --------
            log("STEP 2: clicking Fill selected")
            st = review_state(assistant)
            if st["fillDisabled"]:
                log("Fill selected is DISABLED (no mappings)")
            else:
                assistant.evaluate("() => document.getElementById('fillSelectedButton').click()")
                wait_for(
                    lambda: review_state(assistant)["status"] in ("Filled", "Needs attention"),
                    "fill to finish", 120,
                )
                time.sleep(3)
            snapshot("2_fill", assistant, job)

            # -------- STEP 3: ASK AI --------
            log("STEP 3: clicking Ask AI")
            st = review_state(assistant)
            if st["askAiDisabled"]:
                log("Ask AI is DISABLED (no askable unmapped fields)")
            else:
                assistant.evaluate("() => document.getElementById('askAiButton').click()")
                time.sleep(3)
                # done when the finally-block resets the AI status
                last = [""]

                def ai_done():
                    s = review_state(assistant)
                    if s["aiStatus"] != last[0]:
                        last[0] = s["aiStatus"]
                        log(f"  ai status: {s['aiStatus']}")
                    return s["aiStatus"] == "AI idle"

                wait_for(ai_done, "Ask AI to finish", 900, poll=2.0)
                time.sleep(3)
            snapshot("3_askai", assistant, job)

            log("done")
        finally:
            save_results()
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        RESULTS["fatal"] = str(e)
        save_results()
        sys.exit(1)
