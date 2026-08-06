# Upstream Fault Classification Design

## Context

On 2026-08-04 and 2026-08-05 the scheduled `AnyRouter 自动签到` workflow failed twice in a row with `成功 0/9，失败 9/9`. Every account reached the sign-in endpoint successfully, received `HTTP 200`, and then failed on the response body:

```
Error 1290 (HY000): The MySQL server is running with the LOCK_WRITE_GROWTH
option so it cannot execute this statement
```

This is AnyRouter's own database refusing writes — a hosted-MySQL storage lock, unrelated to account credentials. The existing code path treated it as an ordinary sign-in business error: nine account-level failures, a nine-row failure notification, and exit code `1`.

The 2026-05-28 infrastructure preflight already separates *pre-sign-in* outages (DNS, login-page reachability) from account failures. It does not help here, because the login page was reachable and the fault only surfaced inside the sign-in response body, after the account loop had started.

The same two runs also contained two isolated transient failures — one `httpcore.ReadTimeout` on the sign-in `POST`, and one `Page.goto: Timeout 30000ms exceeded` while collecting WAF cookies. Neither the sign-in request nor the WAF-cookie step had any retry.

## Goals

- Classify server-side faults surfaced *inside* a `HTTP 200` sign-in response as upstream faults rather than account failures.
- Classify `5xx` responses from the sign-in endpoint as upstream faults.
- Keep upstream faults out of the failure count and out of the `failed` notification trigger.
- Keep the workflow exit code green when nothing but upstream faults prevented sign-in.
- Still report the outage exactly once, so a silent all-accounts-blocked run is impossible.
- Retry transient timeouts on the sign-in request and on WAF cookie collection.
- Preserve existing behavior for genuine account-level failures such as expired cookies, HTTP 401/403, and invalid account config.

## Non-Goals

- Changing the 2026-05-28 infrastructure preflight or its exit code `1` behavior.
- Suppressing repeated notifications across runs. One concise message per run during an outage is accepted; cross-run deduplication would need persisted state.
- Retrying business errors, credential errors, or non-timeout transport errors.
- Treating a persistent sign-in timeout as an upstream fault. After retries are exhausted it remains an account-level failure.

## Proposed Approach

Add a third channel to the result of `CheckinService.check_in_account()`. It returns `(success, user_info, upstream_fault)`, where `upstream_fault` is `None` unless the sign-in response proves the failure came from AnyRouter's own backend.

`Application.run()` counts those accounts separately from failures. They do not set `has_any_failed`, do not enter `NotificationStats.failed_count`, and do not push the exit code to `1`. They are surfaced as a distinct `upstream_fault` account status that notification templates and the Actions summary render in their own section.

## Components

### Upstream Fault Model

A frozen dataclass nested in `CheckinService`, mirroring `InfrastructureCheckResult`:

- `reason`: stable machine-readable reason — `upstream_database_error` or `upstream_server_error`.
- `message`: the raw upstream error text, used by logs, notification, and summary.

Kept local to the check-in flow. No cross-project error framework.

### Detection

Two independent checks inside `check_in_account()`:

1. **Status code.** `response.status_code >= 500` on the sign-in endpoint yields `upstream_server_error`. This runs before the existing `!= 200` branch, so `401`/`403`/`404` still fall through to account-level failure.
2. **Response body.** `_detect_upstream_fault()` lowercases the `msg`/`message` field and matches it against `Config.Upstream.ERROR_MARKERS`, yielding `upstream_database_error`.

The marker list is a conservative allowlist of server-side database and gateway signatures — `lock_write_growth`, `hy000`, `mysql server`, `read-only`, `read only`, `database is locked`, `too many connections`, `deadlock found`, `bad gateway`, `service unavailable`, `gateway timeout`.

The default is deliberately biased toward *not* classifying: any error that does not match stays an account failure. A missed upstream fault produces a noisy but correct alert; a false upstream classification would silently hide expired credentials.

### Retry Policy

`Config.Retry` holds all three values so tests can neutralize the delay:

- `CHECKIN_MAX_ATTEMPTS = 3` — sign-in `POST`, retried only on `httpx.TimeoutException`. Other transport errors propagate on the first attempt.
- `WAF_MAX_ATTEMPTS = 2` — WAF cookie collection, retried on any failed attempt. Lower than the sign-in count because each attempt relaunches a browser.
- `DELAY_SECONDS = 5` — fixed delay between attempts, deliberately shorter than the preflight's 60 seconds because this runs per account inside the loop.

`_post_checkin_with_retry()` re-raises the last timeout after exhausting attempts, leaving the existing outer handler to record an account failure. `_get_waf_cookies_with_playwright()` wraps the single-attempt `_fetch_waf_cookies_once()` and returns `None` after exhausting attempts, preserving its original contract.

