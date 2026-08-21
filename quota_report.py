#!/usr/bin/env python3
"""quota_report.py — AI 额度监控表 v2（只读，不修改任何外部状态）

监控对象（统一口径：本周已用 + 周重置）：
- Kimi（本人 99/月）：KimiCodeBar credentials 里 alias=Leo-usage 的 key
- Kimi（Andy 199/月）：KimiCodeBar credentials 里 alias=andy* 的 key
- Codex（Mac）：最新 ~/.codex/sessions/**/rollout-*.jsonl 的 rate_limits
- Codex（Win）：ssh desktop 读取对端最新 rollout 的 rate_limits
- Grok（SuperGrok）：tmux 驱动本机 grok TUI 的 /usage 面板截屏解析

用法：
  python3 quota_report.py            # 终端表格
  python3 quota_report.py --json     # 机器可读
  python3 quota_report.py --log      # 追加快照到 quota-log.csv
  python3 quota_report.py --html     # 生成 quota-dashboard.html
安全：永不打印任何 key/token；网络调用全部只读且带超时。
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
import urllib.request
from pathlib import Path

HOME = Path.home()
LOG_CSV = Path(__file__).with_name("quota-log.csv")
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
HTTP_TIMEOUT = 15
SSH_TIMEOUT = 20


def _http_json(url: str, key: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _fmt_utc(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ts


def _fmt_epoch(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return "?"


# ---------- 数据源 ----------

def _kimi_quota(label: str, key: str | None) -> dict:
    if not key:
        return {"name": label, "status": "未配置 key"}
    try:
        d = _http_json(KIMI_USAGE_URL, key)
        u = d.get("usage", {})
        lim, used = int(u.get("limit", 0)), int(u.get("used", 0))
        row = {
            "name": label,
            "status": "ok",
            "used_pct": round(used / lim * 100, 1) if lim else None,
            "used_text": f"{used}/{lim}",
            "reset": _fmt_utc(u.get("resetTime")),
        }
        for w in d.get("limits", []):
            win = w.get("window", {})
            if win.get("duration") == 300 and win.get("timeUnit") == "TIME_UNIT_MINUTE":
                det = w.get("detail", {})
                l5, u5 = int(det.get("limit", 0)), int(det.get("used", 0))
                row["fiveh_pct"] = round(u5 / l5 * 100, 1) if l5 else None
                row["fiveh_text"] = f"{u5}/{l5}"
                row["fiveh_reset"] = _fmt_utc(det.get("resetTime"))
        return row
    except Exception as e:  # noqa: BLE001
        return {"name": label, "status": f"查询失败: {type(e).__name__}"}


def _find_rate_limits(o):
    if isinstance(o, dict):
        rl = o.get("rate_limits")
        if isinstance(rl, dict) and "primary" in rl:
            return rl
        for v in o.values():
            r = _find_rate_limits(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_rate_limits(v)
            if r:
                return r
    return None


def _codex_row(label: str, rl: dict, note: str = "") -> dict:
    p = rl.get("primary", {})
    return {
        "name": label,
        "status": "ok",
        "used_pct": p.get("used_percent"),
        "used_text": f"{p.get('used_percent')}%",
        "reset": _fmt_epoch(p.get("resets_at")),
        "note": note or (rl.get("plan_type") or ""),
    }


def _tmux_slash_probe(cmd: str, slash: str, wait_boot: int = 12, wait_panel: int = 5,
                      pre_enter: bool = False) -> str | None:
    """通用：tmux 驱动 TUI 执行 slash 命令并截屏。pre_enter 用于先按一次回车（如 agy 的信任确认）。"""
    if not shutil.which("tmux"):
        return None
    sess = f"quota_probe_{cmd.replace('/', '_').replace(' ', '_')}"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-x", "200", "-y", "50", cmd],
                       check=True, timeout=15, capture_output=True)
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
    """'Refreshes in 74h 29m' → 绝对时间 %m-%d %H:%M"""
    m = re.search(r"(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?", s)
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
    m_week = re.search(r"Weekly Limit Remaining\s*\n[^%\n]*?([\d.]+)%[^\n]*\n\s*[\d.]+% remaining · Refreshes in ([^\n]+)", seg)
    m_5h = re.search(r"Five Hour Limit Remaining\s*\n[^%\n]*?([\d.]+)%[^\n]*\n\s*[\d.]+% remaining · Refreshes in ([^\n]+)", seg)
    if not m_week:
        return None
    week_left = float(m_week.group(1))
    row = {
        "used_pct": round(100 - week_left, 1),
        "used_text": f"{100 - week_left:g}%",
        "reset": _parse_refreshes_in(m_week.group(2)),
    }
    if m_5h:
        left5 = float(m_5h.group(1))
        row["fiveh_pct"] = round(100 - left5, 1)
        row["fiveh_text"] = f"{100 - left5:g}%"
        row["fiveh_reset"] = _parse_refreshes_in(m_5h.group(2))
    return row


def _agy() -> list[dict]:
    """Antigravity：tmux 驱动 agy TUI /usage，面板含 GEMINI 与 CLAUDE/GPT 两组额度。"""
    text = _tmux_slash_probe("agy", "/usage", wait_boot=10, wait_panel=6, pre_enter=True)
    if not text:
        return [{"name": "Antigravity（Gemini/Claude）", "status": "TUI 探测失败"}]
    g = _agy_parse_group(text, "GEMINI MODELS")
    c = _agy_parse_group(text, "CLAUDE AND GPT MODELS")
    rows = []
    if g:
        g.update({"name": "Antigravity · Gemini 组", "status": "ok", "note": "Google AI Pro"})
        rows.append(g)
    if c:
        c.update({"name": "Antigravity · Claude/GPT 组", "status": "ok", "note": "Google AI Pro"})
        rows.append(c)
    if not rows:
        rows.append({"name": "Antigravity（Gemini/Claude）", "status": "面板解析失败"})
    return rows


def _codex_bin() -> str | None:
    """返回可用的 codex 二进制：~/.local/bin 的 npm 全局版可能缺可选依赖，逐个试 --version。"""
    candidates = ["codex", str(HOME / ".local/bin/codex"), "/opt/homebrew/bin/codex"]
    for c in candidates:
        try:
            r = subprocess.run([c, "--version"], capture_output=True, timeout=8)
            if r.returncode == 0:
                return c
        except Exception:  # noqa: BLE001
            continue
    return None


def _codex_local(label: str) -> dict:
    """Mac 端：tmux 驱动 codex TUI /status，状态栏含 'weekly N% left'（实时）。
    失败则回退到会话文件扫描（可能滞后）。"""
    bin_path = _codex_bin()
    if not bin_path:
        return {"name": label, "status": "本机 codex 二进制均不可用"}
    text = _tmux_slash_probe(bin_path, "/status", wait_boot=12, wait_panel=5)
    if text:
        m = re.search(r"weekly\s+(\d+(?:\.\d+)?)%\s+left", text)
        if m:
            left = float(m.group(1))
            m_model = re.search(r"› /status.*?(gpt[\w.\- ]+?) ·", text.replace("\n", " "))
            note = m_model.group(1).strip() if m_model else ""
            # TUI 状态栏不带重置时刻；resets_at 来自 Codex 会话里的官方字段
            reset = "滚动周窗"
            try:
                files = sorted((HOME / ".codex/sessions").rglob("rollout-*.jsonl"),
                               key=lambda p: p.stat().st_mtime)
                for f in reversed(files[-5:]):
                    rl = _extract_rate_limits(f)
                    if rl and rl.get("primary", {}).get("resets_at"):
                        reset = _fmt_epoch(rl["primary"]["resets_at"])
                        break
            except Exception:  # noqa: BLE001
                pass
            return {
                "name": label,
                "status": "ok",
                "used_pct": round(100 - left, 1),
                "used_text": f"{100 - left:g}%（剩 {left:g}%）",
                "reset": reset,
                "note": (note + " · 实时" if note else "实时"),
            }
    # 回退：会话文件
    root = HOME / ".codex" / "sessions"
    try:
        files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
        for f in reversed(files[-5:]):
            rl = _extract_rate_limits(f)
            if rl:
                row = _codex_row(label, rl)
                row["note"] = f"会话文件 · 数据 {_fmt_epoch(f.stat().st_mtime)}（非实时）"
                return row
        return {"name": label, "status": "TUI 探测失败且无会话数据"}
    except Exception as e:  # noqa: BLE001
        return {"name": label, "status": f"读取失败: {type(e).__name__}"}


def _extract_rate_limits(path: Path) -> dict | None:
    last = None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "rate_limits" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                found = _find_rate_limits(obj)
                if found:
                    last = found
    except Exception:  # noqa: BLE001
        return None
    return last


def _codex_win(label: str) -> dict:
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$f = Get-ChildItem -Path $env:USERPROFILE\\.codex\\sessions -Recurse -Filter *.jsonl "
        "| Sort-Object LastWriteTime | Select-Object -Last 1; "
        "$line = Select-String -Path $f.FullName -Pattern rate_limits | Select-Object -Last 1; "
        "if ($line) { Write-Output $line.Line }"
    )
    try:
        out = subprocess.run(
            ["ssh", "-o", f"ConnectTimeout={SSH_TIMEOUT - 10}", "desktop",
             "powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=SSH_TIMEOUT, check=False,
        )
        text = out.stdout.decode("utf-8", errors="ignore")
        if out.returncode != 0 or not text.strip():
            return {"name": label, "status": "ssh 无输出（台式机离线？）"}
        for raw in reversed(text.strip().splitlines()):
            try:
                rl = _find_rate_limits(json.loads(raw))
            except Exception:  # noqa: BLE001
                continue
            if rl:
                return _codex_row(label, rl)
        return {"name": label, "status": "对端会话无额度数据"}
    except Exception as e:  # noqa: BLE001
        return {"name": label, "status": f"ssh 失败: {type(e).__name__}"}


def _grok(label: str) -> dict:
    """tmux 驱动 grok TUI 的 /usage 面板，截取 Weekly limit；结束杀临时会话。"""
    if not shutil.which("tmux"):
        return {"name": label, "status": "tmux 不可用，无法探测"}
    sess = "grok_quota_probe"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-x", "200", "-y", "50", "grok"],
                       check=True, timeout=15, capture_output=True)
        time.sleep(12)
        subprocess.run(["tmux", "send-keys", "-t", sess, "/usage", "Enter"],
                       check=True, timeout=5, capture_output=True)
        time.sleep(6)
        out = subprocess.run(["tmux", "capture-pane", "-t", sess, "-p"],
                             capture_output=True, timeout=5)
        text = out.stdout.decode("utf-8", errors="ignore")
        if "Weekly limit" not in text:
            return {"name": label, "status": "未捕获到 /usage 面板"}
        seg = text.split("Weekly limit", 1)[1][:1500]
        m_pct = re.search(r"(\d+(?:\.\d+)?)%", seg)
        m_rs = re.search(r"Resets:\s*([^\n]+)", seg)
        if not m_pct:
            return {"name": label, "status": "面板解析失败"}
        reset = m_rs.group(1).split("│")[0].strip() if m_rs else "?"
        return {
            "name": label,
            "status": "ok",
            "used_pct": float(m_pct.group(1)),
            "used_text": f"{m_pct.group(1)}%",
            "reset": reset,
        }
    except Exception as e:  # noqa: BLE001
        return {"name": label, "status": f"探测失败: {type(e).__name__}"}
    finally:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)


def _mmx_bin() -> str | None:
    if shutil.which("mmx"):
        return "mmx"
    cands = sorted(Path.home().glob(".nvm/versions/node/*/bin/mmx"))
    return str(cands[-1]) if cands else None


def _minimax(label: str) -> dict:
    """MiniMax：本机 mmx-cli 的 `mmx quota show`（JSON 输出）。
    取 model_name=general：current_weekly_remaining_percent / current_interval_remaining_percent（5h 窗）。"""
    mmx = _mmx_bin()
    if not mmx:
        return {"name": label, "status": "mmx-cli 未找到"}
    try:
        out = subprocess.run([mmx, "quota", "show"], capture_output=True, timeout=20, check=False)
        d = json.loads(out.stdout.decode("utf-8", errors="ignore"))
        models = d.get("model_remains", [])
        g = next((m for m in models if m.get("model_name") == "general"), models[0] if models else None)
        if not g:
            return {"name": label, "status": "返回为空"}
        w_remain = g.get("current_weekly_remaining_percent")
        i_remain = g.get("current_interval_remaining_percent")
        row = {
            "name": label,
            "status": "ok",
            "used_pct": round(100 - float(w_remain), 1) if w_remain is not None else None,
            "used_text": f"{100 - float(w_remain):g}%" if w_remain is not None else "?",
            "reset": _fmt_epoch((g.get("weekly_end_time") or 0) / 1000),
        }
        if i_remain is not None:
            row["fiveh_pct"] = round(100 - float(i_remain), 1)
            row["fiveh_text"] = f"{100 - float(i_remain):g}%"
            row["fiveh_reset"] = _fmt_epoch((g.get("end_time") or 0) / 1000)
        return row
    except Exception as e:  # noqa: BLE001
        return {"name": label, "status": f"查询失败: {type(e).__name__}"}


def _kimi_keys_from_codebar() -> tuple[str | None, str | None]:
    mine = andy = None
    try:
        d = json.loads((HOME / "Library/Application Support/KimiCodeBar/credentials.json").read_text())
        for a in d.get("accounts", []):
            cred = a.get("credential", {}).get("apiKey")
            key = None
            if isinstance(cred, dict):
                key = cred.get("key") or cred.get("value") or next(iter(cred.values()), None)
            elif isinstance(cred, str):
                key = cred
            if not key:
                continue
            alias = (a.get("alias") or "").lower()
            if "leo" in alias:
                mine = key
            elif "andy" in alias:
                andy = key
    except Exception:  # noqa: BLE001
        pass
    return mine, andy


def _load_kimi_keys() -> tuple[str | None, str | None]:
    mine, andy = _kimi_keys_from_codebar()
    if mine or andy:
        return mine, andy
    try:
        auth = json.loads((HOME / ".local/share/opencode/auth.json").read_text())
        k = auth.get("kimi-for-coding") or {}
        if k.get("type") == "api":
            mine = k.get("key")
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Print :kimiApiKey",
             str(HOME / "Library/Preferences/com.kimicodebar.app.plist")],
            capture_output=True, timeout=5, check=False,
        )
        if out.returncode == 0:
            andy = out.stdout.decode().strip() or None
    except Exception:  # noqa: BLE001
        pass
    return mine, andy


def collect() -> list[dict]:
    mine, andy = _load_kimi_keys()
    rows = [
        _kimi_quota("Kimi · 本人（948/年）", mine),
        _kimi_quota("Kimi · Andy（199/月）", andy),
        _codex_local("Codex · Mac"),
        _codex_win("Codex · Win"),
        _grok("Grok · SuperGrok"),
        _minimax("MiniMax · Plus"),
    ] + _agy()
    return rows


# ---------- 排序 / 刷新提醒 ----------

_MONTHS = {n: i for i, n in enumerate(
    "January February March April May June July August September "
    "October November December".split(), 1)}
_WEEKLY_SOON_H = 36
_FIVEH_SOON_H = 3


def _remain_pct(r: dict, key: str = "used_pct") -> float | None:
    pct = r.get(key)
    if pct is None:
        return None
    return max(0.0, min(100.0, 100.0 - float(pct)))


def _parse_reset(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    raw = str(s).strip()
    now = dt.datetime.now()
    m = re.search(r"(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", raw)
    if m:
        month, day, hh, mm = (int(x) for x in m.groups())
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
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{1,2}):(\d{2})", raw)
    if m:
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        day, hh, mm = int(m.group(2)), int(m.group(3)), int(m.group(4))
        try:
            d = now.replace(month=mon, day=day, hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            return None
        if d < now - dt.timedelta(days=2):
            try:
                d = d.replace(year=now.year + 1)
            except ValueError:
                pass
        return d
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
        if r.get("status") != "ok":
            continue
        if "minimax" in (r.get("name") or "").lower():
            continue
        rem = _remain_pct(r)
        wr = _parse_reset(r.get("reset"))
        hit_week = False
        if rem is not None and rem > 3 and wr is not None:
            hours = (wr - now).total_seconds() / 3600
            if 0 <= hours <= _WEEKLY_SOON_H:
                out.append({
                    "name": r["name"],
                    "kind": "week",
                    "remain": rem,
                    "when": wr,
                    "until": _fmt_until(wr, now),
                })
                hit_week = True
        if hit_week:
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
        window = "周额度" if a["kind"] == "week" else "5h 窗"
        lines.append(
            f"- **{a['name']}**：{window} {a['until']}刷新，还剩 {a['remain']:.0f}%"
        )
    lines.append("")
    return lines


def _html_text(value: object) -> str:
    """把动态数据编码为 HTML 文本，保留生成器自身的标签结构。"""
    return html.escape(str(value), quote=True)


def _alerts_html(alerts: list[dict]) -> str:
    if not alerts:
        return ""
    items = "".join(
        f"<li><b>{_html_text(a['name'])}</b> · "
        f"{'周额度' if a['kind']=='week' else '5h 窗'} {_html_text(a['until'])}刷新"
        f"，还剩 {a['remain']:.0f}%，抓紧用</li>"
        for a in alerts
    )
    return f"""<div class="alertband"><h3>快刷新了，剩余额度抓紧用</h3><ul>{items}</ul></div>"""


# ---------- 输出 ----------

def render(rows: list[dict]) -> str:
    rows = _sort_rows(rows)
    alerts = _alerts(rows)
    lines = [f"# AI 额度快照 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 按周剩余从小到大", ""]
    for r in rows:
        if r.get("status") != "ok":
            lines.append(f"- **{r['name']}**：{r['status']}")
            continue
        rem = _remain_pct(r)
        rem_s = ""
        if rem is not None and f"剩 {rem:g}%" not in str(r.get("used_text") or ""):
            rem_s = f"，剩 {rem:g}%"
        s = f"- **{r['name']}**：本周已用 {r['used_text']}"
        if r.get("used_pct") is not None and "%" not in str(r["used_text"]):
            s += f"（{r['used_pct']}%）"
        s += rem_s
        s += f"，周重置 {r.get('reset', '?')}"
        if r.get("fiveh_text"):
            s += f"，5h窗已用 {r['fiveh_text']}（重置 {r.get('fiveh_reset', '?')}）"
        if r.get("note"):
            s += f"，{r['note']}"
        lines.append(s)
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


def _card(r: dict, alert: dict | None = None) -> str:
    name = _html_text(r["name"])
    if r.get("status") != "ok":
        return f"""<div class="card"><div class="top"><span class="name">{name}</span>
