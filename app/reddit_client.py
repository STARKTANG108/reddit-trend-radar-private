from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

import httpx

from . import budget, config
from .db import rows, tx

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def extract_images(d: dict) -> list[str]:
    """Image URLs that come for free in the listing payload (gallery / preview / direct image / thumbnail)."""
    out: list[str] = []
    meta = d.get("media_metadata") or {}
    if d.get("is_gallery") and meta:
        order = [i.get("media_id") for i in (d.get("gallery_data") or {}).get("items", [])] or list(meta)
        for mid in order:
            s = (meta.get(mid) or {}).get("s") or {}
            if s.get("u") or s.get("gif"):
                out.append(s.get("u") or s.get("gif"))
    url = d.get("url") or ""
    if url.lower().split("?")[0].endswith(IMAGE_EXT):
        out.append(url)
    for img in (d.get("preview") or {}).get("images", []):
        if (img.get("source") or {}).get("url"):
            out.append(img["source"]["url"])
    thumb = d.get("thumbnail") or ""
    if not out and thumb.startswith("http"):
        out.append(thumb)
    return list(dict.fromkeys(out))[:6]


def normalize_post(d: dict) -> dict:
    url = d.get("url") or ""
    if d.get("is_self"):
        post_type = "text"
    elif d.get("is_video"):
        post_type = "video"
    elif d.get("is_gallery"):
        post_type = "gallery"
    elif d.get("post_hint") == "image" or url.lower().endswith(IMAGE_EXT):
        post_type = "image"
    else:
        post_type = "link"
    return {
        "id": d["id"],
        "subreddit": d.get("subreddit", ""),
        "title": d.get("title", ""),
        "selftext": (d.get("selftext") or "")[:4000],
        "author": d.get("author"),
        "url": url,
        "permalink": "https://www.reddit.com" + d.get("permalink", ""),
        "post_type": post_type,
        "flair": d.get("link_flair_text"),
        "over_18": int(bool(d.get("over_18"))),
        "created_utc": int(d.get("created_utc", time.time())),
        "score": int(d.get("score", 0)),
        "num_comments": int(d.get("num_comments", 0)),
        "upvote_ratio": float(d.get("upvote_ratio", 0) or 0),
        "removed": bool(d.get("removed_by_category")) or d.get("author") == "[deleted]",
        "images": extract_images(d),
        "stickied": bool(d.get("stickied") or d.get("pinned")),
        "distinguished": d.get("distinguished"),
        "promoted": bool(d.get("promoted") or d.get("is_created_from_ads_ui")),
        "domain": d.get("domain") or "",
    }


def normalize_redditdate_post(d: dict) -> dict:
    """Normalize a post item from RedditDate Data API (https://redditdate.top)."""
    pub = d.get("published_at")
    if isinstance(pub, (int, float)):
        created_utc = int(pub)
    elif isinstance(pub, str) and pub:
        try:
            created_utc = int(datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp())
        except ValueError:
            created_utc = int(time.time())
    else:
        created_utc = int(time.time())

    url = d.get("url") or d.get("reddit_url") or ""
    permalink = d.get("reddit_url") or f"https://www.reddit.com/r/{d.get('subreddit', '')}/comments/{d['id']}/"
    imgs: list[str] = []
    if url.lower().split("?")[0].endswith(IMAGE_EXT):
        imgs.append(url)
    for m in d.get("media") or []:
        murl = m.get("url") or ""
        turl = m.get("thumbnail_url") or ""
        if m.get("type") == "image" and murl.startswith("http"):
            imgs.append(murl)
        elif murl.lower().split("?")[0].endswith(IMAGE_EXT):
            imgs.append(murl)
        if turl.startswith("http"):
            imgs.append(turl)

    raw_content = (d.get("content") or "").strip()
    is_removed_body = raw_content.lower() in ("[removed]", "[deleted]")
    selftext = "" if is_removed_body else raw_content[:4000]
    title = d.get("title") or ""
    author = d.get("author")
    post_type = d.get("post_type") or ("text" if url == permalink else "link")
    removed = (
        bool(d.get("removed"))
        or author in ("[deleted]", "[removed]")
        or title.strip().lower().startswith("[ removed by")
        or (post_type == "text" and is_removed_body)
    )
    try:
        domain = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        domain = ""
    return {
        "id": str(d["id"]),
        "subreddit": d.get("subreddit", ""),
        "title": title,
        "selftext": selftext,
        "author": author,
        "url": url,
        "permalink": permalink,
        "post_type": post_type,
        "flair": d.get("flair") or d.get("link_flair_text"),
        "over_18": int(bool(d.get("is_nsfw") or d.get("over_18"))),
        "created_utc": created_utc,
        "score": int(d.get("score") or 0),
        "num_comments": int(d.get("comment_count") if d.get("comment_count") is not None else (d.get("num_comments") or 0)),
        "upvote_ratio": float(d.get("upvote_ratio") or 0.9),
        "removed": removed,
        "images": list(dict.fromkeys(imgs))[:6],
        "stickied": bool(d.get("is_stickied") or d.get("stickied")),
        "distinguished": d.get("distinguished"),
        "promoted": bool(d.get("promoted")),
        "domain": domain,
    }


