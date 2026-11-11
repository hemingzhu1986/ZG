"""把条目渲染成 Telegram HTML 消息。规则：整条为单位打包，绝不在一条内容中间截断；每条消息 ≤ 限制（UTF-16）。"""
from __future__ import annotations
import datetime as dt
import html

from .models import Item
from .text import utf16_len, trunc

WEEK = "一二三四五六日"


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def date_label(date: str) -> str:
    d = dt.date.fromisoformat(date)
    return f"{date} 周{WEEK[d.weekday()]}"


def hashtag(t: str) -> str:
    return "#" + t.replace("·", "").replace(" ", "")


def _pub_date(it: Item) -> str:
    try:
        d = dt.datetime.fromisoformat(it.published)
        return f"{d.month}/{d.day}发布"
    except (ValueError, TypeError):
        return ""


def item_block(n: int, it: Item, show_source: bool = True) -> str:
    title = esc(it.title_zh or it.title)
    lines = [f'{n}. <b><a href="{esc(it.url)}">{title}</a></b>']
    if it.summary_zh:
        lines.append(esc(it.summary_zh))
    foot = [esc(it.source_name)] if show_source else []      # 渠道消息的标题里已有渠道名，不再重复
    if it.heat_text:
        foot.append(esc(it.heat_text))
    elif not show_source and _pub_date(it):
        foot.append(_pub_date(it))
    if it.also_in:
        foot.append("另见：" + esc("、".join(it.also_in[:2])))
    mark = []
    if "follow_up" in it.flags:
        mark.append("📈昨日追踪")
    if "untranslated" in it.flags:
        mark.append("（未译）")
    tags = " ".join(hashtag(t) for t in it.tags[:2])
    lines.append("<i>" + " · ".join(foot) + "</i>" + (" " + " ".join(mark) if mark else "") + (" " + tags if tags else ""))
    return "\n".join(lines)


def pack(header: str, blocks: list[str], limit: int, cont_header: str = "") -> list[str]:
    """按块打包成多条消息；每条都带标题（后续条带“续”）。"""
    msgs, cur = [], header
    for b in blocks:
        b = b if utf16_len(b) <= limit - utf16_len(header) - 4 else trunc(b, 700)
        trial = cur + "\n\n" + b
        if utf16_len(trial) <= limit:
            cur = trial
        else:
            msgs.append(cur)
            cur = (cont_header or header) + "\n\n" + b
    msgs.append(cur)
    return msgs


# ───────────────────────── 各栏目 ─────────────────────────
def _head(date: str, meta: dict) -> str:
    title = meta.get("title") or f"{meta['channel']}资讯"
    note = meta.get("note", "")
    return (f"<b>{meta.get('emoji', '📰')} {esc(title)}</b>\n{esc(date_label(date))}"
            + (f"\n<i>{esc(note)}</i>" if note else ""))


def fit_one(head: str, blocks: list[str], limit: int, tail: str = "") -> tuple[str, int]:
    """把尽可能多的整条内容放进**一条**消息（不拆成多条，也不在条目中间截断）。
    放不下的低排名条目被舍弃，返回（消息文本, 实际放入条数）。"""
    text, n = head, 0
    reserve = (utf16_len(tail) + 2) if tail else 0
    for b in blocks:
        if n == 0 and utf16_len(head) + utf16_len(b) + 2 + reserve > limit:
            b = trunc(b, max(200, limit - utf16_len(head) - reserve - 10))
        trial = text + "\n\n" + b
        if utf16_len(trial) + reserve > limit:
            break
        text, n = trial, n + 1
    if tail:
        text += "\n\n" + tail
    return text, n


def render_channel(date: str, meta: dict, items: list[Item], limit: int) -> tuple[str, str, int]:
    """一个渠道一条消息。返回（key, 文本, 实际条数）。"""
    if not items:
        return meta["key"], "", 0
    text, n = fit_one(_head(date, meta), [item_block(i, it, show_source=False) for i, it in enumerate(items, 1)], limit)
    return meta["key"], text, n


def render_chart_channel(date: str, meta: dict, items: list[Item], limit: int) -> tuple[str, str, int]:
    """榜单类渠道：一条消息，内部按榜单（如电影/剧集）分组。"""
    if not items:
        return meta["key"], "", 0
    boards: dict[str, list[Item]] = {}
    for it in sorted(items, key=lambda i: (i.extra.get("board_order", 9), i.rank or 99)):
        boards.setdefault(it.extra.get("board", it.source_name), []).append(it)
    blocks, shown = [], 0
    for name, rows in boards.items():
        rows = sorted(rows, key=lambda x: x.rank or 99)
        note = rows[0].extra.get("board_note", "")
        lines = [f"<b>{esc(name)}</b>" + (f"　<i>{esc(note)}</i>" if note else "")]
        for it in rows:
            zh = it.title_zh or it.title
            orig = it.extra.get("original_title") or (it.title if it.title != zh else "")
            label = esc(zh) + (f"（{esc(orig)}）" if orig and orig != zh else "")
            sub = it.extra.get("subtitle", "")
            extra = " · ".join(x for x in (esc(sub) if sub and sub not in (it.summary_zh or "") else "", esc(it.heat_text)) if x)
            lines.append(f'{it.rank}. <a href="{esc(it.url)}">{label}</a>' + (f" <i>{extra}</i>" if extra else ""))
            if it.summary_zh and it.summary_zh != sub:
                lines.append("　" + esc(it.summary_zh))
            shown += 1
        blocks.append("\n".join(lines))
    foot = ["榜单口径各异，名次不可跨榜比较。"]
    if any(i.source.startswith("tmdb") for i in items):
        foot.append("This product uses the TMDB API but is not endorsed or certified by TMDB.")
    text, n = fit_one(_head(date, meta), blocks, limit, "<i>" + " ".join(esc(x) for x in foot) + "</i>")
    return meta["key"], text, shown


