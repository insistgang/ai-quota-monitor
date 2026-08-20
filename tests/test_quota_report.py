import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import quota_report


CSV_HEADER = ["ts", "name", "status", "used_pct", "used_text", "reset", "fiveh_text"]


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


if __name__ == "__main__":
    unittest.main()
