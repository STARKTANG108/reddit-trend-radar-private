"""Hotness scoring: compare each post with its own subreddit's baseline at the same age."""
from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlparse

from .db import get_settings, rows, tx

BUCKETS = [15, 30, 60, 120, 240, 480, 720, 1440]
MIN_SAMPLES = 15
_cache: dict[str, dict[int, dict]] = {}
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid", "gclid", "mc_cid", "mc_eid"}
_INTERNAL_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "i.redd.it", "v.redd.it", "preview.redd.it", "i.imgur.com", "imgur.com", "example.com"}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def rebuild_baselines(days: int = 14) -> int:
    since = int(time.time()) - days * 86400
    with tx() as c:
        data = rows(
            c,
            """SELECT p.subreddit, s.post_id, s.age_min, s.score, s.num_comments FROM snapshots s
               JOIN posts p ON p.id = s.post_id WHERE p.created_utc >= ?""",
            (since,),
        )
    # nearest snapshot per (post, bucket) within ±35% of the bucket age
    best: dict[tuple[str, str, int], tuple[float, int, int]] = {}
    for r in data:
        sub_lc = (r["subreddit"] or "").lower()
        for b in BUCKETS:
            d = abs(r["age_min"] - b)
            if d <= b * 0.35:
                key = (sub_lc, r["post_id"], b)
                if key not in best or d < best[key][0]:
                    best[key] = (d, r["score"], r["num_comments"])
    groups: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    for (sub, _, b), (_, sc, cm) in best.items():
        groups[(sub, b)].append((sc, cm))
        groups[("*", b)].append((sc, cm))
    now = int(time.time())
    out = []
    for (sub, b), vals in groups.items():
        scores, comments = [v[0] for v in vals], [v[1] for v in vals]
        out.append((sub, b, _pct(scores, 0.5), _pct(scores, 0.9), _pct(comments, 0.5), _pct(comments, 0.9), len(vals), now))
    with tx() as c:
        c.execute("DELETE FROM baselines")
        c.executemany("INSERT INTO baselines VALUES(?,?,?,?,?,?,?,?)", out)
    _cache.clear()
    return len(out)


def _load(sub: str) -> dict[int, dict]:
    key = sub.lower()
    if key not in _cache:
        with tx() as c:
            _cache[key] = {r["bucket"]: r for r in rows(c, "SELECT * FROM baselines WHERE lower(subreddit)=?", (key,))}
    return _cache[key]


def _bucket_value(sub: str, b: int) -> dict:
    for src in (sub, "*"):
        r = _load(src).get(b)
        if r and r["n"] >= MIN_SAMPLES:
            return r
    scale = b / 15
    return {"p50": 2 * scale ** 0.6, "p90": 8 * scale ** 0.75, "c50": 1 * scale ** 0.6, "c90": 4 * scale ** 0.7, "n": 0}


def baseline_at(sub: str, age_min: float) -> dict:
    """Log-linear interpolation between age buckets."""
    a = min(max(age_min, BUCKETS[0]), BUCKETS[-1])
    for lo, hi in zip(BUCKETS, BUCKETS[1:]):
        if a <= hi:
            break
    t = (math.log(a) - math.log(lo)) / (math.log(hi) - math.log(lo))
    vlo, vhi = _bucket_value(sub, lo), _bucket_value(sub, hi)
    out = {k: vlo[k] + (vhi[k] - vlo[k]) * t for k in ("p50", "p90", "c50", "c90")}
    if age_min < BUCKETS[0]:
        out = {k: v * max(age_min, 1) / BUCKETS[0] for k, v in out.items()}
    out["n"] = min(vlo["n"], vhi["n"])
    return out


def _sig(x: float) -> float:
    return x / (1 + x) if x > 0 else 0.0


