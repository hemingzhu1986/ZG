"""来源健康报告：每次运行都生成，缺口一目了然，不靠人工手写。"""
from __future__ import annotations

from .render import esc

ICON = {"ok": "✅", "empty": "⚪️", "error": "❌", "stale": "🕘", "skipped": "⏭"}
LABEL = {"ok": "正常", "empty": "无内容", "error": "失败", "stale": "已过期", "skipped": "跳过"}


def source_table(results) -> str:
    rows = []
    for r in sorted(results, key=lambda x: (x.status != "error", x.status, x.id)):
        tail = f"{r.count}条" if r.status == "ok" else esc(r.error)[:60]
        rows.append(f"{ICON.get(r.status, '•')} {esc(r.name)}｜{LABEL.get(r.status, r.status)}｜{tail}｜{r.secs}s")
    return "\n".join(rows)


def doctor_details(results) -> str:
    """doctor 专用：正常的来源给出前两条示例，失败的来源给出响应片段，方便据实修正解析。"""
    lines = []
    for r in sorted(results, key=lambda x: x.id):
        if r.status == "ok" and r.items:
            ex = " ／ ".join((i.title_zh or i.title)[:26] for i in r.items[:2])
            lines.append(f"  · {r.name}：{ex}")
        elif r.status in ("error", "stale") and r.sample:
            lines.append(f"  · {r.name} 响应片段：{r.sample[:220]!r}")
    return "\n".join(lines)


def admin_report(date: str, results, gaps: dict, drops: dict, enrich_stats: dict, llm_on: bool,
                 publish_res: dict | None, notes: list[str], channels: list | None = None, mt_stats: dict | None = None) -> str:
    bad = [r for r in results if r.status in ("error", "stale")]
    ch = channels or []
    sent = [c for c in ch if c["shown"]]
    lines = [f"<b>📋 雷达运行报告 {esc(date)}</b>",
             f"渠道消息 {len(sent)} 条｜共 {gaps.get('selected', 0)} 条资讯"]
    empty = [c["name"] for c in ch if not c["shown"]]
    if empty:
        lines.append("⚠️ 今日无内容的渠道：" + esc("、".join(empty)))
    if drops:
        lines.append("剔除：" + "、".join(f"{k}{v}" for k, v in drops.items()))
    mt = mt_stats or {}
    lines.append(f"翻译：大模型{enrich_stats.get('llm', 0)}条｜机器翻译{enrich_stats.get('mt', 0)}条"
                 f"｜未能翻译{enrich_stats.get('raw', 0)}条（已标“未译”）｜大模型{'已启用' if llm_on else '未配置'}")
    if not llm_on:
        lines.append("⚠️ 未配置大模型：外文只能机器翻译，译文质量较低；请按 README 配置免费的 Gemini。")
    if mt.get("failed"):
        lines.append(f"⚠️ 机器翻译失败 {mt['failed']} 次（通道可能被拦截或额度用尽）")
    if publish_res:
        lines.append(f"发送：成功{len(publish_res['sent'])}｜已发过{len(publish_res['skipped'])}｜"
                     f"失败{len(publish_res['failed'])}｜待核实{len(publish_res['uncertain'])}")
        for k, why in (publish_res.get("errors") or {}).items():
            lines.append(f"❗ {esc(k)} 发送失败原因：{esc(why)}")
        if publish_res["uncertain"]:
            lines.append("❗ 待核实：" + "、".join(publish_res["uncertain"]) + "（请查频道后运行 resolve）")
    if ch:
        lines.append("\n<b>各渠道条数</b>\n" + "｜".join(f"{esc(c['name'])} {c['shown']}" for c in ch))
    lines += notes
    ok_n = sum(1 for r in results if r.status == "ok")
    skip_n = sum(1 for r in results if r.status == "skipped")
    empty_n = sum(1 for r in results if r.status == "empty")
    lines.append(f"\n<b>来源状态</b>：正常 {ok_n}｜无内容 {empty_n}｜跳过 {skip_n}｜异常 {len(bad)}")
    if bad:
        lines.append(source_table(bad))
    text = "\n".join(lines)
    return text if len(text) <= 3800 else text[:3780] + "\n…（报告过长已截断）"
