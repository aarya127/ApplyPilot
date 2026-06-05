from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from application_agent.agent.profile_loader import load_profile
from application_agent.agent.runner import ApplicationAgent


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "autofill_extension/profile.private.json"
DEFAULT_BROWSER_PROFILE = Path(__file__).resolve().parent / "browser_profile"


def main() -> None:
    profile = load_profile(DEFAULT_PROFILE)
    agent = ApplicationAgent(profile)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(DEFAULT_BROWSER_PROFILE),
            headless=False,
            args=["--start-maximized"],
        )
        page = context.pages[-1] if context.pages else context.new_page()

        print("Open your job system, click Apply, then press Enter here.")
        input()

        page = context.pages[-1] if context.pages else page
        agent.complete_current_application(page)
        print("Agent paused before final submit or manual review.")
        page.pause()


if __name__ == "__main__":
    main()

