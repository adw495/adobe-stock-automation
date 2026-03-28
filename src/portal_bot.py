"""
Adobe Stock Portal Bot — Web Upload + Metadata + AI Disclosure

Logs in to contributor.stock.adobe.com, uploads images via web UI,
fills metadata (title/keywords/category), ticks AI disclosure checkboxes,
and submits for review.

Run as: python -m src.portal_bot  (processes any queued images)
Called by: src/main.py after image generation + quality filter + metadata
"""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, FileChooser
from src import config

logger = logging.getLogger(__name__)

CATEGORY_NAMES = {
    1: "Business",
    44: "Technology",
    22: "Abstract",
    12: "Backgrounds/Textures"
}


def _load_session_cookies() -> dict | None:
    """
    Load pre-authenticated Adobe session cookies from the ADOBE_SESSION_COOKIES
    environment variable (base64-encoded JSON, exported via scripts/export_auth.py).
    Returns the storage_state dict for Playwright, or None if not available.
    """
    raw = os.environ.get("ADOBE_SESSION_COOKIES")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode())
    except Exception as e:
        logger.warning("Failed to decode ADOBE_SESSION_COOKIES: %s", e)
        return None


async def upload_and_submit(
    images: list[dict],    # [{"image_path": str, "prompt_id": int, "source": str}]
    metadata: list[dict],  # [{"prompt_id": int, "title": str, "keywords": list[str], "category_id": int}]
    state: dict
) -> dict:
    """
    Upload images with metadata and AI disclosure to Adobe Stock.
    Returns {"uploaded": int, "failed": int}
    Updates state["uploaded_hashes"], state["used_prompt_ids"], state["total_uploaded"]
    """
    # Build metadata lookup by prompt_id
    meta_lookup = {m["prompt_id"]: m for m in metadata}

    uploaded = 0
    failed = 0

    storage_state = _load_session_cookies()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context_kwargs = dict(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        if storage_state:
            context_kwargs["storage_state"] = storage_state
            logger.info("Loaded pre-authenticated session cookies")
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        try:
            # Login
            if not await _login(page):
                logger.error("Login failed — aborting upload batch")
                return {"uploaded": 0, "failed": len(images)}

            # Upload each image one at a time
            for img in images:
                meta = meta_lookup.get(img["prompt_id"])
                if not meta:
                    logger.warning(f"No metadata for prompt_id {img['prompt_id']} — skipping")
                    failed += 1
                    continue

                success = await _upload_single(page, img["image_path"], meta)
                if success:
                    # Update state
                    try:
                        from PIL import Image
                        import imagehash
                        phash = str(imagehash.phash(Image.open(img["image_path"])))
                        state.setdefault("uploaded_hashes", []).append(phash)
                    except Exception:
                        pass
                    state.setdefault("used_prompt_ids", []).append(img["prompt_id"])
                    state["total_uploaded"] = state.get("total_uploaded", 0) + 1
                    uploaded += 1
                else:
                    failed += 1

                # Brief pause between uploads to avoid rate limiting
                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Unexpected error in upload_and_submit: {e}", exc_info=True)
        finally:
            await browser.close()

    logger.info(f"Upload complete: {uploaded} uploaded, {failed} failed")
    return {"uploaded": uploaded, "failed": failed}


async def _login(page) -> bool:
    """Log in to contributor.stock.adobe.com. Returns True on success."""
    try:
        await page.goto("https://contributor.stock.adobe.com", timeout=60000)
        # Use domcontentloaded — networkidle can hang on pages with long-polling
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        logger.info("Loaded URL: %s", page.url)

        # Check if already logged in
        if await page.locator("text=Dashboard").count() > 0:
            logger.info("Already logged in")
            return True

        # The contributor portal landing page shows a "Sign in" link/button —
        # click it to be redirected to the Adobe IMS login page.
        for sign_in_text in ["Sign in", "Sign In", "Log in", "Log In", "Sign Up"]:
            loc = page.get_by_role("link", name=sign_in_text, exact=True)
            if await loc.count() == 0:
                loc = page.get_by_role("button", name=sign_in_text, exact=True)
            if await loc.count() > 0:
                logger.info("Clicking '%s' on landing page", sign_in_text)
                await loc.first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
                logger.info("After sign-in click, URL: %s", page.url)
                break

        # ── Email step ────────────────────────────────────────────────────────
        # Adobe IMS uses id="EmailPage-EmailField" on the email input
        email_selectors = [
            'input#EmailPage-EmailField',
            'input[name="username"]',
            'input[type="email"]',
            'input[placeholder*="email" i]',
        ]
        email_filled = False
        for sel in email_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.wait_for(state="visible", timeout=10000)
                    await loc.first.click()
                    await loc.first.fill(config.ADOBE_PORTAL_EMAIL)
                    email_filled = True
                    logger.info("Email filled via selector: %s", sel)
                    break
            except Exception:
                continue

        if not email_filled:
            logger.error("Could not find email field. URL: %s", page.url)
            await _screenshot(page, "login_no_email")
            return False

        # Click Continue / Next
        for btn_name in ["Continue", "Next", "Sign in"]:
            btn = page.get_by_role("button", name=btn_name)
            if await btn.count() > 0:
                await btn.first.click()
                break
        await page.wait_for_timeout(3000)

        # ── Password step ─────────────────────────────────────────────────────
        pwd_selectors = [
            'input#PasswordPage-PasswordField',
            'input[name="password"]',
            'input[type="password"]',
        ]
        pwd_filled = False
        for sel in pwd_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.wait_for(state="visible", timeout=10000)
                    await loc.first.click()
                    await loc.first.fill(config.ADOBE_PORTAL_PASS)
                    pwd_filled = True
                    logger.info("Password filled via selector: %s", sel)
                    break
            except Exception:
                continue

        if not pwd_filled:
            logger.error("Could not find password field. URL: %s", page.url)
            await _screenshot(page, "login_no_password")
            return False

        for btn_name in ["Continue", "Sign in", "Log In"]:
            btn = page.get_by_role("button", name=btn_name)
            if await btn.count() > 0:
                await btn.first.click()
                break

        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        logger.info("Post-login URL: %s", page.url)

        if await page.locator("text=Dashboard").count() > 0:
            logger.info("Login successful")
            return True

        logger.error("Login failed — Dashboard not visible. URL: %s", page.url)
        await _screenshot(page, "login_failed")
        return False

    except Exception as e:
        logger.error(f"Login exception: {e}")
        await _screenshot(page, "login_exception")
        return False


async def _screenshot(page, name: str) -> None:
    """Save a screenshot to /tmp for debugging; swallow errors."""
    try:
        path = f"/tmp/adobe_{name}.png"
        await page.screenshot(path=path)
        logger.info("Screenshot saved: %s", path)
    except Exception:
        pass


async def _upload_single(page, image_path: str, meta: dict) -> bool:
    """
    Upload one image, fill metadata, tick AI disclosure, submit.
    Returns True on success.
    """
    fname = Path(image_path).name
    try:
        logger.info("Uploading %s — navigating to submit page", fname)

        # Always navigate directly to the submit/upload page
        await page.goto("https://contributor.stock.adobe.com/en/submit", timeout=30000)
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)

        logger.info("Submit page URL: %s", page.url)
        await _screenshot(page, f"upload_start_{fname[:20]}")

        # ── File input ────────────────────────────────────────────────────────
        # Try the hidden file input first; if not found, trigger file chooser
        file_input = page.locator('input[type="file"]')
        if await file_input.count() > 0:
            logger.info("Using direct file input for %s", fname)
            await file_input.first.set_input_files(image_path)
        else:
            logger.info("No file input found — trying file chooser for %s", fname)
            # Click any element that might trigger the file picker
            triggered = False
            for trigger_sel in [
                page.get_by_text("Browse", exact=False),
                page.get_by_text("Upload", exact=False),
                page.get_by_text("IMAGES", exact=False),
                page.locator('[class*="upload" i]').first,
                page.locator('[class*="dropzone" i]').first,
            ]:
                try:
                    if await trigger_sel.count() > 0:
                        async with page.expect_file_chooser(timeout=5000) as fc_info:
                            await trigger_sel.first.click()
                        fc = await fc_info.value
                        await fc.set_files(image_path)
                        triggered = True
                        break
                except Exception:
                    continue
            if not triggered:
                logger.error("Could not trigger file input for %s", fname)
                await _screenshot(page, f"upload_no_input_{fname[:20]}")
                return False

        # Wait for the upload to process — look for metadata fields appearing
        logger.info("File set, waiting for upload to process for %s", fname)
        await page.wait_for_timeout(10000)
        await _screenshot(page, f"upload_after_file_{fname[:20]}")

        # Log page URL and a snippet of the page body to understand the state
        logger.info("Post-upload URL: %s", page.url)
        try:
            body_text = await page.locator("body").inner_text()
            logger.info("Page body snippet (first 500 chars): %s", body_text[:500].replace("\n", " "))
        except Exception:
            pass

        # ── Title ─────────────────────────────────────────────────────────────
        title_filled = False
        for sel in [
            page.get_by_label("Title"),
            page.get_by_placeholder("Title"),
            page.locator('[name="title"]'),
            page.locator('input[placeholder*="title" i]'),
            page.locator('textarea[placeholder*="title" i]'),
        ]:
            if await sel.count() > 0:
                await sel.first.fill(meta["title"])
                title_filled = True
                logger.info("Title filled for %s", fname)
                break

        if not title_filled:
            logger.warning("No title field found for %s", fname)

        # ── Keywords ──────────────────────────────────────────────────────────
        keywords_str = ", ".join(meta["keywords"])
        kw_filled = False
        for sel in [
            page.get_by_label("Keywords"),
            page.get_by_placeholder("Keywords"),
            page.locator('[name="keywords"]'),
            page.locator('input[placeholder*="keyword" i]'),
            page.locator('textarea[placeholder*="keyword" i]'),
        ]:
            if await sel.count() > 0:
                await sel.first.fill(keywords_str)
                kw_filled = True
                logger.info("Keywords filled for %s", fname)
                break

        if not kw_filled:
            logger.warning("No keywords field found for %s", fname)

        # ── AI disclosure checkboxes ───────────────────────────────────────────
        for label_text in ["Created using generative AI tools", "generative AI"]:
            try:
                cb = page.get_by_label(label_text)
                if await cb.count() > 0 and not await cb.first.is_checked():
                    await cb.first.check()
                    logger.info("Checked AI disclosure: %s", label_text)
                    break
            except Exception:
                pass

        for label_text in ["People and property are fictional", "People and Property are fictional", "fictional"]:
            try:
                cb = page.get_by_label(label_text)
                if await cb.count() > 0 and not await cb.first.is_checked():
                    await cb.first.check()
                    logger.info("Checked property checkbox: %s", label_text)
                    break
            except Exception:
                pass

        await _screenshot(page, f"upload_pre_submit_{fname[:20]}")

        # ── Submit ────────────────────────────────────────────────────────────
        for btn_name in ["Submit", "Submit for Review", "Save and Submit", "Save"]:
            btn = page.get_by_role("button", name=btn_name)
            if await btn.count() > 0:
                await btn.first.click()
                logger.info("Clicked submit for %s", fname)
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)
                await _screenshot(page, f"upload_post_submit_{fname[:20]}")
                return True

        logger.warning("No submit button found for %s", fname)
        await _screenshot(page, f"upload_no_submit_{fname[:20]}")
        return False

    except Exception as e:
        logger.error(f"Upload failed for {fname}: {e}")
        await _screenshot(page, f"upload_error_{fname[:20]}")
        return False


