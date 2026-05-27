# Infrastructure Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a domain/login-page preflight with three 60-second retries so DNS or AnyRouter login-page outages are reported as infrastructure failures instead of account failures.

**Architecture:** Add a small structured preflight result and retry helper to `CheckinService`, call it once before account processing in `Application`, and add dedicated raw notification plus GitHub Actions summary output for infrastructure failures. Existing account-level sign-in, notification templates, and account failure behavior remain unchanged when preflight succeeds.

**Tech Stack:** Python 3.11, httpx, pytest, pytest-asyncio, unittest.mock, existing `NotificationKit`, existing `GitHubReporter`.

---

## File Structure

- Modify `src/core/checkin_service.py`
  - Add `InfrastructureCheckResult`.
  - Add preflight methods: `check_infrastructure()`, `_check_login_page_once()`, and `_classify_infrastructure_error()`.
  - Keep account sign-in behavior unchanged.
- Modify `src/application.py`
  - Run preflight after account loading and balance hash loading, before the account loop.
  - Add an infrastructure failure branch that skips account processing, sends raw notification, writes summary, and exits `1`.
- Modify `src/core/github_reporter.py`
  - Add `generate_infrastructure_summary()` for infrastructure failures.
- Modify `src/notif/notification_kit.py`
  - Make `push_raw_message()` return whether at least one handler sent successfully.
- Add `tests/unit/test_infrastructure_preflight.py`
  - Unit coverage for retry, classification, and reachable login-page responses.
- Modify `tests/integration/test_error_handling.py`
  - Integration coverage for infrastructure failure branch and HTTP 401 separation.

---

### Task 1: Add Infrastructure Preflight Model and Unit Tests

**Files:**
- Modify: `src/core/checkin_service.py`
- Create: `tests/unit/test_infrastructure_preflight.py`

- [ ] **Step 1: Write failing tests for preflight classification and retry**

Create `tests/unit/test_infrastructure_preflight.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.checkin_service import CheckinService


def build_response(status: int):
	response = MagicMock()
	response.status_code = status
	return response


@pytest.mark.asyncio
async def test_preflight_success_on_reachable_status_without_retry():
	service = CheckinService()

	with patch.object(service, '_check_login_page_once', new=AsyncMock(return_value=None)) as check_once:
		check_once.return_value = service.InfrastructureCheckResult(
			available=True,
			reason='available',
			message='AnyRouter login page is reachable',
			attempts=1,
		)

		result = await service.check_infrastructure(max_attempts=3, delay_seconds=0)

	assert result.available is True
	assert result.attempts == 1
	assert check_once.await_count == 1


@pytest.mark.asyncio
async def test_preflight_retries_once_then_succeeds():
	service = CheckinService()
	first = service.InfrastructureCheckResult(
		available=False,
		reason='dns_resolution_failed',
		message='DNS resolution failed for https://anyrouter.top/login',
		attempts=1,
	)
	second = service.InfrastructureCheckResult(
		available=True,
		reason='available',
		message='AnyRouter login page is reachable',
		attempts=2,
	)

	with patch.object(service, '_check_login_page_once', new=AsyncMock(side_effect=[first, second])) as check_once:
		with patch('asyncio.sleep', new=AsyncMock()) as sleep_mock:
			result = await service.check_infrastructure(max_attempts=3, delay_seconds=60)

	assert result.available is True
	assert result.attempts == 2
	assert check_once.await_count == 2
	sleep_mock.assert_awaited_once_with(60)


@pytest.mark.asyncio
async def test_preflight_exhausts_retries_for_dns_failure():
	service = CheckinService()
	failures = [
		service.InfrastructureCheckResult(
			available=False,
			reason='dns_resolution_failed',
			message='DNS resolution failed for https://anyrouter.top/login',
			attempts=attempt,
		)
		for attempt in (1, 2, 3)
	]

	with patch.object(service, '_check_login_page_once', new=AsyncMock(side_effect=failures)) as check_once:
		with patch('asyncio.sleep', new=AsyncMock()) as sleep_mock:
			result = await service.check_infrastructure(max_attempts=3, delay_seconds=60)

	assert result.available is False
	assert result.reason == 'dns_resolution_failed'
	assert result.attempts == 3
	assert check_once.await_count == 3
	assert sleep_mock.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 302, 401, 403, 404])
async def test_login_page_reachable_statuses_are_available(status):
	service = CheckinService()

	async def get_handler(*args, **kwargs):
		return build_response(status)

	mock_client = MagicMock()
	mock_client.get = get_handler
	mock_client.__aenter__ = AsyncMock(return_value=mock_client)
	mock_client.__aexit__ = AsyncMock(return_value=None)

	with patch('httpx.AsyncClient', return_value=mock_client):
		result = await service._check_login_page_once(attempt=1)

	assert result.available is True
	assert result.reason == 'available'
	assert result.attempts == 1


@pytest.mark.asyncio
async def test_login_page_5xx_is_infrastructure_failure():
	service = CheckinService()

	async def get_handler(*args, **kwargs):
		return build_response(503)

	mock_client = MagicMock()
	mock_client.get = get_handler
	mock_client.__aenter__ = AsyncMock(return_value=mock_client)
	mock_client.__aexit__ = AsyncMock(return_value=None)

	with patch('httpx.AsyncClient', return_value=mock_client):
		result = await service._check_login_page_once(attempt=1)

	assert result.available is False
	assert result.reason == 'server_error'
	assert 'HTTP 503' in result.message


@pytest.mark.asyncio
async def test_dns_connect_error_is_classified():
	service = CheckinService()

	async def get_handler(*args, **kwargs):
		raise httpx.ConnectError('[Errno 8] nodename nor servname provided, or not known')

	mock_client = MagicMock()
	mock_client.get = get_handler
	mock_client.__aenter__ = AsyncMock(return_value=mock_client)
	mock_client.__aexit__ = AsyncMock(return_value=None)

	with patch('httpx.AsyncClient', return_value=mock_client):
		result = await service._check_login_page_once(attempt=1)

	assert result.available is False
	assert result.reason == 'dns_resolution_failed'
	assert result.attempts == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
mise exec -- pytest tests/unit/test_infrastructure_preflight.py -q
```

