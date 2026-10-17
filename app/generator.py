from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger("radar.generator")

MAX_WEIGHT = 280
URL_WEIGHT = 23
URL_RE = re.compile(r"https?://\S+")
_LIGHT = [(0, 4351), (8192, 8205), (8208, 8223), (8242, 8247)]
_SKIP_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "i.redd.it", "v.redd.it", "preview.redd.it", "i.imgur.com", "imgur.com", "example.com", "localhost"}
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".pdf", ".zip")
_META_RE = re.compile(
    r'<meta[^>]+(?:property=["\'](?:og:description|twitter:description)["\']|name=["\']description["\'])[^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property=["\'](?:og:description|twitter:description)["\']|name=["\']description["\'])',
    re.I,
)
_P_RE = re.compile(r"<p(?:\s[^>]*)?>(.*?)</p>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def weighted_len(text: str) -> int:
    """Approximates X's weighted length: URLs = 23, CJK/emoji = 2, latin = 1."""
    n = len(URL_RE.findall(text)) * URL_WEIGHT
    for ch in URL_RE.sub("", text):
        n += 1 if any(lo <= ord(ch) <= hi for lo, hi in _LIGHT) else 2
    return n


def fit(body: str, link: str = "", max_weight: int = MAX_WEIGHT) -> str:
    body = body.strip()
    suffix = f"\n\n{link}" if link else ""
    limit = max(140, int(max_weight or MAX_WEIGHT))
    while body and weighted_len(body + suffix) > limit:
        body = body[:-2].rstrip() + "…"
    return body + suffix


def _safe_external_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host or host in _SKIP_HOSTS or host.endswith(".reddit.com") or host.endswith(".redd.it"):
        return False
    if p.path.lower().endswith(_SKIP_EXT):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass
    return True


def extract_link_context(url: str) -> str:
    """Fetch external article meta description and opening paragraphs (0 Reddit API calls)."""
    if not url or not _safe_external_url(url):
        return ""
    try:
        with httpx.Client(timeout=6.0, follow_redirects=True, headers={"User-Agent": config.REDDIT_USER_AGENT}) as client:
            r = client.get(url)
        if r.status_code >= 400 or "html" not in (r.headers.get("content-type") or "").lower():
            return ""
        raw = r.text[:250_000]
        parts: list[str] = []
        for m in _META_RE.finditer(raw):
            desc = html.unescape(m.group(1) or m.group(2) or "").strip()
            if len(desc) >= 20 and desc not in parts:
                parts.append(desc)
        for m in _P_RE.finditer(raw):
            txt = re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", m.group(1)))).strip()
            if len(txt) >= 45 and txt not in parts:
                parts.append(txt)
            if sum(len(x) for x in parts) >= 1400:
                break
        return "\n".join(parts)[:1500]
    except Exception:
        return ""


_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.I)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)

AI_GLOSSARY = (
    "专有名词规范：Antigravity（Google 推出的 AI 编程 IDE，严禁译为“反重力”）、"
    "Codex、Claude、Claude Code、Anthropic、Gemini、OpenAI、ChatGPT、Argon、Luna、Opus、Sonnet、Haiku 等产品与模型代号保持英文原文。"
)


def _parse_llm_json(raw_text: str) -> dict:
    cleaned = _THINK_RE.sub("", raw_text or "").strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    m = re.search(r"\{[\s\S]*\}", cleaned)
    target = m.group(0) if m else cleaned
    try:
        return json.loads(target)
    except Exception:
        try:
            return json.loads(target, strict=False)
        except Exception:
            pass
    fixed = re.sub(r",\s*\}", "}", target)
    try:
        return json.loads(fixed, strict=False)
    except Exception:
        pass
    pairs = {}
    for k, v in re.findall(r'"([a-zA-Z0-9_]+)"\s*:\s*"(.*?)"(?=\s*,\s*"[a-zA-Z0-9_]+"|\s*\})', target, re.DOTALL):
        pairs[k] = v.strip()
    if pairs:
        return pairs
    return json.loads(target)


def _call_llm(messages: list[dict], temperature: float = 0.7, timeout: float = 90.0) -> dict:
    is_minimax = "minimaxi.com" in config.LLM_BASE_URL or "minimax.chat" in config.LLM_BASE_URL
    candidates = [config.LLM_MODEL]
    if is_minimax:
        for fb in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.1"):
            if fb not in candidates:
                candidates.append(fb)
    last_resp = None
    for model_name in candidates:
        payload: dict = {"model": model_name, "messages": messages, "temperature": temperature}
        if not is_minimax:
            payload["response_format"] = {"type": "json_object"}
        r = httpx.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
            json=payload,
            timeout=timeout,
        )
        last_resp = r
        if r.status_code == 400 and "unknown model" in r.text.lower() and len(candidates) > 1:
            continue
        r.raise_for_status()
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return _parse_llm_json(content)
    if last_resp is not None:
        last_resp.raise_for_status()
    raise RuntimeError("LLM 调用失败")


