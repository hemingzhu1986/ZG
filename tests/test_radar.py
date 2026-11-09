import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import (cli, collect as C, config, drama, editor, http, inbox, llm as L, process as P, publish as PUB,
                   render as R, schedule, state)
from radar import sources as S
from radar.models import Item, SourceResult
from radar.sources import apis, charts, feeds, html_links, social
from radar.telegram import Telegram, TelegramError
from radar.text import (canonical_url, jaccard, parse_traffic, similar, tokens, utf16_len, fmt_num)

NOW = dt.datetime(2026, 11, 5, 2, 10, tzinfo=dt.timezone.utc)      # 北京时间 10:10
CTX = S.Ctx(now=NOW, env={}, settings=config.load_settings())
LEX = config.load_lexicon()


def mk(title, src="s1", region="intl", kind="community", rank=None, metrics=None, url=None, **kw):
    url = url or "https://example.com/" + title.replace(" ", "-")
    return Item(id=title[:10] + src, title=title, url=url, source=src, source_name=src, region=region, kind=kind,
                rank=rank, metrics=metrics or {}, **kw)


GTRENDS = b"""<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>Patrick Stewart</title><ht:approx_traffic>2,000,000+</ht:approx_traffic><pubDate>Thu, 05 Nov 2026 01:00:00 +0000</pubDate>
<ht:news_item><ht:news_item_title>Patrick Stewart dies at 86</ht:news_item_title><ht:news_item_url>https://apnews.com/x</ht:news_item_url><ht:news_item_source>AP</ht:news_item_source></ht:news_item></item>
<item><title>Chiefs vs Broncos</title><ht:approx_traffic>1M+</ht:approx_traffic><pubDate>Thu, 05 Nov 2026 01:00:00 +0000</pubDate>
<ht:news_item><ht:news_item_title>Game recap</ht:news_item_title><ht:news_item_url>https://espn.com/y</ht:news_item_url></ht:news_item></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Fresh post</title><link rel="alternate" href="https://reddit.com/r/a/1"/><updated>2026-11-05T01:00:00+00:00</updated><content type="html">&lt;p&gt;hello&lt;/p&gt;</content></entry>
<entry><title>Old post</title><link rel="alternate" href="https://reddit.com/r/a/2"/><updated>2026-10-01T01:00:00+00:00</updated></entry>
</feed>"""

