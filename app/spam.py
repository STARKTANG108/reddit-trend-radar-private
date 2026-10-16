"""Junk / ad filter that only uses fields already present in the listing payload (zero extra API calls)."""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from .db import tx

URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.I)
THREAD_RE = re.compile(r"\b(daily|weekly|monthly)\b.*\b(thread|discussion|megathread)\b|\bmegathread\b", re.I)
SHORT_WORD_RE = re.compile(r"^[a-z0-9]{1,4}$")


def _csv(value: str) -> list[str]:
    return [x.strip().lower() for x in (value or "").split(",") if x.strip()]


def _extract_hosts(p: dict) -> set[str]:
    hosts: set[str] = set()
    dom = (p.get("domain") or "").strip().lower().split(":")[0]
    if dom and not dom.startswith("self."):
        hosts.add(dom)
    for raw in [p.get("url") or "", *URL_RE.findall(p.get("selftext") or "")]:
        if not raw:
            continue
        try:
            h = (urlparse(raw).hostname or "").lower()
            if h:
                hosts.add(h)
        except ValueError:
            pass
    return hosts


def _domain_hit(dm: str, hosts: set[str], blob: str) -> bool:
    dm = dm.removeprefix("http://").removeprefix("https://").strip("/")
    if not dm:
        return False
    if "/" not in dm:
        if any(h == dm or h.endswith("." + dm) for h in hosts):
            return True
    pattern = rf"(?<![a-z0-9._-])(?:[a-z0-9-]+\.)*{re.escape(dm)}(?=$|[/:\s?#)\]>\"'])"
    return bool(re.search(pattern, blob))


def _keyword_hit(kw: str, text: str) -> bool:
    if SHORT_WORD_RE.match(kw):
        return bool(re.search(rf"\b{re.escape(kw)}\b", text))
    return kw in text


def author_counts(authors: list[str], since: int) -> dict[str, int]:
    authors = [a for a in set(authors) if a and a != "[deleted]"]
    if not authors:
        return {}
    with tx() as c:
        rs = c.execute(
            f"SELECT author, COUNT(*) n FROM posts WHERE created_utc >= ? AND author IN ({','.join('?' * len(authors))}) GROUP BY author",
            [since, *authors],
        )
        return {r["author"]: r["n"] for r in rs}


def check(p: dict, s: dict, author_posts: int = 0) -> str | None:
    """Returns a human-readable reason if the post looks like junk/ads, else None."""
    if not s["spam_filter"]:
        return None
    if p.get("promoted"):
        return "广告投放帖"
    if p.get("stickied"):
        return "置顶帖"
    if p.get("distinguished") in ("moderator", "admin"):
        return "版务帖"
    title = p.get("title") or ""
    text = f"{title}\n{p.get('selftext') or ''}".lower()
    for kw in _csv(s["spam_keywords"]):
        if _keyword_hit(kw, text):
            return f"命中广告关键词「{kw}」"
    hosts = _extract_hosts(p)
    blob = f"{p.get('url') or ''} {p.get('selftext') or ''}".lower()
    for dm in _csv(s["spam_domains"]):
        if _domain_hit(dm, hosts, blob):
            return f"包含推广域名 {dm}"
    flair = (p.get("flair") or "").strip().lower()
    if flair and flair in _csv(s["spam_flairs"]):
        return f"flair 为「{p['flair']}」"
    if THREAD_RE.search(title):
        return "固定讨论串"
    if len(URL_RE.findall(p.get("selftext") or "")) >= 4:
        return "正文外链过多"
    letters = [ch for ch in title if ch.isalpha() and ch.isascii()]
    if len(letters) >= 15 and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:
        return "标题全大写"
    if len(title.strip()) < 6:
        return "标题过短"
    if author_posts > s["max_author_posts_24h"]:
        return f"同作者 24h 内发帖 {author_posts} 条，疑似刷屏"
    return None


def check_engagement(p: dict, s: dict, now: float | None = None) -> str | None:
    """Post-refresh check: heavily downvoted posts are almost always ads or bait."""
    if not s["spam_filter"]:
        return None
    age_min = ((now or time.time()) - p["created_utc"]) / 60
    if age_min >= 30 and p.get("upvote_ratio") and p["upvote_ratio"] < s["min_upvote_ratio"]:
        return f"好评率仅 {p['upvote_ratio']:.0%}，疑似广告/引战"
    return None
