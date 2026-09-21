from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.checkin_service import CheckinService
from core.github_reporter import GitHubReporter
from core.privacy_handler import PrivacyHandler
from notif import NotificationKit
from tests.tools.data_builders import build_account_result, build_notification_data

# 2026-08 线上实际出现的上游错误：AnyRouter 服务端数据库被锁写
MYSQL_LOCK_ERROR = (
	'Error 1290 (HY000): The MySQL server is running with '
	'the LOCK_WRITE_GROWTH option so it cannot execute this statement'
)


class TestUpstreamFaultDetection:
	"""签到响应中的上游服务故障识别"""

	@pytest.mark.parametrize(
		'error_msg',
		[
			MYSQL_LOCK_ERROR,
			'Error 1290 (HY000): The MySQL server is running with the --read-only option',
			'Error 1040 (HY000): Too many connections',
			'502 Bad Gateway',
			'503 Service Unavailable',
		],
	)
	def test_server_side_errors_are_upstream_faults(self, error_msg: str):
		fault = CheckinService()._detect_upstream_fault(error_msg)

		assert fault is not None
		assert fault.reason == 'upstream_database_error'
		assert fault.message == error_msg

	@pytest.mark.parametrize(
		'error_msg',
		[
			'未登录或登录已过期',
			'今日已签到',
			'无效的用户令牌',
			'未知错误',
		],
	)
	def test_account_level_errors_are_not_upstream_faults(self, error_msg: str):
		assert CheckinService()._detect_upstream_fault(error_msg) is None


class TestCheckinRetry:
	"""签到请求与 WAF cookies 的瞬时故障重试"""

	@pytest.mark.asyncio
	async def test_checkin_retries_after_timeout_then_succeeds(self):
		service = CheckinService()
		expected = MagicMock(status_code=200)
		client = MagicMock()
		client.post = AsyncMock(side_effect=[httpx.ReadTimeout('timeout'), expected])

		with patch('asyncio.sleep', new=AsyncMock()) as sleep_mock:
			response = await service._post_checkin_with_retry(
				client=client,
				headers={},
				account_name='测试账号',
			)

		assert response is expected
		assert client.post.await_count == 2
		sleep_mock.assert_awaited_once_with(CheckinService.Config.Retry.DELAY_SECONDS)

	@pytest.mark.asyncio
	async def test_checkin_raises_after_exhausting_attempts(self):
		service = CheckinService()
		max_attempts = CheckinService.Config.Retry.CHECKIN_MAX_ATTEMPTS
		client = MagicMock()
		client.post = AsyncMock(side_effect=httpx.ReadTimeout('timeout'))

		with patch('asyncio.sleep', new=AsyncMock()) as sleep_mock:
			with pytest.raises(httpx.ReadTimeout):
				await service._post_checkin_with_retry(
					client=client,
					headers={},
					account_name='测试账号',
				)

		assert client.post.await_count == max_attempts
		assert sleep_mock.await_count == max_attempts - 1

	@pytest.mark.asyncio
	async def test_checkin_does_not_retry_non_timeout_errors(self):
		service = CheckinService()
		client = MagicMock()
		client.post = AsyncMock(side_effect=httpx.ConnectError('connection refused'))

		with pytest.raises(httpx.ConnectError):
			await service._post_checkin_with_retry(
				client=client,
				headers={},
				account_name='测试账号',
			)

		assert client.post.await_count == 1, '非超时错误不应重试'

	@pytest.mark.asyncio
	async def test_waf_cookies_retry_after_playwright_timeout(self):
		service = CheckinService()
		cookies = {name: 'value' for name in CheckinService.Config.WAF.COOKIE_NAMES}

		fetch_once = AsyncMock(side_effect=[None, cookies])
		with patch.object(service, '_fetch_waf_cookies_once', new=fetch_once):
			with patch('asyncio.sleep', new=AsyncMock()) as sleep_mock:
				result = await service._get_waf_cookies_with_playwright('测试账号')

		assert result == cookies
		assert fetch_once.await_count == 2
		sleep_mock.assert_awaited_once_with(CheckinService.Config.Retry.DELAY_SECONDS)

	@pytest.mark.asyncio
	async def test_waf_cookies_return_none_after_exhausting_attempts(self):
		service = CheckinService()
		max_attempts = CheckinService.Config.Retry.WAF_MAX_ATTEMPTS

		fetch_once = AsyncMock(return_value=None)
		with patch.object(service, '_fetch_waf_cookies_once', new=fetch_once):
			with patch('asyncio.sleep', new=AsyncMock()):
				result = await service._get_waf_cookies_with_playwright('测试账号')

		assert result is None
		assert fetch_once.await_count == max_attempts


