"""可选的大模型层。没有配置就返回 None，整套流水线仍能运行（降级为规则模板）。
支持：gemini（免费档，用官方 REST）、openai（任何 OpenAI 兼容接口，如 Groq / OpenRouter / 自建 Ollama）。
不在代码里写死模型名——免费档的可用模型和额度会变，请到服务商控制台选标注“免费”的模型填进 LLM_MODEL。"""
from __future__ import annotations
import json
import re

from . import http


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


class LLM:
    def __init__(self, provider: str = "none", key: str = "", model: str = "", base_url: str = ""):
        self.provider = (provider or "none").lower()
        self.key, self.model, self.base_url = key, model, base_url

    @classmethod
    def from_env(cls, env: dict) -> "LLM":
        return cls(env.get("LLM_PROVIDER", "none"), env.get("LLM_API_KEY", ""), env.get("LLM_MODEL", ""),
                   env.get("LLM_BASE_URL", ""))

    @property
    def available(self) -> bool:
        return self.provider in ("gemini", "openai") and bool(self.key) and bool(self.model)

    def complete_json(self, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.3):
        if not self.available:
            raise RuntimeError("LLM not configured")
        if self.provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens,
                                         "responseMimeType": "application/json"}}
            data = http.post_json(url, body, headers={"x-goog-api-key": self.key})
            parts = data["candidates"][0]["content"]["parts"]
            return extract_json("".join(p.get("text", "") for p in parts))
        base = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        body = {"model": self.model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        data = http.post_json(base + "/chat/completions", body, headers={"Authorization": "Bearer " + self.key})
        return extract_json(data["choices"][0]["message"]["content"])
