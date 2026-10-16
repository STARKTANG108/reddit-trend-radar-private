from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DB_PATH = Path(_env("DB_PATH", str(BASE_DIR / "data" / "radar.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

REDDIT_CLIENT_ID = _env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = _env("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = _env("REDDIT_USERNAME")
REDDIT_PASSWORD = _env("REDDIT_PASSWORD")
REDDIT_USER_AGENT = _env("REDDIT_USER_AGENT", "python:reddit-trend-radar:v0.1 (by /u/unknown)")

REDDITDATE_BASE_URL = _env("REDDITDATE_BASE_URL", "https://redditdate.top").rstrip("/")
REDDITDATE_API_KEY = _env("REDDITDATE_API_KEY")

LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = _env("LLM_API_KEY")
LLM_MODEL = _env("LLM_MODEL", "gpt-4o-mini")

IMAGE_BASE_URL = _env("IMAGE_BASE_URL", LLM_BASE_URL).rstrip("/")
IMAGE_API_KEY = _env("IMAGE_API_KEY", LLM_API_KEY)
IMAGE_MODEL = _env("IMAGE_MODEL")
IMAGE_SIZE = _env("IMAGE_SIZE", "1536x1024")

MEDIA_DIR = DB_PATH.parent / "media"
CACHE_DIR = DB_PATH.parent / "cache"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SUBREDDITS = [
    s for s in _env("DEFAULT_SUBREDDITS", "Codex,Claude,Anthropic,GeminiAI,OpenAI,google_antigravity").split(",") if s
]

_demo = _env("DEMO_MODE", "auto").lower()
REDDITDATE_CONFIGURED = bool(REDDITDATE_API_KEY)
OAUTH_CONFIGURED = bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET)
REDDIT_CONFIGURED = REDDITDATE_CONFIGURED or OAUTH_CONFIGURED
REDDIT_PROVIDER = "redditdate" if REDDITDATE_CONFIGURED else "oauth" if OAUTH_CONFIGURED else "demo"
DEMO_MODE = _demo in ("1", "true", "yes") or (_demo == "auto" and not REDDIT_CONFIGURED)
LLM_CONFIGURED = bool(LLM_API_KEY)
IMAGE_CONFIGURED = bool(IMAGE_API_KEY and IMAGE_MODEL)
