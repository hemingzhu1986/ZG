"""渠道：一个渠道发一条消息。同一个 channel 名的来源（含备用源）合并为一个渠道。"""
from __future__ import annotations
import re
from collections import defaultdict

from .models import Item

MANUAL = {"channel": "团队投稿", "emoji": "✍️", "channel_order": 1, "show": 8, "note": "团队成员通过 /add 投稿的内容"}


def channel_of(cfg: dict) -> str:
    return cfg.get("channel") or re.sub(r"[（(].*?[）)]", "", cfg["name"]).strip()


def assign_channels(items: list[Item], cfg_by_id: dict) -> None:
    for it in items:
        if it.channel:
            continue
        if it.source == "manual":
            it.channel = MANUAL["channel"]
        else:
            cfg = cfg_by_id.get(it.source)
            it.channel = channel_of(cfg) if cfg else it.source_name


def channel_meta(name: str, cfgs: list[dict]) -> dict:
    """同一渠道可能有主源和备用源：以主源的配置为准。"""
    if name == MANUAL["channel"]:
        return dict(MANUAL, key="ch_manual", title="")
    own = [c for c in cfgs if channel_of(c) == name]
    own.sort(key=lambda c: (c.get("role") == "fallback", c.get("channel_order", 500)))
    c = own[0] if own else {}
    return {"channel": name, "emoji": c.get("emoji", "📰"), "channel_order": c.get("channel_order", 500),
            "show": c.get("show", 6), "note": c.get("note", ""), "title": c.get("title", ""),
            "key": "ch_" + (c.get("id") or re.sub(r"\W+", "", name)[:12] or "x")}


def group_by_channel(items: list[Item]) -> dict[str, list[Item]]:
    g: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        g[it.channel].append(it)
    return g


def order_items(items: list[Item]) -> list[Item]:
    """有平台名次的渠道（微博、抖音、B站……）按平台名次排；否则按综合分数排。"""
    ranked = sum(1 for i in items if i.rank is not None)
    if items and ranked / len(items) >= 0.7:
        return sorted(items, key=lambda i: (i.rank if i.rank is not None else 999, -i.score))
    return sorted(items, key=lambda i: -i.score)
