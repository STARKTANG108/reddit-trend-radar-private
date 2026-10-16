"""Collector and parser for Top AI Official Blogs (OpenAI, Anthropic, Google DeepMind, Google AI, Meta AI)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import config
from .db import row, rows, tx

log = logging.getLogger("radar.official")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}


def init_official_db() -> None:
    with tx() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS official_blogs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                title_zh TEXT,
                summary TEXT,
                summary_zh TEXT,
                url TEXT NOT NULL UNIQUE,
                category TEXT,
                published_at INTEGER NOT NULL,
                first_seen INTEGER NOT NULL
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_official_published ON official_blogs(published_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_official_source ON official_blogs(source)")


def _clean_text(html: str) -> str:
    t = re.sub(r"<[^>]+>", " ", html)
    return " ".join(t.split()).strip()


def fetch_openai(client: httpx.Client) -> list[dict]:
    items = []
    try:
        r = client.get("https://openai.com/news/rss.xml")
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for it in root.findall(".//item")[:15]:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = it.findtext("pubDate") or ""
                desc = _clean_text(it.findtext("description") or "")[:400]
                category = it.findtext("category") or "News"
                try:
                    ts = int(parsedate_to_datetime(pub).timestamp())
                except Exception:
                    ts = int(time.time())
                if link and title:
                    item_id = "oai_" + hashlib.md5(link.encode()).hexdigest()[:10]
                    items.append({
                        "id": item_id, "source": "OpenAI", "title": title, "url": link,
                        "summary": desc, "category": category, "published_at": ts
                    })
    except Exception as e:
        log.warning("OpenAI RSS fetch failed: %s", e)
    return items


def fetch_deepmind(client: httpx.Client) -> list[dict]:
    items = []
    try:
        r = client.get("https://deepmind.google/blog/rss.xml")
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for it in root.findall(".//item")[:15]:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = it.findtext("pubDate") or ""
                desc = _clean_text(it.findtext("description") or "")[:400]
                category = it.findtext("category") or "Research"
                try:
                    ts = int(parsedate_to_datetime(pub).timestamp())
                except Exception:
                    ts = int(time.time())
                if link and title:
                    item_id = "dm_" + hashlib.md5(link.encode()).hexdigest()[:10]
                    items.append({
                        "id": item_id, "source": "Google DeepMind", "title": title, "url": link,
                        "summary": desc, "category": category, "published_at": ts
                    })
    except Exception as e:
        log.warning("Google DeepMind RSS fetch failed: %s", e)
    return items


def fetch_google_ai(client: httpx.Client) -> list[dict]:
    items = []
    try:
        r = client.get("https://blog.google/technology/ai/rss/")
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for it in root.findall(".//item")[:15]:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = it.findtext("pubDate") or ""
                desc = _clean_text(it.findtext("description") or "")[:400]
                category = it.findtext("category") or "AI & Gemini"
                try:
                    ts = int(parsedate_to_datetime(pub).timestamp())
                except Exception:
                    ts = int(time.time())
                if link and title:
                    item_id = "gai_" + hashlib.md5(link.encode()).hexdigest()[:10]
                    items.append({
                        "id": item_id, "source": "Google AI", "title": title, "url": link,
                        "summary": desc, "category": category, "published_at": ts
                    })
    except Exception as e:
        log.warning("Google AI RSS fetch failed: %s", e)
    return items


def fetch_anthropic(client: httpx.Client) -> list[dict]:
    items = []
    try:
        r = client.get("https://www.anthropic.com/news")
        if r.status_code == 200:
            seen = set()
            for link, content in re.findall(r'<a\s+href="(/news/[^"]+)"[^>]*>(.*?)</a>', r.text, re.DOTALL):
                if link in seen:
                    continue
                seen.add(link)
                full_url = f"https://www.anthropic.com{link}"
                clean = _clean_text(content)
                # Parse "Month Day, Year Category Title"
                m = re.search(r"([A-Za-z]{3}\s+\d{1,2},\s+\d{4})\s+([A-Za-z]+)\s+(.+)", clean)
                if m:
                    date_str, category, title = m.groups()
                else:
                    m2 = re.search(r"(.+?)\s+([A-Za-z]+)\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", clean)
                    if m2:
                        title, category, date_str = m2.groups()
                    else:
                        continue
                try:
                    ts = int(datetime.strptime(date_str, "%b %d, %Y").timestamp())
                except Exception:
                    ts = int(time.time())
                item_id = "ant_" + hashlib.md5(full_url.encode()).hexdigest()[:10]
                items.append({
                    "id": item_id, "source": "Anthropic", "title": title, "url": full_url,
                    "summary": f"Anthropic 官方 {category} 发布：{title}", "category": category, "published_at": ts
                })
    except Exception as e:
        log.warning("Anthropic fetch failed: %s", e)
    return items


def fetch_meta_ai(client: httpx.Client) -> list[dict]:
    items = []
    try:
        r = client.get("https://engineering.fb.com/feed/")
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for it in root.findall(".//item")[:15]:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = it.findtext("pubDate") or ""
                desc = _clean_text(it.findtext("description") or "")[:400]
                category = it.findtext("category") or "Engineering"
                try:
                    ts = int(parsedate_to_datetime(pub).timestamp())
                except Exception:
                    ts = int(time.time())
                if link and title:
                    item_id = "meta_" + hashlib.md5(link.encode()).hexdigest()[:10]
                    items.append({
                        "id": item_id, "source": "Meta AI", "title": title, "url": link,
                        "summary": desc, "category": category, "published_at": ts
                    })
    except Exception as e:
        log.warning("Meta AI RSS fetch failed: %s", e)
    return items


def collect_all() -> int:
    """Fetch all official feeds and save new posts to official_blogs."""
    init_official_db()
    now = int(time.time())
    new_count = 0
    with httpx.Client(timeout=15, follow_redirects=True, headers=HEADERS) as client:
        all_items: list[dict] = []
        all_items.extend(fetch_openai(client))
        all_items.extend(fetch_deepmind(client))
        all_items.extend(fetch_google_ai(client))
        all_items.extend(fetch_anthropic(client))
        all_items.extend(fetch_meta_ai(client))

    with tx() as c:
        for it in all_items:
            existing = c.execute("SELECT 1 FROM official_blogs WHERE url=?", (it["url"],)).fetchone()
            if not existing:
                c.execute(
                    """INSERT OR IGNORE INTO official_blogs(id, source, title, title_zh, summary, summary_zh, url, category, published_at, first_seen)
                       VALUES(?,?,?,NULL,?,NULL,?,?,?,?)""",
                    (it["id"], it["source"], it["title"], it["summary"], it["url"], it["category"], it["published_at"], now),
                )
                new_count += 1
    log.info("Collected official blogs: %d total scraped, %d new inserted", len(all_items), new_count)
    return new_count


def translate_missing(limit: int = 25) -> int:
    """Translate missing title_zh and summary_zh using MiniMax LLM."""
    if not config.LLM_CONFIGURED:
        return 0
    with tx() as c:
        targets = rows(
            c,
            "SELECT id, source, title, summary FROM official_blogs WHERE title_zh IS NULL ORDER BY published_at DESC LIMIT ?",
            (limit,),
        )
    if not targets:
        return 0

    from .generator import _call_llm

    system_prompt = (
        "你是顶尖科技与人工智能官方博客的中英双语翻译官。请将各大 AI 实验室（OpenAI、Anthropic、Google DeepMind 等）"
        "的官方公告标题和摘要翻译为精准、地道、信达雅的中文。保留专业术语原文如 Antigravity, Argon, GPT, Claude, Gemini, Rosalind 等。\n"
        "严格返回 JSON 字典，键为文章 id，值为对象包含 title_zh 和 summary_zh，格式示例：\n"
        "{\n"
        "  \"oai_abc123\": {\"title_zh\": \"将 GPT-Rosalind 引入学术研究实验室\", \"summary_zh\": \"OpenAI 宣布向高校研究者开放 Rosalind 生物科学计算学术计划。\"}\n"
        "}"
    )

    total_translated = 0
    chunk_size = 10
    for i in range(0, len(targets), chunk_size):
        chunk = targets[i : i + chunk_size]
        batch_payload = [
            {"id": t["id"], "source": t["source"], "title": t["title"], "summary": (t["summary"] or "")[:250]}
            for t in chunk
        ]
        user_prompt = f"请翻译以下官方博客公告：\n{json.dumps(batch_payload, ensure_ascii=False)}"

        try:
            data = _call_llm([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ], temperature=0.2, timeout=60.0)
            if not isinstance(data, dict):
                continue
            with tx() as c:
                for pid, item in data.items():
                    if isinstance(item, dict) and item.get("title_zh"):
                        c.execute(
                            "UPDATE official_blogs SET title_zh=?, summary_zh=? WHERE id=?",
                            (item["title_zh"].strip(), (item.get("summary_zh") or "").strip(), pid),
                        )
                        total_translated += 1
                    elif isinstance(item, str) and item.strip():
                        c.execute(
                            "UPDATE official_blogs SET title_zh=? WHERE id=?",
                            (item.strip(), pid),
                        )
                        total_translated += 1
        except Exception as e:
            log.warning("Failed to translate official blogs chunk: %s", e)

    log.info("Translated %d official blog titles/summaries", total_translated)
    return total_translated


def list_blogs(source: str = "", limit: int = 100) -> list[dict]:
    """List official blogs sorted by published_at DESC."""
    init_official_db()
    with tx() as c:
        if source:
            items = rows(
                c,
                "SELECT * FROM official_blogs WHERE lower(source)=lower(?) ORDER BY published_at DESC LIMIT ?",
                (source, limit),
            )
        else:
            items = rows(
                c,
                "SELECT * FROM official_blogs ORDER BY published_at DESC LIMIT ?",
                (limit,),
            )
    return items
