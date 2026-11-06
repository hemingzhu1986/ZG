"""并发采集 + 来源隔离 + 备用源回退。任何一个源失败都只影响它自己。"""
from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor

from . import sources as S
from .http import FetchError
from .models import SourceResult


def _run_one(cfg: dict, ctx, reg: dict) -> SourceResult:
    res = SourceResult(id=cfg["id"], name=cfg["name"])
    t0 = time.time()
    try:
        fn = reg[cfg["type"]]
        items = fn(cfg, ctx)
        res.items, res.count = items, len(items)
        res.status = "ok" if items else "empty"
    except S.Skipped as e:
        res.status, res.error = "skipped", str(e)
    except S.Stale as e:
        res.status, res.error = "stale", str(e)
    except FetchError as e:
        res.status, res.error = "error", f"{e}"
    except Exception as e:                                   # 解析错误等，只记录类型，不泄露 URL/密钥
        res.status, res.error = "error", type(e).__name__
    res.secs = round(time.time() - t0, 1)
    return res


def collect(source_cfgs: list[dict], ctx, only: set[str] | None = None) -> list[SourceResult]:
    reg = S.registry()
    cfgs = [c for c in source_cfgs if c.get("enabled", True) and (not only or c["id"] in only)]
    primary = [c for c in cfgs if c.get("role") != "fallback"]
    fallback = [c for c in cfgs if c.get("role") == "fallback"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda c: _run_one(c, ctx, reg), primary))
    ok_groups = {c.get("group") for c, r in zip(primary, results) if r.status == "ok" and c.get("group")}
    need = [c for c in fallback if c.get("group") not in ok_groups]
    skipped = [c for c in fallback if c.get("group") in ok_groups]
    if need:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results += list(pool.map(lambda c: _run_one(c, ctx, reg), need))
    for c in skipped:
        results.append(SourceResult(id=c["id"], name=c["name"], status="skipped", error="主源正常，未启用备用源"))
    return results
