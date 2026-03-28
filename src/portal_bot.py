"""
Adobe Stock Portal Submission Bot

Logs in to contributor.stock.adobe.com, finds images needing metadata review,
ticks "Created using generative AI tools" and "People and property are fictional",
then submits for review.

Run as: python -m src.portal_bot
"""

import asyncio
import logging
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from src import config

logger = logging.getLogger(__name__)


async def run_portal_bot() -> int:
    """
    Returns count of images successfully submitted.
    """
    submitted_count = 0

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            # ----------------------------------------------------------------
            # 1. Navigate to the contributor portal
            # ----------------------------------------------------------------
            logger.debug("Navigating to Adobe Stock Contributor Portal")
            await page.goto(
                "https://contributor.stock.adobe.com",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_load_state("networkidle", timeout=30000)

            # ----------------------------------------------------------------
            # 2. Log in if a login form is present
            # ----------------------------------------------------------------
            needs_login = False
            try:
                # Adobe IMS login page shows an email/username field
                email_field = page.get_by_label("Email address", exact=False)
                await email_field.wait_for(timeout=5000)
                needs_login = True
            except PlaywrightTimeoutError:
                logger.debug("No login form detected — assuming already authenticated")

            if needs_login:
                logger.debug("Login form detected — filling credentials")

                # Fill email and proceed
                await page.get_by_label("Email address", exact=False).fill(
                    config.ADOBE_PORTAL_EMAIL or ""
                )
                logger.debug("Filled email field")

                # Adobe IMS often has a two-step flow: email → continue → password
                try:
                    continue_btn = page.get_by_role("button", name="Continue")
                    await continue_btn.wait_for(timeout=5000)
                    await continue_btn.click()
                    logger.debug("Clicked Continue button (two-step login flow)")
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    logger.debug("No Continue button — attempting single-step login")

                # Fill password
                try:
                    password_field = page.get_by_label("Password", exact=False)
                    await password_field.wait_for(timeout=10000)
                    await password_field.fill(config.ADOBE_PORTAL_PASS or "")
                    logger.debug("Filled password field")
                except PlaywrightTimeoutError:
                    logger.warning("Password field not found — login may have failed")

                # Click sign-in button
                try:
                    signin_btn = page.get_by_role(
                        "button", name="Sign in", exact=False
                    )
                    await signin_btn.wait_for(timeout=10000)
                    await signin_btn.click()
                    logger.debug("Clicked Sign in button")
                except PlaywrightTimeoutError:
                    # Fallback: look for a submit button
                    submit_btn = page.get_by_role("button", name="Submit", exact=False)
                    await submit_btn.wait_for(timeout=10000)
                    await submit_btn.click()
                    logger.debug("Clicked Submit button (fallback sign-in)")

                # Wait for the portal dashboard to load after login
                await page.wait_for_load_state("networkidle", timeout=30000)
                logger.debug("Login navigation complete")

            # ----------------------------------------------------------------
            # 3. Navigate to the "Needs Metadata" / "To Do" queue
            # ----------------------------------------------------------------
            logger.debug("Looking for Needs Metadata / To Do section")

            # Try clicking the "To Do" tab if present
            try:
                todo_tab = page.get_by_role("tab", name="To Do", exact=False)
                await todo_tab.wait_for(timeout=10000)
                await todo_tab.click()
                logger.debug("Clicked To Do tab")
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                logger.debug("No To Do tab found — trying Needs Metadata link")
                try:
                    needs_meta_link = page.get_by_text("Needs Metadata", exact=False)
                    await needs_meta_link.wait_for(timeout=10000)
                    await needs_meta_link.click()
                    logger.debug("Clicked Needs Metadata link")
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    logger.debug(
                        "Needs Metadata link not found — assuming current view is queue"
                    )

            # ----------------------------------------------------------------
            # 4. Process images with pagination
            # ----------------------------------------------------------------
            page_number = 1
            while True:
                logger.debug("Processing queue page %d", page_number)

                # Collect all image cards / edit buttons on this page
                # Adobe Stock contributor portal typically shows cards with an
                # "Edit" or pencil-icon button per image. We target all of them.
                edit_buttons = await page.get_by_role(
                    "button", name="Edit", exact=False
                ).all()

                if not edit_buttons:
                    # Fallback: look for image cards by a common wrapper role
                    edit_buttons = await page.get_by_role("link", name="Edit", exact=False).all()

                logger.debug(
                    "Found %d image edit buttons on page %d",
                    len(edit_buttons),
                    page_number,
                )

                if not edit_buttons:
                    logger.debug("No editable images found on page %d — stopping", page_number)
                    break

                for idx, edit_btn in enumerate(edit_buttons):
                    logger.debug("Processing image %d on page %d", idx + 1, page_number)

                    try:
                        await edit_btn.click()
                        await page.wait_for_load_state("networkidle", timeout=30000)
                        logger.debug("Opened edit view for image %d", idx + 1)
                    except PlaywrightTimeoutError:
                        logger.warning(
                            "Timed out opening image %d on page %d — skipping",
                            idx + 1,
                            page_number,
                        )
                        continue

                    # ----------------------------------------------------
                    # 4a. Check title is populated — skip if empty
                    # ----------------------------------------------------
                    title_populated = False
                    try:
                        title_field = page.get_by_label("Title", exact=False)
                        await title_field.wait_for(timeout=10000)
                        title_value = await title_field.input_value()
                        if title_value and title_value.strip():
                            title_populated = True
                            logger.debug("Title is populated: %r", title_value[:60])
                        else:
                            logger.warning(
                                "Image %d on page %d has empty title — skipping",
                                idx + 1,
                                page_number,
                            )
                    except PlaywrightTimeoutError:
                        logger.warning(
                            "Could not find title field for image %d — skipping",
                            idx + 1,
                            page_number,
                        )

                    if not title_populated:
                        await page.go_back()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        continue

                    # ----------------------------------------------------
                    # 4b. Tick AI disclosure checkboxes
                    # ----------------------------------------------------
                    ai_checkbox_ticked = False
                    try:
                        ai_checkbox = page.get_by_label(
                            "Created using generative AI tools", exact=False
                        )
                        await ai_checkbox.wait_for(timeout=10000)
                        if not await ai_checkbox.is_checked():
                            await ai_checkbox.check()
                            logger.debug(
                                "Checked 'Created using generative AI tools' for image %d",
                                idx + 1,
                            )
                        else:
                            logger.debug(
                                "'Created using generative AI tools' already checked for image %d",
                                idx + 1,
                            )
                        ai_checkbox_ticked = True
                    except PlaywrightTimeoutError:
                        logger.warning(
                            "AI disclosure checkbox not found for image %d — skipping",
                            idx + 1,
                        )

                    if not ai_checkbox_ticked:
                        await page.go_back()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        continue

                    try:
                        fictional_checkbox = page.get_by_label(
                            "People and property are fictional", exact=False
                        )
                        await fictional_checkbox.wait_for(timeout=10000)
                        if not await fictional_checkbox.is_checked():
                            await fictional_checkbox.check()
                            logger.debug(
                                "Checked 'People and property are fictional' for image %d",
                                idx + 1,
                            )
                        else:
                            logger.debug(
                                "'People and property are fictional' already checked for image %d",
                                idx + 1,
                            )
                    except PlaywrightTimeoutError:
                        # Not fatal — this checkbox may not always be present
                        logger.debug(
                            "'People and property are fictional' checkbox not found for image %d "
                            "— continuing without it",
                            idx + 1,
                        )

                    # ----------------------------------------------------
                    # 4c. Submit for review
                    # ----------------------------------------------------
                    submitted = False
                    for btn_name in ("Submit", "Submit for Review", "Save", "Save and Submit"):
                        try:
                            submit_btn = page.get_by_role(
                                "button", name=btn_name, exact=False
                            )
                            await submit_btn.wait_for(timeout=10000)
                            await submit_btn.click()
                            logger.debug(
                                "Clicked '%s' for image %d", btn_name, idx + 1
                            )
                            await page.wait_for_load_state("networkidle", timeout=30000)
                            submitted = True
                            break
                        except PlaywrightTimeoutError:
                            continue

                    if submitted:
                        submitted_count += 1
                        logger.debug(
                            "Image %d on page %d submitted — running total: %d",
                            idx + 1,
                            page_number,
                            submitted_count,
                        )
                    else:
                        logger.warning(
                            "No submit button found for image %d on page %d — skipping",
                            idx + 1,
                            page_number,
                        )
                        # Return to queue list if no submit was triggered
                        try:
                            await page.go_back()
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass

                # ----------------------------------------------------------------
                # 5. Pagination — click Next if present
                # ----------------------------------------------------------------
                try:
                    next_btn = page.get_by_role("button", name="Next", exact=False)
                    await next_btn.wait_for(timeout=5000)
                    is_disabled = await next_btn.get_attribute("disabled")
                    if is_disabled is not None:
                        logger.debug("Next button is disabled — no more pages")
                        break
                    await next_btn.click()
                    logger.debug("Clicked Next — advancing to page %d", page_number + 1)
                    await page.wait_for_load_state("networkidle", timeout=30000)
                    page_number += 1
                except PlaywrightTimeoutError:
                    logger.debug("No Next button found — end of queue")
                    break

            await browser.close()
            logger.debug("Browser closed — total submitted: %d", submitted_count)

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Portal bot encountered an unrecoverable error: %s", exc, exc_info=True
        )
        return 0

    return submitted_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    count = asyncio.run(run_portal_bot())
    print(f"Portal bot: submitted {count} images for review")
