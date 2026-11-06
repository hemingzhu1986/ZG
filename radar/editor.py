"""编辑加工：为每条热点生成中文标题与简介。
三档降级：LLM → 免费翻译+摘要 → 英文原文（并标注）。任何一档失败都不会中断发布。"""
from __future__ import annotations
import json
import re

from .config import load_prompt
from .models import Item
from .process import theme_hits
from .text import has_cjk, first_sentence, trunc

TAGS = ["明星八卦", "影视综艺", "音乐演出", "短视频·梗", "游戏动漫", "社会热点", "关系讨论", "职场生活", "AI科技",
        "怀旧", "体育"]


def _guess_tags(it: Item, lex: dict) -> list[str]:
    text = f"{it.title} {it.desc}".lower()
    hits = [t for t, words in lex["tags"].items() if any(w in text for w in words)]
    return hits[:2] or (["社会热点"] if it.kind == "trend" else [])


def _apply_llm(items: list[Item], out, log) -> int:
    if not isinstance(out, list):
        raise ValueError("expected array")
    by_id = {it.id: it for it in items}
    n = 0
    for row in out:
        if not isinstance(row, dict) or row.get("id") not in by_id:
            continue
        it = by_id[row["id"]]
        t, s = str(row.get("title_zh", "")).strip(), str(row.get("summary_zh", "")).strip()
        if not t or not has_cjk(t + s):
            continue
        it.title_zh = trunc(t, 36)
        it.summary_zh = trunc(s, 110)
        it.tags = [x for x in (row.get("tags") or []) if x in TAGS][:3]
        it.drama_signal = trunc(str(row.get("drama_signal", "")).strip(), 60)
        n += 1
    return n


def enrich(items: list[Item], llm, translator, lex: dict, settings: dict, log=print) -> dict:
    stats = {"llm": 0, "translated": 0, "raw": 0}
    todo = [it for it in items if not it.title_zh or not it.summary_zh]
    if llm is not None and getattr(llm, "available", False) and todo:
        system = load_prompt("post_zh.txt")
        size = settings.get("llm", {}).get("batch_size", 25)
        for i in range(0, len(todo), size):
            chunk = todo[i:i + size]
            payload = [{"id": it.id, "title": it.title, "source": it.source_name, "desc": trunc(it.desc, 280),
                        "heat": it.heat_text, "region": it.region} for it in chunk]
            try:
                out = llm.complete_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=6000)
                stats["llm"] += _apply_llm(chunk, out, log)
            except Exception as e:
                log(f"[editor] LLM 批次失败：{type(e).__name__}，该批降级处理")
    for it in items:
        # 标题
        if not it.title_zh:
            if has_cjk(it.title):
                it.title_zh = it.title
            else:
                t = translator.translate(it.title) if translator else None
                if t:
                    it.title_zh = t
                    stats["translated"] += 1
                else:
                    it.title_zh = it.title
                    it.flags.append("untranslated")
                    stats["raw"] += 1
        # 简介
        if not it.summary_zh and it.desc:
            if has_cjk(it.desc):
                it.summary_zh = first_sentence(it.desc, 90)
            else:
                d = translator.translate(first_sentence(it.desc, 120)) if translator else None
                it.summary_zh = trunc(d, 90) if d else "（英文原文）" + first_sentence(it.desc, 100)
        if not it.tags:
            it.tags = _guess_tags(it, lex)
    return stats


def overview(items: list[Item], llm, date_label: str, log=print, period: str = "今日") -> str:
    """今日简介：LLM 概括；没有 LLM 时用模板，不编造。"""
    if llm is not None and getattr(llm, "available", False) and items:
        try:
            payload = [{"title": it.title_zh, "tags": it.tags} for it in items[:30]]
            out = llm.complete_json(load_prompt("overview_zh.txt"), json.dumps(payload, ensure_ascii=False),
                                    max_tokens=400)
            if isinstance(out, dict) and has_cjk(str(out.get("intro", ""))):
                return trunc(str(out["intro"]), 130)
        except Exception as e:
            log(f"[editor] 今日简介生成失败：{type(e).__name__}")
    from collections import Counter
    tags = Counter(t for it in items for t in it.tags)
    top = "、".join(t for t, _ in tags.most_common(3)) or "综合"
    cn = sum(1 for i in items if i.region == "cn")
    return f"{period}收录 {len(items)} 条（国际 {len(items) - cn}、国内 {cn}），话题集中在：{top}。"
