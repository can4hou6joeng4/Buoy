import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from application import Application
from tests.fixtures.data import STANDARD_ACCOUNTS
from tests.fixtures.mock_dependencies import MockHttpClient, MockPlaywright
from tests.unit.test_upstream_fault import MYSQL_LOCK_ERROR


async def _mysql_lock_post_handler(*args, **kwargs):
	"""签到接口返回 HTTP 200，但响应体透传服务端数据库错误"""
	return MockHttpClient.build_response(
		status=200,
		json_data={'ret': 0, 'msg': MYSQL_LOCK_ERROR},
	)


class TestUpstreamFaultFlow:
	"""上游服务故障在整条签到流程中的处理"""

	@pytest.mark.asyncio
	async def test_upstream_fault_does_not_fail_the_workflow(self, accounts_env, tmp_path, monkeypatch):
		"""服务端数据库错误不应记为账号失败，也不应让工作流退出码变红。"""
		accounts_env(STANDARD_ACCOUNTS)
		# 只保留 failed 触发器，隔离出「没有账号失败 -> 模板通知不发」的路径
		monkeypatch.setenv('NOTIFY_TRIGGERS', 'failed')
		app = Application()
		app.balance_manager.balance_hash_file = tmp_path / 'hash_upstream.txt'

		with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
			with ExitStack() as stack:
				MockPlaywright.setup_success(stack)
				MockHttpClient.setup(stack, MockHttpClient.get_success_handler, _mysql_lock_post_handler)

				with patch.object(app.github_reporter, 'generate_summary') as summary:
					with patch.object(
						app.notification_kit, 'push_raw_message', new=AsyncMock(return_value=True)
					) as push_raw:
						with patch.object(app.notification_kit, 'push_message', new=AsyncMock()) as push_message:
							with pytest.raises(SystemExit) as exc_info:
								await app.run()

		assert exc_info.value.code == 0, '上游故障不应让工作流失败'
		assert push_message.await_count == 0, '不应发送逐账号失败通知'

		# 上游故障单独通知一次，说明这不是账号凭据问题
		push_raw.assert_awaited_once()
		await_args = push_raw.await_args
		assert await_args is not None
		assert await_args.kwargs['title'] == 'AnyRouter 上游服务故障'
		content = await_args.kwargs['content']
		assert MYSQL_LOCK_ERROR in content
		assert '2/2 个账号本次未能签到' in content
		assert '不代表账号凭据失效' in content

		# summary 中账号状态被标为上游故障而非失败
		summary.assert_called_once()
		summary_kwargs = summary.call_args.kwargs
		assert summary_kwargs['success_count'] == 0
		assert summary_kwargs['upstream_fault_message'] == MYSQL_LOCK_ERROR
		assert [acc.status for acc in summary_kwargs['account_results']] == ['upstream_fault'] * 2

	@pytest.mark.asyncio
	async def test_checkin_5xx_is_upstream_fault(self, accounts_env, tmp_path, monkeypatch):
		"""签到接口 5xx 属于服务端故障，同样不记为账号失败。"""
		accounts_env(STANDARD_ACCOUNTS)
		monkeypatch.setenv('NOTIFY_TRIGGERS', 'failed')
		app = Application()
		app.balance_manager.balance_hash_file = tmp_path / 'hash_upstream_5xx.txt'

		async def post_handler(*args, **kwargs):
			return MockHttpClient.build_response(status=503)

		with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
			with ExitStack() as stack:
				MockPlaywright.setup_success(stack)
				MockHttpClient.setup(stack, MockHttpClient.get_success_handler, post_handler)

				with patch.object(app.notification_kit, 'push_raw_message', new=AsyncMock(return_value=True)):
					with pytest.raises(SystemExit) as exc_info:
						await app.run()

		assert exc_info.value.code == 0

	@pytest.mark.asyncio
	async def test_real_failure_alongside_upstream_fault_still_fails(self, accounts_env, tmp_path, monkeypatch):
		"""上游故障与真实账号失败并存时，仍应因账号失败退出码为 1。"""
		accounts_env(STANDARD_ACCOUNTS)
		monkeypatch.setenv('NOTIFY_TRIGGERS', 'failed')
		app = Application()
		app.balance_manager.balance_hash_file = tmp_path / 'hash_upstream_mixed.txt'

		call_count = {'post': 0}

		async def post_handler(*args, **kwargs):
			call_count['post'] += 1
			if call_count['post'] == 1:
				return MockHttpClient.build_response(
					status=200,
					json_data={'ret': 0, 'msg': MYSQL_LOCK_ERROR},
				)
			return MockHttpClient.build_response(
				status=200,
				json_data={'ret': 0, 'msg': '未登录或登录已过期'},
			)

		with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '/dev/null'}):
			with ExitStack() as stack:
				MockPlaywright.setup_success(stack)
				MockHttpClient.setup(stack, MockHttpClient.get_success_handler, post_handler)

				with patch.object(app.notification_kit, 'push_raw_message', new=AsyncMock()) as push_raw:
					with patch.object(app.notification_kit, 'push_message', new=AsyncMock()) as push_message:
						with pytest.raises(SystemExit) as exc_info:
							await app.run()

		assert exc_info.value.code == 1, '存在真实账号失败时仍应失败'
		assert push_message.await_count == 1, '账号失败走原有的逐账号通知'
		assert push_raw.await_count == 0, '模板通知已发出时不再单独推送上游故障'
