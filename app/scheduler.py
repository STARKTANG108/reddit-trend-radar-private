from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from . import collector, config, pipeline, scoring
from .db import get_settings

log = logging.getLogger("radar")

if config.DEMO_MODE:
    from .demo import FakeRedditClient

    client = FakeRedditClient()
elif config.REDDIT_PROVIDER == "redditdate":
    from .reddit_client import RedditDateClient

    client = RedditDateClient()
else:
    from .reddit_client import RedditClient

    client = RedditClient()


def _sync_gh_pages() -> None:
    try:
        from scripts.build_static import build

        build(skip_fetch=True, push=True)
    except Exception as e:
        log.warning("gh-pages sync skipped: %s", e)


def _refresh() -> dict:
    out = collector.refresh_due(client)
    out["candidates"] = pipeline.process_candidates()
    threading.Thread(target=_sync_gh_pages, daemon=True).start()
    return out


def _collect() -> dict:
    out = collector.collect_new(client)
    out["candidates"] = pipeline.process_candidates()
    if config.LLM_CONFIGURED:
        out["translated"] = pipeline.translate_missing_titles(40)
    threading.Thread(target=_sync_gh_pages, daemon=True).start()
    return out


JOBS: dict[str, dict] = {
    "collect": {"label": "拉取新帖", "fn": _collect},
    "refresh": {"label": "刷新热度", "fn": _refresh},
    "baselines": {"label": "重建社区基线", "fn": scoring.rebuild_baselines},
    "prune": {"label": "清理过期快照与缓存", "fn": collector.prune_old_data},
}
for j in JOBS.values():
    j.update(last_run=None, last_result=None, last_error=None, running=False, lock=threading.Lock())

scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120})


def run_job(name: str) -> dict:
    job = JOBS[name]
    fn: Callable = job["fn"]
    if not job["lock"].acquire(blocking=False):
        return {"skipped": "任务正在运行"}
    job["running"] = True
    try:
        result = fn()
        job.update(last_result=result, last_error=None)
        return {"result": result}
    except Exception as e:
        log.error("job %s failed: %s", name, traceback.format_exc())
        job["last_error"] = str(e)
        return {"error": str(e)}
    finally:
        job.update(last_run=int(time.time()), running=False)
        job["lock"].release()


def _intervals() -> dict[str, int]:
    s = get_settings()
    return {
        "collect": max(1, int(s["poll_new_minutes"])),
        "refresh": max(1, int(s["refresh_check_minutes"])),
        "baselines": 60,
        "prune": 360,
    }


def start() -> None:
    for name, minutes in _intervals().items():
        scheduler.add_job(run_job, "interval", minutes=minutes, args=[name], id=name, replace_existing=True)
    scheduler.start()


def reschedule() -> None:
    for name, minutes in _intervals().items():
        scheduler.reschedule_job(name, trigger="interval", minutes=minutes)


def jobs_status() -> list[dict]:
    out = []
    for name, j in JOBS.items():
        sj = scheduler.get_job(name) if scheduler.running else None
        out.append({
            "name": name, "label": j["label"], "running": j["running"], "last_run": j["last_run"],
            "last_result": j["last_result"], "last_error": j["last_error"],
            "next_run": int(sj.next_run_time.timestamp()) if sj and sj.next_run_time else None,
        })
    return out