def _prompt(post: dict, styles: list[str], lang: str, with_link: bool, max_weight: int, persona: str) -> list[dict]:
    language = "简体中文" if lang == "zh" else "English"
    budget = (max_weight - (URL_WEIGHT + 2 if with_link else 0)) // (2 if lang == "zh" else 1)
    link_ctx = (post.get("link_context") or "").strip()
    reasons_raw = post.get("reasons")
    if isinstance(reasons_raw, str):
        try:
            reasons_list = json.loads(reasons_raw)
        except Exception:
            reasons_list = []
    else:
        reasons_list = reasons_raw or []
    vibe = "；".join(reasons_list[:3]) if reasons_list else ""
    user = f"""社区：r/{post['subreddit']}
标题：{post['title']}
正文：{(post.get('selftext') or '')[:2500] or '(无)'}
外链：{post.get('url') or '(无)'}
{f"外链网页摘要：{link_ctx}" if link_ctx else ""}
帖子类型：{post.get('post_type')}
热度：发帖 {post['age_min']:.0f} 分钟已有 {post['num_comments']} 条评论、{post['score']} 赞（好评率 {int((post.get('upvote_ratio') or 0) * 100)}%）
{f"讨论态势：{vibe}" if vibe else ""}

请同时完成两项任务：
任务一（原文翻译）：将原帖标题翻译为流畅准确的中文（`title_zh`），并将原帖正文（若无正文但有外链摘要则翻译外链摘要）完整翻译为中文（`selftext_zh`）。
任务二（爆款二创）：写 {len(styles)} 条二创推文，角度依次为：{'、'.join(styles)}。"""
    persona_line = f"\n8. 账号人设与额外风格要求：{persona.strip()}" if persona and persona.strip() else ""
    system = f"""你同时担任「AI 科技社区资深中文翻译」与「擅长把 Reddit 热帖做成 X(Twitter) 爆款内容的中文社媒主笔」。
{AI_GLOSSARY}
要求：
1. `title_zh`：忠实、自然地把原帖标题翻译成简体中文；`selftext_zh`：把原帖正文（或外链摘要）完整、准确地翻译成简体中文（分段清晰，若原帖既无正文也无外链摘要则填 ""）。
2. `variants` 二创推文：用{language}、用自己的话重新组织，每条正文不超过 {budget} 个字。
3. 二创结构：第一句是钩子（反常识/冲突/数字/悬念），中间给出核心信息和你的解读或背景，结尾抛一个能引发讨论的问题或观点。
4. 每条角度、开头句式都要不同；口语化、有态度，但不能编造原帖没有的事实、数据或引语，不确定的写“据网友说/帖子称”。
5. 不要 emoji，不要超过 2 个 hashtag，不要写链接。
6. 每条再给一个配图大标题 `card_title`：{'不超过 18 个汉字' if lang == 'zh' else 'at most 8 words'}，适合印在图片上，冲击力强。
7. 再给一条 `image_prompt`：用英文描述一张适合作为配图的插画/照片（不含文字、不含真实人物肖像、不含品牌 logo）。{persona_line}
只输出严格合法的 JSON（不要输出 markdown 代码块之外的任何解释）：
{{"title_zh":"原帖标题中文翻译","selftext_zh":"原帖正文中文翻译","image_prompt":"...","variants":[{{"style":"角度","text":"正文","card_title":"配图标题"}}]}}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _template(post: dict, styles: list[str], lang: str) -> tuple[list[dict], str]:
    t = post.get("title_zh") or post["title"]
    n, sub = post["num_comments"], post["subreddit"]
    ctx = (post.get("selftext_zh") or post.get("link_context") or post.get("selftext") or "").strip()
    snippet = f"\n\n核心背景：{ctx[:90]}…" if ctx and lang == "zh" else f"\n\nContext: {ctx[:120]}…" if ctx else ""
    if lang == "zh":
        bodies = [
            f"刚发出 {post['age_min']:.0f} 分钟就炸出 {n} 条评论：\n\n{t}{snippet}\n\nr/{sub} 的网友已经吵翻了，你站哪边？",
            f"Reddit 今天最值得看的讨论之一：{t}{snippet}\n\n为什么这么多人在意？背后其实是一个更大的问题。",
            f"r/{sub} 今日热议：{t}\n\n评论区 {n} 条还在狂飙，你怎么看？",
        ]
    else:
        bodies = [
            f"{n} comments in {post['age_min']:.0f} minutes:\n\n{t}{snippet}\n\nr/{sub} is split. Where do you stand?",
            f"One of today's most important Reddit threads: {t}{snippet}\n\nWhy does everyone care? It's bigger than it looks.",
            f"r/{sub} today: {t}\n\n{n} comments and counting.",
        ]
    return [{"style": st, "text": bodies[i % len(bodies)], "card_title": t[:60]} for i, st in enumerate(styles)], \
        f"Editorial illustration about: {post['title']}. Clean, modern, high contrast, no text."


def translate_post_content(post: dict) -> tuple[str, str]:
    """Translate a single post's title and selftext/link_context into Chinese via LLM."""
    if not config.LLM_CONFIGURED:
        return "", ""
    body_src = (post.get("selftext") or post.get("link_context") or "").strip()[:3000]
    messages = [
        {
            "role": "system",
            "content": (
                f"你是专业的 AI 与科技社区中文翻译官。{AI_GLOSSARY}\n"
                "请将给定的 Reddit 帖子标题和正文忠实、流畅地翻译为简体中文（保留段落结构与技术术语）。"
                '只输出严格 JSON：{"title_zh":"...","selftext_zh":"..."}'
            ),
        },
        {
            "role": "user",
            "content": f"社区：r/{post.get('subreddit', '')}\n标题：{post.get('title', '')}\n正文：{body_src or '(无正文)'}",
        },
    ]
    data = _call_llm(messages, temperature=0.3, timeout=60.0)
    title_zh = (data.get("title_zh") or "").strip()
    selftext_zh = (data.get("selftext_zh") or "").strip() if body_src else ""
    return title_zh, selftext_zh


