"""JSON 接口类适配器：维基百科、B站、微博、Mastodon、TMDB。"""
from __future__ import annotations
import datetime as dt
import re
from urllib.parse import quote

from .. import http
from ..models import Item
from ..text import strip_html, item_id, fmt_num, trunc, first_sentence, has_cjk
from . import Skipped, parse_date

_WIKI_SKIP = re.compile(r"^(Main_Page|Special:|Wikipedia:|Portal:|Help:|File:|Category:|Template:|Talk:|User:|"
                        r"Deaths_in_|List_of_Telugu|-$)", re.I)


# ───────────────────────── 维基百科 ─────────────────────────
def _wiki_top(project: str, day: dt.date) -> list[dict]:
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{project}/all-access/"
           f"{day.year}/{day.month:02d}/{day.day:02d}")
    data = http.fetch_json(url, headers={"Accept": "application/json"})
    return data["items"][0]["articles"]


def fetch_wikipedia_top(cfg: dict, ctx) -> list[Item]:
    project = cfg.get("project", "en.wikipedia")
    today = ctx.now.astimezone(dt.timezone.utc).date()
    day, rows = None, None
    for back in (1, 2):
        try:
            rows, day = _wiki_top(project, today - dt.timedelta(days=back)), today - dt.timedelta(days=back)
            break
        except http.FetchError as e:
            if e.status != 404:
                raise
    if not rows:
        raise http.FetchError("NoData")
    prev = {}
    try:                                                    # 前一天对比，剔除长年高居不下的“常青词条”
        prev = {r["article"]: r["views"] for r in _wiki_top(project, day - dt.timedelta(days=1))}
    except Exception:
        pass
    out: list[Item] = []
    for r in rows:
        art = r["article"]
        if _WIKI_SKIP.match(art) or art.startswith("List_of"):
            continue
        views = int(r["views"])
        p = prev.get(art)
        if p and views / max(p, 1) < 1.2:
            continue
        title = art.replace("_", " ")
        url = f"https://{project.split('.')[0]}.wikipedia.org/wiki/{quote(art)}"
        it = Item(id=item_id(url, title), title=title, url=url, source=cfg["id"], source_name=cfg["name"],
                  region="intl", kind="rank", rank=int(r.get("rank", 0)) or None, metrics={"views": views},
                  heat_text=f"维基浏览{fmt_num(views)}（{day.month}/{day.day}）", published=dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).isoformat())
        if p:
            it.metrics["views_prev"] = p
            it.heat_text += f"，较前日×{views / p:.1f}"
        out.append(it)
        if len(out) >= cfg.get("limit", 15):
            break
    if cfg.get("fetch_summary"):
        for it in out:
            try:
                s = http.fetch_json(f"https://{project.split('.')[0]}.wikipedia.org/api/rest_v1/page/summary/"
                                    f"{quote(it.title.replace(' ', '_'))}", timeout=10, retries=1)
                short = s.get("description", "")
                ext = first_sentence(s.get("extract", ""), 160)
                it.desc = trunc((short + "。 " if short else "") + ext, 300)
            except Exception:
                continue
    return out


# ───────────────────────── B站 ─────────────────────────
def fetch_bilibili(cfg: dict, ctx) -> list[Item]:
    data = http.fetch_json(ctx.expand(cfg["url"]), headers={"Referer": "https://www.bilibili.com/"})
    if data.get("code") != 0:
        raise http.FetchError(f"BilibiliCode{data.get('code')}")
    out: list[Item] = []
    for rank, v in enumerate(data["data"]["list"], 1):
        st = v.get("stat", {})
        view = int(st.get("view", 0) or 0)
        url = "https://www.bilibili.com/video/" + v["bvid"]
        pub = dt.datetime.fromtimestamp(v["pubdate"], dt.timezone.utc).isoformat() if v.get("pubdate") else ""
        desc = " ".join(x for x in (f"分区：{v.get('tname', '')}" if v.get("tname") else "", v.get("desc", "")) if x)
        it = Item(id=item_id(url, v["title"]), title=v["title"], url=url, source=cfg["id"], source_name=cfg["name"],
                  region="cn", kind="rank", rank=rank, desc=trunc(strip_html(desc), 400), published=pub,
                  metrics={"views": view, "likes": int(st.get("like", 0) or 0), "danmaku": int(st.get("danmaku", 0) or 0)},
                  heat_text=f"B站全站榜第{rank}·{fmt_num(view)}播放（累计）")
        it.extra["author"] = (v.get("owner") or {}).get("name", "")
        out.append(it)
        if len(out) >= cfg.get("limit", 12):
            break
    return out


