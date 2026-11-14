import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import quota_report


CSV_HEADER = ["ts", "name", "status", "used_pct", "used_text", "reset", "fiveh_text"]


def setUpModule():
    global _public_filter_test_dir, _public_filter_patch
    _public_filter_test_dir = tempfile.TemporaryDirectory()
    config = Path(_public_filter_test_dir.name) / "public-hidden-accounts.json"
    config.write_text(json.dumps({"codex_emails": ["test@example.invalid"]}), encoding="utf-8")
    _public_filter_patch = mock.patch.object(quota_report, "PUBLIC_HIDDEN_ACCOUNTS_PATH", config)
    _public_filter_patch.start()


def tearDownModule():
    _public_filter_patch.stop()
    _public_filter_test_dir.cleanup()


class AntigravityQuotaTests(unittest.TestCase):
    def test_claude_gpt_group_is_hidden_from_history(self):
        rows = [
            {"name": "Antigravity · Claude/GPT 组", "status": "ok", "used_pct": 6.0},
            {"name": "Antigravity · Gemini 组", "status": "ok", "used_pct": 68.0},
        ]

        names = [r["name"] for r in quota_report._visible_quota_rows(rows)]

        self.assertEqual(names, ["Antigravity · Gemini 组"])

    def test_enrich_antigravity_rows_promotes_flash_and_claude_to_dual_windows(self):
        panel = """
GEMINI MODELS
  Models within this group: Gemini Flash, Gemini Pro

  Weekly Limit Remaining
    86.11%
    Refreshes in 110h 47m

  Five Hour Limit Remaining
    77.00%
    Refreshes in 3h 4m
"""
        sub_rows = [
            {
                "name": "Gemini · 3.8 Flash",
                "status": "ok",
                "used_pct": 23.0,
                "used_text": "23%",
                "reset": "11-10 00:42",
                "window_kind": "5h",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "agy-groups.json"
            with (
                mock.patch.object(quota_report, "AGY_GROUPS_CACHE", cache_path),
                mock.patch.object(quota_report, "_tmux_slash_probe", return_value=panel),
            ):
                enriched = quota_report._enrich_antigravity_rows(sub_rows)

        self.assertEqual(enriched[0]["used_pct"], 13.9)
        self.assertEqual(enriched[0]["used_text"], "13.9%")
        self.assertEqual(enriched[0]["fiveh_pct"], 23.0)
        self.assertEqual(enriched[0]["fiveh_text"], "23%")
        self.assertEqual(enriched[0]["fiveh_reset"], "11-10 00:42")
        self.assertEqual(enriched[0]["window_kind"], "dual")


class CollectionRetryTests(unittest.TestCase):
    def test_publish_health_gate_stops_before_writing_multiple_failures(self):
        rows = [
            {"name": "网络来源", "status": "查询失败"},
            {"name": "缓存来源", "status": "ok", "stale": True},
        ]
        argv = [
            "quota_report.py",
            "--log",
            "--html",
            "--public-html",
            "docs/index.html",
            "--max-unhealthy",
            "1",
        ]

        with (
            mock.patch.object(quota_report.sys, "argv", argv),
            mock.patch.object(quota_report, "collect", return_value=rows),
            mock.patch.object(quota_report, "append_log") as append_log,
            mock.patch.object(quota_report, "render_html") as render_html,
            mock.patch.object(quota_report, "render_public_site") as render_public_site,
        ):
            result = quota_report.main()

        self.assertEqual(result, 2)
        append_log.assert_not_called()
        render_html.assert_not_called()
        render_public_site.assert_not_called()


class DailyDeltaTests(unittest.TestCase):
    def _daily_rows(self, snapshots, day="2026-08-20"):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "quota-log.csv"
            with log_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_HEADER)
                writer.writerows(snapshots)
            with mock.patch.object(quota_report, "LOG_CSV", log_path):
                days = quota_report._daily_deltas()
        return next(rows for date, rows in days if date == day)

    def test_stale_and_failed_samples_do_not_change_daily_consumption(self):
        rows = self._daily_rows([
            ["2026-08-19T23:31:00", "MiniMax", "ok", "10", "10%", "08-25 00:00", ""],
            ["2026-08-20T10:31:00", "MiniMax", "ok", "15", "15%", "08-25 00:00", ""],
            ["2026-08-20T20:31:00", "MiniMax", "stale", "99", "99%", "08-25 00:00", ""],
            ["2026-08-20T21:31:00", "MiniMax", "error", "99", "99%", "08-25 00:00", ""],
        ])
        self.assertEqual(rows, [
            {"name": "MiniMax", "delta": 5.0, "reset": False, "partial": False},
        ])

    def test_reset_day_counts_usage_before_and_after_windows_roll_over(self):
        rows = self._daily_rows([
            ["2026-08-19T23:31:00", "Codex · Win", "ok", "75", "75%", "08-20 13:38", ""],
            ["2026-08-20T09:31:00", "Codex · Win", "ok", "75", "75%", "08-20 13:38", ""],
            ["2026-08-20T13:31:00", "Codex · Win", "ok", "100", "100%", "08-20 13:38", ""],
            ["2026-08-20T14:31:00", "Codex · Win", "ok", "0", "0%", "08-27 13:51", ""],
            ["2026-08-20T20:31:00", "Codex · Win", "ok", "26", "26%", "08-27 13:51", ""],
        ])

        self.assertEqual(rows, [{
            "name": "Codex · Win",
            "delta": 51.0,
            "reset": True,
            "partial": False,
        }])

    def test_reset_day_counts_post_reset_usage_when_old_window_was_full(self):
        rows = self._daily_rows([
            ["2026-08-19T23:31:00", "Codex · Mac", "ok", "100", "100%", "08-20 11:36", ""],
            ["2026-08-20T11:31:00", "Codex · Mac", "ok", "100", "100%", "08-20 11:36", ""],
            ["2026-08-20T12:31:00", "Codex · Mac", "ok", "1", "1%", "08-27 12:22", ""],
            ["2026-08-20T20:31:00", "Codex · Mac", "ok", "7", "7%", "08-27 12:22", ""],
        ])

        self.assertEqual(rows[0]["delta"], 7.0)
        self.assertTrue(rows[0]["reset"])

    def test_same_window_source_jitter_is_not_treated_as_a_reset(self):
        rows = self._daily_rows([
            ["2026-08-20T13:31:00", "Codex · Mac", "ok", "100", "100%", "08-21 11:36", ""],
            ["2026-08-20T14:31:00", "Codex · Mac", "ok", "89", "89%", "08-21 11:36", ""],
            ["2026-08-20T15:31:00", "Codex · Mac", "ok", "100", "100%", "08-21 11:36", ""],
        ])

        self.assertEqual(rows[0]["delta"], 0.0)
        self.assertFalse(rows[0]["reset"])
        self.assertTrue(rows[0]["partial"])

    def test_missing_calendar_days_do_not_get_charged_to_the_next_recorded_day(self):
        rows = self._daily_rows([
            ["2026-08-17T23:31:00", "Kimi · 本人", "ok", "10", "10%", "08-23 01:18", ""],
            ["2026-08-20T09:31:00", "Kimi · 本人", "ok", "30", "30%", "08-23 01:18", ""],
            ["2026-08-20T20:31:00", "Kimi · 本人", "ok", "35", "35%", "08-23 01:18", ""],
        ])

        self.assertEqual(rows[0]["delta"], 5.0)
        self.assertTrue(rows[0]["partial"])

    def test_grok_sources_are_hidden_from_daily_history(self):
        rows = self._daily_rows([
            ["2026-08-19T23:31:00", "Grok · SuperGrok", "ok", "0", "0%", "08-31 00:00", ""],
            ["2026-08-20T20:31:00", "Grok · SuperGrok", "ok", "8", "8%", "08-31 00:00", ""],
            ["2026-08-19T23:31:00", "Codex · Mac", "ok", "4", "4%", "09-01 22:18", ""],
            ["2026-08-20T20:31:00", "Codex · Mac", "ok", "6", "6%", "09-01 22:18", ""],
        ])
        names = [row["name"] for row in rows]
        self.assertIn("Codex · Mac", names)
        self.assertTrue(all("grok" not in name.lower() for name in names))

    def test_doubao_is_hidden_from_history_without_changing_other_usage(self):
        rows = self._daily_rows([
            ["2026-08-19T23:31:00", "豆包 · 标准套餐", "ok", "10", "10%", "08-24 00:00", ""],
            ["2026-08-20T20:31:00", "豆包 · 标准套餐", "ok", "18", "18%", "08-24 00:00", ""],
            ["2026-08-19T23:31:00", "Codex · Mac", "ok", "4", "4%", "09-01 22:18", ""],
            ["2026-08-20T20:31:00", "Codex · Mac", "ok", "6", "6%", "09-01 22:18", ""],
        ])
        self.assertEqual(rows, [
            {"name": "Codex · Mac", "delta": 2.0, "reset": False, "partial": False},
        ])

    def test_history_html_shows_consumption_on_a_reset_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "quota-log.csv"
            with log_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_HEADER)
                writer.writerows([
                    ["2026-08-19T23:31:00", "Codex · Win", "ok", "75", "75%", "08-20 13:38", ""],
                    ["2026-08-20T13:31:00", "Codex · Win", "ok", "100", "100%", "08-20 13:38", ""],
                    ["2026-08-20T14:31:00", "Codex · Win", "ok", "0", "0%", "08-27 13:51", ""],
                    ["2026-08-20T20:31:00", "Codex · Win", "ok", "26", "26%", "08-27 13:51", ""],
                ])
            with mock.patch.object(quota_report, "LOG_CSV", log_path):
                html = quota_report._history_html(public=False)

        self.assertIn("+51%", html)
        self.assertIn("↺", html)


