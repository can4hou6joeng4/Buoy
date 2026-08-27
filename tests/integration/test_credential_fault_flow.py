import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from application import Application
from core.checkin_service import CheckinService
from tests.fixtures.data import STANDARD_ACCOUNTS
from tests.fixtures.mock_dependencies import DEFAULT_QUOTA, DEFAULT_USED_QUOTA, MockHttpClient, MockPlaywright


async def _expired_session_get_handler(*args, **kwargs):
	"""session 过期后 `/api/user/self` 的实际表现：WAF 已放行，鉴权中间件返回 401"""
	return MockHttpClient.build_response(status=401)


class TestCredentialFaultFlow:
	"""凭据失效在整条签到流程中的聚合与呈现"""

	@pytest.mark.asyncio
	async def test_all_accounts_expired_send_one_aggregated_notification(
		self,
		accounts_env,
		tmp_path,
		monkeypatch,
	):
		"""9 个账号同时 401 时只发一条聚合告警，而不是 N 条内容相同的失败通知。"""
		accounts_env(STANDARD_ACCOUNTS)
		monkeypatch.setenv('NOTIFY_TRIGGERS', 'failed')
		app = Application()
		app.balance_manager.balance_hash_file = tmp_path / 'hash_credential_all.txt'

		with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
			with ExitStack() as stack:
				MockPlaywright.setup_success(stack)
				MockHttpClient.setup(stack, _expired_session_get_handler, MockHttpClient.post_success_handler)

				with patch.object(app.github_reporter, 'generate_summary') as summary:
					with patch.object(
						app.notification_kit, 'push_raw_message', new=AsyncMock(return_value=True)
					) as push_raw:
						with patch.object(app.notification_kit, 'push_message', new=AsyncMock()) as push_message:
							with pytest.raises(SystemExit) as exc_info:
								await app.run()

		assert exc_info.value.code == 1, '凭据失效是需要人工处理的真实失败'
		assert push_message.await_count == 0, '聚合告警应替代逐账号失败通知'

		push_raw.assert_awaited_once()
		await_args = push_raw.await_args
		assert await_args is not None
		assert await_args.kwargs['title'] == 'AnyRouter 账号凭据集体失效'
		content = await_args.kwargs['content']
		assert '2/2 个账号本次全部认证失败' in content
		assert CheckinService.Config.Authentication.ACTION_HINT in content
		assert '重新登录 anyrouter.top 获取新的 session cookie' in content

		summary.assert_called_once()
		summary_kwargs = summary.call_args.kwargs
		assert [acc.status for acc in summary_kwargs['account_results']] == ['credential_expired'] * 2
		assert CheckinService.Config.Authentication.ACTION_HINT in summary_kwargs['credential_fault_message']

	@pytest.mark.asyncio
	async def test_partial_expiry_keeps_per_account_notification(self, accounts_env, tmp_path, monkeypatch):
		"""只有部分账号失效时不是系统性事件，仍走逐账号模板通知。"""
		accounts_env(STANDARD_ACCOUNTS)
		monkeypatch.setenv('NOTIFY_TRIGGERS', 'failed')
		app = Application()
		app.balance_manager.balance_hash_file = tmp_path / 'hash_credential_partial.txt'

		call_count = {'get': 0}

		async def get_handler(*args, **kwargs):
			call_count['get'] += 1
			if call_count['get'] == 1:
				return MockHttpClient.build_response(status=401)
			return MockHttpClient.build_response(
				status=200,
				json_data={'success': True, 'data': {'quota': DEFAULT_QUOTA, 'used_quota': DEFAULT_USED_QUOTA}},
			)

		with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
			with ExitStack() as stack:
				MockPlaywright.setup_success(stack)
				MockHttpClient.setup(stack, get_handler, MockHttpClient.post_success_handler)

				with patch.object(app.github_reporter, 'generate_summary') as summary:
					with patch.object(app.notification_kit, 'push_raw_message', new=AsyncMock()) as push_raw:
						with patch.object(app.notification_kit, 'push_message', new=AsyncMock()) as push_message:
							with pytest.raises(SystemExit):
								await app.run()

		assert push_message.await_count == 1, '部分失效仍需逐账号展示成功与失败'
		assert push_raw.await_count == 0, '未全量失效时不发聚合告警'
		assert [acc.status for acc in summary.call_args.kwargs['account_results']] == [
			'credential_expired',
			'success',
		]

	@pytest.mark.asyncio
	async def test_single_account_expiry_is_not_aggregated(self, accounts_env, tmp_path, monkeypatch):
		"""单账号失效无从判断是否系统性，也没有重复行可折叠，不触发聚合。"""
		accounts_env([{'name': '单账号', 'cookies': {'session': 'expired'}, 'api_user': 'user_single'}])
		monkeypatch.setenv('NOTIFY_TRIGGERS', 'failed')
		app = Application()
		app.balance_manager.balance_hash_file = tmp_path / 'hash_credential_single.txt'

		with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
			with ExitStack() as stack:
				MockPlaywright.setup_success(stack)
				MockHttpClient.setup(stack, _expired_session_get_handler, MockHttpClient.post_success_handler)

				with patch.object(app.notification_kit, 'push_raw_message', new=AsyncMock()) as push_raw:
					with patch.object(app.notification_kit, 'push_message', new=AsyncMock()) as push_message:
						with pytest.raises(SystemExit):
							await app.run()

		assert push_message.await_count == 1
		assert push_raw.await_count == 0

	@pytest.mark.asyncio
	async def test_aggregation_respects_notify_triggers(self, accounts_env, tmp_path, monkeypatch):
		"""聚合只是换了通知形态，不能绕过 never 触发器强推。"""
		accounts_env(STANDARD_ACCOUNTS)
		monkeypatch.setenv('NOTIFY_TRIGGERS', 'never')
		app = Application()
		app.balance_manager.balance_hash_file = tmp_path / 'hash_credential_never.txt'

		with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
			with ExitStack() as stack:
				MockPlaywright.setup_success(stack)
				MockHttpClient.setup(stack, _expired_session_get_handler, MockHttpClient.post_success_handler)

				with patch.object(app.notification_kit, 'push_raw_message', new=AsyncMock()) as push_raw:
					with patch.object(app.notification_kit, 'push_message', new=AsyncMock()) as push_message:
						with pytest.raises(SystemExit):
							await app.run()

		assert push_raw.await_count == 0
		assert push_message.await_count == 0