Expected: FAIL because `CheckinService.InfrastructureCheckResult`, `check_infrastructure()`, and `_check_login_page_once()` do not exist.

- [ ] **Step 3: Implement minimal preflight model and retry methods**

Modify `src/core/checkin_service.py`:

```python
import asyncio
from dataclasses import dataclass
```

Inside `class CheckinService`, before `async def check_in_account(...)`, add:

```python
	@dataclass(frozen=True)
	class InfrastructureCheckResult:
		available: bool
		reason: str
		message: str
		attempts: int
		url: str = 'https://anyrouter.top/login'

	async def check_infrastructure(
		self,
		max_attempts: int = 3,
		delay_seconds: int = 60,
	) -> InfrastructureCheckResult:
		"""Check AnyRouter login-page reachability before account processing."""
		last_result = self.InfrastructureCheckResult(
			available=False,
			reason='not_checked',
			message='AnyRouter infrastructure has not been checked',
			attempts=0,
			url=self.Config.URLs.LOGIN,
		)

		for attempt in range(1, max_attempts + 1):
			last_result = await self._check_login_page_once(attempt=attempt)
			if last_result.available:
				return last_result

			if attempt < max_attempts:
				logger.warning(
					f'基础设施预检失败（{last_result.message}），'
					f'{delay_seconds} 秒后重试 {attempt + 1}/{max_attempts}',
					tag='基础设施',
				)
				await asyncio.sleep(delay_seconds)

		return last_result

	async def _check_login_page_once(self, attempt: int) -> InfrastructureCheckResult:
		"""Run one AnyRouter login-page availability probe."""
		try:
			async with httpx.AsyncClient(http2=True, timeout=30.0, follow_redirects=False) as client:
				response = await client.get(
					url=self.Config.URLs.LOGIN,
					headers={'User-Agent': ' '.join(self.Config.Browser.USER_AGENT_PARTS)},
				)

			if response.status_code >= 500:
				return self.InfrastructureCheckResult(
					available=False,
					reason='server_error',
					message=f'AnyRouter login page returned HTTP {response.status_code}',
					attempts=attempt,
					url=self.Config.URLs.LOGIN,
				)

			return self.InfrastructureCheckResult(
				available=True,
				reason='available',
				message='AnyRouter login page is reachable',
				attempts=attempt,
				url=self.Config.URLs.LOGIN,
			)

		except Exception as exc:
			reason, message = self._classify_infrastructure_error(exc)
			return self.InfrastructureCheckResult(
				available=False,
				reason=reason,
				message=message,
				attempts=attempt,
				url=self.Config.URLs.LOGIN,
			)

	def _classify_infrastructure_error(self, exc: Exception) -> tuple[str, str]:
		"""Classify login-page reachability exceptions for notifications and summaries."""
		error_text = str(exc)
		lowered = error_text.lower()

		dns_markers = (
			'err_name_not_resolved',
			'name or service not known',
			'nodename nor servname',
			'temporary failure in name resolution',
			'getaddrinfo failed',
			'name does not resolve',
		)
		if isinstance(exc, httpx.ConnectError) and any(marker in lowered for marker in dns_markers):
			return 'dns_resolution_failed', f'DNS resolution failed for {self.Config.URLs.LOGIN}: {error_text}'

		if isinstance(exc, httpx.TimeoutException):
			return 'timeout', f'Timed out reaching {self.Config.URLs.LOGIN}: {error_text}'

		if isinstance(exc, httpx.ConnectError):
			return 'connection_failed', f'Connection failed for {self.Config.URLs.LOGIN}: {error_text}'

		return 'unknown_infrastructure_error', f'Infrastructure check failed for {self.Config.URLs.LOGIN}: {error_text}'
```

