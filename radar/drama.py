"""AI 短剧选题：从当天入选热点里挑候选 → 生成选题卡。
有 LLM：生成完整选题卡；无 LLM：只输出“素材候选卡”（主题标签+冲突类型+可参考类型），并明确标注，不冒充完整选题。"""
from __future__ import annotations
import json

from .config import load_prompt
from .models import Item
from .process import theme_hits
from .text import trunc

REQUIRED = ["working_title", "genre", "format", "logline", "basis_ids", "emotion", "conflict", "setup",
            "characters", "structure", "first3", "twist", "hooks", "ai_feasibility", "risks", "versions"]


def pick_candidates(items: list[Item], lex: dict, k: int = 3, history_items: list[dict] | None = None) -> list[Item]:
    """按“情绪冲突主题”打分，并要求主题互不相同，避免三张卡写同一个故事。"""
    themes = lex["themes"]
    scored = []
    for it in items:
        if it.section != "daily" or "sports" in it.flags:
            continue
        hits = theme_hits(it, themes)
        if not hits and not it.drama_signal:
            continue
        s = it.score + 0.15 * len(hits) + (0.2 if it.drama_signal else 0)
        scored.append((s, hits, it))
    scored.sort(key=lambda x: -x[0])
    picks, used = [], set()
    for s, hits, it in scored:
        main = hits[0] if hits else "其他"
        if main in used and len(scored) > k * 2:
            continue
        it.extra["themes"] = hits
        picks.append(it)
        used.add(main)
        if len(picks) >= k:
            break
    return picks


def validate_card(card: dict, valid_ids: set[str]) -> dict:
    missing = [k for k in REQUIRED if k not in card or card[k] in (None, "", [], {})]
    if missing:
        raise ValueError("missing:" + ",".join(missing))
    basis = [b for b in card["basis_ids"] if b in valid_ids]
    if not basis:
        raise ValueError("no valid basis_ids")
    card["basis_ids"] = basis
    for key in ("characters", "structure", "first3", "hooks", "risks"):
        if isinstance(card[key], str):
            card[key] = [card[key]]
        card[key] = [str(x) for x in card[key]]
    if not isinstance(card["ai_feasibility"], dict) or "level" not in card["ai_feasibility"]:
        raise ValueError("ai_feasibility")
    if not isinstance(card["versions"], dict):
        raise ValueError("versions")
    card["mode"] = "full"
    return card


def generate(picks: list[Item], llm, k: int, log=print) -> list[dict]:
    if not picks:
        return []
    if llm is None or not getattr(llm, "available", False):
        return []
    payload = {"k": min(k, len(picks)), "candidates": [
        {"id": it.id, "title_zh": it.title_zh, "summary_zh": it.summary_zh, "tags": it.tags,
         "drama_signal": it.drama_signal, "heat_text": it.heat_text} for it in picks]}
    try:
        out = llm.complete_json(load_prompt("drama_zh.txt"), json.dumps(payload, ensure_ascii=False), max_tokens=7000,
                                temperature=0.7)
    except Exception as e:
        log(f"[drama] LLM 失败：{type(e).__name__}，改为输出素材候选卡")
        return []
    valid = {it.id for it in picks}
    cards = []
    for c in out if isinstance(out, list) else []:
        try:
            cards.append(validate_card(c, valid))
        except Exception as e:
            log(f"[drama] 丢弃一张不合格的卡：{e}")
    return cards


def material_cards(picks: list[Item], lex: dict) -> list[dict]:
    cards = []
    for it in picks:
        themes = it.extra.get("themes") or theme_hits(it, lex["themes"]) or []
        main = themes[0] if themes else ""
        cards.append({
            "mode": "material", "basis_ids": [it.id], "themes": themes[:3],
            "emotion": it.drama_signal or "（需人工判断）",
            "conflict": lex["theme_conflict"].get(main, "（需人工判断）"),
            "genre": lex["theme_format"].get(main, "（需人工判断）"),
            "working_title": "素材：" + trunc(it.title_zh, 20)})
    return cards
