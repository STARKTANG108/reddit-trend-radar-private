from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import budget, config, images, official, pipeline, scheduler, scoring
from .collector import next_check_at
from .db import get_settings, init_db, row, rows, tx, update_settings
from .generator import weighted_len

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = config.BASE_DIR / "app" / "static"
SUB_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")


def _bootstrap() -> None:
    init_db()
    with tx() as c:
        if not c.execute("SELECT 1 FROM subreddits LIMIT 1").fetchone():
            c.executemany(
                "INSERT OR IGNORE INTO subreddits(name, added_at) VALUES(?, ?)",
                [(s, int(time.time())) for s in config.DEFAULT_SUBREDDITS if SUB_RE.match(s)],
            )
        empty = not c.execute("SELECT 1 FROM posts LIMIT 1").fetchone()
    if config.DEMO_MODE and empty:
        from .demo import seed_history

        with tx() as c:
            subs = [r["name"] for r in c.execute("SELECT name FROM subreddits WHERE enabled=1")]
        seed_history(scheduler.client, subs)
    scoring.rebuild_baselines()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _bootstrap()
    scheduler.start()
    threading.Thread(target=lambda: [scheduler.run_job("collect"), scheduler.run_job("refresh")], daemon=True).start()
    yield
    scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="Reddit Trend Radar", lifespan=lifespan)


def _post_view(p: dict, now: float) -> dict:
    age = max(1.0, (now - p["created_utc"]) / 60)
    p["age_min"] = round(age, 1)
    p["comment_velocity"] = round(p["num_comments"] / age, 2)
    p["reasons"] = json.loads(p.get("reasons") or "[]")
    p["images"] = json.loads(p.get("images") or "[]")
    return p


def _day_start() -> int:
    return int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


@app.get("/api/status")
def status():
    s = get_settings()
    acct = scheduler.client.account_usage() if hasattr(scheduler.client, "account_usage") else None
    return {
        "demo_mode": config.DEMO_MODE,
        "reddit_configured": config.REDDIT_CONFIGURED,
        "reddit_provider": config.REDDIT_PROVIDER,
        "redditdate_usage": acct,
        "llm_configured": config.LLM_CONFIGURED,
        "llm_model": config.LLM_MODEL if config.LLM_CONFIGURED else None,
        "image_configured": config.IMAGE_CONFIGURED,
        "image_model": config.IMAGE_MODEL or None,
        "max_tweet_weight": int(s.get("max_tweet_weight") or 280),
        "desktop_notify": bool(s.get("desktop_notify")),
        "jobs": scheduler.jobs_status(),
        "server_time": int(time.time()),
    }


@app.get("/api/overview")
def overview():
    now = time.time()
    today = _day_start()
    with tx() as c:
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM posts GROUP BY status")}
        tracking = row(c, "SELECT COUNT(*) n FROM posts WHERE tracking=1")["n"]
        drafts = row(c, "SELECT COUNT(DISTINCT post_id) n FROM tweets WHERE status='draft'")["n"]
        new_today = row(c, "SELECT COUNT(*) n FROM posts WHERE first_seen >= ?", (today,))["n"]
        spam_today = row(c, "SELECT COUNT(*) n FROM posts WHERE status='spam' AND first_seen >= ?", (today,))["n"]
        used_today = row(c, "SELECT COUNT(*) n FROM tweets WHERE status='used' AND used_at >= ?", (today,))["n"]
        top = rows(c, """SELECT * FROM posts WHERE status IN ('tracking','candidate','drafted')
                         ORDER BY hot_score DESC LIMIT 10""")
        latest = rows(c, """SELECT * FROM posts WHERE status IN ('tracking','candidate','drafted')
                            ORDER BY created_utc DESC LIMIT 10""")
    return {
        "budget": budget.status(),
        "counts": counts,
        "tracking": tracking,
        "pending_review": drafts,
        "used_today": used_today,
        "new_today": new_today,
        "spam_today": spam_today,
        "top": [_post_view(p, now) for p in top],
        "latest": [_post_view(p, now) for p in latest],
    }


@app.get("/api/usage")
def usage(days: int = 30):
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    with tx() as c:
        data = rows(
            c,
            """SELECT date(ts, 'unixepoch', 'localtime') d, endpoint, COUNT(*) n FROM api_usage
               WHERE ts >= ? GROUP BY d, endpoint ORDER BY d""",
            (int(start.timestamp()),),
        )
    by_day: dict[str, dict] = {}
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        by_day[d] = {"date": d, "total": 0}
    for r in data:
        if r["d"] in by_day:
            by_day[r["d"]][r["endpoint"]] = r["n"]
            by_day[r["d"]]["total"] += r["n"]
    return {"days": list(by_day.values()), "budget": budget.status()}


