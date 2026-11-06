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


def admin_report(date: str, results, gaps: dict, drops: dict, enrich_stats: dict, llm_on: bool,
                 publish_res: dict | None, notes: list[str]) -> str:
    bad = [r for r in results if r.status in ("error", "stale")]
    lines = [f"<b>📋 雷达运行报告 {esc(date)}</b>",
             f"入选 {gaps.get('selected', 0)}/{gaps.get('target', 0)} 条｜国内 {gaps.get('cn', 0)}（目标≥{gaps.get('cn_min', 0)}）"]
    if gaps.get("short_by"):
        lines.append(f"⚠️ 比目标少 {gaps['short_by']} 条（不凑数，已如实少发）")
    if gaps.get("cn", 0) < gaps.get("cn_min", 0):
        lines.append("⚠️ 国内条数低于目标，请查看下方来源状态")
    if drops:
        lines.append("剔除：" + "、".join(f"{k}{v}" for k, v in drops.items()))
    lines.append(f"编辑：大模型{enrich_stats.get('llm', 0)}条｜免费翻译{enrich_stats.get('translated', 0)}条｜"
                 f"原文{enrich_stats.get('raw', 0)}条｜LLM{'已启用' if llm_on else '未配置'}")
    if publish_res:
        lines.append(f"发送：成功{len(publish_res['sent'])}｜已发过{len(publish_res['skipped'])}｜"
                     f"失败{len(publish_res['failed'])}｜待核实{len(publish_res['uncertain'])}")
        if publish_res["uncertain"]:
            lines.append("❗ 待核实：" + "、".join(publish_res["uncertain"]) + "（请查频道后运行 resolve）")
    lines += notes
    lines.append(f"\n<b>来源状态</b>（异常 {len(bad)}）\n" + source_table(results))
    return "\n".join(lines)