def translate_titles_batch(items: list[dict]) -> dict[str, str]:
    """Batch-translate post titles into Chinese in chunks of up to 15 posts per LLM call."""
    if not config.LLM_CONFIGURED or not items:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(items), 15):
        chunk = items[i : i + 15]
        lines = "\n".join(f'{p["id"]}: [r/{p.get("subreddit", "")}] {p["title"]}' for p in chunk)
        messages = [
            {
                "role": "system",
                "content": (
                    f"你是专业的 AI 科技社区中文翻译官。{AI_GLOSSARY}\n"
                    "请把下面每条 Reddit 帖子标题准确、自然地翻译成简体中文。"
                    '只输出严格 JSON 对象，键为帖子 id，值为中文标题，例如：{"1x749j8": "中文标题"}'
                ),
            },
            {"role": "user", "content": lines},
        ]
        try:
            data = _call_llm(messages, temperature=0.2, timeout=60.0)
            if isinstance(data, dict):
                for k, v in data.items():
                    val = ""
                    if isinstance(v, str):
                        val = v.strip()
                    elif isinstance(v, dict):
                        val = (v.get("title_zh") or v.get("zh") or v.get("title") or "").strip()
                    if val:
                        out[str(k)] = val
        except Exception as e:
            log.warning("translate_titles_batch chunk %d-%d failed: %s", i, i + len(chunk), e)
            continue
    return out


def generate(post: dict, settings: dict) -> tuple[list[dict], str, str]:
    styles = [x.strip() for x in settings["tweet_styles"].split(",") if x.strip()][: max(1, int(settings["variants"]))] or ["爆点钩子"]
    lang = settings["tweet_language"]
    link = post["permalink"] if settings["append_link"] else ""
    max_weight = max(140, int(settings.get("max_tweet_weight") or MAX_WEIGHT))
    persona = str(settings.get("custom_persona") or "")
    source, variants, image_prompt = "template", [], ""
    if config.LLM_CONFIGURED:
        try:
            data = _call_llm(_prompt(post, styles, lang, bool(link), max_weight, persona), temperature=0.8, timeout=90.0)
            variants = [v for v in data.get("variants", []) if v.get("text")]
            image_prompt = data.get("image_prompt") or ""
            if data.get("title_zh"):
                post["title_zh"] = str(data["title_zh"]).strip()
            if data.get("selftext_zh") is not None and (post.get("selftext") or post.get("link_context")):
                post["selftext_zh"] = str(data.get("selftext_zh") or "").strip()
            source = "llm"
        except Exception as e:  # fall back to templates rather than blocking the pipeline
            source = f"template (LLM 失败: {str(e)[:120]})"
    if not variants:
        variants, image_prompt = _template(post, styles, lang)
    fallback_title = post.get("title_zh") or post["title"]
    out = [{"style": v.get("style", ""), "content": fit(v["text"], link, max_weight), "card_title": (v.get("card_title") or fallback_title).strip()}
           for v in variants]
    return out, image_prompt, source

