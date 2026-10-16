"""Build static site bundle (dist/) for GitHub Pages deployment."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, images, main, pipeline, scheduler, scoring  # noqa: E402
from app.db import row, rows, tx  # noqa: E402
from app.generator import weighted_len  # noqa: E402


def build(skip_fetch: bool = False) -> None:
    main._bootstrap()
    db.update_settings({"poll_new_minutes": 20, "refresh_check_minutes": 20, "candidate_max_age_hours": 12, "min_comments": 5})

    if not skip_fetch:
        scheduler.run_job("collect")
        scoring.rebuild_baselines()
        scheduler.run_job("refresh")
        with tx() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM posts WHERE status='tracking'")]
        promoted = scoring.score_posts(ids)
        if promoted:
            pipeline.process_candidates()
        if config.LLM_CONFIGURED:
            pipeline.translate_missing_titles(60)

    now = time.time()
    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "app" / "static", dist / "static")

    # Write index.html with window.RADAR_STATIC = true injected
    idx_html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    idx_html = idx_html.replace(
        '<script src="static/charts.js"></script>',
        '<script>window.RADAR_STATIC = true;</script>\n  <script src="static/charts.js"></script>',
    )
    (dist / "index.html").write_text(idx_html, encoding="utf-8")
    (dist / ".nojekyll").write_text("", encoding="utf-8")

    # Build post_details from DB without triggering per-post synchronous LLM calls
    all_posts = main.list_posts(status="", max_age_hours=72, limit=500)
    post_details: dict[str, dict] = {}
    with tx() as c:
        all_snaps = rows(c, "SELECT post_id, ts, age_min, score, num_comments, upvote_ratio FROM snapshots ORDER BY ts")
        all_tweets = rows(c, "SELECT * FROM tweets WHERE status IN ('draft','used') ORDER BY id")

    snaps_by_post: dict[str, list[dict]] = {}
    for s in all_snaps:
        snaps_by_post.setdefault(s["post_id"], []).append(
            {"ts": s["ts"], "age_min": s["age_min"], "score": s["score"], "num_comments": s["num_comments"], "upvote_ratio": s["upvote_ratio"]}
        )
    tweets_by_post: dict[str, list[dict]] = {}
    for t in all_tweets:
        t["weight"] = weighted_len(t["content"])
        tweets_by_post.setdefault(t["post_id"], []).append(t)

    for p in all_posts:
        pid = p["id"]
        age = (now - p["created_utc"]) / 60
        curve_ages = sorted({a for a in [5, 10, 15, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720, 1080, 1440] if a <= max(age, 15) * 1.2})
        baseline = [{"age_min": a, **{k: round(v, 1) for k, v in scoring.baseline_at(p["subreddit"], a).items()}} for a in curve_ages]
        post_details[pid] = {
            "post": p,
            "snapshots": snaps_by_post.get(pid, []),
            "baseline": baseline,
            "tweets": tweets_by_post.get(pid, []),
            "assets": images.list_assets(pid),
        }

    bundle = {
        "status": main.status(),
        "overview": main.overview(),
        "usage": main.usage(30),
        "estimate": main.estimate(),
        "settings": main.read_settings(),
        "subreddits": main.list_subreddits(),
        "posts": all_posts,
        "tweets": main.list_tweets(status="draft,used,discarded,skipped", limit=500),
        "post_details": post_details,
    }
    (dist / "static-bundle.json").write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")

    # Persist radar.db inside dist/.state/radar.db so GitHub Actions can restore state across 20-min runs
    state_dir = dist / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)
    if config.DB_PATH.exists():
        shutil.copy2(config.DB_PATH, state_dir / "radar.db")

    print(f"Static build complete: {len(all_posts)} posts, {len(bundle['tweets'])} tweets -> {dist}")


if __name__ == "__main__":
    build(skip_fetch="--skip-fetch" in sys.argv)
