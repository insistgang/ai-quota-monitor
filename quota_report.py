#!/usr/bin/env python3
"""quota_report.py — AI 额度监控表（查询用量，不发送模型任务或主动重置额度）

配置 Sub2API 后，已映射来源改由服务器读取；未配置时保留以下本机采集器。

监控对象（统一口径：本周已用 + 周重置）：
- Kimi（本人 99/月）：KimiCodeBar credentials 里 alias=Leo-usage 的 key
- Kimi（朋友 199/月）：KimiCodeBar credentials 里 alias=friend* 的 key
- 豆包个人会员：已登录 Chrome 中官方额度页的可见 DOM
- Codex（codex-auth 多账号）：`codex-auth list --json` 一次读取全部已登录账号的官方 5h/周窗额度（API 实时）
- MiniMax：本机 mmx CLI 的 `mmx quota show`（JSON 输出）

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
from collections.abc import Callable
from pathlib import Path

import sub2api_quota

HOME = Path.home()
LOG_CSV = Path(__file__).with_name("quota-log.csv")
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
DOUBAO_QUOTA_URL = "https://www.doubao.com/member/quota-management"
DOUBAO_QUOTA_CACHE = Path(os.environ.get(
    "DOUBAO_QUOTA_CACHE",
    str(HOME / ".cache/ai-quota-monitor/doubao-quota.json"),
))
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
DOUBAO_APPLESCRIPT = Path(__file__).with_name("scripts") / "read_doubao_quota.applescript"
HTTP_TIMEOUT = 15
SOURCE_MAX_AGE_SECONDS = 5 * 60


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


def _snapshot_stale(timestamp) -> bool:
    """只有近五分钟、时间可验证的源快照可跳过实时查询。"""
    try:
        if isinstance(timestamp, str) and "T" in timestamp:
            parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return True
            captured = parsed.timestamp()
        else:
            captured = float(timestamp)
        age = time.time() - captured
        return not -60 <= age <= SOURCE_MAX_AGE_SECONDS
    except (TypeError, ValueError, OverflowError):
        return True


# ---------- 数据源 ----------


def _doubao_pct(line: str) -> tuple[float, str]:
    """解析豆包已用百分比及“已用完/未消耗”状态。"""
    state = line.strip()
    if state == "已用完":
        return 100.0, "100%"
    if state == "未消耗":
        return 0.0, "0%"
    m = re.search(r"已用\s*(<)?\s*(\d+(?:\.\d+)?)\s*%", line)
    if not m:
        raise ValueError("豆包额度百分比缺失")
    value = float(m.group(2))
    if not 0 <= value <= 100:
        raise ValueError("豆包额度百分比超出范围")
    raw = f"{'<' if m.group(1) else ''}{m.group(2)}%"
    return value, raw


def _doubao_reset_value(month: int, day: int, hour: int, minute: int) -> str:
    try:
        dt.datetime(2000, month, day, hour, minute)
    except ValueError as exc:
        raise ValueError("豆包额度重置时间非法") from exc
    return f"{month:02d}-{day:02d} {hour:02d}:{minute:02d}"


def _doubao_reset(line: str, captured_at: dt.datetime) -> str:
    """把豆包的中文绝对/相对重置时间归一化为 MM-DD HH:MM。"""
    if line.strip() == "开始使用后计时":
        return "开始使用后计时"
    absolute = re.search(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})\s*重置", line)
    if absolute:
        month, day, hour, minute = (int(x) for x in absolute.groups())
        return _doubao_reset_value(month, day, hour, minute)

    relative = re.search(
        r"(?:(\d+)\s*天)?\s*(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分钟)?\s*后重置",
        line,
    )
    if relative and any(relative.groups()):
        try:
            days, hours, minutes = (int(x) if x else 0 for x in relative.groups())
            delta = dt.timedelta(days=days, hours=hours, minutes=minutes)
        except (OverflowError, ValueError) as exc:
            raise ValueError("豆包额度重置时间非法") from exc
        if delta <= dt.timedelta(0) or delta > dt.timedelta(days=8):
            raise ValueError("豆包额度重置时间非法")
        try:
            reset_at = captured_at.replace(second=0, microsecond=0) + delta
        except (OverflowError, ValueError) as exc:
            raise ValueError("豆包额度重置时间非法") from exc
        return reset_at.strftime("%m-%d %H:%M")
    raise ValueError("豆包额度重置时间缺失")


def _parse_doubao_dom(text: str, captured_at: dt.datetime | None = None) -> dict:
    """从额度管理页的可见文本中提取最小字段，不保留原始 DOM。"""
    captured_at = captured_at or dt.datetime.now().astimezone()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    current_idx = next(
        (i for i, line in enumerate(lines) if line == "当前时段"),
        None,
    )
    weekly_idx = next(
        (i for i, line in enumerate(lines) if re.fullmatch(r"近\s*7\s*天", line)),
        None,
    )
    if current_idx is None or weekly_idx is None or weekly_idx <= current_idx:
        raise ValueError("未找到豆包“当前时段/近 7 天”额度区")

    def usage_block(start: int, end: int) -> tuple[float, str, str]:
        block = lines[start + 1:end]
        used_line = next((
            line for line in block
            if line.startswith("已用") or line == "未消耗"
        ), "")
        reset_line = next((
            line for line in block
            if "重置" in line or line == "开始使用后计时"
        ), "")
        used_pct, used_text = _doubao_pct(used_line)
        return used_pct, used_text, _doubao_reset(reset_line, captured_at)

    current_pct, current_text, current_reset = usage_block(current_idx, weekly_idx)
    weekly_pct, weekly_text, weekly_reset = usage_block(weekly_idx, len(lines))

    management_idx = next((
        i for i in range(current_idx - 1, -1, -1)
        if "订阅与额度管理" in lines[i]
    ), None)
    plan = "个人会员"
    if management_idx is not None and management_idx > 0:
        candidate = lines[management_idx - 1]
        if candidate.endswith("套餐") and not candidate.startswith(("升级至", "购买")):
            plan = candidate
    trial = ""
    if management_idx is not None:
        match = re.search(
            r"(免费体验至\s*\d{1,2}月\d{1,2}日)",
            lines[management_idx],
        )
        if match:
            trial = match.group(1).replace(" ", "")

    return {
        "name": f"豆包 · {plan}",
        "status": "ok",
        "used_pct": weekly_pct,
        "used_text": weekly_text,
        "reset": weekly_reset,
        "fiveh_pct": current_pct,
        "fiveh_text": current_text,
        "fiveh_reset": current_reset,
        "fiveh_label": "当前时段",
        "note": trial,
    }


def _doubao_snapshot(text: str, captured_at: dt.datetime | None = None) -> dict:
    captured_at = captured_at or dt.datetime.now().astimezone()
    return {
        "schema_version": 1,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "source_url": DOUBAO_QUOTA_URL,
        "row": _parse_doubao_dom(text, captured_at),
    }


def _write_doubao_snapshot(snapshot: dict, cache_path: Path = DOUBAO_QUOTA_CACHE) -> None:
    """原子写入仅含额度数字的 0600 本地缓存。"""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.chmod(0o600)
        temp_path.replace(cache_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _doubao_pct_value_is_valid(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= float(value) <= 100
    )


def _doubao_reset_text_is_valid(value: object) -> bool:
    """重置文案要么是固定时刻，要么是“开始使用后计时”这种滚动窗口提示。

    _doubao_reset() 会把页面上的“开始使用后计时”原样保留，校验器若只认
    MM-DD HH:MM，滚动窗口套餐的快照会被判无效，缓存回退随之失效。
    """
    if value == "开始使用后计时":
        return True
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", value)
    if not match:
        return False
    try:
        _doubao_reset_value(*(int(part) for part in match.groups()))
    except ValueError:
        return False
    return True


def _doubao_fiveh_reset_text_is_valid(value: object) -> bool:
    return _doubao_reset_text_is_valid(value)


def _read_doubao_snapshot(cache_path: Path = DOUBAO_QUOTA_CACHE) -> dict | None:
    try:
        snapshot = json.loads(cache_path.read_text(encoding="utf-8"))
        captured_at = snapshot.get("captured_at")
        if not isinstance(captured_at, str):
            return None
        captured = dt.datetime.fromisoformat(captured_at)
        row = snapshot.get("row")
        if (
            snapshot.get("schema_version") != 1
            or snapshot.get("source_url") != DOUBAO_QUOTA_URL
            or captured.tzinfo is None
            or not isinstance(row, dict)
            or row.get("status") != "ok"
            or not isinstance(row.get("name"), str)
            or not row["name"].startswith("豆包 · ")
            or not _doubao_pct_value_is_valid(row.get("used_pct"))
            or not _doubao_pct_value_is_valid(row.get("fiveh_pct"))
            or not _doubao_reset_text_is_valid(row.get("reset"))
            or not _doubao_fiveh_reset_text_is_valid(row.get("fiveh_reset"))
        ):
            return None
        return snapshot
    except Exception:  # noqa: BLE001
        return None


def _doubao_max_age_hours() -> float:
    try:
        return max(0.25, float(os.environ.get("DOUBAO_QUOTA_MAX_AGE_HOURS", "6")))
    except ValueError:
        return 6.0


DOUBAO_SYNC_ERROR_SHORT = {
    "Chrome 未运行": "Chrome未开",
    "Chrome 中未打开豆包额度管理页": "未开额度页",
    "Chrome 未允许来自 Apple 事件的 JavaScript": "需开Apple事件JS",
    "macOS 未授权自动化控制 Chrome": "需授权自动化",
    "豆包额度页刷新后未加载": "额度页未加载",
    "页面额度格式异常": "页面格式异常",
    "实时刷新异常": "刷新失败",
}


def _doubao_short_error(public_error: str) -> str:
    return DOUBAO_SYNC_ERROR_SHORT.get(public_error, public_error)


def _doubao_age_label(age_hours: float) -> str:
    if age_hours < 1:
        minutes = max(1, int(round(age_hours * 60)))
        return f"过期{minutes}分钟"
    if age_hours < 10:
        value = round(age_hours, 1)
        if value == int(value):
            value = int(value)
        return f"过期{value}小时"
    return f"过期{int(round(age_hours))}小时"


def _doubao_compose_note(
    base_note: str = "",
    *,
    captured: dt.datetime,
    stale: bool,
    age_hours: float,
    cache_fallback: bool = False,
    sync_error: str | None = None,
) -> str:
    parts = [
        part for part in (base_note or "").split(" · ")
        if part.startswith("免费体验至")
    ]
    if cache_fallback or stale:
        parts.append("缓存 " + captured.astimezone().strftime("%m-%d %H:%M"))
        if stale:
            parts.append(_doubao_age_label(age_hours))
    if sync_error:
        parts.append(_doubao_short_error(sync_error))
    return " · ".join(parts)


def _doubao_row_from_snapshot(
    snapshot: dict,
    now: dt.datetime | None = None,
    *,
    cache_fallback: bool = False,
    sync_error: str | None = None,
) -> dict:
    row = dict(snapshot["row"])
    captured = dt.datetime.fromisoformat(snapshot["captured_at"])
    now = now or dt.datetime.now().astimezone()
    age_hours = max(0.0, (now.timestamp() - captured.timestamp()) / 3600)
    row["stale"] = cache_fallback or age_hours > _doubao_max_age_hours()
    row["note"] = _doubao_compose_note(
        row.get("note", ""),
        captured=captured,
        stale=row["stale"],
        age_hours=age_hours,
        cache_fallback=cache_fallback,
        sync_error=sync_error,
    )
    return row


def _read_doubao_chrome_dom() -> str:
    """刷新并读取已打开、已登录的 Chrome 额度页；不启动浏览器或读存储。"""
    if not DOUBAO_APPLESCRIPT.exists():
        raise RuntimeError("豆包 Chrome 读取脚本缺失")
    running = subprocess.run(
        ["pgrep", "-x", "Google Chrome"], capture_output=True, timeout=3, check=False,
    )
    if running.returncode != 0:
        raise RuntimeError("Chrome 未运行")
    result = subprocess.run(
        ["osascript", str(DOUBAO_APPLESCRIPT), DOUBAO_QUOTA_URL, "refresh"],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout).lower()
        if "not authorized" in error or "-1743" in error:
            raise RuntimeError("macOS 未授权自动化控制 Chrome")
        if (
            "javascript through apple events" in error
            or "apple events" in error
            or ("javascript" in error and "apple 事件" in error)
            or ("applescript 执行 javascript" in error and "功能已关闭" in error)
        ):
            raise RuntimeError("Chrome 未允许来自 Apple 事件的 JavaScript")
        if "未找到" in error:
            raise RuntimeError("Chrome 中未打开豆包额度管理页")
        if "刷新后未加载" in error:
            raise RuntimeError("豆包额度页刷新后未加载")
        raise RuntimeError("Chrome DOM 读取失败")
    text = result.stdout.strip()
    if not text:
        raise RuntimeError("豆包额度页 DOM 返回为空")
    return text


def _doubao_public_sync_error(exc: Exception) -> str:
    message = str(exc)
    known_errors = (
        "Chrome 未运行",
        "Chrome 中未打开豆包额度管理页",
        "Chrome 未允许来自 Apple 事件的 JavaScript",
        "macOS 未授权自动化控制 Chrome",
        "豆包额度页刷新后未加载",
    )
    for known in known_errors:
        if known in message:
            return known
    if isinstance(exc, ValueError):
        return "页面额度格式异常"
    return "实时刷新异常"


def _sync_doubao_chrome(
    cache_path: Path = DOUBAO_QUOTA_CACHE,
    captured_at: dt.datetime | None = None,
) -> dict:
    snapshot = _doubao_snapshot(_read_doubao_chrome_dom(), captured_at)
    _write_doubao_snapshot(snapshot, cache_path)
    return snapshot


def _import_doubao_dom(
    text: str,
    cache_path: Path = DOUBAO_QUOTA_CACHE,
    captured_at: dt.datetime | None = None,
) -> dict:
    snapshot = _doubao_snapshot(text, captured_at)
    _write_doubao_snapshot(snapshot, cache_path)
    return snapshot


def _doubao_quota(label: str = "豆包 · 个人会员") -> dict:
    """优先实时读取 Chrome DOM；失败时使用本地最小快照并标明新鲜度。

    豆包额度只能从已登录 Chrome 的额度页读取，页面没开属于外部环境状态，不是采集
    故障。降级行标记 retryable=False，不占用发布容错额度——否则这个来源会长期占满
    额度，让任何一次别的来源抖动都取消整轮发布。具体失败原因仍写在状态和备注里。
    """
    try:
        return _doubao_row_from_snapshot(_sync_doubao_chrome())
    except Exception as exc:  # noqa: BLE001
        public_error = _doubao_public_sync_error(exc)
        snapshot = _read_doubao_snapshot()
        if snapshot:
            try:
                row = _doubao_row_from_snapshot(
                    snapshot,
                    cache_fallback=True,
                    sync_error=public_error,
                )
                row["cache_fallback"] = True
                row["retryable"] = False
                return row
            except Exception:  # noqa: BLE001
                pass
        return {
            "name": label,
            "status": f"{_doubao_short_error(public_error)} · 无缓存",
            "retryable": False,
        }


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


def _rate_window_usable(win) -> bool:
    return isinstance(win, dict) and win.get("used_percent") is not None


def _codex_windows(rl: dict) -> tuple[dict, dict]:
    """按 window_minutes 区分周窗和 5h 窗，避免把 primary 的 5h 数据当成周额度。"""
    week, fiveh = {}, {}
    for key in ("secondary", "primary"):
        win = rl.get(key)
        if not _rate_window_usable(win):
            continue
        try:
            minutes = int(win.get("window_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes >= 24 * 60:
            week = win
        elif minutes and minutes <= 12 * 60:
            fiveh = win
    if not week and not fiveh:
        named_week = rl.get("secondary") if _rate_window_usable(rl.get("secondary")) else {}
        named_fiveh = rl.get("primary") if _rate_window_usable(rl.get("primary")) else {}
        week, fiveh = named_week or {}, named_fiveh or {}
    return week or {}, fiveh or {}


def _fiveh_window_active(win: dict) -> bool:
    """resets_at 已过期的 5h 窗是旧会话快照，继续展示会误导，直接丢弃。"""
    resets = win.get("resets_at")
    if resets is None:
        return True
    try:
        return float(resets) > time.time()
    except (TypeError, ValueError):
        return True


def _codex_row(label: str, rl: dict, note: str = "") -> dict:
    week, fiveh = _codex_windows(rl)
    if fiveh and not _fiveh_window_active(fiveh):
        fiveh = {}
    if not week and fiveh:
        note = (note + " · " if note else "") + "仅5h窗数据"
        row = {
            "name": label,
            "status": "ok",
            "used_pct": None,
            "used_text": "周窗未知",
            "reset": "?",
            "note": note,
            "fiveh_text": f"{fiveh['used_percent']:g}%",
            "fiveh_pct": fiveh["used_percent"],
            "fiveh_reset": _fmt_epoch(fiveh.get("resets_at")),
        }
        return row
    used = week.get("used_percent")
    row = {
        "name": label,
        "status": "ok",
        "used_pct": used,
        "used_text": f"{used}%" if used is not None else "?",
        "reset": _fmt_epoch(week.get("resets_at")),
        "note": note or (rl.get("plan_type") or ""),
    }
    if fiveh.get("used_percent") is not None:
        row["fiveh_text"] = f"{fiveh['used_percent']:g}%"
        row["fiveh_pct"] = fiveh["used_percent"]
        row["fiveh_reset"] = _fmt_epoch(fiveh.get("resets_at"))
    return row


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
        AGY_GROUPS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        AGY_GROUPS_CACHE.write_text(
            json.dumps({"captured_at": time.time(), "groups": groups}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _enrich_antigravity_rows(rows: list[dict]) -> list[dict]:
    """为 Sub2API 拆出的 Gemini · 3.8 Flash / Gemini · Claude 补齐周额度 + 5h 双窗口。"""
    needs_enrichment = any(
        r.get("status") == "ok"
        and r.get("name") in {"Gemini · 3.8 Flash", "Gemini · Claude"}
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

    if not cache_fresh or "Gemini · 3.8 Flash" not in groups:
        text = _tmux_slash_probe(_agy_bin(), "/usage", wait_boot=12, wait_panel=6, pre_enter=False)
        if text:
            parsed_groups: dict[str, dict] = {}
            for marker, target_name in (
                ("GEMINI MODELS", "Gemini · 3.8 Flash"),
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
            and name in {"Gemini · 3.8 Flash", "Gemini · Claude"}
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


def _agy() -> list[dict]:
    """Antigravity：从真实 TUI /usage 面板读取官方真实周额度与 5h 窗（只取 Gemini 组）。"""
    bin_path = _agy_bin()
    text = _tmux_slash_probe(bin_path, "/usage", wait_boot=12, wait_panel=6, pre_enter=False)
    if not text:
        return [{"name": "Antigravity · Gemini 组", "status": "TUI 探测失败"}]
    g = _agy_parse_group(text, "GEMINI MODELS")
    if not g:
        return [{"name": "Antigravity · Gemini 组", "status": "面板解析失败"}]
    g.update({"name": "Antigravity · Gemini 组", "status": "ok", "note": "Google AI Pro"})
    return [g]


def _codex_auth_accounts() -> list[dict]:
    """Codex 多账号：`codex-auth list --json` 一次取回全部已登录账号的官方 5h/周窗额度。"""
    bin_path = shutil.which("codex-auth") or str(HOME / ".local/bin/codex-auth")
    try:
        out = subprocess.run(
            [bin_path, "list", "--json"],
            capture_output=True, timeout=60, check=False,
        )
    except FileNotFoundError:
        return [{"name": "Codex（codex-auth）", "status": "codex-auth 未安装"}]
    except Exception as e:  # noqa: BLE001
        return [{"name": "Codex（codex-auth）", "status": f"查询失败: {type(e).__name__}"}]
    if out.returncode != 0:
        return [{"name": "Codex（codex-auth）", "status": "codex-auth list 执行失败"}]
    try:
        data = json.loads(out.stdout.decode("utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        return [{"name": "Codex（codex-auth）", "status": "codex-auth 输出解析失败"}]
    rows = []
    for acct in data.get("accounts", []):
        if not isinstance(acct, dict):
            continue
        email = acct.get("email") or f"账号{acct.get('number', '?')}"
        label = f"Codex · {email}"
        usage = acct.get("usage") or {}
        refresh = usage.get("refresh") or {}
        if not (
            _rate_window_usable(usage.get("primary"))
            or _rate_window_usable(usage.get("secondary"))
        ):
            rows.append({"name": label, "status": refresh.get("error_code") or "无额度数据"})
            continue
        note_parts = []
        plan = acct.get("plan")
        if plan:
            note_parts.append(str(plan).capitalize())
        if acct.get("active"):
            note_parts.append("当前活跃")
        refresh_failed = bool(refresh and refresh.get("status") != "ok")
        if refresh_failed:
            note_parts.append("API 刷新失败，数据可能滞后")
        updated = usage.get("updated_at")
        if updated:
            note_parts.append(f"数据 {_fmt_epoch(updated)}")
        stale = _snapshot_stale(updated)
        if stale:
            note_parts.append("快照过期" if updated else "数据时间未知")
        row = _codex_row(label, usage, note=" · ".join(note_parts))
        row["stale"] = refresh_failed or stale or usage.get("source") == "cache"
        row["cache_fallback"] = refresh_failed or usage.get("source") == "cache"
        row["captured_at"] = updated
        rows.append(row)
    if not rows:
        return [{"name": "Codex（codex-auth）", "status": "codex-auth 未登录任何账号"}]
    return rows


def _mmx_bin() -> str | None:
    if shutil.which("mmx"):
        return "mmx"
    cands = sorted(Path.home().glob(".nvm/versions/node/*/bin/mmx"))
    return str(cands[-1]) if cands else None


def _minimax(label: str) -> dict:
    """MiniMax：本机 mmx-cli 的 `mmx quota show` 读取官方真实账号剩余额度。"""
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
    mine = friend = None
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
            elif "friend" in alias:
                friend = key
    except Exception:  # noqa: BLE001
        pass
    return mine, friend


def _load_kimi_keys() -> tuple[str | None, str | None]:
    mine, friend = _kimi_keys_from_codebar()
    if mine or friend:
        return mine, friend
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
            friend = out.stdout.decode().strip() or None
    except Exception:  # noqa: BLE001
        pass
    return mine, friend


def _collect_batches_with_retries(
    batches: list[tuple[str, Callable[[], list[dict]]]],
    *,
    retry_attempts: int = 0,
    retry_delay: float = 0,
) -> list[dict]:
    """采集各来源；只重试异常或缓存回退的批次，成功批次保持原结果。"""
    results = {key: collector() for key, collector in batches}
    for _attempt in range(max(0, retry_attempts)):
        retry_keys = {
            key
            for key, rows in results.items()
            if not rows or any(
                row.get("status") != "ok" or row.get("stale")
                for row in rows
            )
        }
        if not retry_keys:
            break
        if retry_delay > 0:
            time.sleep(retry_delay)
        for key, collector in batches:
            if key in retry_keys:
                results[key] = collector()
    return [row for key, _collector in batches for row in results[key]]


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
        LAST_GOOD_JSON.parent.mkdir(parents=True, exist_ok=True)
        LAST_GOOD_JSON.write_text(
            json.dumps(
                {"captured_at": dt.datetime.now().isoformat(timespec="seconds"), "rows": fresh},
                ensure_ascii=False,
            ),
            encoding="utf-8",
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
    source = os.environ.get("QUOTA_SOURCE", "auto")
    if source not in {"auto", "local", "sub2api"}:
        raise sub2api_quota.ConfigError("QUOTA_SOURCE 只能为 auto、local 或 sub2api")
    config = sub2api_quota.load_config() if source != "local" else None
    if source == "sub2api" and config is None:
        raise sub2api_quota.ConfigError("尚未配置 Sub2API 额度数据源")
    rows, covered = [], set()
    if config:
        covered = {a["collector"] for a in config["accounts"]}
        covered.update(a["collector"] for a in config.get("unmapped_accounts", []))
        rows = _enrich_antigravity_rows(
            sub2api_quota.collect(config, retry_attempts=retry_attempts, retry_delay=retry_delay)
        )

    keys = None

    def kimi_row(index: int, label: str) -> list[dict]:
        nonlocal keys
        if keys is None:
            keys = _load_kimi_keys()
        return [_kimi_quota(label, keys[index])]

    batches = [
        ("kimi_mine", lambda: kimi_row(0, "Kimi · 本人（948/年）")),
        ("kimi_shared", lambda: kimi_row(1, "Kimi · 副账号")),
        ("codex_auth", _codex_auth_accounts),
        ("minimax", lambda: [_minimax("MiniMax")]),
        ("antigravity", _agy),
    ]
    rows.extend(_collect_batches_with_retries(
        [(name, collector) for name, collector in batches if name not in covered],
        retry_attempts=retry_attempts,
        retry_delay=retry_delay,
    ))
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
        if r.get("status") != "ok" or r.get("stale"):
            continue
        if _quota_hidden(r.get("name")):
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
        # 周额度已用完时，5h 窗剩余同样无法使用，不再提示抓紧用。
        if rem is not None and rem <= 3:
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
        f"<li><b>{_html_text(a['name'])}</b> · "
        f"{_html_text('周额度' if a['kind']=='week' else a.get('window_label', '5h 窗'))} "
        f"{_html_text(a['until'])}刷新"
        f"，还剩 {a['remain']:.0f}%，抓紧用</li>"
        for a in alerts
    )
    return f"""<div class="alertband"><h3>快刷新了，剩余额度抓紧用</h3><ul>{items}</ul></div>"""


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
        return f"""<div class="card" data-provider="{pkey}"><div class="top"><div class="title-wrap"><span class="pbadge">{picon} {pbadge}</span><span class="name">{name}</span></div>
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
            window_label = _html_text(r.get("fiveh_label", "5h 窗"))
            fiveh = f"""<div class="sub-box{f_exh}"><div class="sub-head"><span class="sub">⚡ {window_label}（5h 限流窗）</span><span class="sub-stats">{f_pct_badge}{f_rem_badge}</span></div>
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
    if r.get("cache_fallback"):
        pill = "<span class='pill stale'>使用缓存</span>"
    elif r.get("stale"):
        pill = "<span class='pill stale'>快照过期</span>"
    else:
        fp_val = r.get("fiveh_pct")
        if rem is not None and rem <= 3:
            pill = "<span class='pill bad'>已用完</span>"
        elif fp_val is not None and fp_val >= 100:
            pill = "<span class='pill bad'>5h 已满</span>"
        else:
            pill = "<span class='pill warn'>抓紧用</span>" if alert else "<span class='pill ok'>正常</span>"
    return f"""<div class="card{urgent}" data-provider="{pkey}"><div class="top"><div class="title-wrap"><span class="pbadge">{picon} {pbadge}</span><span class="name">{name}</span></div>
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
        rem = _remain_pct(r)
        if rem is not None:
            rem_vals.append(rem)
        fp = r.get("fiveh_pct")
        if (rem is not None and rem <= 3) or (fp is not None and fp >= 100):
            exhausted += 1
        elif r.get("name") in alert_names or (rem is not None and rem <= 20) or r.get("stale"):
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


