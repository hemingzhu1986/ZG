"""社媒热榜类适配器：抖音、知乎、TikTok 创作者中心、Reddit（第三方托管接口）、Apify 通用接口。
这些平台都没有稳定的官方开放接口，随时可能改版或拦截；所以每个适配器都配有备用来源，失败会在报告里写明原因。"""
from __future__ import annotations
import json
import re
from urllib.parse import quote

from .. import http
from ..models import Item
from ..text import item_id, strip_html, trunc, fmt_num
from . import ParseError, Skipped

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")


# ───────────────────────── 抖音热搜 ─────────────────────────
DOUYIN_URLS = ["https://www.douyin.com/aweme/v1/web/hot/search/list/?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1",
               "https://aweme.snssdk.com/aweme/v1/hot/search/list/?aid=1128&version_code=880"]


def parse_douyin(data: dict) -> list[dict]:
    d = data.get("data") or {}
    rows = d.get("word_list") or d.get("trending_list") or []
    out = []
    for i, r in enumerate(rows, 1):
        word = (r.get("word") or r.get("title") or "").strip()
        if not word or r.get("is_ad"):
            continue
        sid = r.get("sentence_id") or r.get("group_id")
        out.append({"word": word, "rank": int(r.get("position") or i), "hot": int(r.get("hot_value") or 0),
                    "url": f"https://www.douyin.com/hot/{sid}" if sid else f"https://www.douyin.com/search/{quote(word)}"})
    return out


def fetch_douyin(cfg: dict, ctx) -> list[Item]:
    last_sample, last_err = "", None
    for url in cfg.get("urls") or DOUYIN_URLS:
        try:
            data = http.fetch_json(url, headers={"User-Agent": DESKTOP_UA, "Referer": "https://www.douyin.com/hot"}, retries=1)
        except http.FetchError as e:
            last_err, last_sample = e, e.body
            continue
        rows = parse_douyin(data)
        if rows:
            return [Item(id=item_id(r["url"], r["word"]), title=r["word"], url=r["url"], source=cfg["id"],
                         source_name=cfg["name"], region="cn", kind="trend", rank=r["rank"],
                         metrics={"hot": r["hot"]},
                         heat_text=f"抖音热榜第{r['rank']}" + (f"·热度{fmt_num(r['hot'])}" if r["hot"] else ""))
                    for r in rows[: cfg.get("limit", 15)]]
        last_sample = json.dumps(data, ensure_ascii=False)
    if last_err and not last_sample:
        raise last_err
    raise ParseError("抖音热榜没有返回词条（接口可能需要签名或已改版）", last_sample)


# ───────────────────────── 知乎热榜 ─────────────────────────
def fetch_zhihu(cfg: dict, ctx) -> list[Item]:
    data = http.fetch_json("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50&desktop=true",
                           headers={"User-Agent": DESKTOP_UA, "Referer": "https://www.zhihu.com/hot",
                                    "x-api-version": "3.0.91"}, retries=1)
    rows = data.get("data") or []
    if not rows:
        raise ParseError("知乎热榜没有返回内容（可能需要登录态）", json.dumps(data, ensure_ascii=False))
    out = []
    for i, r in enumerate(rows, 1):
        t = r.get("target") or {}
        title = strip_html(t.get("title") or "")
        if not title:
            continue
        qid = re.search(r"/questions?/(\d+)", t.get("url") or "")
        url = f"https://www.zhihu.com/question/{qid.group(1)}" if qid else "https://www.zhihu.com/hot"
        heat = strip_html(r.get("detail_text") or "")
        out.append(Item(id=item_id(url, title), title=title, url=url, source=cfg["id"], source_name=cfg["name"],
                        region="cn", kind="trend", rank=i, desc=trunc(strip_html(t.get("excerpt") or ""), 300),
                        heat_text=f"知乎热榜第{i}" + (f"·{heat}" if heat else "")))
        if len(out) >= cfg.get("limit", 15):
            break
    return out