class AlertTests(unittest.TestCase):
    def test_kimi_ignores_five_hour_window_when_weekly_reset_is_far_away(self):
        now = dt.datetime.now()
        rows = [{
            "name": "Kimi · 本人（948/年）",
            "status": "ok",
            "used_pct": 80.0,
            "reset": (now + dt.timedelta(days=4)).strftime("%m-%d %H:%M"),
            "fiveh_pct": 0.0,
            "fiveh_reset": (now + dt.timedelta(hours=1)).strftime("%m-%d %H:%M"),
        }]

        self.assertEqual(quota_report._alerts(rows), [])

    def test_kimi_still_alerts_when_weekly_reset_is_within_one_day(self):
        now = dt.datetime.now()
        rows = [{
            "name": "Kimi · 本人（948/年）",
            "status": "ok",
            "used_pct": 80.0,
            "reset": (now + dt.timedelta(hours=24)).strftime("%m-%d %H:%M"),
            "fiveh_pct": 0.0,
            "fiveh_reset": (now + dt.timedelta(hours=1)).strftime("%m-%d %H:%M"),
        }]

        alerts = quota_report._alerts(rows)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "week")

    def test_non_kimi_sources_keep_five_hour_alerts(self):
        now = dt.datetime.now()
        rows = [{
            "name": "Antigravity · Gemini 组",
            "status": "ok",
            "used_pct": 30.0,
            "reset": (now + dt.timedelta(days=4)).strftime("%m-%d %H:%M"),
            "fiveh_pct": 20.0,
            "fiveh_reset": (now + dt.timedelta(hours=1)).strftime("%m-%d %H:%M"),
        }]

        alerts = quota_report._alerts(rows)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "fiveh")


