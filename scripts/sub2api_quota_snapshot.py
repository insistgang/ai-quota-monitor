#!/usr/bin/env python3
"""Read bounded quota snapshots from the deployed Sub2API instance.

This helper sends GET requests only. Sub2API may persist its normal quota
snapshots or maintain its own OAuth credentials while servicing those reads;
the helper never implements credential refresh, model-generation, or reset calls.
"""

import base64
import hashlib
import hmac
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


ALLOWED_PLATFORMS = {"openai", "kimi", "minimax", "grok", "antigravity"}
MAX_ACCOUNTS = 16
MAX_BIGINT = (1 << 63) - 1
MAX_INPUT_BYTES = 8192
MAX_RESPONSE_BYTES = 1 << 20
MAX_WORKERS = 4
SQL_TIMEOUT_SECONDS = 10
HTTP_TIMEOUT_SECONDS = 30
SUB2API_BASE_URL = "http://127.0.0.1:8080"
OPENAI_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

_JWT_SECRET_SQL = (
    "SELECT json_build_object('value', value) "
    "FROM security_secrets WHERE key='jwt_secret';"
)
_ADMIN_SQL = (
    "SELECT json_build_object('id', id, 'email', email, 'password_hash', password_hash) "
    "FROM users WHERE role='admin' AND status='active' AND deleted_at IS NULL "
    "ORDER BY id LIMIT 1;"
)


class RequestFailure(Exception):
    """A safe, output-ready error code with no upstream body or URL details."""

    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def parse_request_argument(argument):
    if not isinstance(argument, str) or not argument or len(argument) > MAX_INPUT_BYTES:
        raise ValueError("invalid_input")
    try:
        padded = argument + ("=" * (-len(argument) % 4))
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        request = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid_input") from None

    if not isinstance(request, dict) or set(request) != {"accounts"}:
        raise ValueError("invalid_input")
    accounts = request["accounts"]
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= MAX_ACCOUNTS:
        raise ValueError("invalid_input")

    normalized = []
    seen_ids = set()
    for account in accounts:
        if not isinstance(account, dict) or set(account) != {"id", "platform"}:
            raise ValueError("invalid_input")
        account_id = account["id"]
        platform = account["platform"]
        if type(account_id) is not int or not 1 <= account_id <= MAX_BIGINT:
            raise ValueError("invalid_input")
        if not isinstance(platform, str) or platform not in ALLOWED_PLATFORMS:
            raise ValueError("invalid_input")
        if account_id in seen_ids:
            raise ValueError("invalid_input")
        seen_ids.add(account_id)
        normalized.append({"id": account_id, "platform": platform})
    return normalized


def resolved_token_version(admin):
    """Match Sub2API's resolvedTokenVersion for a DB-loaded user."""
    email = str(admin.get("email", "")).strip().lower()
    password_hash = admin.get("password_hash")
    if not email or not isinstance(password_hash, str) or not password_hash:
        raise ValueError("admin_auth_unavailable")
    material = (email + "\n" + password_hash).encode("utf-8")
    fingerprint = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & MAX_BIGINT
    legacy_version = admin.get("token_version", 0)
    if type(legacy_version) is not int:
        raise ValueError("admin_auth_unavailable")
    return legacy_version ^ fingerprint


def mint_admin_jwt(secret, admin, now=None):
    """Mint a short-lived admin JWT in memory; never return its inputs."""
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("admin_auth_unavailable")
    admin_id = admin.get("id")
    email = admin.get("email")
    if type(admin_id) is not int or admin_id <= 0 or not isinstance(email, str) or not email.strip():
        raise ValueError("admin_auth_unavailable")
    issued_at = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "user_id": admin_id,
        "email": email,
        "role": "admin",
        "token_version": resolved_token_version(admin),
        "exp": issued_at + 180,
        "iat": issued_at,
        "nbf": issued_at,
    }
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    unsigned = encoded_header + "." + encoded_claims
    signature = hmac.new(secret.encode("utf-8"), unsigned.encode("ascii"), hashlib.sha256).digest()
    return unsigned + "." + _b64url(signature)


