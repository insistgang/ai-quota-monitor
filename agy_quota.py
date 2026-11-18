"""Read shared quota windows from the signed-in Antigravity CLI."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import time
from pathlib import Path


SOURCE = "antigravity_cli"
GEMINI_LABEL = "Gemini · 共享额度"
ARGON_LABEL = "Gemini · 4 Argon"
CLAUDE_LABEL = "Claude / GPT-OSS · 共享额度"
UNKNOWN_LABEL = "Antigravity · CLI"
_MODEL_NAME = re.compile(r"(?:Gemini |Claude |GPT-OSS |Orchestrator )[A-Za-z0-9.() -]{1,55}")


def _display_label(group_name: str) -> str | None:
    """Map a CLI model group onto a stable dashboard label; unknown groups stay hidden."""
    low = group_name.casefold()
    if "argon" in low:
        return ARGON_LABEL
    if "gemini" in low:
        return GEMINI_LABEL
    if "claude" in low or "gpt" in low:
        return CLAUDE_LABEL
    return None


def _usage_groups(payload: object) -> list:
    # An unknown slash command may be treated as an agent prompt. Never use or
    # retry a normal model response; require the CLI's zero-turn usage envelope.
    if not isinstance(payload, dict) or payload.get("status") != "SUCCESS":
        raise ValueError("invalid quota envelope")
    command = payload.get("command")
    usage = payload.get("usage")
    if (type(payload.get("num_turns")) is not int or payload["num_turns"] != 0
            or not isinstance(usage, dict) or type(usage.get("total_tokens")) is not int
            or usage["total_tokens"] != 0
            or not isinstance(command, dict) or command.get("name") != "usage"):
        raise ValueError("not a CLI usage response")
    data = command.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise ValueError("missing quota groups")
    return data["groups"]


def _group_row(group: dict, name: str, captured: dt.datetime) -> dict | None:
    buckets = group.get("buckets")
    if not isinstance(buckets, list):
        return None
    windows = {}
    seen = set()
    for bucket in buckets:
        if not isinstance(bucket, dict) or bucket.get("window") not in ("weekly", "5h"):
            continue
        window = bucket["window"]
        if window in seen:
            return None  # Ambiguous bucket: do not silently choose one.
        seen.add(window)
        remaining = bucket.get("remaining_fraction")
        reset_time = bucket.get("reset_time")
        if type(remaining) not in (int, float) or not 0 <= remaining <= 1 or not isinstance(reset_time, str):
            continue
        try:
            reset = dt.datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
            if reset.tzinfo is not None and reset > captured:
                windows[window] = (round((1 - remaining) * 100, 1), reset.astimezone().strftime("%m-%d %H:%M"))
        except (ValueError, OverflowError, OSError):
            continue
    if not windows:
        return None

    row = {"name": name, "status": "ok", "source": SOURCE, "captured_at": captured.timestamp(),
           "used_pct": None, "used_text": "未提供", "reset": "?"}
    note = [f"Antigravity CLI · 数据 {captured.astimezone():%m-%d %H:%M}"]
    if "weekly" in windows:
        used, reset = windows["weekly"]
        row.update(used_pct=used, used_text=f"{used:g}%", reset=reset)
    else:
        note.append("周窗口暂无有效数据")
    if "5h" in windows:
        used, reset = windows["5h"]
        row.update(fiveh_pct=used, fiveh_text=f"{used:g}%", fiveh_reset=reset)
    else:
        note.append("5h 窗口暂无有效数据")

    description = group.get("description")
    prefix = "Models within this group: "
    if isinstance(description, str) and len(description) <= 1024 and description.startswith(prefix):
        models = [model.strip() for model in description[len(prefix):].split(",")]
        if models and all(_MODEL_NAME.fullmatch(model) for model in models):
            note.append("共享模型：" + "、".join(models))
    row["note"] = " · ".join(note)
    return row


def collect(*, retry_attempts: int = 0, retry_delay: float = 0) -> list[dict]:
    """Collect the groups the CLI actually reports; absence of agy keeps Sub2API-only setups intact."""
    home = Path.home()
    agy = shutil.which("agy") or shutil.which(str(home / ".local/bin/agy"))
    if not agy:
        return []
    rows: dict[str, dict] = {}
    for attempt in range(max(0, retry_attempts) + 1):
        if attempt and retry_delay > 0:
            time.sleep(retry_delay)
        try:
            result = subprocess.run(
                [agy, "--print", "/usage", "--output-format", "json"],
                cwd=home, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, encoding="utf-8", timeout=30,
            )
        except subprocess.TimeoutExpired:
            continue
        except (OSError, UnicodeError):
            break
        if result.returncode != 0:
            continue
        try:
            groups = _usage_groups(json.loads(result.stdout))
        except ValueError:
            continue  # Unsupported or unexpected output; never fall back to a prompt.
        captured = dt.datetime.now(dt.timezone.utc)
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("name"), str):
                continue
            label = _display_label(group["name"])
            if label is None or label in rows:
                continue
            row = _group_row(group, label, captured)
            if row is not None:
                rows[label] = row
        if rows:
            break
    if not rows:
        return [{"name": UNKNOWN_LABEL, "source": SOURCE,
                 "status": "Antigravity CLI 未返回有效额度"}]
    return list(rows.values())
