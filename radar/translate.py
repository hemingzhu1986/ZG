"""免费机器翻译兜底（没有大模型、或大模型漏译时使用）。
通道依次尝试：Google 非官方 gtx 接口 → MyMemory（匿名每天 5000 字；提供邮箱 MYMEMORY_EMAIL 后每天 5 万字，无需注册）。
每个通道连续失败 3 次就在本次运行里停用，避免被拦截时逐条等超时；长文本按句子分块；结果带缓存。"""
from __future__ import annotations
import json
from collections import Counter
from urllib.parse import urlencode

from . import http
from .state import STATE, read_json, write_json
from .text import chunk_text, has_cjk, needs_translation, _KANA, _HANGUL


class Translator:
    def __init__(self, enabled: bool = True, daily_cap: int = 0, cache_path=None, email: str = "",
                 providers: tuple = ("gtx", "mymemory")):
        self.enabled = enabled
        self.path = cache_path or STATE / "translations.json"
        self.cache: dict = read_json(self.path, {})
        self.email = (email or "").strip()
        self.providers = providers
        self.fails = Counter()
        self.off: set[str] = set()
        self.stats = Counter()
        self.mm_chars = 0
        self.mm_budget = 45_000 if self.email else 4_500
        self.dirty = False

    # ── 通道 ──
    def _gtx(self, text: str) -> str:
        url = "https://translate.googleapis.com/translate_a/single?" + urlencode(
            {"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
        data = json.loads(http.fetch(url, timeout=10, retries=1).decode("utf-8", "replace"))
        out = "".join(p[0] for p in data[0] if p and p[0]).strip()
        if not has_cjk(out):
            raise ValueError("no chinese in result")
        return out

    def _mymemory(self, text: str) -> str:
        if self.mm_chars + len(text) > self.mm_budget:
            raise ValueError("mymemory budget used up")
        src = "ja" if _KANA.search(text) else "ko" if _HANGUL.search(text) else "en"
        params = {"q": text, "langpair": f"{src}|zh-CN"}
        if self.email:
            params["de"] = self.email
        data = json.loads(http.fetch("https://api.mymemory.translated.net/get?" + urlencode(params),
                                     timeout=12, retries=1).decode("utf-8", "replace"))
        out = str((data.get("responseData") or {}).get("translatedText") or "").strip()
        if str(data.get("responseStatus")) != "200" or "MYMEMORY WARNING" in out.upper() or not has_cjk(out):
            raise ValueError("mymemory rejected")
        self.mm_chars += len(text)
        return out

    # ── 对外 ──
    def translate(self, text: str) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        if not needs_translation(text):
            return text
        if text in self.cache:
            self.stats["cached"] += 1
            return self.cache[text]
        if not self.enabled:
            return None
        outs = []
        for ch in chunk_text(text):
            got = None
            for name in self.providers:
                if name in self.off:
                    continue
                try:
                    got = getattr(self, "_" + name)(ch)
                    self.fails[name] = 0
                    self.stats[name] += 1
                    break
                except Exception:
                    self.fails[name] += 1
                    if self.fails[name] >= 3:
                        self.off.add(name)
            if not got:
                self.stats["failed"] += 1
                return None
            outs.append(got)
        res = "".join(outs)
        self.cache[text] = res
        self.dirty = True
        return res

    def save(self) -> None:
        if self.dirty:
            write_json(self.path, dict(list(self.cache.items())[-2500:]))
