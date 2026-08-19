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
import json
import re
import shutil
import subprocess
import sys
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
            # TUI 不显示重置时间；用最新会话文件的 resets_at 推算
            reset = "滚动周窗"
            try:
                files = sorted((HOME / ".codex/sessions").rglob("rollout-*.jsonl"),
                               key=lambda p: p.stat().st_mtime)
                for f in reversed(files[-5:]):
                    rl = _extract_rate_limits(f)
                    if rl and rl.get("primary", {}).get("resets_at"):
                        reset = _fmt_epoch(rl["primary"]["resets_at"]) + "（推算）"
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


def _minimax(label: str) -> dict:
    """MiniMax：本机 mmx-cli 的 `mmx quota show`（JSON 输出）。
    取 model_name=general：current_weekly_remaining_percent / current_interval_remaining_percent（5h 窗）。"""
    if not shutil.which("mmx"):
        return {"name": label, "status": "mmx-cli 未安装"}
    try:
        out = subprocess.run(["mmx", "quota", "show"], capture_output=True, timeout=20, check=False)
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
        _kimi_quota("Kimi · 本人（99/月）", mine),
        _kimi_quota("Kimi · Andy（199/月）", andy),
        _codex_local("Codex · Mac"),
        _codex_win("Codex · Win"),
        _grok("Grok · SuperGrok"),
        _minimax("MiniMax · Plus"),
    ] + _agy()
    return rows


# ---------- 输出 ----------

def render(rows: list[dict]) -> str:
    lines = [f"# AI 额度快照 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    for r in rows:
        if r.get("status") != "ok":
            lines.append(f"- **{r['name']}**：{r['status']}")
            continue
        s = f"- **{r['name']}**：本周已用 {r['used_text']}"
        if r.get("used_pct") is not None and "%" not in str(r["used_text"]):
            s += f"（{r['used_pct']}%）"
        s += f"，周重置 {r.get('reset', '?')}"
        if r.get("fiveh_text"):
            s += f"，5h窗已用 {r['fiveh_text']}（重置 {r.get('fiveh_reset', '?')}）"
        if r.get("note"):
            s += f"，{r['note']}"
        lines.append(s)
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


def _card(r: dict) -> str:
    name = r["name"]
    if r.get("status") != "ok":
        return f"""<div class="card"><div class="top"><span class="name">{name}</span>
<span class="pill bad">异常</span></div><div class="msg">{r['status']}</div></div>"""
    pct = r.get("used_pct")
    pct_txt = f"{pct:g}%" if pct is not None else "—"
    c = _color(pct)
    fiveh = ""
    if r.get("fiveh_text"):
        fp = r.get("fiveh_pct")
        fc = _color(fp)
        fiveh = f"""<div class="sub">5h 窗</div>
<div class="bar small"><div class="fill" style="width:{fp or 0}%;background:{fc}"></div></div>
<div class="meta">已用 {r['fiveh_text']} · 重置 {r.get('fiveh_reset', '?')}</div>"""
    note = f"<div class='meta dim'>{r['note']}</div>" if r.get("note") else ""
    return f"""<div class="card"><div class="top"><span class="name">{name}</span>
<span class="pill ok">正常</span></div>
<div class="pct" style="color:{c}">{pct_txt}</div>
<div class="bar"><div class="fill" style="width:{pct or 0}%;background:{c}"></div></div>
<div class="meta">本周已用 {r.get('used_text', '?')} · 重置 {r.get('reset', '?')}</div>
{fiveh}{note}</div>"""


def render_html(rows: list[dict], path: Path) -> None:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cards = "".join(_card(r) for r in rows)
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>AI 额度监控 · {now}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🤖</text></svg>">
<style>
*{{box-sizing:border-box}}
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,"PingFang SC",sans-serif;padding:28px;max-width:1080px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}} .ts{{color:#8b949e;font-size:12px;margin-bottom:20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px 18px}}
.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}}
.name{{font-size:14px;font-weight:600}}
.pill{{font-size:11px;padding:2px 8px;border-radius:99px}}
.pill.ok{{background:rgba(63,185,80,.15);color:#3fb950}}
.pill.bad{{background:rgba(248,81,73,.15);color:#f85149}}
.pct{{font-size:34px;font-weight:700;letter-spacing:-1px;margin:2px 0 8px}}
.bar{{height:8px;background:#21262d;border-radius:99px;overflow:hidden}}
.bar.small{{height:5px;margin-top:4px}}
.fill{{height:100%;border-radius:99px;transition:width .4s}}
.meta{{font-size:12px;color:#c9d1d9;margin-top:8px}}
.meta.dim{{color:#8b949e}}
.sub{{font-size:11px;color:#8b949e;margin-top:12px}}
.msg{{font-size:13px;color:#d29922}}
</style></head><body>
<h1>🤖 AI 额度监控</h1>
<div class="ts">生成于 {now} · 统一口径：本周已用 / 周重置 · 数据源全部本机只读</div>
<div class="grid">{cards}</div>
</body></html>"""
    path.write_text(html, encoding="utf-8")


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
    args = ap.parse_args()
    rows = collect()
    if args.log:
        append_log(rows)
    if args.html:
        out = Path(__file__).with_name("quota-dashboard.html")
        render_html(rows, out)
        print(f"dashboard: {out}")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
