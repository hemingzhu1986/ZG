"""榜单类适配器：JustWatch、豆瓣、短剧日榜（第三方）、聚合热榜站。
榜单条目统一 section="chart"，extra 里带 board（榜单名）、board_order（展示顺序）、board_note（口径说明）。"""
from __future__ import annotations
import datetime as dt
import html as htmllib
import json
import re
from urllib.parse import quote

from .. import http
from ..models import Item
from ..text import item_id, strip_html, trunc, has_cjk
from . import ParseError, Skipped, Stale

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _chart_item(cfg, board, order, note, rank, title, url, desc="", heat="", subtitle="", region="intl", **extra) -> Item:
    it = Item(id=item_id(url, title, salt=f"{board}|{rank}"), title=title, url=url, source=cfg["id"],
              source_name=cfg["name"], region=region, kind="chart", section="chart", rank=rank,
              desc=trunc(desc, 400), heat_text=heat)
    it.extra.update({"board": board, "board_order": order, "board_note": note, "subtitle": subtitle, **extra})
    return it


# ───────────────────────── JustWatch（公开 GraphQL，无需密钥） ─────────────────────────
JW_QUERY = """query GetPopularTitles($country: Country!, $language: Language!, $first: Int!, $filter: TitleFilter) {
  popularTitles(country: $country, first: $first, filter: $filter) {
    edges { node { __typename id objectType objectId
      ... on MovieOrShow { content(country: $country, language: $language) { title originalReleaseYear shortDescription fullPath } } } }
  }
}"""


def fetch_justwatch(cfg: dict, ctx) -> list[Item]:
    country, language, n = cfg.get("country", "US"), cfg.get("language", "en"), cfg.get("limit", 5)
    out: list[Item] = []
    for order, (obj, label) in enumerate((("MOVIE", "电影"), ("SHOW", "剧集"))):
        payload = {"operationName": "GetPopularTitles", "query": JW_QUERY,
                   "variables": {"country": country, "language": language, "first": n,
                                 "filter": {"objectTypes": [obj]}}}
        data = http.post_json("https://apis.justwatch.com/graphql", payload, retries=1, timeout=30,
                              headers={"Referer": "https://www.justwatch.com/", "app-version": "3.9.3-web-web",
                                       "Accept": "*/*"})
        edges = (((data.get("data") or {}).get("popularTitles")) or {}).get("edges") or []
        if not edges:
            raise ParseError("JustWatch 没有返回榜单（接口可能改版）", json.dumps(data, ensure_ascii=False))
        board = f"JustWatch {country} · {label}"
        for i, e in enumerate(edges[:n], 1):
            node = e.get("node") or {}
            c = node.get("content") or {}
            title = (c.get("title") or "").strip()
            if not title:
                continue
            path = c.get("fullPath") or ""
            url = "https://www.justwatch.com" + path if path else "https://www.justwatch.com/" + country.lower()
            year = c.get("originalReleaseYear")
            out.append(_chart_item(cfg, board, order, "按 JustWatch 用户关注度排序，不是票房或收视", i, title, url,
                                   desc=c.get("shortDescription") or "", heat=f"JustWatch{label}榜第{i}",
                                   subtitle=str(year or ""), media="movie" if obj == "MOVIE" else "tv"))
    return out


# ───────────────────────── 豆瓣（移动端公开榜单接口） ─────────────────────────
def fetch_douban(cfg: dict, ctx) -> list[Item]:
    coll, n = cfg["collection"], cfg.get("limit", 5)
    url = (f"https://m.douban.com/rexxar/api/v2/subject_collection/{coll}/items"
           f"?os=mac+os+x&for_mobile=1&start=0&count={n}&loc_id=0&_=0")
    data = http.fetch_json(url, headers={"Referer": "https://m.douban.com/movie/", "User-Agent": MOBILE_UA})
    rows = data.get("subject_collection_items") or []
    if not rows:
        raise ParseError("豆瓣没有返回榜单（接口可能改版或被拦）", json.dumps(data, ensure_ascii=False))
    board = cfg.get("board", f"豆瓣 · {coll}")
    out = []
    for i, r in enumerate(rows[:n], 1):
        title = (r.get("title") or "").strip()
        if not title:
            continue
        link = r.get("url") or (f"https://movie.douban.com/subject/{r['id']}/" if r.get("id") else "https://movie.douban.com/")
        rating = (r.get("rating") or {}).get("value")
        sub = strip_html(r.get("card_subtitle") or "")
        out.append(_chart_item(cfg, board, cfg.get("board_order", 5), "豆瓣实时热门", i, title, link, desc=sub,
                               heat=f"豆瓣榜第{r.get('rank_value') or i}" + (f"·评分{rating}" if rating else ""),
                               subtitle=sub, region="cn", rating=rating))
    return out


