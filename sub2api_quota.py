"""Read provider quota from the owner's Sub2API host, without local OAuth copies."""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import re
import shlex
import subprocess
import time
from pathlib import Path


CONFIG_PATH = Path.home() / ".cache/ai-quota-monitor/sub2api.json"
REMOTE_SCRIPT = Path(__file__).with_name("scripts") / "sub2api_quota_snapshot.py"
COLLECTORS = {
    "openai": {"codex_auth"}, "kimi": {"kimi_mine", "kimi_shared"},
    "minimax": {"minimax"}, "antigravity": {"antigravity"},
}


class ConfigError(ValueError):
    """Only fixed, non-secret diagnostics cross this boundary."""


def load_config(path: Path | None = None) -> dict | None:
    path = path or Path(os.environ.get("QUOTA_SUB2API_CONFIG", str(CONFIG_PATH)))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError
        if data.get("enabled") is False:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", data.get("ssh_host", "")):
            raise ValueError
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or not 1 <= len(accounts) <= 16:
            raise ValueError
        ids, names = set(), set()
        for account in accounts:
            account_id, name = account.get("id"), account.get("name")
            if type(account_id) is not int or account_id <= 0 or account_id in ids:
                raise ValueError
            if not isinstance(name, str) or not name.strip() or len(name) > 200 or re.search(r"[\x00-\x1f]", name) or name in names:
                raise ValueError
            if account.get("collector") not in COLLECTORS.get(account.get("platform"), set()):
                raise ValueError
            ids.add(account_id); names.add(name)
        unmapped = data.get("unmapped_accounts", [])
        if not isinstance(unmapped, list):
            raise ValueError
        for account in unmapped:
            name = account.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 200 or re.search(r"[\x00-\x1f]", name) or name in names:
                raise ValueError
            if account.get("collector") not in set().union(*COLLECTORS.values()):
                raise ValueError
            names.add(name)
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        raise ConfigError("Sub2API 监控配置无效") from None


def _epoch(value) -> float | None:
    if isinstance(value, bool):
        raise ValueError
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError
        number = date.timestamp()
    else:
        raise ValueError
    if not math.isfinite(number) or number < 0:
        raise ValueError
    return number


