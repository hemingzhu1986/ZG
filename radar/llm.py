"""可选的大模型层。没有配置就返回 None，整套流水线仍能运行（降级为机器翻译与规则模板）。
支持：gemini（免费档，官方 REST）、openai（任何 OpenAI 兼容接口，如 Groq / OpenRouter / 自建 Ollama）。
Gemini 的免费模型名和额度经常变：本模块会在你填的模型不可用时，自动从该密钥的可用列表里挑一个 Flash/Flash-Lite 模型。"""
from __future__ import annotations
import json
import re
import time

from . import http

# 不适合“写中文简介”的模型：Pro（免费档通常无额度）、语音/图像/视频生成、Gemma（不支持系统指令与 JSON 模式）等
BAD_MODEL_WORDS = ("pro", "tts", "image", "live", "embed", "gemma", "customtools", "audio", "robotics", "computer",
                   "nano", "banana", "imagen", "veo", "aqa", "learnlm", "vision", "thinking-exp")
PREFER = ("flash-lite-latest", "flash-latest", "flash-lite", "flash")


class LLMError(Exception):
    """大模型返回了 200，但内容不可用（被拦截、没有文本、输出被截断等）。"""


def extract_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    spots = [(t.find(o), o, c) for o, c in (("[", "]"), ("{", "}")) if t.find(o) != -1]
    for _, o, c in sorted(spots):
        i, j = t.find(o), t.rfind(c)
        if j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found")


def describe_error(e: Exception) -> str:
    """把异常变成人能看懂的一句话（含 Google 返回的原因），不含密钥。"""
    if isinstance(e, http.FetchError):
        msg = ""
        try:
            msg = json.loads(e.body or "{}").get("error", {}).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            msg = (e.body or "")[:160]
        return f"{e}" + (f"：{re.sub(r'\\s+', ' ', msg)[:220]}" if msg else "")
    return f"{type(e).__name__}: {e}"[:260]


def _rank(name: str) -> tuple:
    r = next((i for i, w in enumerate(PREFER) if w in name), 99)
    return (r, "preview" in name or "exp" in name, name)


class LLM:
    def __init__(self, provider: str = "none", key: str = "", model: str = "", base_url: str = "",
                 min_interval: float = 7.0, fallback: bool = True):
        self.provider = (provider or "none").lower()
        self.key, self.base_url = key.strip(), base_url.strip()
        self.model = model.strip().removeprefix("models/")          # 兼容从控制台复制来的 models/xxx 写法
        self.min_interval, self._last = min_interval, 0.0
        self.fallback = fallback
        self.last_error = ""        # 最近一次失败的原因（给运行报告和 llm-test 显示）
        self.last_raw = ""          # 最近一次返回的原文片段（解析失败时用于诊断）
        self.notice = ""            # 例如“配置的模型不可用，已自动改用 xxx”
        self._models_cache: list[str] | None = None

    @classmethod
    def from_env(cls, env: dict) -> "LLM":
        return cls(env.get("LLM_PROVIDER", "none"), env.get("LLM_API_KEY", ""), env.get("LLM_MODEL", ""),
                   env.get("LLM_BASE_URL", ""))

    @property
    def available(self) -> bool:
        return self.provider in ("gemini", "openai") and bool(self.key) and bool(self.model)

    def list_models(self) -> list[str]:
        """列出这个密钥可用、支持生成文本的模型名（仅 gemini）。"""
        if self.provider != "gemini" or not self.key:
            return []
        if self._models_cache is None:
            data = http.fetch_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                   headers={"x-goog-api-key": self.key})
            self._models_cache = [m["name"].removeprefix("models/") for m in data.get("models", [])
                                  if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        return self._models_cache

    def candidate_models(self) -> list[str]:
        """适合写中文简介的模型：只要 Flash/Flash-Lite 文本模型，优先稳定别名（xxx-latest），其次非预览版。"""
        try:
            names = self.list_models()
        except Exception:
            return []
        ok = [n for n in names if "flash" in n and not any(b in n for b in BAD_MODEL_WORDS)]
        return sorted(ok, key=_rank)

    # ── 对外调用 ──
    def complete_json(self, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.3):
        if not self.available:
            raise RuntimeError("LLM not configured")
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            try:
                return self._try(system, user, max_tokens, temperature)
            except http.FetchError as first:
                self.last_error = describe_error(first)
                if self.provider != "gemini" or not self.fallback or first.status in (401, 403):
                    raise                                        # 密钥/权限问题，换模型没用
                original = self.model
                for m in [x for x in self.candidate_models() if x != original][:3]:
                    self.model = m
                    try:
                        res = self._try(system, user, max_tokens, temperature)
                        self.notice = f"你配置的模型 {original} 不可用（{self.last_error[:80]}），已自动改用 {m}；建议把 LLM_MODEL 改成 {m}"
                        return res
                    except http.FetchError as e2:
                        self.last_error = describe_error(e2)
                self.model = original
                raise
        finally:
            self._last = time.time()

    def _try(self, system: str, user: str, max_tokens: int, temperature: float):
        """一次调用：遇到免费档限流等 1 次重试；遇到“不支持系统指令/JSON 模式”（如 Gemma）改用兼容写法再试。"""
        try:
            return self._call(system, user, max_tokens, temperature, compat=False)
        except http.FetchError as e:
            body = (e.body or "").lower()
            if e.status == 429 and not re.search(r'limit"?\s*:\s*0\b', body):
                time.sleep(25)                                  # 触发每分钟限额：等一会儿再试一次
                return self._call(system, user, max_tokens, temperature, compat=False)
            if e.status == 400 and self.provider == "gemini":
                return self._call(system, user, max_tokens, temperature, compat=True)
            raise

    def _call(self, system: str, user: str, max_tokens: int, temperature: float, compat: bool):
        if self.provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            gen = {"temperature": temperature, "maxOutputTokens": max_tokens}
            if compat:                                           # 兼容写法：指令并入用户消息，不用系统指令与 JSON 模式
                body = {"contents": [{"role": "user", "parts": [{"text": system + "\n\n" + user}]}], "generationConfig": gen}
            else:
                gen["responseMimeType"] = "application/json"
                body = {"systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
            data = http.post_json(url, body, headers={"x-goog-api-key": self.key}, retries=1)
            cands = data.get("candidates") or []
            if not cands:
                raise LLMError("模型没有返回候选结果：" + json.dumps(data.get("promptFeedback") or data, ensure_ascii=False)[:200])
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text.strip():
                raise LLMError(f"模型没有返回文本（finishReason={cands[0].get('finishReason')}，可能输出额度被思考用完）")
            self.last_raw = text[:300]
            return extract_json(text)
        base = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        body = {"model": self.model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        data = http.post_json(base + "/chat/completions", body, headers={"Authorization": "Bearer " + self.key}, retries=1)
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise LLMError("模型没有返回文本")
        self.last_raw = text[:300]
        return extract_json(text)