def route_for(account_id, platform):
    # Keep OpenAI on the direct read-only usage endpoint. The generic OpenAI
    # account-usage route can run a model probe and the admin quota handler can
    # notify auto-reset. Grok's generic usage route is billing-only; Sub2API may
    # update its own cached snapshot or maintain its own OAuth token there.
    # The helper itself performs no such writes or refreshes.
    if platform == "openai":
        return OPENAI_USAGE_URL
    if platform in ("kimi", "minimax"):
        return f"{SUB2API_BASE_URL}/api/v1/admin/cn-providers/accounts/{account_id}/quota"
    if platform in ("grok", "antigravity"):
        return f"{SUB2API_BASE_URL}/api/v1/admin/accounts/{account_id}/usage?force=true"
    raise ValueError("invalid_input")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def read_json(url, headers):
    """Make one GET with verified TLS, no proxy, no redirect, and bounded body."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RequestFailure("response_too_large", response.status)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RequestFailure("invalid_json", response.status) from None
            if not isinstance(payload, dict):
                raise RequestFailure("invalid_json", response.status)
            return response.status, payload
    except urllib.error.HTTPError as error:
        # Do not read, log, or return the response body: it can contain upstream details.
        raise RequestFailure("http_error", error.code) from None
    except RequestFailure:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RequestFailure("request_failed") from None


def sql(query):
    """Run a read-only SQL query through the local Sub2API database container."""
    if not isinstance(query, str) or not query.lstrip().upper().startswith("SELECT"):
        raise ValueError("read_only_query_required")
    process = subprocess.run(
        [
            "docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on",
            "-i", "sub2api-db", "sh", "-c",
            'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAt -v ON_ERROR_STOP=1',
        ],
        input=query,
        capture_output=True,
        text=True,
        check=True,
        timeout=SQL_TIMEOUT_SECONDS,
    )
    try:
        return [json.loads(line) for line in process.stdout.splitlines() if line]
    except json.JSONDecodeError:
        raise RuntimeError("database_error") from None


def _error_result(account, code, http_status=None):
    return {
        "id": account["id"],
        "platform": account["platform"],
        "http_status": http_status,
        "data": {},
        "error": code,
    }


def _safe_number(value):
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def _filter_window(value):
    if not isinstance(value, dict):
        return None
    allowed = ("used_percent", "limit_window_seconds", "reset_after_seconds", "reset_at")
    return {key: value[key] for key in allowed if key in value and _safe_number(value[key])}


def _filter_rate_limit(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("allowed", "limit_reached"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    for key in ("primary_window", "secondary_window"):
        window = _filter_window(value.get(key))
        if window is not None:
            result[key] = window
    return result


def _filter_progress(value):
    if not isinstance(value, dict):
        return None
    result = {}
    if _safe_number(value.get("utilization")):
        result["utilization"] = value["utilization"]
    reset = value.get("resets_at")
    if isinstance(reset, str):
        result["resets_at"] = reset
    remaining = value.get("remaining_seconds")
    if type(remaining) is int:
        result["remaining_seconds"] = remaining
    return result


def _filter_antigravity_quota(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for model, quota in value.items():
        if not isinstance(model, str) or not isinstance(quota, dict):
            continue
        entry = {}
        if _safe_number(quota.get("utilization")):
            entry["utilization"] = quota["utilization"]
        reset = quota.get("reset_time")
        if isinstance(reset, str):
            entry["reset_time"] = reset
        if entry:
            result[model] = entry
    return result


def _filter_grok_billing(value):
    if not isinstance(value, dict):
        return None
    allowed = (
        "status_code", "weekly_status_code", "fetched_at", "weekly_updated_at",
        "period_type", "period_end", "usage_percent",
    )
    result = {}
    for key in allowed:
        item = value.get(key)
        if key in ("status_code", "weekly_status_code") and type(item) is int:
            result[key] = item
        elif key in ("fetched_at", "weekly_updated_at", "period_type", "period_end") and isinstance(item, str):
            result[key] = item
        elif key == "usage_percent" and _safe_number(item):
            result[key] = item
    return result


def filter_payload(platform, payload, captured_at):
    """Project provider payloads onto the collector's non-secret quota contract."""
    if not isinstance(payload, dict):
        return {}
    if platform == "openai":
        result = {}
        if isinstance(payload.get("plan_type"), str):
            result["plan_type"] = payload["plan_type"]
        rate_limit = _filter_rate_limit(payload.get("rate_limit"))
        if rate_limit is not None:
            result["rate_limit"] = rate_limit
        # The upstream /wham/usage payload has no fetched_at field. Stamp the read.
        result["fetched_at"] = int(captured_at)
        return result

    if platform in ("kimi", "minimax"):
        result = {}
        for key in ("provider", "source"):
            if isinstance(payload.get(key), str):
                result[key] = payload[key]
        for key in ("success", "credential_valid"):
            if isinstance(payload.get(key), bool):
                result[key] = payload[key]
        if isinstance(payload.get("tiers"), list):
            tiers = []
            for tier in payload["tiers"]:
                if not isinstance(tier, dict):
                    continue
                item = {}
                if isinstance(tier.get("window"), str):
                    item["window"] = tier["window"]
                if _safe_number(tier.get("used_percent")):
                    item["used_percent"] = tier["used_percent"]
                if isinstance(tier.get("reset_at"), str):
                    item["reset_at"] = tier["reset_at"]
                if item:
                    tiers.append(item)
            result["tiers"] = tiers
        for key in ("fetched_at", "status_code"):
            if type(payload.get(key)) is int:
                result[key] = payload[key]
        return result

    result = {}
    for key in ("five_hour", "seven_day", "thirty_day"):
        progress = _filter_progress(payload.get(key))
        if progress is not None:
            result[key] = progress
    if isinstance(payload.get("updated_at"), str):
        result["updated_at"] = payload["updated_at"]
    if isinstance(payload.get("error_code"), str):
        result["error_code"] = payload["error_code"]
    antigravity_quota = _filter_antigravity_quota(payload.get("antigravity_quota"))
    if antigravity_quota is not None:
        result["antigravity_quota"] = antigravity_quota
    grok_billing = _filter_grok_billing(payload.get("grok_billing"))
    if grok_billing is not None:
        result["grok_billing"] = grok_billing
    snapshot_state = payload.get("grok_quota_snapshot_state")
    if isinstance(snapshot_state, str):
        result["grok_quota_snapshot_state"] = snapshot_state
    return result