# ───────────────────────── TikTok 创作者中心（公开趋势） ─────────────────────────
def fetch_tiktok_trends(cfg: dict, ctx) -> list[Item]:
    country, period, n = cfg.get("country", "US"), cfg.get("period", 7), cfg.get("limit", 8)
    url = ("https://ads.tiktok.com/creative_radar_api/v1/popular_trend/hashtag/list"
           f"?page=1&limit={max(n, 10)}&period={period}&country_code={country}&sort_by=popular")
    data = http.fetch_json(url, headers={"User-Agent": DESKTOP_UA,
                                         "Referer": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en"},
                           retries=1)
    if data.get("code") not in (0, "0", None):
        raise ParseError(f"TikTok 返回错误码 {data.get('code')}（可能需要签名）", json.dumps(data, ensure_ascii=False))
    rows = (data.get("data") or {}).get("list") or []
    if not rows:
        raise ParseError("TikTok 没有返回话题标签（接口可能改版）", json.dumps(data, ensure_ascii=False))
    out = []
    for i, r in enumerate(rows[:n], 1):
        name = (r.get("hashtag_name") or "").strip().lstrip("#")
        if not name:
            continue
        views = int(r.get("video_views") or 0)
        posts = int(r.get("publish_cnt") or 0)
        it = Item(id=item_id(f"https://www.tiktok.com/tag/{name}", name), title=f"#{name}", url=f"https://www.tiktok.com/tag/{quote(name)}",
                  source=cfg["id"], source_name=cfg["name"], region="intl", kind="trend", rank=int(r.get("rank") or i),
                  desc=f"TikTok 创作者中心近 {period} 天{country}地区热门话题标签。", metrics={"views": views, "posts": posts},
                  heat_text=f"TikTok热门标签第{i}" + (f"·播放{fmt_num(views)}" if views else ""))
        out.append(it)
    return out


# ───────────────────────── Reddit（第三方托管接口，需要免费密钥） ─────────────────────────
def fetch_reddit_managed(cfg: dict, ctx) -> list[Item]:
    key = ctx.env.get("CRAWLORA_API_KEY", "")
    if not key:
        raise Skipped("未设置 CRAWLORA_API_KEY（Reddit 在云服务器上无免费通道，见 README）")
    sub, n = cfg["subreddit"], cfg.get("limit", 10)
    data = http.fetch_json(f"https://api.crawlora.net/api/v1/reddit/subreddit/{sub}/posts?sort={cfg.get('sort', 'hot')}&limit={n}",
                           headers={"x-api-key": key})
    posts = (data.get("data") or {}).get("posts") or []
    if not posts:
        raise ParseError("Reddit 托管接口没有返回帖子（字段可能变化）", json.dumps(data, ensure_ascii=False))
    out = []
    for i, p in enumerate(posts[:n], 1):
        title = strip_html(p.get("title") or "")
        link = p.get("permalink") or p.get("url") or ""
        if link.startswith("/"):
            link = "https://www.reddit.com" + link
        if not title or not link.startswith("http"):
            continue
        score = int(p.get("score") or 0)
        com = int(p.get("num_comments") or p.get("comments") or p.get("comment_count") or 0)
        it = Item(id=item_id(link, title), title=title, url=link, source=cfg["id"], source_name=cfg["name"],
                  region="intl", kind="community", rank=i, desc=trunc(strip_html(p.get("selftext") or p.get("text") or ""), 500),
                  metrics={"score": score, "comments": com}, heat_text=f"Reddit {score}分·{com}评论")
        out.append(it)
    return out


# ───────────────────────── Apify（通用第三方抓取接口，可选） ─────────────────────────
def fetch_apify(cfg: dict, ctx) -> list[Item]:
    """调用你在 Apify 商店里选好的抓取器。需要 APIFY_TOKEN；按用量计费（有免费月度额度，以官网为准）。
    cfg: actor（如 user~name）、input（抓取器的输入 JSON）、map（字段映射：title/url/desc/rank/heat）。"""
    token = ctx.env.get("APIFY_TOKEN", "")
    if not token:
        raise Skipped("未设置 APIFY_TOKEN")
    data = http.post_json(f"https://api.apify.com/v2/acts/{cfg['actor']}/run-sync-get-dataset-items?token={token}",
                          cfg.get("input", {}), timeout=150, retries=0)
    if not isinstance(data, list) or not data:
        raise ParseError("Apify 抓取器没有返回数据", json.dumps(data, ensure_ascii=False))
    m = {"title": "title", "url": "url", "desc": "description", "rank": "rank", "heat": "heat", **(cfg.get("map") or {})}
    out = []
    for i, r in enumerate(data[: cfg.get("limit", 10)], 1):
        title = strip_html(str(r.get(m["title"]) or ""))
        url = str(r.get(m["url"]) or "")
        if not title or not url.startswith("http"):
            continue
        heat = r.get(m["heat"])
        out.append(Item(id=item_id(url, title), title=title, url=url, source=cfg["id"], source_name=cfg["name"],
                        region=cfg.get("region", "intl"), kind=cfg.get("kind", "trend"), rank=int(r.get(m["rank"]) or i),
                        desc=trunc(strip_html(str(r.get(m["desc"]) or "")), 400),
                        heat_text=f"{cfg['name']}第{i}" + (f"·{heat}" if heat else "")))
    return out
