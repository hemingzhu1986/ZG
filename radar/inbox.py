"""人工投稿入口：管理员在与机器人的私聊里发 `/add 链接 备注`，下次日报会把它作为候选。
用于补足无法自动抓取的来源（Reddit、TikTok、X、小红书……）。只接受 ADMIN_USER_IDS（或 ADMIN_CHAT_ID）发来的指令。"""
from __future__ import annotations
import datetime as dt
import re

from . import http
from .models import Item
from .sources.html_links import _Meta
from .state import STATE, read_json, write_json
from .text import has_cjk, item_id, strip_html, trunc

URL = re.compile(r"https?://\S+")


def _page_meta(url: str) -> tuple[str, str]:
    try:
        mp = _Meta()
        mp.feed(http.fetch(url, timeout=12, retries=1, max_bytes=600_000).decode("utf-8", "replace"))
        return (strip_html(mp.meta.get("og:title") or mp.title), strip_html(mp.meta.get("og:description")
                or mp.meta.get("description") or ""))
    except Exception:
        return "", ""


def poll(tg, env: dict, now: dt.datetime, keep_days: int = 3, log=print) -> list[Item]:
    path_pending, path_off = STATE / "inbox.json", STATE / "tg_offset.json"
    pending = read_json(path_pending, [])
    admin_ids = {int(x) for x in env.get("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
    admin_chat = str(env.get("ADMIN_CHAT_ID", "")).strip()
    if tg is not None and (admin_ids or admin_chat):
        offset = read_json(path_off, {}).get("offset")
        try:
            updates = tg.get_updates(offset)
        except Exception as e:
            log(f"[inbox] 读取投稿失败：{type(e).__name__}")
            updates = []
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            uid, cid = (msg.get("from") or {}).get("id"), str((msg.get("chat") or {}).get("id", ""))
            ok = (uid in admin_ids) if admin_ids else (cid == admin_chat)
            if not (ok and text.startswith("/add")):
                continue
            m = URL.search(text)
            if not m:
                try:
                    tg.send(cid, "用法：/add 链接 备注（备注可写你想说的一句话）", silent=True, html_mode=False)
                except Exception:
                    pass
                continue
            url = m.group(0)
            note = trunc(text.replace("/add", "", 1).replace(url, "").strip(), 120)
            title, desc = _page_meta(url)
            title = note or title or url
            pending.append({"url": url, "title": title, "desc": desc or note, "added": now.isoformat()})
            try:
                tg.send(cid, "已收录，将进入下一次日报候选。", silent=True, html_mode=False)
            except Exception:
                pass
        if offset is not None:
            write_json(path_off, {"offset": offset})
    cutoff = now - dt.timedelta(days=keep_days)
    pending = [p for p in pending if dt.datetime.fromisoformat(p["added"]) >= cutoff]
    write_json(path_pending, pending)
    items = []
    for p in pending:
        it = Item(id=item_id(p["url"], p["title"]), title=p["title"], url=p["url"], source="manual",
                  source_name="人工投稿", region="cn" if has_cjk(p["title"]) else "intl", kind="manual",
                  desc=trunc(p["desc"], 400), published=p["added"], heat_text="团队投稿")
        it.flags.append("manual")
        items.append(it)
    return items
