"""Candidate handling: draft 二创 copy. No comment fetching and no auto-publishing — the user copies and posts manually."""
from __future__ import annotations

import logging
import time

import httpx

from . import generator
from .db import get_settings, row, tx

log = logging.getLogger("radar")


def send_webhook(post: dict, variants: list[dict], url: str) -> dict:
    """Send a notification to Feishu / WeCom / Discord / Bark / Slack / generic webhook."""
    url = (url or "").strip()
    if not url:
        return {"sent": False, "reason": "未配置 webhook_url"}
    first = variants[0]["content"] if variants else ""
    summary = (
        f"🔥 Reddit 爆款候选 (热度 {round(post.get('hot_score') or 0)})\n"
        f"社区: r/{post['subreddit']} · 评论 {post['num_comments']} · 赞 {post['score']}\n"
        f"标题: {post['title']}\n"
        f"原帖: {post.get('permalink') or ''}\n\n"
        f"【二创草稿】\n{first}"
    )
    u_low = url.lower()
    if "feishu.cn" in u_low or "larksuite.com" in u_low:
        payload = {"msg_type": "text", "content": {"text": summary}}
    elif "qyapi.weixin.qq.com" in u_low:
        payload = {"msgtype": "text", "text": {"content": summary}}
    elif "discord.com/api/webhooks" in u_low:
        payload = {"content": summary[:1950]}
    elif "api.day.app" in u_low:
        payload = {"title": f"🔥 r/{post['subreddit']} 爆款 ({round(post.get('hot_score') or 0)})", "body": first or post["title"]}
    else:
        payload = {"text": summary, "post_id": post["id"], "subreddit": post["subreddit"], "title": post["title"],
                   "hot_score": post.get("hot_score"), "permalink": post.get("permalink"), "draft": first}
    r = httpx.post(url, json=payload, timeout=10.0)
    r.raise_for_status()
    return {"sent": True, "status_code": r.status_code}


def draft_tweets(post_id: str, notify: bool = False) -> dict:
    s = get_settings()
    with tx() as c:
        post = row(c, "SELECT * FROM posts WHERE id=?", (post_id,))
    if not post:
        raise ValueError("帖子不存在")
    post["age_min"] = max(1.0, (time.time() - post["created_utc"]) / 60)
    if s.get("fetch_link_context", True) and post.get("post_type") == "link" and not post.get("selftext") and not post.get("link_context"):
        ctx = generator.extract_link_context(post.get("url") or "")
        if ctx:
            post["link_context"] = ctx
            with tx() as c:
                c.execute("UPDATE posts SET link_context=? WHERE id=?", (ctx, post_id))
    variants, image_prompt, source = generator.generate(post, s)
    now = int(time.time())
    with tx() as c:
        c.execute("UPDATE tweets SET status='skipped' WHERE post_id=? AND status='draft'", (post_id,))
        c.executemany(
            "INSERT INTO tweets(post_id, style, content, card_title, created_at) VALUES(?,?,?,?,?)",
            [(post_id, v["style"], v["content"], v["card_title"], now) for v in variants],
        )
        c.execute(
            "UPDATE posts SET image_prompt=?, title_zh=COALESCE(?, title_zh), selftext_zh=COALESCE(?, selftext_zh) WHERE id=?",
            (image_prompt, post.get("title_zh"), post.get("selftext_zh"), post_id),
        )
        if post["status"] in ("tracking", "candidate", "expired", "discarded"):
            c.execute("UPDATE posts SET status='drafted', candidate_at=COALESCE(candidate_at, ?) WHERE id=?", (now, post_id))
    webhook_url = str(s.get("webhook_url") or "").strip()
    if notify and webhook_url and (post.get("hot_score") or 0) >= float(s.get("webhook_min_hot") or 70):
        try:
            send_webhook(post, variants, webhook_url)
        except Exception as e:
            log.warning("webhook failed for %s: %s", post_id, e)
    return {"post_id": post_id, "drafts": len(variants), "source": source, "title_zh": post.get("title_zh"), "selftext_zh": post.get("selftext_zh")}


def translate_post(post_id: str) -> dict:
    with tx() as c:
        post = row(c, "SELECT * FROM posts WHERE id=?", (post_id,))
    if not post:
        raise ValueError("帖子不存在")
    if post.get("post_type") == "link" and not post.get("selftext") and not post.get("link_context"):
        ctx = generator.extract_link_context(post.get("url") or "")
        if ctx:
            post["link_context"] = ctx
            with tx() as c:
                c.execute("UPDATE posts SET link_context=? WHERE id=?", (ctx, post_id))
    title_zh, selftext_zh = generator.translate_post_content(post)
    with tx() as c:
        c.execute(
            "UPDATE posts SET title_zh=COALESCE(NULLIF(?, ''), title_zh), selftext_zh=COALESCE(NULLIF(?, ''), selftext_zh) WHERE id=?",
            (title_zh, selftext_zh, post_id),
        )
    return {"post_id": post_id, "title_zh": title_zh or post.get("title_zh"), "selftext_zh": selftext_zh or post.get("selftext_zh")}


def translate_missing_titles(limit: int = 40) -> int:
    with tx() as c:
        missing = [
            dict(r)
            for r in c.execute(
                """SELECT id, subreddit, title FROM posts
                   WHERE status IN ('tracking','candidate','drafted') AND (title_zh IS NULL OR title_zh = '')
                   ORDER BY hot_score DESC, created_utc DESC LIMIT ?""",
                (limit,),
            )
        ]
    if not missing:
        return 0
    mapping = generator.translate_titles_batch(missing)
    if not mapping:
        return 0
    with tx() as c:
        c.executemany("UPDATE posts SET title_zh=? WHERE id=?", [(zh, pid) for pid, zh in mapping.items()])
    return len(mapping)


def process_candidates() -> dict:
    if not get_settings()["auto_generate"]:
        return {"drafted": 0}
    with tx() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM posts WHERE status='candidate' ORDER BY hot_score DESC LIMIT 10")]
    results = [draft_tweets(i, notify=True) for i in ids]
    return {"drafted": len(results), "sources": sorted({r["source"] for r in results})}

