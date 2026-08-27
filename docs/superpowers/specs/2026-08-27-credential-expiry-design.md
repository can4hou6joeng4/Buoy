# Credential Expiry Reporting Design

## Context

On 2026-08-26 the scheduled `AnyRouter 自动签到` workflow started failing with `成功 0/9，失败 9/9`. Every account collected all three WAF cookies successfully and then failed on `/api/user/self`:

```
获取用户信息失败：HTTP 401
```

The nine per-account secrets were last updated on 2026-07-27 (04:08 and 05:48 UTC). The last green run was 2026-08-26T01:03 UTC; the first red run was 2026-08-26T06:40 UTC. The 30-day mark of the stored sessions falls inside that window, and `README.md` already records the same lifetime: *"遇到 401 错误时请重新获取 cookies，理论 1 个月失效"*. AnyRouter runs new-api, whose auth middleware answers `HTTP 401` only when the session cookie is missing or no longer valid — the WAF answers with `HTTP 200` and a JS challenge page instead, so a 401 is unambiguously a credential problem.

Two reporting defects made that run harder to act on than it needed to be:

1. The failure text was the bare transport detail `获取用户信息失败：HTTP 401`. It states what the server returned, not what the operator must do. The 2026-08-05 upstream-fault work deliberately left HTTP 401/403 as an ordinary account failure, which is correct for classification but leaves the message unhelpful.
2. Nine accounts produced nine identical failure rows in one notification. The repetition hides the only fact that matters: *every* account failed the same way at the same time, which points at session lifetime or a server-side session reset rather than at any individual account.

The 2026-08-27 credential auto-refresh work (`username`/`password` → `/api/user/login`) removes this class of outage for accounts that carry login credentials. Accounts configured with cookies only still expire, so the reporting path still has to be legible.

## Goals

- Replace bare HTTP status text with an error that names the fault and the next action.
- Keep the server's own wording when it supplied one, and append the action to it.
- Give credential failures a distinguishable account status without removing them from the failure count.
- Collapse an all-accounts credential failure into a single notification that says it is systemic.
- Keep the aggregated notification subject to `NOTIFY_TRIGGERS`.
- Report credential expiry in the Actions Summary with the same next step.

## Non-Goals

- Changing which responses count as authentication failures. `_classify_user_info_failure` and the 401/403 branch already decide this, and the auto-refresh path depends on that classification being stable.
- Changing the exit code. Credential expiry is a real failure that needs a human, so it keeps exit code `1` — unlike an upstream fault.
- Changing the credential auto-refresh flow. Refresh runs first; this design only covers what is reported when refresh is impossible or fails.
- Rewriting the eight platform notification templates. The aggregated case bypasses templates entirely; the per-account case improves because the message it renders improved.

## Proposed Approach

Treat credential expiry as a *sub-kind* of failure rather than a sibling of `upstream_fault`. It stays inside `failed_count`, `failed_accounts`, and the non-zero exit code; it only gains its own status string, its own message, and — when it is unanimous — its own notification.

## Components

### Action Hint

`Config.Authentication.ACTION_HINT` holds the one sentence every credential failure ends with, and `Config.Authentication.FAILURE_REASONS` lists the reasons (`authentication_failed`, `credential_refresh_failed`) that count as credential failures. Both live next to the existing `ERROR_MARKERS` so the vocabulary stays in one place.

`CheckinService._build_credential_error(detail)` wraps a trigger detail into `账号凭据已失效（{detail}）：{ACTION_HINT}`. `detail` is `HTTP 401`/`HTTP 403` for the status-code branch and the server's own message for the marker branch, so no server wording is lost.

Non-authentication API errors are left untouched — dressing an unrelated business error as credential expiry would send the operator to re-fetch cookies for nothing.

### Account Status

`AccountResult.status` gains `credential_expired`. Every existing consumer groups failures as `status not in ('success', 'upstream_fault')`, so the new status lands in the failure group everywhere without touching those predicates. `Application` derives it from the account's `user_info['reason']`.

### Aggregation

`Application` counts credential failures during the account loop and keeps the first message as the representative detail. Aggregation fires only when:

- `total_count >= MIN_ACCOUNTS_FOR_CREDENTIAL_AGGREGATION` (2), and
- `success_count == 0`, and
- `upstream_fault_count == 0`, and
- `credential_fault_count == total_count`.

A single expired account is not aggregated: there is no repetition to collapse and no evidence of a systemic cause. Any success, any upstream fault, or any other kind of failure in the same run also disables aggregation, because then the per-account breakdown carries information the summary line would drop.

### Notification

When aggregation fires, `_notify_credential_fault` sends one `AnyRouter 账号凭据集体失效` message through `push_raw_message` *instead of* the template notification, mirroring `_notify_upstream_fault`. It names the affected ratio, the representative detail, why simultaneous failure implies a systemic cause, and both remedies (re-fetch the cookie, or add `username`/`password` for auto-refresh).

The aggregated path sits inside the existing `if need_notify` branch, so `NOTIFY_TRIGGERS=never` still suppresses it. Aggregation changes the shape of a notification, not the decision to send one.

Templates additionally receive `credential_expired_accounts`, `has_credential_expired`, and `all_credential_expired` for the non-aggregated cases, following the existing pattern of pre-grouped lists (Stencil cannot compare strings).

### GitHub Actions Summary

`generate_summary` accepts `credential_fault_message`. When every account expired, the headline becomes `**🔑 所有账号凭据已失效，需要更新 cookie**`; otherwise the existing headline logic is untouched. A `### 🔑 账号凭据失效` section lists the affected accounts and the remedy, and the redacted failure table shows `🔑 凭据失效` for those rows. The failure ratio still counts them.

## Data Flow

1. `_get_user_info` returns `{success: False, error: <actionable text>, reason: 'authentication_failed'}`.
2. `check_in_account` attempts auto-refresh when the account has `username`/`password`; otherwise it returns the failure unchanged.
3. `Application` marks the account `credential_expired`, increments `credential_fault_count`, and records the first message.
4. After the loop, `all_credentials_expired` decides between the aggregated notification and the per-account template notification.
5. `generate_summary` renders the credential section regardless of which notification was sent.

## Error Handling

- `credential_refresh_failed` is included in `FAILURE_REASONS`: an account whose refresh attempt failed is still an expired account, and if every account fails that way the run is still systemic.
- A missing `error` value falls back to `ACTION_HINT`, so the notification never degrades to an empty reason.
- Aggregation never suppresses the Actions Summary; a run is never silent about which accounts failed.

## Testing

- `tests/unit/test_credential_fault.py` — message construction for 401/403 and for server-supplied auth messages, non-auth errors left alone, notification-context grouping, and both Summary headline paths.
- `tests/integration/test_credential_fault_flow.py` — all-expired sends exactly one raw notification and no template notification, partial expiry keeps the template notification, a single expired account is not aggregated, and `NOTIFY_TRIGGERS=never` suppresses the aggregate.
- `tests/integration/test_error_handling.py::test_account_401_still_runs_account_flow` continues to pin single-account 401 to the ordinary failure path.

## Acceptance Criteria

- A 401 from `/api/user/self` produces an error naming both the cause and the remedy.
- Credential failures remain inside `failed_count` and keep exit code `1`.
- Nine simultaneously expired accounts produce one notification, not nine rows.
- One expired account among many still produces the per-account notification.
- `NOTIFY_TRIGGERS=never` sends nothing in either case.
