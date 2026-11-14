<p align="center">
  <a href="./README.md">简体中文</a> · English
</p>

<!-- README_SYNC: source=main@85ac18c; updated=2026-11-15 -->

<div align="center">

# 🤖 AI Quota Monitor

### Every AI account's quota window, on one board that refreshes every hour

[![GitHub Stars](https://img.shields.io/github/stars/insistgang/ai-quota-monitor?style=for-the-badge&logo=github&label=Stars&color=f5b301)](https://github.com/insistgang/ai-quota-monitor/stargazers)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#requirements)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Issues](https://img.shields.io/github/issues/insistgang/ai-quota-monitor?style=for-the-badge)](https://github.com/insistgang/ai-quota-monitor/issues)
[![Last Commit](https://img.shields.io/github/last-commit/insistgang/ai-quota-monitor?style=for-the-badge)](https://github.com/insistgang/ai-quota-monitor/commits/main)

**If it saved you from opening three different clients, a Star helps more people stuck with the same quota anxiety find it.**

<img src="./docs/assets/dashboard-overview.jpg" alt="Overview page: source counters, refresh notice and per-account quota cards" width="100%" />

</div>

## What it is

One command that collects the **weekly quota, 5-hour rolling window and reset time** of several AI coding accounts, and turns them into a terminal table, a local card-style dashboard and three public static pages.

Nothing is scraped from your laptop: quota data is read through an existing SSH connection, read-only, inside the Sub2API server, and Antigravity's weekly window is completed with `agy /usage`. Every query is read-only — no model requests, no quota resets.

**📊 Live snapshot (updated hourly, 9:30–23:30 daily)**: https://insistgang.top/ai-quota-monitor/

## What you do without it

| Option | What you have to do | What you never get |
| --- | --- | --- |
| Check each client | Switch windows per account | One cross-account view, reset countdowns, continuous history |
| Vendor usage pages | Log into each site | Real windows of relayed accounts, cross-week totals |
| Sub2API dashboard | Read the relay's raw log | Normalized windows, a daily-spend ledger, a public board |
| **AI Quota Monitor** | Run one command, or let it run hourly | — |

## The live board

Three tabs answer three different questions: which account needs attention now, how much this month cost, and which subscriptions expire soon.

### Quota overview

![Quota overview: source counters, refresh notice and per-account quota cards](docs/assets/dashboard-overview.jpg)

Cards are ordered by remaining weekly quota, so the account that needs action comes first. The main percentage is the weekly usage the vendor actually reported; the 5-hour window is shown separately and never mixed into the weekly number. An unknown weekly window shows `—` instead of a fake 0%.

### Daily spend

![Daily spend: last 7 days of account usage, range switch and account filter](docs/assets/daily-history.jpg)

A normal day is the last snapshot of today minus the last snapshot of yesterday. On a reset day the two halves are added separately, and a day with missing snapshots never absorbs several days of usage.

### Subscriptions and billing

![Subscriptions: monthly average cost, expiry reminders and active subscriptions](docs/assets/subscriptions.jpg)

Non-AI renewals are tracked next to AI subscriptions (for example `ikuuu`). ChatGPT prepaid balance is shown as of the date it was checked manually, and a future deduction stays a plan — it is never booked as spend that already happened.

> The time at the top of a page is the page generation time; the "data" time on a card is when that source was actually sampled. Pages check for a new version every minute and update cards in place — **opening a page never triggers collection on your machine**.

## Quick start

```bash
git clone https://github.com/insistgang/ai-quota-monitor.git
cd ai-quota-monitor
python3 quota_report.py --html     # build the local card dashboard
open quota-dashboard.html           # open it in a browser
```

To collect real data you also need the private Sub2API mapping at `~/.cache/ai-quota-monitor/sub2api.json` (mode `0600`). When it is missing, the program fails loudly and refuses to write instead of publishing an empty snapshot. See [Sub2API quota access](docs/sub2api-quota.md) (Chinese) for the setup.

Common commands:

```bash
python3 quota_report.py            # terminal table
python3 quota_report.py --json     # machine-readable output
python3 quota_report.py --log      # append a snapshot to quota-log.csv (daily ledger)
python3 quota_report.py --html     # render quota-dashboard.html
python3 quota_report.py --serve    # local server mode: http://127.0.0.1:8788 with a refresh button
./publish.sh                       # collect + render public pages + push to GitHub Pages
```

## Which accounts are covered

| Source | How it is read |
|---|---|
| Kimi for Coding (two accounts) | Sub2API CN quota endpoints, reading the 5h and weekly windows |
| Codex (OpenAI accounts) | Official usage endpoint using the current Sub2API credentials inside the server; 5h and 7-day windows are separated by their real duration |
| MiniMax | Sub2API CN quota endpoints; only the windows the upstream actually returned for this call |
| Antigravity (Gemini group) | Per-model Sub2API quota, completed with `agy /usage` for the Gemini / Claude weekly windows |
| Non-AI services (such as `ikuuu`) | Expiry dates read from the private finance notes, surfaced on the subscriptions page and in urgent reminders |

The current live snapshot covers 9 accounts: Kimi ×2, Codex ×3, MiniMax ×1, Antigravity ×3. The Grok collector, the Doubao DOM collector and the old local CLI collectors have been removed; their history stays in the local ledger and is filtered out at display time.

## Scheduling on macOS (launchd)

```bash
./scripts/install-launchd-runtime.sh
cp examples/com.leo.quota-report.plist ~/Library/LaunchAgents/
# replace /Users/YOURNAME in the plist with your own path
launchctl load ~/Library/LaunchAgents/com.leo.quota-report.plist
```

By default it runs **hourly from 9:30 to 23:30** (15 runs a day). launchd calls `~/.local/bin/quota-publish` (deliberately not in a Documents folder, to avoid macOS TCC interception), which opens the single `publish_runtime.py` inside the repository and runs collection, CSV writing, the three HTML pages and the push. Running `./publish.sh` by hand enters the same Python entry point, so there is exactly one publishing path. Missed runs while the Mac sleeps are caught up after wake.

launchd writes stdout/stderr to `~/Library/Logs/ai-quota-monitor/`, which the install command creates for you. Keep the entry point and the logs out of `Documents`, otherwise macOS TCC/System Policy can block the script before it even starts.

## Privacy and security

- Only read-only quota endpoints are queried inside the Sub2API server; plans, reset policies and model traffic are never touched
- Keys, tokens and admin password digests are never printed or cached; short-lived server-side credentials exist only in the collector process memory, and SSH brings back nothing but the allowed quota fields
- The local dashboard, the live server and the public pages mask emails by default (for example `f***@163.com`); public pages additionally apply `public_replacements` from `~/.cache/ai-quota-monitor/finance.json` to strip real names, bank names and private account nicknames, while the local ledger keeps the original identifiers for continuous statistics
- `quota-log.csv` and `quota-dashboard.html` contain your usage data and are excluded by `.gitignore`, so they cannot leak into the repository
- The hidden-account list lives in `~/.cache/ai-quota-monitor/public-hidden-accounts.json` (mode `0600`); if it is missing or invalid, public page generation stops

## Semantics and limits

- The main percentage is "used this week"; the remainder is `100 - used_pct`. Over-quota values reported by CN vendors are kept as they are, and the progress bar is capped at 100%
- The 5-hour window never counts towards "daily spend", otherwise one usage burst would be counted twice
- Once the weekly quota is exhausted (3% or less remaining), the card switches to "used up" instead of nudging you to spend the 5-hour window
- When collection fails, the last valid snapshot is kept and labelled with the failure reason — no pretending to be live, and no writing it as 0%
- GitHub pushes are retried up to 3 times with a 60-second timeout each; on failure the local commit is kept and the next scheduled run pushes it

The full data flow, calculation rules, scheduling, failure handling and privacy design are described in [project design notes](docs/project-design.md) (Chinese).

## Repository layout

```text
ai-quota-monitor/
├── quota_report.py              # Sub2API normalization, Antigravity weekly window, daily math, HTML rendering
├── sub2api_quota.py             # private mapping, SSH reads, window validation, per-account retry
├── scripts/
│   ├── sub2api_quota_snapshot.py    # restricted usage query inside the server, returns no credentials
│   ├── quota-publish-launchd.sh     # stable launchd entry point outside Documents
│   └── install-launchd-runtime.sh
├── publish_runtime.py           # the single render, commit and push flow
├── publish.sh                   # manual publishing entry point
├── docs/
│   ├── index.html               # quota overview
│   ├── history.html             # daily spend
│   ├── subscriptions.html       # subscriptions and billing
│   ├── project-design.md        # data flow and calculation rules
│   ├── sub2api-quota.md         # Sub2API access and permission boundary
│   └── assets/                  # real screenshots used by this README
├── examples/
│   └── com.leo.quota-report.plist
└── tests/                       # 117 regression tests for quota math, privacy, HTML escaping and publishing
```

## Requirements

- macOS + Python 3.10+ (standard library only, nothing to pip install)
- Sub2API: an existing non-interactive SSH login, with Python 3, Docker and read access to the Sub2API database on the server
- Antigravity weekly window: `tmux` plus a logged-in `agy`
- Scheduled publishing: macOS launchd

## Development and tests

```bash
python3 -B -m unittest discover -s tests -q
python3 -m py_compile quota_report.py sub2api_quota.py publish_runtime.py scripts/sub2api_quota_snapshot.py
bash -n publish.sh scripts/*.sh
```

117 tests cover quota calculations, daily-spend segmentation, masking and public replacements, HTML escaping, failure degradation and the publishing flow. The full acceptance checklist lives in [project design notes](docs/project-design.md) (Chinese).

## License

[MIT](./LICENSE). The repository also keeps the author's local usage-data pipeline; everything published is masked quota, billing and totals only.

## Support

If this saved you from juggling several clients, or made "how much quota is left" a fact instead of a memory, a Star is the most direct feedback.

Found a wrong quota rule, a source that stops reporting, or want to add a new one? Open an [issue](https://github.com/insistgang/ai-quota-monitor/issues) with the relevant log excerpt.
