"""Quota-aware collection: incremental /new polling + batched /api/info refreshes."""
from __future__ import annotations

import json
import time
from collections import Counter

from . import config, scoring, spam
from .budget import QuotaExceeded
from .db import get_settings, rows, tx

MULTI_CHUNK = 20  # subreddits per multireddit request
MAX_PAGES = 3


def enabled_subreddits() -> list[str]:
    with tx() as c:
        return [r["name"] for r in c.execute("SELECT name FROM subreddits WHERE enabled=1 ORDER BY name")]


def next_check_at(created_utc: int, hot: float, now: float, s: dict) -> int | None:
    age_min = (now - created_utc) / 60
    track_min = s["track_hours"] * 60
    if age_min >= track_min:
        return None
    final = created_utc + track_min * 60
    if age_min >= s["early_drop_hours"] * 60 and hot < 20:
        return int(final)  # low performer: skip straight to the final label snapshot
    step = 15 if age_min < 120 else 30 if age_min < 360 else 120
    if hot >= 50:
        step = min(step, 15)
    return int(min(now + step * 60, final))


def _snapshot(p: dict, now: float) -> tuple:
    return (p["id"], int(now), round((now - p["created_utc"]) / 60, 1), p["score"], p["num_comments"], p["upvote_ratio"])


