"""清洗、去重、打分、选取。所有规则都写在这里，便于审阅。"""
from __future__ import annotations
import datetime as dt
import math
import re
from collections import Counter

from .models import Item
from .text import tokens, jaccard, similar, canonical_url, has_cjk, heat_log


# ───────────────────────── 清洗 ─────────────────────────
def clean(items: list[Item], lex: dict, section: str = "daily", filters: dict | None = None) -> tuple[list[Item], dict]:
    """清洗：剔除商业推广；日报/榜单屏蔽政治与 AI 科技话题；成人栏只做“非露骨”边界过滤。
    日报不做“露骨内容”筛选；唯一底线是未经当事人同意传播的私密影像（针对真人的泄露类内容）。"""
    filters = filters if filters is not None else {"politics": True, "ai_tech": True}
    commercial = re.compile(lex["commercial_re"], re.I)
    ncii = re.compile(lex["ncii_re"], re.I)
    adult_explicit = re.compile(lex["adult_explicit_re"], re.I)
    politics = re.compile(lex["politics_re"], re.I)
    ai_cs, ai_ci = re.compile(lex["ai_cs_re"]), re.compile(lex["ai_re"], re.I)
    sports = re.compile(lex["sports_re"], re.I)
    kept, drop = [], Counter()
    for it in items:
        text = f"{it.title} {it.desc}"
        if not it.title.strip() or not it.url.startswith("http"):
            drop["无效"] += 1
            continue
        if commercial.search(text):
            drop["商业推广"] += 1
            continue
        if section == "adult":
            if adult_explicit.search(text):
                drop["成人栏边界过滤"] += 1
                continue
            kept.append(it)
            continue
        if ncii.search(text):
            drop["未经同意的私密影像"] += 1
            continue
        if section == "chart":
            if filters.get("politics", True) and politics.search(it.title):
                drop["政治"] += 1
                continue
            kept.append(it)
            continue
        if filters.get("politics", True) and politics.search(text):
            drop["政治"] += 1
            continue
        if filters.get("ai_tech", True) and (ai_cs.search(text) or ai_ci.search(text)):
            drop["AI科技"] += 1
            continue
        if section == "daily" and sports.search(text):
            it.flags.append("sports")
        kept.append(it)
    return kept, dict(drop)


# ───────────────────────── 去重与跨源印证 ─────────────────────────
def heat(it: Item) -> float:
    cands = [0.0]
    if it.rank:
        cands.append(1.0 / (1.0 + 0.12 * (it.rank - 1)))
    m = it.metrics
    if m.get("traffic"):
        cands.append(0.9 * heat_log(m["traffic"], 7))
    if m.get("views"):
        cands.append(0.9 * heat_log(m["views"], 7))
    if m.get("hot"):
        cands.append(0.9 * heat_log(m["hot"], 7))
    if m.get("accounts"):
        cands.append(0.8 * heat_log(m["accounts"], 4))
    base = max(cands)
    if it.kind == "news" and base == 0:
        base = 0.30
    if it.kind == "manual":
        base = max(base, 0.8)
    if it.kind == "chart" and base == 0:
        base = 0.5
    return base


def dedupe(items: list[Item], threshold: float = 0.55) -> list[Item]:
    """同一渠道内：同一事件只留热度最高的一条。（跨渠道不删，各渠道消息各自完整。）"""
    order = sorted(items, key=lambda i: -heat(i))
    kept: list[Item] = []
    for it in order:
        key = canonical_url(it.url)
        dup = next((k for k in kept if canonical_url(k.url) == key or similar(k.title, it.title, threshold)), None)
        if dup is None:
            kept.append(it)
        elif len(it.desc) > len(dup.desc):
            dup.desc = it.desc
    return kept


def corroborate(items: list[Item], threshold: float = 0.55) -> None:
    """跨渠道印证：同一事件在别的渠道也出现，记为“另见”，并提高综合分数。只标注，不删除。"""
    toks = {i.id: tokens(i.title) for i in items}
    for idx, a in enumerate(items):
        for b in items[idx + 1:]:
            if a.channel == b.channel:
                continue
            if jaccard(toks[a.id], toks[b.id]) >= threshold or similar(a.title, b.title, threshold):
                if b.channel not in a.also_in:
                    a.also_in.append(b.channel)
                if a.channel not in b.also_in:
                    b.also_in.append(a.channel)


def drop_recent_repeats(items: list[Item], recent: list[dict], window_days: int, gain: float) -> tuple[list[Item], int]:
    """近 N 天已发过的事件不重复，除非热度明显上升（标记为“追踪”）。"""
    seen: dict[str, dict] = {}
    for rec in recent[:window_days]:
        for r in rec.get("items", []):
            seen.setdefault(r["id"], r)
            for old_title in [r.get("title", "")]:
                seen.setdefault("t:" + old_title.lower(), r)
    kept, dropped = [], 0
    for it in items:
        prev = seen.get(it.id)
        if prev is None:
            for pid, r in seen.items():
                if pid.startswith("t:") and similar(r.get("title", ""), it.title, 0.6):
                    prev = r
                    break
        if prev is None:
            kept.append(it)
        elif it.score >= prev.get("score", 0) * (1 + gain):
            it.flags.append("follow_up")
            kept.append(it)
        else:
            dropped += 1
    return kept, dropped


# ───────────────────────── 打分 ─────────────────────────
def _parse_dt(s: str):
    """把各来源的时间字符串统一成带时区（UTC）的 datetime；纯日期也能处理；失败返回 None。"""
    try:
        d = dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def score(items: list[Item], cfg_by_id: dict, now: dt.datetime) -> None:
    for it in items:
        h = heat(it)
        pub = _parse_dt(it.published) if it.published else None
        recency = math.exp(-max(0.0, (now - pub).total_seconds() / 3600) / 36.0) if pub else 0.7
        corro = min(1.0, 0.5 * len(it.also_in))
        weight = cfg_by_id.get(it.source, {}).get("weight", 1.0)
        it.score = round((0.6 * h + 0.15 * recency + 0.25 * corro) * weight, 4)
        if "manual" in it.flags or it.kind == "manual":
            it.score += 0.2
        if "sports" in it.flags:
            it.score *= 0.6


# ───────────────────────── 渠道内选取 ─────────────────────────
def select_channel(items: list[Item], count: int, max_sports: int = 1) -> list[Item]:
    """在一个渠道内按平台名次（或综合分数）取前 count 条；体育赛事限量。"""
    from .channels import order_items
    out, sports = [], 0
    for it in order_items(items):
        if "sports" in it.flags:
            if sports >= max_sports:
                continue
            sports += 1
        out.append(it)
        if len(out) >= count:
            break
    return out


def theme_hits(it: Item, themes: dict) -> list[str]:
    text = f"{it.title} {it.title_zh} {it.summary_zh} {it.desc} {it.drama_signal}".lower()
    return [name for name, words in themes.items() if any(w in text for w in words)]
