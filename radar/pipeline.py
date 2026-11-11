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
from .channels import assign_channels, channel_meta, group_by_channel
from .collect import collect
from .models import Item
from .process import select_channel
from .sources import Ctx
from .text import similar

KEEP = ("id", "title", "url", "source", "source_name", "region", "kind", "section", "rank", "heat_text", "title_zh",
        "summary_zh", "tags", "drama_signal", "score", "also_in", "flags", "published", "channel")


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
    channels: list = field(default_factory=list)     # 各渠道展示条数
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
    """每个渠道一条消息：谷歌趋势、维基百科、微博热搜……各发各的，不混在一起。"""
    ed = Edition("daily", date)
    cfg_by_id = {c["id"]: c for c in sources_cfg}
    limit = settings["telegram"]["message_limit"]
    chats = _chat_ids(env, settings)
    dd, flt = settings["dedupe"], settings.get("filters", {})
    llm_on = llm is not None and getattr(llm, "available", False)
    reserve = settings.get("channels", {}).get("reserve", 2) if llm_on else 0     # 备选：大模型二次判定政治/AI 后补位
    max_sports = settings["caps"].get("max_sports", 1)

    # ── 1) 得到“每个渠道的入选条目” ──
    groups: dict[str, list[Item]] = {}
    if override is not None:                                   # 离线演示/测试：直接使用给定条目
        allitems = [Item.from_dict(d) for k in ("daily", "chart", "adult") for d in override.get(k, [])]
        assign_channels(allitems, cfg_by_id)
        groups = group_by_channel(allitems)
        show_of = {n: 999 for n in groups}
    else:
        ed.results = collect(sources_cfg, Ctx(now=now_utc, env=env, settings=settings))
        raw = [it for r in ed.results for it in r.items] + list(inbox_items or [])
        assign_channels(raw, cfg_by_id)
        sec = {"daily": [], "chart": [], "adult": []}
        for it in raw:
            sec.setdefault(it.section, []).append(it)
        daily, ed.drops = P.clean(sec["daily"], lex, "daily", flt)
        P.corroborate(daily, dd["title_similarity"])
        P.score(daily, cfg_by_id, now_utc)
        recent = history.recent(date, dd["repeat_window_days"])
        rep_total = 0
        for name, rows in group_by_channel(daily).items():
            meta = channel_meta(name, sources_cfg)
            rows = P.dedupe(rows, dd["title_similarity"])
            rows, rep = P.drop_recent_repeats(rows, recent, dd["repeat_window_days"], dd["repeat_score_gain"])
            rep_total += rep
            groups[name] = select_channel(rows, meta["show"] + reserve, max_sports)
        if rep_total:
            ed.drops["近3天重复"] = rep_total
        if settings.get("chart", {}).get("enabled", True):
            charts, cd = P.clean(sec["chart"], lex, "chart", flt)
            for k, v in cd.items():
                ed.drops[k] = ed.drops.get(k, 0) + v
            for name, rows in group_by_channel(charts).items():
                groups[name] = rows
        if settings["adult"]["enabled"]:
            a, d2 = P.clean(sec["adult"], lex, "adult")
            for k, v in d2.items():
                ed.drops[k] = ed.drops.get(k, 0) + v
            for name, rows in group_by_channel(P.dedupe(a, dd["title_similarity"])).items():
                rows.sort(key=lambda x: x.published, reverse=True)
                rows, _ = P.drop_recent_repeats(rows, history.recent(date, 14), 14, 99)
                groups[name] = rows[: settings["adult"]["max_items"]]
        show_of = {n: channel_meta(n, sources_cfg)["show"] for n in groups}
        # 抓正文片段，作为写“更长简介”的输入（失败静默；只作输入，不存档）
        pool = [i for rows in groups.values() for i in rows if i.section in ("daily", "adult")]
        got = attach_bodies(pool)
        ed.notes.append(f"正文抓取成功 {got}/{len(pool)} 条")

    # ── 2) 编辑加工：翻译成中文、写简介 ──
    everything = [i for rows in groups.values() for i in rows]
    ed.enrich_stats = E.enrich(everything, llm, translator, lex, settings, log)

    # ── 3) 大模型二次判定政治/AI 科技 → 剔除并补位；再截到各渠道的展示条数 ──
    for name, rows in list(groups.items()):
        bad = [i for i in rows if i.extra.get("topic") in E.BLOCKED_TOPICS]
        for i in bad:
            key = "政治(AI判定)" if i.extra["topic"] == "politics" else "AI科技(AI判定)"
            ed.drops[key] = ed.drops.get(key, 0) + 1
        groups[name] = [i for i in rows if i not in bad][: show_of[name]]

    # ── 4) AI 短剧选题：从所有日报渠道的入选热点里挑 ──
    k = settings["targets"]["drama_picks"]
    news_items = [i for rows in groups.values() for i in rows if i.section == "daily"]
    picks = D.pick_candidates(news_items, lex, k)
    if override is not None and override.get("drama_cards"):
        valid = {i.id for i in news_items}
        cards = [D.validate_card(dict(c), valid) for c in override["drama_cards"]]
    else:
        cards = D.generate(picks, llm, k, log) or D.material_cards(picks, lex)
    ed.drama_cards = cards

    # ── 5) 渲染：一个渠道一条消息，按渠道顺序发送 ──
    metas = sorted((channel_meta(n, sources_cfg) for n in groups), key=lambda m: m["channel_order"])
    parts, cstats = [], []
    for meta in metas:
        rows = groups.get(meta["channel"], [])
        if not rows:
            cstats.append({"name": meta["channel"], "shown": 0})
            continue
        is_chart = all(i.section == "chart" for i in rows)
        is_adult = all(i.section == "adult" for i in rows)
        fn = R.render_chart_channel if is_chart else (R.render_adult if is_adult else R.render_channel)
        key, text, shown = fn(date, meta, rows, limit)
        cstats.append({"name": meta["channel"], "shown": shown})
        if text:
            parts.append({"key": key, "chat_id": chats["main"], "text": text, "silent": bool(parts), "label": meta["channel"]})
    ed.channels = cstats
    ed.items = [i for rows in groups.values() for i in rows]
    drama_parts = _parts(R.render_drama(date, cards, news_items, limit), chats["drama"])
    ed.preview_parts = drama_parts
    if chats["drama"]:
        parts += drama_parts
    else:
        ed.notes.append("ℹ️ 未设置 DRAMA_CHAT_ID / ADMIN_CHAT_ID，今日短剧选题只保存到 archive，未发送。")
    ed.parts = parts
    ed.gaps = {"selected": sum(c["shown"] for c in cstats), "channels": sum(1 for c in cstats if c["shown"]),
               "empty": [c["name"] for c in cstats if not c["shown"]]}
    ed.notes.append(f"短剧选题：{len(cards)} 则（{'完整卡' if cards and cards[0].get('mode') == 'full' else '素材候选'}）")
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
        if len(picked) >= 25:
            break
    ed.gaps = {"selected": len(picked), "channels": 0, "empty": []}
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