def collect_new(client) -> dict:
    s = get_settings()
    subs = enabled_subreddits()
    now = time.time()
    new_posts, calls, piggybacked = [], 0, []
    chunk_size = 1 if getattr(client, "SINGLE_SUB", False) else MULTI_CHUNK
    for i in range(0, len(subs), chunk_size):
        chunk = subs[i : i + chunk_size]
        lower_chunk = [sub.lower() for sub in chunk]
        with tx() as c:
            has_history = c.execute(
                f"SELECT 1 FROM posts WHERE lower(subreddit) IN ({','.join('?' * len(lower_chunk))}) LIMIT 1", lower_chunk
            ).fetchone()
        after = None
        for _ in range(MAX_PAGES):
            try:
                items, after = client.new_posts(chunk, after=after)
            except QuotaExceeded:
                break
            except Exception:
                break
            calls += 1
            ids = [p["id"] for p in items]
            with tx() as c:
                known_rows = rows(c, f"SELECT id, tracking FROM posts WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else []
            known = {r["id"] for r in known_rows}
            tracking_known = {r["id"] for r in known_rows if r["tracking"]}
            fresh = [p for p in items if p["id"] not in known and not p["removed"]]
            piggybacked.extend(p for p in items if p["id"] in tracking_known and not p["removed"])
            new_posts += fresh
            # stop once we overlap with already-known posts, or on the very first run
            if not after or len(fresh) < len(items) or not has_history:
                break
    max_age = s["track_hours"] * 3600
    new_posts = [p for p in new_posts if now - p["created_utc"] < max_age]
    new_posts.sort(key=lambda p: p["created_utc"])
    counts = spam.author_counts([p["author"] for p in new_posts], int(now - 86400))
    batch_counts: Counter[str] = Counter()
    rows_, spam_n = [], 0
    for p in new_posts:
        author = p.get("author")
        valid_author = author and author != "[deleted]"
        if valid_author:
            batch_counts[author] += 1
        author_total = (counts.get(author, 0) + batch_counts[author]) if valid_author else 0
        reason = spam.check(p, s, author_total)
        spam_n += bool(reason)
        rows_.append((
            p["id"], p["subreddit"], p["title"], p["selftext"], p["author"], p["url"], p["permalink"], p["post_type"],
            p["flair"], p["over_18"], p["created_utc"], int(now), int(now),
            None if reason else next_check_at(p["created_utc"], 0, now, s), 0 if reason else 1,
            p["score"], p["num_comments"], p["upvote_ratio"], json.dumps(p["images"]), "spam" if reason else "tracking", reason,
        ))
    with tx() as c:
        c.executemany(
            """INSERT OR IGNORE INTO posts(id, subreddit, title, selftext, author, url, permalink, post_type, flair, over_18,
               created_utc, first_seen, last_checked, next_check, tracking, score, num_comments, upvote_ratio, images, status, spam_reason)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows_,
        )
        c.executemany("INSERT INTO snapshots VALUES(?,?,?,?,?,?)", [_snapshot(p, now) for p in new_posts])
        for p in piggybacked:
            c.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)", _snapshot(p, now))
            c.execute(
                "UPDATE posts SET score=?, num_comments=?, upvote_ratio=?, last_checked=?, images=? WHERE id=?",
                (p["score"], p["num_comments"], p["upvote_ratio"], int(now), json.dumps(p["images"]), p["id"]),
            )
    to_score = [r[0] for r in rows_ if r[-1] is None] + [p["id"] for p in piggybacked]
    promoted = scoring.score_posts(list(dict.fromkeys(to_score)))
    return {"calls": calls, "new_posts": len(new_posts), "spam": spam_n, "promoted": len(promoted)}


def refresh_due(client) -> dict:
    s = get_settings()
    now = time.time()
    with tx() as c:
        due = rows(
            c,
            """SELECT id, status, hot_score FROM posts WHERE tracking=1 AND next_check <= ?
               ORDER BY CASE WHEN status IN ('candidate','drafted','used') THEN 0 ELSE 1 END, hot_score DESC, next_check""",
            (int(now),),
        )
    calls, updated, skipped, spammed = 0, [], 0, 0
    for i in range(0, len(due), 100):
        batch = due[i : i + 100]
        hot_batch = any(p["status"] != "tracking" or p["hot_score"] >= 40 for p in batch)
        priority = "high" if hot_batch else "normal" if i == 0 else "low"
        try:
            fresh = client.info([p["id"] for p in batch], priority=priority)
        except QuotaExceeded:
            skipped += len(due) - i
            break
        calls += 1
        now = time.time()
        got = {p["id"]: p for p in fresh}
        with tx() as c:
            for p in batch:
                f = got.get(p["id"])
                if not f or f["removed"]:
                    c.execute(
                        "UPDATE posts SET tracking=0, next_check=NULL, status=CASE WHEN status='used' THEN status ELSE 'removed' END WHERE id=?",
                        (p["id"],),
                    )
                    continue
                c.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)", _snapshot(f, now))
                c.execute(
                    "UPDATE posts SET score=?, num_comments=?, upvote_ratio=?, last_checked=?, images=? WHERE id=?",
                    (f["score"], f["num_comments"], f["upvote_ratio"], int(now), json.dumps(f["images"]), p["id"]),
                )
                reason = spam.check_engagement(f, s, now) if p["status"] == "tracking" else None
                if reason:
                    c.execute("UPDATE posts SET status='spam', spam_reason=?, tracking=0, next_check=NULL WHERE id=?", (reason, p["id"]))
                    spammed += 1
                    continue
                updated.append(p["id"])
    promoted = scoring.score_posts(updated)
    with tx() as c:
        for p in rows(c, f"SELECT id, created_utc, hot_score, status FROM posts WHERE id IN ({','.join('?' * len(updated))})", updated) if updated else []:
            nxt = next_check_at(p["created_utc"], p["hot_score"], now, s)
            status = "expired" if nxt is None and p["status"] == "tracking" else p["status"]
            c.execute(
                "UPDATE posts SET next_check=?, tracking=?, status=? WHERE id=?",
                (nxt, 0 if nxt is None else 1, status, p["id"]),
            )
    return {"calls": calls, "refreshed": len(updated), "spam": spammed, "promoted": len(promoted), "deferred": skipped}


def prune_old_data() -> dict:
    """Clean up old snapshots, api_usage logs, and cached proxy images beyond retention window."""
    s = get_settings()
    keep_days = max(7, int(s.get("retention_days") or 14))
    now = time.time()
    snap_cutoff = int(now - keep_days * 86400)
    usage_cutoff = int(now - 90 * 86400)
    with tx() as c:
        del_snaps = c.execute(
            """DELETE FROM snapshots WHERE ts < ? AND post_id IN (
               SELECT id FROM posts WHERE tracking=0 AND status NOT IN ('candidate','drafted','used'))""",
            (snap_cutoff,),
        ).rowcount
        del_usage = c.execute("DELETE FROM api_usage WHERE ts < ?", (usage_cutoff,)).rowcount
    del_cache = 0
    cache_cutoff = now - keep_days * 86400
    if config.CACHE_DIR.exists():
        for f in config.CACHE_DIR.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cache_cutoff:
                    f.unlink(missing_ok=True)
                    del_cache += 1
            except OSError:
                pass
    return {"deleted_snapshots": del_snaps, "deleted_usage": del_usage, "deleted_cache_files": del_cache}