<span class="pill bad">异常</span></div><div class="msg">{_html_text(r['status'])}</div></div>"""
    pct = r.get("used_pct")
    rem = _remain_pct(r)
    pct_txt = f"{pct:g}%" if pct is not None else "—"
    rem_txt = f"剩 {rem:g}%" if rem is not None else ""
    c = _color(pct)
    fiveh = ""
    if r.get("fiveh_text"):
        fp = r.get("fiveh_pct")
        fc = _color(fp)
        fiveh = f"""<div class="sub">5h 窗</div>
<div class="bar small"><div class="fill" style="width:{fp or 0}%;background:{fc}"></div></div>
<div class="meta">已用 {_html_text(r['fiveh_text'])} · 重置 {_html_text(r.get('fiveh_reset', '?'))}</div>"""
    note = f"<div class='meta dim'>{_html_text(r['note'])}</div>" if r.get("note") else ""
    urgent = " urgent" if alert else ""
    pill = "<span class='pill warn'>抓紧用</span>" if alert else "<span class='pill ok'>正常</span>"
    return f"""<div class="card{urgent}"><div class="top"><span class="name">{name}</span>
{pill}</div>
<div class="pct" style="color:{c}">{pct_txt}</div>
<div class="remain">{rem_txt}</div>
<div class="bar"><div class="fill" style="width:{pct or 0}%;background:{c}"></div></div>
<div class="meta">本周已用 {_html_text(r.get('used_text', '?'))} · 重置 {_html_text(r.get('reset', '?'))}</div>
{fiveh}{note}</div>"""


FINANCE_MD = Path(os.environ.get(
    "QUOTA_FINANCE_MD",
    "/Users/insistgang/Documents/knowledge/06-Growth/Finance/大模型订阅与消费盘点.md"))


def _strip_md(s: str) -> str:
    return s.replace("**", "").replace("~~", "").strip()


def _load_finance() -> dict:
    """从《大模型订阅与消费盘点.md》解析：订阅表 / 历史充值台账 / 非AI订阅 / 合计口径。"""
    out = {"subs": [], "ledger": [], "non_ai": [], "total_ai": "", "total_all": ""}
    try:
        text = FINANCE_MD.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return out
    section = ""
    for line in text.splitlines():
        if line.startswith("## 一、"):
            section = "subs"; continue
        if line.startswith("## 二、"):
            section = "ledger"; continue
        if line.startswith("## "):
            if section in ("subs", "ledger"):
                section = ""
        if section and line.startswith("|") and "---" not in line:
            cells = [_strip_md(c) for c in line.strip().strip("|").split("|")]
            if section == "subs" and len(cells) >= 5 and cells[0] != "产品":
                if "已停用" not in cells[1] and "已停用" not in cells[0]:
                    out["subs"].append({"name": cells[0], "cost": cells[2], "renewal": cells[4]})
            elif section == "ledger" and len(cells) >= 3 and cells[0] != "日期":
                out["ledger"].append({"date": cells[0], "item": cells[1], "amount": cells[2]})
        m = re.search(r"AI 固定订阅合计（本人实付）：约\s*([\d.]+\s*元/月)", line)
        if m:
            out["total_ai"] = m.group(1)
        m2 = re.search(r"固定订阅总额约\s*\**([\d.]+\s*元/月)", line)
        if m2:
            out["total_all"] = m2.group(1)
        m3 = re.match(r"^\s*-\s*(iCloud|得到会员|得到大脑会员|微信读书|百度网盘|视频会员)：(.+)$", line)
        if m3:
            out["non_ai"].append(f"{m3.group(1)}：{_strip_md(m3.group(2))}")
    return out


_SVC_ICONS = [
    ("ChatGPT", "🤖"), ("Kimi", "🌙"), ("Grok", "🚀"), ("Gemini", "♊"),
    ("MiniMax", "🎬"), ("智谱", "🧠"), ("ChatGLM", "🧠"), ("中转", "🔀"),
    ("DeepSeek", "🔍"), ("Claude", "🎭"), ("iCloud", "☁️"), ("得到", "🎧"),
    ("微信读书", "📚"), ("百度网盘", "💾"), ("视频", "📺"),
]


def _svc_icon(name: str) -> str:
    for k, v in _SVC_ICONS:
        if k.lower() in name.lower():
            return v
    return "📦"


def _short_renewal(s: str) -> str:
    """把冗长的刷新/到期描述收成一行：每月 X/Y 日 + 到期日。"""
    days = re.findall(r"每月\s*(\d+)\s*日", s)
    expiry = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:到期|续费)", s)
    parts = []
    if days:
        parts.append("每月 " + "/".join(days) + " 日")
    if expiry:
        parts.append(expiry.group(1) + " 到期")
    if parts:
        return "；".join(parts)
    return s if len(s) <= 16 else s[:15] + "…"


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


def _subs_html(public: bool) -> str:
    fin = _load_finance()
    if not fin["subs"] and not fin["ledger"]:
        return ""

    def pub(s: str) -> str:
        return s.replace("Andy", "朋友") if public else s

    cards = []
    for r in fin["subs"]:
        name, cost, renewal = pub(r["name"]), pub(r["cost"]), pub(r["renewal"])
        expiry_m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:到期|续费)", renewal)
        expiry = expiry_m.group(1) if expiry_m else None
        cards.append(f"""<div class="scard">
