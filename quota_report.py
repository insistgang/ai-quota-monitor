#!/usr/bin/env python3
"""quota_report.py — AI 额度监控表（查询用量，不发送模型任务或主动重置额度）

所有监控来源均通过 Sub2API 在服务器只读读取；Antigravity 分组额度按需补充本机
`agy /usage` 面板的周重置周期。

用法：
  python3 quota_report.py            # 终端表格
  python3 quota_report.py --json     # 机器可读
  python3 quota_report.py --log      # 追加快照到 quota-log.csv
  python3 quota_report.py --html     # 生成 quota-dashboard.html
  python3 quota_report.py --serve    # 本地实时刷新服务（127.0.0.1:8788）
安全：永不打印任何 key/token；网页统一打码邮箱并应用私有脱敏映射。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import sub2api_quota

HOME = Path.home()
LOG_CSV = Path(__file__).with_name("quota-log.csv")
PUBLIC_HIDDEN_ACCOUNTS_PATH = Path(os.environ.get(
    "QUOTA_PUBLIC_HIDDEN_ACCOUNTS",
    str(HOME / ".cache/ai-quota-monitor/public-hidden-accounts.json"),
))
LAST_GOOD_JSON = Path(os.environ.get(
    "QUOTA_LAST_GOOD",
    str(HOME / ".cache/ai-quota-monitor/last-good.json"),
))
AGY_GROUPS_CACHE = Path(os.environ.get(
    "QUOTA_AGY_GROUPS_CACHE",
    str(HOME / ".cache/ai-quota-monitor/agy-groups.json"),
))


# ---------- 本地缓存读写 ----------


def _write_json_0600(path: Path, payload: dict, *, indent: int | None = 2) -> None:
    """原子写入权限为 0600 的本地 JSON 缓存，防止中途中断破坏或过宽权限暴露。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=indent) + "\n",
            encoding="utf-8",
        )
        temp_path.chmod(0o600)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


# ---------- 数据源 ----------


def _agy_bin() -> str:
    return shutil.which("agy") or str(HOME / ".local/bin/agy")


def _tmux_slash_probe(cmd: str, slash: str, wait_boot: int = 12, wait_panel: int = 5,
                      pre_enter: bool = False) -> str | None:
    """通用：tmux 驱动 TUI 执行 slash 命令并截屏。pre_enter 用于先按一次回车（如 agy 的信任确认）。"""
    if not shutil.which("tmux"):
        return None
    sess = f"quota_probe_{Path(cmd).name.replace(' ', '_')}"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    env = os.environ.copy()
    env["PATH"] = f"{HOME}/.local/bin:{env.get('PATH', '')}"
    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-x", "200", "-y", "50", cmd],
                       env=env, check=True, timeout=15, capture_output=True)
        time.sleep(wait_boot)
        if pre_enter:
            subprocess.run(["tmux", "send-keys", "-t", sess, "Enter"],
                           check=True, timeout=5, capture_output=True)
            time.sleep(4)
        subprocess.run(["tmux", "send-keys", "-t", sess, slash, "Enter"],
                       check=True, timeout=5, capture_output=True)
        time.sleep(wait_panel)
        out = subprocess.run(["tmux", "capture-pane", "-t", sess, "-p"],
                             capture_output=True, timeout=5)
        return out.stdout.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None
    finally:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)


def _parse_refreshes_in(s: str) -> str:
    """'Refreshes in 74h 29m' 或 '3 hours, 59 minutes' → 绝对时间 %m-%d %H:%M"""
    m = re.search(
        r"(?:(\d+)\s*(?:d|days?))?[,\s]*(?:(\d+)\s*(?:h|hours?))?[,\s]*(?:(\d+)\s*(?:m|minutes?))?",
        s,
    )
    if not m or not any(m.groups()):
        return "?"
    d, h, mi = (int(g) if g else 0 for g in m.groups())
    t = dt.datetime.now() + dt.timedelta(days=d, hours=h, minutes=mi)
    return t.strftime("%m-%d %H:%M")


def _agy_parse_group(text: str, marker: str) -> dict | None:
    """解析 agy /usage 面板中的一个模型组。"""
    if marker not in text:
        return None
    seg = text.split(marker, 1)[1][:2000]
    for stop in ("GEMINI MODELS", "CLAUDE AND GPT MODELS", "GEMINI 4 ARGON", "Within each group"):
        if stop != marker and stop in seg:
            seg = seg.split(stop, 1)[0]
    m_week = re.search(
        r"(?:Weekly Limit Remaining|Gemini 4 Argon Weekly Limit)\s*\n[^%\n]*?([\d.]+)%[^\n]*\n\s*"
        r"(?:[\d.]+%\s+remaining\s*·\s*)?Refreshes in ([^\n]+)",
        seg,
    )
    m_5h = re.search(
        r"Five Hour Limit Remaining\s*\n[^%\n]*?([\d.]+)%[^\n]*\n\s*"
        r"(?:[\d.]+%\s+remaining\s*·\s*)?Refreshes in ([^\n]+)",
        seg,
    )
    m_5h_disabled = re.search(
        r"Five Hour Limit Remaining\s*\n\s*Disabled:\s*You have hit your 5-hour limit[^\n]*?refresh in ([^\n.]+)",
        seg,
        re.IGNORECASE,
    )
    if not m_week:
        return None
    week_left = float(m_week.group(1))
    used_week = round(100 - week_left, 1)
    row = {
        "used_pct": used_week,
        "used_text": f"{used_week:g}%",
        "reset": _parse_refreshes_in(m_week.group(2)),
    }
    if m_5h:
        left5 = float(m_5h.group(1))
        used_5h = round(100 - left5, 1)
        row["fiveh_pct"] = used_5h
        row["fiveh_text"] = f"{used_5h:g}%"
        row["fiveh_reset"] = _parse_refreshes_in(m_5h.group(2))
    elif m_5h_disabled:
        row["fiveh_pct"] = 100.0
        row["fiveh_text"] = "100%"
        row["fiveh_reset"] = _parse_refreshes_in(m_5h_disabled.group(1))
    return row


def _load_agy_groups_cache() -> dict[str, dict]:
    try:
        payload = json.loads(AGY_GROUPS_CACHE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("groups"), dict):
            return {}
        return payload
    except (OSError, ValueError):
        return {}


def _save_agy_groups_cache(groups: dict[str, dict]) -> None:
    try:
        _write_json_0600(AGY_GROUPS_CACHE, {"captured_at": time.time(), "groups": groups})
    except OSError:
        pass


def _enrich_antigravity_rows(rows: list[dict]) -> list[dict]:
    """为 Sub2API 拆出的 Gemini 4 Hydrogen / Gemini · Claude 补齐周额度 + 5h 双窗口。"""
    needs_enrichment = any(
        r.get("status") == "ok"
        and r.get("name") in {"Gemini 4 Hydrogen", "Gemini · Claude"}
        and r.get("fiveh_pct") is None
        and r.get("window_kind") == "5h"
        for r in rows
    )
    if not needs_enrichment:
        return rows

    cache = _load_agy_groups_cache()
    captured_at = cache.get("captured_at")
    groups = cache.get("groups") if isinstance(cache.get("groups"), dict) else {}
    cache_fresh = isinstance(captured_at, (int, float)) and 0 <= time.time() - float(captured_at) <= 1800

    if not cache_fresh or "Gemini 4 Hydrogen" not in groups:
        text = _tmux_slash_probe(_agy_bin(), "/usage", wait_boot=12, wait_panel=6, pre_enter=False)
        if text:
            parsed_groups: dict[str, dict] = {}
            for marker, target_name in (
                ("GEMINI MODELS", "Gemini 4 Hydrogen"),
                ("CLAUDE AND GPT MODELS", "Gemini · Claude"),
                ("GEMINI 4 ARGON", "Gemini · 4 Argon"),
            ):
                g = _agy_parse_group(text, marker)
                if g:
                    parsed_groups[target_name] = g
            if parsed_groups:
                groups = {**groups, **parsed_groups}
                _save_agy_groups_cache(groups)

    enriched = []
    for r in rows:
        name = r.get("name")
        if (
            r.get("status") == "ok"
            and name in {"Gemini 4 Hydrogen", "Gemini · Claude"}
            and r.get("fiveh_pct") is None
            and r.get("window_kind") == "5h"
            and isinstance(groups.get(name), dict)
        ):
            g = groups[name]
            week_pct = g.get("used_pct")
            week_reset = g.get("reset")
            if week_pct is not None and week_reset:
                updated = {
                    **r,
                    "fiveh_pct": r.get("used_pct"),
                    "fiveh_text": r.get("used_text", "—"),
                    "fiveh_reset": r.get("reset", "?"),
                    "fiveh_label": "5h 窗",
                    "used_pct": week_pct,
                    "used_text": g.get("used_text", f"{week_pct:g}%"),
                    "reset": week_reset,
                    "window_kind": "dual",
                }
                enriched.append(updated)
                continue
        enriched.append(r)
    return enriched


def _unhealthy_rows(rows: list[dict]) -> list[dict]:
    """统计需要重试或阻断写入的来源。

    明确标记 retryable=False 的来源（配置里未接入 Sub2API 的账号）是既定状态，
    不是本次采集故障，不计入异常预算——否则容错额度会被永久占满。
    """
    return [
        row for row in rows
        if row.get("retryable") is not False
        and (row.get("status") != "ok" or row.get("stale"))
    ]


def _save_last_good(rows: list[dict]) -> None:
    """记住最近一次全部新鲜的快照，供整体读取失败时降级展示。"""
    fresh = {r["name"]: r for r in rows if r.get("status") == "ok" and not r.get("stale")}
    if not fresh:
        return
    try:
        _write_json_0600(
            LAST_GOOD_JSON,
            {"captured_at": dt.datetime.now().isoformat(timespec="seconds"), "rows": fresh},
            indent=None,
        )
    except OSError:
        pass