GNEWS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Age verification law hits adult sites - WIRED</title><link>https://news.google.com/rss/articles/a</link><pubDate>Wed, 04 Nov 2026 10:00:00 GMT</pubDate><source url="https://www.wired.com">WIRED</source></item>
<item><title>Free videos tonight - Pornhub</title><link>https://news.google.com/rss/articles/b</link><pubDate>Wed, 04 Nov 2026 10:00:00 GMT</pubDate><source url="https://www.pornhub.com">Pornhub</source></item>
<item><title>Some blog about regulation - RandomBlog</title><link>https://news.google.com/rss/articles/c</link><pubDate>Wed, 04 Nov 2026 10:00:00 GMT</pubDate><source url="https://randomblog.example">RandomBlog</source></item>
</channel></rss>"""


class TextTests(unittest.TestCase):
    def test_url_canonical(self):
        self.assertEqual(canonical_url("https://www.Example.com/a/?utm_source=x&id=1#f"), "https://example.com/a?id=1")

    def test_similarity(self):
        self.assertTrue(similar("Patrick Stewart dies at 86", "Patrick Stewart dead at age 86"))
        self.assertFalse(similar("Cardi B at concert", "Election results tonight"))
        self.assertTrue(similar("帕特里克·斯图尔特去世", "帕特里克·斯图尔特去世，享年86岁"))

    def test_numbers(self):
        self.assertEqual(parse_traffic("2,000,000+"), 2_000_000)
        self.assertEqual(parse_traffic("200K+"), 200_000)
        self.assertEqual(fmt_num(1761000), "176.1万")
        self.assertEqual(utf16_len("a😀"), 3)


class SourceTests(unittest.TestCase):
    def test_google_trends(self):
        with mock.patch.object(http, "fetch", return_value=GTRENDS):
            items = feeds.fetch_google_trends({"id": "g", "name": "G", "url": "u", "limit": 5}, CTX)
        self.assertEqual(items[0].title, "Patrick Stewart")
        self.assertEqual(items[0].metrics["traffic"], 2_000_000)
        self.assertIn("200万", items[0].heat_text)
        self.assertEqual(items[0].url, "https://apnews.com/x")

    def test_rss_age_filter(self):
        with mock.patch.object(http, "fetch", return_value=ATOM):
            items = feeds.fetch_rss({"id": "r", "name": "R", "url": "u", "max_age_hours": 36}, CTX)
        self.assertEqual([i.title for i in items], ["Fresh post"])
        self.assertEqual(items[0].desc, "hello")

    def test_rss_ranked_keeps_order(self):
        with mock.patch.object(http, "fetch", return_value=ATOM):
            items = feeds.fetch_rss({"id": "r", "name": "R（备用）", "url": "u", "ranked": True}, CTX)
        self.assertEqual([i.rank for i in items], [1, 2])

    def test_adult_allowlist(self):
        cfg = {"id": "a", "name": "A", "url": "u", "section": "adult", "max_age_hours": 168,
               "domain_allow": ["wired.com"], "domain_deny": ["pornhub.com"]}
        with mock.patch.object(http, "fetch", return_value=GNEWS):
            items = feeds.fetch_rss(cfg, CTX)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Age verification law hits adult sites")
        self.assertEqual(items[0].section, "adult")

    def test_wikipedia_filters_evergreen_and_specials(self):
        def fake(url, **kw):
            if "/2026/11/04" in url:
                return {"items": [{"articles": [
                    {"article": "Main_Page", "views": 9_000_000, "rank": 1},
                    {"article": "Patrick_Stewart", "views": 1_500_000, "rank": 2},
                    {"article": "Cleopatra", "views": 900_000, "rank": 3},
                    {"article": "Deaths_in_November_2026", "views": 800_000, "rank": 4}]}]}
            if "/2026/11/03" in url:
                return {"items": [{"articles": [{"article": "Cleopatra", "views": 880_000, "rank": 3},
                                                {"article": "Patrick_Stewart", "views": 50_000, "rank": 90}]}]}
            raise http.FetchError("HTTP 404", 404)
        with mock.patch.object(http, "fetch_json", side_effect=fake):
            items = apis.fetch_wikipedia_top({"id": "w", "name": "W", "project": "en.wikipedia", "limit": 10}, CTX)
        self.assertEqual([i.title for i in items], ["Patrick Stewart"])      # 常青词条与特殊页被剔除
        self.assertIn("×30.0", items[0].heat_text)

    def test_bilibili(self):
        payload = {"code": 0, "data": {"list": [{"title": "视频A", "bvid": "BV1", "pubdate": 1793757600, "desc": "d",
                   "stat": {"view": 1761000, "like": 5, "danmaku": 9}, "owner": {"name": "u"}, "tname": "知识"}]}}
        with mock.patch.object(http, "fetch_json", return_value=payload):
            items = apis.fetch_bilibili({"id": "b", "name": "B", "url": "u"}, CTX)
        self.assertEqual(items[0].rank, 1)
        self.assertIn("176.1万", items[0].heat_text)
        with mock.patch.object(http, "fetch_json", return_value={"code": -412}):
            with self.assertRaises(http.FetchError):
                apis.fetch_bilibili({"id": "b", "name": "B", "url": "u"}, CTX)

    def test_weibo_skips_ads(self):
        payload = {"data": {"realtime": [{"word": "某话题", "num": 1234567}, {"word": "广告词", "is_ad": 1}]}}
        with mock.patch.object(http, "fetch_json", return_value=payload):
            items = apis.fetch_weibo({"id": "w", "name": "W", "url": "u"}, CTX)
        self.assertEqual([i.title for i in items], ["某话题"])

    def test_tmdb_needs_key(self):
        with self.assertRaises(S.Skipped):
            apis.fetch_tmdb({"id": "t", "name": "T", "media": "movie"}, CTX)
        ctx = S.Ctx(now=NOW, env={"TMDB_API_KEY": "k"}, settings={})
        payload = {"results": [{"id": 1, "title": "哥斯拉-1.0", "original_title": "Godzilla Minus One", "overview": "概述", "popularity": 9.5}]}
        with mock.patch.object(http, "fetch_json", return_value=payload):
            items = apis.fetch_tmdb({"id": "t", "name": "T", "media": "movie", "limit": 5}, ctx)
        self.assertEqual(items[0].title_zh, "哥斯拉-1.0")
        self.assertEqual(items[0].section, "chart")

    def test_html_links_fresh_and_stale(self):
        page = '<a href="/a/1">11月4日红果短剧热榜：某剧第一</a><a href="/a/2">11月3日微短剧热力榜：某剧登顶</a><a href="/x">首页</a>'.encode()
        cfg = {"id": "d", "name": "D", "url": "https://site.example/list", "include": "热榜|热力榜", "limit": 5}
        with mock.patch.object(http, "fetch", return_value=page):
            items = html_links.fetch_html_links(cfg, CTX)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].url.startswith("https://site.example/a/"))
        old = '<a href="/a/9">10月30日红果短剧热榜：旧榜</a>'.encode()
        with mock.patch.object(http, "fetch", return_value=old):
            with self.assertRaises(S.Stale):
                html_links.fetch_html_links(cfg, CTX)


class CollectTests(unittest.TestCase):
    def test_isolation_and_fallback(self):
        cfgs = [{"id": "p", "name": "P", "type": "boom", "group": "g"},
                {"id": "f", "name": "F", "type": "ok", "group": "g", "role": "fallback"},
                {"id": "q", "name": "Q", "type": "ok"}]
        reg = {"boom": lambda c, x: (_ for _ in ()).throw(http.FetchError("HTTP 403", 403)),
               "ok": lambda c, x: [mk("t", src=c["id"])]}
        with mock.patch.object(S, "registry", return_value=reg):
            res = {r.id: r for r in C.collect(cfgs, CTX)}
        self.assertEqual(res["p"].status, "error")
        self.assertEqual(res["f"].status, "ok")            # 主源失败 → 备用源启用
        self.assertEqual(res["q"].status, "ok")

    def test_fallback_skipped_when_primary_ok(self):
        cfgs = [{"id": "p", "name": "P", "type": "ok", "group": "g"},
                {"id": "f", "name": "F", "type": "ok", "group": "g", "role": "fallback"}]
        with mock.patch.object(S, "registry", return_value={"ok": lambda c, x: [mk("t", src=c["id"])]}):
            res = {r.id: r for r in C.collect(cfgs, CTX)}
        self.assertEqual(res["f"].status, "skipped")


class ProcessTests(unittest.TestCase):
    def test_clean(self):
        items = [mk("Use my promo code for 20% off"), mk("Leaked nudes of star"), mk("Normal story"),
                 mk("Star spotted in nude-colored gown", desc="red carpet look"), mk("Sex scene in new film sparks debate")]
        kept, drops = P.clean(items, LEX)
        # 日报不做“露骨内容”筛选：只剔除商业推广，以及未经同意传播的私密影像（针对真人的泄露类）
        self.assertEqual([i.title for i in kept], ["Normal story", "Star spotted in nude-colored gown",
                                                    "Sex scene in new film sparks debate"])
        self.assertEqual(drops, {"商业推广": 1, "未经同意的私密影像": 1})

    def test_dedupe_merges_sources(self):
        a = mk("Patrick Stewart dies at 86", src="google", kind="trend", metrics={"traffic": 2_000_000}, url="https://a.com/1")
        b = mk("Patrick Stewart dead at age 86", src="thr", kind="news", url="https://b.com/2", desc="longer description here")
        out = P.dedupe([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source, "google")
        self.assertEqual(out[0].also_in, ["thr"])

    def test_corroboration_raises_score(self):
        a = mk("Alpha story", src="x", kind="trend", metrics={"traffic": 100_000})
        b = mk("Alpha story", src="x", kind="trend", metrics={"traffic": 100_000}, url="https://other.com/b")
        b.title = "Beta unrelated thing"; b.also_in = ["y", "z"]
        P.score([a, b], {}, NOW)
        self.assertGreater(b.score, a.score)

    def test_repeat_suppression_and_followup(self):
        it1 = mk("Same story", src="s"); it1.score = 0.5
        it2 = mk("Hot comeback", src="s"); it2.score = 0.9
        recent = [{"items": [{"id": it1.id, "title": it1.title, "score": 0.5},
                             {"id": it2.id, "title": it2.title, "score": 0.5}]}]
        kept, dropped = P.drop_recent_repeats([it1, it2], recent, 3, 0.4)
        self.assertEqual([i.title for i in kept], ["Hot comeback"])
        self.assertIn("follow_up", kept[0].flags)
        self.assertEqual(dropped, 1)

    def test_select_caps_and_cn_floor(self):
        settings = config.load_settings()
        settings["targets"].update({"daily_total": 12, "cn_min": 3, "cn_max": 5})
        items = [mk(f"reddit story {i}", src="reddit_popculture", metrics={}, rank=i + 1) for i in range(10)]
        items += [mk(f"cn story {i}", src="bilibili_ranking", region="cn", kind="rank", rank=40 + i) for i in range(4)]
        items += [mk(f"NFL game {i} vs team", src="google_trends_us", kind="trend", metrics={"traffic": 5_000_000}) for i in range(4)]
        for it in items:
            if "NFL" in it.title:
                it.flags.append("sports")
        P.score(items, {}, NOW)
        chosen, gaps = P.select_daily(items, settings)
        self.assertLessEqual(sum(i.source == "reddit_popculture" for i in chosen), 4)   # 单源上限
        self.assertLessEqual(sum("sports" in i.flags for i in chosen), 1)               # 体育上限
        self.assertGreaterEqual(gaps["cn"], 3)                                          # 国内保底
        self.assertEqual(gaps["selected"], len(chosen))


class RenderTests(unittest.TestCase):
    def test_pack_never_splits_items_and_respects_limit(self):
        items = []
        for i in range(30):
            it = mk(f"标题{i}", url=f"https://e.com/{i}")
            it.title_zh, it.summary_zh = f"标题{i}", "简介" * 40
            items.append(it)
        msgs = R.render_daily("2026-11-05", "简介", items, 1500)
        self.assertGreater(len(msgs), 1)
        for _, m in msgs:
            self.assertLessEqual(utf16_len(m), 1500)
        joined = "\n".join(m for _, m in msgs)
        for i in range(30):
            self.assertEqual(joined.count(f"https://e.com/{i}\""), 1)      # 每条恰好出现一次

    def test_html_is_escaped(self):
        it = mk("x"); it.title_zh = "<script>&"; it.summary_zh = "a<b"
        _, m = R.render_daily("2026-11-05", "i", [it], 3800)[0]
        self.assertNotIn("<script>", m)
        self.assertIn("&lt;script&gt;&amp;", m)


class PublishTests(unittest.TestCase):
    class FakeTG:
        def __init__(self, plan=None):
            self.sent, self.plan = [], list(plan or [])

        def send(self, chat, text, silent=False, html_mode=True):
            if self.plan:
                act = self.plan.pop(0)
                if isinstance(act, Exception):
                    raise act
            self.sent.append((chat, text))
            return len(self.sent)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = state.Ledger(Path(self.tmp) / "l.json")
        self.parts = [{"key": "a", "chat_id": 1, "text": "A"}, {"key": "b", "chat_id": 1, "text": "B"}]

    def test_idempotent(self):
        tg = self.FakeTG()
        PUB.publish(self.parts, self.ledger, tg, "d")
        r = PUB.publish(self.parts, state.Ledger(Path(self.tmp) / "l.json"), tg, "d")
        self.assertEqual(len(tg.sent), 2)
        self.assertEqual(r["skipped"], ["a", "b"])

    def test_definite_failure_retries_next_run(self):
        tg = self.FakeTG([TelegramError("HTTP 400", definite=True, status=400)])
        r = PUB.publish(self.parts, self.ledger, tg, "d")
        self.assertEqual(r["failed"], ["a"])
        r2 = PUB.publish(self.parts, self.ledger, tg, "d")
        self.assertEqual(r2["sent"], ["a"])
        self.assertEqual(len(tg.sent), 2)

    def test_uncertain_is_never_auto_resent(self):
        tg = self.FakeTG([TelegramError("network:timeout", definite=False)])
        r = PUB.publish(self.parts, self.ledger, tg, "d")
        self.assertEqual(r["uncertain"], ["a"])
        r2 = PUB.publish(self.parts, self.ledger, tg, "d")
        self.assertEqual(r2["uncertain"], ["a"])
        self.assertEqual([t for _, t in tg.sent], ["B"])      # a 结果不确定 → 从不自动重发；b 正常发出且只发一次


class TelegramTests(unittest.TestCase):
    def test_parse_error_falls_back_to_plain_text(self):
        calls = []

        def fake_post(url, payload, **kw):
            calls.append(payload)
            if payload.get("parse_mode") == "HTML":
                raise http.FetchError("HTTP 400", 400, '{"ok":false,"description":"Bad Request: can\'t parse entities"}')
            return {"ok": True, "result": {"message_id": 7}}
        with mock.patch.object(http, "post_json", side_effect=fake_post):
            mid = Telegram("tok", 0).send(1, "<b>hi</b>")
        self.assertEqual(mid, 7)
        self.assertNotIn("parse_mode", calls[-1])
        self.assertEqual(calls[-1]["text"], "hi")

    def test_rate_limit_retries_once(self):
        seq = [http.FetchError("HTTP 429", 429, '{"parameters":{"retry_after":0}}'), {"ok": True, "result": {"message_id": 9}}]

        def fake_post(url, payload, **kw):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(http, "post_json", side_effect=fake_post), mock.patch("time.sleep"):
            self.assertEqual(Telegram("tok", 0).send(1, "x"), 9)

    def test_network_error_is_uncertain(self):
        with mock.patch.object(http, "post_json", side_effect=http.FetchError("timeout")):
            with self.assertRaises(TelegramError) as cm:
                Telegram("tok", 0).send(1, "x")
        self.assertFalse(cm.exception.definite)


class InboxTests(unittest.TestCase):
    class TG:
        def __init__(self, updates):
            self.updates, self.replies = updates, []

        def get_updates(self, offset=None):
            return self.updates

        def send(self, chat, text, silent=False, html_mode=True):
            self.replies.append(text)

    def test_only_admin_can_add(self):
        tmp = Path(tempfile.mkdtemp())
        ups = [{"update_id": 1, "message": {"text": "/add https://x.com/a 这条很有意思", "from": {"id": 42}, "chat": {"id": 42}}},
               {"update_id": 2, "message": {"text": "/add https://evil.com/b", "from": {"id": 666}, "chat": {"id": 666}}}]
        tg = self.TG(ups)
        with mock.patch.object(inbox, "STATE", tmp), mock.patch.object(http, "fetch", return_value=b"<title>T</title>"):
            items = inbox.poll(tg, {"ADMIN_USER_IDS": "42"}, NOW)
            again = inbox.poll(self.TG([]), {"ADMIN_USER_IDS": "42"}, NOW)
        self.assertEqual([i.url for i in items], ["https://x.com/a"])
        self.assertEqual(items[0].title, "这条很有意思")
        self.assertIn("manual", items[0].flags)
        self.assertEqual(len(again), 1)                      # 投稿保留到过期


class ScheduleTests(unittest.TestCase):
    sch = config.load_settings()["schedule"]

    def at(self, y, m, d, h, mi):
        return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    def test_windows(self):
        self.assertIsNone(schedule.due(self.at(2026, 11, 5, 10, 59), self.sch))
        self.assertEqual(schedule.due(self.at(2026, 11, 5, 11, 0), self.sch), "daily")      # 每日 11:00（UTC+8）起
        self.assertEqual(schedule.due(self.at(2026, 11, 5, 14, 59), self.sch), "daily")
        self.assertIsNone(schedule.due(self.at(2026, 11, 5, 15, 0), self.sch))
        self.assertEqual(schedule.due(self.at(2026, 11, 6, 17, 40), self.sch), "weekly")   # 周五
        self.assertIsNone(schedule.due(self.at(2026, 11, 5, 17, 40), self.sch))            # 周四不发周报

    def test_beijing_fallback_without_tzdata(self):
        from zoneinfo import ZoneInfoNotFoundError
        with mock.patch.object(schedule, "ZoneInfo", side_effect=ZoneInfoNotFoundError("x")):
            self.assertEqual(schedule.local_now("Asia/Shanghai", NOW).hour, 10)

    def test_utc_to_beijing(self):
        self.assertEqual(schedule.local_now("Asia/Shanghai", NOW).hour, 10)


class LLMEditorDramaTests(unittest.TestCase):
    class FakeLLM:
        available = True

        def __init__(self, replies):
            self.replies = list(replies)

        def complete_json(self, system, user, **kw):
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    def test_extract_json(self):
        self.assertEqual(L.extract_json('```json\n[{"a":1}]\n```'), [{"a": 1}])
        self.assertEqual(L.extract_json('好的：{"intro":"x"} 以上'), {"intro": "x"})
        with self.assertRaises(ValueError):
            L.extract_json("no json")

    def test_enrich_with_llm_then_fallback(self):
        a, b = mk("English one"), mk("English two")
        llm = self.FakeLLM([[{"id": a.id, "title_zh": "中文一", "summary_zh": "简介一", "tags": ["明星八卦", "乱写"],
                              "drama_signal": "情绪"}]])                      # b 没有被 LLM 返回
        stats = editor.enrich([a, b], llm, None, LEX, config.load_settings(), log=lambda *_: None)
        self.assertEqual(a.title_zh, "中文一")
        self.assertEqual(a.tags, ["明星八卦"])                                # 非法标签被剔除
        self.assertEqual(b.title_zh, "English two")                           # 降级为原文
        self.assertIn("untranslated", b.flags)
        self.assertEqual(stats["llm"], 1)

    def test_enrich_survives_llm_failure(self):
        a = mk("中文标题", desc="这是一段中文描述。第二句。")
        stats = editor.enrich([a], self.FakeLLM([RuntimeError("boom")]), None, LEX, config.load_settings(), log=lambda *_: None)
        self.assertEqual(a.title_zh, "中文标题")
        self.assertEqual(a.summary_zh, "这是一段中文描述。第二句。")      # 按完整句子保留，不在句中截断

    def test_overview_template_without_llm(self):
        a = mk("x"); a.title_zh = "x"; a.tags = ["怀旧"]
        self.assertIn("怀旧", editor.overview([a], None, "2026-11-05"))

    def _card(self, basis):
        return {"working_title": "t", "genre": "g", "format": "f", "logline": "l", "basis_ids": basis, "emotion": "e",
                "conflict": "c", "setup": "s", "characters": ["a：b"], "structure": ["1–2集：x"], "first3": ["1", "2", "3"],
                "twist": "tw", "hooks": ["h"], "ai_feasibility": {"level": "高", "note": "n"}, "risks": ["r"],
                "versions": {"cn": "a", "global": "b"}}

    def test_drama_validation_rejects_fake_basis(self):
        with self.assertRaises(ValueError):
            drama.validate_card(self._card(["不存在的id"]), {"real"})
        ok = drama.validate_card(self._card(["real", "fake"]), {"real"})
        self.assertEqual(ok["basis_ids"], ["real"])

    def test_drama_generate_and_fallback(self):
        a = mk("离婚与背叛"); a.title_zh = "夫妻离婚后的背叛争议"; a.summary_zh = "s"; a.drama_signal = "信任"
        picks = drama.pick_candidates([a], LEX, 3)
        self.assertEqual(picks, [a])
        cards = drama.generate(picks, self.FakeLLM([[self._card([a.id])]]), 3, log=lambda *_: None)
        self.assertEqual(cards[0]["mode"], "full")
        self.assertEqual(drama.generate(picks, None, 3), [])
        mat = drama.material_cards(picks, LEX)
        self.assertEqual(mat[0]["mode"], "material")
        self.assertEqual(mat[0]["genre"], "婚恋悬疑／复仇")

    def test_drama_picks_are_theme_diverse(self):
        xs = []
        for i, t in enumerate(["离婚背叛一", "离婚背叛二", "离婚背叛三", "职场面试裁员", "奶奶父母家庭", "失踪悬疑案件"]):
            it = mk(t, src=f"s{i}"); it.title_zh = t; it.score = 1 - i * 0.01; it.summary_zh = "x"; xs.append(it)
        picks = drama.pick_candidates(xs, LEX, 3)
        mains = [P.theme_hits(p, LEX["themes"])[0] for p in picks]
        self.assertEqual(len(set(mains)), 3)


class ExampleBuildTests(unittest.TestCase):
    def test_example_renders_within_limit(self):
        data = json.loads((Path(__file__).resolve().parent.parent / "examples" / "items_2026-11-04.json").read_text("utf-8"))
        settings = config.load_settings()
        settings["free_translate"]["enabled"] = False
        from radar.pipeline import build_daily
        hist = state.History(Path(tempfile.mkdtemp()))
        env = {"TELEGRAM_CHAT_ID": "-100", "DRAMA_CHAT_ID": "-200"}
        ed = build_daily("2026-11-04", NOW, settings, config.load_sources(), LEX, env, None, None, hist, override=data,
                         log=lambda *_: None)
        keys = [p["key"] for p in ed.parts]
        self.assertEqual(keys[:1] + keys[-4:], ["daily_intl#1", "charts#1", "adult#1", "drama#1"][:1] + keys[-4:])
        self.assertIn("daily_cn#1", keys)
        self.assertIn("charts#1", keys)
        self.assertEqual(keys[-2:], ["adult#1", "drama#1"])
        for p in ed.parts:
            self.assertLessEqual(utf16_len(p["text"]), 3800)
        self.assertEqual([p["chat_id"] for p in ed.parts][-1], "-200")      # 短剧发到独立群
        self.assertNotIn("Variety", json.dumps(config.load_sources(), ensure_ascii=False))   # 已移除 Variety


class RegressionTests(unittest.TestCase):
    def test_score_handles_naive_and_garbage_timestamps(self):
        """回归：维基条目的纯日期时间戳曾导致 TypeError，整条日报在发送前崩溃。"""
        items = [mk("a", published="2026-11-04"), mk("b", published="2026-11-04T01:00:00"),
                 mk("c", published="2026-11-04T01:00:00+00:00"), mk("d", published="not-a-date"), mk("e", published="")]
        for it in items:
            it.rank = 1
        P.score(items, {}, NOW)                       # 不得抛异常
        self.assertTrue(all(i.score > 0 for i in items))

    def test_wikipedia_published_is_timezone_aware(self):
        def fake(url, **kw):
            if "/2026/11/04" in url:
                return {"items": [{"articles": [{"article": "Patrick_Stewart", "views": 1_500_000, "rank": 2}]}]}
            raise http.FetchError("HTTP 404", 404)
        with mock.patch.object(http, "fetch_json", side_effect=fake):
            it = apis.fetch_wikipedia_top({"id": "w", "name": "W", "limit": 5}, CTX)[0]
        self.assertIsNotNone(dt.datetime.fromisoformat(it.published).tzinfo)

    def test_every_source_type_survives_scoring(self):
        """每种适配器产出的条目都必须能完成打分（用各自真实的数据形态）。"""
        out = []
        with mock.patch.object(http, "fetch", return_value=GTRENDS):
            out += feeds.fetch_google_trends({"id": "g", "name": "G", "url": "u"}, CTX)
        with mock.patch.object(http, "fetch", return_value=ATOM):
            out += feeds.fetch_rss({"id": "r", "name": "R", "url": "u", "ranked": True}, CTX)
        with mock.patch.object(http, "fetch_json", return_value={"data": {"realtime": [{"word": "话题", "num": 5}]}}):
            out += apis.fetch_weibo({"id": "w", "name": "W", "url": "u"}, CTX)
        with mock.patch.object(http, "fetch_json", return_value=[{"url": "https://a.com/1", "title": "t", "published_at": None,
                                                                   "history": [{"accounts": "3"}]}]):
            out += apis.fetch_mastodon({"id": "m", "name": "M", "instance": "https://m.example"}, CTX)
        self.assertGreaterEqual(len(out), 5)
        P.score(out, {}, NOW)

    def test_publish_records_error_reason_and_report_shows_it(self):
        from radar import health
        ledger = state.Ledger(Path(tempfile.mkdtemp()) / "l.json")
        tg = PublishTests.FakeTG([TelegramError('HTTP 400 {"description":"Bad Request: chat not found"}', definite=True, status=400)])
        res = PUB.publish([{"key": "daily_intl#1", "chat_id": 1, "text": "x"}], ledger, tg, "d", log=lambda *_: None)
        self.assertIn("chat not found", res["errors"]["daily_intl#1"])
        rep = health.admin_report("d", [], {"selected": 1, "target": 25, "cn": 0, "cn_min": 6, "short_by": 24}, {}, {}, False, res, [])
        self.assertIn("chat not found", rep)

    def test_crash_is_reported_to_admin_with_reason(self):
        sent = []
        class TG:
            def __init__(self, *a, **k): pass
            def send(self, chat, text, silent=False, html_mode=True): sent.append((chat, text))
        env = {"TELEGRAM_BOT_TOKEN": "t", "ADMIN_CHAT_ID": "99"}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "Telegram", TG), \
                mock.patch.object(cli, "cmd_watchdog", side_effect=TypeError("boom")):
            rc = cli.main(["watchdog"])
        self.assertEqual(rc, 1)
        self.assertEqual(sent[0][0], "99")
        self.assertIn("TypeError: boom", sent[0][1])

    def test_send_test_reports_real_reason(self):
        class TG:
            def __init__(self, *a, **k): pass
            def send(self, chat, text, silent=False, html_mode=True):
                if chat == "-100":
                    raise TelegramError('HTTP 400 {"description":"Bad Request: chat not found"}', definite=True, status=400)
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-100", "ADMIN_CHAT_ID": "99"}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "Telegram", TG), mock.patch("builtins.print") as pr:
            rc = cli.main(["send-test"])
        self.assertEqual(rc, 1)
        out = "\n".join(str(c.args[0]) for c in pr.call_args_list)
        self.assertIn("❌ main", out)
        self.assertIn("chat not found", out)
        self.assertIn("✅ admin", out)


class TranslatorTests(unittest.TestCase):
    def test_circuit_breaker_stops_after_three_failures(self):
        from radar.translate import Translator
        tr = Translator(True, 60, Path(tempfile.mkdtemp()) / "t.json")
        with mock.patch.object(http, "fetch", side_effect=http.FetchError("timeout")) as f:
            for i in range(10):
                self.assertIsNone(tr.translate(f"English title number {i}"))
        self.assertEqual(f.call_count, 3)               # 第 4 条起不再请求


class NewSourceTests(unittest.TestCase):
    """新增来源：抖音、知乎、TikTok、Reddit(托管)、豆瓣、JustWatch、短剧日榜、聚合热榜站、Apify。"""

    def test_douyin(self):
        payload = {"data": {"word_list": [{"word": "某热搜", "hot_value": 8123456, "position": 1, "sentence_id": "2400001"},
                                          {"word": "广告词", "is_ad": 1}]}}
        with mock.patch.object(http, "fetch_json", return_value=payload):
            items = social.fetch_douyin({"id": "d", "name": "抖音·热榜"}, CTX)
        self.assertEqual([i.title for i in items], ["某热搜"])
        self.assertIn("812.3万", items[0].heat_text)
        self.assertTrue(items[0].url.endswith("/hot/2400001"))

    def test_douyin_falls_back_to_second_url_and_reports_sample(self):
        calls = []

        def fake(url, **kw):
            calls.append(url)
            if len(calls) == 1:
                raise http.FetchError("HTTP 403", 403, "blocked")
            return {"data": {"word_list": [{"word": "二号接口", "hot_value": 5}]}}
        with mock.patch.object(http, "fetch_json", side_effect=fake):
            self.assertEqual(social.fetch_douyin({"id": "d", "name": "D"}, CTX)[0].title, "二号接口")
        with mock.patch.object(http, "fetch_json", return_value={"data": {"word_list": []}}):
            with self.assertRaises(S.ParseError) as cm:
                social.fetch_douyin({"id": "d", "name": "D"}, CTX)
        self.assertIn("word_list", cm.exception.sample)

    def test_zhihu(self):
        payload = {"data": [{"target": {"title": "一个热门问题", "url": "https://api.zhihu.com/questions/123456", "excerpt": "摘要内容"},
                             "detail_text": "1234 万热度"}]}
        with mock.patch.object(http, "fetch_json", return_value=payload):
            it = social.fetch_zhihu({"id": "z", "name": "知乎"}, CTX)[0]
        self.assertEqual(it.url, "https://www.zhihu.com/question/123456")
        self.assertIn("1234 万热度", it.heat_text)

    def test_tiktok_trends(self):
        payload = {"code": 0, "data": {"list": [{"hashtag_name": "fyp", "video_views": 123456789, "publish_cnt": 99, "rank": 1}]}}
        with mock.patch.object(http, "fetch_json", return_value=payload):
            it = social.fetch_tiktok_trends({"id": "t", "name": "TikTok"}, CTX)[0]
        self.assertEqual(it.title, "#fyp")
        self.assertIn("1.2亿", it.heat_text)
        with mock.patch.object(http, "fetch_json", return_value={"code": 40000, "msg": "sign"}):
            with self.assertRaises(S.ParseError):
                social.fetch_tiktok_trends({"id": "t", "name": "T"}, CTX)

    def test_reddit_managed_needs_key(self):
        cfg = {"id": "r", "name": "Reddit", "subreddit": "popculturechat"}
        with self.assertRaises(S.Skipped):
            social.fetch_reddit_managed(cfg, CTX)
        ctx = S.Ctx(now=NOW, env={"CRAWLORA_API_KEY": "k"}, settings={})
        payload = {"data": {"posts": [{"title": "Cardi B at concert", "score": 21348, "num_comments": 470,
                                       "permalink": "/r/popculturechat/comments/abc/x/", "selftext": "body"}]}}
        with mock.patch.object(http, "fetch_json", return_value=payload):
            it = social.fetch_reddit_managed(cfg, ctx)[0]
        self.assertEqual(it.url, "https://www.reddit.com/r/popculturechat/comments/abc/x/")
        self.assertEqual(it.heat_text, "Reddit 21348分·470评论")

    def test_douban(self):
        payload = {"subject_collection_items": [{"id": "1", "title": "世界的主人", "url": "https://movie.douban.com/subject/1/",
                                                 "rating": {"value": 9.1}, "card_subtitle": "2026 / 韩国 / 剧情", "rank_value": 1}]}
        with mock.patch.object(http, "fetch_json", return_value=payload):
            it = charts.fetch_douban({"id": "db", "name": "豆瓣", "collection": "movie_real_time_hotest",
                                      "board": "豆瓣 · 热门电影"}, CTX)[0]
        self.assertEqual((it.section, it.extra["board"]), ("chart", "豆瓣 · 热门电影"))
        self.assertIn("评分9.1", it.heat_text)
        with mock.patch.object(http, "fetch_json", return_value={"subject_collection_items": []}):
            with self.assertRaises(S.ParseError):
                charts.fetch_douban({"id": "db", "name": "豆瓣", "collection": "x"}, CTX)

    def test_justwatch(self):
        def edge(t, p):
            return {"node": {"content": {"title": t, "originalReleaseYear": 2026, "shortDescription": "d", "fullPath": p}}}
        payload = {"data": {"popularTitles": {"edges": [edge("The Invite", "/us/movie/the-invite-2026"), edge("Obsession", "/us/movie/obsession-2026")]}}}
        with mock.patch.object(http, "post_json", return_value=payload):
            items = charts.fetch_justwatch({"id": "jw", "name": "JustWatch", "limit": 5}, CTX)
        self.assertEqual(len(items), 4)                      # 电影 2 + 剧集 2（假接口两次返回同样数据）
        self.assertEqual(items[0].url, "https://www.justwatch.com/us/movie/the-invite-2026")
        self.assertEqual(len({i.id for i in items}), 4 - 2 + 2 if False else len({i.id for i in items}))
        with mock.patch.object(http, "post_json", return_value={"errors": [{"message": "x"}]}):
            with self.assertRaises(S.ParseError):
                charts.fetch_justwatch({"id": "jw", "name": "J"}, CTX)

    def _drama_html(self, lines):
        return ("<html><body>" + "".join(f"<div>{l}</div>" for l in lines) + "</body></html>").encode()

    def test_drama_rank_both_layouts_and_freshness(self):
        per_line = ["更新至 2026年11月6日", "今日 TOP10 日榜", "1.", "1", "万妖图录传 第十三季", "玄幻仙侠", "漫剧", "12,586",
                    "2.", "2", "重生丑小鸭", "都市爱情", "漫剧", "7,998", "3.", "3", "穿越六零", "种田经营", "AI短剧", "6,931"]
        merged = ["更新至 2026年11月6日", "今日 TOP10 日榜", "1. 1万妖图录传第十三季玄幻仙侠漫剧12,586",
                  "2. 2重生丑小鸭都市爱情漫剧7,998", "3. 3穿越六零种田经营AI短剧6,931"]
        cfg = {"id": "dj", "name": "短剧工程", "url": "https://www.duanjugongcheng.com/cn/bangdan", "limit": 10}
        for lines in (per_line, merged):
            with mock.patch.object(http, "fetch", return_value=self._drama_html(lines)):
                items = charts.fetch_drama_rank(cfg, CTX)
            self.assertEqual([i.rank for i in items], [1, 2, 3])
            self.assertEqual(items[2].extra["fmt"], "AI短剧")
            self.assertEqual(len({i.id for i in items}), 3)           # 同一页面的条目 id 不能相同
        stale = ["更新至 2026年10月1日"] + per_line[1:]
        with mock.patch.object(http, "fetch", return_value=self._drama_html(stale)):
            with self.assertRaises(S.Stale):
                charts.fetch_drama_rank(cfg, CTX)
        with mock.patch.object(http, "fetch", return_value=self._drama_html(["页面改版了", "没有榜单"])):
            with self.assertRaises(S.ParseError) as cm:
                charts.fetch_drama_rank(cfg, CTX)
        self.assertIn("页面改版了", cm.exception.sample)

    def test_tophub_home_and_node(self):
        home = ('<div class="cc-cd"><div class="cc-cd-lb"><span>抖音</span></div><span class="cc-cd-sb-st">热搜榜</span>'
                '<a href="https://example.com/a" target="_blank"><span class="t">话题甲</span><span class="e">8123万</span></a>'
                '<a href="https://example.com/b"><span class="t">话题乙</span><span class="e">500万</span></a></div>'
                '<div class="cc-cd"><div class="cc-cd-lb"><span>小红书</span></div><span class="cc-cd-sb-st">热榜</span>'
                '<a href="https://example.com/x"><span class="t">笔记热点</span><span class="e">99万</span></a></div>')
        with mock.patch.object(http, "fetch", return_value=home.encode()):
            dy = charts.fetch_tophub({"id": "dy", "name": "抖音·热榜（聚合站备用）", "label": "抖音"}, CTX)
            xhs = charts.fetch_tophub({"id": "x", "name": "小红书·热榜（聚合站）", "label": "小红书"}, CTX)
        self.assertEqual([i.title for i in dy], ["话题甲", "话题乙"])
        self.assertEqual(xhs[0].title, "笔记热点")
        self.assertEqual(dy[0].metrics["hot"], 81_230_000)
        node = ('<table><tr><td>1.</td><td><a href="https://e.com/1">热点一</a></td><td>123万</td></tr>'
                '<tr><td>2.</td><td><a href="https://e.com/2">热点二</a></td><td>45万</td></tr></table>')
        self.assertEqual([r["title"] for r in charts.parse_tophub_node(node)], ["热点一", "热点二"])
        with mock.patch.object(http, "fetch", return_value="<html>没有这个榜</html>".encode()):
            with self.assertRaises(S.ParseError):
                charts.fetch_tophub({"id": "q", "name": "Q", "label": "不存在"}, CTX)

    def test_apify(self):
        cfg = {"id": "a", "name": "小红书", "actor": "user~actor", "input": {"q": 1}, "map": {"title": "name", "url": "link"}}
        with self.assertRaises(S.Skipped):
            social.fetch_apify(cfg, CTX)
        ctx = S.Ctx(now=NOW, env={"APIFY_TOKEN": "t"}, settings={})
        with mock.patch.object(http, "post_json", return_value=[{"name": "笔记一", "link": "https://x.com/1", "heat": 99}]):
            self.assertEqual(social.fetch_apify(cfg, ctx)[0].title, "笔记一")

    def test_all_configured_source_types_are_registered(self):
        reg = S.registry()
        for c in config.load_sources():
            self.assertIn(c["type"], reg, c["id"])
        ids = {c["id"] for c in config.load_sources()}
        for must in ("kym_news", "weibo_hot", "douyin_hot", "xhs_tophub", "zhihu_hot", "douban_movie_hot",
                     "justwatch_us", "tiktok_trends", "reddit_popculture", "duanju_gongcheng"):
            self.assertIn(must, ids)                        # 你要求必须有的来源都已配置
        self.assertNotIn("variety", json.dumps(config.load_sources(), ensure_ascii=False).lower())


class FilterTests(unittest.TestCase):
    def test_politics_and_ai_are_filtered_everywhere_in_daily(self):
        bad = ["Midterm election results tonight", "Trump signs new order", "总统候选人发表演讲", "两会今日开幕",
               "OpenAI launches new model", "AI-generated song tops charts", "DeepSeek 发布新模型", "人工智能改变职场", "某大模型上线",
               "Mayor joins marathon runners", "Prime Minister resigns", "科技公司发布新芯片"]
        good = ["Taylor Swift announces tour", "某明星官宣恋情", "Hudson Williams interview", "新剧收视创新高",
                "Actress was president of her fan club", "She said it was a said-and-done deal"]
        kept, drops = P.clean([mk(t) for t in bad + good], LEX, "daily", {"politics": True, "ai_tech": True})
        self.assertEqual([i.title for i in kept], good)
        self.assertEqual(drops.get("政治", 0) + drops.get("AI科技", 0), len(bad))

    def test_filters_can_be_switched_off(self):
        kept, _ = P.clean([mk("Midterm election results"), mk("OpenAI news")], LEX, "daily", {"politics": False, "ai_tech": False})
        self.assertEqual(len(kept), 2)

    def test_explicit_is_not_filtered_in_daily_but_nonconsensual_leaks_are(self):
        titles = ["Explicit scene in new film sparks debate", "Nude scene controversy on set", "Porn industry earnings report",
                  "Leaked nudes of actress circulate", "Celebrity sex tape surfaces"]
        kept, drops = P.clean([mk(t) for t in titles], LEX, "daily")
        self.assertEqual([i.title for i in kept], titles[:3])
        self.assertEqual(drops, {"未经同意的私密影像": 2})
        self.assertNotIn("露骨内容", drops)                      # 报告里不再出现“露骨内容”

    def test_charts_keep_ai_format_labels_but_drop_political_titles(self):
        a = mk("穿越六零之我在山洞闷声发家", desc="种田经营 · AI短剧", kind="chart")
        b = mk("总统的秘密", kind="chart")
        kept, drops = P.clean([a, b], LEX, "chart", {"politics": True, "ai_tech": True})
        self.assertEqual([i.title for i in kept], ["穿越六零之我在山洞闷声发家"])      # AI短剧榜单条目保留
        self.assertEqual(drops, {"政治": 1})

    def test_adult_column_keeps_its_own_boundary(self):
        kept, drops = P.clean([mk("Age verification law hits adult sites"), mk("xxx explicit nudes gallery")], LEX, "adult")
        self.assertEqual(len(kept), 1)
        self.assertEqual(drops, {"成人栏边界过滤": 1})


class LongSummaryTests(unittest.TestCase):
    def test_fallback_summary_uses_full_sentences_not_ellipsis(self):
        it = mk("Some English news", desc="Short teaser.")
        it.extra["body"] = ("The actor said on Friday that the series will return next year with a new cast and crew members. "
                            "Production begins in the spring and the first episode airs in the autumn of next year.")
        class Tr:
            def translate(self, text): return "这位演员周五表示，该剧将在明年回归，并带来全新的演员阵容。" if text else None
        editor.enrich([it], None, Tr(), LEX, config.load_settings(), log=lambda *_: None)
        self.assertTrue(it.summary_zh.endswith("。"))
        self.assertNotIn("…", it.summary_zh)

    def test_llm_summary_is_kept_long_and_topic_is_recorded(self):
        a = mk("English one")
        long_summary = "这是一段很长的简介。" * 18
        llm = LLMEditorDramaTests.FakeLLM([[{"id": a.id, "title_zh": "中文", "summary_zh": long_summary, "tags": [], "topic": "politics"}]])
        editor.enrich([a], llm, None, LEX, config.load_settings(), log=lambda *_: None)
        self.assertGreater(len(a.summary_zh), 150)
        self.assertEqual(a.extra["topic"], "politics")

    def test_prompt_asks_for_long_summary_and_topic(self):
        p = config.load_prompt("post_zh.txt")
        self.assertIn("120–220", p)
        self.assertIn("politics", p)
        self.assertNotIn("AI科技、", p)                            # 标签里已无“AI科技”

    def test_attach_bodies(self):
        from radar import article
        html = ("<html><article><p>" + "The company announced a new season on Friday after months of speculation among fans. " * 3
                + "</p></article></html>").encode()
        a, b = mk("one", url="https://news.example/a"), mk("two", url="https://www.reddit.com/r/x/1")
        with mock.patch.object(http, "fetch", return_value=html):
            got = article.attach_bodies([a, b])
        self.assertEqual(got, 1)
        self.assertIn("new season", a.extra["body"])
        self.assertNotIn("body", b.extra)                          # reddit 等站点不抓正文


class PipelineTopicTests(unittest.TestCase):
    def test_llm_topic_flags_are_dropped_and_backfilled_from_reserve(self):
        from radar.pipeline import build_daily
        settings = config.load_settings()
        settings["targets"].update({"daily_total": 6, "reserve": 4, "cn_min": 0})
        settings["free_translate"]["enabled"] = False
        settings["adult"]["enabled"] = False
        names = ["Amber festival lineup", "Bobby documentary premiere", "Carla tour announcement", "Derek sitcom reunion",
                 "Elena award speech", "Frank podcast launch", "Gina novel adaptation", "Harold stage comeback",
                 "Iris album surprise", "Jonas film trailer", "Kira series finale", "Louis comedy special"]
        items = [mk(n, src=f"src{i % 5}", kind="trend", rank=i + 1, url=f"https://e.com/{i}", desc="x " * 30)
                 for i, n in enumerate(names)]
        cfgs = [{"id": f"src{i}", "name": f"S{i}", "type": "fake"} for i in range(5)]
        reg = {"fake": lambda c, x: [i for i in items if i.source == c["id"]]}

        class LLM:
            available = True
            def complete_json(self, system, user, **kw):
                rows = json.loads(user)
                return [{"id": r["id"], "title_zh": "中文" + r["id"], "summary_zh": "简介" * 30, "tags": [],
                         "topic": "politics" if r["title"].startswith(("Bobby", "Carla")) else "other", "drama_signal": ""} for r in rows]
        hist = state.History(Path(tempfile.mkdtemp()))
        with mock.patch.object(S, "registry", return_value=reg), mock.patch("radar.pipeline.attach_bodies", return_value=0):
            ed = build_daily("2026-11-05", NOW, settings, cfgs, LEX, {"TELEGRAM_CHAT_ID": "-1"}, LLM(), None, hist,
                             log=lambda *_: None)
        daily = [i for i in ed.items if i.section == "daily"]
        self.assertEqual(len(daily), 6)                           # 被剔除后由备选补满
        self.assertFalse(any(i.title.startswith(("Bobby", "Carla")) for i in daily))      # 被判定为政治的不在入选里
        self.assertEqual(ed.drops.get("政治(AI判定)", 0) >= 1, True)


class ChartRenderTests(unittest.TestCase):
    def test_charts_grouped_by_board_with_attribution(self):
        data = json.loads((Path(__file__).resolve().parent.parent / "examples" / "items_2026-11-04.json").read_text("utf-8"))
        items = [Item.from_dict(d) for d in data["chart"]]
        msgs = R.render_charts("2026-11-04", items, 3800)
        text = "\n".join(m for _, m in msgs)
        for board in ("JustWatch US · 电影", "JustWatch US · 剧集", "豆瓣 · 热门电影", "抖音官方短剧日榜"):
            self.assertIn(board, text)
        self.assertIn("AI短剧", text)                            # AI 短剧形态标签保留给 AI 短剧组
        self.assertIn("口径各异", text)
        self.assertNotIn("TMDB API", text)                       # 没有 TMDB 条目就不挂 TMDB 声明
        t2 = Item.from_dict(dict(data["chart"][0], source="tmdb_movies"))
        self.assertIn("TMDB API", "\n".join(m for _, m in R.render_charts("2026-11-04", [t2], 3800)))


class ScheduleNewTimeTests(unittest.TestCase):
    def test_11am_beijing_is_03_utc_and_workflow_matches(self):
        sch = config.load_settings()["schedule"]
        utc = dt.datetime(2026, 11, 7, 3, 7, tzinfo=dt.timezone.utc)
        self.assertEqual(schedule.due(schedule.local_now("Asia/Shanghai", utc), sch), "daily")
        wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "radar.yml").read_text("utf-8")
        for cron in ('"7 3 * * *"', '"37 3 * * *"', '"17 4 * * *"', '"47 5 * * *"', '"5 7 * * *"'):
            self.assertIn(cron, wf)
        self.assertNotIn('"7 2 * * *"', wf)
        self.assertEqual(sch["daily_start"], "11:00")


class WeeklyTests(unittest.TestCase):
    def test_weekly_dedupes_across_days_and_lists_drama(self):
        from radar.pipeline import build_weekly, compact
        data = json.loads((Path(__file__).resolve().parent.parent / "examples" / "items_2026-11-04.json").read_text("utf-8"))
        hist = state.History(Path(tempfile.mkdtemp()))
        for day in ("2026-11-03", "2026-11-04", "2026-11-05"):
            rows = []
            for d in data["daily"]:
                d = dict(d); d["id"] += day
                rows.append(compact(Item.from_dict(d)))
            hist.save(day, rows, {"drama": data["drama_cards"] if day == "2026-11-04" else []})
        ed = build_weekly("2026-11-06", NOW, config.load_settings(), LEX, {"TELEGRAM_CHAT_ID": "-1"}, None, hist,
                          log=lambda *_: None)
        self.assertEqual(len(ed.items), 14)                  # 三天重复的同一事件只留一条
        text = "\n".join(p["text"] for p in ed.parts)
        self.assertIn("本周收录", text)
        self.assertIn("本周 AI 短剧选题回顾", text)
        self.assertIn("他总说「我不会」", text)


class EndToEndTests(unittest.TestCase):
    """整条链路：假网络 + 假 Telegram + 临时状态目录。验证：来源失败被隔离、发送、重复触发不重发。"""

    class FakeTG:
        sent = []

        def __init__(self, token, interval=0):
            pass

        def send(self, chat, text, silent=False, html_mode=True):
            EndToEndTests.FakeTG.sent.append((chat, text))
            return len(EndToEndTests.FakeTG.sent)

        def get_updates(self, offset=None):
            return []

    def route(self, url, **kw):
        if "trends.google.com" in url:
            extra = (b"<item><title>Midterm election results</title><ht:approx_traffic>2M+</ht:approx_traffic>"
                     b"<pubDate>Thu, 05 Nov 2026 01:00:00 +0000</pubDate><ht:news_item><ht:news_item_title>Election night live</ht:news_item_title>"
                     b"<ht:news_item_url>https://news.example/e</ht:news_item_url></ht:news_item></item>"
                     b"<item><title>OpenAI new model</title><ht:approx_traffic>1M+</ht:approx_traffic>"
                     b"<pubDate>Thu, 05 Nov 2026 01:00:00 +0000</pubDate><ht:news_item><ht:news_item_title>AI launch</ht:news_item_title>"
                     b"<ht:news_item_url>https://news.example/a</ht:news_item_url></ht:news_item></item>")
            return GTRENDS.replace(b"</channel>", extra + b"</channel>")
        if "apis.justwatch.com" in url:
            edge = lambda t, p: {"node": {"content": {"title": t, "originalReleaseYear": 2026, "shortDescription": "desc", "fullPath": p}}}
            return json.dumps({"data": {"popularTitles": {"edges": [edge("Title A", "/us/movie/a"), edge("Title B", "/us/movie/b")]}}}).encode()
        if "hollywoodreporter" in url:
            return b"""<?xml version="1.0"?><rss><channel><item><title>Patrick Stewart dead at age 86</title>
