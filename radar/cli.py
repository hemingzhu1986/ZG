"""命令行。常用：
  python -m radar doctor                 检查所有数据源的真实状态（首次部署必跑）
  python -m radar build --out out        只生成不发送（预览）
  python -m radar run                    正式运行：到点才发，已发不重发
  python -m radar llm-test               检查翻译链路（大模型 + 机器翻译兜底）
  python -m radar run --force daily      忽略时间窗强制跑日报（测试频道用）
  python -m radar serve                  常驻调度（不依赖 cron / GitHub 定时任务；服务器上用）
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from . import config, health, inbox, schedule
from .llm import LLM
from .pipeline import build_daily, build_weekly, compact
from .publish import publish
from .sources import Ctx
from .collect import collect
from .state import ARCHIVE, STATE, History, Ledger, read_json, write_json
from .telegram import Telegram, TelegramError
from .translate import Translator


def _env() -> dict:
    return dict(os.environ)


def _notify_admin(text: str) -> None:
    """把具体错误私信给管理员；自身失败时静默，避免掩盖原始错误。"""
    env = _env()
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("ADMIN_CHAT_ID"):
        try:
            Telegram(env["TELEGRAM_BOT_TOKEN"], 0).send(env["ADMIN_CHAT_ID"].strip(), text, silent=False, html_mode=False)
        except Exception:
            pass


def cmd_doctor(a) -> int:
    settings, cfgs = config.load_settings(), config.load_sources()
    now = dt.datetime.now(dt.timezone.utc)
    only = set(a.only.split(",")) if a.only else None
    res = collect(cfgs, Ctx(now=now, env=_env(), settings=settings), only)
    un = lambda t: t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    print(un(health.source_table(res)))
    det = health.doctor_details(res)
    if det:
        print("\n── 详情（正常来源的示例条目；失败来源的响应片段）──\n" + un(det))
    ok = sum(r.status == "ok" for r in res)
    print(f"\n正常 {ok}/{len(res)}。experimental 的源失败属正常现象——以此处输出为准，把失败的源在 config/sources.json 里关掉或换 URL。")
    if a.json:
        write_json(STATE / "source_health.json", [{"id": r.id, "status": r.status, "count": r.count,
                                                    "error": r.error, "secs": r.secs} for r in res])
    return 0


def _write_out(ed, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    seen = set()
    for p in list(ed.parts) + list(ed.preview_parts):
        if p["key"] in seen:
            continue
        seen.add(p["key"])
        (out / f"{ed.date}-{p['key'].replace('#', '_')}.html.txt").write_text(p["text"], encoding="utf-8")
    write_json(out / f"{ed.date}-{ed.kind}-items.json", [compact(i) for i in ed.items])


def cmd_build(a) -> int:
    settings, cfgs, lex, env = config.load_settings(), config.load_sources(), config.load_lexicon(), _env()
    now = dt.datetime.now(dt.timezone.utc)
    date = a.date or schedule.local_now(settings["timezone"], now).date().isoformat()
    history = History()
    llm = None if a.no_llm else LLM.from_env(env)
    if llm:
        llm.min_interval = settings.get("llm", {}).get("min_interval", 5.0)
    override = json.loads(Path(a.from_items).read_text(encoding="utf-8")) if a.from_items else None
    if override:
        settings["free_translate"]["enabled"] = False       # 离线演示不联网
    tr = Translator(settings["free_translate"]["enabled"], email=env.get("MYMEMORY_EMAIL", ""))
    if a.kind == "weekly":
        ed = build_weekly(date, now, settings, lex, env, llm, history)
    else:
        ed = build_daily(date, now, settings, cfgs, lex, env, llm, tr, history, override=override)
    _write_out(ed, Path(a.out))
    print(f"已生成 {len(ed.parts) + len([p for p in ed.preview_parts if p['key'] not in {x['key'] for x in ed.parts}])} 条消息 → {a.out}/（未发送）")
    for p in list(ed.parts) + [p for p in ed.preview_parts if p['key'] not in {x['key'] for x in ed.parts}]:
        print(f"  {p.get('label') or p['key']}: {len(p['text'])} 字符")
    for n in ed.notes:
        print(n)
    return 0


def cmd_run(a) -> int:
    settings, cfgs, lex, env = config.load_settings(), config.load_sources(), config.load_lexicon(), _env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not env.get("TELEGRAM_CHAT_ID"):
        print("缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return 2
    now = dt.datetime.now(dt.timezone.utc)
    local = schedule.local_now(settings["timezone"], now)
    kind = a.force or schedule.due(local, settings["schedule"])
    if not kind:
        print(f"当前 {local:%H:%M}（北京时间）不在发送窗口，退出。")
        return 0
    date = local.date().isoformat()
    ledger, history = Ledger(), History()
    flag = f"_complete_{kind}"
    if ledger.flag(date, flag) and not a.force:
        print(f"{date} {kind} 已完成，退出。")
        return 0
    tg = Telegram(token, settings["telegram"]["send_interval_seconds"])
    admin = env.get("ADMIN_CHAT_ID", "")
    llm = LLM.from_env(env)
    llm.min_interval = settings.get("llm", {}).get("min_interval", 5.0)
    tr = Translator(settings["free_translate"]["enabled"], email=env.get("MYMEMORY_EMAIL", ""))

    # 冻结文本：同一天第二次触发只补发没发出的那几条，不会内容前后不一致
    staged_path = STATE / "staged" / f"{date}-{kind}.json"
    staged = read_json(staged_path, None)
    ed = None
    if staged is None:
        if kind == "daily":
            ed = build_daily(date, now, settings, cfgs, lex, env, llm, tr, history,
                             inbox_items=inbox.poll(tg, env, now))
        else:
            ed = build_weekly(date, now, settings, lex, env, llm, history)
        tr.save()
        write_json(staged_path, {"parts": ed.parts})
        parts = ed.parts
    else:
        parts = staged["parts"]
        print("使用已冻结的当日内容，仅补发未发送的消息。")

    # 目标聊天未设置的部分（如未配置短剧群）不发送
    sendable = [p for p in parts if p["chat_id"]]
    res = publish(sendable, ledger, tg, date)
    print(f"发送结果：成功 {len(res['sent'])}｜已发过 {len(res['skipped'])}｜失败 {len(res['failed'])}｜待核实 {len(res['uncertain'])}")

    if ed is not None:
        out = ARCHIVE / date
        _write_out(ed, out)
        history.save(date, [compact(i) for i in ed.items],
                     {"drama": ed.drama_cards, "kind": kind})
        write_json(out / "report.json", {"gaps": ed.gaps, "drops": ed.drops, "enrich": ed.enrich_stats,
                                         "sources": [{"id": r.id, "status": r.status, "count": r.count,
                                                      "error": r.error} for r in ed.results]})
    if not res["failed"] and not res["uncertain"]:
        ledger.mark(date, flag, True)
    if ed is not None and admin and (settings["admin"]["daily_summary"] or res["failed"] or res["uncertain"]):
        try:
            tg.send(admin, health.admin_report(date, ed.results, ed.gaps, ed.drops, ed.enrich_stats, llm.available,
                                               res, ed.notes, ed.channels, dict(tr.stats)), silent=True)
        except Exception as e:
            print(f"管理员报告发送失败：{type(e).__name__}")
    ledger.prune()
    return 1 if (res["failed"] or res["uncertain"]) else 0


def cmd_watchdog(a) -> int:
    """日报窗口过后仍未完成 → 通知管理员。建议每天 14:05（北京）跑一次。"""
    settings, env = config.load_settings(), _env()
    now = dt.datetime.now(dt.timezone.utc)
    local = schedule.local_now(settings["timezone"], now)
    date = local.date().isoformat()
    if schedule.past_deadline(local, settings["schedule"], "daily") and not Ledger().flag(date, "_complete_daily"):
        msg = f"⚠️ {date} 的每日精选在截止时间后仍未完成发送，请检查运行记录。"
        print(msg)
        if env.get("TELEGRAM_BOT_TOKEN") and env.get("ADMIN_CHAT_ID"):
            Telegram(env["TELEGRAM_BOT_TOKEN"]).send(env["ADMIN_CHAT_ID"], msg, html_mode=False)
        return 1
    print("日报状态正常。")
    return 0


def serve_loop(settings: dict, max_loops: int | None = None, sleep=None, now_fn=None, run_fn=None,
               watchdog_fn=None, notify_fn=None, log=print) -> None:
    """常驻调度：每 5 分钟醒来一次，到点就跑（账本保证只发一次）；出错后 15 分钟再试；
    日报窗口过后仍未完成，当天私信管理员一次。任何异常都不会让进程退出。"""
    import time as _time
    sleep = sleep or _time.sleep
    now_fn = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
    run_fn = run_fn or (lambda: cmd_run(argparse.Namespace(force=None)))
    watchdog_fn = watchdog_fn or (lambda: cmd_watchdog(argparse.Namespace()))
    notify_fn = notify_fn or _notify_admin
    last_watch, last_err_day, loops = None, None, 0
    while max_loops is None or loops < max_loops:
        loops += 1
        wait, day = 300, None
        try:
            now = now_fn()
            local = schedule.local_now(settings["timezone"], now)
            day = local.date().isoformat()
            if schedule.due(local, settings["schedule"]):
                rc = run_fn()
                if rc not in (0, None):
                    wait = 900                                   # 本轮有失败：15 分钟后重试
            elif schedule.past_deadline(local, settings["schedule"], "daily") and last_watch != day:
                last_watch = day
                watchdog_fn()                                    # 窗口已过：检查日报是否发完，没发完会私信管理员
        except Exception as e:                                   # 调度器自己永不退出
            wait = 900
            msg = f"{type(e).__name__}: {e}"[:300]
            log(f"[serve] 本轮出错：{msg}")
            try:
                if last_err_day != day:
                    last_err_day = day
                    notify_fn(f"⚠️ 雷达调度器出错（将自动重试）：{msg}")
            except Exception:
                pass
        if max_loops is None or loops < max_loops:
            sleep(wait)


def cmd_serve(a) -> int:
    """常驻运行：`python -m radar serve`。适合 Docker / systemd / 任何服务器；不需要 cron，也不依赖 GitHub。"""
    settings = config.load_settings()
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / "serve.lock", "w")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass
    except OSError:
        print("已有另一个 serve 进程在运行，本进程退出。")
        return 1
    print(f"织光内容雷达常驻调度已启动：每天 {settings['schedule']['daily_start']}（{settings['timezone']}）起发送。")
    serve_loop(settings, max_loops=a.max_loops)
    return 0


def cmd_llm_test(a) -> int:
    """检查翻译链路：1) 大模型配置是否可用（并列出可选模型）；2) 用样例外文条目试译；3) 免费机器翻译兜底是否可用。"""
    from . import editor
    from .models import Item
    env = _env()
    settings = config.load_settings()
    llm = LLM.from_env(env)
    llm.min_interval = 0
    ok = True
    print(f"大模型配置：provider={llm.provider or 'none'}｜model={llm.model or '（未填）'}｜密钥={'已设置' if llm.key else '未设置'}")
    if not llm.available:
        print("❌ 大模型未配置完整。请按 README 配置免费 Gemini：LLM_PROVIDER=gemini、LLM_API_KEY、LLM_MODEL。")
        ok = False
    else:
        try:
            models = llm.list_models()
            if models:
                print("该密钥可用的模型（请选一个标注“免费”的 Flash/Flash-Lite 填入 LLM_MODEL）：\n  " + "\n  ".join(models[:25]))
                if llm.model not in models:
                    print(f"⚠️ 你填的 LLM_MODEL={llm.model} 不在上面的列表里，可能填错或已下线。")
        except Exception as e:
            print(f"（模型列表获取失败：{type(e).__name__}，不影响后面的试译）")
        samples = [Item(id="t1", title="Hudson Williams says his bond with co-star Connor Storrie is platonic", url="https://example.com/1",
                        source="s", source_name="示例", desc="The Heated Rivalry actor said in an interview that he and his co-star are close friends, sparking a debate about how male affection is perceived."),
                   Item(id="t2", title="Fans react as Netflix cancels cult sci-fi series after two seasons", url="https://example.com/2",
                        source="s", source_name="示例", desc="Viewers expressed disappointment online after the streamer confirmed the show would not return.")]
        n = editor._llm_pass(samples, llm, settings, lambda *_: None)
        for s_ in samples:
            print(f"  试译：{s_.title_zh or '（失败）'} ｜ {s_.summary_zh[:60]}")
        if n < len(samples):
            print("❌ 大模型试译未全部成功：请检查密钥、模型名，或稍后重试（免费档有每分钟/每天次数限制）。")
            ok = False
        else:
            print("✅ 大模型试译成功。")
    tr = Translator(True, email=env.get("MYMEMORY_EMAIL", ""))
    tr.cache = {}
    t = tr.translate("The actor said on Friday that the series will return next year.")
    print(f"机器翻译兜底：{'✅ ' + t if t else '❌ 全部通道失败（通道可能被拦截）'}｜通道统计 {dict(tr.stats)}｜MyMemory 邮箱={'已设置' if env.get('MYMEMORY_EMAIL') else '未设置（匿名每天 5000 字）'}")
    return 0 if ok else 1


def cmd_alert(a) -> int:
    env = _env()
    if not (env.get("TELEGRAM_BOT_TOKEN") and env.get("ADMIN_CHAT_ID")):
        print(a.text)
        return 0
    Telegram(env["TELEGRAM_BOT_TOKEN"]).send(env["ADMIN_CHAT_ID"], a.text, html_mode=False)
    return 0


def cmd_send_test(a) -> int:
    """逐个向 主频道/管理员/短剧群 发测试消息，并把 Telegram 拒绝的真实原因打印出来。"""
    env = _env()
    if not env.get("TELEGRAM_BOT_TOKEN"):
        print("❌ 没有设置 TELEGRAM_BOT_TOKEN")
        return 2
    tg = Telegram(env["TELEGRAM_BOT_TOKEN"], 0)
    targets = {"main": env.get("TELEGRAM_CHAT_ID"), "admin": env.get("ADMIN_CHAT_ID"), "drama": env.get("DRAMA_CHAT_ID")}
    names = list(targets) if a.to == "all" else [a.to]
    bad, lines = 0, []
    for n in names:
        cid = (targets.get(n) or "").strip()
        if not cid:
            lines.append(f"⏭ {n}：未设置")
            continue
        try:
            tg.send(cid, f"✅ 织光内容雷达连通性测试（{n}）", silent=True, html_mode=False)
            lines.append(f"✅ {n}：发送成功")
        except TelegramError as e:
            bad += 1
            lines.append(f"❌ {n}：{e}")
    print("\n".join(lines))
    if targets["admin"]:
        try:
            tg.send(targets["admin"].strip(), "连通性测试结果\n" + "\n".join(lines), silent=True, html_mode=False)
        except TelegramError:
            pass
    return 1 if bad else 0


def cmd_resolve(a) -> int:
    Ledger().set(a.date, a.key, status=a.status)
    print(f"已将 {a.date} {a.key} 标记为 {a.status}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="radar", description="织光内容雷达 v2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor"); d.add_argument("--only"); d.add_argument("--json", action="store_true"); d.set_defaults(fn=cmd_doctor)
    b = sub.add_parser("build"); b.add_argument("--kind", default="daily", choices=["daily", "weekly"])
    b.add_argument("--date"); b.add_argument("--from-items"); b.add_argument("--no-llm", action="store_true")
    b.add_argument("--out", default="out"); b.set_defaults(fn=cmd_build)
    r = sub.add_parser("run"); r.add_argument("--force", choices=["daily", "weekly"]); r.set_defaults(fn=cmd_run)
    sub.add_parser("watchdog").set_defaults(fn=cmd_watchdog)
    sub.add_parser("llm-test").set_defaults(fn=cmd_llm_test)
    sv = sub.add_parser("serve"); sv.add_argument("--max-loops", type=int, default=None, help=argparse.SUPPRESS); sv.set_defaults(fn=cmd_serve)
    al = sub.add_parser("alert"); al.add_argument("--text", required=True); al.set_defaults(fn=cmd_alert)
    st = sub.add_parser("send-test"); st.add_argument("--to", default="all", choices=["all", "main", "admin", "drama"]); st.set_defaults(fn=cmd_send_test)
    rv = sub.add_parser("resolve"); rv.add_argument("--date", required=True); rv.add_argument("--key", required=True)
    rv.add_argument("--status", required=True, choices=["sent", "failed"]); rv.set_defaults(fn=cmd_resolve)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:300]
        print(f"运行失败：{msg}")
        _notify_admin(f"⚠️ 雷达运行失败（{a.cmd}）：{msg}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
