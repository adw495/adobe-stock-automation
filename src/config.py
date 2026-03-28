import os

from dotenv import load_dotenv

load_dotenv()

ADOBE_SFTP_HOST = os.environ.get("ADOBE_SFTP_HOST")
ADOBE_SFTP_USER = os.environ.get("ADOBE_SFTP_USER")
ADOBE_SFTP_PASS = os.environ.get("ADOBE_SFTP_PASS")
ADOBE_PORTAL_EMAIL = os.environ.get("ADOBE_PORTAL_EMAIL")
ADOBE_PORTAL_PASS = os.environ.get("ADOBE_PORTAL_PASS")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
HUGGINGFACE_TOKEN = os.environ.get("HUGGINGFACE_TOKEN")
LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")

# In CI, portal + Gemini credentials must be present — fail fast with a clear error.
# SFTP vars are optional (SFTP upload not available on standard Adobe accounts).
if os.environ.get("CI") == "true":
    _required = {
        "ADOBE_PORTAL_EMAIL": ADOBE_PORTAL_EMAIL,
        "ADOBE_PORTAL_PASS": ADOBE_PORTAL_PASS,
        "GEMINI_API_KEY": GEMINI_API_KEY,
    }
    _missing = [name for name, value in _required.items() if value is None]
    if _missing:
        raise EnvironmentError(
            f"Missing required environment variables in CI: {', '.join(_missing)}"
        )
