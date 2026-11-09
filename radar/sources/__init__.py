"""数据源适配器注册表。每个适配器: fetch(cfg, ctx) -> list[Item]。
失败时抛异常（由流水线隔离并写入来源报告）；缺少密钥等“不是故障”的情况抛 Skipped。"""
from __future__ import annotations
import datetime as dt
import os
import re
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime


class Skipped(Exception):
    """配置不全，本次不运行（不算故障）。"""


class Stale(Exception):
    """来源可访问，但内容已过期（不能当作今天的热点）。"""


class ParseError(Exception):
    """来源可访问，但返回内容不是预期的格式（多半是对方改版）。sample 保留响应片段供诊断。"""

    def __init__(self, msg: str, sample: str = ""):
        super().__init__(msg)
        self.sample = (sample or "")[:300]


@dataclass
class Ctx:
    now: dt.datetime
    env: dict = field(default_factory=lambda: dict(os.environ))
    settings: dict = field(default_factory=dict)

    def expand(self, url: str) -> str:
        base = (self.env.get("RSSHUB_BASE") or "https://rsshub.app").rstrip("/")
        return url.replace("{RSSHUB_BASE}", base)


def parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except (TypeError, ValueError, IndexError):
        pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except ValueError:
        return None


def domain_matches(host: str, domains: list[str]) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def compile_re(p: str | None):
    return re.compile(p, re.I) if p else None


def registry():
    from . import feeds, apis, html_links, charts, social
    return {
        "justwatch": charts.fetch_justwatch,
        "douban": charts.fetch_douban,
        "drama_rank": charts.fetch_drama_rank,
        "tophub": charts.fetch_tophub,
        "douyin_hot": social.fetch_douyin,
        "zhihu_hot": social.fetch_zhihu,
        "tiktok_trends": social.fetch_tiktok_trends,
        "reddit_managed": social.fetch_reddit_managed,
        "apify": social.fetch_apify,
        "rss": feeds.fetch_rss,
        "google_trends": feeds.fetch_google_trends,
        "wikipedia_top": apis.fetch_wikipedia_top,
        "bilibili_ranking": apis.fetch_bilibili,
        "weibo_hot": apis.fetch_weibo,
        "mastodon_trends": apis.fetch_mastodon,
        "tmdb_chart": apis.fetch_tmdb,
        "html_links": html_links.fetch_html_links,
    }
