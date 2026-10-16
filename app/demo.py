"""Deterministic Reddit simulator so the whole pipeline and UI can run without API credentials."""
from __future__ import annotations

import hashlib
import json
import math
import random
import time

from . import budget
from .db import tx

SLOT = 720  # one post per subreddit roughly every 12 minutes
NEW_WINDOW = 3 * 3600
SNAP_AGES = [5, 15, 30, 60, 120, 240, 480, 720]

TITLES = {
    "technology": [
        "{co} quietly changes its privacy policy to allow {thing}",
        "EU fines {co} €{n}M over {thing}",
        "{co} announces layoffs of {n}00 employees amid AI push",
        "New battery tech claims {n}x energy density, ships next year",
        "{co} is being sued for {thing}",
        "Right-to-repair bill passes in {place}, {co} lobbied against it",
    ],
    "artificial": [
        "{co} releases a new model that beats GPT on {bench}",
        "Researchers show LLMs can {thing} with only {n}% of the data",
        "I built an agent that {thing} — here's what I learned",
        "{co} CEO says AGI is {n} years away",
        "Open-source model matches {co}'s flagship on {bench}",
    ],
    "localllama": [
        "{co} just dropped open weights for a {n}B model",
        "Running a {n}B model on a single 24GB GPU at 40 tok/s",
        "New quantization method keeps {n}% quality at 3 bits",
        "Benchmark: {n} local models compared on {bench}",
        "Is anyone else getting weird results with the new {co} release?",
    ],
    "gaming": [
        "{co} announces sequel to its {n}-year-old classic",
        "After {n} years, this indie dev finally released their game",
        "{co} reverses decision on {thing} after player backlash",
        "Steam sale: {n} hidden gems under $5",
        "Leaked trailer shows {co}'s next open-world game",
    ],
    "worldnews": [
        "{place} announces nationwide ban on {thing}",
        "Record heatwave hits {place}, temperatures reach {n}°C",
        "{place} and {place2} sign landmark agreement on {thing}",
        "Thousands protest in {place} over {thing}",
    ],
}
GENERIC = [
    "What's your unpopular opinion about {thing}?",
    "TIL that {thing} was invented in {place}",
    "{co} confirms {thing} is coming next month",
    "Discussion: is {thing} overrated?",
]
FILL = {
    "co": ["Apple", "Google", "Meta", "Microsoft", "Nvidia", "OpenAI", "Anthropic", "Valve", "Nintendo", "Samsung", "Mistral", "DeepSeek", "Qwen"],
    "thing": ["training AI on user photos", "facial recognition", "loot boxes", "remote work", "single-use plastics",
              "write its own unit tests", "solve olympiad geometry", "subscription pricing", "right to repair", "always-online DRM"],
    "bench": ["SWE-bench", "MMLU", "GPQA", "HumanEval", "ARC-AGI"],
    "place": ["Japan", "Germany", "Brazil", "Canada", "India", "Australia", "France", "Kenya"],
    "place2": ["South Korea", "Mexico", "Norway", "Indonesia"],
}
SPAM = [
    ("Use promo code SAVE50 for 50% off the best VPN of 2026", None, False),
    ("DM me for cheap followers and likes, fast delivery", None, False),
    ("[Hiring] Remote data labelers needed, $40/hr, apply via Telegram", None, False),
    ("Check out my new YouTube channel where I review gadgets!", "Self-Promotion", False),
    ("Join my Discord for exclusive crypto airdrop signals", None, False),
    ("Weekly Discussion Thread - ask anything here", "Meta", True),
    ("FREE GIFT CARDS FOR EVERYONE CLICK NOW BEFORE IT ENDS", None, False),
]


def _rng(*keys) -> random.Random:
    return random.Random(int(hashlib.md5("|".join(map(str, keys)).encode()).hexdigest()[:12], 16))


def _b36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out or "0"


