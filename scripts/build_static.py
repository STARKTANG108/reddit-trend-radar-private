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


def build(skip_fetch: bool = False, push: bool = False) -> None:
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
    git_backup = None
    if (dist / ".git").exists():
        git_backup = ROOT / ".dist_git_tmp"
        if git_backup.exists():
            shutil.rmtree(git_backup)
        shutil.move(dist / ".git", git_backup)
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True, exist_ok=True)
    if git_backup and git_backup.exists():
        shutil.move(git_backup, dist / ".git")
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

    print(f"Static build complete: {len(all_posts)} posts, {len(bundle['tweets'])} tweets -> {dist}")
    if push:
        push_gh_pages(dist)


def push_gh_pages(dist: Path | None = None) -> bool:
    import os
    import subprocess

    dist = dist or (ROOT / "dist")
    if not dist.exists():
        return False
    git_dir = dist / ".git"
    env = {**os.environ, "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(dist)}
    try:
        out = subprocess.check_output(
            ["git", "credential-osxkeychain", "get"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            timeout=5,
        )
        creds = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
        token = creds.get("password", "")
    except Exception:
        token = ""
    remote = (
        f"https://x-access-token:{token}@github.com/STARKTANG108/reddit-trend-radar.git"
        if token
        else "https://github.com/STARKTANG108/reddit-trend-radar.git"
    )
    if not git_dir.exists():
        subprocess.run(["git", "init", "-b", "gh-pages", str(dist)], check=False, capture_output=True)
    subprocess.run(["git", "config", "user.name", "STARKTANG108"], env=env, check=False, capture_output=True)
    subprocess.run(["git", "config", "user.email", "STARKTANG108@users.noreply.github.com"], env=env, check=False, capture_output=True)
    subprocess.run(["git", "add", "-A"], env=env, check=False, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"chore: auto-refresh radar ({int(time.time())})"], env=env, check=False, capture_output=True)
    subprocess.run(["git", "remote", "remove", "origin"], env=env, check=False, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", remote], env=env, check=False, capture_output=True)
    r = subprocess.run(["git", "push", "-u", "origin", "gh-pages", "--force"], env=env, check=False, capture_output=True)
    subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/STARKTANG108/reddit-trend-radar.git"], env=env, check=False, capture_output=True)
    return r.returncode == 0


if __name__ == "__main__":
    build(skip_fetch="--skip-fetch" in sys.argv, push="--push" in sys.argv)