# ───────────────────────── 抖音官方短剧日榜（第三方：短剧工程） ─────────────────────────
FORMATS = ("AI漫剧", "AI短剧", "真人短剧", "真人剧", "漫剧", "短剧")
_NUM = re.compile(r"^[\d,]+(?:\.\d+)?$")


def _flatten(html: str) -> list[str]:
    h = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    h = re.sub(r"(?s)<[^>]+>", "\n", h)
    return [l for l in (" ".join(x.split()) for x in htmllib.unescape(h).split("\n")) if l]


def parse_drama_rank(lines: list[str], top_n: int) -> list[dict]:
    """从“今日 TOP10”之后解析：名次 → 剧名 / 题材 / 形态 / 热度。同时兼容“逐行”与“合并成一行”两种排版。"""
    start = next((i for i, l in enumerate(lines) if re.search(r"今日\s*TOP", l, re.I)), 0)
    rows, cur, expect = [], None, 1
    fmt_alt = "|".join(FORMATS)
    for l in lines[start + 1:]:
        mm = re.match(rf"^{expect}[\.、]?\s*(?:{expect}\s*)?(.+?)({fmt_alt})\s*([\d,\.]+)$", l)
        if mm:                                              # 整条记录在同一行
            if cur:
                rows.append(cur)
            cur = {"rank": expect, "parts": [mm.group(1).strip(), mm.group(2), mm.group(3)]}
            expect += 1
            if len(rows) >= top_n:
                break
            continue
        m = re.fullmatch(r"(\d{1,3})\.?", l)
        if m and int(m.group(1)) == expect:
            if cur:
                rows.append(cur)
            cur, expect = {"rank": expect, "parts": []}, expect + 1
            continue
        if cur is not None:
            if re.fullmatch(r"\d{1,3}\.?", l) and int(l.strip(".")) == cur["rank"]:
                continue                                    # 名次重复出现，跳过
            cur["parts"].append(l)
        if len(rows) >= top_n:
            break
    if cur and len(rows) < top_n:
        rows.append(cur)
    out = []
    for r in rows[:top_n]:
        parts = r["parts"]
        fmt = next((p for p in parts if p in FORMATS), "")
        heat = next((p for p in reversed(parts) if _NUM.match(p)), "")
        text = [p for p in parts if p != fmt and p != heat]
        if text:
            title, genre = text[0], (text[1] if len(text) > 1 else "")
        else:
            joined = " ".join(parts)
            m = re.match(rf"^(?P<t>.+?)(?P<f>{'|'.join(FORMATS)})\s*(?P<h>[\d,\.]+)$", joined)
            if not m:
                continue
            title, fmt, heat, genre = m["t"], m["f"], m["h"], ""
        if title:
            out.append({"rank": r["rank"], "title": title, "genre": genre, "format": fmt, "heat": heat})
    return out


