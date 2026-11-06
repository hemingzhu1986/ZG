"""HTTP：带重试、超时与体积上限。所有网络访问都走这里，测试时可整体替换。"""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; ContentRadar/2.0; internal editorial tool)"


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


def fetch(url: str, headers: dict | None = None, timeout: int = 20, retries: int = 2,
          max_bytes: int = 3_000_000, data: bytes | None = None) -> bytes:
    h = {"User-Agent": UA, "Accept-Encoding": "identity", "Accept": "*/*"}
    h.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(max_bytes)
        except urllib.error.HTTPError as e:
            try:
                body = e.read(400).decode("utf-8", "replace")
            except Exception:
                body = ""
            last = FetchError(f"HTTP {e.code}", e.code, body)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = e.headers.get("Retry-After") if e.headers else None
                time.sleep(min(float(wait), 30) if wait and wait.isdigit() else 1.5 ** (attempt + 1))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = FetchError(type(e).__name__)
            if attempt < retries:
                time.sleep(1.5 ** (attempt + 1))
                continue
            raise last
    raise last or FetchError("unknown")


def fetch_json(url: str, **kw):
    try:
        return json.loads(fetch(url, **kw).decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise FetchError("InvalidJSON") from e


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 60, retries: int = 2):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    body = fetch(url, headers=h, timeout=timeout, retries=retries, data=json.dumps(payload).encode("utf-8"),
                 max_bytes=8_000_000)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise FetchError("InvalidJSON") from e
