"""
image_generator.py — async image generator calling 4 free APIs in parallel.

APIs (in priority order):
  1. Pollinations.ai  — unlimited, primary
  2. HuggingFace      — 1,000/day
  3. Leonardo.ai      — ~30 generations/day (conservative token budget)
  4. Ideogram         — 10/day free
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

from src import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _save_image(data: bytes, path: str) -> None:
    """Write raw bytes to path (sync write is fine for images of this size)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)


# ---------------------------------------------------------------------------
# Per-API coroutines
# ---------------------------------------------------------------------------

async def _pollinations(
    session: aiohttp.ClientSession,
    prompt_item: dict,
    batch_dir: str,
) -> dict | None:
    """
    Pollinations.ai — unlimited free API, no auth required.
    GET https://image.pollinations.ai/prompt/{encoded_prompt}
    Returns JPEG bytes directly.
    """
    prompt_id = prompt_item["id"]
    prompt_text = prompt_item["prompt"]
    encoded = quote(prompt_text, safe="")
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        "?width=2048&height=2048&model=flux&seed=-1"
    )
    headers = {
        "Referer": "https://pollinations.ai/",
        "User-Agent": "Mozilla/5.0 (compatible; AdobeStockBot/1.0)",
    }
    image_path = os.path.join(batch_dir, f"pollinations_{prompt_id}.jpg")
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=90)) as resp:
            if resp.status != 200:
                logger.warning(
                    "Pollinations returned %s for prompt %s", resp.status, prompt_id
                )
                return None
            data = await resp.read()
        await _save_image(data, image_path)
        logger.info("Pollinations OK — prompt %s saved to %s", prompt_id, image_path)
        return {"image_path": image_path, "prompt_id": prompt_id, "source": "pollinations"}
    except Exception as exc:
        logger.error("Pollinations error for prompt %s: %s", prompt_id, exc)
        return None


async def _huggingface(
    session: aiohttp.ClientSession,
    prompt_item: dict,
    batch_dir: str,
    state: dict,
) -> dict | None:
    """
    HuggingFace Inference API — SDXL 1.0, 1,000 req/day.
    NOTE: SDXL max is 1024×1024; the quality filter downstream will handle the 4MP check.
    """
    if config.HUGGINGFACE_TOKEN is None:
        return None
    if state["daily"]["huggingface"] >= 1000:
        logger.info("HuggingFace daily budget exhausted, skipping prompt %s", prompt_item["id"])
        return None

    prompt_id = prompt_item["id"]
    url = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"
    headers = {"Authorization": f"Bearer {config.HUGGINGFACE_TOKEN}"}
    payload = {
        "inputs": prompt_item["prompt"],
        "parameters": {"width": 1024, "height": 1024},
    }
    image_path = os.path.join(batch_dir, f"huggingface_{prompt_id}.jpg")
    try:
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "HuggingFace returned %s for prompt %s", resp.status, prompt_id
                )
                return None
            data = await resp.read()
        await _save_image(data, image_path)
        # Upscale 1024×1024 → 2048×2048 so it passes the 4MP quality filter
        try:
            from PIL import Image as PILImage
            img = PILImage.open(image_path)
            img_up = img.resize((2048, 2048), PILImage.LANCZOS)
            img_up.save(image_path, "JPEG", quality=95)
            logger.info("HuggingFace upscaled to 2048×2048 for prompt %s", prompt_id)
        except Exception as up_exc:
            logger.warning("HuggingFace upscale failed for prompt %s: %s", prompt_id, up_exc)
        state["daily"]["huggingface"] += 1
        state["total_generated"] += 1
        logger.info("HuggingFace OK — prompt %s saved to %s", prompt_id, image_path)
        return {"image_path": image_path, "prompt_id": prompt_id, "source": "huggingface"}
    except Exception as exc:
        logger.error("HuggingFace error for prompt %s: %s", prompt_id, exc)
        return None


