"""
prompt_engine.py — picks unused prompts from bank.json and marks them used.
"""

import json
from pathlib import Path

BANK_PATH = Path(__file__).parent.parent / "prompts" / "bank.json"


def _load_bank() -> list[dict]:
    """Load and return the prompt bank as a list of dicts."""
    return json.loads(BANK_PATH.read_text())


def _save_bank(bank: list[dict]) -> None:
    """Write the prompt bank back to disk."""
    BANK_PATH.write_text(json.dumps(bank, indent=2))


def pick_prompts(state: dict, count: int) -> list[dict]:
    """
    Pick `count` unused prompts from bank.json.

    Returns a list of {"id": N, "prompt": "...", "category": "..."} dicts.
    Updates state["last_prompt_index"] as a watermark pointing to the index
    immediately after the last picked prompt.

    Does NOT mark prompts as used — caller must call mark_used() with the
    returned IDs when the prompts have been processed.

    If fewer than `count` unused prompts remain from the watermark onwards,
    wraps around to the beginning of the bank. If the entire bank is exhausted,
    returns however many unused prompts are available (may be fewer than `count`).
    """
    bank = _load_bank()
    total = len(bank)
    start_index = state.get("last_prompt_index", 0)

    picked: list[dict] = []
    # We allow one full wrap-around pass at most.
    checked = 0

    index = start_index % total if total else 0

    while len(picked) < count and checked < total:
        entry = bank[index]
        if not entry["used"]:
            picked.append({
                "id": entry["id"],
                "prompt": entry["prompt"],
                "category": entry["category"],
            })
        index = (index + 1) % total
        checked += 1

    # Update the watermark to point just past the last examined position.
    state["last_prompt_index"] = index
    return picked


def mark_used(prompt_ids: list[int]) -> None:
    """
    Mark the given prompt IDs as used=True in bank.json and save to disk.

    Silently ignores IDs that do not exist in the bank.
    """
    bank = _load_bank()
    id_set = set(prompt_ids)
    for entry in bank:
        if entry["id"] in id_set:
            entry["used"] = True
    _save_bank(bank)


def reset_bank() -> None:
    """
    Reset all prompts to used=False.

    Called by the weekly refresh workflow to recycle the prompt bank.
    """
    bank = _load_bank()
    for entry in bank:
        entry["used"] = False
    _save_bank(bank)