### Notification

`AccountResult.status` gains `upstream_fault`. `NotificationStats` gains `upstream_fault_count`. `NotificationData` gains `upstream_fault_message`.

`NotificationKit._build_context_data()` exposes `upstream_fault_accounts`, `has_upstream_fault`, and `upstream_fault_message`, and excludes upstream-fault accounts from `failed_accounts`. `all_success` and `all_failed` both require `upstream_fault_count == 0`, so an outage is never rendered as "all succeeded" or "all accounts failed".

All eight default platform templates gain a `🚧 上游服务故障（非账号问题）` block and a `🚧 上游故障：N/M` statistics line, both gated on `has_upstream_fault`. The Telegram title gains a matching branch, ordered after `has_failed` so a real account failure still wins the headline.

Because upstream faults no longer trip the `failed` trigger, a pure outage usually leaves `should_notify()` false and sends nothing. `Application._notify_upstream_fault()` covers that gap: when the templated notification is skipped and an upstream fault occurred, it pushes one raw message naming the service, reason, affected count, and the fact that credentials are not implicated. When the templated notification *does* fire, the raw message is suppressed to avoid double-reporting — the template already carries the same information.

### GitHub Actions Summary

`generate_summary()` takes an optional `upstream_fault_message` and partitions accounts three ways. It adds an `- **上游故障**：N/M` detail row, a dedicated `### 🚧 上游服务故障` section listing the affected accounts and the raw error, and a status headline `**🚧 上游服务故障，本次未能签到（非账号问题）**` for the case where nothing succeeded and nothing genuinely failed.

### Exit Code

```
has_real_failure = (total_count - success_count - upstream_fault_count) > 0
exit(0 if success_count > 0 or not has_real_failure else 1)
```

The first clause preserves the previous rule that any success is a green run. The second clause is the new behavior: zero successes still exits `0` when every unsuccessful account was blocked upstream. A run that mixes upstream faults with genuine failures still exits `1`.

## Data Flow

1. Infrastructure preflight passes; the account loop starts (unchanged).
2. For each account, WAF cookies are collected with up to two attempts.
3. The sign-in `POST` runs with up to three attempts on timeout.
4. The response is classified: success, upstream fault (`5xx` or marked body), or account failure.
5. `Application` accumulates `success_count`, `has_any_failed`, `upstream_fault_count`, and the first `UpstreamFault` seen.
6. Notification triggers are evaluated with upstream faults excluded from `has_failed`.
7. Either the templated notification fires (carrying the upstream section) or, if it does not and an upstream fault occurred, one raw upstream message is pushed.
8. The Actions summary is written with the upstream partition.
9. The exit code ignores upstream faults.

## Error Handling

| Sign-in outcome | Classification |
| :--- | :--- |
| `2xx` with success payload | Success |
| `2xx` with body matching an upstream marker | `upstream_database_error` |
| `2xx` with any other business error | Account failure |
| `5xx` | `upstream_server_error` |
| `401` / `403` / other non-`2xx` | Account failure |
| Timeout, all attempts exhausted | Account failure |
| Non-timeout transport error | Account failure |
| WAF cookies unavailable after retries | Account failure |

## Testing

`tests/unit/test_upstream_fault.py` covers marker detection against the real production error string and against account-level errors that must not match; sign-in retry success, exhaustion, and non-retry of non-timeout errors; WAF retry success and exhaustion; template context partitioning; default Telegram template rendering; and summary output.

`tests/integration/test_upstream_fault_flow.py` covers a full run where every account hits the MySQL lock (exit `0`, no per-account notification, one raw upstream message, summary statuses all `upstream_fault`), a `503` run, and a mixed run where a genuine failure coexists with an upstream fault (exit `1`, templated notification, no raw message).

An autouse fixture in `tests/conftest.py` sets `Config.Retry.DELAY_SECONDS` to `0` so retry paths run without sleeping.

`tests/integration/test_checkin_flow.py` was updated: it previously used `HTTP 500` to simulate an account failure, which is now an upstream fault. It uses a `200` response with an account-level business error instead.

## Acceptance Criteria

- A `LOCK_WRITE_GROWTH` outage no longer produces a nine-account failure notification or a red workflow run.
- The outage is still reported exactly once per run, naming the upstream cause and stating that credentials are not implicated.
- An expired-cookie account still fails, still notifies, and still exits `1`.
- A single transient sign-in timeout or WAF-cookie timeout no longer fails that account.
- Unrecognized sign-in errors continue to be treated as account failures.
