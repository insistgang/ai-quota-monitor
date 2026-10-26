import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sub2api_quota as quota
import quota_report


NOW = 1800000000


def account(platform="openai", account_id=6):
    return {"id": account_id, "platform": platform, "name": "Codex · test@example.invalid",
            "collector": "codex_auth"}


def snapshot(data, platform="openai", account_id=6):
    return {"id": account_id, "platform": platform, "http_status": 200, "data": data}


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def openai_data():
    return {"fetched_at": NOW, "plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": 31, "limit_window_seconds": 604800, "reset_at": NOW + 86400},
        "secondary_window": {"used_percent": 9, "limit_window_seconds": 18000, "reset_at": NOW + 3600},
    }}


class NormalizationTests(unittest.TestCase):
    def test_openai_uses_explicit_window_duration_not_position(self):
        row = quota.normalize_account(account(), snapshot(openai_data()), now=NOW)
        self.assertEqual(row["used_pct"], 31)
        self.assertEqual(row["fiveh_pct"], 9)
        self.assertEqual(row["source"], "sub2api")
        self.assertEqual(row["captured_at"], NOW)
        self.assertFalse(row["stale"])

    def test_unknown_window_is_not_mislabeled_weekly(self):
        data = openai_data()
        data["rate_limit"]["primary_window"]["limit_window_seconds"] = 3600
        row = quota.normalize_account(account(), snapshot(data), now=NOW)
        self.assertIsNone(row["used_pct"])
        self.assertEqual(row["fiveh_pct"], 9)

    def test_minimax_short_window_does_not_invent_weekly_quota(self):
        data = {"success": True, "credential_valid": True, "fetched_at": NOW,
                "tiers": [{"window": "5h", "used_percent": 39, "reset_at": iso(NOW + 3600)}]}
        row = quota.normalize_account(account("minimax", 5), snapshot(data, "minimax", 5), now=NOW)
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["used_pct"])
        self.assertEqual(row["fiveh_pct"], 39)
        self.assertIn("周", row["note"])

    def test_kimi_keeps_zero_and_separates_weekly(self):
        data = {"success": True, "credential_valid": True, "fetched_at": NOW, "tiers": [
            {"window": "5h", "used_percent": 0, "reset_at": iso(NOW + 3600)},
            {"window": "weekly", "used_percent": 100, "reset_at": iso(NOW + 86400)},
        ]}
        row = quota.normalize_account(account("kimi", 3), snapshot(data, "kimi", 3), now=NOW)
        self.assertEqual(row["fiveh_pct"], 0)
        self.assertEqual(row["used_pct"], 100)

    def test_cn_overage_is_preserved_while_progress_bars_are_bounded(self):
        data = {"success": True, "credential_valid": True, "fetched_at": NOW, "tiers": [
            {"window": "5h", "used_percent": 120, "reset_at": iso(NOW + 3600)},
            {"window": "weekly", "used_percent": 105, "reset_at": iso(NOW + 86400)},
        ]}
        row = quota.normalize_account(account("kimi", 3), snapshot(data, "kimi", 3), now=NOW)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["used_pct"], 105)
        self.assertEqual(quota_report._remain_pct(row), 0)
        page = quota_report._card(row)
        self.assertIn("105%", page)
        self.assertNotIn("width:105", page)
        self.assertNotIn("width:120", page)

    def test_gateway_wallet_is_not_subscription_quota(self):
        row = quota.normalize_account(account(), snapshot({"remaining": 500, "usage": {"today": {"cost": 3}}}), now=NOW)
        self.assertNotEqual(row["status"], "ok")
        self.assertNotIn("used_pct", row)

    def test_invalid_percentages_are_not_reported_as_healthy(self):
        for invalid in (None, True, -1, 101, float("nan"), float("inf"), "not-a-number"):
            data = openai_data()
            data["rate_limit"]["primary_window"]["used_percent"] = invalid
            with self.subTest(value=invalid):
                row = quota.normalize_account(account(), snapshot(data), now=NOW)
                self.assertNotEqual(row["status"], "ok")

    def test_stale_missing_future_and_expired_windows_are_visible(self):
        for stamp in (None, NOW - 301, NOW + 61):
            data = openai_data(); data["fetched_at"] = stamp
            row = quota.normalize_account(account(), snapshot(data), now=NOW)
            self.assertTrue(row["stale"])
        data = openai_data(); data["rate_limit"]["primary_window"]["reset_at"] = NOW - 1
        self.assertTrue(quota.normalize_account(account(), snapshot(data), now=NOW)["stale"])

    def test_expired_short_window_does_not_discard_fresh_weekly_data(self):
        data = openai_data(); data["rate_limit"]["secondary_window"]["reset_at"] = NOW - 1
        row = quota.normalize_account(account(), snapshot(data), now=NOW)
        self.assertEqual(row["used_pct"], 31)
        self.assertNotIn("fiveh_pct", row)
        self.assertFalse(row["stale"])

    def test_http_and_provider_failure_redact_upstream_message(self):
        bad = snapshot({"error": "Bearer never-expose-this-value"}); bad["http_status"] = 401
        row = quota.normalize_account(account(), bad, now=NOW)
        self.assertNotEqual(row["status"], "ok")
        self.assertNotIn("never-expose", json.dumps(row))
        bad = snapshot({"success": False, "credential_valid": False, "error": "private"}, "minimax", 5)
        self.assertNotEqual(quota.normalize_account(account("minimax", 5), bad, now=NOW)["status"], "ok")

    def test_mismatched_identity_cannot_replace_another_account(self):
        bad = snapshot(openai_data(), account_id=7)
        self.assertNotEqual(quota.normalize_account(account(), bad, now=NOW)["status"], "ok")

    def test_grok_uses_weekly_billing_timestamp_not_new_page_timestamp(self):
        data = {"updated_at": iso(NOW), "seven_day": {"utilization": 4, "resets_at": iso(NOW + 86400)},
                "grok_billing": {"weekly_status_code": 200, "weekly_updated_at": iso(NOW - 3600)}}
        row = quota.normalize_account(account("grok", 10), snapshot(data, "grok", 10), now=NOW)
        self.assertEqual(row["used_pct"], 4)
        self.assertTrue(row["stale"])

    def test_antigravity_models_provide_weekly_quota(self):
        data = {"updated_at": iso(NOW), "antigravity_quota": {
            "gemini-3.8-flash-high": {"utilization": 8, "reset_time": iso(NOW + 7200)},
            "claude-opus": {"utilization": 99, "reset_time": iso(NOW + 3600)},
            "gemini-3.1-flash-image": {"utilization": 100, "reset_time": iso(NOW + 3600)},
        }}
        row = quota.normalize_account(account("antigravity", 9), snapshot(data, "antigravity", 9), now=NOW)
        self.assertEqual(row["used_pct"], 8)
        self.assertEqual(row["used_text"], "8%")
        self.assertNotIn("fiveh_pct", row)