def _strip_md(s: str) -> str:
    return s.replace("**", "").replace("~~", "").strip()


def _load_finance() -> dict:
    """解析订阅、私有预存余额、历史充值、非 AI 订阅与合计口径。"""
    out = {"subs": [], "prepaid": [], "ledger": [], "non_ai": [], "total_ai": "", "total_all": ""}
    if FINANCE_MD is None:
        return out
    try:
        text = FINANCE_MD.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return out
    section = ""
    prepaid_public_column = None
    for line in text.splitlines():
        if line.startswith("## 一、"):
            section = "subs"; continue
        if line.startswith("## 二、"):
            section = "ledger"; continue
        if line == "## ChatGPT 预存余额与扣费计划（私密）":
            prepaid_public_column = None
            section = "prepaid"; continue
        if line.startswith("## "):
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
        m3 = re.match(r"^\s*-\s*(iCloud|得到会员|得到大脑会员|微信读书|百度网盘|视频会员)：(.+)$", line)
        if m3:
            out["non_ai"].append(f"{m3.group(1)}：{_strip_md(m3.group(2))}")
    return out


_SVC_ICONS = [
    ("ChatGPT", "🤖"), ("Kimi", "🌙"), ("Gemini", "♊"),
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
    prepaid_accounts = fin.get("prepaid", [])
    if public:
        prepaid_accounts = [
            {**account, "name": account["public_name"]}
            for account in prepaid_accounts if account.get("public_name")
        ]
    if not fin["subs"] and not fin["ledger"] and not prepaid_accounts:
        return ""

    def pub(s: str) -> str:
        return s.replace("朋友", "朋友") if public else s

    prepaid_html = ""
    if prepaid_accounts:
        prepaid_cards = "".join(
            f'<div class="scard"><div class="sname">{_html_text(r["name"])}</div>'
            f'<div class="scost">余额 {_html_text(r["balance"])}</div>'
            f'<div class="smeta">截至 {_html_text(r["as_of"])} · 每次扣费 {_html_text(r["debit"])}</div>'
            f'<div class="meta">可覆盖扣费：{_html_text(r["covered"])}</div>'
            f'<div class="meta">需在 {_html_text(r["next_topup"])} 扣费前补款</div></div>'
            for r in prepaid_accounts
        )
        prepaid_html = (
            '<h2>💰 ChatGPT 预存余额与扣费计划</h2>'
            f'<div class="sgrid">{prepaid_cards}</div>'
            '<div class="sub">TRY 为土耳其里拉。余额为所示日期的人工记录；扣费计划按每次金额不变估算，未来扣款尚未记为支出。</div>'
        )

    cards = []
    for r in fin["subs"]:
        if _quota_hidden(r["name"]):
            continue
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
{prepaid_html}
<h2>📒 消费台账（历史充值）</h2>
<table class="ledger"><tr><td>日期</td><td>项目</td><td>金额</td></tr>{ledger_trs}</table>
<h2>🧾 非 AI 固定订阅</h2>
<div class="sgrid">{non_ai}</div>"""


PUBLIC_LABELS = {
    "Kimi · 本人（948/年）": "Kimi · 主账号",
    "Kimi · 副账号": "Kimi · 副账号",
}

_HIST_PUBLIC = {"Kimi · 本人": "Kimi · 主账号", "Kimi · 副账号": "Kimi · 副账号"}


def _mask_email(text) -> str:
    """网页不暴露完整邮箱：保留首字符，其余打码。"""
    return re.sub(r"([\w.+-])[\w.+-]*(?=@)", r"\1***", str(text))


def _public_name(name: str) -> str:
    return _mask_email(PUBLIC_LABELS.get(name, _HIST_PUBLIC.get(name, name)))


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
    per: dict[tuple[str, str], list[tuple[str, float, str]]] = {}
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
            if _quota_hidden(name) or name.casefold() in hidden_names:
                continue
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
    history_days = _daily_deltas(days=days, public=public)
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
                name = _public_name(r["name"]) if public else r["name"]
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
            name = _public_name(r["name"]) if public else r["name"]
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
    rows = _visible_quota_rows(rows)
    if public:
        hidden_names = _public_hidden_codex_names()
        rows = [row for row in rows if (row.get("name") or "").casefold() not in hidden_names]
    if page not in {"all", "overview", "history", "subscriptions"}:
        raise ValueError(f"unknown dashboard page: {page}")
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _sort_rows(list(rows))
    if public:
        rows = [{**r, "name": _public_name(r["name"])} for r in rows]
    alerts = _alerts(rows)
    alert_by_name = {a["name"]: a for a in alerts}
    cards = "".join(_card(r, alert_by_name.get(r["name"])) for r in rows)
    banner = _alerts_html(alerts)
    summary_strip = _summary_strip_html(rows, alerts)
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

    if page == "overview":
        heading = "🤖 AI 额度监控"
        title = "AI 额度监控"
        detail = "卡片按周剩余从小到大"
        content = (
            f'{live_ui}{summary_strip}{banner}<div class="grid">{cards}</div>'
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
            f'{live_ui}{summary_strip}{banner}<div class="grid">{cards}</div>'
            f'{_history_html(public)}{_subs_html(public)}'
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
body{{background:radial-gradient(ellipse 90% 55% at 50% -18%,rgba(56,139,253,.16),transparent 65%),radial-gradient(ellipse 60% 40% at 85% 10%,rgba(163,113,247,.08),transparent 60%),#090d13;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","PingFang SC","Hiragino Sans GB",system-ui,sans-serif;padding:28px 28px 56px;max-width:1180px;margin:0 auto;line-height:1.45;-webkit-font-smoothing:antialiased}}
.page-head{{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 20px;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid rgba(240,246,252,.07)}}
h1{{font-size:26px;font-weight:750;letter-spacing:-.025em;margin:0 0 6px;color:#f0f6fc;display:flex;align-items:center;gap:10px}}
.ts{{color:#8b949e;font-size:13px;margin:0;line-height:1.5;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.ts::before{{content:"";display:inline-block;width:7px;height:7px;border-radius:99px;background:#3fb950;box-shadow:0 0 8px rgba(63,185,80,.75)}}
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
.kpi{{background:linear-gradient(180deg,rgba(24,30,40,.9) 0%,rgba(16,20,28,.95) 100%);border:1px solid rgba(48,54,61,.85);border-radius:14px;padding:13px 16px;display:flex;flex-direction:column;gap:3px;box-shadow:0 4px 14px rgba(0,0,0,.18),inset 0 1px 0 rgba(255,255,255,.04)}}
.kpi-label{{font-size:11.5px;font-weight:600;color:#8b949e;letter-spacing:.02em}}
.kpi-val{{font-size:24px;font-weight:750;color:#f0f6fc;letter-spacing:-.03em;line-height:1.15;font-variant-numeric:tabular-nums}}
.kpi-sub{{font-size:11px;color:#6e7681;margin-top:1px}}
.kpi.ok .kpi-val{{color:#3fb950}}
.kpi.warn .kpi-val{{color:#f0883e}}
.kpi.bad .kpi-val{{color:#ff7b72}}
.kpi.accent .kpi-val{{color:#58a6ff}}
table{{border-collapse:separate;border-spacing:0;width:100%;background:rgba(22,27,34,.85);border:1px solid rgba(48,54,61,.85);border-radius:14px;overflow:hidden}}
td{{border-bottom:1px solid rgba(48,54,61,.6);padding:10px 14px;font-size:14px}}
tr:last-child td{{border-bottom:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px;align-items:stretch}}
.card{{position:relative;overflow:hidden;display:flex;flex-direction:column;background:linear-gradient(180deg,rgba(26,32,43,.94) 0%,rgba(16,21,29,.98) 100%);border:1px solid rgba(56,63,73,.78);border-radius:16px;padding:18px 20px;box-shadow:0 6px 22px rgba(0,0,0,.26),inset 0 1px 0 rgba(255,255,255,.05);transition:transform .2s cubic-bezier(.16,1,.3,1),border-color .2s ease,box-shadow .2s ease}}
.card::before{{content:"";position:absolute;top:0;left:0;right:0;height:2.5px;background:linear-gradient(90deg,rgba(88,166,255,.65),rgba(88,166,255,.1));opacity:.85}}
.card[data-provider="codex"]::before{{background:linear-gradient(90deg,#10b981,#34d399)}}
.card[data-provider="claude"]::before{{background:linear-gradient(90deg,#f97316,#fb923c)}}
.card[data-provider="gemini"]::before{{background:linear-gradient(90deg,#3b82f6,#60a5fa)}}
.card[data-provider="argon"]::before{{background:linear-gradient(90deg,#8b5cf6,#a78bfa)}}
.card[data-provider="kimi"]::before{{background:linear-gradient(90deg,#06b6d4,#38bdf8)}}
.card[data-provider="minimax"]::before{{background:linear-gradient(90deg,#ec4899,#f472b6)}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px}}
.title-wrap{{display:flex;flex-direction:column;gap:4px;min-width:0}}
.pbadge{{display:inline-flex;align-items:center;gap:4px;width:max-content;font-size:10.5px;font-weight:600;color:#8b949e;background:rgba(110,118,129,.13);border:1px solid rgba(240,246,252,.07);padding:2px 7px;border-radius:6px;letter-spacing:.02em}}
.card[data-provider="claude"] .pbadge{{color:#fdba74;background:rgba(249,115,22,.12);border-color:rgba(249,115,22,.24)}}
.card[data-provider="gemini"] .pbadge{{color:#93c5fd;background:rgba(59,130,246,.12);border-color:rgba(59,130,246,.24)}}
.card[data-provider="argon"] .pbadge{{color:#c4b5fd;background:rgba(139,92,246,.12);border-color:rgba(139,92,246,.24)}}
.card[data-provider="codex"] .pbadge{{color:#6ee7b7;background:rgba(16,185,129,.12);border-color:rgba(16,185,129,.24)}}
.card[data-provider="kimi"] .pbadge{{color:#7dd3fc;background:rgba(6,182,212,.12);border-color:rgba(6,182,212,.24)}}
.card[data-provider="minimax"] .pbadge{{color:#f9a8d4;background:rgba(236,72,153,.12);border-color:rgba(236,72,153,.24)}}
.name{{font-size:15.5px;font-weight:650;color:#f0f6fc;letter-spacing:-.01em;line-height:1.3;word-break:break-word}}
.win-label{{font-size:11px;font-weight:600;color:#8b949e;letter-spacing:.01em;margin-bottom:2px}}
.pill{{font-size:11px;font-weight:600;padding:3px 9px;border-radius:99px;letter-spacing:.02em;flex-shrink:0;border:1px solid transparent;margin-top:2px}}
.pill.ok{{background:rgba(63,185,80,.12);color:#3fb950;border-color:rgba(63,185,80,.28)}}
.pill.bad{{background:rgba(248,81,73,.14);color:#ff7b72;border-color:rgba(248,81,73,.32)}}
.pill.stale{{background:rgba(139,148,158,.14);color:#8b949e;border-color:rgba(139,148,158,.28)}}
.pill.warn{{background:rgba(240,136,62,.14);color:#f0883e;border-color:rgba(240,136,62,.32)}}
.card.urgent{{border-color:rgba(240,136,62,.55);background:linear-gradient(180deg,rgba(240,136,62,.08) 0%,rgba(16,21,29,.98) 48%)}}
.stat-row{{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin:2px 0 9px}}
.pct{{font-size:36px;font-weight:750;letter-spacing:-1.5px;line-height:1;font-variant-numeric:tabular-nums}}
.remain{{font-size:12.5px;font-weight:600;color:#c9d1d9;background:rgba(110,118,129,.15);border:1px solid rgba(240,246,252,.08);padding:3px 9px;border-radius:7px;font-variant-numeric:tabular-nums}}
.alertband{{background:linear-gradient(135deg,rgba(240,136,62,.13) 0%,rgba(210,153,34,.05) 100%);border:1px solid rgba(240,136,62,.38);border-left:4px solid #f0883e;border-radius:14px;padding:14px 18px;margin:0 0 20px;box-shadow:0 4px 16px rgba(0,0,0,.18)}}
.alertband h3{{margin:0 0 8px;font-size:14.5px;font-weight:650;color:#f0883e}}
.alertband ul{{margin:0;padding-left:18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:6px 18px}}
.alertband li{{font-size:13.5px;color:#e6edf3;margin:0}}
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
.meta.nonai{{padding:6px 0;border-bottom:1px dashed #21262d}}
.card:hover,.scard:hover{{transform:translateY(-2px);border-color:rgba(88,166,255,.55);box-shadow:0 12px 30px rgba(0,0,0,.38),0 0 0 1px rgba(88,166,255,.12)}}
.sgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}}
.scard{{background:linear-gradient(180deg,rgba(28,34,44,.9) 0%,rgba(19,24,32,.95) 100%);border:1px solid rgba(48,54,61,.85);border-radius:14px;padding:15px 18px;transition:transform .2s ease,border-color .2s ease,box-shadow .2s ease}}
.scard.mini{{padding:11px 15px}}
.sname{{font-size:14px;font-weight:600;color:#f0f6fc;margin-bottom:6px}}
.scost{{font-size:19px;font-weight:700;color:#58a6ff;margin-bottom:6px;font-variant-numeric:tabular-nums}}
.smeta{{font-size:12.5px;color:#8b949e}}
.cd{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;margin-left:6px;white-space:nowrap}}
.cd.green{{background:rgba(63,185,80,.14);color:#3fb950}}
.cd.yellow{{background:rgba(210,153,34,.14);color:#d29922}}
.cd.red{{background:rgba(248,81,73,.14);color:#ff7b72}}
.totalband{{background:linear-gradient(135deg,rgba(88,166,255,.12),rgba(88,166,255,.04));border:1px solid rgba(88,166,255,.32);border-radius:12px;padding:12px 18px;font-size:15px;font-weight:600;margin-bottom:16px}}
table.ledger td{{font-size:14px;padding:9px 14px}}
table.ledger td.amt{{color:#d29922;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}}
.meta.dim{{color:#6e7681;font-size:11.5px;margin-top:auto;padding-top:11px;border-top:1px solid rgba(48,54,61,.45)}}
.card > .main-meta + .meta.dim{{margin-top:auto}}
.card > .sub-box + .meta.dim{{margin-top:11px}}
.sub{{font-size:11.5px;color:#8b949e;margin-top:12px;line-height:1.5}}
.msg{{font-size:13px;color:#d29922;margin-top:6px}}
.hday{{background:linear-gradient(180deg,rgba(28,34,44,.88) 0%,rgba(19,24,32,.94) 100%);border:1px solid rgba(48,54,61,.85);border-radius:14px;padding:14px 18px;margin-bottom:12px;box-shadow:0 2px 10px rgba(0,0,0,.16)}}
.hdate{{font-size:13px;font-weight:600;color:#8b949e;margin-bottom:10px;font-variant-numeric:tabular-nums}}
.hrow{{display:flex;align-items:center;gap:12px;margin:6px 0;font-size:13px}}
.hrow.dim{{color:#8b949e}}
.hname{{width:176px;flex-shrink:0;color:#c9d1d9;font-weight:500}}
.hbar{{flex:1;height:9px;background:rgba(110,118,129,.16);border-radius:99px;overflow:hidden}}
.hfill{{height:100%;background:linear-gradient(90deg,#1f6feb 0%,#58a6ff 100%);border-radius:99px}}
.hval{{width:76px;text-align:right;color:#58a6ff;font-weight:600;flex-shrink:0;font-variant-numeric:tabular-nums}}
.section-head{{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-top:34px}}
.section-head h2{{margin:0 0 10px}}
.more-link{{font-size:13px;font-weight:500;white-space:nowrap}}
.history-tools{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 0 14px}}
.range-switch{{display:flex;gap:6px}}
.range-switch button,.source-filter select{{border:1px solid rgba(48,54,61,.9);border-radius:8px;background:#161b22;color:#c9d1d9;padding:7px 12px;font-size:12.5px;font-weight:500;transition:all .15s}}
.range-switch button{{cursor:pointer}}
.range-switch button:hover{{border-color:#58a6ff;color:#f0f6fc}}
.range-switch button.active{{background:#1f6feb;border-color:#1f6feb;color:#fff;box-shadow:0 2px 8px rgba(31,111,235,.35)}}
.source-filter{{display:flex;align-items:center;gap:8px;color:#8b949e;font-size:12.5px}}
@media(min-width:960px){{
  .grid{{grid-template-columns:repeat(3,1fr)}}
}}
@media(min-width:601px) and (max-width:959px){{
  .grid{{grid-template-columns:repeat(2,1fr)}}
  .summary-strip{{grid-template-columns:repeat(3,minmax(0,1fr))}}
}}
@media(max-width:600px){{
  body{{padding:18px 14px 32px}}
  .nav{{top:6px;width:100%;justify-content:space-between}}
  .nav a{{padding:7px 9px}}
  .summary-strip{{grid-template-columns:repeat(2,minmax(0,1fr))}}
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
<header class="page-head"><div><h1>{heading}</h1>
<div class="ts">更新于 {now} · {"公开快照 · 每小时自动更新" if public else "统一口径：本周已用 / 周重置 · 数据源全部本机只读"} · {detail}</div></div></header>
<main id="quotaContent">{content}</main>
{freshness}
</body></html>"""
    if path is not None:
        path.write_text(html, encoding="utf-8")
    return html


def render_public_site(rows: list[dict], index_path: Path) -> None:
    """生成 GitHub Pages 的概览、每日消耗和订阅账单三个静态页面。"""
    _public_hidden_codex_names()
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
        rows = collect()
    except sub2api_quota.ConfigError as exc:
        with _STATE_LOCK:
            _STATE["updating"] = False
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
    ap.add_argument(
        "--sync-doubao-chrome",
        action="store_true",
        help="从已打开、已登录的 Chrome 豆包额度页读取可见 DOM 并更新本地快照",
    )
    ap.add_argument(
        "--import-doubao-dom",
        metavar="PATH",
        help="从 PATH 导入豆包额度页可见文本；使用 - 从标准输入读取",
    )
    args = ap.parse_args()
    if args.sync_doubao_chrome:
        try:
            snapshot = _sync_doubao_chrome()
        except Exception as exc:  # noqa: BLE001
            print(f"豆包额度同步失败：{exc}", file=sys.stderr)
            return 1
        print(json.dumps(_doubao_row_from_snapshot(snapshot), ensure_ascii=False, indent=1))
        return 0
    if args.import_doubao_dom:
        try:
            if args.import_doubao_dom == "-":
                dom_text = sys.stdin.read()
            else:
                dom_text = Path(args.import_doubao_dom).read_text(encoding="utf-8")
            snapshot = _import_doubao_dom(dom_text)
        except Exception as exc:  # noqa: BLE001
            print(f"豆包额度导入失败：{exc}", file=sys.stderr)
            return 1
        print(json.dumps(_doubao_row_from_snapshot(snapshot), ensure_ascii=False, indent=1))
        return 0
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