def _lookup_accounts(accounts, sql_runner):
    ids = ",".join(str(account["id"]) for account in accounts)
    query = (
        "SELECT json_build_object('id', id, 'platform', platform) "
        f"FROM accounts WHERE id IN ({ids}) AND deleted_at IS NULL ORDER BY id;"
    )
    rows = sql_runner(query)
    found = {}
    for row in rows:
        if isinstance(row, dict) and type(row.get("id")) is int and isinstance(row.get("platform"), str):
            found[row["id"]] = row["platform"]
    return found


def _load_admin_jwt(sql_runner, now):
    secret_rows = sql_runner(_JWT_SECRET_SQL)
    admin_rows = sql_runner(_ADMIN_SQL)
    if not secret_rows or not admin_rows:
        raise ValueError("admin_auth_unavailable")
    secret = secret_rows[0].get("value") if isinstance(secret_rows[0], dict) else None
    admin = admin_rows[0] if isinstance(admin_rows[0], dict) else None
    if admin is None:
        raise ValueError("admin_auth_unavailable")
    return mint_admin_jwt(secret, admin, now=now)


def _load_openai_credentials(account_id, sql_runner):
    query = (
        "SELECT json_build_object('access_token', credentials->>'access_token', "
        "'chatgpt_account_id', credentials->>'chatgpt_account_id') "
        f"FROM accounts WHERE id={account_id} AND platform='openai' "
        "AND deleted_at IS NULL LIMIT 1;"
    )
    rows = sql_runner(query)
    if not rows or not isinstance(rows[0], dict):
        raise ValueError("missing_credential")
    credentials = rows[0]
    token = credentials.get("access_token")
    chatgpt_account_id = credentials.get("chatgpt_account_id")
    if not isinstance(token, str) or not token.strip() or not isinstance(chatgpt_account_id, str) or not chatgpt_account_id.strip():
        raise ValueError("missing_credential")
    return token, chatgpt_account_id