def _percent(value, *, allow_overage: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    if not math.isfinite(value) or value < 0 or (value > 100 and not allow_overage):
        raise ValueError
    return round(float(value), 1)


def _when(value: float | None) -> str:
    return dt.datetime.fromtimestamp(value).strftime("%m-%d %H:%M") if value is not None else "?"


def _error(account: dict, message: str) -> dict:
    return {"name": account["name"], "status": message, "source": "sub2api", "account_id": account["id"]}


def _windows(platform: str, data: dict) -> tuple[dict, object, bool]:
    windows = {}
    stamp = data.get("fetched_at")
    incomplete = False
    if platform == "openai":
        limits = data.get("rate_limit") or {}
        for key in ("primary_window", "secondary_window"):
            window = limits.get(key)
            if not isinstance(window, dict):
                continue
            kind = {18000: "short", 604800: "week"}.get(window.get("limit_window_seconds"))
            if kind:
                if kind in windows:
                    raise ValueError
                windows[kind] = (window.get("used_percent"), window.get("reset_at"))
    elif platform in ("kimi", "minimax"):
        if data.get("success") is not True or data.get("credential_valid") is not True:
            raise ValueError
        if data.get("provider", platform) != platform:
            raise ValueError
        for window in data.get("tiers") or []:
            kind = {"5h": "short", "weekly": "week"}.get(window.get("window"))
            if kind:
                if kind in windows:
                    raise ValueError
                windows[kind] = (window.get("used_percent"), window.get("reset_at"))
    elif platform == "antigravity":
        stamp = data.get("updated_at")
        models = data.get("antigravity_quota") or {}
        candidates = [(_percent(window.get("utilization")), window.get("reset_time"))
                      for name, window in models.items()
                      if name.startswith("gemini-") and "image" not in name.lower()]
        if candidates:
            windows["week"] = max(candidates, key=lambda item: item[0])
    return windows, stamp, incomplete


def _antigravity_summary_windows(summary: object) -> dict[str, dict[str, tuple[float, object]]]:
    if not isinstance(summary, dict):
        return {}
    out: dict[str, dict[str, tuple[float, object]]] = {}
    for group in summary.get("groups") or []:
        if not isinstance(group, dict):
            continue
        gname = str(group.get("displayName") or "").lower()
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            bid = str(bucket.get("bucketId") or "").lower()
            win = str(bucket.get("window") or "").lower()
            rem = bucket.get("remainingFraction")
            if isinstance(rem, bool) or not isinstance(rem, (int, float)) or not math.isfinite(rem) or not 0 <= float(rem) <= 1:
                continue
            used = _percent((1.0 - float(rem)) * 100.0)
            reset = bucket.get("resetTime")
            if "claude" in gname or bid.startswith("3p-"):
                label = "Claude"
            elif "argon" in gname or "argon" in bid:
                label = "Gemini 4 Argon"
            elif "gemini" in gname:
                label = "Gemini 4 Hydrogen"
            else:
                continue
            if win == "weekly" or "weekly" in bid:
                out.setdefault(label, {})["week"] = (used, reset)
            elif win == "5h" or "5h" in bid:
                out.setdefault(label, {})["short"] = (used, reset)
    return out


def _build_antigravity_sub_quota(
    label: str,
    candidates: list[tuple[float, object]],
    summary_group: dict[str, tuple[float, object]],
    *,
    dual_window: bool,
    now: float,
) -> dict | None:
    best = max(candidates, key=lambda item: item[0]) if candidates else None
    week = summary_group.get("week")
    short = summary_group.get("short")
    if not dual_window:
        chosen = week or best
        if not chosen:
            return None
        return {
            "label": label,
            "used_pct": chosen[0],
            "used_text": f"{chosen[0]:g}%",
            "reset": _when(_epoch(chosen[1])),
            "window_kind": "weekly",
        }

    if week and (short or best):
        short_win = short or best
        return {
            "label": label,
            "used_pct": week[0],
            "used_text": f"{week[0]:g}%",
            "reset": _when(_epoch(week[1])),
            "fiveh_pct": short_win[0],
            "fiveh_text": f"{short_win[0]:g}%",
            "fiveh_reset": _when(_epoch(short_win[1])),
            "fiveh_label": "5h 窗",
            "window_kind": "dual",
        }

    chosen = week or best or short
    if not chosen:
        return None
    reset_epoch = _epoch(chosen[1])
    is_short = (
        week is None
        and reset_epoch is not None
        and 0 < reset_epoch - now <= 6 * 3600
    )
    return {
        "label": label,
        "used_pct": chosen[0],
        "used_text": f"{chosen[0]:g}%",
        "reset": _when(reset_epoch),
        "window_kind": "5h" if is_short else "weekly",
    }


def normalize_account(account: dict, snapshot: dict, *, now: float | None = None) -> dict:
    """Only explicit provider windows can become percentages or history samples."""
    now = time.time() if now is None else now
    if snapshot.get("id") != account["id"] or snapshot.get("platform") != account["platform"]:
        return _error(account, "Sub2API 账号响应不匹配")
    status = snapshot.get("http_status")
    if status != 200:
        suffix = f"（HTTP {status}）" if type(status) is int and 100 <= status <= 599 else ""
        return _error(account, "Sub2API 查询失败" + suffix)
    data = snapshot.get("data")
    if not isinstance(data, dict) or data.get("error_code"):
        return _error(account, "Sub2API 上游额度不可用")
    try:
        windows, stamp, incomplete = _windows(account["platform"], data)
        if not windows:
            return _error(account, "上游未提供可识别额度")
        captured = _epoch(stamp)
        stale = incomplete or captured is None or not -60 <= now - captured <= 300
        parsed = {}
        expired_short = False
        for kind, (percent, reset) in windows.items():
            percent = _percent(percent, allow_overage=account["platform"] in {"kimi", "minimax"})
            reset = _epoch(reset)
            if kind == "short" and reset is not None and reset <= now and "week" in windows:
                expired_short = True
                continue
            parsed[kind] = (percent, reset)
            stale = stale or (reset is not None and reset <= now)
        row = {"name": account["name"], "status": "ok", "source": "sub2api",
               "account_id": account["id"], "captured_at": captured, "stale": stale,
               "used_pct": None, "used_text": "未提供", "reset": "?"}
        if "week" in parsed:
            percent, reset = parsed["week"]
            row.update(used_pct=percent, used_text=f"{percent:g}%", reset=_when(reset))
        if "short" in parsed:
            percent, reset = parsed["short"]
            row.update(fiveh_pct=percent, fiveh_text=f"{percent:g}%", fiveh_reset=_when(reset))
            if account["platform"] == "antigravity":
                row["fiveh_label"] = "Gemini 模型窗口"
        if account["platform"] == "antigravity":
            models = data.get("antigravity_quota") or {}
            summary_windows = _antigravity_summary_windows(data.get("antigravity_quota_summary"))
            argon_candidates = [(_percent(w.get("utilization")), w.get("reset_time"))
                                for n, w in models.items() if "gemini-4-argon" in n.lower()]
            hydrogen_candidates = [
                (_percent(w.get("utilization")), w.get("reset_time"))
                for n, w in models.items()
                if (
                    "gemini-4-hydrogen" in n.lower()
                    or "gemini-3.8-flash" in n.lower()
                    or ("gemini-3" in n.lower() and "flash" in n.lower() and "image" not in n.lower())
                )
            ]
            cloud_candidates = [(_percent(w.get("utilization")), w.get("reset_time"))
                                for n, w in models.items() if n.lower().startswith("claude")]

            sub_quotas = []
            for label, cands, dual in (
                ("Gemini 4 Argon", argon_candidates, False),
                ("Gemini 4 Hydrogen", hydrogen_candidates, True),
                ("Claude", cloud_candidates, True),
            ):
                sq = _build_antigravity_sub_quota(
                    label,
                    cands,
                    summary_windows.get(label, {}),
                    dual_window=dual,
                    now=now,
                )
                if sq is not None:
                    sub_quotas.append(sq)
            if sub_quotas:
                row["sub_quotas"] = sub_quotas
        notes = ["Sub2API", "数据 " + _when(captured)]
        plan = data.get("plan_type")
        if plan in {"free", "plus", "pro", "prolite", "team", "business", "enterprise"}:
            notes.insert(1, plan)
        if "week" not in parsed:
            notes.append("无周额度" if account["platform"] == "minimax" else "上游未提供周额度")
        if account["platform"] == "antigravity":
            if "sub_quotas" in row:
                parts = [f"{sq['label'].replace('Gemini ', '')} {sq['used_text']}" for sq in row["sub_quotas"]]
                notes.append(" · ".join(parts))
            else:
                notes.append("Gemini 文本模型最高使用率")
        if expired_short:
            notes.append("已隐藏过期短时窗口")
        if stale:
            notes.append("快照过期或窗口待刷新")
        row["note"] = " · ".join(notes)
        return row
    except (TypeError, ValueError, AttributeError, OverflowError, OSError):
        return _error(account, "Sub2API 额度数据无效")


def split_antigravity_rows(row: dict) -> list[dict]:
    """将 Antigravity 单账号拆分为 3 栏独立卡片：Gemini 4 Argon、Gemini 4 Hydrogen 与 Claude。"""
    if not row.get("sub_quotas"):
        return [row]
    sub_rows = []
    label_map = {
        "Gemini 4 Argon": "Gemini · 4 Argon",
        "Gemini 4 Hydrogen": "Gemini 4 Hydrogen",
        "Gemini 3.8 Flash": "Gemini 4 Hydrogen",
        "Claude": "Gemini · Claude",
        "Cloud (Claude)": "Gemini · Claude",
    }
    for sq in row["sub_quotas"]:
        label = sq["label"]
        name = label_map.get(label, f"Gemini · {label}")
        item = {
            "name": name,
            "status": row.get("status", "ok"),
            "source": row.get("source", "sub2api"),
            "account_id": row.get("account_id"),
            "captured_at": row.get("captured_at"),
            "stale": row.get("stale", False),
            "used_pct": sq.get("used_pct"),
            "used_text": sq.get("used_text", "—"),
            "reset": sq.get("reset", "?"),
            "note": f"Sub2API · 数据 {_when(row.get('captured_at'))} · Google Antigravity",
        }
        if sq.get("fiveh_pct") is not None or sq.get("fiveh_text"):
            item["fiveh_pct"] = sq.get("fiveh_pct")
            item["fiveh_text"] = sq.get("fiveh_text", "—")
            item["fiveh_reset"] = sq.get("fiveh_reset", "?")
            item["fiveh_label"] = sq.get("fiveh_label", "5h 窗")
        if sq.get("window_kind"):
            item["window_kind"] = sq["window_kind"]
        sub_rows.append(item)
    return sub_rows


def fetch_snapshot(config: dict, accounts: list[dict]) -> dict:
    request = {"accounts": [{"id": a["id"], "platform": a["platform"]} for a in accounts]}
    encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
    result = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         config["ssh_host"], "python3 - " + shlex.quote(encoded)],
        input=REMOTE_SCRIPT.read_text(), text=True, capture_output=True,
        timeout=180, check=True,
    )
    if len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("response_too_large")
    body = json.loads(result.stdout)
    if not isinstance(body, dict) or body.get("version") != 1 or not isinstance(body.get("accounts"), list):
        raise ValueError("invalid_response")
    return body