class TestUpstreamFaultReporting:
	"""上游服务故障在通知模板与 Actions summary 中的呈现"""

	def test_upstream_fault_accounts_are_not_counted_as_failed(self, clean_notification_env: None):
		kit = NotificationKit()
		data = build_notification_data(
			[
				build_account_result(name='账号 A', status='upstream_fault', error=MYSQL_LOCK_ERROR),
				build_account_result(name='账号 B', status='failed', error='未登录或登录已过期'),
			],
			upstream_fault_message=MYSQL_LOCK_ERROR,
		)

		context = kit._build_context_data(data)

		assert [acc.name for acc in context['upstream_fault_accounts']] == ['账号 A']
		assert [acc.name for acc in context['failed_accounts']] == ['账号 B']
		assert context['has_upstream_fault'] is True
		assert data.stats.upstream_fault_count == 1
		assert data.stats.failed_count == 1

	def test_all_success_is_false_when_only_upstream_faults(self, clean_notification_env: None):
		kit = NotificationKit()
		data = build_notification_data(
			[build_account_result(name='账号 A', status='upstream_fault', error=MYSQL_LOCK_ERROR)],
			upstream_fault_message=MYSQL_LOCK_ERROR,
		)

		context = kit._build_context_data(data)

		assert context['all_success'] is False, '上游故障不能被当成全部成功'
		assert context['all_failed'] is False, '上游故障也不是账号全部失败'
		assert context['has_failed'] is False

	def test_default_telegram_template_renders_upstream_fault_section(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
	):
		monkeypatch.setenv('TELEGRAM_NOTIF_CONFIG', '{"bot_token": "test_token", "chat_id": "123456"}')
		kit = NotificationKit()
		assert kit.telegram_config is not None
		assert kit.telegram_config.template is not None

		data = build_notification_data(
			[
				build_account_result(name='账号 A', status='upstream_fault', error=MYSQL_LOCK_ERROR),
				build_account_result(name='账号 B', status='upstream_fault', error=MYSQL_LOCK_ERROR),
			],
			upstream_fault_message=MYSQL_LOCK_ERROR,
		)
		context = kit._build_context_data(data)

		rendered_title, rendered_content = kit._render_template(kit.telegram_config.template, context)

		assert rendered_title == '🚧 AnyRouter 上游异常'
		assert '<b>✅ 签到结果：</b>0/2' in rendered_content
		assert f'<b>🚧 上游故障：</b>{MYSQL_LOCK_ERROR}' in rendered_content
		assert '<b>📡 影响范围：</b>2/2' in rendered_content
		assert '账号 A' not in rendered_content
		assert '账号 B' not in rendered_content
		assert '<b>❌' not in rendered_content, '上游故障不应出现在失败账号区块'

	def test_summary_reports_upstream_fault_instead_of_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
		summary_file = tmp_path / 'summary.md'
		monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary_file))
		reporter = GitHubReporter(PrivacyHandler(show_sensitive_info=False))

		reporter.generate_summary(
			success_count=0,
			total_count=2,
			account_results=[
				build_account_result(name='账号 A', status='upstream_fault', error=MYSQL_LOCK_ERROR),
				build_account_result(name='账号 B', status='upstream_fault', error=MYSQL_LOCK_ERROR),
			],
			notify_sent=True,
			notify_triggers=['failed'],
			notify_reasons=['未出现失败账号'],
			upstream_fault_message=MYSQL_LOCK_ERROR,
		)

		content = summary_file.read_text(encoding='utf-8')

		assert '**🚧 上游服务故障，本次未能签到（非账号问题）**' in content
		assert '- **失败比例**：0/2' in content
		assert '- **上游故障**：2/2' in content
		assert MYSQL_LOCK_ERROR in content
		assert '不代表账号凭据失效' in content
		assert '### 失败账号' not in content
