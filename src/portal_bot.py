"""
Adobe Stock Portal Bot — Web Upload + Metadata + AI Disclosure

Logs in to contributor.stock.adobe.com, uploads images via web UI,
fills metadata (title/keywords/category), ticks AI disclosure checkboxes,
and submits for review.

Run as: python -m src.portal_bot  (processes any queued images)
Called by: src/main.py after image generation + quality filter + metadata
"""

import asyncio
import logging
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
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
        await page.goto("https://contributor.stock.adobe.com", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=30000)

        # Check if already logged in (dashboard visible)
        if await page.locator("text=Dashboard").count() > 0:
            logger.debug("Already logged in")
            return True

        # Adobe IMS two-step login: email first
        email_field = page.get_by_label("Email address")
        if await email_field.count() > 0:
            await email_field.fill(config.ADOBE_PORTAL_EMAIL)
            await page.get_by_role("button", name="Continue").click()
            await page.wait_for_timeout(2000)

        # Password step
        pwd_field = page.get_by_label("Password")
        if await pwd_field.count() > 0:
            await pwd_field.fill(config.ADOBE_PORTAL_PASS)
            await page.get_by_role("button", name="Continue").click()
        else:
            # Single-step fallback
            await page.get_by_label("Password").fill(config.ADOBE_PORTAL_PASS)
            await page.get_by_role("button", name="Sign in").click()

        await page.wait_for_load_state("networkidle", timeout=30000)

        # Confirm login succeeded
        if await page.locator("text=Dashboard").count() > 0:
            logger.info("Login successful")
            return True

        logger.error("Login failed — Dashboard not visible after sign-in")
        return False

    except Exception as e:
        logger.error(f"Login exception: {e}")
        return False


async def _upload_single(page, image_path: str, meta: dict) -> bool:
    """
    Upload one image, fill metadata, tick AI disclosure, submit.
    Returns True on success.
    """
    try:
        logger.debug(f"Uploading {Path(image_path).name}")

        # Navigate to upload page / click Upload button
        upload_btn = page.get_by_role("button", name="Upload")
        if await upload_btn.count() > 0:
            await upload_btn.first.click()
        else:
            await page.goto("https://contributor.stock.adobe.com/en/submit", timeout=30000)

        await page.wait_for_load_state("networkidle", timeout=15000)

        # Find file input — try multiple strategies
        # Strategy 1: hidden file input on page
        file_input = page.locator('input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(image_path)
        else:
            # Strategy 2: click Browse to trigger file chooser
            async with page.expect_file_chooser(timeout=10000) as fc_info:
                browse = page.get_by_text("Browse")
                if await browse.count() > 0:
                    await browse.first.click()
                else:
                    # Strategy 3: click the IMAGES (JPEG FILES) button
                    await page.get_by_text("IMAGES").first.click()
            fc = await fc_info.value
            await fc.set_files(image_path)

        # Wait for upload to complete — look for progress disappearing or image preview appearing
        logger.debug("Waiting for upload to complete...")
        await page.wait_for_timeout(8000)  # base wait for upload start

        # Wait up to 60s for upload progress to finish
        try:
            # Wait for a success indicator (thumbnail or checkmark)
            await page.wait_for_selector(
                '[class*="progress"]:not([style*="display: none"])',
                state="hidden",
                timeout=60000
            )
        except PlaywrightTimeoutError:
            pass  # Continue anyway — progress indicator selector may not match

        await page.wait_for_timeout(2000)

        # Fill title
        title_filled = False
        for title_selector in [
            page.get_by_label("Title"),
            page.get_by_placeholder("Title"),
            page.locator('[name="title"]'),
            page.locator('input[placeholder*="title" i]'),
        ]:
            if await title_selector.count() > 0:
                await title_selector.first.fill(meta["title"])
                title_filled = True
                break

        if not title_filled:
            logger.warning(f"Could not find title field for {Path(image_path).name}")

        # Fill keywords — Adobe Stock uses comma-separated input or tag-style
        keywords_str = ", ".join(meta["keywords"])
        keywords_filled = False
        for kw_selector in [
            page.get_by_label("Keywords"),
            page.get_by_placeholder("Keywords"),
            page.locator('[name="keywords"]'),
            page.locator('input[placeholder*="keyword" i]'),
            page.locator('textarea[placeholder*="keyword" i]'),
        ]:
            if await kw_selector.count() > 0:
                await kw_selector.first.fill(keywords_str)
                keywords_filled = True
                break

        if not keywords_filled:
            logger.warning(f"Could not find keywords field for {Path(image_path).name}")

        # Tick AI disclosure checkboxes
        for label_text in [
            "Created using generative AI tools",
            "generative AI",
        ]:
            try:
                cb = page.get_by_label(label_text)
                if await cb.count() > 0 and not await cb.first.is_checked():
                    await cb.first.check()
                    logger.debug(f"Checked: {label_text}")
                    break
            except Exception:
                pass

        for label_text in [
            "People and property are fictional",
            "People and Property are fictional",
            "fictional",
        ]:
            try:
                cb = page.get_by_label(label_text)
                if await cb.count() > 0 and not await cb.first.is_checked():
                    await cb.first.check()
                    logger.debug(f"Checked: {label_text}")
                    break
            except Exception:
                pass

        # Submit
        submitted = False
        for btn_name in ["Submit", "Submit for Review", "Save and Submit", "Save"]:
            btn = page.get_by_role("button", name=btn_name)
            if await btn.count() > 0:
                await btn.first.click()
                logger.info(f"Submitted: {Path(image_path).name}")
                submitted = True
                await page.wait_for_load_state("networkidle", timeout=15000)
                break

        if not submitted:
            logger.warning(f"No submit button found for {Path(image_path).name}")
            return False

        return True

    except Exception as e:
        logger.error(f"Upload failed for {Path(image_path).name}: {e}")
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