@app.get("/api/posts")
def list_posts(status: str = "", subreddit: str = "", sort: str = "hot", q: str = "", limit: int = 100, max_age_hours: float = 6):
    now = time.time()
    where, params = ["created_utc >= ?"], [int(now - max_age_hours * 3600)]
    if status:
        statuses = status.split(",")
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params += statuses
    if subreddit:
        where.append("lower(subreddit) = ?")
        params.append(subreddit.lower())
    if q:
        where.append("(title LIKE ? OR COALESCE(title_zh, '') LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    order = {"hot": "hot_score DESC", "new": "created_utc DESC", "comments": "num_comments DESC"}.get(sort, "hot_score DESC")
    with tx() as c:
        items = rows(c, f"SELECT * FROM posts WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?", params + [min(limit, 500)])
    return [_post_view(p, now) for p in items]


@app.get("/api/posts/{post_id}")
def get_post(post_id: str):
    now = time.time()
    with tx() as c:
        p = row(c, "SELECT * FROM posts WHERE id=?", (post_id,))
        if not p:
            raise HTTPException(404, "帖子不存在")
    if config.LLM_CONFIGURED and (not p.get("title_zh") or (p.get("selftext") and not p.get("selftext_zh"))):
        try:
            tr = pipeline.translate_post(post_id)
            p["title_zh"] = tr.get("title_zh") or p.get("title_zh")
            p["selftext_zh"] = tr.get("selftext_zh") or p.get("selftext_zh")
        except Exception:
            pass
    with tx() as c:
        snaps = rows(c, "SELECT ts, age_min, score, num_comments, upvote_ratio FROM snapshots WHERE post_id=? ORDER BY ts", (post_id,))
        tweets = rows(c, "SELECT * FROM tweets WHERE post_id=? AND status IN ('draft','used') ORDER BY id", (post_id,))
    age = (now - p["created_utc"]) / 60
    curve_ages = sorted({a for a in [5, 10, 15, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720, 1080, 1440] if a <= max(age, 15) * 1.2})
    baseline = [{"age_min": a, **{k: round(v, 1) for k, v in scoring.baseline_at(p["subreddit"], a).items()}} for a in curve_ages]
    for t in tweets:
        t["weight"] = weighted_len(t["content"])
    return {"post": _post_view(p, now), "snapshots": snaps, "baseline": baseline, "tweets": tweets,
            "assets": images.list_assets(post_id)}


def _set_post_status(post_id: str, status: str, **extra) -> dict:
    with tx() as c:
        if not row(c, "SELECT 1 x FROM posts WHERE id=?", (post_id,)):
            raise HTTPException(404, "帖子不存在")
        sets = ", ".join(["status=?"] + [f"{k}=?" for k in extra])
        c.execute(f"UPDATE posts SET {sets} WHERE id=?", (status, *extra.values(), post_id))
        if status in ("discarded", "spam"):
            c.execute("UPDATE tweets SET status='discarded' WHERE post_id=? AND status='draft'", (post_id,))
    return {"ok": True}


@app.post("/api/posts/{post_id}/generate")
def generate(post_id: str):
    try:
        return pipeline.draft_tweets(post_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/posts/{post_id}/translate")
def translate_post_endpoint(post_id: str):
    try:
        return pipeline.translate_post(post_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/posts/{post_id}/discard")
def discard(post_id: str):
    return _set_post_status(post_id, "discarded")


@app.post("/api/posts/{post_id}/spam")
def mark_spam(post_id: str):
    return _set_post_status(post_id, "spam", spam_reason="手动标记为垃圾", tracking=0, next_check=None)


@app.post("/api/posts/{post_id}/restore")
def restore(post_id: str):
    """Undo discard / spam. Restored posts are immediately rescored and resume tracking if inside the window."""
    s = get_settings()
    now = time.time()
    with tx() as c:
        if not row(c, "SELECT 1 x FROM posts WHERE id=?", (post_id,)):
            raise HTTPException(404, "帖子不存在")
        c.execute("UPDATE posts SET status='tracking', spam_reason=NULL, tracking=1 WHERE id=?", (post_id,))
    promoted = scoring.score_posts([post_id])
    with tx() as c:
        p = row(c, "SELECT created_utc, hot_score, status FROM posts WHERE id=?", (post_id,))
        nxt = next_check_at(p["created_utc"], p["hot_score"], now, s)
        final_status = p["status"] if p["status"] != "tracking" else ("tracking" if nxt else "expired")
        c.execute(
            "UPDATE posts SET status=?, tracking=?, next_check=? WHERE id=?",
            (final_status, int(bool(nxt)), int(now) if nxt else None, post_id),
        )
    if promoted:
        pipeline.process_candidates()
    return {"ok": True, "promoted": bool(promoted)}


class WebhookTestIn(BaseModel):
    url: str = ""


@app.post("/api/webhook/test")
def test_webhook(body: WebhookTestIn):
    url = body.url.strip() or str(get_settings().get("webhook_url") or "").strip()
    if not url:
        raise HTTPException(400, "请先填写 Webhook 地址")
    sample_post = {
        "id": "test01",
        "subreddit": "LocalLLaMA",
        "title": "Reddit 热帖雷达 Webhook 连通性测试",
        "num_comments": 128,
        "score": 512,
        "hot_score": 88.5,
        "permalink": "https://www.reddit.com/r/LocalLLaMA/",
    }
    sample_variants = [{"content": "这是一条来自 Reddit 热帖雷达的测试推送。当发现高热度候选帖并生成二创后，你将收到此类通知。"}]
    try:
        return pipeline.send_webhook(sample_post, sample_variants, url)
    except Exception as e:
        raise HTTPException(502, f"Webhook 推送失败: {e}")


class AiImageIn(BaseModel):
    prompt: str = ""


@app.post("/api/posts/{post_id}/ai-image")
def ai_image(post_id: str, body: AiImageIn):
    with tx() as c:
        p = row(c, "SELECT title, image_prompt FROM posts WHERE id=?", (post_id,))
    if not p:
        raise HTTPException(404, "帖子不存在")
    prompt = body.prompt.strip() or p["image_prompt"] or f"Editorial illustration about: {p['title']}. No text."
    try:
        return images.generate_ai_image(post_id, prompt)
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/image-proxy")
def image_proxy(url: str):
    try:
        data, ctype = images.fetch_proxied(url)
    except Exception as e:
        raise HTTPException(400, str(e))
    return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/tweets")
def list_tweets(status: str = "draft", limit: int = 200):
    statuses = status.split(",")
    now = time.time()
    with tx() as c:
        items = rows(
            c,
            f"""SELECT t.*, p.title, p.title_zh, p.selftext_zh, p.subreddit, p.hot_score, p.score, p.num_comments, p.permalink, p.created_utc, p.images
                FROM tweets t JOIN posts p ON p.id = t.post_id WHERE t.status IN ({','.join('?' * len(statuses))})
                ORDER BY COALESCE(t.used_at, 0) DESC, p.created_utc DESC, t.id LIMIT ?""",
            statuses + [limit],
        )
    for t in items:
        t["weight"] = weighted_len(t["content"])
        t["images"] = json.loads(t["images"] or "[]")
        t["age_min"] = round((now - t["created_utc"]) / 60, 1)
    return items


class TweetUpdate(BaseModel):
    content: str
    card_title: str | None = None


@app.put("/api/tweets/{tweet_id}")
def update_tweet(tweet_id: int, body: TweetUpdate):
    with tx() as c:
        c.execute("UPDATE tweets SET content=?, card_title=COALESCE(?, card_title) WHERE id=?", (body.content, body.card_title, tweet_id))
    return {"ok": True, "weight": weighted_len(body.content)}


@app.post("/api/tweets/{tweet_id}/use")
def use_tweet(tweet_id: int):
    """Mark a draft as used (the user copied it and posted manually)."""
    now = int(time.time())
    with tx() as c:
        t = row(c, "SELECT post_id FROM tweets WHERE id=?", (tweet_id,))
        if not t:
            raise HTTPException(404, "文案不存在")
        c.execute("UPDATE tweets SET status='used', used_at=? WHERE id=?", (now, tweet_id))
        c.execute("UPDATE tweets SET status='skipped' WHERE post_id=? AND id != ? AND status='draft'", (t["post_id"], tweet_id))
        c.execute("UPDATE posts SET status='used' WHERE id=?", (t["post_id"],))
    return {"ok": True}


@app.post("/api/tweets/{tweet_id}/unuse")
def unuse_tweet(tweet_id: int):
    with tx() as c:
        t = row(c, "SELECT post_id FROM tweets WHERE id=?", (tweet_id,))
        if not t:
            raise HTTPException(404, "文案不存在")
        c.execute("UPDATE tweets SET status='draft', used_at=NULL WHERE id=?", (tweet_id,))
        c.execute("UPDATE tweets SET status='draft' WHERE post_id=? AND status='skipped'", (t["post_id"],))
        c.execute("UPDATE posts SET status='drafted' WHERE id=?", (t["post_id"],))
    return {"ok": True}


@app.post("/api/tweets/{tweet_id}/discard")
def discard_tweet(tweet_id: int):
    with tx() as c:
        c.execute("UPDATE tweets SET status='discarded' WHERE id=? AND status='draft'", (tweet_id,))
    return {"ok": True}


@app.get("/api/official")
def list_official_blogs(source: str = "", limit: int = 100):
    return official.list_blogs(source=source, limit=limit)


@app.get("/api/subreddits")
def list_subreddits():
    since = int(time.time()) - 7 * 86400
    with tx() as c:
        subs = rows(
            c,
            """SELECT s.*, COUNT(p.id) posts_7d, SUM(p.status IN ('candidate','drafted','used')) hits_7d,
                      SUM(p.status='used') used_7d, SUM(p.status='spam') spam_7d, MAX(p.num_comments) max_comments_7d
               FROM subreddits s LEFT JOIN posts p ON lower(p.subreddit)=lower(s.name) AND p.created_utc >= ?
               GROUP BY s.name ORDER BY s.name""",
            (since,),
        )
        base = {(r["subreddit"].lower(), r["bucket"]): r for r in rows(c, "SELECT * FROM baselines WHERE bucket IN (60, 180, 240)")}
    for s in subs:
        b60, b240 = base.get((s["name"].lower(), 60)), base.get((s["name"].lower(), 240))
        s["baseline_60_c90"] = round(b60["c90"], 1) if b60 else None
        s["baseline_240_c90"] = round(b240["c90"], 1) if b240 else None
        s["baseline_samples"] = b60["n"] if b60 else 0
    return subs


class SubredditIn(BaseModel):
    name: str


@app.post("/api/subreddits")
def add_subreddit(body: SubredditIn):
    name = body.name.strip().removeprefix("/").removeprefix("r/")
    if not SUB_RE.match(name):
        raise HTTPException(400, "社区名不合法")
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO subreddits(name, added_at) VALUES(?, ?)", (name, int(time.time())))
    return {"ok": True}


class SubredditPatch(BaseModel):
    enabled: bool


@app.patch("/api/subreddits/{name}")
def toggle_subreddit(name: str, body: SubredditPatch):
    with tx() as c:
        c.execute("UPDATE subreddits SET enabled=? WHERE name=?", (int(body.enabled), name))
    return {"ok": True}


@app.delete("/api/subreddits/{name}")
def delete_subreddit(name: str):
    with tx() as c:
        c.execute("DELETE FROM subreddits WHERE name=?", (name,))
    return {"ok": True}


@app.get("/api/settings")
def read_settings():
    return get_settings()


@app.put("/api/settings")
def write_settings(values: dict):
    try:
        s = update_settings(values)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"设置值不合法: {e}")
    scheduler.reschedule()
    return s


@app.post("/api/jobs/{name}/run")
def run_job(name: str):
    if name not in scheduler.JOBS:
        raise HTTPException(404, "任务不存在")
    return scheduler.run_job(name)


@app.get("/api/estimate")
def estimate():
    """Rough daily call estimate for the current configuration."""
    s = get_settings()
    with tx() as c:
        subs = row(c, "SELECT COUNT(*) n FROM subreddits WHERE enabled=1")["n"]
        per_day = row(c, "SELECT COUNT(*) n FROM posts WHERE created_utc >= ? AND status != 'spam'", (int(time.time()) - 86400,))["n"]
    chunk = 1 if getattr(scheduler.client, "SINGLE_SUB", False) else 20
    polls = 1440 / max(1, s["poll_new_minutes"]) * max(1, -(-subs // chunk))
    checks, t = 0, 0.0
    while (nxt := next_check_at(0, 0, t, s)) is not None and checks < 500:
        checks, t = checks + 1, float(nxt)
    batch = 15 if getattr(scheduler.client, "SINGLE_SUB", False) else 100
    refresh = 1440 / max(1, s["refresh_check_minutes"]) + per_day * checks / batch
    return {"subreddits": subs, "new_posts_per_day": per_day, "checks_per_post": checks, "collect_calls": round(polls),
            "refresh_calls": round(refresh), "total": round(polls + refresh)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/media", StaticFiles(directory=config.MEDIA_DIR), name="media")
