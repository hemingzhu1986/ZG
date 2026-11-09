"""文本工具：规范化 URL、分词、相似度、数字格式、UTF-16 长度。"""
from __future__ import annotations
import hashlib
import html
import math
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

CJK = re.compile(r"[\u4e00-\u9fff]")
_TRACK = re.compile(r"^(utm_|fbclid|gclid|igshid|mc_|ref$|ref_)", re.I)
_TAG = re.compile(r"<[^>]+>")
_STOP = set("""a an the of and or to in on at for with from by is are was were be been it its this that these those as
not no but if so than then there their they them he she his her we you your i my our us has have had do does did will would
can could should may might just about into over after before more most new says say said how what who why when where which
up out off all one two get gets got via per vs""".split())
_COMMON_CAPS = {"The", "This", "That", "These", "Those", "And", "But", "For", "With", "How", "What", "Why", "Who",
                "When", "Where", "After", "Before", "Over", "New", "Now", "Here", "Just", "Our", "Your", "His", "Her"}


def has_cjk(s: str) -> bool:
    return bool(CJK.search(s or ""))


def strip_html(s: str) -> str:
    s = html.unescape(_TAG.sub(" ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def canonical_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    p = urlsplit(u)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACK.match(k)]
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/") or "/"
    return urlunsplit(((p.scheme or "https").lower(), host, path, urlencode(q), ""))


def host_of(u: str) -> str:
    h = urlsplit(u or "").netloc.lower()
    return h[4:] if h.startswith("www.") else h


def item_id(url: str, title: str, salt: str = "") -> str:
    base = canonical_url(url) or (title or "").strip().lower()
    return hashlib.sha1((base + "|" + salt).encode("utf-8")).hexdigest()[:12]


def tokens(s: str) -> set[str]:
    s = (s or "").lower()
    out = {w for w in re.findall(r"[a-z0-9']{2,}", s) if w not in _STOP}
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def proper_nouns(s: str) -> set[str]:
    return {w for w in re.findall(r"\b[A-Z][a-z]{2,}\b", s or "") if w not in _COMMON_CAPS}


def similar(t1: str, t2: str, threshold: float = 0.55) -> bool:
    a, b = tokens(t1), tokens(t2)
    if jaccard(a, b) >= threshold:
        return True
    shared = proper_nouns(t1) & proper_nouns(t2)
    return len(shared) >= 2 and jaccard(a, b) >= 0.3


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def trunc(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def first_sentence(s: str, n: int = 80) -> str:
    s = strip_html(s)
    m = re.split(r"(?<=[。！？.!?])\s*", s, maxsplit=1)
    return trunc(m[0] if m and m[0] else s, n)


def leading_sentences(s: str, max_chars: int = 200) -> str:
    """取开头的若干完整句子，总长不超过 max_chars；第一句本身过长时才截断。"""
    s = strip_html(s)
    if len(s) <= max_chars:
        return s
    parts = re.findall(r".+?(?:[。！？]+|[.!?]+(?=\s|$)|$)", s)
    out = ""
    for p in parts:
        if len(out) + len(p) > max_chars:
            break
        out += p
    return out.strip() or trunc(s, max_chars)


def fmt_num(n: float) -> str:
    n = float(n)
    if n >= 1e8:
        return f"{n / 1e8:.1f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万".replace(".0万", "万")
    return f"{int(n)}"


def parse_traffic(s: str) -> int:
    """'200K+' / '2M+' / '50,000+' -> int"""
    s = (s or "").replace(",", "").replace("+", "").strip().upper()
    m = re.match(r"^([\d.]+)\s*([KMB]?)$", s)
    if not m:
        return 0
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2)]
    return int(float(m.group(1)) * mult)


def heat_log(n: float, ceiling: float = 7.0) -> float:
    return min(1.0, math.log10(max(n, 0) + 1) / ceiling)
