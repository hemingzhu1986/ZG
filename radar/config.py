from __future__ import annotations
import json
import os
from pathlib import Path

from .state import ROOT

CONFIG_DIR = ROOT / "config"


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings(path: Path | None = None) -> dict:
    p = Path(path) if path else CONFIG_DIR / "settings.json"
    s = json.loads(p.read_text(encoding="utf-8"))
    override = os.environ.get("RADAR_SETTINGS_JSON")          # 可用环境变量临时覆盖，如 {"targets":{"daily_total":20}}
    return _deep_merge(s, json.loads(override)) if override else s


def load_sources(path: Path | None = None) -> list[dict]:
    p = Path(path) if path else CONFIG_DIR / "sources.json"
    return json.loads(p.read_text(encoding="utf-8"))["sources"]


def load_lexicon() -> dict:
    return json.loads((CONFIG_DIR / "lexicon.json").read_text(encoding="utf-8"))


def load_prompt(name: str) -> str:
    return (CONFIG_DIR / "prompts" / name).read_text(encoding="utf-8")