def collect(config: dict, *, retry_attempts: int = 0, retry_delay: float = 0,
            now: float | None = None) -> list[dict]:
    """Retry failed or stale reads without re-querying fresh successful accounts."""
    pending = config["accounts"]
    results = {}
    for attempt in range(max(0, retry_attempts) + 1):
        if attempt and retry_delay > 0:
            time.sleep(retry_delay)
        try:
            body = fetch_snapshot(config, pending)
            snapshots = body.get("accounts", [])
            records = {}
            for snapshot in snapshots:
                if not isinstance(snapshot, dict) or type(snapshot.get("id")) is not int:
                    raise ValueError("invalid_response")
                if snapshot["id"] in records:
                    raise ValueError("duplicate_account")
                records[snapshot["id"]] = snapshot
            for account in pending:
                result = records.get(account["id"], {})
                results[account["id"]] = normalize_account(account, result, now=now)
        except Exception:
            # SSH stderr, exception strings and raw responses may contain credentials.
            for account in pending:
                results[account["id"]] = _error(account, "Sub2API 读取失败（连接或响应异常）")
        pending = [a for a in pending if results[a["id"]]["status"] != "ok" or results[a["id"]].get("stale")]
        if not pending:
            break
    rows = []
    for account in config["accounts"]:
        res = results[account["id"]]
        if account.get("platform") == "antigravity" and res.get("status") == "ok" and res.get("sub_quotas"):
            rows.extend(split_antigravity_rows(res))
        else:
            rows.append(res)
    for account in config.get("unmapped_accounts", []):
        rows.append({"name": account["name"], "status": "未接入 Sub2API，未执行本地刷新",
                     "source": "sub2api", "retryable": False})
    return rows