def fetch_drama_rank(cfg: dict, ctx) -> list[Item]:
    page = http.fetch(cfg["url"], headers={"Accept": "text/html"}).decode("utf-8", "replace")
    lines = _flatten(page)
    text = "\n".join(lines)
    today = ctx.now.date()
    max_days = ctx.settings.get("freshness", {}).get("chart_page_days", 3)
    m = re.search(r"更新(?:至|于)?\s*(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    updated = None
    if m:
        try:
            updated = dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            updated = None
    if updated and (today - updated).days > max_days:
        raise Stale(f"榜单页最新日期 {updated}，已超过 {max_days} 天")
    rows = parse_drama_rank(lines, cfg.get("limit", 10))
    if len(rows) < 3:
        raise ParseError("没能从页面解析出榜单（页面可能改版）", " | ".join(lines[:25]))
    board = cfg.get("board", "抖音官方短剧日榜")
    out = []
    for r in rows:
        sub = " · ".join(x for x in (r["genre"], r["format"]) if x)
        out.append(_chart_item(cfg, board, cfg.get("board_order", 8), "抖音官方短剧榜热度（第三方转载）", r["rank"],
                               r["title"], cfg["url"], desc=sub,
                               heat=(f"热度{r['heat']}" if r["heat"] else "") + (f"（{updated.month}/{updated.day}）" if updated else ""),
                               subtitle=sub, region="cn", fmt=r["format"]))
    return out


# ───────────────────────── 聚合热榜站（今日热榜）：国内热榜的通用兜底 ─────────────────────────
def _text(s: str) -> str:
    return " ".join(strip_html(s).split())


def parse_tophub_home(html: str, label: str, board_hint: str = "") -> list[dict]:
    """首页按“卡片”分块：卡片里有平台名、榜单名，以及前若干条（标题、热度、链接）。"""
    blocks = re.split(r'(?=<div[^>]+class="[^"]*\bcc-cd\b[^"]*")', html)
    for b in blocks:
        name = re.search(r'class="[^"]*cc-cd-lb[^"]*"[^>]*>(.*?)</', b, re.S)
        sub = re.search(r'class="[^"]*cc-cd-sb-st[^"]*"[^>]*>(.*?)</', b, re.S)
        nm, sb = _text(name.group(1)) if name else "", _text(sub.group(1)) if sub else ""
        if label not in nm and label not in sb:
            continue
        if board_hint and board_hint not in (nm + sb):
            continue
        rows = []
        for href, inner in re.findall(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S):
            if href.startswith(("#", "javascript", "/n/", "/")) and not href.startswith("//"):
                continue
            t = re.search(r'class="[^"]*\bt\b[^"]*"[^>]*>(.*?)</span>', inner, re.S)
            e = re.search(r'class="[^"]*\be\b[^"]*"[^>]*>(.*?)</span>', inner, re.S)
            title = _text(t.group(1)) if t else _text(inner)
            if len(title) >= 2:
                rows.append({"title": title, "url": href, "heat": _text(e.group(1)) if e else ""})
        if rows:
            return [dict(r, rank=i, board=f"{nm}{sb}") for i, r in enumerate(rows, 1)]
    return []


def parse_tophub_node(html: str) -> list[dict]:
    """节点页：表格，每行 名次 / 标题链接 / 热度。"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        a = re.search(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', tr, re.S)
        if not a:
            continue
        tds = [_text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        title = _text(a.group(2))
        if len(title) >= 2:
            rows.append({"title": title, "url": a.group(1), "heat": tds[-1] if len(tds) >= 3 else "", "board": ""})
    return [dict(r, rank=i) for i, r in enumerate(rows, 1)]


def _discover_nodes(html: str, label: str) -> list[str]:
    ids = []
    for m in re.finditer(r'href="/n/([A-Za-z0-9]+)"', html):
        near = _text(html[m.start(): m.start() + 400])
        if label in near and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def fetch_tophub(cfg: dict, ctx) -> list[Item]:
    label, n = cfg["label"], cfg.get("limit", 15)
    home = http.fetch(cfg.get("url", "https://tophub.today/"), headers={"Accept": "text/html"}).decode("utf-8", "replace")
    rows = parse_tophub_home(home, label, cfg.get("board_hint", ""))
    if not rows:
        for node in _discover_nodes(home, label)[:2]:
            try:
                page = http.fetch(f"https://tophub.today/n/{node}", headers={"Accept": "text/html"}).decode("utf-8", "replace")
            except http.FetchError:
                continue
            rows = parse_tophub_node(page)
            if rows:
                break
    if not rows:
        raise ParseError(f"聚合站里没找到「{label}」榜单（页面可能改版）", _text(home)[:300])
    out = []
    for r in rows[:n]:
        url = r["url"] if r["url"].startswith("http") else "https://tophub.today" + r["url"]
        name = cfg["name"].split("（")[0]
        it = Item(id=item_id(url, r["title"]), title=r["title"], url=url, source=cfg["id"], source_name=cfg["name"],
                  region="cn", kind=cfg.get("kind", "trend"), rank=r["rank"],
                  heat_text=f"{name}第{r['rank']}" + (f"·{r['heat']}" if r["heat"] else ""),
                  metrics={"hot": _heat_num(r["heat"])} if _heat_num(r["heat"]) else {})
        out.append(it)
    return out


def _heat_num(s: str) -> int:
    m = re.search(r"([\d.]+)\s*(万|亿|w|W)?", s or "")
    if not m:
        return 0
    v = float(m.group(1))
    return int(v * {"万": 1e4, "亿": 1e8, "w": 1e4, "W": 1e4}.get(m.group(2) or "", 1))
