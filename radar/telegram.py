from __future__ import annotations
import re
import time

from . import http


class TelegramError(Exception):
    def __init__(self, msg: str, definite: bool, status: int | None = None):
        super().__init__(msg)
        self.definite = definite      # True=Telegram 明确拒绝（没发出去）；False=结果不确定（超时/网络）
        self.status = status


class Telegram:
    def __init__(self, token: str, interval: float = 1.2):
        self.token, self.interval = token, interval
        self._last = 0.0

    def call(self, method: str, payload: dict, timeout: int = 40):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            data = http.post_json(url, payload, timeout=timeout, retries=0)
        except http.FetchError as e:
            if e.status is None:
                raise TelegramError("network:" + str(e), definite=False)
            desc = re.sub(r"\s+", " ", e.body or "")[:200]
            err = TelegramError(f"HTTP {e.status} {desc}", definite=True, status=e.status)
            err.body = e.body
            raise err
        if not data.get("ok"):
            raise TelegramError("not ok: " + str(data.get("description", ""))[:200], definite=True)
        return data["result"]

    def send(self, chat_id, text: str, silent: bool = False, html_mode: bool = True) -> int:
        wait = self.interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        payload = {"chat_id": chat_id, "text": text, "disable_notification": silent,
                   "link_preview_options": {"is_disabled": True}}
        if html_mode:
            payload["parse_mode"] = "HTML"
        try:
            res = self.call("sendMessage", payload)
        except TelegramError as e:
            if e.status == 429:                                  # 限流：按服务器给的秒数等一次
                m = re.search(r'"retry_after"\s*:\s*(\d+)', getattr(e, "body", "") or "")
                time.sleep(min(int(m.group(1)) if m else 5, 30) + 1)
                res = self.call("sendMessage", payload)
            elif e.status == 400 and html_mode and "parse" in str(e).lower():
                plain = re.sub(r"<[^>]+>", "", text)           # 排版解析失败：退回纯文本，保证内容送达
                return self.send(chat_id, plain, silent, html_mode=False)
            else:
                raise
        self._last = time.time()
        return res["message_id"]

    def get_updates(self, offset: int | None = None) -> list[dict]:
        payload = {"timeout": 0, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload)
