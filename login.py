"""One-time helper: log in to Aternos in a real browser and save the session.

Run this on your own machine (not inside Docker), then either

  * copy state/aternos_state.json to the server, or
  * copy the printed ATERNOS_SESSION value into .env.

    pip install -r requirements.txt
    playwright install chromium
    python login.py
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from aternos_runner import chrome_user_agent

load_dotenv()

STATE = Path(os.getenv("ATERNOS_STATE", "state/aternos_state.json"))
TIMEZONE = os.getenv("TZ_NAME", "Asia/Kolkata")


def main() -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, channel="chromium")
        context = browser.new_context(
            locale="en-US",
            timezone_id=TIMEZONE,
            user_agent=chrome_user_agent(browser),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.goto("https://aternos.org/go/", wait_until="domcontentloaded")

        print()
        print("A browser window is open. Log in to Aternos, open the server you")
        print("want the bot to start, then come back here and press Enter.")
        print()
        input("Press Enter when you're logged in and the server page is visible: ")

        if "/go" in page.url:
            print("\nStill on the login page — the session was NOT saved.")
            browser.close()
            raise SystemExit(1)

        context.storage_state(path=str(STATE))
        cookies = {
            c["name"]: c["value"] for c in context.cookies("https://aternos.org")
        }
        browser.close()

    print(f"\nSaved browser state to: {STATE}")
    print("Keep this file private — it is a live login session.\n")
    print("For .env you only strictly need the session cookie:\n")
    print(f"ATERNOS_SESSION={cookies.get('ATERNOS_SESSION', '')}")
    if cookies.get("ATERNOS_SERVER"):
        print(f"ATERNOS_SERVER_ID={cookies['ATERNOS_SERVER']}")
    print("\n(Do not commit these values anywhere.)")


if __name__ == "__main__":
    main()
