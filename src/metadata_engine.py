import asyncio
import json
import google.generativeai as genai
from src import config

SYSTEM_PROMPT = """You are an Adobe Stock metadata specialist. Given an image generation prompt, return a JSON object with exactly these fields:
- title: string, max 70 characters, commercially oriented, present tense, no artist references
- keywords: array of exactly 45 strings, mix of specific and broad, commercially relevant, no duplicates
- category_id: integer — one of: 1 (Business), 44 (Technology/Science), 22 (Abstract), 12 (Backgrounds/Textures)

Respond with ONLY valid JSON, no explanation, no markdown code blocks. Example:
{"title": "Digital Network Connection Concept", "keywords": ["digital", "network", "connection", ...], "category_id": 44}"""

_FALLBACK_KEYWORDS = [
    "abstract", "digital", "concept", "background", "modern", "design",
    "creative", "art", "technology", "business", "professional", "clean",
    "minimal", "graphic", "visual", "illustration", "template", "banner",
    "wallpaper", "texture", "pattern", "color", "gradient", "smooth", "bright",
    "dark", "light", "blue", "white", "black", "gray", "green", "red", "orange",
    "purple", "yellow", "pink", "gold", "silver", "metallic", "glossy", "matte",
    "flat", "3d", "render",
]

_VALID_CATEGORY_IDS = {1, 44, 22, 12}
_GENERIC_PAD_TERMS = [
    "abstract", "digital", "concept", "background", "modern", "design",
    "creative", "art", "technology", "business", "professional", "clean",
    "minimal", "graphic", "visual", "illustration", "template", "banner",
    "wallpaper", "texture", "pattern", "color", "gradient", "smooth", "bright",
    "dark", "light", "blue", "white", "black", "gray", "green", "red", "orange",
    "purple", "yellow", "pink", "gold", "silver", "metallic", "glossy", "matte",
    "flat", "3d", "render",
]


def _fallback_metadata(prompt: dict) -> dict:
    return {
        "prompt_id": prompt["id"],
        "title": prompt["prompt"][:70],
        "keywords": _FALLBACK_KEYWORDS.copy(),
        "category_id": 22,
    }


def _validate_metadata(raw: dict, prompt: dict) -> dict:
    """Validate and normalise a parsed metadata dict."""
    # Title
    title = str(raw.get("title", prompt["prompt"]))[:70]

    # Keywords — deduplicate, then truncate or pad to exactly 45
    raw_keywords = raw.get("keywords", [])
    seen = set()
    keywords = []
    for kw in raw_keywords:
        kw = str(kw).strip()
        if kw and kw not in seen:
            seen.add(kw)
            keywords.append(kw)

    if len(keywords) > 45:
        keywords = keywords[:45]
    elif len(keywords) < 45:
        for pad in _GENERIC_PAD_TERMS:
            if len(keywords) >= 45:
                break
            if pad not in seen:
                seen.add(pad)
                keywords.append(pad)

    # Category ID
    try:
        category_id = int(raw.get("category_id", 22))
    except (TypeError, ValueError):
        category_id = 22
    if category_id not in _VALID_CATEGORY_IDS:
        category_id = 22

    return {
        "prompt_id": prompt["id"],
        "title": title,
        "keywords": keywords,
        "category_id": category_id,
    }


def _call_gemini(prompt_text: str) -> str:
    """Synchronous Gemini API call — intended to run inside asyncio.to_thread()."""
    genai.configure(api_key=config.GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=SYSTEM_PROMPT,
    )
    response = model.generate_content(f"Prompt: {prompt_text}")
    return response.text


def _parse_response(text: str) -> dict:
    """Strip optional markdown code fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence (e.g. ```json or ```)
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


async def _generate_single(prompt: dict) -> dict:
    """Generate metadata for a single prompt dict, with fallback on any error."""
    try:
        raw_text = await asyncio.to_thread(_call_gemini, prompt["prompt"])
        raw = _parse_response(raw_text)
        return _validate_metadata(raw, prompt)
    except Exception:
        return _fallback_metadata(prompt)


async def generate_metadata(prompts: list[dict]) -> list[dict]:
    """
    Generate metadata for a list of prompt dicts.
    Each input:  {"id": int, "prompt": str, "category": str}
    Each output: {"prompt_id": int, "title": str, "keywords": list[str], "category_id": int}
    On failure for any prompt, fallback metadata is used (not None).
    """
    tasks = [_generate_single(prompt) for prompt in prompts]
    results = await asyncio.gather(*tasks)
    return list(results)
