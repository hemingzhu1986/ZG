"""快捷导航：当天所有渠道消息发完后，发一条“导航”，每个渠道名是一个链接，点击跳到该渠道**最新一天**的那条消息；
并把这条导航置顶，频道顶部始终有一个固定入口。
原理：Telegram 支持 t.me/c/<频道编号>/<消息编号> 链接直达某条消息；机器人发送时已经拿到了每条消息的编号。"""
from __future__ import annotations
import datetime as dt
import html

from .render import esc, date_label
from .state import STATE, read_json, write_json
from .text import utf16_len

GROUPS = [("榜单", "📊"), ("国际", "🌍"), ("国内", "🇨🇳"), ("其他", "📌")]


def link_base(env: dict) -> str:
    """消息链接前缀。优先用公开频道用户名（TELEGRAM_CHANNEL_USERNAME 或 @用户名形式的 chat id）；
    否则用频道编号：-1001234567890 → https://t.me/c/1234567890"""
    user = (env.get("TELEGRAM_CHANNEL_USERNAME") or "").strip().lstrip("@")
    chat = str(env.get("TELEGRAM_CHAT_ID", "")).strip()
    if not user and chat.startswith("@"):
        user = chat.lstrip("@")
    if user:
        return f"https://t.me/{user}"
    if chat.startswith("-100"):
        return "https://t.me/c/" + chat[4:]
    return ""


def group_of(items_sections: set, region_cn: bool, channel: str) -> str:
    if channel == "团队投稿":
        return "其他"
    if items_sections == {"chart"}:
        return "榜单"
    if items_sections == {"adult"}:
        return "其他"
    return "国内" if region_cn else "国际"


def meta_from_config(label: str, cfgs: list[dict]) -> dict | None:
    """旧版冻结的消息没有导航元信息时，按渠道名从配置里推断（分组、图标、顺序）。"""
    from .channels import channel_of
    own = [c for c in cfgs if channel_of(c) == label]
    if not own:
        return None
    own.sort(key=lambda c: (c.get("role") == "fallback", c.get("channel_order", 500)))
    c = own[0]
    group = ("榜单" if c.get("section") == "chart" else "其他" if c.get("section") == "adult"
             else "国内" if c.get("region") == "cn" else "国际")
    return {"name": label, "emoji": c.get("emoji", "📰"), "order": c.get("channel_order", 500), "group": group}


def update_index(date: str, parts: list[dict], ledger, keep_days: int = 7, path=None) -> dict:
    """把今天已发送成功的渠道消息记入索引；没有发新消息的渠道保留它最近一次的记录。"""
    path = path or STATE / "nav.json"
    idx = read_json(path, {"channels": {}, "nav": {}})
    for p in parts:
        meta = p.get("nav")
        if not meta:
            continue
        slot = ledger.get(date, p["key"])
        if slot.get("status") != "sent" or not slot.get("message_id"):
            continue
        idx["channels"][meta["name"]] = {"emoji": meta["emoji"], "order": meta["order"], "group": meta["group"],
                                         "date": date, "message_id": slot["message_id"], "key": p["key"]}
    cutoff = (dt.date.fromisoformat(date) - dt.timedelta(days=keep_days)).isoformat()
    idx["channels"] = {n: v for n, v in idx["channels"].items() if v["date"] >= cutoff}
    write_json(path, idx)
    return idx


def render_nav(date: str, idx: dict, base: str, limit: int = 3800) -> str:
    chans = idx.get("channels", {})
    today = date
    md = lambda d: f"{int(d[5:7])}/{int(d[8:10])}"
    head = [f"<b>📍 快捷导航</b>｜{esc(date_label(date))}", "<i>点渠道名，直接跳到该渠道最新一条消息</i>"]
    todays = sorted((v for v in chans.values() if v["date"] == today), key=lambda v: v["message_id"])
    if todays:
        head.append(f'<a href="{base}/{todays[0]["message_id"]}">⏫ 回到今日第一条</a>')
    blocks = []
    for gname, gemoji in GROUPS:
        rows = sorted(((n, v) for n, v in chans.items() if v["group"] == gname), key=lambda x: (x[1]["order"], x[0]))
        if not rows:
            continue
        links = []
        for n, v in rows:
            tag = "" if v["date"] == today else f"（{md(v['date'])}）"
            links.append(f'<a href="{base}/{v["message_id"]}">{esc(v["emoji"])}{esc(n)}</a>{tag}')
        blocks.append(f"<b>{gemoji} {gname}</b>\n" + "　".join(links))
    text = "\n".join(head) + "\n\n" + "\n\n".join(blocks)
    return text if utf16_len(text) <= limit else text[: limit - 20] + "…"


def send_nav(tg, publish_fn, ledger, env: dict, settings: dict, date: str, parts: list[dict], log=print) -> dict:
    """发导航并置顶。返回 {"status": sent|skipped|failed|disabled|nolink, "pin": ok|failed|off, "note": ...}。"""
    cfg = settings.get("nav", {})
    out = {"status": "disabled", "pin": "off", "note": ""}
    if not cfg.get("enabled", True):
        return out
    base = link_base(env)
    chat = env.get("TELEGRAM_CHAT_ID")
    if not base or not chat:
        out.update(status="nolink", note="无法生成消息链接：TELEGRAM_CHAT_ID 需要是 -100 开头的频道编号，或设置 TELEGRAM_CHANNEL_USERNAME")
        return out
    if ledger.get(date, "nav").get("status") == "sent":
        out["status"] = "skipped"
        return out
    idx = update_index(date, parts, ledger, cfg.get("keep_days", 7))
    if not idx["channels"]:
        out["status"] = "nolink"
        return out
    text = render_nav(date, idx, base, settings["telegram"]["message_limit"])
    res = publish_fn([{"key": "nav", "chat_id": chat, "text": text, "silent": True}], ledger, tg, date, log=log)
    if "nav" not in res["sent"]:
        out["status"] = "failed"
        out["note"] = res["errors"].get("nav", "")
        return out
    out["status"] = "sent"
    mid = ledger.get(date, "nav").get("message_id")
    if cfg.get("pin", True) and mid:
        prev = (idx.get("nav") or {}).get("message_id")
        try:
            tg.pin(chat, mid)
            out["pin"] = "ok"
            if prev and prev != mid:
                try:
                    tg.unpin(chat, prev)                     # 取消昨天的置顶，只保留最新的导航
                except Exception:
                    pass
        except Exception as e:
            out["pin"] = "failed"
            out["note"] = ("导航已发送但未能置顶：机器人需要频道管理员权限里的“编辑其他人的消息（Edit messages of others）”。"
                           f"（{str(e)[:120]}）")
        idx["nav"] = {"message_id": mid, "date": date}
        write_json(STATE / "nav.json", idx)
    return out
