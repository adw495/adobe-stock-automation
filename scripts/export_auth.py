"""
Export Adobe Stock session cookies for use in GitHub Actions.

Run this script ONCE locally after a manual login. It:
  1. Opens a visible browser at contributor.stock.adobe.com
  2. Lets you log in manually (handles email verification, 2FA, etc.)
  3. Once you press Enter in the terminal, saves the session cookies
  4. Prints a base64-encoded JSON string to stdout

Set the output as the GitHub Secret ADOBE_SESSION_COOKIES.
The portal_bot will load these cookies to skip the login form.

Usage:
    python scripts/export_auth.py
"""

import asyncio
import base64
import json
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # visible browser so you can interact
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto("https://contributor.stock.adobe.com")

        print()
        print("=" * 60)
        print("A browser has opened. Please log in to Adobe Stock.")
        print("Complete any email verification or 2FA prompts.")
        print()
        print("Once you can see the Dashboard (you are fully logged in),")
        print("press Enter here to save the session cookies.")
        print("=" * 60)
        input()

        # Save storage state (cookies + localStorage)
        state = await context.storage_state()
        state_json = json.dumps(state)
        state_b64 = base64.b64encode(state_json.encode()).decode()

        await browser.close()

    print()
    print("=" * 60)
    print("SUCCESS — copy the string below as a GitHub Secret named:")
    print("  ADOBE_SESSION_COOKIES")
    print()
    print("Go to: GitHub repo → Settings → Secrets and variables →")
    print("  Actions → New repository secret")
    print("=" * 60)
    print()
    print(state_b64)
    print()


if __name__ == "__main__":
    asyncio.run(main())