async def _process_existing_queue():
    """Standalone mode: process any images already in Needs Metadata queue."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            if not await _login(page):
                print("Login failed")
                return
            # Navigate to needs-metadata queue
            await page.goto("https://contributor.stock.adobe.com", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            # Find and process items in queue (original portal_bot logic)
            count = 0
            todo_items = await page.locator('[class*="todo"], [class*="needs-metadata"]').all()
            for item in todo_items:
                try:
                    await item.click()
                    await page.wait_for_timeout(1000)
                    for label in ["Created using generative AI tools", "generative AI"]:
                        cb = page.get_by_label(label)
                        if await cb.count() > 0 and not await cb.first.is_checked():
                            await cb.first.check()
                            break
                    for label in ["People and property are fictional", "fictional"]:
                        cb = page.get_by_label(label)
                        if await cb.count() > 0 and not await cb.first.is_checked():
                            await cb.first.check()
                            break
                    for btn in ["Submit", "Submit for Review"]:
                        b = page.get_by_role("button", name=btn)
                        if await b.count() > 0:
                            await b.first.click()
                            count += 1
                            break
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    logger.debug(f"Item processing error: {e}")
            print(f"Processed {count} items from queue")
        finally:
            await browser.close()


if __name__ == "__main__":
    # When run standalone, process any images already in "Needs Metadata" queue
    asyncio.run(_process_existing_queue())
