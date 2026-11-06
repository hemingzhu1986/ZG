from __future__ import annotations
from dataclasses import dataclass, field, asdict


@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str                 # 配置里的 source id
    source_name: str            # 展示名
    region: str = "intl"        # intl | cn
    kind: str = "news"          # trend | rank | news | community | chart | manual
    section: str = "daily"      # daily | screen | adult
    desc: str = ""
    rank: int | None = None
    metrics: dict = field(default_factory=dict)
    published: str = ""
    heat_text: str = ""         # 展示用热度文案，如“全站第15·176.1万播放”
    # —— 编辑加工结果 ——
    title_zh: str = ""
    summary_zh: str = ""
    tags: list = field(default_factory=list)
    drama_signal: str = ""
    # —— 评分与标记 ——
    score: float = 0.0
    also_in: list = field(default_factory=list)   # 其他同时出现的来源（跨源印证）
    flags: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Item":
        known = set(Item.__dataclass_fields__)
        return Item(**{k: v for k, v in d.items() if k in known})


@dataclass
class SourceResult:
    id: str
    name: str
    status: str = "ok"          # ok | empty | error | stale | skipped
    count: int = 0
    error: str = ""
    secs: float = 0.0
    items: list = field(default_factory=list)
