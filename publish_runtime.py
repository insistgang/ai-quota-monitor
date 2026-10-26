#!/usr/bin/env python3
"""Canonical quota publishing workflow shared by launchd and publish.sh."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parent
PUBLIC_PAGES = (
    "docs/index.html",
    "docs/history.html",
    "docs/subscriptions.html",
)
PUSH_REMOTE = "https://github.com/insistgang/ai-quota-monitor.git"
COLLECT_RETRY_ATTEMPTS = 2
COLLECT_RETRY_DELAY_SECONDS = 15
MAX_UNHEALTHY_SOURCES = 1
PUSH_RETRY_ATTEMPTS = 3
PUSH_RETRY_DELAY_SECONDS = 2
PUSH_TIMEOUT_SECONDS = 60


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
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=repo,
        env=env,
        check=check,
        text=True,
        timeout=timeout,
    )


def _push_with_retries(repo: Path, env: dict[str, str]) -> None:
    command = ("git", "push", PUSH_REMOTE, "main")
    push_env = env.copy()
    push_env["GIT_TERMINAL_PROMPT"] = "0"
    for attempt in range(PUSH_RETRY_ATTEMPTS):
        try:
            _run(
                command,
                repo=repo,
                env=push_env,
                timeout=PUSH_TIMEOUT_SECONDS,
            )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == PUSH_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(PUSH_RETRY_DELAY_SECONDS)


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
            "--retry-failures",
            str(COLLECT_RETRY_ATTEMPTS),
            "--retry-delay",
            str(COLLECT_RETRY_DELAY_SECONDS),
            "--max-unhealthy",
            str(MAX_UNHEALTHY_SOURCES),
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
    committed = False
    if changed.returncode == 1:
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
        committed = True
    elif changed.returncode != 0:
        raise subprocess.CalledProcessError(changed.returncode, changed.args)

    if push:
        _push_with_retries(repo, env)
    return committed


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
