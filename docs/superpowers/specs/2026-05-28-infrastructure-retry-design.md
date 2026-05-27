# Infrastructure Retry Design

## Context

The scheduled `AnyRouter 自动签到` workflow can fail when GitHub Actions cannot resolve or reach `https://anyrouter.top/login`. The observed failure was `Page.goto: net::ERR_NAME_NOT_RESOLVED`, which happened before account authentication and before API requests. The current implementation runs the WAF-cookie step once per account, so a single infrastructure outage is reported as every account failing.

This design separates infrastructure availability from account-level sign-in results. A domain or login-page outage should not be represented as `0/N` account failures.

## Goals

- Detect `anyrouter.top` DNS or login-page availability failures before account sign-in starts.
- Retry transient infrastructure failures three times with a 60 second delay between attempts.
- Stop account processing when infrastructure remains unavailable after retries.
- Report the result as an infrastructure failure, not as per-account failures.
- Preserve existing behavior for account-level failures such as HTTP 401, invalid account config, or sign-in API errors.
- Keep GitHub Actions failing when infrastructure is unavailable so the scheduled job remains observable.

## Non-Goals

- Changing account credential formats or secret loading.
- Retrying individual account authentication failures.
- Changing the notification trigger model for ordinary account failures.
- Introducing external monitoring or a new service dependency.

## Proposed Approach

Add a preflight check before the account loop in `Application.run()`.

The preflight check verifies that the AnyRouter login page is reachable. It should use a lightweight HTTP-level probe or a small service helper that does not require account credentials. If the probe fails with a DNS resolution error, connection error, timeout, or other login-page reachability issue, it retries up to three total attempts. Between failed attempts it waits 60 seconds.

If any attempt succeeds, the normal account loop proceeds unchanged.

If all attempts fail, the application creates an infrastructure-failure outcome, sends a dedicated infrastructure notification, writes an Actions summary, and exits with code `1`. It does not call `check_in_account()` for any account and does not create account-level failed results.

## Components

### Infrastructure Error Model

Introduce a small structured result for preflight:

- `available`: whether the login page is reachable.
- `reason`: stable machine-readable reason, such as `dns_resolution_failed`, `timeout`, or `connection_failed`.
- `message`: concise human-readable message for logs and notification.
- `attempts`: number of attempts used.

Keep this structure local to the check-in flow for this change. Do not introduce a cross-project error framework.

### Preflight Service

Add a method responsible for checking login-page availability. It should:

- Target `CheckinService.Config.URLs.LOGIN`.
- Avoid using account cookies or `new-api-user`.
- Classify DNS resolution failures separately when possible.
- Return the structured preflight result instead of raising through the application layer.

### Retry Policy

Use a fixed retry policy:

- Maximum attempts: `3`.
- Delay between attempts: `60` seconds.
- Retry only infrastructure reachability failures.
- Do not retry account-level HTTP 401 or API-level sign-in failures.

Tests may inject a zero-delay retry policy or patched sleep so the suite remains fast.

### Notification

When preflight fails after all retries, send one infrastructure-failure notification. The notification should not include account success/failure counts. It should include:

- The affected service: `anyrouter.top`.
- The failed URL or domain.
- The failure reason.
- Retry count and delay.
- A clear statement that account sign-in was not attempted.

Existing account-level Telegram templates should remain unchanged for normal account results. The infrastructure message can be emitted through the existing notification kit with a small dedicated path, or through a focused helper if that is cleaner.

### GitHub Actions Summary

The Actions summary should show an infrastructure-failure section instead of an account table:

- Status: infrastructure failure.
- Reason and message.
- Attempts used.
- Whether a notification was sent.

The workflow should fail with exit code `1` after the summary and notification are written.

## Data Flow

1. Load account configuration.
2. Load balance hash state.
3. Run AnyRouter preflight with three attempts and 60 second delays.
4. If preflight succeeds, continue the existing account loop.
5. If preflight fails:
   - Skip account loop.
   - Send infrastructure-failure notification.
   - Generate infrastructure-failure GitHub Actions summary.
   - Exit with code `1`.
6. Preserve existing account processing, balance tracking, notification trigger evaluation, and exit-code logic when preflight succeeds.

## Error Handling

DNS failures such as `ERR_NAME_NOT_RESOLVED`, `httpx.ConnectError`, and socket resolution errors should be classified as infrastructure failures. Timeouts and connection failures to the login page should also be infrastructure failures.

HTTP responses from the login page are classified by reachability:

- `2xx`, `3xx`, `401`, `403`, and `404` prove that DNS and network reachability are working, so preflight succeeds and account processing continues.
- `5xx` from the login page is treated as infrastructure failure and is retried.
- Account API errors after the account loop starts remain account-level results.

## Testing

Add focused tests for:

- Preflight succeeds on the first attempt and account processing continues.
- Preflight fails once then succeeds; account processing continues after one retry.
- Preflight fails three times; account processing is skipped, notification is sent, summary is generated, and exit code is `1`.
- HTTP 401 from account API remains account failure and is not infrastructure failure.
- Existing all-success flow remains unchanged.

Retry delay should be patched or injected in tests to avoid waiting.

## Acceptance Criteria

- A DNS/login-page outage no longer produces Telegram content that says every account failed.
- The workflow retries the infrastructure check three times with 60 second delays in production behavior.
- If all retries fail, Telegram reports infrastructure failure and says account sign-in was not attempted.
- If preflight later succeeds, the existing account sign-in flow runs normally.
- Tests cover retry success, retry exhaustion, and account-level failure separation.