<div class="sname">{_svc_icon(name)} {_html_text(name)}</div>
<div class="scost">{_html_text(cost)}</div>
<div class="smeta">{_html_text(_short_renewal(renewal))} {_countdown_pill(expiry)}</div>
</div>""")
    ledger_trs = "".join(
        f"<tr><td>{_html_text(l['date'])}</td><td>{_svc_icon(l['item'])} {_html_text(pub(l['item']))}</td>"
        f"<td class='amt'>{_html_text(l['amount'])}</td></tr>"
        for l in fin["ledger"])
    non_ai = "".join(
        f"<div class='scard mini'><div class='sname'>{_svc_icon(x)} {_html_text(pub(x))}</div></div>"
        for x in fin["non_ai"])
    totals = ""
    if fin["total_ai"] or fin["total_all"]:
        parts = []
        if fin["total_ai"]:
            parts.append(f"本人 AI 固定支出约 {_html_text(fin['total_ai'])}")
        if fin["total_all"]:
            parts.append(f"含非 AI 合计约 {_html_text(fin['total_all'])}")
        totals = f"<div class='totalband'>💰 {' · '.join(parts)}</div>"
    return f"""<h2>💳 订阅 · 到期 · 续费</h2>
{totals}
<div class="sgrid">{''.join(cards)}</div>
<h2>📒 消费台账（历史充值）</h2>
<table class="ledger"><tr><td>日期</td><td>项目</td><td>金额</td></tr>{ledger_trs}</table>
<h2>🧾 非 AI 固定订阅</h2>
<div class="sgrid">{non_ai}</div>"""


PUBLIC_LABELS = {
    "Kimi · 本人（948/年）": "Kimi · 主账号",
    "Kimi · Andy（199/月）": "Kimi · 副账号",
}

_HIST_PUBLIC = {"Kimi · 本人": "Kimi · 主账号", "Kimi · Andy": "Kimi · 副账号"}


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


def _daily_deltas(days: int = 7) -> list[tuple[str, list[dict]]]:
    """从 LOG_CSV 计算每源每日消耗（周计数口径）。

    普通日为当天末次快照 − 前一日末次快照；跨周重置时按重置前、后两段相加。
    底账首日或快照中断后的首日无连续前值，从当天首次快照起算（partial 标记）。
    返回 [(日期, [行...]), ...]，新日期在前。
    """
    if not LOG_CSV.exists():
        return []
    per: dict[tuple[str, str], list[tuple[str, float, str]]] = {}
    with open(LOG_CSV, encoding="utf-8") as f:
        for r in csv.reader(f):
            if len(r) < 4 or not r[0] or not r[0][0].isdigit():
                continue
            try:
                pct = float(r[3])
            except ValueError:
                continue
            name = r[1].split("（")[0].strip()  # 合并标签改名（如 Kimi 本人 99/月→948/年）
            reset = r[5] if len(r) > 5 else ""
            per.setdefault((r[0][:10], name), []).append((r[0], pct, reset))
    dates = sorted({d for d, _ in per}, reverse=True)[:days]
    out = []
    for d in dates:
        rows = []
        for (dd, name), snaps in sorted(per.items()):
            if dd != d:
                continue
            snaps.sort()
            prev_days = [p for (p, n2) in per if n2 == name and p < d]
            previous_day = max(prev_days) if prev_days else None
            try:
                is_consecutive = (
                    previous_day is not None
                    and dt.date.fromisoformat(d) - dt.date.fromisoformat(previous_day) == dt.timedelta(days=1)
                )
            except ValueError:
                is_consecutive = False
            if is_consecutive:
                previous = sorted(per[(previous_day, name)])[-1]
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
                  more_href: str = "") -> str:
    """每日消耗柱状图：按天分块，源按消耗降序，纯 CSS 横条。"""
    history_days = _daily_deltas(days=days)
    if not history_days:
        return ""
    blocks = []
    source_names: set[str] = set()
    for day_index, (d, rows) in enumerate(history_days):
        shown = [r for r in rows if r["delta"] > 0.05]
        resets = [r for r in rows if r["reset"] and r["delta"] <= 0.05]
        lines = []
        if shown:
            mx = max(r["delta"] for r in shown)
            for r in sorted(shown, key=lambda x: -x["delta"]):
                name = _HIST_PUBLIC.get(r["name"], r["name"]) if public else r["name"]
                source_names.add(name)
                w = max(3, round(r["delta"] / mx * 100))
                tags = ("*" if r["partial"] else "") + (" ↺" if r["reset"] else "")
                lines.append(
                    f'<div class="hrow" data-source="{_html_text(name)}">'
                    f'<span class="hname">{_html_text(name)}</span>'
                    f'<div class="hbar"><div class="hfill" style="width:{w}%"></div></div>'
                    f'<span class="hval">+{r["delta"]:g}%{tags}</span></div>')
        else:
            lines.append('<div class="hrow dim">当日未记录到消耗</div>')
        for r in resets:
            name = _HIST_PUBLIC.get(r["name"], r["name"]) if public else r["name"]
            source_names.add(name)
            lines.append(
                f'<div class="hrow dim" data-source="{_html_text(name)}">'
                f'{_html_text(name)} ↺ 周重置</div>')
        blocks.append(
            f'<div class="hday" data-day-index="{day_index}">'
            f'<div class="hdate">{_html_text(d[5:])}</div>{"".join(lines)}</div>')

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
    return ('<div class="section-head"><h2>📊 每日消耗（周计数口径）</h2>' + more + '</div>'
            + tools + "".join(blocks)
            + '<div class="sub">普通日 = 当天末次快照 − 前一日末次快照；↺ 跨周重置日按重置前后分段相加；'
              '* 底账首日或快照中断后从当天首次快照起算；5h 窗不计；底账随快照积累逐日丰富</div>')


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


def _freshness_html(now: str) -> str:
    """标签页长期打开时，仅提示有新版，不强制刷新打断阅读位置。"""
    return f'''<div id="updateNotice" class="update-notice" data-current="{_html_text(now)}" hidden>
发现有新数据 <button type="button" id="reloadLatest">点击更新</button></div>
<script>
(()=>{{
  const notice=document.getElementById('updateNotice');
  const current=notice.dataset.current;
  async function checkLatest(){{
    try{{
      const response=await fetch('index.html',{{cache:'no-store'}});
      const latest=new DOMParser().parseFromString(await response.text(),'text/html')
        .querySelector('meta[name="quota-generated-at"]')?.content;
      if(latest && latest!==current) notice.hidden=false;
    }}catch(_error){{}}
  }}
  document.getElementById('reloadLatest').addEventListener('click',()=>location.reload());
  document.addEventListener('visibilitychange',()=>{{if(!document.hidden) checkLatest();}});
  setInterval(checkLatest,15*60*1000);
}})();
</script>'''


def _history_filter_script() -> str:
    return '''<script>
(()=>{
  const buttons=[...document.querySelectorAll('[data-days]')];
  const source=document.getElementById('sourceFilter');
  function applyFilters(){
    const limit=Number(document.querySelector('[data-days].active')?.dataset.days || 7);
    const selected=source?.value || '';
    document.querySelectorAll('.hday').forEach(day=>{
      const inRange=Number(day.dataset.dayIndex)<limit;
      let hasVisibleRow=false;
      day.querySelectorAll('.hrow').forEach(row=>{
        const matches=!selected || row.dataset.source===selected;
        row.hidden=!inRange || !matches;
        if(!row.hidden) hasVisibleRow=true;
      });
      day.hidden=!inRange || !hasVisibleRow;
    });
  }
  buttons.forEach(button=>button.addEventListener('click',()=>{
    buttons.forEach(other=>other.classList.toggle('active',other===button));
    applyFilters();
  }));
  source?.addEventListener('change',applyFilters);
  applyFilters();
})();
</script>'''


def render_html(rows: list[dict], path: Path | None = None, live: bool = False,
                public: bool = False, page: str = "all") -> str:
    if page not in {"all", "overview", "history", "subscriptions"}:
        raise ValueError(f"unknown dashboard page: {page}")
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _sort_rows(list(rows))
    if public:
        rows = [{**r, "name": PUBLIC_LABELS.get(r["name"], r["name"])} for r in rows]
    alerts = _alerts(rows)
    alert_by_name = {a["name"]: a for a in alerts}
    cards = "".join(_card(r, alert_by_name.get(r["name"])) for r in rows)
    banner = _alerts_html(alerts)
    live_ui = """