def _request_headers(account, admin_token, sql_runner):
    if account["platform"] == "openai":
        access_token, chatgpt_account_id = _load_openai_credentials(account["id"], sql_runner)
        return {
            "Authorization": "Bearer " + access_token,
            "ChatGPT-Account-Id": chatgpt_account_id,
            "User-Agent": "codex_cli_rs/0.134.0",
            "OpenAI-Beta": "codex-1",
            "originator": "Codex Desktop",
            "Accept": "application/json",
        }
    return {"Authorization": "Bearer " + admin_token, "Accept": "application/json"}


def _fetch_account(account, admin_token, sql_runner, http_reader, captured_at):
    try:
        headers = _request_headers(account, admin_token, sql_runner)
        status, body = http_reader(route_for(account["id"], account["platform"]), headers)
        if type(status) is not int:
            raise RequestFailure("request_failed")
        if account["platform"] != "openai":
            if not isinstance(body, dict) or body.get("code", 0) != 0:
                return _error_result(account, "api_error", status)
            body = body.get("data") or {}
        if not isinstance(body, dict):
            return _error_result(account, "invalid_json", status)
        data = filter_payload(account["platform"], body, captured_at)
        if account["platform"] == "openai" and "error" in body:
            return _error_result(account, "api_error", status)
        if account["platform"] == "openai" and not data.get("rate_limit"):
            return _error_result(account, "quota_unavailable", status)
        if account["platform"] in ("kimi", "minimax") and body.get("success") is True and not data.get("tiers"):
            return _error_result(account, "no_quota_windows", status)
        return {
            "id": account["id"],
            "platform": account["platform"],
            "http_status": status,
            "data": data,
        }
    except RequestFailure as error:
        return _error_result(account, error.code, error.http_status)
    except ValueError as error:
        code = str(error)
        if code not in {"missing_credential", "admin_auth_unavailable"}:
            code = "database_error"
        return _error_result(account, code)
    except Exception:
        # Never serialize exception strings: subprocess/upstream errors may carry secrets.
        return _error_result(account, "request_failed")


def collect(argument, *, sql_runner=sql, http_reader=read_json, now=time.time):
    accounts = parse_request_argument(argument)
    captured_at = int(now())
    results = {}
    try:
        account_platforms = _lookup_accounts(accounts, sql_runner)
    except Exception:
        account_platforms = None

    if account_platforms is None:
        return {
            "version": 1,
            "captured_at": captured_at,
            "accounts": [_error_result(account, "database_error") for account in accounts],
        }

    valid_accounts = []
    for account in accounts:
        actual_platform = account_platforms.get(account["id"])
        if actual_platform is None:
            results[account["id"]] = _error_result(account, "account_not_found")
        elif actual_platform != account["platform"]:
            results[account["id"]] = _error_result(account, "platform_mismatch")
        else:
            valid_accounts.append(account)

    admin_accounts = [account for account in valid_accounts if account["platform"] != "openai"]
    admin_token = None
    if admin_accounts:
        try:
            admin_token = _load_admin_jwt(sql_runner, captured_at)
        except Exception:
            for account in admin_accounts:
                results[account["id"]] = _error_result(account, "admin_auth_unavailable")
            valid_accounts = [account for account in valid_accounts if account["platform"] == "openai"]

    if valid_accounts:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(valid_accounts))) as executor:
            futures = {
                executor.submit(_fetch_account, account, admin_token, sql_runner, http_reader, captured_at): account
                for account in valid_accounts
            }
            for future in as_completed(futures):
                account = futures[future]
                try:
                    results[account["id"]] = future.result()
                except Exception:
                    results[account["id"]] = _error_result(account, "request_failed")

    return {
        "version": 1,
        "captured_at": captured_at,
        "accounts": [results[account["id"]] for account in accounts],
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 1:
            raise ValueError("invalid_input")
        output = collect(argv[0])
    except ValueError:
        output = {"version": 1, "captured_at": int(time.time()), "accounts": [], "error": "invalid_input"}
    except Exception:
        output = {"version": 1, "captured_at": int(time.time()), "accounts": [], "error": "internal_error"}
    print(json.dumps(output, separators=(",", ":")))
    return 0 if "error" not in output else 2


if __name__ == "__main__":
    raise SystemExit(main())