class ConfigurationAndCollectionTests(unittest.TestCase):
    def config(self):
        return {"version": 1, "ssh_host": "rn-vps", "accounts": [account()], "unmapped_accounts": []}

    def test_missing_config_keeps_existing_collection_available(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(quota.load_config(Path(directory) / "missing.json"))

    def test_valid_config_and_invalid_host_or_account_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.config()))
            self.assertEqual(quota.load_config(path)["ssh_host"], "rn-vps")
            for field, value in (("ssh_host", "-oProxyCommand=bad"), ("accounts", [account(), account()])):
                bad = self.config(); bad[field] = value; path.write_text(json.dumps(bad))
                with self.assertRaises(quota.ConfigError):quota.load_config(path)

    def test_retry_only_failed_accounts_and_preserve_missing_local_account(self):
        cfg = self.config(); second = account(account_id=7); second["name"] = "Codex · second@example.invalid"
        cfg["accounts"].append(second)
        cfg["unmapped_accounts"] = [{"name": "Codex · free@example.invalid", "collector": "codex_auth"}]
        good = snapshot(openai_data()); bad = {"id": 7, "platform": "openai", "http_status": 503}
        fixed = snapshot(openai_data(), account_id=7)
        with mock.patch.object(quota, "fetch_snapshot", side_effect=[{"accounts": [good, bad]}, {"accounts": [fixed]}]) as fetch:
            rows = quota.collect(cfg, retry_attempts=1, retry_delay=0, now=NOW)
        self.assertEqual([r["status"] for r in rows[:2]], ["ok", "ok"])
        self.assertEqual([a["id"] for a in fetch.call_args_list[1].args[1]], [7])
        self.assertIn("未接入", rows[2]["status"])
        self.assertFalse(rows[2]["retryable"])

    def test_transport_failure_has_no_secret_or_local_cli_fallback(self):
        with mock.patch.object(quota, "fetch_snapshot", side_effect=RuntimeError("private-key-value")):
            rows = quota.collect(self.config(), now=NOW)
        self.assertNotEqual(rows[0]["status"], "ok")
        self.assertNotIn("private-key-value", json.dumps(rows))

    def test_duplicate_remote_records_are_not_accepted(self):
        good = snapshot(openai_data())
        with mock.patch.object(quota, "fetch_snapshot", return_value={"accounts": [good, good]}):
            rows = quota.collect(self.config(), now=NOW)
        self.assertNotEqual(rows[0]["status"], "ok")