<div style="margin:14px 0"><button id="rb" onclick="doRefresh()" style="background:#238636;color:#fff;border:0;border-radius:8px;padding:8px 18px;font-size:14px;cursor:pointer">🔄 重新查询</button>
<span id="st" style="font-size:12px;color:#8b949e;margin-left:10px"></span></div>
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

    if page == "overview":
        heading = "🤖 AI 额度监控"
        title = "AI 额度监控"
        detail = "卡片按周剩余从小到大"
        content = (
            f'{live_ui}<div class="grid">{cards}</div>{banner}'
            f'{_history_html(public, days=2, more_href="history.html")}'
        )
    elif page == "history":
        heading = "📊 每日消耗"
        title = "每日消耗 · AI 额度监控"
        detail = "默认最近 7 天，可切换 30 天并按账号筛选"
        content = _history_html(public, days=30, controls=True) + _history_filter_script()
    elif page == "subscriptions":
        heading = "💳 订阅账单"
        title = "订阅账单 · AI 额度监控"
        detail = "订阅、历史充值与非 AI 固定支出"
        content = _subs_html(public)
    else:
        heading = "🤖 AI 额度监控"
        title = "AI 额度监控"
        detail = "卡片按周剩余从小到大"
        content = (
            f'{live_ui}<div class="grid">{cards}</div>{banner}'
            f'{_history_html(public)}{_subs_html(public)}'
        )

    split_site = page != "all"
    navigation = _nav_html(page) if split_site else ""
    freshness = _freshness_html(now) if split_site else ""
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{title} · {now}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="quota-generated-at" content="{now}">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🤖</text></svg>">
<style>
*{{box-sizing:border-box}}
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,"PingFang SC",sans-serif;padding:28px;max-width:1080px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 4px}} .ts{{color:#8b949e;font-size:13px;margin-bottom:6px}}
h2{{font-size:17px;margin:26px 0 8px}}
a{{color:#58a6ff;text-decoration:none}} a:hover{{text-decoration:underline}}
.nav{{position:sticky;top:10px;z-index:20;display:flex;gap:6px;width:max-content;max-width:100%;padding:5px;margin:0 0 20px;background:rgba(22,27,34,.94);border:1px solid #30363d;border-radius:11px;backdrop-filter:blur(12px)}}
.nav a{{color:#8b949e;padding:7px 12px;border-radius:7px;font-size:13px;font-weight:600;white-space:nowrap}}
.nav a:hover{{color:#e6edf3;text-decoration:none;background:#21262d}}
.nav a.active{{color:#fff;background:#238636}}
.update-notice{{position:fixed;right:18px;bottom:18px;z-index:30;background:#1f6feb;color:#fff;border-radius:10px;padding:10px 12px;font-size:13px;box-shadow:0 8px 28px rgba(0,0,0,.35)}}
.update-notice button{{margin-left:8px;border:0;border-radius:6px;padding:5px 9px;background:#fff;color:#0969da;cursor:pointer;font-weight:600}}
[hidden]{{display:none!important}}
table{{border-collapse:collapse;width:100%}} td{{border:1px solid #30363d;padding:9px 12px;font-size:15px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px 18px}}
.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}}
.name{{font-size:14px;font-weight:600}}
.pill{{font-size:11px;padding:2px 8px;border-radius:99px}}
.pill.ok{{background:rgba(63,185,80,.15);color:#3fb950}}
.pill.bad{{background:rgba(248,81,73,.15);color:#f85149}}
.pill.warn{{background:rgba(240,136,62,.18);color:#f0883e}}
.card.urgent{{border-color:#f0883e}}
.pct{{font-size:34px;font-weight:700;letter-spacing:-1px;margin:2px 0 4px}}
.remain{{font-size:13px;color:#8b949e;margin-bottom:8px}}
.alertband{{background:rgba(240,136,62,.1);border:1px solid rgba(240,136,62,.45);border-radius:10px;padding:12px 16px;margin:12px 0 18px}}
.alertband h3{{margin:0 0 8px;font-size:15px;color:#f0883e}}
.alertband ul{{margin:0;padding-left:18px}}
.alertband li{{font-size:14px;margin:5px 0}}
.bar{{height:8px;background:#21262d;border-radius:99px;overflow:hidden}}
.bar.small{{height:5px;margin-top:4px}}
.fill{{height:100%;border-radius:99px;transition:width .4s}}
.meta{{font-size:13px;color:#c9d1d9;margin-top:8px}}
.meta.nonai{{padding:6px 0;border-bottom:1px dashed #21262d}}
.card:hover,.scard:hover{{border-color:#58a6ff}}
.sgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}}
.scard{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:14px 16px;transition:border-color .2s}}
.scard.mini{{padding:10px 14px}}
.sname{{font-size:14px;font-weight:600;margin-bottom:6px}}
.scost{{font-size:19px;font-weight:700;color:#58a6ff;margin-bottom:6px}}
.smeta{{font-size:13px;color:#8b949e}}
.cd{{font-size:11px;padding:2px 8px;border-radius:99px;margin-left:6px;white-space:nowrap}}
.cd.green{{background:rgba(63,185,80,.15);color:#3fb950}}
.cd.yellow{{background:rgba(210,153,34,.15);color:#d29922}}
.cd.red{{background:rgba(248,81,73,.15);color:#f85149}}
.totalband{{background:rgba(88,166,255,.1);border:1px solid rgba(88,166,255,.3);border-radius:10px;padding:10px 16px;font-size:15px;font-weight:600;margin-bottom:14px}}
table.ledger td{{font-size:14px;padding:7px 12px}}
table.ledger td.amt{{color:#d29922;font-weight:600;white-space:nowrap}}
.meta.dim{{color:#8b949e}}
.sub{{font-size:11px;color:#8b949e;margin-top:12px}}
.msg{{font-size:13px;color:#d29922}}
.hday{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:12px 16px;margin-bottom:10px}}
.hdate{{font-size:13px;font-weight:600;color:#8b949e;margin-bottom:8px}}
.hrow{{display:flex;align-items:center;gap:10px;margin:5px 0;font-size:13px}}
.hrow.dim{{color:#8b949e}}
.hname{{width:170px;flex-shrink:0}}
.hbar{{flex:1;height:10px;background:#21262d;border-radius:99px;overflow:hidden}}
.hfill{{height:100%;background:#58a6ff;border-radius:99px}}
.hval{{width:74px;text-align:right;color:#58a6ff;font-weight:600;flex-shrink:0}}
.section-head{{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-top:26px}}
.section-head h2{{margin:0 0 8px}}
.more-link{{font-size:13px;white-space:nowrap}}
.history-tools{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:2px 0 12px}}
.range-switch{{display:flex;gap:5px}}
.range-switch button,.source-filter select{{border:1px solid #30363d;border-radius:7px;background:#161b22;color:#c9d1d9;padding:7px 10px;font-size:12px}}
.range-switch button{{cursor:pointer}}
.range-switch button.active{{background:#1f6feb;border-color:#1f6feb;color:#fff}}
.source-filter{{display:flex;align-items:center;gap:7px;color:#8b949e;font-size:12px}}
@media(max-width:600px){{
  body{{padding:18px 14px}}
  .nav{{top:6px;width:100%;justify-content:space-between}}
  .nav a{{padding:7px 9px}}
  .grid{{grid-template-columns:1fr}}
  .section-head{{align-items:flex-start}}
  .history-tools{{align-items:stretch;flex-direction:column}}
  .source-filter select{{flex:1}}
  .hrow{{gap:7px}}
  .hname{{width:112px}}
  .hval{{width:66px}}
}}
</style></head><body>
{navigation}
<h1>{heading}</h1>
<div class="ts">更新于 {now} · {"公开快照 · 每小时自动更新" if public else "统一口径：本周已用 / 周重置 · 数据源全部本机只读"} · {detail}</div>
{content}
{freshness}
</body></html>"""
    if path is not None:
        path.write_text(html, encoding="utf-8")
    return html


def render_public_site(rows: list[dict], index_path: Path) -> None:
    """生成 GitHub Pages 的概览、每日消耗和订阅账单三个静态页面。"""
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
    rows = collect()
    with _STATE_LOCK:
        _STATE["rows"] = rows
        _STATE["updating"] = False
        _STATE["last"] = dt.datetime.now().isoformat(timespec="seconds")
    if log:
        append_log(rows)
    try:
        render_html(rows, Path(__file__).with_name("quota-dashboard.html"))
    except Exception:  # noqa: BLE001
        pass


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

    threading.Thread(target=_refresh_state, kwargs={"log": True}, daemon=True).start()
    with _STATE_LOCK:
        _STATE["updating"] = True
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
            w.writerow([ts, r["name"], r["status"], r.get("used_pct", ""),
                        r.get("used_text", ""), r.get("reset", ""), r.get("fiveh_text", "")])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", action="store_true", help="追加快照到 quota-log.csv")
    ap.add_argument("--html", action="store_true", help="生成 quota-dashboard.html 仪表盘")
    ap.add_argument("--serve", action="store_true", help="启动本地服务，网页上可直接刷新查询")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--public-html", type=str, default="", help="生成脱敏公开版页面到指定路径")
    args = ap.parse_args()
    if args.serve:
        serve(args.port)
        return 0
    rows = collect()
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
