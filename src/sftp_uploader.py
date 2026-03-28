import csv
import io
import logging
import os
import shutil
from pathlib import Path

import paramiko
from PIL import Image
import imagehash

from src import config

logger = logging.getLogger(__name__)


def _compute_phash(image_path: str) -> str:
    img = Image.open(image_path)
    return str(imagehash.phash(img))


def _build_csv(images: list[dict], metadata_lookup: dict[int, dict]) -> str:
    """Build Adobe Stock bulk metadata CSV as a string."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["Filename", "Title", "Keywords", "Category"])

    for img in images:
        prompt_id = img["prompt_id"]
        meta = metadata_lookup.get(prompt_id)
        if meta is None:
            logger.warning("No metadata found for prompt_id=%s, skipping CSV row", prompt_id)
            continue

        filename = Path(img["image_path"]).name
        title = meta["title"]

        # "AI Generated" prepended as first keyword, then up to 45 from metadata
        raw_keywords = meta.get("keywords", [])
        keywords = ["AI Generated"] + raw_keywords[:45]
        keywords_str = ";".join(keywords)

        category = str(meta["category_id"])

        writer.writerow([filename, title, keywords_str, category])

    return output.getvalue()


def upload_batch(
    images: list[dict],
    metadata: list[dict],
    state: dict,
) -> dict:
    """
    Upload images + CSV metadata to Adobe Stock SFTP.

    Args:
        images:   [{"image_path": str, "prompt_id": int, "source": str}, ...]
        metadata: [{"prompt_id": int, "title": str, "keywords": list[str], "category_id": int}, ...]
        state:    mutable pipeline state dict

    Returns:
        {"uploaded": int, "failed": int, "uploaded_filenames": list[str]}
    """
    # Build metadata lookup keyed by prompt_id
    metadata_lookup: dict[int, dict] = {m["prompt_id"]: m for m in metadata}

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(
            config.ADOBE_SFTP_HOST,
            port=22,
            username=config.ADOBE_SFTP_USER,
            password=config.ADOBE_SFTP_PASS,
            timeout=30,
        )
        sftp = ssh.open_sftp()
    except Exception as exc:
        logger.error("SFTP connection failed: %s", exc)
        return {"uploaded": 0, "failed": len(images), "uploaded_filenames": []}

    uploaded_images: list[dict] = []  # images that were uploaded successfully
    failed_count = 0
    uploaded_filenames: list[str] = []
    batch_dirs: set[str] = set()

    try:
        # Upload each image file
        for img in images:
            local_path = img["image_path"]
            remote_filename = Path(local_path).name

            # Track batch directory for later cleanup
            batch_dir = str(Path(local_path).parent)
            if "batch_" in batch_dir:
                batch_dirs.add(batch_dir)

            try:
                sftp.put(local_path, remote_filename)
                uploaded_images.append(img)
                uploaded_filenames.append(remote_filename)
                logger.info("Uploaded image: %s", remote_filename)
            except Exception as exc:
                logger.error("Failed to upload image %s: %s", remote_filename, exc)
                failed_count += 1

        # Build CSV only for successfully uploaded images
        if uploaded_images:
            csv_str = _build_csv(uploaded_images, metadata_lookup)
            csv_bytes = csv_str.encode("utf-8")

            try:
                sftp.putfo(io.BytesIO(csv_bytes), "metadata.csv")
                logger.info("Uploaded metadata.csv (%d rows)", len(uploaded_images))
            except Exception as exc:
                logger.error("Failed to upload metadata.csv: %s", exc)
                # CSV failure is logged but does not reclassify image uploads as failures

    finally:
        try:
            sftp.close()
        except Exception:
            pass
        try:
            ssh.close()
        except Exception:
            pass

    # Update state for successfully uploaded images
    for img in uploaded_images:
        try:
            phash_str = _compute_phash(img["image_path"])
            state.setdefault("uploaded_hashes", []).append(phash_str)
        except Exception as exc:
            logger.warning("Could not compute phash for %s: %s", img["image_path"], exc)

        state.setdefault("used_prompt_ids", []).append(img["prompt_id"])

    uploaded_count = len(uploaded_images)
    state["total_uploaded"] = state.get("total_uploaded", 0) + uploaded_count

    # Clean up /tmp/batch_* directories
    for batch_dir in batch_dirs:
        try:
            shutil.rmtree(batch_dir)
            logger.info("Cleaned up batch directory: %s", batch_dir)
        except Exception as exc:
            logger.warning("Could not remove batch directory %s: %s", batch_dir, exc)

    return {
        "uploaded": uploaded_count,
        "failed": failed_count,
        "uploaded_filenames": uploaded_filenames,
    }