class MonitorIntegrationTests(unittest.TestCase):
    def test_configured_collectors_do_not_read_local_tokens_or_launch_clis(self):
        specs = [(3, "kimi", "kimi_mine"), (4, "kimi", "kimi_andy"),
                 (5, "minimax", "minimax"), (6, "openai", "codex_auth"),
                 (9, "antigravity", "antigravity"), (10, "grok", "grok")]
        config = {"accounts": [{"id": i, "platform": p, "collector": c, "name": c} for i, p, c in specs]}
        expected = [{"name": "MiniMax", "status": "ok", "source": "sub2api"}]
        with mock.patch.dict(quota_report.os.environ, {"QUOTA_SOURCE": "auto"}), \
             mock.patch.object(quota_report.sub2api_quota, "load_config", return_value=config), \
             mock.patch.object(quota_report.sub2api_quota, "collect", return_value=expected), \
             mock.patch.object(quota_report, "_load_kimi_keys", side_effect=AssertionError("local keys read")), \
             mock.patch.object(quota_report, "_codex_auth_accounts", side_effect=AssertionError("local OAuth queried")), \
             mock.patch.object(quota_report, "_minimax", side_effect=AssertionError("local CLI queried")), \
             mock.patch.object(quota_report, "_grok", side_effect=AssertionError("local CLI queried")), \
             mock.patch.object(quota_report, "_agy", side_effect=AssertionError("local CLI queried")):
            self.assertEqual(quota_report.collect(), expected)

    def test_invalid_configuration_stops_before_writing_history_or_pages(self):
        with mock.patch.object(quota_report.sys, "argv", ["quota_report.py", "--log", "--html"]), \
             mock.patch.object(quota_report, "collect", side_effect=quota.ConfigError("配置无效")), \
             mock.patch.object(quota_report, "append_log") as append, \
             mock.patch.object(quota_report, "render_html") as render:
            self.assertEqual(quota_report.main(), 2)
        append.assert_not_called(); render.assert_not_called()

    def test_missing_weekly_metric_does_not_enter_daily_consumption(self):
        data = {"success": True, "credential_valid": True, "fetched_at": NOW,
                "tiers": [{"window": "5h", "used_percent": 39, "reset_at": iso(NOW + 3600)}]}
        row = quota.normalize_account(account("minimax", 5), snapshot(data, "minimax", 5), now=NOW)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(quota_report, "LOG_CSV", Path(directory) / "history.csv"):
            quota_report.append_log([row])
            quota_report.append_log([row])
            self.assertEqual(quota_report._daily_deltas(), [])

    def test_configuration_error_does_not_leave_live_refresh_stuck(self):
        state = {"rows": [{"name": "previous", "status": "ok"}], "updating": True, "last": "previous-time"}
        with mock.patch.object(quota_report, "_STATE", state), \
             mock.patch.object(quota_report, "collect", side_effect=quota.ConfigError("配置无效")), \
             mock.patch.object(quota_report, "append_log") as append:
            quota_report._refresh_state()
        self.assertFalse(state["updating"])
        self.assertTrue(state["rows"][0]["stale"])
        self.assertTrue(state["rows"][0]["cache_fallback"])
        self.assertEqual(state["last"], "previous-time")
        self.assertEqual(state["rows"][0]["name"], "previous")
        append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
