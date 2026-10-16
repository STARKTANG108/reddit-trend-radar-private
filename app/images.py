"""Image helpers: same-origin proxy for Reddit-hosted images (so the browser can draw them on canvas) and optional AI images."""
from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import urlparse

import httpx

from . import config
from .db import row, rows, tx

ALLOWED_HOSTS = {
    "i.redd.it", "preview.redd.it", "external-preview.redd.it", "a.thumbs.redditmedia.com", "b.thumbs.redditmedia.com",
    "i.imgur.com", "picsum.photos", "fastly.picsum.photos",
}
MAX_BYTES = 15 * 1024 * 1024


def _check_host(request: httpx.Request) -> None:
    if request.url.host not in ALLOWED_HOSTS:
        raise ValueError(f"不允许的图片域名: {request.url.host}")


def fetch_proxied(url: str) -> tuple[bytes, str]:
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in ALLOWED_HOSTS:
        raise ValueError("不允许的图片地址")
    key = hashlib.sha1(url.encode()).hexdigest()
    path, ctype_path = config.CACHE_DIR / key, config.CACHE_DIR / f"{key}.type"
    if path.exists() and ctype_path.exists():
        return path.read_bytes(), ctype_path.read_text()
    with httpx.Client(timeout=20, follow_redirects=True, event_hooks={"request": [_check_host]},
                      headers={"User-Agent": config.REDDIT_USER_AGENT}) as client:
        r = client.get(url)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/") or len(r.content) > MAX_BYTES:
        raise ValueError("不是图片或图片过大")
    path.write_bytes(r.content)
    ctype_path.write_text(ctype)
    return r.content, ctype


def generate_ai_image(post_id: str, prompt: str) -> dict:
    if not config.IMAGE_CONFIGURED:
        raise RuntimeError("未配置 IMAGE_MODEL / IMAGE_API_KEY")
    r = httpx.post(
        f"{config.IMAGE_BASE_URL}/images/generations",
        headers={"Authorization": f"Bearer {config.IMAGE_API_KEY}"},
        json={"model": config.IMAGE_MODEL, "prompt": prompt, "size": config.IMAGE_SIZE, "n": 1},
        timeout=180,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"生图失败 {r.status_code}: {r.text[:300]}")
    item = r.json()["data"][0]
    data = base64.b64decode(item["b64_json"]) if item.get("b64_json") else httpx.get(item["url"], timeout=60).content
    name = f"{post_id}_{int(time.time())}.png"
    (config.MEDIA_DIR / name).write_bytes(data)
    with tx() as c:
        cur = c.execute("INSERT INTO assets(post_id, kind, path, prompt, created_at) VALUES(?,?,?,?,?)",
                        (post_id, "ai", name, prompt, int(time.time())))
        return row(c, "SELECT * FROM assets WHERE id=?", (cur.lastrowid,))


def list_assets(post_id: str) -> list[dict]:
    with tx() as c:
        return rows(c, "SELECT * FROM assets WHERE post_id=? ORDER BY id DESC", (post_id,))
