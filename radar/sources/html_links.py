"""第三方榜单页适配器：从“列表页”里挑出榜单文章链接，并读取其摘要。
只保存标题、摘要与链接，不转载正文。超过 N 天未更新的页面会被判定为 Stale，不会当作今天的热点。"""
from __future__ import annotations
import datetime as dt
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .. import http
from ..models import Item
from ..text import item_id, strip_html, trunc
from . import Stale, compile_re


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._buf = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._buf).split())))
            self._href = None


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.title, self._in_title = {}, "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = a.get("property") or a.get("name")
            if k and a.get("content"):
                self.meta.setdefault(k.lower(), a["content"])
        elif tag == "title":
            self._in_title = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


_MD = re.compile(r"(\d{1,2})月(\d{1,2})日")
_YMD = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")


def guess_date(text: str, today: dt.date) -> dt.date | None:
    m = _YMD.search(text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _MD.search(text)
    if m:
        try:
            d = dt.date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        return d if d <= today + dt.timedelta(days=1) else d.replace(year=today.year - 1)
    return None


def fetch_html_links(cfg: dict, ctx) -> list[Item]:
    page_url = cfg["url"]
    if not page_url:
        raise http.FetchError("EmptyUrl")
    html = http.fetch(page_url, headers={"Accept": "text/html"}).decode("utf-8", "replace")
    parser = _Links()
    parser.feed(html)
    inc = compile_re(cfg.get("include"))
    today = ctx.now.date()
    max_days = ctx.settings.get("freshness", {}).get("chart_page_days", 3)
    seen, cands = set(), []
    for href, text in parser.links:
        if not href or not text or len(text) < 6 or (inc and not inc.search(text)):
            continue
        url = urljoin(page_url, href)
        if url in seen:
            continue
        seen.add(url)
        d = guess_date(text, today)
        cands.append((d, url, text))
    if not cands:
        raise http.FetchError("NoLinks")
    fresh = [c for c in cands if c[0] and (today - c[0]).days <= max_days]
    if not fresh:
        newest = max((c[0] for c in cands if c[0]), default=None)
        raise Stale(f"最新榜单日期 {newest or '未知'}，已超过 {max_days} 天")
    fresh.sort(key=lambda c: c[0], reverse=True)
    out: list[Item] = []
    for d, url, text in fresh[: cfg.get("limit", 6)]:
        desc = ""
        if cfg.get("fetch_detail"):
            try:
                mp = _Meta()
                mp.feed(http.fetch(url, timeout=15, retries=1).decode("utf-8", "replace"))
                desc = strip_html(mp.meta.get("og:description") or mp.meta.get("description") or "")
            except Exception:
                desc = ""
        out.append(Item(id=item_id(url, text), title=text, url=url, source=cfg["id"], source_name=cfg["name"],
                        region="cn", kind="chart", desc=trunc(desc, 400), published=dt.datetime.combine(
                            d, dt.time(), tzinfo=dt.timezone.utc).isoformat(),
                        heat_text=f"榜单日期{d.month}/{d.day}（第三方转载）"))
    return out