class HtmlEscapingTests(unittest.TestCase):
    def test_quota_card_escapes_all_dynamic_text(self):
        rendered = quota_report._card({
            "name": '<img src=x onerror="alert(1)">',
            "status": "ok",
            "used_pct": 10.0,
            "used_text": "<script>used</script>",
            "reset": "<b>tomorrow</b>",
            "fiveh_pct": 5.0,
            "fiveh_text": "<i>five</i>",
            "fiveh_reset": "<u>soon</u>",
            "note": "<svg onload=alert(1)>",
        })

        for raw_tag in ("<img", "<script>", "<b>", "<i>", "<u>", "<svg"):
            self.assertNotIn(raw_tag, rendered)
        self.assertIn("&lt;script&gt;used&lt;/script&gt;", rendered)

    def test_card_with_only_short_window_promotes_to_main_display(self):
        rendered = quota_report._card({
            "name": "MiniMax",
            "status": "ok",
            "used_pct": None,
            "used_text": "未提供",
            "reset": "?",
            "fiveh_pct": 16.0,
            "fiveh_text": "16%",
            "fiveh_reset": "10-27 00:00",
            "fiveh_label": "5h 窗",
            "note": "Sub2API · 数据 10-26 20:09 · 无周额度",
        })
        self.assertNotIn("本周已用 未提供", rendered)
        self.assertNotIn(">—<", rendered)
        self.assertIn(">16%</div>", rendered)
        self.assertIn("剩 84%", rendered)
        self.assertIn("5h 窗已用 16% · 重置 10-27 00:00", rendered)

    def test_error_card_escapes_name_and_status(self):
        rendered = quota_report._card({
            "name": "<em>source</em>",
            "status": "<script>failed</script>",
        })

        self.assertNotIn("<em>", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;failed&lt;/script&gt;", rendered)

    def test_alert_banner_escapes_dynamic_text(self):
        rendered = quota_report._alerts_html([{
            "name": "<img src=x>",
            "kind": "week",
            "until": "<b>soon</b>",
            "remain": 20.0,
        }])

        self.assertNotIn("<img", rendered)
        self.assertNotIn("<b>soon</b>", rendered)
        self.assertIn("&lt;b&gt;soon&lt;/b&gt;", rendered)

    def test_finance_sections_escape_dynamic_text_but_remain_public(self):
        finance = {
            "subs": [{"name": "<img src=x>", "cost": "<b>99 元</b>", "renewal": "<i>长期</i>"}],
            "ledger": [{"date": "<u>today</u>", "item": "<script>item</script>", "amount": "<b>1 元</b>"}],
            "non_ai": ["<svg onload=alert(1)>"],
            "total_ai": "<strong>99 元/月</strong>",
            "total_all": "<strong>100 元/月</strong>",
        }

        with mock.patch.object(quota_report, "_load_finance", return_value=finance):
            rendered = quota_report._subs_html(public=True)

        for raw_tag in ("<img", "<script>", "<svg", "<strong>", "<u>", "<i>"):
            self.assertNotIn(raw_tag, rendered)
        self.assertIn("&lt;script&gt;item&lt;/script&gt;", rendered)
        self.assertIn("消费台账", rendered)

    def test_history_escapes_source_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "quota-log.csv"
            with log_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_HEADER)
                writer.writerows([
                    ["2026-08<x>09:31:00", "<img src=x>", "ok", "10", "10%", "08-23 01:18", ""],
                    ["2026-08<x>20:31:00", "<img src=x>", "ok", "20", "20%", "08-23 01:18", ""],
                ])
            with mock.patch.object(quota_report, "LOG_CSV", log_path):
                rendered = quota_report._history_html(public=False)

        self.assertNotIn("<img", rendered)
        self.assertNotIn("<x>", rendered)
        self.assertIn("&lt;img src=x&gt;", rendered)
        self.assertIn("08&lt;x&gt;", rendered)


