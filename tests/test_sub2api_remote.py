import base64
import hashlib
import hmac
import io
import json
import math
import unittest
from unittest import mock

from scripts import sub2api_quota_snapshot as remote


def encoded_request(accounts):
    payload = json.dumps({"accounts": accounts}, separators=(",", ":")).encode()
    return base64.b64encode(payload).decode()


class RequestValidationTests(unittest.TestCase):
    def test_decodes_allowed_accounts_and_preserves_order(self):
        encoded = encoded_request([
            {"id": 17, "platform": "kimi"},
            {"id": 29, "platform": "openai"},
        ])

        self.assertEqual(
            remote.parse_request_argument(encoded),
            [{"id": 17, "platform": "kimi"}, {"id": 29, "platform": "openai"}],
        )

    def test_rejects_malformed_and_oversized_input(self):
        for invalid in ("not-base64", "", base64.b64encode(b"[]").decode()):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    remote.parse_request_argument(invalid)

        over_limit = encoded_request([
            {"id": number, "platform": "kimi"} for number in range(1, 18)
        ])
        with self.assertRaises(ValueError):
            remote.parse_request_argument(over_limit)

    def test_rejects_duplicate_ids_invalid_platforms_and_extra_fields(self):
        invalid_accounts = [
            [{"id": 1, "platform": "kimi"}, {"id": 1, "platform": "grok"}],
            [{"id": 1, "platform": "deepseek"}],
            [{"id": 1, "platform": []}],
            [{"id": 1, "platform": {"name": "kimi"}}],
            [{"id": True, "platform": "kimi"}],
            [{"id": 0, "platform": "kimi"}],
            [{"id": 1, "platform": "kimi", "access_token": "should-not-be-accepted"}],
        ]
        for accounts in invalid_accounts:
            with self.subTest(accounts=accounts):
                with self.assertRaises(ValueError):
                    remote.parse_request_argument(encoded_request(accounts))


class AuthenticationTests(unittest.TestCase):
    def test_jwt_version_and_signature_match_sub2api_hs256_semantics(self):
        admin = {
            "id": 42,
            "email": " Admin@Example.Invalid ",
            "password_hash": "opaque-hash-for-unit-test",
        }
        secret = "test-only-jwt-secret"

        token = remote.mint_admin_jwt(secret, admin, now=1_900_000_000)
        header_part, claims_part, signature_part = token.split(".")
        unsigned = f"{header_part}.{claims_part}".encode()
        expected_signature = remote._b64url(hmac.new(secret.encode(), unsigned, hashlib.sha256).digest())
        header = json.loads(base64.urlsafe_b64decode(header_part + "=="))
        claims = json.loads(base64.urlsafe_b64decode(claims_part + "=="))
        material = "admin@example.invalid\nopaque-hash-for-unit-test".encode()
        expected_version = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF

        self.assertEqual(signature_part, expected_signature)
        self.assertEqual(header, {"alg": "HS256", "typ": "JWT"})
        self.assertEqual(claims["token_version"], expected_version)
        self.assertEqual(claims["user_id"], 42)
        self.assertEqual(claims["exp"], 1_900_000_180)
        self.assertNotIn(secret, token)
        self.assertNotIn(admin["password_hash"], token)