class RedditClient:
    """Minimal OAuth client. Every data request is counted against the local quota."""

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    BASE = "https://oauth.reddit.com"
    SINGLE_SUB = False

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_exp = 0.0
        self._http = httpx.Client(timeout=20, headers={"User-Agent": config.REDDIT_USER_AGENT})

    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        data = (
            {"grant_type": "password", "username": config.REDDIT_USERNAME, "password": config.REDDIT_PASSWORD}
            if config.REDDIT_USERNAME and config.REDDIT_PASSWORD
            else {"grant_type": "client_credentials"}
        )
        r = self._http.post(self.TOKEN_URL, data=data, auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET))
        r.raise_for_status()
        payload = r.json()
        if "access_token" not in payload:
            raise RuntimeError(f"Reddit 授权失败: {payload}")
        self._token = payload["access_token"]
        self._token_exp = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, params: dict, endpoint: str, priority: str) -> dict | list:
        budget.ensure(priority)
        for attempt in range(2):
            r = self._http.get(
                self.BASE + path,
                params={**params, "raw_json": 1},
                headers={"Authorization": f"bearer {self._auth()}"},
            )
            remaining = r.headers.get("x-ratelimit-remaining")
            budget.record_usage(endpoint, r.status_code, float(remaining) if remaining else None)
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code == 429:
                reset = float(r.headers.get("x-ratelimit-reset", "60"))
                raise budget.QuotaExceeded(f"Reddit 限流，{reset:.0f}s 后重置")
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Reddit 请求失败")

    def new_posts(self, subs: list[str], after: str | None = None, limit: int = 100) -> tuple[list[dict], str | None]:
        params = {"limit": limit}
        if after:
            params["after"] = after
        data = self._get(f"/r/{'+'.join(subs)}/new", params, "new", "normal")
        listing = data.get("data", {})
        return [normalize_post(c["data"]) for c in listing.get("children", [])], listing.get("after")

    def info(self, post_ids: list[str], priority: str = "normal") -> list[dict]:
        data = self._get("/api/info", {"id": ",".join(f"t3_{i}" for i in post_ids[:100])}, "info", priority)
        return [normalize_post(c["data"]) for c in data.get("data", {}).get("children", [])]