class PrepaidSubscriptionTests(unittest.TestCase):
    FINANCE_TEXT = """## 一、当前在用订阅
| 产品 | 档位/形式 | 费用 | 计费周期 | 刷新/到期时间 | 状态与证据 |
|------|----------|------|----------|--------------|-----------|
| ChatGPT ×2 | 土区订阅 | 150 元/月 | 月付 | 每月 2 日、每月 22 日 | 本人确认 |

## ChatGPT 预存余额与扣费计划（私密）
| 账号 | 记录日期 | 预存余额 | 每次扣费 | 可覆盖的扣费日 | 下次需补款的扣费日 |
|------|----------|----------|----------|----------------|--------------------|
| 私密共享账号 | 2026-07-15 | 800 TRY | 400 TRY | 2026-08-02、2026-09-02 | 2026-10-02 |
| 私密本人账号 | 2026-07-15 | 1,200 TRY | 400 TRY | 2026-07-22、2026-08-22、2026-09-22 | 2026-10-22 |

## 二、历史充值与一次性消费
| 日期 | 项目 | 金额 | 备注 |
|------|------|------|------|
| 2026-02-02 | Kimi Agent | 948 元 | 年费 |
"""

    def test_private_prepaid_table_does_not_become_public_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(self.FINANCE_TEXT, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path):
                finance = quota_report._load_finance()

        self.assertEqual(len(finance["subs"]), 1)
        self.assertEqual(len(finance["ledger"]), 1)
        self.assertEqual(len(finance["prepaid"]), 2)
        self.assertEqual(finance["prepaid"][0]["balance"], "800 TRY")
        self.assertEqual(finance["prepaid"][1]["next_topup"], "2026-10-22")

    def test_balances_and_plans_are_visible_only_on_local_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(self.FINANCE_TEXT, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path), \
                 mock.patch.object(quota_report, "LOG_CSV", Path(tmp) / "missing.csv"):
                private_details = ("私密共享账号", "私密本人账号", "800 TRY",
                                   "1,200 TRY", "2026-10-02", "2026-10-22")
                for live in (False, True):
                    local = quota_report.render_html([], live=live)
                    for value in private_details:
                        self.assertIn(value, local)
                    self.assertIn("2026-07-22、2026-08-22、2026-09-22", local)
                    self.assertIn("截至 2026-07-15", local)
                for page in ("all", "overview", "history", "subscriptions"):
                    public = quota_report.render_html([], public=True, page=page)
                    for value in private_details:
                        self.assertNotIn(value, public)

    def test_private_prepaid_details_are_escaped_and_emails_are_masked(self):
        text = self.FINANCE_TEXT.replace("私密本人账号", "<img src=x> owner@example.com")
        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(text, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path):
                local = quota_report._subs_html(public=False)

        self.assertNotIn("<img", local)
        self.assertNotIn("owner@example.com", local)
        self.assertIn("&lt;img src=x&gt; o***@example.com", local)

    def test_public_prepaid_records_use_only_explicit_public_labels(self):
        text = self.FINANCE_TEXT.replace(
            "| 下次需补款的扣费日 |\n", "| 下次需补款的扣费日 | 公开展示名 |\n",
        ).replace(
            "| 2026-10-02 |\n", "| 2026-10-02 | ChatGPT · 共享账号 |\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(text, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path), \
                 mock.patch.object(quota_report, "LOG_CSV", Path(tmp) / "missing.csv"):
                for page in ("all", "subscriptions"):
                    public = quota_report.render_html([], public=True, page=page)
                    self.assertIn("ChatGPT · 共享账号", public)
                    self.assertIn("800 TRY", public)
                    self.assertIn("2026-10-02", public)
                    self.assertNotIn("私密共享账号", public)
                    self.assertNotIn("私密本人账号", public)
                    self.assertNotIn("1,200 TRY", public)
                    self.assertNotIn("2026-10-22", public)
                local = quota_report._subs_html(public=False)
                self.assertIn("私密共享账号", local)
                self.assertIn("私密本人账号", local)

    def test_public_prepaid_label_is_escaped_and_email_is_masked(self):
        text = self.FINANCE_TEXT.replace(
            "| 下次需补款的扣费日 |\n", "| 下次需补款的扣费日 | 公开展示名 |\n",
        ).replace(
            "| 2026-10-02 |\n", "| 2026-10-02 | <img src=x> public@example.com |\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(text, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path):
                public = quota_report._subs_html(public=True)

        self.assertNotIn("<img", public)
        self.assertNotIn("public@example.com", public)
        self.assertNotIn("私密共享账号", public)
        self.assertIn("&lt;img src=x&gt; p***@example.com", public)

    def test_escaped_pipe_in_private_account_cannot_enable_public_display(self):
        text = self.FINANCE_TEXT.replace("私密共享账号", r"私密账号 A \| B")
        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(text, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path):
                finance = quota_report._load_finance()
                public = quota_report._subs_html(public=True)

        self.assertEqual(finance["prepaid"][0]["name"], "私密账号 A | B")
        self.assertNotIn("800 TRY", public)
        self.assertNotIn("2026-10-02", public)

    def test_extra_cell_without_public_header_cannot_authorize_public_display(self):
        text = self.FINANCE_TEXT.replace(
            "| 2026-10-02 |\n", "| 2026-10-02 | 未授权额外内容 |\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(text, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path):
                public = quota_report._subs_html(public=True)

        self.assertNotIn("800 TRY", public)
        self.assertNotIn("未授权额外内容", public)

    def test_private_finance_config_resolves_path_and_masks_public_replacements(self):
        text = (
            self.FINANCE_TEXT
            + "| 2026-11-04 | 好友甲（昵称甲）GPT 合拼分摊 | -240 元 |\n"
            + "| 2026-11-05 | Google Pro | 80 元 |\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            finance_path = root / "finance.md"
            finance_path.write_text(text, encoding="utf-8")
            cfg_path = root / "finance.json"
            cfg_path.write_text(json.dumps({
                "version": 1,
                "finance_md": str(finance_path),
                "public_replacements": {
                    "好友甲": "匿名甲",
                    "好友甲（昵称甲）": "朋友 ",
                },
            }), encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", None), \
                 mock.patch.object(quota_report, "FINANCE_CONFIG_PATH", cfg_path), \
                 mock.patch.dict(quota_report.os.environ, {"QUOTA_FINANCE_MD": ""}):
                public = quota_report._subs_html(public=True)
                local = quota_report._subs_html(public=False)

        self.assertIn("朋友 GPT 合拼分摊", public)
        self.assertNotIn("好友甲", public)
        self.assertNotIn("昵称甲", public)
        self.assertNotIn("匿名甲", public)
        self.assertIn("♊ Google Pro", public)
        self.assertIn("好友甲（昵称甲）GPT 合拼分摊", local)

    def test_env_finance_md_overrides_config_path_while_keeping_public_replacements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_md = root / "env.md"
            cfg_md = root / "cfg.md"
            env_md.write_text(
                self.FINANCE_TEXT + "| 2026-11-04 | 好友甲充值 | 50 元 |\n",
                encoding="utf-8",
            )
            cfg_md.write_text(self.FINANCE_TEXT, encoding="utf-8")
            cfg_path = root / "finance.json"
            cfg_path.write_text(json.dumps({
                "version": 1,
                "finance_md": str(cfg_md),
                "public_replacements": {"好友甲": "朋友"},
            }), encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", None), \
                 mock.patch.object(quota_report, "FINANCE_CONFIG_PATH", cfg_path), \
                 mock.patch.dict(quota_report.os.environ, {"QUOTA_FINANCE_MD": str(env_md)}):
                public = quota_report._subs_html(public=True)

        self.assertIn("朋友充值", public)
        self.assertNotIn("好友甲", public)

    def test_malformed_finance_config_blocks_public_site_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hidden_cfg = root / "public-hidden-accounts.json"
            hidden_cfg.write_text(json.dumps({"codex_emails": ["a@example.com"]}), encoding="utf-8")
            bad_fin_cfg = root / "finance.json"
            bad_fin_cfg.write_text("{invalid-json", encoding="utf-8")
            with mock.patch.object(quota_report, "PUBLIC_HIDDEN_ACCOUNTS_PATH", hidden_cfg), \
                 mock.patch.object(quota_report, "FINANCE_CONFIG_PATH", bad_fin_cfg):
                with self.assertRaisesRegex(RuntimeError, "财务私有配置"):
                    quota_report.render_public_site([], root / "index.html")
            self.assertFalse((root / "index.html").exists())
            self.assertFalse((root / "subscriptions.html").exists())

    def test_missing_finance_config_and_env_returns_empty_finance(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_cfg = Path(tmp) / "missing-finance.json"
            with mock.patch.object(quota_report, "FINANCE_MD", None), \
                 mock.patch.object(quota_report, "FINANCE_CONFIG_PATH", missing_cfg), \
                 mock.patch.dict(quota_report.os.environ, {"QUOTA_FINANCE_MD": ""}):
                fin = quota_report._load_finance()
                html = quota_report._subs_html(public=True)
        self.assertEqual(fin, {"subs": [], "prepaid": [], "ledger": [], "non_ai": [], "total_ai": "", "total_all": ""})
        self.assertEqual(html, "")

    def test_split_cost_headline_and_sub_card_formatting(self):
        self.assertEqual(
            quota_report._split_cost_headline("100 美元/月（实扣人民币 667 元）"),
            ("100 美元/月", "实扣人民币 667 元"),
        )
        self.assertEqual(
            quota_report._split_cost_headline("朋友 付费，不计入本人支出"),
            ("朋友 付费", "不计入本人支出"),
        )
        self.assertEqual(
            quota_report._split_cost_headline("490 元/年"),
            ("490 元/年", ""),
        )
        self.assertEqual(quota_report._sub_provider_meta("DeepSeek API")[0], "deepseek")
        self.assertEqual(quota_report._sub_provider_meta("智谱 ChatGLM")[0], "zhipu")

    def test_subs_html_renders_kpi_countdowns_reversed_ledger_and_nonai_cards(self):
        finance_text = """## 一、当前在用订阅
| 产品 | 档位/形式 | 费用 | 计费周期 | 刷新/到期时间 | 状态与证据 |
|------|----------|------|----------|--------------|-----------|
| ChatGPT Pro | 100 刀 | 100 美元/月（实扣 667 元） | 月付 | 2026-10-27 续订，下次扣费 2026-11-27 | 确认 |
| 朋友 Kimi | 个人 | 朋友 付费，不计入本人支出 | 年付 | 每月 17 日；2027-08-17 到期 | 确认 |
| 智谱 ChatGLM | Coding | 0 元 | 年付 | 2027-05-25 到期 | 确认 |

## 二、历史充值与一次性消费
| 日期 | 项目 | 金额 | 备注 |
|------|------|------|------|
| 2026-02-02 | Kimi Agent | 948 元 | 年费 |
| 2026-11-04 | GPT 分摊结清 | -240.00 元（回款冲抵） | 冲抵 |
| 2026-11-07 | DeepSeek API 获赠算力额度 | 0 元（获赠 1,100 元额度） | 赠额 |

## 四、月度支出估算
- AI 固定订阅合计（本人实付）：约 270 元/月
- 非 AI 固定订阅：
  - iCloud：21 元/月（252 元/年）
  - 百度网盘：会员 2026-10-25 到期。决定不续，Codex 改走本地
  - 视频会员：0 元——手机卡会员活动，白嫖一年
- 固定订阅总额约 **310 元/月**
"""

        class _FixedDate(quota_report.dt.date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 14)

        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(finance_text, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path), \
                 mock.patch.object(quota_report.dt, "date", _FixedDate):
                html = quota_report._subs_html(public=True)

        self.assertIn('class="summary-strip subs-kpi"', html)
        self.assertIn("270 元/月", html)
        self.assertIn("310 元/月", html)
        self.assertIn("最近：ChatGPT Pro（剩 13 天）", html)
        self.assertIn('class="scard sub-card urgent" data-provider="codex"', html)
        self.assertIn("📅 下次扣费 2026-11-27", html)
        self.assertIn('class="scost-main compact free">朋友 付费</div>', html)
        self.assertIn('class="scost-note">不计入本人支出</div>', html)
        self.assertIn('class="scost-main free">0 元</div>', html)
        self.assertLess(html.index("2026-11-04"), html.index("2026-02-02"))
        self.assertIn("<thead><tr><th>日期</th><th>项目</th><th class=\"amt\">金额</th></tr></thead>", html)
        self.assertIn("<td class='amt credit'>-240.00 元（回款冲抵）</td>", html)
        self.assertIn("<td class='amt credit'>0 元（获赠 1,100 元额度）</td>", html)
        self.assertIn("<td class='litem'>⚡ GPT 分摊结清</td>", html)
        self.assertIn("💾 百度网盘", html)
        self.assertIn("<span class='cd neutral'>已停续</span>", html)
        self.assertIn("<span class='cd green'>0 元</span>", html)

    def test_expired_subscription_is_counted_in_kpi_warning(self):
        finance_text = """## 一、当前在用订阅
| 产品 | 档位/形式 | 费用 | 计费周期 | 刷新/到期时间 | 状态与证据 |
|------|----------|------|----------|--------------|-----------|
| 旧订阅 | 月付 | 30 元/月 | 月付 | 2026-11-10 到期 | 确认 |
"""

        class _FixedDate(quota_report.dt.date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 14)

        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(finance_text, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path), \
                 mock.patch.object(quota_report.dt, "date", _FixedDate):
                html = quota_report._subs_html(public=True)

        self.assertIn('<span class="kpi-val">1</span><span class="kpi-sub">最近：旧订阅（已到期）</span>', html)

    def test_reserve_prepaid_pool_and_nonrenewing_sub_render_without_urgent_alarm(self):
        finance_text = """## 一、当前在用订阅
| 产品 | 档位/形式 | 费用 | 计费周期 | 刷新/到期时间 | 状态与证据 |
|------|----------|------|----------|--------------|-----------|
| Google Pro | 土区 | 80 元 | 月付 | 2026-12-05 到期（不续费） | 确认 |

## ChatGPT 预存余额与扣费计划（私密）
| 账号 | 记录日期 | 预存余额 | 每次扣费 | 可覆盖的扣费日 | 下次需补款的扣费日 | 公开展示名 |
|------|----------|----------|----------|----------------|--------------------|------------|
| Apple · 测试账号 | 2026-11-14 | 3,130.07 TRY | — | 原用于 Google Pro，余款留作备用 | 停续留存 | Apple 土区 · 余额池 |
"""

        class _FixedDate(quota_report.dt.date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 14)

        with tempfile.TemporaryDirectory() as tmp:
            finance_path = Path(tmp) / "finance.md"
            finance_path.write_text(finance_text, encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path), \
                 mock.patch.object(quota_report.dt, "date", _FixedDate):
                html = quota_report._subs_html(public=True)

        self.assertIn('<span class="kpi-val">0</span><span class="kpi-sub">未来 30 天无临近到期</span>', html)
        self.assertIn('class="scard sub-card" data-provider="gemini"', html)
        self.assertIn("剩 21 天·不续", html)
        self.assertIn("2026-12-05 到期（不续）", html)
        self.assertIn('class="scard prepaid-card" data-provider="apple"', html)
        self.assertIn("余额 3,130.07 TRY", html)
        self.assertIn("<span class='cd neutral'>停续留存</span>", html)
        self.assertIn("暂不自动扣费", html)


class EmailPrivacyTests(unittest.TestCase):
    def test_static_live_and_public_pages_mask_cards_notes_errors_and_alerts(self):
        rows = [
            {"name": "Codex · first@163.com", "status": "ok", "used_pct": 16,
             "used_text": "16%", "reset": "10-10 11:30",
             "note": "当前活跃：owner+tag@example.com"},
            {"name": "Codex · second@hotmail.com",
             "status": "登录失败：second@hotmail.com"},
        ]
        alerts = [{"name": "Codex · first@163.com", "kind": "week",
                   "until": "1 小时后", "remain": 84}]
        with mock.patch.object(quota_report, "_alerts", return_value=alerts):
            for options in ({}, {"live": True}, {"public": True}):
                with self.subTest(options=options):
                    page = quota_report.render_html(rows, page="overview", **options)
                    for email in ("first@163.com", "owner+tag@example.com",
                                  "second@hotmail.com"):
                        self.assertNotIn(email, page)
                    for masked in ("f***@163.com", "o***@example.com", "s***@hotmail.com"):
                        self.assertIn(masked, page)
        self.assertEqual(rows[0]["name"], "Codex · first@163.com")
        self.assertEqual(rows[0]["note"], "当前活跃：owner+tag@example.com")

    def test_history_masks_labels_attributes_and_filter_options(self):
        history = [("2026-10-04", [
            {"name": "Codex · first@163.com", "delta": 5, "reset": False, "partial": False},
            {"name": "Codex · second@hotmail.com", "delta": 0, "reset": True, "partial": False},
        ])]
        with mock.patch.object(quota_report, "_daily_deltas", return_value=history):
            for public in (False, True):
                with self.subTest(public=public):
                    page = quota_report._history_html(public=public, controls=True)
                    for raw, masked in (("first@163.com", "f***@163.com"),
                                        ("second@hotmail.com", "s***@hotmail.com")):
                        self.assertNotIn(raw, page)
                        self.assertIn(f'data-source="Codex · {masked}"', page)
                        self.assertIn(f'<option value="Codex · {masked}">', page)

    def test_subscription_pages_mask_emails_before_html_escaping(self):
        finance = {
            "subs": [{"name": "Codex · first@163.com", "cost": "99 元/月",
                      "renewal": "<b>o@x.co</b>"}],
            "ledger": [{"date": "2026-10-04", "item": "充值 first@163.com", "amount": "10 元"}],
            "non_ai": ["会员 second@hotmail.com"], "total_ai": "", "total_all": "",
        }
        with mock.patch.object(quota_report, "_load_finance", return_value=finance):
            for public in (False, True):
                with self.subTest(public=public):
                    page = quota_report.render_html([], public=public, page="subscriptions")
                    for email in ("first@163.com", "o@x.co", "second@hotmail.com"):
                        self.assertNotIn(email, page)
                    self.assertIn("&lt;b&gt;o***@x.co&lt;/b&gt;", page)


class PageSplitTests(unittest.TestCase):
    def test_public_site_refuses_missing_or_invalid_account_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "public-hidden-accounts.json"
            for content in (None, "{not-json}",
                            json.dumps({"codex_emails": ["not-an-email"]}),
                            json.dumps({"codex_emails": ["valid@example.com", 123]})):
                if content is not None:
                    config.write_text(content, encoding="utf-8")
                with mock.patch.object(quota_report, "PUBLIC_HIDDEN_ACCOUNTS_PATH", config):
                    with self.assertRaisesRegex(RuntimeError, "公开过滤配置"):
                        quota_report.render_public_site([], root / "index.html")
                self.assertFalse((root / "index.html").exists())

    def test_public_render_refuses_missing_account_filter_even_without_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "missing.json"
            rows = [{"name": "Codex · 1private@qq.com", "status": "ok",
                     "used_pct": 10, "used_text": "10%", "reset": "?"}]
            with mock.patch.object(quota_report, "PUBLIC_HIDDEN_ACCOUNTS_PATH", config):
                with self.assertRaisesRegex(RuntimeError, "公开过滤配置"):
                    quota_report.render_html(rows, public=True, page="overview")

    def test_public_site_splits_overview_history_and_subscriptions(self):
        snapshots = []
        for day, start, end in (
            ("2026-08-18", 10, 15),
            ("2026-08-19", 15, 22),
            ("2026-08-20", 22, 31),
        ):
            snapshots.extend([
                [f"{day}T09:31:00", "Kimi · 本人", "ok", str(start), f"{start}%", "08-24 01:18", ""],
                [f"{day}T20:31:00", "Kimi · 本人", "ok", str(end), f"{end}%", "08-24 01:18", ""],
            ])

        finance = {
            "subs": [{"name": "Kimi", "cost": "99 元/月", "renewal": "每月 2 日"}],
            "ledger": [{"date": "2026-08-01", "item": "测试充值", "amount": "10 元"}],
            "non_ai": ["iCloud：6 元/月"],
            "total_ai": "99 元/月",
            "total_all": "105 元/月",
        }
        quota_rows = [{
            "name": "Kimi · 本人（948/年）",
            "status": "ok",
            "used_pct": 31.0,
            "used_text": "31/100",
            "reset": "08-24 01:18",
        }]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "public-hidden-accounts.json"
            config.write_text(json.dumps({"codex_emails": ["test@example.invalid"]}), encoding="utf-8")
            log_path = root / "quota-log.csv"
            with log_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_HEADER)
                writer.writerows(snapshots)
            with (
                mock.patch.object(quota_report, "LOG_CSV", log_path),
                mock.patch.object(quota_report, "PUBLIC_HIDDEN_ACCOUNTS_PATH", config),
                mock.patch.object(quota_report, "_load_finance", return_value=finance),
            ):
                quota_report.render_public_site(quota_rows, root / "index.html")

            overview = (root / "index.html").read_text(encoding="utf-8")
            history = (root / "history.html").read_text(encoding="utf-8")
            subscriptions = (root / "subscriptions.html").read_text(encoding="utf-8")

        for page in (overview, history, subscriptions):
            self.assertIn('href="index.html"', page)
            self.assertIn('href="history.html"', page)
            self.assertIn('href="subscriptions.html"', page)
            self.assertIn("有新数据", page)

        self.assertIn("08-20", overview)
        self.assertIn("08-19", overview)
        self.assertNotIn("08-18", overview)
        self.assertNotIn("消费台账", overview)

        self.assertIn("08-18", history)
        self.assertIn('data-days="7"', history)
        self.assertIn('data-days="30"', history)
        self.assertIn('id="sourceFilter"', history)

        self.assertIn("消费台账", subscriptions)
        self.assertIn("iCloud：6 元/月", subscriptions)
        self.assertNotIn("每日消耗（周计数口径）", subscriptions)


class SnapshotFreshnessTests(unittest.TestCase):
    def test_stale_row_is_counted_as_unhealthy(self):
        row = {"name": "MiniMax", "status": "ok", "stale": True}
        self.assertEqual(quota_report._unhealthy_rows([row]), [row])

    def test_cached_snapshot_is_not_logged_as_a_new_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "quota.csv"
            with mock.patch.object(quota_report, "LOG_CSV", log):
                quota_report.append_log([{
                    "name": "MiniMax", "status": "ok", "stale": True,
                    "used_pct": 66, "used_text": "66%",
                }])
            with log.open() as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["status"], "stale")

    def test_cache_fallback_is_flagged_on_the_card(self):
        row = {"name": "MiniMax", "status": "ok", "used_pct": 66,
               "used_text": "66%", "reset": "?", "cache_fallback": True}
        self.assertIn("使用缓存", quota_report._card(row))


class PublicOverviewEmailTests(unittest.TestCase):
    def test_public_page_masks_account_emails(self):
        rows = [{
            "name": "Codex · first@163.com",
            "status": "ok",
            "used_pct": 16.0,
            "used_text": "16%",
            "reset": "10-10 11:30",
        }]

        page = quota_report.render_html(rows, public=True, page="overview")

        self.assertNotIn("first@163.com", page)
        self.assertIn("f***@163.com", page)


class QuotaVisibilityTests(unittest.TestCase):
    def test_only_exact_configured_codex_account_is_hidden_publicly(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "public-hidden-accounts.json"
            config.write_text(json.dumps({"codex_emails": ["1first@qq.com"]}), encoding="utf-8")
            rows = [
                {"name": name, "status": "ok", "used_pct": 10,
                 "used_text": "10%", "reset": "?"}
                for name in ("Codex · 1first@qq.com", "Codex · 1second@qq.com",
                             "MiniMax")
            ]
            with mock.patch.object(quota_report, "PUBLIC_HIDDEN_ACCOUNTS_PATH", config), \
                 mock.patch.object(quota_report, "_daily_deltas", return_value=[]), \
                 mock.patch.object(quota_report, "_load_finance", return_value={
                     "subs": [], "ledger": [], "non_ai": [], "total_ai": "", "total_all": ""}):
                public = quota_report.render_html(rows, public=True, page="overview")
                local = quota_report.render_html(rows, public=False, page="overview")

        self.assertEqual(public.count('class="card"'), 2)
        self.assertEqual(public.count("Codex · 1***@qq.com"), 1)
        self.assertIn("MiniMax", public)
        self.assertEqual(local.count('class="card"'), 3)

    def test_public_history_hides_only_configured_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "public-hidden-accounts.json"
            config.write_text(json.dumps({"codex_emails": ["1first@qq.com"]}), encoding="utf-8")
            log_path = root / "quota-log.csv"
            with log_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_HEADER)
                for name in ("Codex · 1first@qq.com", "Codex · 1second@qq.com",
                             "MiniMax"):
                    writer.writerow(["2026-08-19T23:31:00", name, "ok", "10", "10%", "08-24 00:00", ""])
                    writer.writerow(["2026-08-20T20:31:00", name, "ok", "18", "18%", "08-24 00:00", ""])
            with mock.patch.object(quota_report, "PUBLIC_HIDDEN_ACCOUNTS_PATH", config), \
                 mock.patch.object(quota_report, "LOG_CSV", log_path):
                public_rows = quota_report._daily_deltas(public=True)
                local_rows = quota_report._daily_deltas()

        self.assertEqual(
            {row["name"] for _date, rows in public_rows for row in rows},
            {"Codex · 1second@qq.com", "MiniMax"},
        )
        self.assertEqual(
            {row["name"] for _date, rows in local_rows for row in rows},
            {"Codex · 1first@qq.com", "Codex · 1second@qq.com", "MiniMax"},
        )

    def test_doubao_is_hidden_from_dashboards_and_subscriptions(self):
        names = ["豆包 · 体验套餐", "Kimi", "Codex · doubao@example.com",
                 "MiniMax", "Antigravity · Gemini 组"]
        rows = [{"name": name, "status": "ok", "used_pct": 10,
                 "used_text": "10%", "reset": "?"} for name in names]
        finance = {
            "subs": [{"name": "豆包会员", "cost": "费用待核对", "renewal": "2026-12-15 到期"},
                     {"name": "MiniMax", "cost": "490 元/年", "renewal": "2027-03-22 到期"}],
            "ledger": [], "non_ai": [], "total_ai": "", "total_all": "",
        }
        with mock.patch.object(quota_report, "_load_finance", return_value=finance), \
             mock.patch.object(quota_report, "_daily_deltas", return_value=[]):
            for public in (False, True):
                page = quota_report.render_html(rows, public=public)
                self.assertNotIn("豆包 · 体验套餐", page)
                for name in ("Kimi", "MiniMax", "Antigravity · Gemini 组"):
                    self.assertIn(name, page)
                self.assertIn("Codex · d***@example.com", page)
                self.assertIn("490 元/年", page)
        self.assertFalse(quota_report._quota_hidden("Codex · doubao@example.com"))
        self.assertTrue(quota_report._quota_hidden("豆包 · 体验套餐"))


class CollectSourceTests(unittest.TestCase):
    def test_collect_requires_sub2api_config(self):
        with mock.patch.object(quota_report.sub2api_quota, "load_config", return_value=None):
            with self.assertRaises(quota_report.sub2api_quota.ConfigError):
                quota_report.collect()

    def test_collect_runs_sub2api_enrichment_and_hides_retired_rows(self):
        cfg = {"accounts": [{"id": 5, "platform": "minimax", "collector": "minimax", "name": "MiniMax"}]}
        rows = [
            {"name": "MiniMax", "status": "ok"},
            {"name": "豆包 · 个人会员", "status": "ok"},
        ]
        with mock.patch.object(quota_report.sub2api_quota, "load_config", return_value=cfg), \
             mock.patch.object(quota_report.sub2api_quota, "collect", return_value=rows):
            names = [r["name"] for r in quota_report.collect()]

        self.assertEqual(names, ["MiniMax"])


class AuditBugFixTests(unittest.TestCase):
    def test_fiveh_only_source_is_not_treated_as_weekly_quota_or_weekly_exhausted(self):
        now = dt.datetime.now()
        rows = [
            {
                "name": "Codex · test@example.com",
                "status": "ok",
                "used_pct": 20.0,
                "used_text": "20%",
                "reset": (now + dt.timedelta(days=5)).strftime("%m-%d %H:%M"),
            },
            {
                "name": "MiniMax",
                "status": "ok",
                "used_pct": None,
                "used_text": "未提供",
                "reset": "?",
                "fiveh_pct": 85.0,
                "fiveh_text": "85%",
                "fiveh_reset": (now + dt.timedelta(hours=1)).strftime("%m-%d %H:%M"),
            },
            {
                "name": "MiniMax Full",
                "status": "ok",
                "used_pct": None,
                "used_text": "未提供",
                "reset": "?",
                "fiveh_pct": 100.0,
                "fiveh_text": "100%",
                "fiveh_reset": (now + dt.timedelta(hours=2)).strftime("%m-%d %H:%M"),
            },
        ]
        alerts = quota_report._alerts(rows)
        self.assertEqual([a["name"] for a in alerts], ["MiniMax"])
        full_card = quota_report._card(rows[2])
        self.assertIn("5h 已满", full_card)
        self.assertNotIn("已用完", full_card)
        near_full_card = quota_report._card({**rows[1], "fiveh_pct": 98.0, "fiveh_text": "98%"})
        self.assertNotIn("已用完", near_full_card)
        strip = quota_report._summary_strip_html(rows, alerts)
        # 平均周剩余仅统计真实有周额度的账号（Codex 剩 80%），不被 MiniMax 5h 剩余稀释
        self.assertIn('<span class="kpi-label">平均周剩余</span><span class="kpi-val">80%</span>', strip)

    def test_cache_helpers_write_atomically_with_0600_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agy_path = root / "agy-groups.json"
            last_path = root / "last-good.json"
            with mock.patch.object(quota_report, "AGY_GROUPS_CACHE", agy_path), \
                 mock.patch.object(quota_report, "LAST_GOOD_JSON", last_path):
                quota_report._save_agy_groups_cache({"Gemini · 3.8 Flash": {"used_pct": 1.0}})
                quota_report._save_last_good([{"name": "MiniMax", "status": "ok", "used_pct": None}])
            self.assertEqual(agy_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(last_path.stat().st_mode & 0o777, 0o600)

    def test_refresh_state_always_clears_updating_flag_on_unexpected_exception(self):
        state = {"rows": [{"name": "MiniMax", "status": "ok"}], "updating": True, "last": "2026-11-14T12:00:00"}
        with mock.patch.object(quota_report, "_STATE", state), \
             mock.patch.object(quota_report, "collect", side_effect=RuntimeError("boom")):
            quota_report._refresh_state(log=False)
        self.assertFalse(state["updating"])
        self.assertTrue(state["rows"][0]["stale"])
        self.assertTrue(state["rows"][0]["cache_fallback"])

    def test_history_and_subscriptions_pages_skip_unused_overview_card_rendering(self):
        rows = [{"name": "MiniMax", "status": "ok", "used_pct": 10.0, "used_text": "10%", "reset": "?"}]
        with mock.patch.object(quota_report, "_card", side_effect=AssertionError("should skip cards")), \
             mock.patch.object(quota_report, "_alerts", side_effect=AssertionError("should skip alerts")), \
             mock.patch.object(quota_report, "_daily_deltas", return_value=[]):
            html_h = quota_report.render_html(rows, public=True, page="history")
            html_s = quota_report.render_html(rows, public=True, page="subscriptions")
        self.assertIn("每日消耗", html_h)
        self.assertIn("订阅账单", html_s)

    def test_history_controls_include_empty_state_placeholder(self):
        history = [("2026-11-14", [{"name": "MiniMax", "delta": 5.0, "reset": False, "partial": False}])]
        with mock.patch.object(quota_report, "_daily_deltas", return_value=history):
            page = quota_report.render_html([], public=False, page="history")
        self.assertIn('id="historyEmpty"', page)
        self.assertIn("emptyBox.hidden=visibleDays>0", page)

    def test_non_ai_includes_ikuuu_with_urgent_expiry_and_kpi_warning(self):
        finance_text = """## 一、当前在用订阅
| 产品 | 档位/形式 | 费用 | 计费周期 | 刷新/到期时间 | 状态与证据 |
|------|----------|------|----------|--------------|-----------|
| ChatGPT | 月付 | 150 元/月 | 月付 | 每月 2 日 | 确认 |

## 四、月度支出估算
- **AI 固定订阅合计（本人实付）：约 150 元/月**
  - 内部明细：不该进入非 AI
- **非 AI 固定订阅**：
  - iCloud：21 元/月
  - ikuuu（科学上网）双账号：**QQ 邮箱账号 2026-11-21 到期**；**网易邮箱账号 2027-06-27 到期**（8/24 好友甲疑似充了三个月）
- **合并口径**：当前已知固定订阅总额约 **171 元/月**
"""

        class _FixedDate(quota_report.dt.date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 14)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            finance_path = root / "finance.md"
            finance_path.write_text(finance_text, encoding="utf-8")
            cfg_path = root / "finance.json"
            cfg_path.write_text(json.dumps({
                "version": 1,
                "finance_md": str(finance_path),
                "public_replacements": {"好友甲": "亲友"},
            }), encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_MD", finance_path), \
                 mock.patch.object(quota_report, "FINANCE_CONFIG_PATH", cfg_path), \
                 mock.patch.object(quota_report.dt, "date", _FixedDate):
                html = quota_report._subs_html(public=True)

        self.assertIn("🌐 ikuuu（科学上网）双账号", html)
        self.assertIn('class="scard nonai-card urgent"', html)
        self.assertIn("<span class='cd red'>剩 7 天</span>", html)
        self.assertIn("最近：ikuuu（剩 7 天）", html)
        self.assertIn("亲友疑似充了三个月", html)
        self.assertNotIn("好友甲", html)
        self.assertNotIn("内部明细", html)

    def test_public_replacements_mask_overview_and_history_labels(self):
        rows = [{
            "name": "Kimi · 私密代号（199/月）",
            "status": "ok",
            "used_pct": 25.0,
            "used_text": "25%",
            "reset": "11-16 15:17",
        }]
        history = [("2026-11-14", [{"name": "Kimi · 私密代号", "delta": 10.0, "reset": False, "partial": False}])]
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "finance.json"
            cfg_path.write_text(json.dumps({
                "version": 1,
                "public_replacements": {
                    "Kimi · 私密代号（199/月）": "Kimi · 副账号",
                    "Kimi · 私密代号": "Kimi · 副账号",
                },
            }), encoding="utf-8")
            with mock.patch.object(quota_report, "FINANCE_CONFIG_PATH", cfg_path), \
                 mock.patch.object(quota_report, "_daily_deltas", return_value=history):
                overview = quota_report.render_html(rows, public=True, page="overview")
                hist_page = quota_report.render_html([], public=True, page="history")
        for page in (overview, hist_page):
            self.assertIn("Kimi · 副账号", page)
            self.assertNotIn("私密代号", page)


if __name__ == "__main__":
    unittest.main()
