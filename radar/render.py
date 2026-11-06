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


def item_block(n: int, it: Item) -> str:
    title = esc(it.title_zh or it.title)
    lines = [f'{n}. <b><a href="{esc(it.url)}">{title}</a></b>']
    if it.summary_zh:
        lines.append(esc(it.summary_zh))
    foot = [esc(it.source_name)]
    if it.heat_text:
        foot.append(esc(it.heat_text))
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
def render_daily(date: str, intro: str, items: list[Item], limit: int) -> list[tuple[str, str]]:
    intl = [i for i in items if i.region != "cn"]
    cn = [i for i in items if i.region == "cn"]
    out: list[tuple[str, str]] = []
    if intl:
        head = (f"<b>🌍 织光内容雷达｜每日精选 · 国际</b>\n{esc(date_label(date))}\n"
                f"<b>今日简介</b>：{esc(intro)}")
        msgs = pack(head, [item_block(n, it) for n, it in enumerate(intl, 1)], limit,
                    f"<b>🌍 每日精选 · 国际（续）</b>")
        out += [(f"daily_intl#{k}", m) for k, m in enumerate(msgs, 1)]
    if cn:
        head = f"<b>🇨🇳 织光内容雷达｜每日精选 · 国内</b>\n{esc(date_label(date))}"
        msgs = pack(head, [item_block(n, it) for n, it in enumerate(cn, 1)], limit, "<b>🇨🇳 每日精选 · 国内（续）</b>")
        out += [(f"daily_cn#{k}", m) for k, m in enumerate(msgs, 1)]
    return out


def render_screen(date: str, items: list[Item], limit: int) -> list[tuple[str, str]]:
    if not items:
        return []
    head = (f"<b>🎬 织光内容雷达｜影视日榜</b>\n{esc(date_label(date))}\n"
            f"<i>依据 TMDB 站内日趋势热度排序，不是票房或收视榜。</i>")
    blocks = []
    for label, media in (("电影", "movie"), ("剧集", "tv")):
        rows = sorted([i for i in items if i.extra.get("media") == media], key=lambda x: x.rank or 99)
        if not rows:
            continue
        lines = [f"<b>{label} Top {len(rows)}</b>"]
        for it in rows:
            orig = it.extra.get("original_title", "")
            name = esc(it.title_zh or it.title) + (f"（{esc(orig)}）" if orig and orig != (it.title_zh or "") else "")
            lines.append(f'{it.rank}. <a href="{esc(it.url)}">{name}</a>')
            if it.summary_zh:
                lines.append("　" + esc(it.summary_zh))
        blocks.append("\n".join(lines))
    blocks.append("<i>This product uses the TMDB API but is not endorsed or certified by TMDB.</i>")
    return [(f"screen#{k}", m) for k, m in enumerate(pack(head, blocks, limit), 1)]


def render_adult(date: str, items: list[Item], limit: int) -> list[tuple[str, str]]:
    if not items:
        return []
    head = (f"<b>🔎 织光内容雷达｜成人平台行业观察</b>\n{esc(date_label(date))}\n"
            f"<i>仅收录主流媒体的非露骨行业、监管与研究类报道；不提供内容榜单、演员信息或观看入口。</i>")
    msgs = pack(head, [item_block(n, it) for n, it in enumerate(items, 1)], limit)
    return [(f"adult#{k}", m) for k, m in enumerate(msgs, 1)]


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
