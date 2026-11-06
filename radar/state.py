"""状态文件：原子写入的 JSON。账本防重发、历史用于去重与周回顾。"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("RADAR_ROOT", Path(__file__).resolve().parent.parent))
STATE = ROOT / "state"
ARCHIVE = ROOT / "archive"


def read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class Ledger:
    """发送账本：每条消息一个槽位，状态 pending / sent。pending 代表结果不确定，不会自动重发。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else STATE / "ledger.json"
        self.data = read_json(self.path, {})

    def get(self, date: str, key: str) -> dict:
        return self.data.get(date, {}).get(key, {})

    def set(self, date: str, key: str, **kw) -> None:
        self.data.setdefault(date, {}).setdefault(key, {}).update(kw)
        write_json(self.path, self.data)

    def mark(self, date: str, flag: str, value=True) -> None:
        self.data.setdefault(date, {})[flag] = value
        write_json(self.path, self.data)

    def flag(self, date: str, flag: str):
        return self.data.get(date, {}).get(flag)

    def prune(self, keep_days: int = 60, today: dt.date | None = None) -> None:
        today = today or dt.date.today()
        cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
        self.data = {d: v for d, v in self.data.items() if d >= cutoff}
        write_json(self.path, self.data)


class History:
    """每日入选条目的精简记录，用于跨天去重、周回顾与选题复盘。"""

    def __init__(self, root: Path | None = None):
        self.dir = Path(root) if root else STATE / "history"

    def save(self, date: str, items: list[dict], extra: dict | None = None) -> None:
        write_json(self.dir / f"{date}.json", {"date": date, "items": items, **(extra or {})})

    def load(self, date: str) -> dict:
        return read_json(self.dir / f"{date}.json", {"date": date, "items": []})

    def recent(self, today: str, days: int) -> list[dict]:
        base = dt.date.fromisoformat(today)
        out = []
        for i in range(1, days + 1):
            d = (base - dt.timedelta(days=i)).isoformat()
            rec = self.load(d)
            if rec.get("items"):
                out.append(rec)
        return out

    def window(self, today: str, days: int) -> list[dict]:
        """含当天，往前 days 天。"""
        base = dt.date.fromisoformat(today)
        out = []
        for i in range(0, days):
            d = (base - dt.timedelta(days=i)).isoformat()
            rec = self.load(d)
            if rec.get("items"):
                out.append(rec)
        return out
