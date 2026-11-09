"""编排：采集 → 清洗 → 去重 → 打分 → 选取 → 编辑加工 → 短剧选题 → 渲染。"""
from __future__ import annotations
import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

from . import drama as D
from . import editor as E
from . import process as P
from . import render as R
from .article import attach_bodies
from .collect import collect
from .models import Item
from .sources import Ctx
from .text import similar

KEEP = ("id", "title", "url", "source", "source_name", "region", "kind", "section", "rank", "heat_text", "title_zh",
        "summary_zh", "tags", "drama_signal", "score", "also_in", "flags", "published")


def compact(it: Item) -> dict:
    d = it.to_dict()
    return {k: d[k] for k in KEEP}


@dataclass
class Edition:
    kind: str
    date: str
    parts: list = field(default_factory=list)       # [{key, chat_id, text, silent}]
    items: list = field(default_factory=list)
    results: list = field(default_factory=list)
    gaps: dict = field(default_factory=dict)
    drops: dict = field(default_factory=dict)
    enrich_stats: dict = field(default_factory=dict)
    drama_cards: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    preview_parts: list = field(default_factory=list)   # 未配置目标群时，仍写入预览/归档


def _chat_ids(env: dict, settings: dict) -> dict:
    main = env.get("TELEGRAM_CHAT_ID", "MAIN")
    admin = env.get("ADMIN_CHAT_ID", "")
    drama = env.get("DRAMA_CHAT_ID") or (main if settings.get("drama", {}).get("post_to_main_channel") else admin)
    return {"main": main, "drama": drama, "admin": admin}


def _parts(rendered, chat, silent_after_first=True) -> list[dict]:
    return [{"key": k, "chat_id": chat, "text": t, "silent": (i > 0 and silent_after_first)}
            for i, (k, t) in enumerate(rendered)]


def build_daily(date: str, now_utc: dt.datetime, settings, sources_cfg, lex, env, llm, translator, history,
                inbox_items=None, override: dict | None = None, log=print) -> Edition:
    ed = Edition("daily", date)
    cfg_by_id = {c["id"]: c for c in sources_cfg}
    limit = settings["telegram"]["message_limit"]
    chats = _chat_ids(env, settings)
    dd, flt = settings["dedupe"], settings.get("filters", {})
    total, reserve = settings["targets"]["daily_total"], settings["targets"].get("reserve", 10)

    if override is not None:                                   # 离线演示/测试：直接使用给定条目
        selected = [Item.from_dict(d) for d in override.get("daily", [])]
        pool = list(selected)
        charts = [Item.from_dict(d) for d in override.get("chart", [])]
        adult = [Item.from_dict(d) for d in override.get("adult", [])]
    else:
        ed.results = collect(sources_cfg, Ctx(now=now_utc, env=env, settings=settings))
        raw = [it for r in ed.results for it in r.items] + list(inbox_items or [])
        sec = {"daily": [], "chart": [], "adult": []}
        for it in raw:
            sec.setdefault(it.section, []).append(it)
        daily, ed.drops = P.clean(sec["daily"], lex, "daily", flt)
        daily = P.dedupe(daily, dd["title_similarity"])
        P.score(daily, cfg_by_id, now_utc)
        recent = history.recent(date, dd["repeat_window_days"])
        daily, rep = P.drop_recent_repeats(daily, recent, dd["repeat_window_days"], dd["repeat_score_gain"])
        if rep:
            ed.drops["近3天重复"] = rep
        pool, ed.gaps = P.select_daily(daily, settings, reserve=reserve)      # 多选一批备选，供二次判定后补位
        # 榜单：同样屏蔽政治/AI 科技话题；按“榜单顺序、名次”排列
        if settings.get("chart", {}).get("enabled", True):
            charts, cd = P.clean(sec["chart"], lex, "chart", flt)
            for k, v in cd.items():
                ed.drops[k] = ed.drops.get(k, 0) + v
            charts.sort(key=lambda i: (i.extra.get("board_order", 9), i.extra.get("board", ""), i.rank or 99))
        else:
            charts = []
        # 成人平台行业栏：只收主流媒体的非露骨报道；14 天内不重复；无内容不发
        adult = []
        if settings["adult"]["enabled"]:
            a, d2 = P.clean(sec["adult"], lex, "adult")
            for k, v in d2.items():
                ed.drops[k] = ed.drops.get(k, 0) + v
            a = P.dedupe(a, dd["title_similarity"])
            a.sort(key=lambda x: x.published, reverse=True)
            a, _ = P.drop_recent_repeats(a, history.recent(date, 14), 14, 99)
            adult = a[: settings["adult"]["max_items"]]
        # 抓正文片段，作为写“更长简介”的输入（失败静默；只作输入，不存档）
        got = attach_bodies(pool + adult)
        ed.notes.append(f"正文抓取成功 {got}/{len(pool) + len(adult)} 条")

    everything = pool + charts + adult
    ed.enrich_stats = E.enrich(everything, llm, translator, lex, settings, log)

    if override is None:
        # 二次判定：大模型把内容归为政治/AI科技的，从备选里剔除并补位
        bad = [i for i in pool if i.extra.get("topic") in E.BLOCKED_TOPICS]
        for i in bad:
            key = "政治(AI判定)" if i.extra["topic"] == "politics" else "AI科技(AI判定)"
            ed.drops[key] = ed.drops.get(key, 0) + 1
        pool = [i for i in pool if i not in bad]
        selected = pool[:total]
        ed.gaps.update({"selected": len(selected), "target": total,
                        "cn": sum(1 for i in selected if i.region == "cn"),
                        "short_by": max(0, total - len(selected))})
    else:
        ed.gaps = {"selected": len(selected), "target": total, "cn": sum(1 for i in selected if i.region == "cn"),
                   "cn_min": settings["targets"]["cn_min"], "short_by": max(0, total - len(selected))}

    intro = E.overview(selected, llm, date, log)
    # AI 短剧选题
    k = settings["targets"]["drama_picks"]
    picks = D.pick_candidates(selected, lex, k)
    if override is not None and override.get("drama_cards"):
        valid = {i.id for i in selected}
        cards = [D.validate_card(dict(c), valid) for c in override["drama_cards"]]
    else:
        cards = D.generate(picks, llm, k, log) or D.material_cards(picks, lex)
    ed.drama_cards = cards

    ed.items = selected + charts + adult
    parts = _parts(R.render_daily(date, intro, selected, limit), chats["main"])
    parts += _parts(R.render_charts(date, charts, limit), chats["main"])
    parts += _parts(R.render_adult(date, adult, limit), chats["main"])
    drama_parts = _parts(R.render_drama(date, cards, selected, limit), chats["drama"])
    ed.preview_parts = drama_parts
    if chats["drama"]:
        parts += drama_parts
    else:
        ed.notes.append("ℹ️ 未设置 DRAMA_CHAT_ID / ADMIN_CHAT_ID，今日短剧选题只保存到 archive，未发送。")
    ed.parts = parts
    ed.notes.append(f"榜单：{len(charts)} 条｜成人栏：{len(adult)} 条｜短剧选题：{len(cards)} 则"
                    f"（{'完整卡' if cards and cards[0].get('mode') == 'full' else '素材候选'}）")
    return ed