def compute(post: dict, snaps: list[dict], now: float | None = None) -> tuple[float, list[str]]:
    now = now or time.time()
    age = max(1.0, (now - post["created_utc"]) / 60)
    b = baseline_at(post["subreddit"], age)
    n = post["num_comments"]
    sc = post.get("score") or 0
    ur = post.get("upvote_ratio") or 0.0
    comment_ratio = n / max(b["c90"], 2)
    score_ratio = sc / max(b["p90"], 5)
    velocity = n / age
    # comment acceleration: recent comments/min vs lifetime comments/min
    accel = 1.0
    if len(snaps) >= 2:
        s1, s2 = snaps[-2], snaps[-1]
        dt = max(1.0, s2["age_min"] - s1["age_min"])
        accel = ((s2["num_comments"] - s1["num_comments"]) / dt) / max(s1["num_comments"] / max(s1["age_min"], 1), 0.05)
    base_sig = 0.8 * _sig(comment_ratio) + 0.2 * _sig(accel)
    score_bonus = 0.05 * _sig(score_ratio) * _sig(comment_ratio)
    hot = min(100.0, 100 * (base_sig + score_bonus))
    if age < 10:
        hot *= 0.8
    reasons = [
        f"评论 {n} 条，是该社区同帖龄 P90({b['c90']:.0f}) 的 {comment_ratio:.1f} 倍",
        f"平均每分钟 +{velocity:.2f} 条评论（中位帖子同帖龄约 {b['c50']:.0f} 条）",
    ]
    if accel >= 1.3:
        reasons.append(f"最近评论增速是平均的 {accel:.1f} 倍，讨论还在升温")
    elif len(snaps) >= 2 and accel < 0.5:
        reasons.append("评论增速已明显放缓")
    if n >= 15 and 0 < ur <= 0.68:
        reasons.append(f"⚔️ 高争议话题：好评率 {ur:.0%} 但评论密集，正反方观点交锋激烈")
    elif score_ratio >= 1.5 and ur >= 0.9 and n >= 10:
        reasons.append(f"🔥 高赞共识爆款：点赞 {sc}（同帖龄 P90 的 {score_ratio:.1f} 倍），好评率 {ur:.0%}")
    if b["n"] == 0:
        reasons.append("该社区基线样本不足，使用默认基线")
    return round(hot, 1), reasons


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff ]", "", t.lower())


def _norm_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return ""
    host = (p.hostname or "").lower().removeprefix("www.")
    if not host or host in _INTERNAL_HOSTS or host.endswith(".reddit.com") or host.endswith(".redd.it"):
        return ""
    path = p.path.rstrip("/")
    if not path or path == "":
        return ""
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING_PARAMS])
    return f"{host}{path}{'?' + q if q else ''}"


def is_duplicate(
    title: str,
    threshold: float,
    exclude_id: str,
    url: str | None = None,
    extra_recent: list[dict] | None = None,
) -> str | None:
    since = int(time.time()) - 86400
    with tx() as c:
        recent = rows(
            c,
            "SELECT id, title, url FROM posts WHERE id != ? AND status IN ('candidate','drafted','used') AND candidate_at >= ?",
            (exclude_id, since),
        )
    if extra_recent:
        recent.extend(r for r in extra_recent if r["id"] != exclude_id)
    n = _norm(title)
    nu = _norm_url(url)
    for r in recent:
        if nu and _norm_url(r.get("url")) == nu:
            return r["id"]
        if SequenceMatcher(None, n, _norm(r["title"])).ratio() >= threshold:
            return r["id"]
    return None


def score_posts(post_ids: list[str]) -> list[str]:
    """Rescore posts, persist results, and promote qualifying ones. Returns newly promoted ids."""
    if not post_ids:
        return []
    s = get_settings()
    now = time.time()
    marks = ",".join("?" * len(post_ids))
    with tx() as c:
        posts = rows(c, f"SELECT * FROM posts WHERE id IN ({marks})", post_ids)
        snap_rows = rows(c, f"SELECT * FROM snapshots WHERE post_id IN ({marks}) ORDER BY ts", post_ids)
    snaps: dict[str, list[dict]] = defaultdict(list)
    for r in snap_rows:
        snaps[r["post_id"]].append(r)
    scored = []
    for p in posts:
        hot, reasons = compute(p, snaps[p["id"]], now)
        scored.append((hot, reasons, p))
    # Sort descending by hotness so the strongest post wins if two posts in the same batch are duplicates
    scored.sort(key=lambda x: -x[0])
    updates, promoted, batch_recent = [], [], []
    for hot, reasons, p in scored:
        age_h = (now - p["created_utc"]) / 3600
        status, cand_at = p["status"], p["candidate_at"]
        if (
            status == "tracking"
            and hot >= s["candidate_threshold"]
            and age_h <= s["candidate_max_age_hours"]
            and p["num_comments"] >= s["min_comments"]
            and (s["allow_nsfw"] or not p["over_18"])
        ):
            dup = is_duplicate(p["title"], s["dedupe_similarity"], p["id"], url=p.get("url"), extra_recent=batch_recent)
            if dup:
                reasons.append(f"与 {dup} 话题或外链重复，未进入候选")
            else:
                status, cand_at = "candidate", int(now)
                promoted.append(p["id"])
                batch_recent.append({"id": p["id"], "title": p["title"], "url": p.get("url")})
        updates.append((hot, max(hot, p["peak_hot"] or 0), json.dumps(reasons, ensure_ascii=False), status, cand_at, p["id"]))
    with tx() as c:
        c.executemany("UPDATE posts SET hot_score=?, peak_hot=?, reasons=?, status=?, candidate_at=? WHERE id=?", updates)
    return promoted
