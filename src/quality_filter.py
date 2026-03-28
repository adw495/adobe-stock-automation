import io
from pathlib import Path

from PIL import Image
import imagehash

SRGB_PROFILE = None  # lazy-loaded


def _get_srgb_profile() -> bytes:
    global SRGB_PROFILE
    if SRGB_PROFILE is None:
        try:
            from PIL import ImageCms
            srgb = ImageCms.createProfile("sRGB")
            output = io.BytesIO()
            ImageCms.saveProfile(srgb, output)
            SRGB_PROFILE = output.getvalue()
        except Exception:
            SRGB_PROFILE = b""  # fall back to no profile
    return SRGB_PROFILE


def filter_batch(
    images: list[dict],
    state: dict
) -> tuple[list[dict], list[dict]]:
    """
    Filter images for Adobe Stock quality requirements.

    Parameters
    ----------
    images : list of dicts with keys image_path, prompt_id, source
    state  : application state dict (read-only — caller updates after upload)

    Returns
    -------
    passing  : list of {"image_path": str, "prompt_id": int, "source": str}
    rejected : list of {"image_path": str, "prompt_id": int, "source": str, "reason": str}
    """
    passing: list[dict] = []
    rejected: list[dict] = []

    # Build a working set of hashes seen so far in this batch so we catch
    # intra-batch near-duplicates as well as duplicates against uploaded state.
    session_hashes: list[imagehash.ImageHash] = [
        imagehash.hex_to_hash(h) for h in state.get("uploaded_hashes", [])
    ]

    used_prompt_ids: set = set(state.get("used_prompt_ids", []))

    for item in images:
        path = Path(item["image_path"])

        def _reject(reason: str) -> None:
            rejected.append({**item, "reason": reason})

        # ── 1. Open image ────────────────────────────────────────────────────
        try:
            img = Image.open(path)
            img.load()  # force full decode so corrupt files are caught here
        except Exception:
            _reject("cannot_open")
            continue

        # ── 2. Resolution check ──────────────────────────────────────────────
        w, h = img.size
        if w * h < 4_000_000:
            _reject(f"low_resolution:{w}x{h}")
            continue

        # ── 3. Prompt dedup ──────────────────────────────────────────────────
        if item["prompt_id"] in used_prompt_ids:
            _reject("prompt_already_used")
            continue

        # ── 4. Perceptual hash dedup ─────────────────────────────────────────
        img_hash = imagehash.phash(img)
        if any((img_hash - existing) <= 8 for existing in session_hashes):
            _reject("near_duplicate")
            continue

        # ── 5. JPEG/sRGB conversion ──────────────────────────────────────────
        img_rgb = img.convert("RGB")
        img_rgb.save(str(path), "JPEG", quality=95, icc_profile=_get_srgb_profile())

        # ── 6. File size check (after conversion) ────────────────────────────
        size_bytes = path.stat().st_size
        size_kb = size_bytes // 1024
        if size_bytes < 500 * 1024:
            _reject(f"file_too_small:{size_kb}KB")
            continue

        # ── Passed all checks ────────────────────────────────────────────────
        # Track hash and prompt_id within this session so subsequent items in
        # the same batch are checked against already-accepted images.
        session_hashes.append(img_hash)
        used_prompt_ids.add(item["prompt_id"])

        passing.append({"image_path": item["image_path"], "prompt_id": item["prompt_id"], "source": item["source"]})

    return passing, rejected