def build_weekly(date: str, now_utc: dt.datetime, settings, lex, env, llm, history, log=print) -> Edition:
    ed = Edition("weekly", date)
    limit = settings["telegram"]["message_limit"]
    chats = _chat_ids(env, settings)
    recs = history.window(date, 7)
    pool = [Item.from_dict(r) for rec in recs for r in rec["items"] if r.get("section", "daily") == "daily"]
    pool.sort(key=lambda i: -i.score)
    picked: list[Item] = []
    for it in pool:
        if any(p.id == it.id or similar(p.title_zh or p.title, it.title_zh or it.title, 0.6) for p in picked):
            continue
        cn_n = sum(1 for p in picked if p.region == "cn")
        if it.region == "cn" and cn_n >= 7:
            continue
        if it.region != "cn" and len(picked) - cn_n >= 20:
            continue
        picked.append(it)
        if len(picked) >= settings["targets"]["daily_total"]:
            break
    ed.gaps = {"selected": len(picked), "target": settings["targets"]["daily_total"],
               "cn": sum(1 for i in picked if i.region == "cn"), "cn_min": 0,
               "short_by": max(0, settings["targets"]["daily_total"] - len(picked))}
    tags = Counter(t for it in picked for t in it.tags)
    intro = E.overview(picked, llm, date, log, period="本周") if picked else "本周暂无已发布记录。"
    lines = []
    for rec in recs:
        for c in rec.get("drama", []):
            if c.get("mode") != "material" and c.get("working_title") and c.get("logline"):
                lines.append(f"• 《{c['working_title']}》— {c['logline']}")
    ed.items = picked
    ed.parts = _parts(R.render_weekly(date, intro, picked, lines[:5], limit), chats["main"])
    ed.notes.append("本周主题：" + "、".join(f"{t}×{n}" for t, n in tags.most_common(5)))
    return ed
