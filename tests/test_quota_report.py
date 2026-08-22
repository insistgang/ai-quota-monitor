import csv
import datetime as dt
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import quota_report


CSV_HEADER = ["ts", "name", "status", "used_pct", "used_text", "reset", "fiveh_text"]


class DoubaoDomTests(unittest.TestCase):
    DOM_TEXT = """
账号昵称不应进入缓存
标准套餐
订阅与额度管理免费体验至9月16日
升级至标准套餐
购买创作额度包
当前时段
已用 <1%
2 小时 24 分钟后重置
近 7 天
已用 7%
8月24日 21:55 重置
订阅记录
"""

    def test_parse_visible_dom_extracts_weekly_and_current_windows(self):
        captured = dt.datetime(
            2026, 8, 21, 16, 40,
            tzinfo=dt.timezone(dt.timedelta(hours=8)),
        )

        row = quota_report._parse_doubao_dom(self.DOM_TEXT, captured)

        self.assertEqual(row["name"], "豆包 · 标准套餐")
        self.assertEqual(row["used_pct"], 7.0)
        self.assertEqual(row["used_text"], "7%")
        self.assertEqual(row["reset"], "08-24 21:55")
        self.assertEqual(row["fiveh_pct"], 1.0)
        self.assertEqual(row["fiveh_text"], "<1%")
        self.assertEqual(row["fiveh_reset"], "08-21 19:04")
        self.assertEqual(row["fiveh_label"], "当前时段")
        self.assertIn("免费体验至9月16日", row["note"])

    def test_parse_visible_dom_treats_used_up_window_as_fully_used(self):
        captured = dt.datetime(
            2026, 8, 21, 23, 35,
            tzinfo=dt.timezone(dt.timedelta(hours=8)),
        )
        dom_text = self.DOM_TEXT.replace("已用 <1%", "已用完").replace(
            "2 小时 24 分钟后重置",
            "47 分钟后重置",
        )

        row = quota_report._parse_doubao_dom(dom_text, captured)

        self.assertEqual(row["fiveh_pct"], 100.0)
        self.assertEqual(row["fiveh_text"], "100%")
        self.assertEqual(row["fiveh_reset"], "08-22 00:22")

    def test_import_cache_contains_only_normalized_quota_fields(self):
        captured = dt.datetime(
            2026, 8, 21, 16, 40,
            tzinfo=dt.timezone(dt.timedelta(hours=8)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "doubao-quota.json"

            quota_report._import_doubao_dom(self.DOM_TEXT, cache_path, captured)
            payload = json.loads(cache_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["source_url"], quota_report.DOUBAO_QUOTA_URL)
            self.assertEqual(payload["row"]["used_pct"], 7.0)
            self.assertNotIn("账号昵称", cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cache_path.stat().st_mode & 0o777, 0o600)

    def test_stale_cache_remains_visible_but_does_not_look_live(self):
        captured = dt.datetime(
            2026, 8, 21, 8, 0,
            tzinfo=dt.timezone(dt.timedelta(hours=8)),
        )
        snapshot = quota_report._doubao_snapshot(self.DOM_TEXT, captured)
        now = captured + dt.timedelta(hours=7)

        with mock.patch.object(quota_report, "_doubao_max_age_hours", return_value=6):
            row = quota_report._doubao_row_from_snapshot(snapshot, now)

        self.assertTrue(row["stale"])
        self.assertIn("快照已过期", row["note"])
        self.assertEqual(quota_report._alerts([row]), [])
        self.assertIn("快照过期", quota_report._card(row))

    def test_parser_rejects_non_quota_page_text(self):
        with self.assertRaisesRegex(ValueError, "当前时段/近 7 天"):
            quota_report._parse_doubao_dom("豆包聊天页")

    def test_account_name_ending_in_package_is_not_used_as_plan(self):
        dom_text = self.DOM_TEXT.replace(
            "账号昵称不应进入缓存\n标准套餐",
            "隐私昵称套餐\n订阅与额度管理免费体验至1月1日\n其他导航文字\n标准套餐",
        )

        row = quota_report._parse_doubao_dom(dom_text)

        self.assertEqual(row["name"], "豆包 · 标准套餐")
        self.assertNotIn("隐私昵称", json.dumps(row, ensure_ascii=False))
        self.assertIn("免费体验至9月16日", row["note"])
        self.assertNotIn("免费体验至1月1日", row["note"])

    def test_parser_rejects_percentage_above_one_hundred(self):
        dom_text = self.DOM_TEXT.replace("已用 7%", "已用 120%")

        with self.assertRaisesRegex(ValueError, "百分比超出范围"):
            quota_report._parse_doubao_dom(dom_text)

    def test_parser_rejects_invalid_absolute_reset_date(self):
        dom_text = self.DOM_TEXT.replace(
            "8月24日 21:55 重置",
            "13月40日 99:99 重置",
        )

        with self.assertRaisesRegex(ValueError, "重置时间非法"):
            quota_report._parse_doubao_dom(dom_text)

    def test_parser_rejects_unbounded_relative_reset_date(self):
        dom_text = self.DOM_TEXT.replace(
            "2 小时 24 分钟后重置",
            f"{10 ** 50} 天后重置",
        )

        with self.assertRaisesRegex(ValueError, "重置时间非法"):
            quota_report._parse_doubao_dom(dom_text)

    def test_cache_reader_rejects_out_of_range_normalized_values(self):
        snapshot = quota_report._doubao_snapshot(self.DOM_TEXT)
        snapshot["row"]["used_pct"] = 120.0
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "doubao-quota.json"
            cache_path.write_text(json.dumps(snapshot), encoding="utf-8")

            self.assertIsNone(quota_report._read_doubao_snapshot(cache_path))

    def test_chrome_reader_requests_a_page_refresh_before_reading_dom(self):
        def fake_run(command, **kwargs):
            if command[0] == "pgrep":
                return subprocess.CompletedProcess(command, 0, stdout="123\n", stderr="")
            if command[-1] == "refresh":
                return subprocess.CompletedProcess(
                    command, 0, stdout=self.DOM_TEXT, stderr="",
                )
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="refresh mode missing",
            )

        with mock.patch.object(quota_report.subprocess, "run", side_effect=fake_run):
            text = quota_report._read_doubao_chrome_dom()

        self.assertIn("近 7 天", text)

    def test_applescript_refreshes_the_matching_tab_and_waits_for_quota_dom(self):
        source = quota_report.DOUBAO_APPLESCRIPT.read_text(encoding="utf-8")

        self.assertIn("reload chromeTab", source)
        self.assertIn("repeat with attempt", source)
        self.assertLess(
            source.index("reload chromeTab"),
            source.index("execute chromeTab javascript jsCode"),
        )

    def test_chrome_reader_recognizes_chinese_javascript_permission_error(self):
        permission_error = (
            "通过 AppleScript 执行 JavaScript 的功能已关闭。"
            "请允许 Apple 事件中的 JavaScript。"
        )
        results = [
            subprocess.CompletedProcess(["pgrep"], 0, stdout="123\n", stderr=""),
            subprocess.CompletedProcess(
                ["osascript"], 1, stdout="", stderr=permission_error,
            ),
        ]

        with mock.patch.object(quota_report.subprocess, "run", side_effect=results):
            with self.assertRaisesRegex(RuntimeError, "未允许来自 Apple 事件"):
                quota_report._read_doubao_chrome_dom()

    def test_live_refresh_failure_marks_cache_as_non_realtime(self):
        captured = dt.datetime.now().astimezone() - dt.timedelta(minutes=5)
        snapshot = quota_report._doubao_snapshot(self.DOM_TEXT, captured)

        with (
            mock.patch.object(
                quota_report,
                "_sync_doubao_chrome",
                side_effect=RuntimeError("Chrome 中未打开豆包额度管理页"),
            ),
            mock.patch.object(quota_report, "_read_doubao_snapshot", return_value=snapshot),
        ):
            row = quota_report._doubao_quota()

        self.assertTrue(row["stale"])
        self.assertIn("实时刷新失败", row["note"])
        self.assertIn("使用缓存", row["note"])
        self.assertIn("Chrome 中未打开豆包额度管理页", row["note"])
        self.assertIn(
            "<span class='pill stale'>使用缓存</span>",
            quota_report._card(row),
        )


class AntigravityQuotaTests(unittest.TestCase):
    def test_exhausted_group_stays_visible_with_full_usage_and_reset_time(self):
        panel = """
CLAUDE AND GPT MODELS
  Models within this group: Claude Opus, Claude Sonnet, GPT-OSS

  Weekly Limit Remaining
    [░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 0.00%
    Refreshes in 21h 11m

  Five Hour Limit Remaining
    Disabled: You have hit your weekly limit, the 5-hour limit does not currently apply.
"""

        with mock.patch.object(quota_report, "_tmux_slash_probe", return_value=panel):
            rows = quota_report._agy()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Antigravity · Claude/GPT 组")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["used_pct"], 100.0)
        self.assertEqual(rows[0]["used_text"], "100%")
        self.assertRegex(rows[0]["reset"], r"^\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertNotIn("fiveh_pct", rows[0])

        card = quota_report._card(rows[0])
        self.assertIn(">100%</div>", card)
        self.assertIn(f"重置 {rows[0]['reset']}", card)


class CollectionRetryTests(unittest.TestCase):
    def test_retries_only_failed_or_cached_source_batches(self):
        calls = {"stable": 0, "failed": 0, "cached": 0}

        def stable():
            calls["stable"] += 1
            return [{"name": "稳定来源", "status": "ok"}]

        def failed_then_ok():
            calls["failed"] += 1
            if calls["failed"] == 1:
                return [{"name": "瞬时失败来源", "status": "查询失败"}]
            return [{"name": "瞬时失败来源", "status": "ok"}]

        def cached_then_fresh():
            calls["cached"] += 1
            if calls["cached"] == 1:
                return [{"name": "缓存来源", "status": "ok", "stale": True}]
            return [{"name": "缓存来源", "status": "ok", "stale": False}]

        with mock.patch.object(quota_report.time, "sleep") as sleep:
            rows = quota_report._collect_batches_with_retries(
                [
                    ("stable", stable),
                    ("failed", failed_then_ok),
                    ("cached", cached_then_fresh),
                ],
                retry_attempts=2,
                retry_delay=15,
            )

        self.assertEqual([row["status"] for row in rows], ["ok", "ok", "ok"])
        self.assertFalse(rows[2]["stale"])
        self.assertEqual(calls, {"stable": 1, "failed": 2, "cached": 2})
        sleep.assert_called_once_with(15)


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


class PageSplitTests(unittest.TestCase):
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
            log_path = root / "quota-log.csv"
            with log_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_HEADER)
                writer.writerows(snapshots)
            with (
                mock.patch.object(quota_report, "LOG_CSV", log_path),
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


if __name__ == "__main__":
    unittest.main()