- [ ] **Step 4: Run unit tests to verify preflight passes**

Run:

```bash
mise exec -- pytest tests/unit/test_infrastructure_preflight.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/core/checkin_service.py tests/unit/test_infrastructure_preflight.py
git commit -m "feat: 添加签到基础设施预检"
```

---

### Task 2: Add Infrastructure Failure Summary and Raw Notification Result

**Files:**
- Modify: `src/core/github_reporter.py`
- Modify: `src/notif/notification_kit.py`
- Test: `tests/unit/test_notification_kit.py`

- [ ] **Step 1: Write failing tests for raw notification return value**

Append to `tests/unit/test_notification_kit.py`:

```python
@pytest.mark.asyncio
async def test_push_raw_message_returns_true_when_handler_sends():
	kit = NotificationKit()
	handler = NotificationHandler(
		name='Test',
		config=object(),
		send_func=AsyncMock(),
	)
	kit._handlers = [handler]

	result = await kit.push_raw_message('Title', 'Content')

	assert result is True
	handler.send_func.assert_awaited_once()


@pytest.mark.asyncio
async def test_push_raw_message_returns_false_without_handlers():
	kit = NotificationKit()
	kit._handlers = []

	result = await kit.push_raw_message('Title', 'Content')

	assert result is False
```

If `NotificationHandler` is not imported in the file, add:

```python
from notif.models import NotificationHandler
```

- [ ] **Step 2: Run the raw notification tests to verify they fail**

Run:

```bash
mise exec -- pytest tests/unit/test_notification_kit.py::test_push_raw_message_returns_true_when_handler_sends tests/unit/test_notification_kit.py::test_push_raw_message_returns_false_without_handlers -q
```

Expected: FAIL because `push_raw_message()` currently returns `None`.

- [ ] **Step 3: Implement raw notification return value**

Modify `src/notif/notification_kit.py`:

```python
	async def push_raw_message(self, title: str, content: str) -> bool:
		"""
		直接发送预格式化消息，不经过模板渲染

		Returns:
			是否至少有一个通知处理器发送成功
		"""
		if not self._handlers:
			return False

		sent = False
		for handler in self._handlers:
			if handler.is_available():
				try:
					await handler.send_func(
						title=title,
						content=content,
						context_data=None,
					)
					sent = True
					logger.success(f'{handler.name} 消息发送成功！')
				except Exception as e:
					logger.error(
						message=f'{handler.name} 消息发送失败：{e}',
						exc_info=True,
					)

		return sent
```

- [ ] **Step 4: Add GitHub summary method**

Modify `src/core/github_reporter.py` by adding this method after `generate_summary()`:

```python
	def generate_infrastructure_summary(
		self,
		reason: str,
		message: str,
		attempts: int,
		url: str,
		notify_sent: bool,
	):
		"""Generate a GitHub Actions summary for infrastructure failures."""
		summary_file = os.getenv(self.ENV_GITHUB_STEP_SUMMARY)
		if not summary_file:
			logger.debug('未检测到 GitHub Actions 环境，跳过基础设施 summary 生成', tag='Summary')
			return

		try:
			lines = [
				'## 🚧 AnyRouter 基础设施故障',
				'',
				'**❌ 未执行账号签到**',
				'',
				'### **详细信息**',
				f'- **执行时间**：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
				f'- **检查地址**：`{url}`',
				f'- **故障类型**：`{reason}`',
				f'- **故障原因**：{message}',
				f'- **重试次数**：{attempts}',
				f'- **通知结果**：{"已发送" if notify_sent else "已跳过"}',
				'',
				'> 这是域名解析、网络连接或登录页可用性问题，不代表账号凭据全部失效。',
			]

			with open(summary_file, 'a', encoding='utf-8') as f:
				f.write('\n'.join(lines))
				f.write('\n')

			logger.info('GitHub Actions 基础设施故障 Summary 生成成功', tag='Summary')
		except Exception as e:
			logger.warning(f'生成基础设施故障 Summary 失败：{e}', tag='Summary')
```

- [ ] **Step 5: Run notification tests**

Run:

```bash
mise exec -- pytest tests/unit/test_notification_kit.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/notif/notification_kit.py src/core/github_reporter.py tests/unit/test_notification_kit.py
git commit -m "feat: 添加基础设施故障通知摘要"
```

---

### Task 3: Wire Preflight Into Application Flow

**Files:**
- Modify: `src/application.py`
- Modify: `tests/integration/test_error_handling.py`

- [ ] **Step 1: Write failing integration test for infrastructure failure branch**

Append to `tests/integration/test_error_handling.py`:

```python
@pytest.mark.asyncio
async def test_infrastructure_failure_skips_account_processing(accounts_env, tmp_path):
	accounts_env(SINGLE_ACCOUNT)
	app = Application()
	app.balance_manager.balance_hash_file = tmp_path / 'hash_infra.txt'

	infrastructure_result = app.checkin_service.InfrastructureCheckResult(
		available=False,
		reason='dns_resolution_failed',
		message='DNS resolution failed for https://anyrouter.top/login',
		attempts=3,
		url='https://anyrouter.top/login',
	)

	with patch.object(app.checkin_service, 'check_infrastructure', new=AsyncMock(return_value=infrastructure_result)):
		with patch.object(app.checkin_service, 'check_in_account', new=AsyncMock()) as check_account:
			with patch.object(app.notification_kit, 'push_raw_message', new=AsyncMock(return_value=True)) as push_raw:
				with patch.object(app.github_reporter, 'generate_infrastructure_summary') as summary:
					with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
						with pytest.raises(SystemExit) as exc_info:
							await app.run()

	assert exc_info.value.code == 1
	check_account.assert_not_awaited()
	push_raw.assert_awaited_once()
	title, content = push_raw.await_args.args
	assert 'AnyRouter 基础设施故障' in title
	assert '未执行账号签到' in content
	assert 'dns_resolution_failed' in content
	summary.assert_called_once_with(
		reason='dns_resolution_failed',
		message='DNS resolution failed for https://anyrouter.top/login',
		attempts=3,
		url='https://anyrouter.top/login',
		notify_sent=True,
	)
```

- [ ] **Step 2: Write separation test for HTTP 401**

Append to `tests/integration/test_error_handling.py`:

```python
@pytest.mark.asyncio
async def test_account_401_still_runs_account_flow(accounts_env, tmp_path):
	accounts_env([{'name': '401 账号', 'cookies': {'session': '401'}, 'api_user': 'user_401'}])
	app = Application()
	app.balance_manager.balance_hash_file = tmp_path / 'hash_401_separation.txt'

	preflight_result = app.checkin_service.InfrastructureCheckResult(
		available=True,
		reason='available',
		message='AnyRouter login page is reachable',
		attempts=1,
		url='https://anyrouter.top/login',
	)

	with patch.object(app.checkin_service, 'check_infrastructure', new=AsyncMock(return_value=preflight_result)):
		with patch.object(app.notification_kit, 'push_raw_message', new=AsyncMock()) as push_raw:
			with ExitStack() as stack:
				MockPlaywright.setup_success(stack)

				async def get_handler(*args, **kwargs):
					return MockHttpClient.build_response(status=401)

				MockHttpClient.setup(stack, get_handler, MockHttpClient.post_success_handler)

				with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
					with pytest.raises(SystemExit) as exc_info:
						await app.run()

	assert exc_info.value.code == 1
	push_raw.assert_not_awaited()
```

- [ ] **Step 3: Run new integration tests to verify they fail**

Run:

```bash
mise exec -- pytest tests/integration/test_error_handling.py::TestErrorHandling::test_infrastructure_failure_skips_account_processing tests/integration/test_error_handling.py::TestErrorHandling::test_account_401_still_runs_account_flow -q
```

Expected: FAIL because `Application.run()` does not call `check_infrastructure()` or handle infrastructure results.

- [ ] **Step 4: Implement infrastructure branch in `Application.run()`**

In `src/application.py`, after loading `last_balance_hash_dict` and before the account loop, add:

```python
		infrastructure_result = await self.checkin_service.check_infrastructure()
		if not infrastructure_result.available:
			logger.error(
				message=f'基础设施故障：{infrastructure_result.message}',
				tag='基础设施',
			)
			title = 'AnyRouter 基础设施故障'
			content = (
				f'⏰ 执行时间\n'
				f'{datetime.now(ZoneInfo(self.DEFAULT_TIMEZONE)).strftime(self.DEFAULT_TIMESTAMP_FORMAT)}\n\n'
				f'🚧 基础设施故障\n'
				f'服务：anyrouter.top\n'
				f'地址：{infrastructure_result.url}\n'
				f'类型：{infrastructure_result.reason}\n'
				f'原因：{infrastructure_result.message}\n'
				f'重试：{infrastructure_result.attempts}/3，每次间隔 60 秒\n\n'
				f'本次未执行账号签到，不代表账号凭据全部失效。'
			)
			notify_sent = await self.notification_kit.push_raw_message(title=title, content=content)
			self.github_reporter.generate_infrastructure_summary(
				reason=infrastructure_result.reason,
				message=infrastructure_result.message,
				attempts=infrastructure_result.attempts,
				url=infrastructure_result.url,
				notify_sent=notify_sent,
			)
			sys.exit(1)
```

- [ ] **Step 5: Run integration tests**

Run:

```bash
mise exec -- pytest tests/integration/test_error_handling.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/application.py tests/integration/test_error_handling.py
git commit -m "feat: 区分基础设施故障签到结果"
```

---

### Task 4: Full Verification and GitHub Actions Validation

**Files:**
- Verify: `src/core/checkin_service.py`
- Verify: `src/application.py`
- Verify: `.github/workflows/checkin.yml`

- [ ] **Step 1: Run targeted tests**

Run:

```bash
mise exec -- pytest tests/unit/test_infrastructure_preflight.py tests/integration/test_error_handling.py tests/unit/test_notification_kit.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
mise exec -- pytest tests/ -q
```

Expected: PASS.

- [ ] **Step 3: Run lint and format checks**

Run:

```bash
mise exec -- python -m ruff format --check --preview
mise exec -- python -m ruff check
```

Expected: both commands PASS.

- [ ] **Step 4: Run type check**

Run:

```bash
mise exec -- pyright
```

Expected: PASS with zero errors.

- [ ] **Step 5: Commit final verification fixes if verification changed files**

If verification requires any small fixes:

```bash
git status --short
git add src/core/checkin_service.py src/application.py src/core/github_reporter.py src/notif/notification_kit.py tests/unit/test_infrastructure_preflight.py tests/unit/test_notification_kit.py tests/integration/test_error_handling.py
git commit -m "fix: 完善基础设施重试验证"
```

If `git status --short` shows none of those files changed after verification, skip this commit.

- [ ] **Step 6: Push branch**

Run:

```bash
git push
```

Expected: branch pushes successfully.

- [ ] **Step 7: Manually trigger GitHub Actions check-in workflow**

Run:

```bash
gh workflow run checkin.yml --ref main
gh run list --workflow checkin.yml --limit 1 --json databaseId,status,conclusion,url
```

Expected: new workflow run is created.

- [ ] **Step 8: Watch GitHub Actions result**

Run:

```bash
gh run watch RUN_DATABASE_ID --exit-status
```

Replace `RUN_DATABASE_ID` with the `databaseId` returned by the immediately preceding `gh run list` command.

Expected: workflow completes. If AnyRouter is reachable, result should be normal account-level success. If AnyRouter DNS fails, notification and summary should identify infrastructure failure rather than `0/N` account failures.

- [ ] **Step 9: Collect final artifact evidence**

Record:

- GitHub Actions run URL.
- Final account summary or infrastructure-failure summary.
- Relevant test command outputs.
- Commit hashes included in the push.

Use this evidence in the final user update.
