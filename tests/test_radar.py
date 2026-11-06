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
from radar.sources import apis, feeds, html_links
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
        self.assertEqual(items[0].section, "screen")

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
        items = [mk("Use my promo code for 20% off"), mk("Leaked nudes of star"), mk("Normal story")]
        kept, drops = P.clean(items, LEX)
        self.assertEqual([i.title for i in kept], ["Normal story"])
        self.assertEqual(drops, {"商业推广": 1, "露骨内容": 1})

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
        self.assertIsNone(schedule.due(self.at(2026, 11, 5, 9, 59), self.sch))
        self.assertEqual(schedule.due(self.at(2026, 11, 5, 10, 0), self.sch), "daily")
        self.assertEqual(schedule.due(self.at(2026, 11, 5, 13, 59), self.sch), "daily")
        self.assertIsNone(schedule.due(self.at(2026, 11, 5, 14, 0), self.sch))
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
        self.assertEqual(a.summary_zh, "这是一段中文描述。")

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
        for i, t in enumerate(["离婚背叛一", "离婚背叛二", "离婚背叛三", "职场面试裁员", "奶奶父母家庭", "失踪悬疑案件", "AI 算法机器人"]):
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
        self.assertEqual(keys, ["daily_intl#1", "daily_cn#1", "screen#1", "adult#1", "drama#1"])
        for p in ed.parts:
            self.assertLessEqual(utf16_len(p["text"]), 3800)
        self.assertEqual([p["chat_id"] for p in ed.parts][-1], "-200")      # 短剧发到独立群
        self.assertNotIn("Variety", json.dumps(config.load_sources(), ensure_ascii=False))   # 已移除 Variety


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
        self.assertEqual(len(ed.items), 12)                  # 三天重复的同一事件只留一条
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
            return GTRENDS
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
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        chats = [c for c, _ in self.FakeTG.sent]
        self.assertIn("-100", chats)
        self.assertIn("99", chats)                                  # 管理员报告
        texts = "\n".join(t for _, t in self.FakeTG.sent)
        self.assertIn("Patrick Stewart", texts)
        self.assertIn("成人平台行业观察", texts)
        self.assertNotIn("pornhub.com", texts.lower().replace("pornhub 扩大", ""))   # 成人站点链接被拦截
        self.assertEqual(n1, n2)                                    # 第二次触发没有任何新发送


if __name__ == "__main__":
    unittest.main(verbosity=2)