# ───────────────────────── 微博 ─────────────────────────
def fetch_weibo(cfg: dict, ctx) -> list[Item]:
    data = http.fetch_json(cfg["url"], headers={"Referer": "https://weibo.com/", "Accept": "application/json"})
    rows = (data.get("data") or {}).get("realtime") or []
    if not rows:
        raise http.FetchError("EmptyWeibo")
    out: list[Item] = []
    for idx, r in enumerate(rows, 1):
        if r.get("is_ad") or r.get("ad_info") or r.get("promotion"):
            continue
        word = (r.get("word") or r.get("note") or "").strip()
        if not word:
            continue
        url = "https://s.weibo.com/weibo?q=" + quote(f"#{word}#")
        num = int(r.get("num", 0) or 0)
        it = Item(id=item_id(url, word), title=word, url=url, source=cfg["id"], source_name=cfg["name"],
                  region="cn", kind="trend", rank=idx, metrics={"hot": num},
                  heat_text=f"微博热搜第{idx}" + (f"·热度{fmt_num(num)}" if num else ""),
                  desc=strip_html(r.get("label_name", "") or ""))
        out.append(it)
        if len(out) >= cfg.get("limit", 15):
            break
    return out


# ───────────────────────── Mastodon ─────────────────────────
def fetch_mastodon(cfg: dict, ctx) -> list[Item]:
    base = cfg["instance"].rstrip("/")
    data = http.fetch_json(f"{base}/api/v1/trends/{cfg.get('endpoint', 'links')}?limit={cfg.get('limit', 10)}")
    out: list[Item] = []
    for idx, r in enumerate(data, 1):
        url = r.get("url", "")
        title = strip_html(r.get("title", ""))
        if not url or not title:
            continue
        hist = r.get("history") or []
        accounts = sum(int(h.get("accounts", 0)) for h in hist[:2])
        it = Item(id=item_id(url, title), title=title, url=url, source=cfg["id"], source_name=cfg["name"],
                  region="intl", kind="trend", rank=idx, metrics={"accounts": accounts},
                  desc=trunc(strip_html(r.get("description", "")), 300),
                  heat_text=f"Mastodon近2日{accounts}人分享" if accounts else "")
        it.extra["publisher"] = r.get("provider_name", "")
        pub = parse_date(r.get("published_at"))
        it.published = pub.isoformat() if pub else ""
        out.append(it)
    return out


# ───────────────────────── TMDB（免费密钥） ─────────────────────────
def fetch_tmdb(cfg: dict, ctx) -> list[Item]:
    key = ctx.env.get("TMDB_API_KEY", "")
    if not key:
        raise Skipped("未设置 TMDB_API_KEY")
    media = cfg.get("media", "movie")
    url = f"https://api.themoviedb.org/3/trending/{media}/day?language=zh-CN"
    headers = {"Accept": "application/json"}
    if key.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + key
    else:
        url += "&api_key=" + key
    data = http.fetch_json(url, headers=headers)
    out: list[Item] = []
    for idx, r in enumerate(data.get("results", []), 1):
        zh = r.get("title") or r.get("name") or ""
        orig = r.get("original_title") or r.get("original_name") or ""
        page = f"https://www.themoviedb.org/{media}/{r['id']}"
        label = "电影" if media == "movie" else "剧集"
        it = Item(id=item_id(page, zh, salt=f"tmdb|{idx}"), title=orig or zh, url=page, source=cfg["id"], source_name=cfg["name"],
                  region="intl", kind="chart", section="chart", rank=idx, desc=trunc(r.get("overview", ""), 300),
                  metrics={"popularity": float(r.get("popularity", 0))},
                  title_zh=zh if has_cjk(zh) else "", heat_text=f"TMDB{label}日趋势第{idx}")
        it.extra.update({"media": media, "original_title": orig, "board": f"TMDB · {label}日趋势",
                         "board_order": 0 if media == "movie" else 1, "board_note": "TMDB 站内趋势热度，不是票房或收视",
                         "subtitle": (r.get("release_date") or r.get("first_air_date") or "")[:4]})
        out.append(it)
        if len(out) >= cfg.get("limit", 5):
            break
    return out