class RedditDateClient:
    """Client for RedditDate Data API (https://redditdate.top) using X-API-Key authentication."""

    SINGLE_SUB = True

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        raw_key = (api_key or config.REDDITDATE_API_KEY).strip()
        if raw_key == "sk_live_aLA_O3et24Tdr9HA8gigAtcg9--To6V4xH-gERoyWz":
            raw_key += "c"
        self.api_key = raw_key
        self.base_url = (base_url or config.REDDITDATE_BASE_URL).rstrip("/")
        self._http = httpx.Client(
            timeout=25,
            headers={"X-API-Key": self.api_key, "User-Agent": config.REDDIT_USER_AGENT},
        )
        self._usage_cache: tuple[float, dict] | None = None

    def account_usage(self) -> dict | None:
        now = time.time()
        if self._usage_cache and now - self._usage_cache[0] < 60:
            return self._usage_cache[1]
        try:
            r = self._http.get(f"{self.base_url}/v1/account/usage")
            if r.status_code == 200:
                data = r.json()
                self._usage_cache = (now, data)
                return data
        except Exception:
            pass
        return self._usage_cache[1] if self._usage_cache else None

    def _get(self, path: str, params: dict, endpoint: str, priority: str) -> dict:
        budget.ensure(priority)
        for attempt in range(2):
            r = self._http.get(f"{self.base_url}{path}", params=params)
            if r.status_code in (502, 503, 504) and attempt == 0:
                time.sleep(1.5)
                continue
            break
        u = self.account_usage()
        rem = u.get("remaining_records") if u else None
        budget.record_usage(endpoint, r.status_code, float(rem) if rem is not None else None)
        if r.status_code == 402:
            raise budget.QuotaExceeded("RedditDate 账号额度不足")
        if r.status_code == 429:
            raise budget.QuotaExceeded("RedditDate 请求过快，触发限流")
        r.raise_for_status()
        return r.json()

    def new_posts(self, subs: list[str], after: str | None = None, limit: int = 25) -> tuple[list[dict], str | None]:
        if not subs:
            return [], None
        if len(subs) == 1:
            import random

            # Vary limit dynamically to bypass redditdate.top server-side stale cache on fixed limit=25
            req_limit = random.choice([28, 29, 31, 32, 33, 34, 35]) if (limit == 25 and not after) else min(limit, 50)
            params: dict = {"limit": req_limit, "sort": "new"}
            if after:
                params["after"] = int(after)
            payload = self._get(f"/v1/subreddits/{subs[0]}/posts", params, "new", "normal")
            data = payload.get("data") or {}
            items = [normalize_redditdate_post(x) for x in data.get("items", [])]
            nxt = (data.get("pagination") or {}).get("next_cursor")
            return items, str(nxt) if nxt else None
        out: list[dict] = []
        for s in subs:
            items, _ = self.new_posts([s], after=None, limit=min(25, limit))
            out.extend(items)
        out.sort(key=lambda x: -x["created_utc"])
        return out, None

    def info(self, post_ids: list[str], priority: str = "normal") -> list[dict]:
        """Refresh posts via /v1/subreddits/{sub}/posts using timestamp cursor clustering."""
        if not post_ids:
            return []
        marks = ",".join("?" * len(post_ids))
        with tx() as c:
            targets = rows(c, f"SELECT id, subreddit, created_utc FROM posts WHERE id IN ({marks})", post_ids)
        by_sub: dict[str, list[dict]] = defaultdict(list)
        for t in targets:
            by_sub[t["subreddit"]].append(t)
        results: dict[str, dict] = {}
        for sub, group in by_sub.items():
            group.sort(key=lambda x: -x["created_utc"])
            # Cluster posts within 45 minutes of each other to minimize API calls and record usage
            clusters: list[list[dict]] = []
            for p in group:
                if not clusters or (clusters[-1][0]["created_utc"] - p["created_utc"]) > 2700 or len(clusters[-1]) >= 15:
                    clusters.append([p])
                else:
                    clusters[-1].append(p)
            for cl in clusters:
                max_ts = cl[0]["created_utc"]
                span_min = max(1.0, (max_ts - cl[-1]["created_utc"]) / 60)
                lim = 2 if len(cl) == 1 else min(25, max(len(cl) + 3, int(span_min / 2) + 4))
                payload = self._get(
                    f"/v1/subreddits/{sub}/posts",
                    {"limit": lim, "sort": "new", "after": max_ts + 2},
                    "info",
                    priority,
                )
                for item in (payload.get("data") or {}).get("items", []):
                    norm = normalize_redditdate_post(item)
                    results[norm["id"]] = norm
        return list(results.values())
