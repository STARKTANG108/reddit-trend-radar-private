from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS subreddits (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    subreddit TEXT NOT NULL,
    title TEXT NOT NULL,
    selftext TEXT,
    author TEXT,
    url TEXT,
    permalink TEXT,
    post_type TEXT,
    flair TEXT,
    over_18 INTEGER DEFAULT 0,
    created_utc INTEGER NOT NULL,
    first_seen INTEGER NOT NULL,
    last_checked INTEGER,
    next_check INTEGER,
    tracking INTEGER NOT NULL DEFAULT 1,
    score INTEGER DEFAULT 0,
    num_comments INTEGER DEFAULT 0,
    upvote_ratio REAL DEFAULT 0,
    hot_score REAL DEFAULT 0,
    peak_hot REAL DEFAULT 0,
    reasons TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'tracking',
    candidate_at INTEGER,
    images TEXT DEFAULT '[]',
    spam_reason TEXT,
    image_prompt TEXT,
    link_context TEXT,
    title_zh TEXT,
    selftext_zh TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_track ON posts(tracking, next_check);
CREATE INDEX IF NOT EXISTS idx_posts_sub ON posts(subreddit, created_utc);
CREATE TABLE IF NOT EXISTS snapshots (
    post_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    age_min REAL NOT NULL,
    score INTEGER,
    num_comments INTEGER,
    upvote_ratio REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_post ON snapshots(post_id, ts);
CREATE TABLE IF NOT EXISTS tweets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    style TEXT,
    content TEXT NOT NULL,
    card_title TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tweets_status ON tweets(status);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    prompt TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    status_code INTEGER,
    remaining REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON api_usage(ts);
CREATE TABLE IF NOT EXISTS baselines (
    subreddit TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    p50 REAL, p90 REAL, c50 REAL, c90 REAL,
    n INTEGER,
    updated_at INTEGER,
    PRIMARY KEY (subreddit, bucket)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MIGRATIONS = [
    ("posts", "images", "TEXT DEFAULT '[]'"),
    ("posts", "spam_reason", "TEXT"),
    ("posts", "image_prompt", "TEXT"),
    ("posts", "link_context", "TEXT"),
    ("posts", "title_zh", "TEXT"),
    ("posts", "selftext_zh", "TEXT"),
    ("tweets", "card_title", "TEXT"),
    ("tweets", "used_at", "INTEGER"),
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "quota_mode": "monthly",
    "quota_limit": 100000,
    "quota_total_days": 90,
    "poll_new_minutes": 10,
    "refresh_check_minutes": 5,
    "track_hours": 12,
    "early_drop_hours": 2,
    "candidate_threshold": 60,
    "candidate_max_age_hours": 6,
    "min_comments": 10,
    "allow_nsfw": False,
    "dedupe_similarity": 0.75,
    "spam_filter": True,
    "spam_keywords": "promo code,discount code,coupon,use code,% off,giveaway,dm me,dm for,telegram,whatsapp,onlyfans,"
                     "airdrop,free followers,buy now,limited offer,sign up now,link in bio,affiliate,referral,check out my,"
                     "subscribe to my,my channel,join my discord,for sale,[hiring],[for hire],代购,优惠码,加微信,私信我,推广",
    "spam_domains": "bit.ly,tinyurl.com,amzn.to,linktr.ee,t.me,wa.me,onlyfans.com,aliexpress.com,temu.com,gofundme.com,"
                    "patreon.com,ko-fi.com",
    "spam_flairs": "promotion,self-promotion,self promo,sponsored,advertisement,ad,meta,mod post,announcement,"
                   "weekly thread,daily thread,megathread",
    "max_author_posts_24h": 3,
    "min_upvote_ratio": 0.4,
    "auto_generate": True,
    "variants": 3,
    "tweet_language": "zh",
    "tweet_styles": "爆点钩子,深度解读,轻松吐槽",
    "max_tweet_weight": 280,
    "custom_persona": "",
    "fetch_link_context": True,
    "append_link": False,
    "desktop_notify": False,
    "webhook_url": "",
    "webhook_min_hot": 70,
    "retention_days": 14,
}

_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with tx() as c:
        c.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def rows(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def row(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_settings() -> dict[str, Any]:
    with tx() as c:
        stored = {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM settings")}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}


def update_settings(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for k, v in values.items():
        if k not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            clean[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            clean[k] = int(float(v))
        elif isinstance(default, float):
            clean[k] = float(v)
        else:
            clean[k] = str(v)
    with tx() as c:
        c.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in clean.items()],
        )
    return get_settings()
