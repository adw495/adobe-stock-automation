"""
Adobe Stock Automation Pipeline — Main Orchestrator

Entry point for generate-upload.yml GitHub Actions workflow.
Runs every 15 minutes. Picks prompts, generates images, filters,
generates metadata, uploads to Adobe Stock via web portal.
"""

import asyncio
import logging
import sys

from src import state_tracker, prompt_engine, image_generator, quality_filter, metadata_engine, portal_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


async def run() -> None:
    try:
        logger.info("=== Adobe Stock Pipeline starting ===")

        # 1. Load state
        state = state_tracker.load_state()

        # 2. Pick 5 prompts (web upload ~90s/image; 5 × 90s fits within 25min job timeout)
        prompts = prompt_engine.pick_prompts(state, 5)
        if not prompts:
            logger.info("No unused prompts remaining — run refresh_prompts to reset bank")
            return
        logger.info(f"Picked {len(prompts)} prompts")

        # 3. Generate images (async, all 4 APIs in parallel)
        images = await image_generator.generate_batch(prompts, state)
        logger.info(f"Generated {len(images)} images")

        if not images:
            logger.warning("No images generated — skipping rest of pipeline")
            state_tracker.save_state(state)
            return

        # 4. Quality filter
        passing, rejected = quality_filter.filter_batch(images, state)
        logger.info(f"Quality filter: {len(passing)} passed, {len(rejected)} rejected")
        for r in rejected:
            logger.info(f"  Rejected {r['image_path']}: {r['reason']}")

        if not passing:
            logger.warning("No images passed quality filter")
            state_tracker.save_state(state)
            return

        # 5. Generate metadata (async)
        # Build a prompt lookup dict first for efficient access
        prompt_lookup: dict[int, dict] = {p["id"]: p for p in prompts}
        metadata_prompts = [
            {
                "id": img["prompt_id"],
                "prompt": prompt_lookup[img["prompt_id"]]["prompt"],
                "category": prompt_lookup[img["prompt_id"]]["category"],
            }
            for img in passing
        ]
        metadata = await metadata_engine.generate_metadata(metadata_prompts)
        logger.info(f"Generated metadata for {len(metadata)} images")

        # 6. Upload via web portal (upload + metadata + AI disclosure + submit)
        result = await portal_bot.upload_and_submit(passing, metadata, state)
        logger.info(f"Upload: {result['uploaded']} uploaded, {result['failed']} failed")

        # 7. Mark prompts used (portal_bot already updates state["used_prompt_ids"];
        #    this call keeps bank.json in sync)
        uploaded_prompt_ids = [img["prompt_id"] for img in passing[: result["uploaded"]]]
        prompt_engine.mark_used(uploaded_prompt_ids)

        # 8. Save state + commit back to repo
        state_tracker.save_state(state)
        try:
            state_tracker.commit_state()
        except Exception as e:
            logger.warning(f"State commit failed (non-fatal): {e}")

        # 9. Summary
        logger.info("=== Pipeline complete ===")
        logger.info(f"  Prompts picked:    {len(prompts)}")
        logger.info(f"  Images generated:  {len(images)}")
        logger.info(f"  Passed filter:     {len(passing)}")
        logger.info(f"  Uploaded:          {result['uploaded']}")

    except Exception as exc:
        logger.error("Unexpected pipeline failure: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