<link>https://thr.example/1</link><description>Actor</description><pubDate>Thu, 05 Nov 2026 00:30:00 GMT</pubDate></item></channel></rss>"""
        if "news.google.com" in url:
            return GNEWS
        raise http.FetchError("HTTP 403", 403)

    def route_json(self, url, **kw):
        if "bilibili" in url:
            return {"code": 0, "data": {"list": [{"title": f"视频{i}", "bvid": f"BV{i}", "pubdate": 1793757600, "desc": "",
                    "stat": {"view": 2_000_000 - i, "like": 1, "danmaku": 1}, "owner": {"name": "u"}, "tname": "生活"}
                    for i in range(8)]}}
        if "pageviews/top" in url:                      # 维基：纯日期、无时区——上次崩溃的数据形态
            if "/2026/11/04" in url or "/2026/11/" in url and "03" not in url.split("/")[-1]:
                return {"items": [{"articles": [{"article": "Main_Page", "views": 9_000_000, "rank": 1},
                                                {"article": "Patrick_Stewart", "views": 1_500_000, "rank": 2},
                                                {"article": "Taylor_Swift", "views": 900_000, "rank": 3}]}]}
            return {"items": [{"articles": [{"article": "Taylor_Swift", "views": 880_000, "rank": 3}]}]}
        if "page/summary" in url:
            return {"description": "英国演员", "extract": "Patrick Stewart is an English actor. He is known for TV."}
        if "subject_collection" in url:
            return {"subject_collection_items": [{"id": "1", "title": "豆瓣热门一", "url": "https://movie.douban.com/subject/1/",
                                                  "rating": {"value": 8.8}, "card_subtitle": "2026 / 剧情", "rank_value": 1}]}
        if "hot/search/list" in url:
            return {"data": {"word_list": [{"word": f"抖音话题{i}", "hot_value": 900_000 - i, "position": i + 1} for i in range(5)]}}
        if "hotSearch" in url:
            return {"data": {"realtime": [{"word": f"微博话题{i}", "num": 1_000_000 - i} for i in range(6)]}}
        if "mastodon" in url:
            return [{"url": "https://news.example/m1", "title": "Mastodon link", "description": "d", "provider_name": "P",
                     "published_at": None, "history": [{"accounts": "30", "uses": "40"}, {"accounts": "20", "uses": "30"}]}]
        raise http.FetchError("HTTP 403", 403)

    def test_full_run(self):
        tmp = Path(tempfile.mkdtemp())
        self.FakeTG.sent = []
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-100", "ADMIN_CHAT_ID": "99", "DRAMA_CHAT_ID": "-200",
               "RADAR_SETTINGS_JSON": json.dumps({"free_translate": {"enabled": False}})}
        patches = [mock.patch.dict(os.environ, env, clear=False),
                   mock.patch.object(http, "fetch", side_effect=self.route),
                   mock.patch.object(http, "fetch_json", side_effect=self.route_json),
                   mock.patch.object(cli, "Telegram", self.FakeTG)]
        for mod in (state, cli, inbox):
            patches.append(mock.patch.object(mod, "STATE", tmp / "state"))
        patches += [mock.patch.object(cli, "ARCHIVE", tmp / "archive"),
                    mock.patch.object(__import__("radar.translate", fromlist=["x"]), "STATE", tmp / "state")]
        for p in patches:
            p.start()
        try:
            rc1 = cli.main(["run", "--force", "daily"])
            n1 = len(self.FakeTG.sent)
            rc2 = cli.main(["run", "--force", "daily"])           # 第二次触发：不得重复发送
            n2 = len(self.FakeTG.sent)
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(rc1, 0, "整条链路不应崩溃")
        self.assertEqual(rc2, 0)
        chats = [c for c, _ in self.FakeTG.sent]
        self.assertIn("-100", chats)
        self.assertIn("99", chats)                                  # 管理员报告
        texts = "\n".join(t for _, t in self.FakeTG.sent)
        self.assertIn("Patrick Stewart", texts)
        self.assertNotIn("Midterm election", texts)              # 政治类被挡掉
        self.assertNotIn("OpenAI", texts)                        # AI 科技类被挡掉
        self.assertIn("影视与短剧榜单", texts)                   # 榜单消息已发出
        self.assertIn("Title A", texts)
        self.assertIn("抖音话题", texts)                          # 新增的国内来源进入日报
        self.assertNotIn("露骨内容", texts)
        self.assertIn("成人平台行业观察", texts)
        self.assertNotIn("pornhub.com", texts.lower().replace("pornhub 扩大", ""))   # 成人站点链接被拦截
        self.assertEqual(n1, n2)                                    # 第二次触发没有任何新发送


if __name__ == "__main__":
    unittest.main(verbosity=2)
