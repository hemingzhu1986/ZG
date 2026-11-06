"""时间窗：北京时间。每天 10:00 起发日报（补跑窗口到 14:00），周五 17:30 起发周回顾（到 21:00）。
窗口 + 账本 = 多次触发也只发一次；错过窗口不补发过期内容。"""
from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _m(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _zone(tz: str):
    try:
        return ZoneInfo(tz)
    except ZoneInfoNotFoundError:                       # 精简镜像可能没有时区库；北京时间无夏令时，可安全兜底
        if tz == "Asia/Shanghai":
            return dt.timezone(dt.timedelta(hours=8))
        raise


def local_now(tz: str, now: dt.datetime | None = None) -> dt.datetime:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(_zone(tz))


def due(now_local: dt.datetime, sch: dict) -> str | None:
    mins = now_local.hour * 60 + now_local.minute
    if _m(sch["daily_start"]) <= mins < _m(sch["daily_deadline"]):
        return "daily"
    if now_local.weekday() == sch["weekly_weekday"] and _m(sch["weekly_start"]) <= mins < _m(sch["weekly_deadline"]):
        return "weekly"
    return None


def past_deadline(now_local: dt.datetime, sch: dict, kind: str) -> bool:
    mins = now_local.hour * 60 + now_local.minute
    return mins >= _m(sch["daily_deadline" if kind == "daily" else "weekly_deadline"])