def render_adult(date: str, meta: dict, items: list[Item], limit: int) -> tuple[str, str, int]:
    """成人平台行业观察：一条消息；只含非露骨的行业、监管与研究类报道。"""
    if not items:
        return meta["key"], "", 0
    head = (f"<b>{meta.get('emoji', '🔎')} {esc(meta.get('title') or meta['channel'])}</b>\n{esc(date_label(date))}\n"
            f"<i>仅收录主流媒体的非露骨行业、监管与研究类报道；不提供内容榜单、演员信息或观看入口。</i>")
    text, n = fit_one(head, [item_block(i, it, show_source=False) for i, it in enumerate(items, 1)], limit)
    return meta["key"], text, n


def _drama_basis(c: dict, by_id: dict) -> str:
    rows = []
    for bid in c.get("basis_ids", []):
        it = by_id.get(bid)
        if it:
            rows.append(f'• <a href="{esc(it.url)}">{esc(it.title_zh or it.title)}</a>'
                        + (f"（{esc(it.heat_text)}）" if it.heat_text else ""))
    return "\n".join(rows)


def drama_block(n: int, c: dict, by_id: dict) -> str:
    if c.get("mode") == "material":
        themes = "、".join(c.get("themes", [])) or "未识别"
        return "\n".join([
            f"<b>素材候选 {n}｜{esc(c['working_title'].replace('素材：', ''))}</b>",
            f"<b>数据依据</b>\n{_drama_basis(c, by_id)}",
            f"主题：{esc(themes)}｜情绪：{esc(c['emotion'])}",
            f"冲突类型：{esc(c['conflict'])}｜可参考类型：{esc(c['genre'])}",
            "<i>（未配置大模型，仅输出素材候选，不含完整选题。）</i>"])
    af = c["ai_feasibility"]
    ver = c["versions"]
    return "\n".join([
        f"<b>{n}. 《{esc(c['working_title'])}》</b>｜{esc(c['genre'])}｜{esc(c['format'])}",
        f"<b>一句话</b>：{esc(c['logline'])}",
        f"<b>数据依据</b>\n{_drama_basis(c, by_id)}",
        f"<b>情绪与冲突</b>：{esc(c['emotion'])}；{esc(c['conflict'])}",
        f"<b>世界观与规则</b>：{esc(c['setup'])}",
        "<b>主要角色</b>\n" + "\n".join("• " + esc(x) for x in c["characters"]),
        "<b>分集结构</b>\n" + "\n".join("• " + esc(x) for x in c["structure"]),
        "<b>前三集</b>\n" + "\n".join(f"{i}) {esc(x)}" for i, x in enumerate(c["first3"][:3], 1)),
        f"<b>核心反转</b>：{esc(c['twist'])}",
        "<b>钩子</b>\n" + "\n".join("• " + esc(x) for x in c["hooks"][:3]),
        f"<b>AI 制作可行性：{esc(af.get('level'))}</b>｜{esc(af.get('note', ''))}",
        "<b>风险</b>：" + esc("；".join(c["risks"][:2])),
        f"<b>版本</b>：国内——{esc(ver.get('cn', ''))}；海外——{esc(ver.get('global', ''))}",
    ])


def render_drama(date: str, cards: list[dict], items: list[Item], limit: int) -> list[tuple[str, str]]:
    if not cards:
        return []
    by_id = {i.id: i for i in items}
    head = (f"<b>🎬 织光内容雷达｜AI短剧选题 · 今日 {len(cards)} 则</b>\n{esc(date_label(date))}\n"
            f"<i>基于今日入选热点；只借情绪与叙事结构，不借具体事件、真人私事或现有作品。</i>")
    msgs = pack(head, [drama_block(n, c, by_id) for n, c in enumerate(cards, 1)], limit,
                "<b>🎬 AI短剧选题（续）</b>")
    return [(f"drama#{k}", m) for k, m in enumerate(msgs, 1)]


def render_weekly(date: str, intro: str, items: list[Item], drama_lines: list[str], limit: int) -> list[tuple[str, str]]:
    head = (f"<b>🗓 织光内容雷达｜本周热榜回顾</b>\n{esc(date_label(date))}\n"
            f"<b>本周概览</b>：{esc(intro)}\n<i>以下为本周已发布条目按综合热度去重后的精选，非实时榜、非周总量。</i>")
    blocks = [item_block(n, it) for n, it in enumerate(items, 1)]
    if drama_lines:
        blocks.append("<b>🎬 本周 AI 短剧选题回顾</b>\n" + "\n".join(drama_lines))
    return [(f"weekly#{k}", m) for k, m in enumerate(pack(head, blocks, limit, "<b>🗓 本周热榜回顾（续）</b>"), 1)]
