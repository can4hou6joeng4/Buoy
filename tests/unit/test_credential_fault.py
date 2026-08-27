from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.checkin_service import CheckinService
from core.github_reporter import GitHubReporter
from core.privacy_handler import PrivacyHandler
from notif import NotificationKit
from tests.fixtures.mock_dependencies import MockHttpClient
from tests.tools.data_builders import build_account_result, build_notification_data

# 2026-08-26 线上实际出现的失效：9 个账号的 session 满 30 天后同时被服务端拒绝
ACTION_HINT = CheckinService.Config.Authentication.ACTION_HINT


class TestCredentialErrorMessage:
	"""凭据失效错误信息的语义化"""

	@pytest.mark.asyncio
	@pytest.mark.parametrize('status', [401, 403])
	async def test_auth_status_codes_report_action_instead_of_bare_http_code(self, status: int):
		"""裸 `HTTP 401` 无法指导处置，错误信息必须写明要换 cookie。"""
		service = CheckinService()
		client = MagicMock()
		client.get = AsyncMock(return_value=MockHttpClient.build_response(status=status))

		user_info = await service._get_user_info(
			client=client,
			headers={},
			privacy_handler=PrivacyHandler(show_sensitive_info=False),
		)

		assert user_info['success'] is False
		assert user_info['reason'] == 'authentication_failed'
		assert user_info['error'] == f'账号凭据已失效（HTTP {status}）：{ACTION_HINT}'

	@pytest.mark.asyncio
	async def test_server_side_auth_message_keeps_detail_and_gains_action(self):
		"""HTTP 200 但响应体报未登录时，保留服务端原文并补上处置动作。"""
		service = CheckinService()
		client = MagicMock()
		client.get = AsyncMock(
			return_value=MockHttpClient.build_response(
				status=200,
				json_data={'success': False, 'message': '未登录或登录已过期'},
			)
		)

		user_info = await service._get_user_info(
			client=client,
			headers={},
			privacy_handler=PrivacyHandler(show_sensitive_info=False),
		)

		assert user_info['reason'] == 'authentication_failed'
		assert '未登录或登录已过期' in user_info['error']
		assert ACTION_HINT in user_info['error']

	@pytest.mark.asyncio
	async def test_non_auth_api_error_is_not_dressed_as_credential_failure(self):
		"""与凭据无关的业务错误不应被包装成凭据失效，否则会误导用户去换 cookie。"""
		service = CheckinService()
		client = MagicMock()
		client.get = AsyncMock(
			return_value=MockHttpClient.build_response(
				status=200,
				json_data={'success': False, 'message': '数据库暂时不可用'},
			)
		)

		user_info = await service._get_user_info(
			client=client,
			headers={},
			privacy_handler=PrivacyHandler(show_sensitive_info=False),
		)

		assert user_info['reason'] == 'api_error'
		assert user_info['error'] == '数据库暂时不可用'
		assert ACTION_HINT not in user_info['error']

	@pytest.mark.asyncio
	async def test_expired_session_without_password_surfaces_actionable_error(self):
		"""无 username/password 可刷新时，账号结果里带的必须是可执行的失效说明。"""
		service = CheckinService()
		account = {'cookies': {'session': 'expired'}, 'api_user': 'user-1'}
		client = MagicMock()
		client.get = AsyncMock(return_value=MockHttpClient.build_response(status=401))
		client.post = AsyncMock()
		client.cookies = MagicMock()
		client.__aenter__ = AsyncMock(return_value=client)
		client.__aexit__ = AsyncMock(return_value=None)

		with patch.object(service, '_get_waf_cookies_with_playwright', new=AsyncMock(return_value={'acw_tc': 'waf'})):
			with patch('httpx.AsyncClient', return_value=client):
				success, user_info, fault = await service.check_in_account(account, 0)

		assert success is False
		assert fault is None
		assert user_info is not None
		assert user_info['reason'] in CheckinService.Config.Authentication.FAILURE_REASONS
		assert ACTION_HINT in user_info['error']


class TestCredentialFaultReporting:
	"""凭据失效在通知上下文与 Actions summary 中的呈现"""

	def test_credential_expired_accounts_are_grouped_but_still_counted_as_failed(
		self,
		clean_notification_env: None,
	):
		kit = NotificationKit()
		data = build_notification_data([
			build_account_result(name='账号 A', status='credential_expired', error='账号凭据已失效（HTTP 401）'),
			build_account_result(name='账号 B', status='failed', error='签到失败'),
		])

		context = kit._build_context_data(data)

		assert [acc.name for acc in context['credential_expired_accounts']] == ['账号 A']
		assert [acc.name for acc in context['failed_accounts']] == ['账号 A', '账号 B']
		assert context['has_credential_expired'] is True
		assert context['all_credential_expired'] is False
		assert data.stats.failed_count == 2, '凭据失效仍是失败，不能像上游故障那样被排除'

	def test_all_credential_expired_flag_is_set_when_every_account_expired(
		self,
		clean_notification_env: None,
	):
		kit = NotificationKit()
		data = build_notification_data([
			build_account_result(name='账号 A', status='credential_expired', error='账号凭据已失效'),
			build_account_result(name='账号 B', status='credential_expired', error='账号凭据已失效'),
		])

		context = kit._build_context_data(data)

		assert context['all_credential_expired'] is True
		assert context['all_failed'] is True

	def test_summary_reports_credential_expiry_with_next_step(
		self,
		monkeypatch: pytest.MonkeyPatch,
		tmp_path,
	):
		summary_file = tmp_path / 'summary.md'
		monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary_file))
		reporter = GitHubReporter(PrivacyHandler(show_sensitive_info=False))
		message = f'账号凭据已失效（HTTP 401）：{ACTION_HINT}'

		reporter.generate_summary(
			success_count=0,
			total_count=2,
			account_results=[
				build_account_result(name='账号 A', status='credential_expired', error=message),
				build_account_result(name='账号 B', status='credential_expired', error=message),
			],
			notify_sent=True,
			notify_triggers=['failed'],
			notify_reasons=['检测到账号失败'],
			credential_fault_message=message,
		)

		content = summary_file.read_text(encoding='utf-8')

		assert '**🔑 所有账号凭据已失效，需要更新 cookie**' in content
		assert '- **失败比例**：2/2' in content, '凭据失效仍要计入失败比例'
		assert '- **凭据失效**：2/2' in content
		assert '### 🔑 账号凭据失效' in content
		assert '全部账号同时失效' in content
		assert '|账号 A|🔑 凭据失效|' in content

	def test_summary_keeps_generic_failure_wording_for_mixed_failures(
		self,
		monkeypatch: pytest.MonkeyPatch,
		tmp_path,
	):
		summary_file = tmp_path / 'summary.md'
		monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary_file))
		reporter = GitHubReporter(PrivacyHandler(show_sensitive_info=False))

		reporter.generate_summary(
			success_count=0,
			total_count=2,
			account_results=[
				build_account_result(name='账号 A', status='credential_expired', error='账号凭据已失效'),
				build_account_result(name='账号 B', status='failed', error='签到失败'),
			],
			notify_sent=True,
			notify_triggers=['failed'],
			notify_reasons=['检测到账号失败'],
			credential_fault_message='账号凭据已失效',
		)

		content = summary_file.read_text(encoding='utf-8')

		assert '**❌ 所有账号签到失败**' in content, '并非全部因凭据失效时不应改写总标题'
		assert '### 🔑 账号凭据失效' in content, '仍要单独列出失效账号'
		assert '|账号 A|🔑 凭据失效|' in content
		assert '|账号 B|❌ 签到失败|' in content