class FakeRedditClient:
    def __init__(self) -> None:
        self._registry: dict[str, dict] = {}

    def _post(self, sub: str, slot: int) -> dict:
        r = _rng(sub.lower(), slot)
        roll = r.random()
        if roll < 0.04:
            final, tau, cr, ur = r.uniform(2500, 15000), r.uniform(150, 420), r.uniform(0.25, 0.6), r.uniform(0.88, 0.98)
        elif roll < 0.18:
            final, tau, cr, ur = r.uniform(250, 1500), r.uniform(180, 500), r.uniform(0.15, 0.5), r.uniform(0.8, 0.96)
        else:
            final, tau, cr, ur = r.lognormvariate(2.4, 1.0), r.uniform(60, 300), r.uniform(0.1, 0.8), r.uniform(0.55, 0.95)
        tpl = r.choice(TITLES.get(sub.lower(), []) + GENERIC)
        title = tpl.format(**{k: r.choice(v) for k, v in FILL.items()}, n=r.randint(2, 70))
        flair, stickied, author = None, False, f"user_{r.randint(1000, 99999)}"
        if r.random() < 0.07:  # junk / ad posts the filter should catch
            title, flair, stickied = r.choice(SPAM)
            author = r.choice(["deal_bot_99", f"user_{r.randint(1000, 99999)}"])
            final, ur = r.uniform(1, 40), r.uniform(0.2, 0.6)
        pid = _b36(slot) + _b36(int(hashlib.md5(sub.lower().encode()).hexdigest()[:6], 16))[:3]
        p = {
            "id": pid, "subreddit": sub, "title": title, "slot": slot, "flair": flair, "stickied": stickied,
            "created_utc": slot * SLOT + int(r.random() * SLOT),
            "author": author, "final": final, "tau": tau, "cr": cr, "ur": ur,
            "post_type": r.choice(["link", "link", "text", "image", "image", "gallery", "video"]),
        }
        self._registry[pid] = p
        return p

    @staticmethod
    def state(p: dict, now: float) -> dict:
        age = max(0.0, (now - p["created_utc"]) / 60)
        frac = 1 - math.exp(-((age / p["tau"]) ** 1.3))
        score = int(p["final"] * frac) + 1
        comments = int(score * p["cr"] * min(1.0, age / 40 + 0.2))
        is_text = p["post_type"] == "text"
        n_img = {"image": 1, "gallery": 3, "video": 1, "link": 1}.get(p["post_type"], 0)
        return {
            "id": p["id"], "subreddit": p["subreddit"], "title": p["title"],
            "selftext": "Long-time lurker here. Wanted to share this and hear what everyone thinks." if is_text else "",
            "author": p["author"], "url": f"https://example.com/{p['id']}",
            "permalink": f"https://www.reddit.com/r/{p['subreddit']}/comments/{p['id']}/",
            "post_type": p["post_type"], "flair": p.get("flair"), "over_18": 0, "created_utc": p["created_utc"],
            "score": score, "num_comments": comments, "upvote_ratio": round(p["ur"], 2), "removed": False,
            "images": [f"https://picsum.photos/seed/{p['id']}{i}/1200/675" for i in range(n_img)],
            "stickied": p.get("stickied", False), "distinguished": "moderator" if p.get("stickied") else None,
            "promoted": False, "domain": "example.com",
        }

    def _lookup(self, pid: str) -> dict | None:
        if pid in self._registry:
            return self._registry[pid]
        with tx() as c:
            r = c.execute("SELECT subreddit FROM posts WHERE id=?", (pid,)).fetchone()
        return self._post(r["subreddit"], int(pid[:-3], 36)) if r else None

    def new_posts(self, subs: list[str], after: str | None = None, limit: int = 100) -> tuple[list[dict], str | None]:
        budget.ensure("normal")
        budget.record_usage("new", 200)
        now = time.time()
        out = []
        for sub in subs:
            for slot in range(int((now - NEW_WINDOW) // SLOT), int(now // SLOT) + 1):
                p = self._post(sub, slot)
                if p["created_utc"] <= now:
                    out.append(self.state(p, now))
        out.sort(key=lambda x: -x["created_utc"])
        return out[:limit], None

    def info(self, post_ids: list[str], priority: str = "normal") -> list[dict]:
        budget.ensure(priority)
        budget.record_usage("info", 200)
        now = time.time()
        return [self.state(p, now) for p in (self._lookup(i) for i in post_ids[:100]) if p]


def seed_history(client: FakeRedditClient, subs: list[str]) -> None:
    from . import spam
    from .db import get_settings

    settings = get_settings()
    now = time.time()
    posts, snaps = [], []
    for sub in subs:
        for slot in range(int((now - 72 * 3600) // SLOT), int((now - NEW_WINDOW) // SLOT)):
            p = client._post(sub, slot)
            age_min = (now - p["created_utc"]) / 60
            st = client.state(p, now)
            reason = spam.check(st, settings)
            done = age_min > settings["track_hours"] * 60 or bool(reason)
            last = None
            for a in SNAP_AGES:
                if a > age_min:
                    break
                s = client.state(p, p["created_utc"] + a * 60)
                last = s
                snaps.append((p["id"], int(p["created_utc"] + a * 60), float(a), s["score"], s["num_comments"], s["upvote_ratio"]))
            cur = last or st
            posts.append((
                st["id"], sub, st["title"], st["selftext"], st["author"], st["url"], st["permalink"], st["post_type"],
                st["flair"], 0, st["created_utc"], st["created_utc"] + 120, int(now), int(now), 0 if done else 1,
                cur["score"], cur["num_comments"], cur["upvote_ratio"],
                "spam" if reason else "expired" if done else "tracking", reason, json.dumps(st["images"]),
            ))
    usage = []
    r = _rng("usage")
    today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    for d in range(1, 15):
        for _ in range(r.randint(250, 420)):
            usage.append((int(today - d * 86400 + r.random() * 86400), r.choice(["new", "info", "info", "info"]), 200))
    with tx() as c:
        c.executemany(
            """INSERT OR IGNORE INTO posts(id, subreddit, title, selftext, author, url, permalink, post_type, flair, over_18,
               created_utc, first_seen, last_checked, next_check, tracking, score, num_comments, upvote_ratio, status,
               spam_reason, images)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            posts,
        )
        c.executemany("INSERT INTO snapshots VALUES(?,?,?,?,?,?)", snaps)
        c.executemany("INSERT INTO api_usage(ts, endpoint, status_code) VALUES(?,?,?)", usage)