class RouteAndPayloadTests(unittest.TestCase):
    def test_routes_use_only_approved_get_paths(self):
        self.assertEqual(
            remote.route_for(5, "kimi"),
            "http://127.0.0.1:8080/api/v1/admin/cn-providers/accounts/5/quota",
        )
        self.assertEqual(
            remote.route_for(8, "grok"),
            "http://127.0.0.1:8080/api/v1/admin/accounts/8/usage?force=true",
        )
        self.assertEqual(
            remote.route_for(9, "antigravity"),
            "http://127.0.0.1:8080/api/v1/admin/accounts/9/usage?force=true",
        )
        self.assertEqual(
            remote.route_for(6, "openai"),
            "https://chatgpt.com/backend-api/wham/usage",
        )
        for account_id, platform in ((5, "kimi"), (8, "grok"), (9, "antigravity"), (6, "openai")):
            self.assertNotIn("reset", remote.route_for(account_id, platform).lower())
            self.assertNotIn("responses", remote.route_for(account_id, platform).lower())

    def test_payload_filter_removes_identifiers_secrets_and_non_quota_fields(self):
        openai_data = remote.filter_payload("openai", {
            "email": "person@example.invalid",
            "user_id": "user-secret",
            "account_id": "account-secret",
            "plan_type": "plus",
            "access_token": "never-return-this",
            "rate_limit": {
                "allowed": True,
                "primary_window": {"used_percent": 20, "reset_at": 1_900_000_000, "email": "hidden"},
                "unexpected": "hidden",
            },
        }, captured_at=1_900_000_001)
        encoded = json.dumps(openai_data)

        self.assertEqual(openai_data["plan_type"], "plus")
        self.assertEqual(openai_data["fetched_at"], 1_900_000_001)
        self.assertEqual(openai_data["rate_limit"]["primary_window"]["used_percent"], 20)
        self.assertNotIn("email", encoded)
        self.assertNotIn("access_token", encoded)
        self.assertNotIn("user-secret", encoded)

    def test_payload_filter_drops_nonfinite_numbers_instead_of_coercing_to_zero(self):
        payload = remote.filter_payload("openai", {
            "rate_limit": {
                "allowed": True,
                "primary_window": {
                    "used_percent": math.nan,
                    "limit_window_seconds": math.inf,
                    "reset_after_seconds": -math.inf,
                    "reset_at": math.nan,
                },
                "secondary_window": {"used_percent": 0.0, "reset_at": 1_900_000_000},
            },
        }, captured_at=1_900_000_001)

        serialized = json.dumps(payload, allow_nan=False)
        primary = payload["rate_limit"]["primary_window"]
        self.assertEqual(primary, {})
        self.assertNotIn("used_percent", primary)
        self.assertNotIn("Infinity", serialized)
        self.assertEqual(
            payload["rate_limit"]["secondary_window"]["used_percent"],
            0.0,
            "a genuine zero must remain zero while NaN/Infinity are omitted",
        )

    def test_grok_billing_is_reduced_to_the_approved_summary_fields(self):
        data = remote.filter_payload("grok", {
            "updated_at": "2030-03-17T00:00:00Z",
            "grok_billing": {
                "status_code": 200,
                "weekly_status_code": 200,
                "fetched_at": "2030-03-17T00:00:00Z",
                "weekly_updated_at": "2030-03-17T00:00:00Z",
                "period_type": "weekly",
                "period_end": "2030-03-24T00:00:00Z",
                "usage_percent": 41.0,
                "monthly_limit": 999,
                "access_token": "hidden",
            },
            "grok_quota_snapshot_state": "observed",
        }, captured_at=1_900_000_001)

        self.assertEqual(
            set(data["grok_billing"]),
            {"status_code", "weekly_status_code", "fetched_at", "weekly_updated_at", "period_type", "period_end", "usage_percent"},
        )
        self.assertNotIn("monthly_limit", data["grok_billing"])
        self.assertNotIn("access_token", json.dumps(data))

    def test_http_transport_is_get_only_and_never_returns_error_body(self):
        calls = []

        class FakeOpener:
            def open(self, request, timeout):
                calls.append((request, timeout))
                raise remote.urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "unauthorized",
                    hdrs=None,
                    fp=io.BytesIO(b"provider secret response body"),
                )

        with mock.patch.object(remote.urllib.request, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(remote.RequestFailure) as raised:
                remote.read_json("https://example.invalid/read-only", {"Accept": "application/json"})

        self.assertEqual(raised.exception.code, "http_error")
        self.assertEqual(raised.exception.http_status, 401)
        self.assertNotIn("provider secret", str(raised.exception))
        request, timeout = calls[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 30)


class CollectionSafetyTests(unittest.TestCase):
    def test_platform_mismatch_skips_provider_request(self):
        calls = []

        def fake_sql(query):
            if "FROM accounts WHERE id IN" in query:
                return [{"id": 5, "platform": "deepseek"}]
            raise AssertionError("platform mismatch must stop before admin auth or upstream reads")

        def fake_http(url, headers):
            calls.append((url, headers))
            return 200, {"data": {"success": True}}

        result = remote.collect(
            encoded_request([{"id": 5, "platform": "kimi"}]),
            sql_runner=fake_sql,
            http_reader=fake_http,
            now=lambda: 1_900_000_001,
        )

        self.assertEqual(calls, [])
        self.assertEqual(result["accounts"][0]["error"], "platform_mismatch")

    def test_deleted_account_is_filtered_before_any_upstream_request(self):
        queries = []
        calls = []

        def fake_sql(query):
            queries.append(query)
            return []

        result = remote.collect(
            encoded_request([{"id": 5, "platform": "openai"}]),
            sql_runner=fake_sql,
            http_reader=lambda *args: calls.append(args),
            now=lambda: 1_900_000_001,
        )

        self.assertEqual(calls, [])
        self.assertEqual(result["accounts"][0]["error"], "account_not_found")
        self.assertIn("deleted_at IS NULL", queries[0])

    def test_success_without_parsed_coding_plan_windows_is_not_reported_as_fresh_quota(self):
        def fake_sql(query):
            if "FROM accounts WHERE id IN" in query:
                return [{"id": 5, "platform": "minimax"}]
            if "FROM security_secrets" in query:
                return [{"value": "test-secret"}]
            if "FROM users" in query:
                return [{"id": 42, "email": "admin@example.invalid", "password_hash": "hash"}]
            raise AssertionError("unexpected SQL query")

        result = remote.collect(
            encoded_request([{"id": 5, "platform": "minimax"}]),
            sql_runner=fake_sql,
            http_reader=lambda *_: (200, {"code": 0, "data": {"success": True, "tiers": []}}),
            now=lambda: 1_900_000_001,
        )

        self.assertEqual(result["accounts"][0]["http_status"], 200)
        self.assertEqual(result["accounts"][0]["error"], "no_quota_windows")
        self.assertEqual(result["accounts"][0]["data"], {})

    def test_auth_material_and_upstream_error_text_never_enter_output(self):
        secret = "super-secret-test-jwt-key"
        token = "super-secret-test-provider-token"
        password_hash = "sensitive-password-hash"

        def fake_sql(query):
            if "FROM security_secrets" in query:
                return [{"value": secret}]
            if "FROM users" in query:
                return [{"id": 42, "email": "admin@example.invalid", "password_hash": password_hash}]
            if "FROM accounts WHERE id IN" in query:
                return [{"id": 5, "platform": "kimi"}]
            return []

        def fake_http(url, headers):
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
            raise RuntimeError(f"provider error included {token}")

        result = remote.collect(
            encoded_request([{"id": 5, "platform": "kimi"}]),
            sql_runner=fake_sql,
            http_reader=fake_http,
            now=lambda: 1_900_000_001,
        )
        output = json.dumps(result)

        self.assertEqual(result["accounts"][0]["error"], "request_failed")
        for sensitive_value in (secret, token, password_hash, "admin@example.invalid"):
            self.assertNotIn(sensitive_value, output)

    def test_openai_uses_only_database_access_token_and_allowlisted_payload(self):
        provider_token = "sensitive-provider-access-token"
        chatgpt_account_id = "private-account-id"
        sql_queries = []
        requests = []

        def fake_sql(query):
            sql_queries.append(query)
            if "FROM accounts WHERE id IN" in query:
                self.assertIn("deleted_at IS NULL", query)
                return [{"id": 6, "platform": "openai"}]
            if "credentials->>'access_token'" in query:
                self.assertIn("deleted_at IS NULL", query)
                return [{"access_token": provider_token, "chatgpt_account_id": chatgpt_account_id}]
            raise AssertionError("OpenAI direct reads must not mint an admin JWT")

        def fake_http(url, headers):
            requests.append((url, headers))
            return 200, {
                "plan_type": "plus",
                "email": "private@example.invalid",
                "account_id": "also-private",
                "rate_limit": {"allowed": True, "primary_window": {"used_percent": 20}},
            }

        result = remote.collect(
            encoded_request([{"id": 6, "platform": "openai"}]),
            sql_runner=fake_sql,
            http_reader=fake_http,
            now=lambda: 1_900_000_001,
        )
        output = json.dumps(result)

        self.assertEqual(requests[0][0], "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(requests[0][1]["Authorization"], "Bearer " + provider_token)
        self.assertTrue(all(query.lstrip().startswith("SELECT") for query in sql_queries))
        self.assertEqual(result["accounts"][0]["data"]["plan_type"], "plus")
        for sensitive_value in (provider_token, chatgpt_account_id, "private@example.invalid", "also-private"):
            self.assertNotIn(sensitive_value, output)

    def test_sql_rejects_mutations_before_invoking_subprocess(self):
        with mock.patch.object(remote.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                remote.sql("UPDATE accounts SET status='disabled';")

        run.assert_not_called()

    def test_sql_connection_is_read_only_and_sql_stays_on_stdin(self):
        fake_process = mock.Mock(stdout='{"ok":true}\n')
        with mock.patch.object(remote.subprocess, "run", return_value=fake_process) as run:
            self.assertEqual(remote.sql("SELECT 1;"), [{"ok": True}])

        command = run.call_args.args[0]
        self.assertIn("PGOPTIONS=-c default_transaction_read_only=on", command)
        self.assertNotIn("SELECT 1;", command)
        self.assertEqual(run.call_args.kwargs["input"], "SELECT 1;")

    def test_sql_failure_is_a_safe_per_account_error(self):
        def fake_sql(_query):
            raise RuntimeError("database failure contains private detail")

        result = remote.collect(
            encoded_request([{"id": 5, "platform": "kimi"}]),
            sql_runner=fake_sql,
            http_reader=lambda *_: self.fail("no HTTP call after SQL failure"),
            now=lambda: 1_900_000_001,
        )

        self.assertEqual(result["accounts"][0]["error"], "database_error")
        self.assertNotIn("private detail", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
