"""免费翻译兜底（仅在没有 LLM 时使用）。使用 Google 非官方的 gtx 接口：无密钥、无保障、可能随时失效，
因此带缓存与每日上限，失败时静默回退为英文原文。"""
from __future__ import annotations
import json
import re
from urllib.parse import urlencode

from . import http
from .state import STATE, read_json, write_json
from .text import has_cjk


class Translator:
    def __init__(self, enabled: bool = True, daily_cap: int = 60, cache_path=None):
        self.enabled, self.cap = enabled, daily_cap
        self.path = cache_path or STATE / "translations.json"
        self.cache: dict = read_json(self.path, {})
        self.used = 0
        self.fails = 0              # 连续失败次数；达到 3 次即本次运行不再尝试，避免被拦截时逐条等超时
        self.dirty = False

    def translate(self, text: str) -> str | None:
        text = (text or "").strip()
        if not text or has_cjk(text):
            return text or None
        if text in self.cache:
            return self.cache[text]
        if not self.enabled or self.used >= self.cap or self.fails >= 3:
            return None
        self.used += 1
        url = "https://translate.googleapis.com/translate_a/single?" + urlencode(
            {"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text[:400]})
        try:
            data = json.loads(http.fetch(url, timeout=10, retries=1).decode("utf-8", "replace"))
            out = "".join(p[0] for p in data[0] if p and p[0]).strip()
        except Exception:
            self.fails += 1
            return None
        self.fails = 0
        if not has_cjk(out):
            return None
        self.cache[text] = out
        self.dirty = True
        return out

    def save(self) -> None:
        if self.dirty:
            keep = dict(list(self.cache.items())[-1500:])
            write_json(self.path, keep)
