#!/usr/bin/env python3
"""Canonical quota publishing workflow shared by launchd and publish.sh."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parent
PUBLIC_PAGES = (
    "docs/index.html",
    "docs/history.html",
    "docs/subscriptions.html",
)
PUSH_REMOTE = "https://github.com/insistgang/ai-quota-monitor.git"


def _runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    user_home = Path(env.get("HOME") or Path.home())
    preferred_bins = [
        user_home / ".grok" / "bin",
        user_home / ".local" / "bin",
    ]
    node_root = user_home / ".nvm" / "versions" / "node"
    node_bins = sorted(node_root.glob("*/bin")) if node_root.is_dir() else []
    if node_bins:
        preferred_bins.append(node_bins[-1])
    preferred_bins.extend((
        Path("/opt/homebrew/bin"),
        Path("/usr/bin"),
        Path("/bin"),
    ))
    existing_path = env.get("PATH", "")
    env["PATH"] = ":".join(
        [*(str(path) for path in preferred_bins), existing_path]
    ).rstrip(":")
    env["TERM"] = "xterm-256color"
    return env


def _run(
    command: Sequence[str],
    *,
    repo: Path,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=repo,
        env=env,
        check=check,
        text=True,
    )


def publish(
    repo: Path = REPO_ROOT,
    *,
    push: bool = True,
    now: dt.datetime | None = None,
) -> bool:
    """Generate the public pages and commit them; return whether a commit was made."""
    repo = repo.resolve()
    report = repo / "quota_report.py"
    if not report.is_file():
        raise FileNotFoundError(f"quota report missing: {report}")

    env = _runtime_env()
    _run(
        (
            sys.executable,
            str(report),
            "--log",
            "--html",
            "--public-html",
            PUBLIC_PAGES[0],
        ),
        repo=repo,
        env=env,
    )
    _run(("git", "add", "--", *PUBLIC_PAGES), repo=repo, env=env)
    changed = _run(
        ("git", "diff", "--cached", "--quiet", "--", *PUBLIC_PAGES),
        repo=repo,
        env=env,
        check=False,
    )
    if changed.returncode == 0:
        return False
    if changed.returncode != 1:
        raise subprocess.CalledProcessError(changed.returncode, changed.args)

    timestamp = (now or dt.datetime.now()).strftime("%Y-%m-%d_%H%M")
    _run(
        (
            "git",
            "commit",
            "-q",
            "-m",
            f"chore: 每日额度快照 {timestamp}",
            "--only",
            "--",
            *PUBLIC_PAGES,
        ),
        repo=repo,
        env=env,
    )
    if push:
        push_env = env.copy()
        push_env["GIT_TERMINAL_PROMPT"] = "0"
        _run(
            ("git", "push", PUSH_REMOTE, "main"),
            repo=repo,
            env=push_env,
        )
    return True


def runtime_smoke(repo: Path = REPO_ROOT) -> None:
    """Verify launchd can read the runtime and repository without changing state."""
    repo = repo.resolve()
    required = (repo / "quota_report.py", repo / "docs")
    if not required[0].is_file() or not required[1].is_dir():
        raise RuntimeError(f"quota monitor repository incomplete: {repo}")
    subprocess.run(
        ("git", "rev-parse", "--is-inside-work-tree"),
        cwd=repo,
        env=_runtime_env(),
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print("INSTALL_RUNTIME_PASS")


def main() -> int:
    if os.environ.get("QUOTA_PUBLISH_SMOKE_TEST") == "1":
        runtime_smoke()
        return 0
    publish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
