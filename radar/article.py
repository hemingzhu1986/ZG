"""抓取文章正文片段，仅作为写简介的输入（不转载、不存档）。失败一律静默，不影响发布。"""
from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser

from . import http
from .models import Item
from .text import host_of, strip_html

SKIP_DOMAINS = ("reddit.com", "tiktok.com", "instagram.com", "x.com", "twitter.com", "weibo.com", "weibo.cn",
                "douyin.com", "bilibili.com", "xiaohongshu.com", "trends.google.com", "news.google.com",
                "wikipedia.org", "douban.com", "zhihu.com", "justwatch.com", "themoviedb.org", "tophub.today")
_JUNK = ("cookie", "subscribe", "sign up", "newsletter", "advertisement", "all rights reserved", "privacy policy",
         "click here", "read more", "follow us", "订阅", "版权所有", "广告")


class _Paras(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_article = 0
        self.skip = 0
        self.in_p = False
        self.buf: list[str] = []
        self.article_ps: list[str] = []
        self.all_ps: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "nav", "footer", "aside", "form"):
            self.skip += 1
        elif tag == "article":
            self.in_article += 1
        elif tag == "p" and not self.skip:
            self.in_p, self.buf = True, []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "nav", "footer", "aside", "form"):
            self.skip = max(0, self.skip - 1)
        elif tag == "article":
            self.in_article = max(0, self.in_article - 1)
        elif tag == "p" and self.in_p:
            t = " ".join("".join(self.buf).split())
            self.in_p = False
            if len(t) >= 50 and not any(j in t.lower() for j in _JUNK):
                self.all_ps.append(t)
                if self.in_article:
                    self.article_ps.append(t)

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.buf.append(data)


def extract_body(html: str, max_chars: int = 1800) -> str:
    p = _Paras()
    try:
        p.feed(html)
    except Exception:
        pass
    paras = p.article_ps or p.all_ps
    out, n = [], 0
    for t in paras:
        out.append(strip_html(t))
        n += len(t)
        if n >= max_chars:
            break
    return " ".join(out)[:max_chars]


def _one(it: Item) -> tuple[str, str]:
    urls = [it.url] + [n.get("url", "") for n in it.extra.get("news", [])[:2]]
    for u in urls:
        if not u.startswith("http") or any(host_of(u).endswith(d) for d in SKIP_DOMAINS):
            continue
        try:
            html = http.fetch(u, headers={"Accept": "text/html"}, timeout=10, retries=0,
                              max_bytes=900_000).decode("utf-8", "replace")
        except Exception:
            continue
        body = extract_body(html)
        if len(body) >= 120:
            return it.id, body
    return it.id, ""


def attach_bodies(items: list[Item], budget_secs: float = 80.0, workers: int = 8) -> int:
    """并发抓取，总耗时有上限。返回成功条数。"""
    todo = [i for i in items if i.kind in ("news", "trend", "community", "manual") and not i.extra.get("body")]
    if not todo:
        return 0
    by_id = {i.id: i for i in items}
    ok, deadline = 0, time.time() + budget_secs
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = [pool.submit(_one, i) for i in todo]
        for f in as_completed(futs, timeout=budget_secs):
            try:
                iid, body = f.result()
            except Exception:
                continue
            if body:
                by_id[iid].extra["body"] = body
                ok += 1
            if time.time() > deadline:
                break
    except Exception:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return ok