def _load_last_good() -> dict:
    try:
        data = json.loads(LAST_GOOD_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("rows") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def _degrade_with_cache(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """把本次读取失败的来源换成上次成功值，明确标注过期；返回 (行, 无缓存可用者)。"""
    cached = _load_last_good()
    out: list[dict] = []
    missing: list[str] = []
    for row in rows:
        if row.get("status") == "ok" and not row.get("stale"):
            out.append(row)
            continue
        if row.get("retryable") is False:
            out.append(row)  # 既定未接入，不属于本次故障
            continue
        name = row.get("name") or ""
        old = cached.get(name)
        if not isinstance(old, dict) or (old.get("used_pct") is None and old.get("fiveh_pct") is None):
            out.append(row)
            missing.append(name)
            continue
        out.append({
            **old,
            "status": "ok",
            "stale": True,
            "cache_fallback": True,
            "note": f"{old.get('note', '')} · 本次刷新失败，沿用上次快照",
        })
    return out, missing


def _quota_hidden(name: str | None) -> bool:
    """隐藏已下线的 Grok、已停用的 Antigravity 组及已移除的豆包，历史展示同步过滤。"""
    lowered = (name or "").strip().lower()
    return "grok" in lowered or ("antigravity" in lowered and "claude" in lowered and "gemini" not in lowered) or ("豆包" in lowered and "codex" not in lowered)


def _public_hidden_codex_names() -> set[str]:
    """从本机私有配置读取不公开的 Codex 账号，公开仓库不保存完整标识。"""
    try:
        data = json.loads(PUBLIC_HIDDEN_ACCOUNTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("公开过滤配置缺失或不可读") from exc
    emails = data.get("codex_emails") if isinstance(data, dict) else None
    if not isinstance(emails, list) or not emails:
        raise RuntimeError("公开过滤配置缺少账号列表")
    if any(
        not isinstance(email, str)
        or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()) is None
        for email in emails
    ):
        raise RuntimeError("公开过滤配置包含无效邮箱")
    return {f"Codex · {email.strip()}".casefold() for email in emails}


def _visible_quota_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not _quota_hidden(row.get("name"))]


def collect(*, retry_attempts: int = 0, retry_delay: float = 0) -> list[dict]:
    config = sub2api_quota.load_config()
    if config is None:
        raise sub2api_quota.ConfigError("尚未配置 Sub2API 额度数据源")
    rows = _enrich_antigravity_rows(
        sub2api_quota.collect(
            config,
            retry_attempts=retry_attempts,
            retry_delay=retry_delay,
        )
    )
    return _visible_quota_rows(rows)


# ---------- 排序 / 刷新提醒 ----------

_MONTHS = {n: i for i, n in enumerate(
    "January February March April May June July August September "
    "October November December".split(), 1)}
_WEEKLY_SOON_H = 36
_FIVEH_SOON_H = 3


def _remain_pct(r: dict, key: str | None = None) -> float | None:
    if key is not None:
        pct = r.get(key)
        return max(0.0, min(100.0, 100.0 - float(pct))) if pct is not None else None
    pct = r.get("used_pct")
    if pct is not None:
        return max(0.0, min(100.0, 100.0 - float(pct)))
    fp = r.get("fiveh_pct")
    if fp is not None:
        return max(0.0, min(100.0, 100.0 - float(fp)))
    return None


def _resolve_reset_dt(now: dt.datetime, month: int, day: int, hh: int, mm: int) -> dt.datetime | None:
    try:
        d = now.replace(month=month, day=day, hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return None
    if d < now - dt.timedelta(days=2):
        try:
            d = d.replace(year=now.year + 1)
        except ValueError:
            pass
    return d


def _parse_reset(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    raw = str(s).strip()
    now = dt.datetime.now()
    m = re.search(r"(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", raw)
    if m:
        month, day, hh, mm = (int(x) for x in m.groups())
        return _resolve_reset_dt(now, month, day, hh, mm)
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{1,2}):(\d{2})", raw)
    if m:
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        day, hh, mm = int(m.group(2)), int(m.group(3)), int(m.group(4))
        return _resolve_reset_dt(now, mon, day, hh, mm)
    return None


def _fmt_until(when: dt.datetime, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    secs = (when - now).total_seconds()
    if secs <= 0:
        return "已到点"
    hours = secs / 3600
    if hours < 1:
        return f"{max(1, int(secs // 60))} 分钟后"
    if hours < 24:
        return f"{hours:.0f} 小时后"
    if hours < 48:
        return "明天 " + when.strftime("%H:%M")
    return when.strftime("%m-%d %H:%M")


def _sort_rows(rows: list[dict]) -> list[dict]:
    """周剩余从小到大；查失败的放最后。"""
    def key(r: dict):
        ok = r.get("status") == "ok"
        rem = _remain_pct(r)
        return (0 if ok else 1, rem if rem is not None else 999.0, r.get("name") or "")
    return sorted(rows, key=key)


def _alerts(rows: list[dict]) -> list[dict]:
    """还剩额度、且窗口快刷新 → 提醒抓紧用。周窗优先于 5h 窗。"""
    now = dt.datetime.now()
    out: list[dict] = []
    for r in rows:
        if r.get("status") != "ok" or r.get("stale"):
            continue
        if _quota_hidden(r.get("name")):
            continue
        weekly_rem = _remain_pct(r, "used_pct")
        wr = _parse_reset(r.get("reset"))
        hit_week = False
        if weekly_rem is not None and weekly_rem > 3 and wr is not None:
            hours = (wr - now).total_seconds() / 3600
            if 0 <= hours <= _WEEKLY_SOON_H:
                out.append({
                    "name": r["name"],
                    "kind": "week",
                    "remain": weekly_rem,
                    "when": wr,
                    "until": _fmt_until(wr, now),
                })
                hit_week = True
        if hit_week:
            continue
        # 周额度已用完时，5h 窗剩余同样无法使用，不再提示抓紧用。
        if weekly_rem is not None and weekly_rem <= 3:
            continue
        # Kimi 的 5h 窗只限制短时吞吐，不代表周额度即将过期；提醒只看周重置。
        if "kimi" in (r.get("name") or "").lower():
            continue
        f_rem = _remain_pct(r, "fiveh_pct")
        fr = _parse_reset(r.get("fiveh_reset"))
        if f_rem is not None and f_rem > 10 and fr is not None:
            hours = (fr - now).total_seconds() / 3600
            if 0 <= hours <= _FIVEH_SOON_H:
                out.append({
                    "name": r["name"],
                    "kind": "fiveh",
                    "window_label": r.get("fiveh_label", "5h 窗"),
                    "remain": f_rem,
                    "when": fr,
                    "until": _fmt_until(fr, now),
                })
    out.sort(key=lambda a: a["when"])
    return out


def _alerts_text(alerts: list[dict]) -> list[str]:
    if not alerts:
        return []
    lines = ["# 快刷新 · 剩余额度抓紧用", ""]
    for a in alerts:
        window = "周额度" if a["kind"] == "week" else a.get("window_label", "5h 窗")
        lines.append(
            f"- **{a['name']}**：{window} {a['until']}刷新，还剩 {a['remain']:.0f}%"
        )
    lines.append("")
    return lines


def _html_text(value: object) -> str:
    """所有网页动态文本先打码邮箱，再转义；本地底账保留原始账号标识。"""
    return html.escape(_mask_email(value), quote=True)


def _alerts_html(alerts: list[dict]) -> str:
    if not alerts:
        return ""
    items = "".join(
        f"<li><span class='alert-name'><b>{_html_text(a['name'])}</b></span>"
        f"<span class='alert-detail'>"
        f"<span class='alert-win'>{_html_text('周额度' if a['kind']=='week' else a.get('window_label', '5h 窗'))}</span>"
        f"<span class='alert-time'>{_html_text(a['until'])}刷新</span>"
        f"<strong class='alert-rem'>还剩 {a['remain']:.0f}%</strong></span></li>"
        for a in alerts
    )
    return f"""<div class="alertband"><h3>⚡ 快刷新了，剩余额度抓紧用</h3><ul>{items}</ul></div>"""


# ---------- 输出 ----------

def render(rows: list[dict]) -> str:
    rows = _sort_rows(_visible_quota_rows(rows))
    alerts = _alerts(rows)
    lines = [f"# AI 额度快照 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 按周剩余从小到大", ""]
    for r in rows:
        if r.get("status") != "ok":
            lines.append(f"- **{r['name']}**：{r['status']}")
            continue
        rem = _remain_pct(r)
        if r.get("used_pct") is None and r.get("fiveh_text"):
            window_label = r.get("fiveh_label", "5h 窗")
            rem_s = f"，剩 {rem:g}%" if rem is not None else ""
            s = f"- **{r['name']}**：{window_label}已用 {r['fiveh_text']}{rem_s}，重置 {r.get('fiveh_reset', '?')}"
            if r.get("note"):
                s += f"，{r['note']}"
            lines.append(s)
            continue
        rem_s = ""
        if rem is not None and f"剩 {rem:g}%" not in str(r.get("used_text") or ""):
            rem_s = f"，剩 {rem:g}%"
        s = f"- **{r['name']}**：本周已用 {r['used_text']}"
        if r.get("used_pct") is not None and "%" not in str(r["used_text"]):
            s += f"（{r['used_pct']}%）"
        s += rem_s
        s += f"，周重置 {r.get('reset', '?')}"
        if r.get("fiveh_text"):
            window_label = r.get("fiveh_label", "5h 窗")
            s += f"，{window_label}已用 {r['fiveh_text']}（重置 {r.get('fiveh_reset', '?')}）"
        if r.get("note"):
            s += f"，{r['note']}"
        lines.append(s)
        if r.get("sub_quotas"):
            for sq in r["sub_quotas"]:
                lines.append(f"  · {sq['label']}：已用 {sq['used_text']}，重置 {sq['reset']}")
    extra = _alerts_text(alerts)
    if extra:
        lines.append("")
        lines.extend(extra)
    return "\n".join(lines)


def _color(pct) -> str:
    if pct is None:
        return "#8b949e"
    if pct < 50:
        return "#3fb950"
    if pct < 80:
        return "#d29922"
    if pct < 95:
        return "#f0883e"
    return "#f85149"


def _provider_meta(raw_name: str) -> tuple[str, str, str]:
    low = (raw_name or "").lower()
    if "codex" in low:
        return ("codex", "OpenAI · Codex", "⚡")
    if "claude" in low:
        return ("claude", "Anthropic · Claude", "🎭")
    if "argon" in low:
        return ("argon", "Google · Argon", "♊")
    if "gemini" in low or "antigravity" in low:
        return ("gemini", "Google · Gemini", "✨")
    if "kimi" in low:
        return ("kimi", "Moonshot · Kimi", "🌙")
    if "minimax" in low:
        return ("minimax", "MiniMax · Coding", "🎬")
    return ("other", "AI 额度", "🤖")


def _card(r: dict, alert: dict | None = None) -> str:
    name = _html_text(r["name"])
    pkey, pbadge, picon = _provider_meta(r["name"])
    if r.get("status") != "ok":
        return f"""<div class="card" data-provider="{pkey}" data-state="attn"><div class="top"><div class="title-wrap"><span class="pbadge">{picon} {pbadge}</span><span class="name">{name}</span></div>
<span class="pill bad">异常</span></div><div class="msg">{_html_text(r['status'])}</div></div>"""
    rem = _remain_pct(r)
    # 没有周额度、只有短时/5h 窗口的模型（如 MiniMax）：主卡片直接展示短时窗口，避免显示空的"本周已用 未提供"
    if r.get("used_pct") is None and r.get("fiveh_text"):
        pct = r.get("fiveh_pct")
        pct_txt = _html_text(r["fiveh_text"])
        rem_txt = f"剩 {rem:g}%" if rem is not None else ""
        c = _color(pct)
        window_label = _html_text(r.get("fiveh_label", "5h 窗"))
        win_tag_label = f"⏱️ {window_label}周期配额"
        meta_line = f"{window_label}已用 {pct_txt} · 重置 {_html_text(r.get('fiveh_reset', '?'))}"
        fiveh = (
            '<div class="sub-box single-win">'
            '<div class="sub-head"><span class="sub">♾️ 周期配额模式</span>'
            '<span class="sub-tag">滚动短窗</span></div>'
            '<div class="meta">按 5 小时滚动窗口独立计量，不设额外周总额度上限</div></div>'
        )
    else:
        pct = r.get("used_pct")
        pct_txt = f"{pct:g}%" if pct is not None else "—"
        rem_txt = f"剩 {rem:g}%" if rem is not None else ""
        c = _color(pct)
        win_tag_label = "📅 本周额度（周窗口）"
        meta_line = f"本周已用 {_html_text(r.get('used_text', '?'))} · 重置 {_html_text(r.get('reset', '?'))}"
        fiveh = ""
        if r.get("fiveh_text"):
            fp = r.get("fiveh_pct")
            fc = _color(fp)
            f_rem = _remain_pct(r, "fiveh_pct")
            f_rem_badge = f"<span class='sub-rem'>剩 {f_rem:g}%</span>" if f_rem is not None else ""
            f_pct_badge = f"<span class='sub-pct' style='color:{fc}'>{fp:g}%</span>" if fp is not None else ""
            f_exh = " exhausted" if (fp is not None and fp >= 100) else ""
            raw_fiveh_label = r.get("fiveh_label", "5h 窗")
            fiveh_title = (
                "5h 限流窗"
                if raw_fiveh_label == "5h 窗"
                else f"{_html_text(raw_fiveh_label)}（5h 限流窗）"
            )
            fiveh = f"""<div class="sub-box{f_exh}"><div class="sub-head"><span class="sub">⚡ {fiveh_title}</span><span class="sub-stats">{f_pct_badge}{f_rem_badge}</span></div>
<div class="bar small"><div class="fill" style="width:{min(100, max(0, fp or 0))}%;background:{fc}"></div></div>
<div class="meta">已用 {_html_text(r['fiveh_text'])} · 重置 {_html_text(r.get('fiveh_reset', '?'))}</div></div>"""
        elif not r.get("sub_quotas"):
            fiveh = (
                '<div class="sub-box single-win">'
                '<div class="sub-head"><span class="sub">♾️ 独立周配额池</span>'
                '<span class="sub-tag">仅周窗口</span></div>'
                '<div class="meta">直接按 7 天周额度扣减，无 5 小时短时窗口限制</div></div>'
            )
    sub_bars = ""
    if r.get("sub_quotas"):
        blocks = []
        for sq in r["sub_quotas"]:
            sp = sq.get("used_pct")
            sc = _color(sp)
            label = _html_text(sq["label"])
            used_txt = _html_text(sq.get("used_text", "—"))
            reset_txt = _html_text(sq.get("reset", "?"))
            blocks.append(f"""<div class="sub-box"><div class="sub-head"><span class="sub">{label}</span><span class="sub-pct" style="color:{sc}">{used_txt}</span></div>
<div class="bar small"><div class="fill" style="width:{min(100, max(0, sp or 0))}%;background:{sc}"></div></div>
<div class="meta">已用 {used_txt} · 重置 {reset_txt}</div></div>""")
        sub_bars = "".join(blocks)
    note = f"<div class='meta dim'>{_html_text(r['note'])}</div>" if r.get("note") else ""
    urgent = " urgent" if alert else ""
    weekly_rem = _remain_pct(r, "used_pct")
    fp_val = r.get("fiveh_pct")
    if r.get("cache_fallback"):
        pill = "<span class='pill stale'>使用缓存</span>"
    elif r.get("stale"):
        pill = "<span class='pill stale'>快照过期</span>"
    else:
        if weekly_rem is not None and weekly_rem <= 3:
            pill = "<span class='pill bad'>已用完</span>"
        elif fp_val is not None and fp_val >= 100:
            pill = "<span class='pill bad'>5h 已满</span>"
        else:
            pill = "<span class='pill warn'>抓紧用</span>" if alert else "<span class='pill ok'>正常</span>"
    card_state = (
        "attn"
        if (
            alert
            or r.get("stale")
            or r.get("cache_fallback")
            or (weekly_rem is not None and weekly_rem <= 20)
            or (fp_val is not None and fp_val >= 80)
        )
        else "ok"
    )
    return f"""<div class="card{urgent}" data-provider="{pkey}" data-state="{card_state}"><div class="top"><div class="title-wrap"><span class="pbadge">{picon} {pbadge}</span><span class="name">{name}</span></div>
{pill}</div>
<div class="win-label">{win_tag_label}</div>
<div class="stat-row"><div class="pct" style="color:{c}">{pct_txt}</div>
<div class="remain">{rem_txt}</div></div>
<div class="bar"><div class="fill" style="width:{min(100, max(0, pct or 0))}%;background:{c}"></div></div>
<div class="meta main-meta">{meta_line}</div>
{sub_bars}{fiveh}{note}</div>"""


def _summary_strip_html(rows: list[dict], alerts: list[dict]) -> str:
    if not rows:
        return ""
    total = len(rows)
    alert_names = {a["name"] for a in alerts}
    exhausted = 0
    warn_count = 0
    healthy = 0
    rem_vals: list[float] = []
    dual_count = 0
    for r in rows:
        if r.get("fiveh_text") and r.get("used_pct") is not None:
            dual_count += 1
        if r.get("status") != "ok":
            exhausted += 1
            continue
        weekly_rem = _remain_pct(r, "used_pct")
        if weekly_rem is not None:
            rem_vals.append(weekly_rem)
        fp = r.get("fiveh_pct")
        if (weekly_rem is not None and weekly_rem <= 3) or (fp is not None and fp >= 100):
            exhausted += 1
        elif (
            r.get("name") in alert_names
            or (weekly_rem is not None and weekly_rem <= 20)
            or (weekly_rem is None and fp is not None and fp >= 80)
            or r.get("stale")
        ):
            warn_count += 1
        else:
            healthy += 1
    avg_rem = f"{sum(rem_vals) / len(rem_vals):.0f}%" if rem_vals else "—"
    return (
        '<div class="summary-strip" aria-label="额度概览统计">'
        f'<div class="kpi"><span class="kpi-label">监控源总数</span><span class="kpi-val">{total}</span><span class="kpi-sub">含 {dual_count} 个 5h+周双窗模型</span></div>'
        f'<div class="kpi ok"><span class="kpi-label">正常可用</span><span class="kpi-val">{healthy}</span><span class="kpi-sub">周窗与 5h 窗均充裕</span></div>'
        f'<div class="kpi warn"><span class="kpi-label">快刷新 / 关注</span><span class="kpi-val">{warn_count}</span><span class="kpi-sub">临近重置或余量偏低</span></div>'
        f'<div class="kpi bad"><span class="kpi-label">限流 / 已耗尽</span><span class="kpi-val">{exhausted}</span><span class="kpi-sub">周额度用尽或 5h 满载</span></div>'
        f'<div class="kpi accent"><span class="kpi-label">平均周剩余</span><span class="kpi-val">{avg_rem}</span><span class="kpi-sub">按周剩余从低到高排序</span></div>'
        '</div>'
    )


_finance_md = os.environ.get("QUOTA_FINANCE_MD", "").strip()
FINANCE_MD = Path(_finance_md) if _finance_md else None
FINANCE_CONFIG_PATH = Path(os.environ.get(
    "QUOTA_FINANCE_CONFIG",
    str(HOME / ".cache/ai-quota-monitor/finance.json"),
))


def _load_finance_config(path: Path | None = None) -> dict:
    """读取本机私有财务配置（笔记路径与公开脱敏映射）；不存在时返回空配置。"""
    config_path = path or FINANCE_CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        version = data.get("version", 1)
        if type(version) is not int or version != 1:
            raise ValueError
        finance_md = data.get("finance_md")
        if finance_md is not None and not isinstance(finance_md, str):
            raise ValueError
        replacements = data.get("public_replacements")
        if replacements is not None:
            if not isinstance(replacements, dict) or any(
                not isinstance(k, str) or not k or not isinstance(v, str)
                for k, v in replacements.items()
            ):
                raise ValueError
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("财务私有配置缺失或格式无效") from exc


def _resolve_finance_md(config: dict | None = None) -> Path | None:
    if FINANCE_MD is not None:
        return FINANCE_MD
    env_path = os.environ.get("QUOTA_FINANCE_MD", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    cfg = _load_finance_config() if config is None else config
    raw = cfg.get("finance_md", "") if isinstance(cfg, dict) else ""
    return Path(raw.strip()).expanduser() if isinstance(raw, str) and raw.strip() else None


def _strip_md(s: str) -> str:
    return s.replace("**", "").replace("~~", "").strip()


def _load_finance() -> dict:
    """解析订阅、私有预存余额、历史充值、非 AI 订阅与合计口径。"""
    out = {"subs": [], "prepaid": [], "ledger": [], "non_ai": [], "total_ai": "", "total_all": ""}
    finance_path = _resolve_finance_md()
    if finance_path is None:
        return out
    try:
        text = finance_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return out
    section = ""
    prepaid_public_column = None
    in_non_ai = False
    for line in text.splitlines():
        if line.startswith("## 一、"):
            in_non_ai = False
            section = "subs"; continue
        if line.startswith("## 二、"):
            in_non_ai = False
            section = "ledger"; continue
        if line == "## ChatGPT 预存余额与扣费计划（私密）":
            in_non_ai = False
            prepaid_public_column = None
            section = "prepaid"; continue
        if line.startswith("## "):
            in_non_ai = False
            if section in ("subs", "prepaid", "ledger"):
                section = ""
        if section and line.startswith("|") and "---" not in line:
            cells = [
                _strip_md(c.replace(r"\|", "|"))
                for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))
            ]
            if section == "prepaid" and cells[0] == "账号":
                prepaid_public_column = (
                    cells.index("公开展示名", 6) if "公开展示名" in cells[6:] else None
                )
            if section == "subs" and len(cells) >= 5 and cells[0] != "产品":
                if "已停用" not in cells[1] and "已停用" not in cells[0]:
                    out["subs"].append({"name": cells[0], "cost": cells[2], "renewal": cells[4]})
            elif section == "ledger" and len(cells) >= 3 and cells[0] != "日期":
                out["ledger"].append({"date": cells[0], "item": cells[1], "amount": cells[2]})
            elif section == "prepaid" and len(cells) >= 6 and cells[0] != "账号":
                account = dict(zip(
                    ("name", "as_of", "balance", "debit", "covered", "next_topup"), cells[:6],
                ))
                account["public_name"] = (
                    cells[prepaid_public_column]
                    if prepaid_public_column is not None and len(cells) > prepaid_public_column
                    else ""
                )
                out["prepaid"].append(account)
        m = re.search(r"AI 固定订阅合计（本人实付）：约\s*([\d.]+\s*元/月)", line)
        if m:
            out["total_ai"] = m.group(1)
        m2 = re.search(r"固定订阅总额约\s*\**([\d.]+\s*元/月)", line)
        if m2:
            out["total_all"] = m2.group(1)
        if re.match(r"^\s*-\s*\*{0,2}非 AI 固定订阅\*{0,2}：", line):
            in_non_ai = True
            continue
        if in_non_ai:
            if line.strip().startswith("- ") and not line.startswith((" ", "\t")):
                in_non_ai = False
            elif line.startswith((" ", "\t")):
                sub_item = re.match(r"^\s*-\s*([^：]+)：(.+)$", line)
                if sub_item:
                    out["non_ai"].append(
                        f"{_strip_md(sub_item.group(1))}：{_strip_md(sub_item.group(2))}"
                    )
                    continue
        m3 = re.match(
            r"^\s*-\s*(iCloud|得到会员|得到大脑会员|微信读书|百度网盘|视频会员|ikuuu[^：]*)：(.+)$",
            line,
        )
        if m3:
            out["non_ai"].append(f"{m3.group(1)}：{_strip_md(m3.group(2))}")
    return out


_SVC_ICONS = [
    ("ChatGPT", "⚡"), ("Codex", "⚡"), ("GPT", "⚡"), ("Kimi", "🌙"),
    ("Gemini", "♊"), ("Google", "♊"), ("MiniMax", "🎬"), ("智谱", "🧠"),
    ("ChatGLM", "🧠"), ("Grok", "🚀"), ("中转", "🔀"), ("DeepSeek", "🔍"),
    ("Claude", "🎭"), ("土区", "🪙"), ("TRY", "🪙"), ("VPS", "🖥️"),
    ("云服务器", "🖥️"), ("域名", "🌐"), ("节点", "🌐"), ("ikuuu", "🌐"),
    ("科学上网", "🌐"), ("ReadPaper", "📄"),
    ("iCloud", "☁️"), ("得到", "🎧"), ("微信读书", "📚"), ("百度网盘", "💾"),
    ("视频", "📺"),
]

_SUB_PROVIDERS = (
    ("chatgpt", "codex", "⚡ OpenAI · ChatGPT"),
    ("codex", "codex", "⚡ OpenAI · Codex"),
    ("kimi", "kimi", "🌙 Moonshot · Kimi"),
    ("gemini", "gemini", "♊ Google · Gemini"),
    ("google", "gemini", "♊ Google"),
    ("minimax", "minimax", "🎬 MiniMax"),
    ("智谱", "zhipu", "🧠 智谱 AI"),
    ("chatglm", "zhipu", "🧠 智谱 AI"),
    ("中转", "proxy", "🔀 第三方中转"),
    ("deepseek", "deepseek", "🔍 DeepSeek"),
    ("claude", "claude", "🎭 Anthropic · Claude"),
)


def _svc_icon(name: str) -> str:
    for k, v in _SVC_ICONS:
        if k.lower() in name.lower():
            return v
    return "📦"


def _sub_provider_meta(name: str) -> tuple[str, str]:
    lowered = name.lower()
    for needle, key, badge in _SUB_PROVIDERS:
        if needle in lowered:
            return key, badge
    return "", "📦 订阅服务"


def _split_cost_headline(cost: str) -> tuple[str, str]:
    """把长括号说明或补充说明拆离主价格，避免大字号金额区折行过长。"""
    text = cost.strip()
    m = re.fullmatch(r"([^（(]+?)\s*[（(](.+)[）)]", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if (len(text) > 16 or "不计入" in text) and "，" in text:
        head, rest = text.split("，", 1)
        return head.strip(), rest.strip()
    return text, ""


def _short_renewal(s: str) -> str:
    """把冗长的刷新/到期描述收成一行：每月 X/Y 日 + 到期或下次扣费日。"""
    days = re.findall(r"每月\s*(\d+)\s*日", s)
    expiry = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:到期|续费)", s)
    next_debit = re.search(r"下次扣费\s*(\d{4}-\d{2}-\d{2})", s)
    parts = []
    if days:
        parts.append("每月 " + "/".join(days) + " 日")
    if expiry:
        exp_text = expiry.group(1) + " 到期"
        if "不续" in s:
            exp_text += "（不续）"
        parts.append(exp_text)
    elif next_debit:
        parts.append("下次扣费 " + next_debit.group(1))
    if parts:
        return "；".join(parts)
    return s if len(s) <= 20 else s[:19] + "…"


def _extract_expiry_date(text: str) -> str | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:到期|续费)|下次扣费\s*(\d{4}-\d{2}-\d{2})", text)
    return (m.group(1) or m.group(2)) if m else None


def _days_left(expiry: str) -> int | None:
    try:
        return (dt.date.fromisoformat(expiry) - dt.date.today()).days
    except Exception:  # noqa: BLE001
        return None


def _countdown_pill(expiry: str | None) -> str:
    if not expiry:
        return ""
    d = _days_left(expiry)
    if d is None:
        return ""
    if d < 0:
        return "<span class='cd red'>已到期</span>"
    if d < 30:
        cls = "red"
    elif d < 60:
        cls = "yellow"
    else:
        cls = "green"
    return f"<span class='cd {cls}'>剩 {d} 天</span>"


def _sub_status_pill(expiry: str | None, cost: str, renewal: str) -> str:
    if "不续" in renewal:
        d = _days_left(expiry) if expiry else None
        if d is not None and d >= 0:
            return f"<span class='cd neutral'>剩 {d} 天·不续</span>"
        return "<span class='cd neutral'>已停续</span>"
    pill = _countdown_pill(expiry)
    if pill:
        return pill
    combined = f"{cost} {renewal}"
    if any(word in combined for word in ("按量", "余额", "耗尽")):
        return "<span class='cd neutral'>按量余额</span>"
    if "每月" in renewal:
        return "<span class='cd neutral'>周期自动续</span>"
    return ""


def _nonai_expiry_days(desc: str) -> int | None:
    """提取在续非 AI 订阅的剩余天数；已停续或 0 元白嫖不计入临近到期告警。"""
    if "不续" in desc or "已到期" in desc or desc.startswith("0 元") or "白嫖" in desc:
        return None
    expiry = _extract_expiry_date(desc)
    return _days_left(expiry) if expiry else None


def _subs_kpi_strip_html(
    fin: dict,
    visible_subs: list[dict],
    prepaid_accounts: list[dict],
    pub,
) -> str:
    expiring: list[tuple[int, str]] = []
    for sub in visible_subs:
        renewal_text = pub(sub["renewal"])
        if "不续" in renewal_text:
            continue
        expiry = _extract_expiry_date(renewal_text)
        if not expiry:
            continue
        days = _days_left(expiry)
        if days is not None and days < 30:
            expiring.append((days, pub(sub["name"]).split("（", 1)[0].strip()))
    for raw in fin.get("non_ai", []):
        text = pub(raw)
        title, desc = text.split("：", 1) if "：" in text else (text, "")
        days = _nonai_expiry_days(desc)
        if days is not None and days < 30:
            expiring.append((days, title.split("（", 1)[0].strip()))
    expiring.sort()

    ai_val = _html_text(pub(fin["total_ai"])) if fin.get("total_ai") else "—"
    all_val = _html_text(pub(fin["total_all"])) if fin.get("total_all") else "—"
    exp_cls = "warn" if expiring else "ok"
    if expiring:
        days_hint = "已到期" if expiring[0][0] < 0 else f"剩 {expiring[0][0]} 天"
        exp_sub = f"最近：{_html_text(expiring[0][1])}（{days_hint}）"
    else:
        exp_sub = "未来 30 天无临近到期"
    ledger = fin.get("ledger", [])
    latest_date = next(
        (str(item.get("date", "")).strip() for item in reversed(ledger) if item.get("date")),
        "",
    )
    if prepaid_accounts and latest_date:
        ledger_sub = f"预存计划 {len(prepaid_accounts)} 账号 · 最新 {latest_date}"
    elif latest_date:
        ledger_sub = f"最新记录 {latest_date}"
    else:
        ledger_sub = "暂无充值台账"

    return (
        '<div class="summary-strip subs-kpi" aria-label="订阅账单汇总">'
        f'<div class="kpi accent"><span class="kpi-label">AI 固定月均</span><span class="kpi-val">{ai_val}</span>'
        f'<span class="kpi-sub">在用列表 {len(visible_subs)} 项（含共享/按量）</span></div>'
        f'<div class="kpi"><span class="kpi-label">含非 AI 合计</span><span class="kpi-val">{all_val}</span>'
        f'<span class="kpi-sub">非 AI 固定订阅 {len(fin.get("non_ai", []))} 项</span></div>'
        f'<div class="kpi {exp_cls}"><span class="kpi-label">30 天内到期 / 扣费</span>'
        f'<span class="kpi-val">{len(expiring)}</span><span class="kpi-sub">{exp_sub}</span></div>'
        f'<div class="kpi"><span class="kpi-label">历史消费台账</span><span class="kpi-val">{len(ledger)} 笔</span>'
        f'<span class="kpi-sub">{_html_text(ledger_sub)}</span></div>'
        '</div>'
    )


def _sub_card_html(sub: dict, pub) -> str:
    name, cost, renewal = pub(sub["name"]), pub(sub["cost"]), pub(sub["renewal"])
    provider_key, badge = _sub_provider_meta(name)
    provider_attr = f' data-provider="{provider_key}"' if provider_key else ""
    expiry = _extract_expiry_date(renewal)
    days = _days_left(expiry) if expiry else None
    urgent_cls = " urgent" if days is not None and days < 30 and "不续" not in renewal else ""
    pill = _sub_status_pill(expiry, cost, renewal)

    headline, note = _split_cost_headline(cost)
    cost_cls = "scost-main"
    if headline in {"0", "0 元"}:
        cost_cls += " free"
    elif "免费" in headline or ("付费" in headline and "不计入" in cost):
        cost_cls += " compact free"
    elif len(headline) > 22:
        cost_cls += " compact long"
    elif len(headline) > 13:
        cost_cls += " compact"
    note_html = f'<div class="scost-note">{_html_text(note)}</div>' if note else ""

    return (
        f'<div class="scard sub-card{urgent_cls}"{provider_attr}>'
        f'<div class="top"><div class="title-wrap"><span class="pbadge">{_html_text(badge)}</span>'
        f'<div class="sname">{_html_text(name)}</div></div>{pill}</div>'
        f'<div class="{cost_cls}">{_html_text(headline)}</div>'
        f'{note_html}'
        f'<div class="meta dim">📅 {_html_text(_short_renewal(renewal))}</div>'
        '</div>'
    )


def _prepaid_cards_html(prepaid_accounts: list[dict], pub) -> str:
    if not prepaid_accounts:
        return ""
    cards = []
    for account in prepaid_accounts:
        raw_name = account.get("name", "")
        acct_name = pub(raw_name)
        is_apple = "apple" in f"{raw_name} {acct_name}".lower()
        provider_key, badge = (
            ("apple", "🍎 Apple · 土区余额池")
            if is_apple else ("codex", "⚡ OpenAI · 土区预存池")
        )
        as_of = pub(account["as_of"])
        debit = pub(account["debit"]).strip()
        covered = pub(account["covered"]).strip()
        next_topup = pub(account["next_topup"]).strip()
        has_schedule = debit not in ("", "—", "-", "无", "暂停")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", next_topup):
            pill = _countdown_pill(next_topup)
            foot_html = f'<div class="meta dim">⏳ 需在 {_html_text(next_topup)} 扣费前补款</div>'
        else:
            label = next_topup if next_topup and next_topup not in ("—", "-") else "余额留存"
            pill = f"<span class='cd neutral'>{_html_text(label)}</span>"
            foot_html = '<div class="meta dim">📌 暂无自动扣费计划</div>'
        smeta = (
            f"截至 {_html_text(as_of)} · 每次扣费 {_html_text(debit)}"
            if has_schedule else f"截至 {_html_text(as_of)} · 暂不自动扣费"
        )
        box_label = "可覆盖扣费" if has_schedule else "用途备注"
        cards.append(
            f'<div class="scard prepaid-card" data-provider="{provider_key}">'
            f'<div class="top"><div class="title-wrap"><span class="pbadge">{badge}</span>'
            f'<div class="sname">{_html_text(acct_name)}</div></div>{pill}</div>'
            f'<div class="scost-main">余额 {_html_text(pub(account["balance"]))}</div>'
            f'<div class="smeta">{smeta}</div>'
            f'<div class="sub-box"><div class="meta">{box_label}：{_html_text(covered)}</div></div>'
            f'{foot_html}</div>'
        )
    return (
        '<div class="section-head"><h2>💰 土区预存余额与扣费计划</h2>'
        f'<span class="section-count">{len(prepaid_accounts)} 个账号</span></div>'
        f'<div class="prepaid-grid">{"".join(cards)}</div>'
        '<div class="sub">TRY 为土耳其里拉。余额为所示日期的人工记录；扣费计划按每次金额不变估算，未来扣款尚未记为支出。</div>'
    )


def _ledger_table_html(ledger: list[dict], pub) -> str:
    rows_html = []
    limit = 8
    recent_items = list(reversed(ledger))
    for idx, item in enumerate(recent_items):
        amount = pub(item["amount"])
        raw_amt = amount.strip()
        if raw_amt.startswith("-") or any(k in raw_amt for k in ("回款", "冲抵", "获赠")):
            amt_cls = "amt credit"
        elif raw_amt in {"0", "0 元"} or raw_amt.startswith("0 元"):
            amt_cls = "amt zero"
        else:
            amt_cls = "amt"
        extra_attr = ' class="ledger-extra" hidden' if idx >= limit else ""
        rows_html.append(
            f"<tr{extra_attr}><td class='ldate'>{_html_text(item['date'])}</td>"
            f"<td class='litem'>{_svc_icon(item['item'])} {_html_text(pub(item['item']))}</td>"
            f"<td class='{amt_cls}'>{_html_text(amount)}</td></tr>"
        )
    toggle_html = ""
    if len(recent_items) > limit:
        toggle_html = (
            f'<div class="ledger-footer">'
            f'<button type="button" class="ledger-toggle" id="ledgerToggle" '
            f'data-total="{len(recent_items)}" data-limit="{limit}">'
            f'展开全部 {len(recent_items)} 笔明细 ↓</button></div>'
            '<script>(()=>{const btn=document.getElementById("ledgerToggle");if(!btn)return;'
            'let expanded=false;btn.addEventListener("click",()=>{'
            'expanded=!expanded;'
            'document.querySelectorAll("tr.ledger-extra").forEach(tr=>{tr.hidden=!expanded;});'
            'btn.textContent=expanded?"收起历史明细 ↑":`展开全部 ${btn.dataset.total} 笔明细 ↓`;'
            '});})();</script>'
        )
    return (
        '<div class="section-head"><h2>📒 消费台账（历史充值）</h2>'
        f'<span class="section-count">共 {len(ledger)} 笔 · 按时间倒序</span></div>'
        '<div class="table-wrap"><table class="ledger">'
        '<thead><tr><th>日期</th><th>项目</th><th class="amt">金额</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table></div>{toggle_html}'
    )


def _ordered_replacements(cfg: dict | None) -> list[tuple[str, str]]:
    return sorted(
        ((cfg or {}).get("public_replacements") or {}).items(),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )


def _apply_replacements(text: str, replacements: list[tuple[str, str]]) -> str:
    out = text
    for src, dst in replacements:
        out = out.replace(src, dst)
    return out


def _nonai_cards_html(non_ai: list[str], pub) -> str:
    cards = []
    for raw in non_ai:
        text = pub(raw)
        title, desc = text.split("：", 1) if "：" in text else (text, "")
        days = _nonai_expiry_days(desc)
        if "不续" in desc or "已到期" in desc:
            pill = "<span class='cd neutral'>已停续</span>"
        elif desc.startswith("0 元") or "白嫖" in desc:
            pill = "<span class='cd green'>0 元</span>"
        else:
            pill = _countdown_pill(_extract_expiry_date(desc))
        urgent_cls = " urgent" if days is not None and days < 30 else ""
        desc_html = f'<div class="nonai-desc">{_html_text(desc)}</div>' if desc else ""
        cards.append(
            f'<article class="scard nonai-card{urgent_cls}" aria-label="{_html_text(text)}">'
            f'<div class="top"><div class="sname">{_svc_icon(title)} {_html_text(title)}</div>{pill}</div>'
            f'{desc_html}</article>'
        )
    return (
        '<div class="section-head"><h2>🧾 非 AI 固定订阅</h2>'
        f'<span class="section-count">共 {len(non_ai)} 项</span></div>'
        f'<div class="grid nonai-grid">{"".join(cards)}</div>'
    )


def _subs_html(
    public: bool,
    replacements: list[tuple[str, str]] | None = None,
) -> str:
    fin = _load_finance()
    cfg = _load_finance_config() if (public and replacements is None) else {}
    prepaid_accounts = fin.get("prepaid", [])
    if public:
        prepaid_accounts = [
            {**account, "name": account["public_name"]}
            for account in prepaid_accounts if account.get("public_name")
        ]
    if not fin["subs"] and not fin["ledger"] and not prepaid_accounts:
        return ""

    active_replacements = (
        replacements if replacements is not None else _ordered_replacements(cfg)
    ) if public else []

    def pub(s: str) -> str:
        return _apply_replacements(s, active_replacements) if public else s

    visible_subs = [r for r in fin["subs"] if not _quota_hidden(r["name"])]
    kpi_strip = _subs_kpi_strip_html(fin, visible_subs, prepaid_accounts, pub)
    sub_cards = "".join(_sub_card_html(r, pub) for r in visible_subs)
    prepaid_html = _prepaid_cards_html(prepaid_accounts, pub)
    ledger_html = _ledger_table_html(fin["ledger"], pub)
    nonai_html = _nonai_cards_html(fin["non_ai"], pub)

    return (
        f'{kpi_strip}'
        '<div class="section-head"><h2>💳 订阅 · 到期 · 续费</h2>'
        f'<span class="section-count">共 {len(visible_subs)} 项</span></div>'
        f'<div class="grid">{sub_cards}</div>'
        f'{prepaid_html}'
        f'{nonai_html}'
        f'{ledger_html}'
    )


PUBLIC_LABELS = {"Kimi · 本人（948/年）": "Kimi · 主账号"}

_HIST_PUBLIC = {"Kimi · 本人": "Kimi · 主账号"}


def _mask_email(text) -> str:
    """网页不暴露完整邮箱：保留首字符，其余打码。"""
    return re.sub(r"([\w.+-])[\w.+-]*(?=@)", r"\1***", str(text))


def _public_name(
    name: str,
    replacements: list[tuple[str, str]] | None = None,
) -> str:
    mapped = PUBLIC_LABELS.get(name, _HIST_PUBLIC.get(name, name))
    if replacements:
        mapped = _apply_replacements(mapped, replacements)
    return _mask_email(mapped)


def _reset_marker_minutes(raw: str | None) -> int | None:
    """把周重置标记归一化为闰年内分钟数，不依赖日志采集年份。"""
    if not raw:
        return None
    s = str(raw).strip()
    m = re.search(r"(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        month, day, hh, mm = (int(x) for x in m.groups())
    else:
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{1,2}):(\d{2})", s)
        if not m or m.group(1) not in _MONTHS:
            return None
        month = _MONTHS[m.group(1)]
        day, hh, mm = (int(x) for x in m.groups()[1:])
    try:
        marker = dt.datetime(2000, month, day, hh, mm)
    except ValueError:
        return None
    return int((marker - dt.datetime(2000, 1, 1)).total_seconds() // 60)


def _is_weekly_reset(previous: tuple[str, float, str], current: tuple[str, float, str]) -> bool:
    """判断相邻快照间是否跨过周窗；优先使用重置时间，缺失时才看明显回落。"""
    _, previous_pct, previous_reset = previous
    _, current_pct, current_reset = current
    previous_marker = _reset_marker_minutes(previous_reset)
    current_marker = _reset_marker_minutes(current_reset)
    if previous_marker is not None and current_marker is not None:
        minutes_per_leap_year = 366 * 24 * 60
        advance = (current_marker - previous_marker) % minutes_per_leap_year
        return 5 * 24 * 60 <= advance <= 9 * 24 * 60
    return previous_pct - current_pct >= 20.0


def _daily_deltas(days: int = 7, *, public: bool = False) -> list[tuple[str, list[dict]]]:
    """从 LOG_CSV 计算每源每日消耗（周计数口径）。

    普通日为当天末次快照 − 前一日末次快照；跨周重置时按重置前、后两段相加。
    底账首日或快照中断后的首日无连续前值，从当天首次快照起算（partial 标记）。
    返回 [(日期, [行...]), ...]，新日期在前。
    """
    if not LOG_CSV.exists():
        return []
    hidden_names = _public_hidden_codex_names() if public else set()
    by_day: dict[str, dict[str, list[tuple[str, float, str]]]] = {}
    dates_by_name: dict[str, list[str]] = {}
    with open(LOG_CSV, encoding="utf-8") as f:
        for r in csv.reader(f):
            if len(r) < 4 or not r[0] or not r[0][0].isdigit() or r[2] != "ok":
                continue
            try:
                pct = float(r[3])
            except ValueError:
                continue
            name = r[1].split("（")[0].strip()  # 合并标签改名（如 Kimi 本人 99/月→948/年）
            if name == "Gemini · Cloud (Claude)":
                name = "Gemini · Claude"
            elif name == "Gemini · 3.8 Flash":
                name = "Gemini 4 Hydrogen"
            if _quota_hidden(name) or name.casefold() in hidden_names:
                continue
            day_str = r[0][:10]
            reset = r[5] if len(r) > 5 else ""
            day_map = by_day.setdefault(day_str, {})
            if name not in day_map:
                day_map[name] = []
                dates_by_name.setdefault(name, []).append(day_str)
            day_map[name].append((r[0], pct, reset))
    prev_day_for: dict[tuple[str, str], str] = {}
    for name, day_list in dates_by_name.items():
        sorted_days = sorted(day_list)
        for prev_d, curr_d in zip(sorted_days, sorted_days[1:]):
            prev_day_for[(curr_d, name)] = prev_d
    dates = sorted(by_day, reverse=True)[:days]
    out = []
    for d in dates:
        rows = []
        for name, snaps in sorted(by_day[d].items()):
            snaps.sort()
            previous_day = prev_day_for.get((d, name))
            try:
                is_consecutive = (
                    previous_day is not None
                    and dt.date.fromisoformat(d) - dt.date.fromisoformat(previous_day) == dt.timedelta(days=1)
                )
            except ValueError:
                is_consecutive = False
            if is_consecutive:
                previous = sorted(by_day[previous_day][name])[-1]
                sequence = [previous, *snaps]
                partial = False
            else:
                if len(snaps) < 2:
                    continue
                sequence = snaps
                partial = True

            delta = 0.0
            segment_start = sequence[0][1]
            reset = False
            for previous, current in zip(sequence, sequence[1:]):
                if _is_weekly_reset(previous, current):
                    delta += max(0.0, previous[1] - segment_start)
                    segment_start = 0.0
                    reset = True
            delta += max(0.0, sequence[-1][1] - segment_start)
            rows.append({"name": name, "delta": round(delta, 1),
                         "reset": reset, "partial": partial})
        out.append((d, rows))
    return out


def _history_html(public: bool, days: int = 7, controls: bool = False,
                  more_href: str = "",
                  replacements: list[tuple[str, str]] | None = None) -> str:
    """每日消耗柱状图：按天分块，源按消耗降序，纯 CSS 横条。"""
    history_days = _daily_deltas(days=days, public=public)
    if not history_days:
        return ""
    if public and replacements is None:
        replacements = _ordered_replacements(_load_finance_config())
    blocks = []
    source_names: set[str] = set()
    for day_index, (d, rows) in enumerate(history_days):
        shown = [r for r in rows if r["delta"] > 0.05]
        resets = [r for r in rows if r["reset"] and r["delta"] <= 0.05]
        lines = []
        if shown:
            mx = max(r["delta"] for r in shown)
            for r in sorted(shown, key=lambda x: -x["delta"]):
                name = _public_name(r["name"], replacements) if public else r["name"]
                source_names.add(name)
                w = max(3, round(r["delta"] / mx * 100))
                tags = ("*" if r["partial"] else "") + (" ↺" if r["reset"] else "")
                lines.append(
                    f'<div class="hrow" data-source="{_html_text(name)}">'
                    f'<span class="hname" title="{_html_text(name)}">{_html_text(name)}</span>'
                    f'<div class="hbar"><div class="hfill" style="width:{w}%"></div></div>'
                    f'<span class="hval">+{r["delta"]:g}%{tags}</span></div>')
        else:
            lines.append('<div class="hrow dim">当日未记录到消耗</div>')
        for r in resets:
            name = _public_name(r["name"], replacements) if public else r["name"]
            source_names.add(name)
            lines.append(
                f'<div class="hrow dim" data-source="{_html_text(name)}">'
                f'{_html_text(name)} ↺ 周重置</div>')
        day_meta = f'<span class="hdate-meta">{len(shown)} 项有消耗</span>' if shown else ""
        blocks.append(
            f'<div class="hday" data-day-index="{day_index}">'
            f'<div class="hdate"><span class="hdate-day">{_html_text(d[5:])}</span>{day_meta}</div>'
            f'{"".join(lines)}</div>')

    tools = ""
    if controls:
        options = "".join(
            f'<option value="{_html_text(name)}">{_html_text(name)}</option>'
            for name in sorted(source_names)
        )
        tools = f'''<div class="history-tools">
<div class="range-switch" aria-label="历史范围">
<button type="button" class="active" data-days="7">最近 7 天</button>
<button type="button" data-days="30">最近 30 天</button>
</div>
<label class="source-filter">账号
<select id="sourceFilter"><option value="">全部账号</option>{options}</select>
</label></div>'''
    more = (
        f'<a class="more-link" href="{_html_text(more_href)}">查看 30 天完整记录 →</a>'
        if more_href else ""
    )
    empty_box = (
        '<div id="historyEmpty" class="hday" hidden>'
        '<div class="hrow dim">所选时间范围内暂无该账号消耗记录</div></div>'
        if controls else ""
    )
    return ('<div class="section-head"><h2>📊 每日消耗（周计数口径）</h2>' + more + '</div>'
            + tools + "".join(blocks) + empty_box
            + '<div class="sub">普通日 = 当天末次快照 − 前一日末次快照；↺ 跨周重置日按重置前后分段相加；'
              '* 底账首日或快照中断后从当天首次快照起算；短时窗口（5h / 当前时段）不计；'
              '底账随快照积累逐日丰富</div>')


def _nav_html(active: str) -> str:
    links = (
        ("overview", "index.html", "额度概览"),
        ("history", "history.html", "每日消耗"),
        ("subscriptions", "subscriptions.html", "订阅账单"),
    )
    items = []
    for key, href, label in links:
        current = ' class="active" aria-current="page"' if key == active else ""
        items.append(f'<a href="{href}"{current}>{label}</a>')
    return '<nav class="nav" aria-label="页面导航">' + "".join(items) + "</nav>"


def _freshness_html(now: str, *, auto_update: bool = False) -> str:
    """概览页原位更新；其他页保留筛选和阅读状态，并提示新版。"""
    return f'''<div id="updateNotice" class="update-notice" data-current="{_html_text(now)}" hidden>
发现有新数据 <button type="button" id="reloadLatest">点击更新</button></div>
<script>
(()=>{{
  const notice=document.getElementById('updateNotice');
  let current=notice.dataset.current;
  let checking=false;
  async function checkLatest(){{
    if(checking || document.hidden) return;
    checking=true;
    try{{
      const url=new URL(location.href);
      url.searchParams.set('_quota_check',Math.floor(Date.now()/60000));
      const response=await fetch(url,{{cache:'no-store',signal:AbortSignal.timeout(15000)}});
      if(!response.ok) return;
      const updated=new DOMParser().parseFromString(await response.text(),'text/html');
      const latest=updated.querySelector('meta[name="quota-generated-at"]')?.content;
      if(!latest || latest<=current) return;
      const content=document.getElementById('quotaContent');
      const replacement=updated.getElementById('quotaContent');
      const timestamp=updated.querySelector('.ts');
      if({str(auto_update).lower()} && content && replacement && timestamp){{
        const scrollY=window.scrollY;
        content.replaceChildren(...replacement.childNodes);
        document.querySelector('.ts').textContent=timestamp.textContent;
        document.title=updated.title;
        document.querySelector('meta[name="quota-generated-at"]').content=latest;
        current=latest;
        notice.dataset.current=latest;
        notice.hidden=true;
        window.__applyOverviewFilter?.();
        window.scrollTo(0,scrollY);
      }}else{{
        notice.hidden=false;
      }}
    }}catch(_error){{}}finally{{checking=false;}}
  }}
  document.getElementById('reloadLatest').addEventListener('click',()=>{{
    const url=new URL(location.href);
    url.searchParams.set('_quota_check',Date.now());
    location.replace(url);
  }});
  document.addEventListener('visibilitychange',()=>{{if(!document.hidden) checkLatest();}});
  setInterval(checkLatest,60*1000);
  checkLatest();
}})();
</script>'''


def _history_filter_script() -> str:
    return '''<script>
(()=>{
  const buttons=[...document.querySelectorAll('[data-days]')];
  const source=document.getElementById('sourceFilter');
  const emptyBox=document.getElementById('historyEmpty');
  function applyFilters(){
    const limit=Number(document.querySelector('[data-days].active')?.dataset.days || 7);
    const selected=source?.value || '';
    let visibleDays=0;
    document.querySelectorAll('.hday[data-day-index]').forEach(day=>{
      const inRange=Number(day.dataset.dayIndex)<limit;
      let hasVisibleRow=false;
      day.querySelectorAll('.hrow').forEach(row=>{
        const matches=!selected || row.dataset.source===selected;
        row.hidden=!inRange || !matches;
        if(!row.hidden) hasVisibleRow=true;
      });
      const meta=day.querySelector('.hdate-meta');
      if(meta) meta.hidden=Boolean(selected);
      day.hidden=!inRange || !hasVisibleRow;
      if(!day.hidden) visibleDays+=1;
    });
    if(emptyBox) emptyBox.hidden=visibleDays>0;
  }
  buttons.forEach(button=>button.addEventListener('click',()=>{
    buttons.forEach(other=>other.classList.toggle('active',other===button));
    applyFilters();
  }));
  source?.addEventListener('change',applyFilters);
  applyFilters();
})();
</script>'''


def _overview_filter_bar(visible_rows: list[dict], alerts: list[dict]) -> str:
    if len(visible_rows) <= 3:
        return ""
    alert_names = {a.get("name") for a in alerts}
    counts: dict[str, int] = {}
    attn_count = 0
    for r in visible_rows:
        pkey, _, _ = _provider_meta(r.get("name", ""))
        group_key = "gemini" if pkey in {"gemini", "argon"} else pkey
        counts[group_key] = counts.get(group_key, 0) + 1
        wrem = _remain_pct(r, "used_pct")
        fp = r.get("fiveh_pct")
        if (
            r.get("status") != "ok"
            or r.get("name") in alert_names
            or r.get("stale")
            or r.get("cache_fallback")
            or (wrem is not None and wrem <= 20)
            or (fp is not None and fp >= 80)
        ):
            attn_count += 1
    buttons = [
        f'<button type="button" class="filter-pill active" data-filter="all">全部 ({len(visible_rows)})</button>'
    ]
    if attn_count:
        buttons.append(
            f'<button type="button" class="filter-pill warn" data-filter="attn">⚡ 关注 ({attn_count})</button>'
        )
    for key, label in (
        ("codex", "⚡ Codex"),
        ("kimi", "🌙 Kimi"),
        ("gemini", "✨ Gemini"),
        ("claude", "🎭 Claude"),
        ("minimax", "🎬 MiniMax"),
    ):
        if counts.get(key):
            buttons.append(
                f'<button type="button" class="filter-pill" data-filter="{key}">{label} ({counts[key]})</button>'
            )
    return f'<div class="filter-bar" role="toolbar" aria-label="按模型筛选">{"".join(buttons)}</div>'


def _overview_filter_script() -> str:
    return '''<script>
(()=>{
  window.__quotaFilter=window.__quotaFilter||'all';
  function matchesCard(card,mode){
    if(mode==='all') return true;
    if(mode==='attn') return card.dataset.state==='attn';
    const p=card.dataset.provider||'';
    if(mode==='gemini') return p==='gemini'||p==='argon';
    return p===mode;
  }
  window.__applyOverviewFilter=function(){
    const mode=window.__quotaFilter||'all';
    document.querySelectorAll('.filter-bar [data-filter]').forEach(btn=>{
      btn.classList.toggle('active',btn.dataset.filter===mode);
    });
    document.querySelectorAll('.grid .card[data-provider]').forEach(card=>{
      card.hidden=!matchesCard(card,mode);
    });
  };
  document.addEventListener('click',ev=>{
    const btn=ev.target.closest('.filter-bar [data-filter]');
    if(!btn) return;
    window.__quotaFilter=btn.dataset.filter||'all';
    window.__applyOverviewFilter();
  });
  window.__applyOverviewFilter();
})();
</script>'''


def render_html(rows: list[dict], path: Path | None = None, live: bool = False,
                public: bool = False, page: str = "all") -> str:
    if page not in {"all", "overview", "history", "subscriptions"}:
        raise ValueError(f"unknown dashboard page: {page}")
    hidden_names = _public_hidden_codex_names() if public else set()
    replacements = _ordered_replacements(_load_finance_config()) if public else []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    if page in {"all", "overview"}:
        visible_rows = _visible_quota_rows(rows)
        if public:
            visible_rows = [
                row for row in visible_rows
                if (row.get("name") or "").casefold() not in hidden_names
            ]
        visible_rows = _sort_rows(list(visible_rows))
        if public:
            visible_rows = [
                {
                    **r,
                    "name": _public_name(r["name"], replacements),
                    **({"note": _apply_replacements(r["note"], replacements)} if r.get("note") else {}),
                    **({"status": _apply_replacements(r["status"], replacements)} if r.get("status") else {}),
                }
                for r in visible_rows
            ]
        alerts = _alerts(visible_rows)
        alert_by_name = {a["name"]: a for a in alerts}
        cards = "".join(_card(r, alert_by_name.get(r["name"])) for r in visible_rows)
        banner = _alerts_html(alerts)
        summary_strip = _summary_strip_html(visible_rows, alerts)
        filter_bar = _overview_filter_bar(visible_rows, alerts)
        filter_script = _overview_filter_script() if filter_bar else ""
        live_ui = """
<div style="margin:14px 0"><button id="rb" onclick="doRefresh()" style="background:linear-gradient(180deg,#2ea043 0%,#238636 100%);color:#fff;border:1px solid rgba(255,255,255,.15);border-radius:10px;padding:8px 18px;font-size:13.5px;font-weight:600;cursor:pointer;box-shadow:0 4px 12px rgba(35,134,54,.3)">🔄 立即刷新全部额度</button>
<span id="st" style="font-size:12.5px;color:#8b949e;margin-left:10px"></span></div>
<script>
async function doRefresh(){
  const st=document.getElementById('st');st.textContent=' 查询中（约1分钟，期间可切走）…';
  document.getElementById('rb').disabled=true;
  try{await fetch('/api/refresh');poll();}catch(e){st.textContent=' 仅本地服务模式可刷新';document.getElementById('rb').disabled=false;}
}
async function poll(){
  try{const r=await fetch('/api/state');const d=await r.json();
    if(d.updating){setTimeout(poll,2000);}else{location.reload();}
  }catch(e){setTimeout(poll,3000);}
}
</script>""" if live else ""
    else:
        cards = banner = summary_strip = filter_bar = filter_script = live_ui = ""

    if page == "overview":
        heading = "🤖 AI 额度监控"
        title = "AI 额度监控"
        detail = "卡片按周剩余从小到大"
        content = (
            f'{live_ui}{summary_strip}{banner}{filter_bar}<div class="grid">{cards}</div>'
            f'{_history_html(public, days=2, more_href="history.html", replacements=replacements)}'
        )
    elif page == "history":
        heading = "📊 每日消耗"
        title = "每日消耗 · AI 额度监控"
        detail = "默认最近 7 天，可切换 30 天并按账号筛选"
        content = _history_html(public, days=30, controls=True, replacements=replacements) + _history_filter_script()
    elif page == "subscriptions":
        heading = "💳 订阅账单"
        title = "订阅账单 · AI 额度监控"
        detail = "订阅、历史充值与非 AI 固定支出"
        content = _subs_html(public, replacements=replacements)
    else:
        heading = "🤖 AI 额度监控"
        title = "AI 额度监控"
        detail = "卡片按周剩余从小到大"
        content = (
            f'{live_ui}{summary_strip}{banner}{filter_bar}<div class="grid">{cards}</div>'
            f'{_history_html(public, replacements=replacements)}{_subs_html(public, replacements=replacements)}'
        )

    split_site = page != "all"
    navigation = _nav_html(page) if split_site else ""
    freshness = _freshness_html(now, auto_update=page == "overview") if split_site else ""
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{title} · {now}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="quota-generated-at" content="{now}">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🤖</text></svg>">
<style>
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{background:radial-gradient(ellipse 90% 55% at 50% -18%,rgba(56,139,253,.16),transparent 65%),radial-gradient(ellipse 60% 40% at 85% 10%,rgba(163,113,247,.08),transparent 60%),#090d13;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","PingFang SC","Hiragino Sans GB",system-ui,sans-serif;padding:28px 28px 56px;max-width:1180px;margin:0 auto;line-height:1.45;-webkit-font-smoothing:antialiased}}
:focus-visible{{outline:2px solid #58a6ff;outline-offset:2px}}
.page-head{{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 20px;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid rgba(240,246,252,.07)}}
h1{{font-size:26px;font-weight:750;letter-spacing:-.025em;margin:0 0 6px;color:#f0f6fc;display:flex;align-items:center;gap:10px}}
.ts{{position:relative;padding-left:15px;color:#8b949e;font-size:13px;margin:0;line-height:1.5}}
.ts::before{{content:"";position:absolute;left:0;top:.48em;width:7px;height:7px;border-radius:99px;background:#3fb950;box-shadow:0 0 8px rgba(63,185,80,.75)}}
h2{{font-size:17.5px;font-weight:650;letter-spacing:-.015em;color:#f0f6fc;margin:34px 0 14px}}
a{{color:#58a6ff;text-decoration:none;transition:color .15s}} a:hover{{color:#79c0ff;text-decoration:underline}}
.nav{{position:sticky;top:12px;z-index:20;display:flex;gap:6px;width:max-content;max-width:100%;padding:5px;margin:0 0 22px;background:rgba(17,22,29,.84);border:1px solid rgba(240,246,252,.11);border-radius:13px;backdrop-filter:blur(18px) saturate(180%);-webkit-backdrop-filter:blur(18px) saturate(180%);box-shadow:0 10px 28px rgba(0,0,0,.38)}}
.nav a{{color:#8b949e;padding:7px 15px;border-radius:9px;font-size:13px;font-weight:600;white-space:nowrap;transition:all .18s ease}}
.nav a:hover{{color:#f0f6fc;text-decoration:none;background:rgba(110,118,129,.16)}}
.nav a.active{{color:#fff;background:linear-gradient(180deg,#2ea043 0%,#238636 100%);box-shadow:0 2px 8px rgba(35,134,54,.38),inset 0 1px 0 rgba(255,255,255,.18)}}
.update-notice{{position:fixed;right:20px;bottom:20px;z-index:30;background:linear-gradient(135deg,#1f6feb,#388bfd);color:#fff;border:1px solid rgba(255,255,255,.15);border-radius:12px;padding:10px 14px;font-size:13px;font-weight:500;box-shadow:0 10px 30px rgba(0,0,0,.45)}}
.update-notice button{{margin-left:10px;border:0;border-radius:7px;padding:5px 10px;background:#fff;color:#0969da;cursor:pointer;font-weight:600;transition:transform .15s}}
.update-notice button:hover{{transform:scale(1.03)}}
[hidden]{{display:none!important}}
.summary-strip{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:0 0 20px}}
.summary-strip.subs-kpi{{grid-template-columns:repeat(4,minmax(0,1fr))}}
.kpi{{background:linear-gradient(180deg,rgba(24,30,40,.9) 0%,rgba(16,20,28,.95) 100%);border:1px solid rgba(48,54,61,.85);border-radius:14px;padding:13px 16px;display:flex;flex-direction:column;gap:3px;box-shadow:0 4px 14px rgba(0,0,0,.18),inset 0 1px 0 rgba(255,255,255,.04)}}
.kpi-label{{font-size:11.5px;font-weight:600;color:#8b949e;letter-spacing:.02em}}
.kpi-val{{font-size:24px;font-weight:750;color:#f0f6fc;letter-spacing:-.03em;line-height:1.15;font-variant-numeric:tabular-nums;word-break:break-word}}
.kpi-sub{{font-size:11px;color:#6e7681;margin-top:1px;line-height:1.35}}
.kpi.ok .kpi-val{{color:#3fb950}}
.kpi.warn .kpi-val{{color:#f0883e}}
.kpi.bad .kpi-val{{color:#ff7b72}}
.kpi.accent .kpi-val{{color:#58a6ff}}
table{{border-collapse:separate;border-spacing:0;width:100%;background:rgba(22,27,34,.85);border:1px solid rgba(48,54,61,.85);border-radius:14px;overflow:hidden}}
td{{border-bottom:1px solid rgba(48,54,61,.6);padding:10px 14px;font-size:14px}}
tr:last-child td{{border-bottom:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px;align-items:stretch}}
.prepaid-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:16px;align-items:stretch}}
.card,.scard{{position:relative;overflow:hidden;display:flex;flex-direction:column;background:linear-gradient(180deg,rgba(26,32,43,.94) 0%,rgba(16,21,29,.98) 100%);border:1px solid rgba(56,63,73,.78);border-radius:16px;padding:18px 20px;box-shadow:0 6px 22px rgba(0,0,0,.26),inset 0 1px 0 rgba(255,255,255,.05);transition:transform .2s cubic-bezier(.16,1,.3,1),border-color .2s ease,box-shadow .2s ease}}
.card::before,.scard::before{{content:"";position:absolute;top:0;left:0;right:0;height:2.5px;background:linear-gradient(90deg,rgba(88,166,255,.65),rgba(88,166,255,.1));opacity:.85}}
.card[data-provider="codex"]::before,.scard[data-provider="codex"]::before{{background:linear-gradient(90deg,#10b981,#34d399)}}
.card[data-provider="claude"]::before,.scard[data-provider="claude"]::before{{background:linear-gradient(90deg,#f97316,#fb923c)}}
.card[data-provider="gemini"]::before,.scard[data-provider="gemini"]::before{{background:linear-gradient(90deg,#3b82f6,#60a5fa)}}
.card[data-provider="argon"]::before,.scard[data-provider="argon"]::before{{background:linear-gradient(90deg,#8b5cf6,#a78bfa)}}
.card[data-provider="kimi"]::before,.scard[data-provider="kimi"]::before{{background:linear-gradient(90deg,#06b6d4,#38bdf8)}}
.card[data-provider="minimax"]::before,.scard[data-provider="minimax"]::before{{background:linear-gradient(90deg,#ec4899,#f472b6)}}
.scard[data-provider="zhipu"]::before{{background:linear-gradient(90deg,#6366f1,#818cf8)}}
.scard[data-provider="proxy"]::before{{background:linear-gradient(90deg,#f59e0b,#fbbf24)}}
.scard[data-provider="deepseek"]::before{{background:linear-gradient(90deg,#0ea5e9,#38bdf8)}}
.scard[data-provider="apple"]::before{{background:linear-gradient(90deg,#94a3b8,#e2e8f0)}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px}}
.title-wrap{{display:flex;flex-direction:column;gap:4px;min-width:0}}
.pbadge{{display:inline-flex;align-items:center;gap:4px;width:max-content;font-size:10.5px;font-weight:600;color:#8b949e;background:rgba(110,118,129,.13);border:1px solid rgba(240,246,252,.07);padding:2px 7px;border-radius:6px;letter-spacing:.02em}}
[data-provider="claude"] .pbadge{{color:#fdba74;background:rgba(249,115,22,.12);border-color:rgba(249,115,22,.24)}}
[data-provider="gemini"] .pbadge{{color:#93c5fd;background:rgba(59,130,246,.12);border-color:rgba(59,130,246,.24)}}
[data-provider="argon"] .pbadge{{color:#c4b5fd;background:rgba(139,92,246,.12);border-color:rgba(139,92,246,.24)}}
[data-provider="codex"] .pbadge{{color:#6ee7b7;background:rgba(16,185,129,.12);border-color:rgba(16,185,129,.24)}}
[data-provider="kimi"] .pbadge{{color:#7dd3fc;background:rgba(6,182,212,.12);border-color:rgba(6,182,212,.24)}}
[data-provider="minimax"] .pbadge{{color:#f9a8d4;background:rgba(236,72,153,.12);border-color:rgba(236,72,153,.24)}}
[data-provider="zhipu"] .pbadge{{color:#a5b4fc;background:rgba(99,102,241,.12);border-color:rgba(99,102,241,.24)}}
[data-provider="proxy"] .pbadge{{color:#fcd34d;background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.24)}}
[data-provider="deepseek"] .pbadge{{color:#7dd3fc;background:rgba(14,165,233,.12);border-color:rgba(14,165,233,.24)}}
[data-provider="apple"] .pbadge{{color:#e2e8f0;background:rgba(148,163,184,.14);border-color:rgba(148,163,184,.28)}}
.name{{font-size:15.5px;font-weight:650;color:#f0f6fc;letter-spacing:-.01em;line-height:1.3;word-break:break-word}}
.win-label{{font-size:11px;font-weight:600;color:#8b949e;letter-spacing:.01em;margin-bottom:2px}}
.pill{{font-size:11px;font-weight:600;padding:3px 9px;border-radius:99px;letter-spacing:.02em;flex-shrink:0;border:1px solid transparent;margin-top:2px}}
.pill.ok{{background:rgba(63,185,80,.12);color:#3fb950;border-color:rgba(63,185,80,.28)}}
.pill.bad{{background:rgba(248,81,73,.14);color:#ff7b72;border-color:rgba(248,81,73,.32)}}
.pill.stale{{background:rgba(139,148,158,.14);color:#8b949e;border-color:rgba(139,148,158,.28)}}
.pill.warn{{background:rgba(240,136,62,.14);color:#f0883e;border-color:rgba(240,136,62,.32)}}
.card.urgent{{border-color:rgba(240,136,62,.55);background:linear-gradient(180deg,rgba(240,136,62,.08) 0%,rgba(16,21,29,.98) 48%)}}
.scard.urgent{{border-color:rgba(248,81,73,.42);background:linear-gradient(180deg,rgba(248,81,73,.07) 0%,rgba(16,21,29,.98) 48%)}}
.stat-row{{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin:2px 0 9px}}
.pct{{font-size:36px;font-weight:750;letter-spacing:-1.5px;line-height:1;font-variant-numeric:tabular-nums}}
.remain{{font-size:12.5px;font-weight:600;color:#c9d1d9;background:rgba(110,118,129,.15);border:1px solid rgba(240,246,252,.08);padding:3px 9px;border-radius:7px;font-variant-numeric:tabular-nums}}
.alertband{{background:linear-gradient(135deg,rgba(240,136,62,.13) 0%,rgba(210,153,34,.05) 100%);border:1px solid rgba(240,136,62,.38);border-left:4px solid #f0883e;border-radius:14px;padding:14px 18px;margin:0 0 18px;box-shadow:0 4px 16px rgba(0,0,0,.18)}}
.alertband h3{{margin:0 0 10px;font-size:14.5px;font-weight:650;color:#f0883e}}
.alertband ul{{margin:0;padding:0;list-style:none;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px 12px}}
.alertband li{{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:12.5px;color:#e6edf3;background:rgba(13,17,23,.52);border:1px solid rgba(240,136,62,.22);border-radius:10px;padding:7px 11px}}
.alert-name{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.alert-detail{{display:inline-flex;align-items:center;gap:6px;flex-shrink:0;font-variant-numeric:tabular-nums}}
.alert-win{{font-size:11px;font-weight:600;color:#d29922;background:rgba(210,153,34,.14);border:1px solid rgba(210,153,34,.25);padding:1px 6px;border-radius:5px}}
.alert-time{{color:#c9d1d9}}
.alert-rem{{color:#f0883e;font-weight:700}}
.filter-bar{{display:flex;align-items:center;gap:8px;margin:0 0 16px;overflow-x:auto;padding-bottom:2px;scrollbar-width:none}}
.filter-bar::-webkit-scrollbar{{display:none}}
.filter-pill{{appearance:none;border:1px solid rgba(56,63,73,.85);background:rgba(22,27,34,.85);color:#9ca3af;border-radius:99px;padding:6px 13px;font-size:12.5px;font-weight:600;white-space:nowrap;cursor:pointer;transition:all .16s ease}}
.filter-pill:hover{{border-color:rgba(88,166,255,.55);color:#f0f6fc;background:rgba(30,38,48,.95)}}
.filter-pill.active{{background:linear-gradient(180deg,#1f6feb 0%,#1a5bd0 100%);border-color:rgba(88,166,255,.65);color:#fff;box-shadow:0 2px 10px rgba(31,111,235,.32)}}
.filter-pill.warn.active{{background:linear-gradient(180deg,#d97706 0%,#b45309 100%);border-color:rgba(240,136,62,.75);box-shadow:0 2px 10px rgba(240,136,62,.3)}}
.bar{{height:8px;background:rgba(110,118,129,.2);border-radius:99px;overflow:hidden;box-shadow:inset 0 1px 2px rgba(0,0,0,.35)}}
.bar.small{{height:6px;margin-top:6px}}
.fill{{height:100%;border-radius:99px;transition:width .5s cubic-bezier(.22,1,.36,1)}}
.meta{{font-size:12.5px;color:#c9d1d9;margin-top:8px;font-variant-numeric:tabular-nums}}
.main-meta{{margin-bottom:4px;color:#9ca3af}}
.sub-box{{background:rgba(11,15,21,.62);border:1px solid rgba(56,63,73,.68);border-radius:11px;padding:10px 12px;margin-top:11px}}
.sub-box.exhausted{{background:linear-gradient(180deg,rgba(248,81,73,.09) 0%,rgba(11,15,21,.68) 100%);border-color:rgba(248,81,73,.38)}}
.sub-box.single-win{{background:rgba(13,17,23,.36);border-style:dashed;border-color:rgba(48,54,61,.55)}}
.sub-head{{display:flex;align-items:center;justify-content:space-between;gap:8px}}
.sub-box .sub{{margin:0;font-size:11.5px;font-weight:600;color:#c9d1d9;letter-spacing:.01em}}
.sub-stats{{display:inline-flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}}
.sub-pct{{font-size:13px;font-weight:700}}
.sub-rem,.sub-tag{{font-size:10.5px;font-weight:600;color:#8b949e;background:rgba(110,118,129,.14);border:1px solid rgba(240,246,252,.06);padding:1px 6px;border-radius:5px}}
.sub-box .meta{{margin-top:6px;font-size:11.5px;color:#8b949e}}
.card:hover,.scard:hover{{transform:translateY(-2px);border-color:rgba(88,166,255,.55);box-shadow:0 12px 30px rgba(0,0,0,.38),0 0 0 1px rgba(88,166,255,.12)}}
.sname{{font-size:14.25px;font-weight:650;color:#f0f6fc;letter-spacing:-.01em;line-height:1.35;overflow-wrap:break-word;text-wrap:balance}}
.scost-main{{font-size:23px;font-weight:750;color:#58a6ff;letter-spacing:-.02em;line-height:1.25;margin:2px 0 12px;font-variant-numeric:tabular-nums}}
.scost-main.compact{{font-size:17.5px;line-height:1.35}}
.scost-main.compact.long{{font-size:15.5px;line-height:1.4}}
.scost-main.free{{color:#3fb950}}
.scost-note{{font-size:12px;color:#8b949e;line-height:1.5;background:rgba(11,15,21,.52);border:1px solid rgba(48,54,61,.6);border-radius:10px;padding:8px 11px;margin:0 0 12px}}
.prepaid-card .scost-main{{margin-bottom:4px}}
.prepaid-card .sub-box{{margin:12px 0}}
.prepaid-card .sub-box .meta{{margin-top:0;font-size:12.5px;color:#c9d1d9}}
.smeta{{font-size:12.5px;color:#8b949e;font-variant-numeric:tabular-nums}}
.cd{{font-size:11px;font-weight:600;padding:3px 9px;border-radius:99px;margin-left:6px;white-space:nowrap;border:1px solid transparent;flex-shrink:0}}
.top .cd{{margin-left:0;margin-top:2px}}
.cd.green{{background:rgba(63,185,80,.12);color:#3fb950;border-color:rgba(63,185,80,.28)}}
.cd.yellow{{background:rgba(210,153,34,.14);color:#d29922;border-color:rgba(210,153,34,.3)}}
.cd.red{{background:rgba(248,81,73,.14);color:#ff7b72;border-color:rgba(248,81,73,.32)}}
.cd.neutral{{background:rgba(110,118,129,.15);color:#9ca3af;border-color:rgba(240,246,252,.08)}}
.table-wrap{{overflow-x:auto;border:1px solid rgba(56,63,73,.78);border-radius:16px;background:linear-gradient(180deg,rgba(24,30,40,.9) 0%,rgba(15,20,27,.96) 100%);box-shadow:0 6px 22px rgba(0,0,0,.24)}}
table.ledger{{border:0;border-radius:0;background:transparent}}
table.ledger thead th{{background:rgba(11,15,21,.65);color:#8b949e;font-size:11.5px;font-weight:650;letter-spacing:.03em;padding:11px 16px;text-align:left;border-bottom:1px solid rgba(56,63,73,.72);white-space:nowrap}}
table.ledger thead th.amt{{text-align:right}}
table.ledger tbody tr{{transition:background .15s ease}}
table.ledger tbody tr:hover{{background:rgba(56,139,253,.06)}}
table.ledger td{{font-size:13.5px;padding:11px 16px;border-bottom:1px solid rgba(48,54,61,.5)}}
table.ledger td.ldate{{color:#8b949e;white-space:nowrap;width:118px;font-variant-numeric:tabular-nums}}
table.ledger td.litem{{color:#e6edf3;font-weight:500}}
table.ledger td.amt{{text-align:right;color:#f0883e;font-weight:650;white-space:nowrap;font-variant-numeric:tabular-nums}}
table.ledger td.amt.credit{{color:#3fb950}}
table.ledger td.amt.zero{{color:#8b949e}}
.ledger-footer{{display:flex;justify-content:center;margin-top:12px}}
.ledger-toggle{{appearance:none;border:1px solid rgba(56,63,73,.85);background:rgba(22,27,34,.9);color:#c9d1d9;border-radius:9px;padding:8px 16px;font-size:12.5px;font-weight:600;cursor:pointer;transition:all .16s ease}}
.ledger-toggle:hover{{border-color:#58a6ff;color:#f0f6fc;background:rgba(30,38,48,.95)}}
.nonai-card .top{{margin-bottom:6px}}
.nonai-desc{{font-size:12.5px;color:#9ca3af;line-height:1.55}}
.meta.dim{{color:#6e7681;font-size:11.5px;margin-top:auto;padding-top:11px;border-top:1px solid rgba(48,54,61,.45)}}
.card > .main-meta + .meta.dim{{margin-top:auto}}
.card > .sub-box + .meta.dim{{margin-top:11px}}
.sub{{font-size:11.5px;color:#8b949e;margin-top:12px;line-height:1.5}}
.msg{{font-size:13px;color:#d29922;margin-top:6px}}
.hday{{background:linear-gradient(180deg,rgba(28,34,44,.88) 0%,rgba(19,24,32,.94) 100%);border:1px solid rgba(48,54,61,.85);border-radius:14px;padding:14px 18px;margin-bottom:12px;box-shadow:0 2px 10px rgba(0,0,0,.16)}}
.hdate{{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:13px;font-weight:600;color:#8b949e;margin-bottom:10px;font-variant-numeric:tabular-nums}}
.hdate-meta{{font-size:11px;font-weight:600;color:#8b949e;background:rgba(110,118,129,.12);border:1px solid rgba(240,246,252,.06);padding:2px 8px;border-radius:99px}}
.hrow{{display:flex;align-items:center;gap:12px;margin:6px 0;font-size:13px}}
.hrow.dim{{color:#8b949e}}
.hname{{width:184px;flex-shrink:0;color:#c9d1d9;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.hbar{{flex:1;height:9px;background:rgba(110,118,129,.16);border-radius:99px;overflow:hidden}}
.hfill{{height:100%;background:linear-gradient(90deg,#1f6feb 0%,#58a6ff 100%);border-radius:99px;transition:width .35s ease}}
.hval{{width:76px;text-align:right;color:#58a6ff;font-weight:600;flex-shrink:0;font-variant-numeric:tabular-nums}}
.section-head{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:30px 0 12px}}
.section-head h2{{margin:0}}
.section-count{{font-size:11.5px;font-weight:600;color:#8b949e;background:rgba(110,118,129,.13);border:1px solid rgba(240,246,252,.07);padding:3px 10px;border-radius:99px;white-space:nowrap}}
.more-link{{font-size:13px;font-weight:500;white-space:nowrap}}
.history-tools{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 0 14px}}
.range-switch{{display:flex;gap:6px}}
.range-switch button,.source-filter select{{border:1px solid rgba(48,54,61,.9);border-radius:8px;background:#161b22;color:#c9d1d9;padding:7px 12px;font-size:12.5px;font-weight:500;transition:all .15s}}
.range-switch button{{cursor:pointer}}
.range-switch button:hover{{border-color:#58a6ff;color:#f0f6fc}}
.range-switch button.active{{background:#1f6feb;border-color:#1f6feb;color:#fff;box-shadow:0 2px 8px rgba(31,111,235,.35)}}
.source-filter{{display:flex;align-items:center;gap:8px;color:#8b949e;font-size:12.5px}}
@media(min-width:960px){{
  .grid{{grid-template-columns:repeat(3,minmax(0,1fr))}}
}}
@media(min-width:601px) and (max-width:959px){{
  .grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .summary-strip{{grid-template-columns:repeat(3,minmax(0,1fr))}}
  .summary-strip.subs-kpi{{grid-template-columns:repeat(2,minmax(0,1fr))}}
}}
@media(max-width:600px){{
  body{{padding:18px 14px 32px}}
  .nav{{top:6px;width:100%;justify-content:space-between}}
  .nav a{{padding:7px 9px;font-size:12.5px}}
  .card,.scard,.kpi,.title-wrap{{min-width:0}}
  .summary-strip,.summary-strip.subs-kpi{{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .grid,.prepaid-grid{{grid-template-columns:minmax(0,1fr)}}
  .section-head{{align-items:center}}
  table.ledger thead th,table.ledger td{{padding:9px 11px;font-size:12.5px}}
  table.ledger td.ldate{{width:86px;font-size:12px}}
  table.ledger td.amt{{white-space:normal;max-width:128px;line-height:1.35}}
  .history-tools{{align-items:stretch;flex-direction:column}}
  .source-filter select{{flex:1}}
  .alertband ul{{grid-template-columns:minmax(0,1fr)}}
  .alertband li{{flex-wrap:wrap;gap:6px 10px}}
  .hrow{{gap:7px}}
  .hname{{width:128px;font-size:12.5px}}
  .hval{{width:66px}}
}}
</style></head><body>
{navigation}
<header class="page-head"><div><h1>{heading}</h1>
<div class="ts">更新于 {now} · {"公开快照 · 每小时自动更新" if public else "统一口径：本周已用 / 周重置 · 数据源全部本机只读"} · {detail}</div></div></header>
<main id="quotaContent">{content}</main>
{freshness}
{filter_script}
</body></html>"""
    if path is not None:
        path.write_text(html, encoding="utf-8")
    return html


def render_public_site(rows: list[dict], index_path: Path) -> None:
    """生成 GitHub Pages 的概览、每日消耗和订阅账单三个静态页面。"""
    _public_hidden_codex_names()
    _load_finance_config()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    pages = {
        "overview": index_path,
        "history": index_path.with_name("history.html"),
        "subscriptions": index_path.with_name("subscriptions.html"),
    }
    for page, path in pages.items():
        render_html(rows, path, public=True, page=page)


# ---------- 本地服务模式 ----------

_STATE: dict = {"rows": [], "updating": False, "last": None}
_STATE_LOCK = threading.Lock()


def _refresh_state(log: bool = True) -> None:
    try:
        try:
            rows = collect()
        except sub2api_quota.ConfigError as exc:
            with _STATE_LOCK:
                if not _STATE["rows"]:
                    _STATE["rows"] = [{"name": "Sub2API", "status": str(exc)}]
                else:
                    _STATE["rows"] = [
                        {**row, "stale": True, "cache_fallback": True,
                         "note": "配置无效，保留上次快照"}
                        if row.get("status") == "ok" else row
                        for row in _STATE["rows"]
                    ]
            return
        except Exception as exc:  # noqa: BLE001
            with _STATE_LOCK:
                if not _STATE["rows"]:
                    _STATE["rows"] = [{"name": "监控服务", "status": f"刷新失败: {type(exc).__name__}"}]
                else:
                    _STATE["rows"] = [
                        {**row, "stale": True, "cache_fallback": True,
                         "note": "本次刷新失败，沿用上次快照"}
                        if row.get("status") == "ok" else row
                        for row in _STATE["rows"]
                    ]
            return
        with _STATE_LOCK:
            _STATE["rows"] = rows
            _STATE["last"] = dt.datetime.now().isoformat(timespec="seconds")
        if log:
            append_log(rows)
        try:
            render_html(rows, Path(__file__).with_name("quota-dashboard.html"))
        except Exception:  # noqa: BLE001
            pass
    finally:
        with _STATE_LOCK:
            _STATE["updating"] = False


def serve(port: int = 8788) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _send(self, body: str, ctype: str = "text/html; charset=utf-8", code: int = 200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/refresh"):
                with _STATE_LOCK:
                    busy = _STATE["updating"]
                    if not busy:
                        _STATE["updating"] = True
                if not busy:
                    threading.Thread(target=_refresh_state, daemon=True).start()
                self._send(json.dumps({"ok": True, "busy": busy}), "application/json")
            elif self.path.startswith("/api/state"):
                with _STATE_LOCK:
                    self._send(json.dumps({"updating": _STATE["updating"], "last": _STATE["last"]}),
                               "application/json")
            else:
                with _STATE_LOCK:
                    rows = list(_STATE["rows"])
                if not rows:
                    self._send("<meta charset='utf-8'><body style='background:#0d1117;color:#e6edf3;font-family:sans-serif;padding:40px'>"
                               "首次查询进行中，约 1 分钟…<script>setTimeout(()=>location.reload(),5000)</script>")
                else:
                    self._send(render_html(rows, live=True))

        def log_message(self, *a):  # 静默
            pass

    with _STATE_LOCK:
        _STATE["updating"] = True
    threading.Thread(target=_refresh_state, kwargs={"log": True}, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"🤖 额度监控服务已启动：{url}（Ctrl+C 停止）")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def append_log(rows: list[dict]) -> None:
    new = not LOG_CSV.exists()
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "name", "status", "used_pct", "used_text", "reset", "fiveh_text"])
        ts = dt.datetime.now().isoformat(timespec="seconds")
        for r in rows:
            status = "stale" if r.get("stale") else r["status"]
            w.writerow([ts, r["name"], status, r.get("used_pct", ""),
                        r.get("used_text", ""), r.get("reset", ""), r.get("fiveh_text", "")])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", action="store_true", help="追加快照到 quota-log.csv")
    ap.add_argument("--html", action="store_true", help="生成 quota-dashboard.html 仪表盘")
    ap.add_argument("--retry-failures", type=int, default=0, help="异常来源的额外重试次数")
    ap.add_argument("--retry-delay", type=float, default=15, help="异常来源重试前等待秒数")
    ap.add_argument(
        "--max-unhealthy",
        type=int,
        default=-1,
        help="超过此异常/缓存来源数则取消写入；负数表示不限制",
    )
    ap.add_argument("--serve", action="store_true", help="启动本地服务，网页上可直接刷新查询")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--public-html", type=str, default="", help="生成脱敏公开版页面到指定路径")
    args = ap.parse_args()
    if args.serve:
        serve(args.port)
        return 0
    try:
        rows = collect(
            retry_attempts=max(0, args.retry_failures),
            retry_delay=max(0, args.retry_delay),
        )
    except sub2api_quota.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    unhealthy = _unhealthy_rows(rows)
    if args.max_unhealthy >= 0 and len(unhealthy) > args.max_unhealthy:
        names = "、".join(str(row.get("name") or "未知来源") for row in unhealthy)
        rows, missing = _degrade_with_cache(rows)
        if missing:
            print(
                f"额度采集重试后仍有 {len(missing)} 个来源无缓存可用，取消本次写入："
                f"{'、'.join(missing)}",
                file=sys.stderr,
            )
            return 2
        print(
            f"降级发布：{len(unhealthy)} 个来源本次刷新失败，沿用上次成功快照（{names}）",
            file=sys.stderr,
        )
    else:
        _save_last_good(rows)
    if args.log:
        append_log(rows)
    if args.html:
        out = Path(__file__).with_name("quota-dashboard.html")
        render_html(rows, out)
        print(f"dashboard: {out}")
    if args.public_html:
        p = Path(args.public_html)
        render_public_site(rows, p)
        print(f"public: {p}, {p.with_name('history.html')}, {p.with_name('subscriptions.html')}")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