async def _leonardo(
    session: aiohttp.ClientSession,
    prompt_item: dict,
    batch_dir: str,
    state: dict,
) -> dict | None:
    """
    Leonardo.ai — ~30 generations/day (conservative token budget).
    NOTE: 1024×1024 may fail the 4MP check; quality filter handles it.
    """
    if config.LEONARDO_API_KEY is None:
        return None
    if state["daily"]["leonardo"] >= 30:
        logger.info("Leonardo daily budget exhausted, skipping prompt %s", prompt_item["id"])
        return None

    prompt_id = prompt_item["id"]
    headers = {
        "Authorization": f"Bearer {config.LEONARDO_API_KEY}",
        "Content-Type": "application/json",
    }

    # Step 1 — submit generation
    gen_url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    payload = {
        "prompt": prompt_item["prompt"],
        "width": 1024,
        "height": 1024,
        "num_images": 1,
    }
    try:
        async with session.post(
            gen_url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status not in (200, 201):
                logger.warning(
                    "Leonardo submit returned %s for prompt %s", resp.status, prompt_id
                )
                return None
            body = await resp.json()

        generation_id = (
            body.get("sdGenerationJob", {}).get("generationId")
            or body.get("generationId")
        )
        if not generation_id:
            logger.warning("Leonardo: no generation_id in response for prompt %s", prompt_id)
            return None

        # Step 2 — poll until COMPLETE (max 120 s, polling every 5 s)
        poll_url = f"https://cloud.leonardo.ai/api/rest/v1/generations/{generation_id}"
        image_url: str | None = None
        for _ in range(24):  # 24 × 5 s = 120 s
            await asyncio.sleep(5)
            async with session.get(
                poll_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as poll_resp:
                if poll_resp.status != 200:
                    continue
                poll_body = await poll_resp.json()

            gen_data = poll_body.get("generations_by_pk") or poll_body.get("generation", {})
            status = gen_data.get("status")
            if status == "COMPLETE":
                images = gen_data.get("generated_images", [])
                if images:
                    image_url = images[0]["url"]
                break
            if status == "FAILED":
                logger.warning("Leonardo generation FAILED for prompt %s", prompt_id)
                return None

        if not image_url:
            logger.warning("Leonardo: no image URL obtained for prompt %s", prompt_id)
            return None

        # Step 3 — download image
        async with session.get(
            image_url, timeout=aiohttp.ClientTimeout(total=30)
        ) as img_resp:
            if img_resp.status != 200:
                logger.warning(
                    "Leonardo image download returned %s for prompt %s",
                    img_resp.status,
                    prompt_id,
                )
                return None
            data = await img_resp.read()

        image_path = os.path.join(batch_dir, f"leonardo_{prompt_id}.jpg")
        await _save_image(data, image_path)
        state["daily"]["leonardo"] += 1
        state["total_generated"] += 1
        logger.info("Leonardo OK — prompt %s saved to %s", prompt_id, image_path)
        return {"image_path": image_path, "prompt_id": prompt_id, "source": "leonardo"}

    except Exception as exc:
        logger.error("Leonardo error for prompt %s: %s", prompt_id, exc)
        return None


async def _ideogram(
    session: aiohttp.ClientSession,
    prompt_item: dict,
    batch_dir: str,
    state: dict,
) -> dict | None:
    """
    Ideogram — 10 free generations/day.
    IDEOGRAM_API_KEY is not defined in config; skip silently if absent.
    """
    ideogram_key = getattr(config, "IDEOGRAM_API_KEY", None) or os.environ.get("IDEOGRAM_API_KEY")
    if not ideogram_key:
        return None
    if state["daily"]["ideogram"] >= 10:
        logger.info("Ideogram daily budget exhausted, skipping prompt %s", prompt_item["id"])
        return None

    prompt_id = prompt_item["id"]
    headers = {
        "Authorization": f"Bearer {ideogram_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "image_request": {
            "prompt": prompt_item["prompt"],
            "resolution": "RESOLUTION_2048_2048",
            "model": "V_2",
        }
    }
    image_path = os.path.join(batch_dir, f"ideogram_{prompt_id}.jpg")
    try:
        async with session.post(
            "https://api.ideogram.ai/generate",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "Ideogram returned %s for prompt %s", resp.status, prompt_id
                )
                return None
            body = await resp.json()

        image_url = body["data"][0]["url"]

        async with session.get(
            image_url, timeout=aiohttp.ClientTimeout(total=30)
        ) as img_resp:
            if img_resp.status != 200:
                logger.warning(
                    "Ideogram image download returned %s for prompt %s",
                    img_resp.status,
                    prompt_id,
                )
                return None
            data = await img_resp.read()

        await _save_image(data, image_path)
        state["daily"]["ideogram"] += 1
        state["total_generated"] += 1
        logger.info("Ideogram OK — prompt %s saved to %s", prompt_id, image_path)
        return {"image_path": image_path, "prompt_id": prompt_id, "source": "ideogram"}

    except Exception as exc:
        logger.error("Ideogram error for prompt %s: %s", prompt_id, exc)
        return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def generate_batch(prompts: list[dict], state: dict) -> list[dict]:
    """
    Generate images for a list of prompt dicts (each has: id, prompt, category).
    Respects daily budgets in state["daily"].
    Returns list of {"image_path": str, "prompt_id": int, "source": str} dicts.

    All API calls are dispatched in parallel via asyncio.gather().
    Each prompt is sent to all eligible APIs concurrently; successful results
    are collected and returned.  Failures are logged and silently skipped.
    """
    timestamp = int(time.time())
    batch_dir = f"/tmp/batch_{timestamp}"
    os.makedirs(batch_dir, exist_ok=True)
    logger.info("Batch directory: %s (%d prompts)", batch_dir, len(prompts))

    results: list[dict] = []

    async with aiohttp.ClientSession() as session:
        # Build one coroutine per (api, prompt) combination
        tasks = []
        for prompt_item in prompts:
            # Pollinations is currently blocked on GitHub Actions IPs (returns 401).
            # Left in for when/if it becomes available; failures are handled gracefully.
            tasks.append(_pollinations(session, prompt_item, batch_dir))
            # HuggingFace SDXL: 1024×1024, upscaled to 2048×2048 in-place after download.
            tasks.append(_huggingface(session, prompt_item, batch_dir, state))
            # Ideogram: native 2048×2048, 10/day free.
            tasks.append(_ideogram(session, prompt_item, batch_dir, state))
            # Leonardo: 1024×1024 max, excluded until a higher-res model is available.
            # tasks.append(_leonardo(session, prompt_item, batch_dir, state))

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    for item in raw_results:
        if isinstance(item, Exception):
            logger.error("Unhandled exception in gather: %s", item)
            continue
        if item is not None:
            # Pollinations counter/total updated here (the others update inside their coroutines)
            if item["source"] == "pollinations":
                state["daily"]["pollinations"] += 1
                state["total_generated"] += 1
            results.append(item)

    logger.info(
        "Batch complete — %d images generated from %d prompts", len(results), len(prompts)
    )
    return results
