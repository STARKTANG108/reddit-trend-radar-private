from __future__ import annotations

import calendar
import time
from datetime import datetime

from .db import get_settings, row, tx


class QuotaExceeded(Exception):
    pass


def record_usage(endpoint: str, status_code: int | None = None, remaining: float | None = None) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO api_usage(ts, endpoint, status_code, remaining) VALUES(?, ?, ?, ?)",
            (int(time.time()), endpoint, status_code, remaining),
        )


def _day_start(now: datetime) -> int:
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def status() -> dict:
    s = get_settings()
    now = datetime.now()
    today = _day_start(now)
    month = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    with tx() as c:
        used_today = row(c, "SELECT COUNT(*) n FROM api_usage WHERE ts >= ?", (today,))["n"]
        used_month = row(c, "SELECT COUNT(*) n FROM api_usage WHERE ts >= ?", (month,))["n"]
        total = row(c, "SELECT COUNT(*) n, MIN(ts) first FROM api_usage")
    limit = int(s["quota_limit"])
    if s["quota_mode"] == "total":
        used_period = total["n"]
        elapsed = int((today - _day_start(datetime.fromtimestamp(total["first"] or today))) / 86400)
        days_left = max(1, int(s["quota_total_days"]) - elapsed)
    else:
        used_period = used_month
        days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    remaining = max(0, limit - used_period)
    daily_budget = max(1.0, (remaining + used_today) / days_left)
    return {
        "mode": s["quota_mode"],
        "limit": limit,
        "used_today": used_today,
        "used_month": used_month,
        "used_period": used_period,
        "used_total": total["n"],
        "remaining": remaining,
        "days_left": days_left,
        "daily_budget": round(daily_budget, 1),
        "pressure": round(used_today / daily_budget, 3),
    }


def ensure(priority: str = "normal", n: int = 1) -> None:
    """priority: high (candidate work) / normal / low (late-stage tracking)."""
    st = status()
    if st["remaining"] < n:
        raise QuotaExceeded("调用额度已用完")
    limits = {"high": 1.5, "normal": 1.0, "low": 0.8}
    if st["pressure"] >= limits.get(priority, 1.0):
        raise QuotaExceeded(f"今日预算已用 {st['pressure']:.0%}，跳过 {priority} 优先级请求")
