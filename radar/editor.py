"""编辑加工：为每条热点生成中文标题与简介。
三档降级：LLM → 免费翻译+摘要 → 英文原文（并标注）。任何一档失败都不会中断发布。"""
from __future__ import annotations
import json
import re

from .config import load_prompt
from .models import Item
from .process import theme_hits
from .text import has_cjk, needs_translation, first_sentence, trunc, leading_sentences

TAGS = ["明星八卦", "影视综艺", "音乐演出", "短视频·梗", "游戏动漫", "社会热点", "关系讨论", "职场生活", "怀旧", "体育"]
BLOCKED_TOPICS = ("politics", "ai_tech")


def _guess_tags(it: Item, lex: dict) -> list[str]:
    text = f"{it.title} {it.desc}".lower()
    hits = [t for t, words in lex["tags"].items() if any(w in text for w in words)]
    return hits[:2] or (["社会热点"] if it.kind == "trend" else [])


def _apply_llm(items: list[Item], out, log) -> int:
    if isinstance(out, dict):                                   # 兼容 {"items":[...]} / {"results":[...]} 这类外层包装
        out = next((v for v in out.values() if isinstance(v, list)), out)
    if not isinstance(out, list):
        raise ValueError("expected array")
    by_id = {it.id: it for it in items}
    n = 0
    for row in out:
        if not isinstance(row, dict) or row.get("id") not in by_id:
            continue
        it = by_id[row["id"]]
        t, s = str(row.get("title_zh", "")).strip(), str(row.get("summary_zh", "")).strip()
        if not t or needs_translation(t) or (s and needs_translation(s)):
            continue                                            # 仍是外文：视为失败，交给重试/机翻
        it.title_zh = trunc(t, 40)
        it.summary_zh = trunc(s, 360)
        topic = str(row.get("topic", "")).strip().lower()
        if topic in BLOCKED_TOPICS:
            it.extra["topic"] = topic
        it.tags = [x for x in (row.get("tags") or []) if x in TAGS][:3]
        it.drama_signal = trunc(str(row.get("drama_signal", "")).strip(), 60)
        n += 1
    return n


def _llm_pass(todo: list[Item], llm, settings: dict, log) -> int:
    """让大模型处理一批条目；返回成功条数。失败只记录，不中断。"""
    done = 0
    system = load_prompt("post_zh.txt")
    size = settings.get("llm", {}).get("batch_size", 15)
    for i in range(0, len(todo), size):
        chunk = todo[i:i + size]
        payload = [{"id": it.id, "title": it.title, "source": it.source_name, "kind": it.kind,
                    "desc": trunc(it.desc, 400), "body": trunc(it.extra.get("body", ""), 1500),
                    "heat": it.heat_text, "region": it.region} for it in chunk]
        try:
            out = llm.complete_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=12000)
            n = _apply_llm(chunk, out, log)
            done += n
            if n < len(chunk):
                llm.last_error = llm.last_error or f"返回了 {n}/{len(chunk)} 条可用结果（其余为外文或缺失）"
        except Exception as e:
            from .llm import describe_error
            llm.last_error = describe_error(e)
            log(f"[editor] 大模型批次失败：{llm.last_error}；该批将重试或降级")
    return done


def _fallback_translate(it: Item, translator, stats: dict) -> None:
    """机器翻译兜底：标题与简介里凡是外文的，都翻成中文；翻不了的明确标注“未译”。"""
    if not it.title_zh:
        if not needs_translation(it.title):
            it.title_zh = it.title
        else:
            t = translator.translate(it.title) if translator else None
            if t:
                it.title_zh, stats["mt"] = t, stats["mt"] + 1
            else:
                it.title_zh = it.title
                it.flags.append("untranslated")
                stats["raw"] += 1
    if not it.summary_zh:
        src = it.desc
        if len(src) < 90 and it.extra.get("body"):
            src = (src + " " + leading_sentences(it.extra["body"], 160)).strip()
        if src:
            limit = 80 if it.kind == "chart" else 220
            if not needs_translation(src):
                it.summary_zh = leading_sentences(src, limit)
            else:
                d = translator.translate(leading_sentences(src, 300)) if translator else None
                if d:
                    it.summary_zh = leading_sentences(d, limit)
                    stats["mt"] += 1
                else:
                    it.summary_zh = "（未译）" + leading_sentences(src, 160)
                    if "untranslated" not in it.flags:
                        it.flags.append("untranslated")
                    stats["raw"] += 1


def enrich(items: list[Item], llm, translator, lex: dict, settings: dict, log=print) -> dict:
    """生成中文标题与简介。顺序：大模型 → 大模型对漏译条目再试一次 → 免费机器翻译 → 标注“未译”。"""
    stats = {"llm": 0, "mt": 0, "raw": 0}
    todo = [it for it in items if not it.title_zh or not it.summary_zh]
    if llm is not None and getattr(llm, "available", False) and todo:
        stats["llm"] += _llm_pass(todo, llm, settings, log)
        retry = [it for it in todo if not it.title_zh or (not it.summary_zh and it.desc)]
        if retry:                                               # 大模型漏掉或失败的，再试一次
            stats["llm"] += _llm_pass(retry, llm, settings, log)
    for it in items:
        _fallback_translate(it, translator, stats)
        if not it.tags:
            it.tags = _guess_tags(it, lex)
    if llm is not None and getattr(llm, "available", False):
        stats["llm_error"] = (llm.last_error if stats["llm"] == 0 and todo else "")      # 全军覆没时才当作故障上报
        stats["llm_notice"] = getattr(llm, "notice", "")
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
