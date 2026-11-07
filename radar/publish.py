"""发布：账本防重 + 结果不确定时不自动重发。"""
from __future__ import annotations
import datetime as dt

from .state import Ledger
from .telegram import Telegram, TelegramError


def publish(parts: list[dict], ledger: Ledger, tg: Telegram, date: str, log=print) -> dict:
    """parts: [{key, chat_id, text, silent}]。返回 {sent, skipped, failed, uncertain}。"""
    res = {"sent": [], "skipped": [], "failed": [], "uncertain": [], "errors": {}}
    for p in parts:
        key = p["key"]
        slot = ledger.get(date, key)
        if slot.get("status") == "sent":
            res["skipped"].append(key)
            continue
        if slot.get("status") == "pending":
            res["uncertain"].append(key)              # 上一轮发到一半崩溃：不知道是否已送达，交给人核实
            continue
        ledger.set(date, key, status="pending", ts=dt.datetime.now(dt.timezone.utc).isoformat())
        try:
            mid = tg.send(p["chat_id"], p["text"], silent=p.get("silent", False))
        except TelegramError as e:
            if e.definite:
                ledger.set(date, key, status="failed", error=str(e)[:200])
                res["failed"].append(key)
                res["errors"][key] = str(e)[:200]
                log(f"[publish] {key} 发送失败（明确被拒）：{e}")
            else:
                res["uncertain"].append(key)           # 保持 pending
                res["errors"][key] = str(e)[:200]
                log(f"[publish] {key} 结果不确定：{e}")
            continue
        ledger.set(date, key, status="sent", message_id=mid)
        res["sent"].append(key)
    return res
