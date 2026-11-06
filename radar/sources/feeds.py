"""RSS / Atom 通用适配器与谷歌趋势 RSS。"""
from __future__ import annotations
import datetime as dt
import xml.etree.ElementTree as ET

from .. import http
from ..models import Item
from ..text import strip_html, item_id, host_of, parse_traffic, fmt_num, trunc
from . import parse_date, domain_matches, compile_re


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    entries = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d: dict = {"news_items": []}
        for ch in el:
            n = _local(ch.tag)
            if n == "link":
                href = ch.get("href")
                if href and ch.get("rel") in (None, "alternate"):
                    d.setdefault("link", href)
                elif ch.text and ch.text.strip():
                    d.setdefault("link", ch.text.strip())
            elif n in ("title", "description", "summary", "content", "pubDate", "published", "updated",
                       "approx_traffic"):
                d.setdefault(n, (ch.text or "").strip())
            elif n == "source":
                d["source_url"] = ch.get("url", "")
                d["source_text"] = (ch.text or "").strip()
            elif n == "news_item":
                ni = {}
                for g in ch:
                    ni[_local(g.tag)] = (g.text or "").strip()
                d["news_items"].append(ni)
        entries.append(d)
    return entries


def fetch_rss(cfg: dict, ctx) -> list[Item]:
    url = ctx.expand(cfg["url"])
    entries = parse_feed(http.fetch(url, headers=cfg.get("headers")))
    inc, exc = compile_re(cfg.get("include")), compile_re(cfg.get("exclude"))
    allow, deny = cfg.get("domain_allow"), cfg.get("domain_deny")
    max_age = cfg.get("max_age_hours")
    ranked = bool(cfg.get("ranked"))
    out: list[Item] = []
    for idx, e in enumerate(entries, 1):
        title = strip_html(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link.startswith("http"):
            continue
        st = e.get("source_text")
        if st and title.endswith(" - " + st):          # Google News 标题带媒体后缀
            title = title[: -len(st) - 3].strip()
        desc = strip_html(e.get("description") or e.get("summary") or e.get("content") or "")
        if desc.strip().lower() == title.strip().lower():
            desc = ""
        text = title + " " + desc
        if inc and not inc.search(text):
            continue
        if exc and exc.search(text):
            continue
        dom = host_of(e.get("source_url") or link)
        if deny and domain_matches(dom, deny):
            continue
        if allow and not domain_matches(dom, allow):
            continue
        pub = parse_date(e.get("pubDate") or e.get("published") or e.get("updated"))
        if max_age and not ranked:
            if pub is None or not dt.timedelta(0) <= ctx.now - pub <= dt.timedelta(hours=max_age):
                continue                               # 无法确认时效的内容，不当作今天的热点
        it = Item(id=item_id(link, title), title=title, url=link, source=cfg["id"], source_name=cfg["name"],
                  region=cfg.get("region", "intl"), kind=cfg.get("kind", "news"),
                  section=cfg.get("section", "daily"), desc=trunc(desc, 500),
                  rank=idx if ranked else None, published=pub.isoformat() if pub else "")
        if ranked:
            it.heat_text = f"{cfg['name'].split('（')[0]}第{idx}位"
        if st:
            it.extra["publisher"] = st
        if dom:
            it.extra["domain"] = dom
        out.append(it)
        if len(out) >= cfg.get("limit", 10):
            break
    return out


def fetch_google_trends(cfg: dict, ctx) -> list[Item]:
    entries = parse_feed(http.fetch(ctx.expand(cfg["url"])))
    out: list[Item] = []
    for e in entries:
        title = strip_html(e.get("title", ""))
        if not title:
            continue
        news = e.get("news_items", [])
        link = (news[0].get("news_item_url") if news else "") or e.get("link", "")
        if not link.startswith("http"):
            continue
        traffic = parse_traffic(e.get("approx_traffic", ""))
        desc = " / ".join(n.get("news_item_title", "") for n in news[:2] if n.get("news_item_title"))
        pub = parse_date(e.get("pubDate"))
        it = Item(id=item_id(link, title), title=title, url=link, source=cfg["id"], source_name=cfg["name"],
                  region="intl", kind="trend", desc=trunc(strip_html(desc), 400),
                  metrics={"traffic": traffic}, published=pub.isoformat() if pub else "")
        if traffic:
            it.heat_text = f"谷歌搜索量{fmt_num(traffic)}+（24小时）"
        it.extra["news"] = [{"title": n.get("news_item_title", ""), "url": n.get("news_item_url", ""),
                             "source": n.get("news_item_source", "")} for n in news[:3]]
        out.append(it)
    out.sort(key=lambda x: -x.metrics.get("traffic", 0))   # 谷歌 RSS 顺序不是榜单，按搜索量区间排序
    for i, it in enumerate(out, 1):
        it.extra["traffic_order"] = i
    return out[: cfg.get("limit", 20)]
