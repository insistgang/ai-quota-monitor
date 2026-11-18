import datetime as dt
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import agy_quota


NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
BIN = "/example/agy"


def bucket(window, remaining, reset):
    return {"window": window, "remaining_fraction": remaining,
            "reset_time": reset.isoformat().replace("+00:00", "Z")}


WEEK_RESET = NOW + dt.timedelta(days=6)
SHORT_RESET = NOW + dt.timedelta(hours=2)


def groups():
    return [
        {"name": "Gemini Models", "description": "Models within this group: Gemini 4 Argon, Gemini 4 Hydrogen",
         "buckets": [bucket("5h", 0.775, SHORT_RESET), bucket("weekly", 0.96, WEEK_RESET)]},
        {"name": "Claude and GPT models", "description": "Models within this group: Claude Opus 4.6, GPT-OSS 120B",
         "buckets": [bucket("weekly", 1, WEEK_RESET), bucket("5h", 0, SHORT_RESET)]},
    ]


def envelope(quota_groups):
    return {"status": "SUCCESS", "num_turns": 0, "usage": {"total_tokens": 0},
            "conversation_id": "private-session", "response": "private-raw-response",
            "command": {"name": "usage", "data": {"groups": quota_groups}}}


def response(quota_groups):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(envelope(quota_groups)))


class AgQuotaTests(unittest.TestCase):
    def setUp(self):
        which = mock.patch.object(agy_quota.shutil, "which", return_value=BIN)
        self.which = which.start()
        self.addCleanup(which.stop)

    def test_fixed_cli_command_reads_two_shared_pools_with_actual_windows(self):
        with mock.patch.object(agy_quota.subprocess, "run", return_value=response(groups())) as run:
            gemini, claude = agy_quota.collect(retry_attempts=2)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [BIN, "--print", "/usage", "--output-format", "json"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["cwd"], Path.home())
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual((gemini["name"], claude["name"]), (agy_quota.GEMINI_LABEL, agy_quota.CLAUDE_LABEL))
        self.assertEqual((gemini["used_pct"], gemini["fiveh_pct"]), (4.0, 22.5))
        self.assertEqual((claude["used_pct"], claude["fiveh_pct"]), (0.0, 100.0))
        self.assertEqual(gemini["reset"], WEEK_RESET.astimezone().strftime("%m-%d %H:%M"))
        self.assertEqual(gemini["fiveh_reset"], SHORT_RESET.astimezone().strftime("%m-%d %H:%M"))
        self.assertIn("Gemini 4 Argon、Gemini 4 Hydrogen", gemini["note"])
        self.assertIn("Claude Opus 4.6、GPT-OSS 120B", claude["note"])
        self.assertNotIn("private-", json.dumps([gemini, claude]))

    def test_unsupported_or_agent_generated_reply_never_retried_or_stored(self):
        for overrides in ({"num_turns": 1}, {"usage": {"total_tokens": 10}}, {"command": {"name": "status"}}):
            with self.subTest(overrides=overrides):
                stdout = json.dumps({**envelope(groups()), **overrides})
                result = subprocess.CompletedProcess([], 0, stdout=stdout)
                with mock.patch.object(agy_quota.subprocess, "run", return_value=result) as run:
                    rows = agy_quota.collect(retry_attempts=2)
                run.assert_called_once()
                self.assertTrue(all(r["status"] != "ok" and "used_pct" not in r for r in rows))
                self.assertNotIn("private-", json.dumps(rows))

    def test_expired_short_window_keeps_valid_weekly_window(self):
        group = groups()[0]
        group["buckets"][0] = bucket("5h", 0, NOW - dt.timedelta(seconds=1))
        row = agy_quota._group_row(group, agy_quota.GEMINI_LABEL, NOW)
        self.assertEqual(row["used_pct"], 4)
        self.assertNotIn("fiveh_pct", row)
        self.assertIn("5h 窗口暂无有效数据", row["note"])

    def test_bad_weekly_window_is_unknown_not_zero(self):
        for remaining, reset in ((None, WEEK_RESET), (True, WEEK_RESET), (float("nan"), WEEK_RESET),
                                 (-0.5, WEEK_RESET), (1.5, WEEK_RESET), (0.98, WEEK_RESET.replace(tzinfo=None)),
                                 (0.98, NOW - dt.timedelta(seconds=1))):
            with self.subTest(remaining=remaining, reset=reset):
                group = groups()[0]
                group["buckets"][1] = bucket("weekly", remaining, reset)
                row = agy_quota._group_row(group, agy_quota.GEMINI_LABEL, NOW)
                self.assertIsNone(row["used_pct"])
                self.assertEqual(row["fiveh_pct"], 22.5)
                self.assertIn("周窗口暂无有效数据", row["note"])

    def test_ambiguous_duplicate_window_is_not_guessed(self):
        group = groups()[0]
        group["buckets"].append(bucket("weekly", 0.0, WEEK_RESET))
        self.assertIsNone(agy_quota._group_row(group, agy_quota.GEMINI_LABEL, NOW))

    def test_retry_does_not_overwrite_the_first_good_group(self):
        gemini, claude = groups()
        new_gemini = {**gemini, "buckets": [bucket("weekly", 0.11, WEEK_RESET)]}
        with mock.patch.object(agy_quota.subprocess, "run", side_effect=[response([gemini]), response([new_gemini, claude])]) as run:
            rows = agy_quota.collect(retry_attempts=2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual([row["used_pct"] for row in rows], [4.0, 0.0])

    def test_cli_timeout_has_no_upstream_details_or_numeric_quotas(self):
        error = subprocess.TimeoutExpired([BIN], 30, output="private-stdout")
        with mock.patch.object(agy_quota.subprocess, "run", side_effect=error) as run:
            rows = agy_quota.collect(retry_attempts=1)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(r["status"] != "ok" and "used_pct" not in r for r in rows))
        self.assertNotIn("private-", json.dumps(rows))

    def test_no_cli_keeps_sub2api_only_usage_optional(self):
        self.which.return_value = None
        with mock.patch.object(agy_quota.subprocess, "run") as run:
            self.assertEqual(agy_quota.collect(), [])
        run.assert_not_called()

    def test_model_list_never_copies_an_unexpected_description(self):
        group = groups()[0]
        group["description"] = "Models within this group: Gemini 4 Argon, secret@example.invalid"
        row = agy_quota._group_row(group, agy_quota.GEMINI_LABEL, NOW)
        self.assertEqual(row["used_pct"], 4)
        self.assertNotIn("secret", row["note"])


if __name__ == "__main__":
    unittest.main()
