import json
import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from application import Application
from core.checkin_service import CheckinService
from tests.fixtures.mock_dependencies import DEFAULT_QUOTA, DEFAULT_USED_QUOTA, MockHttpClient


def build_client(get_handler, post_handler):
	client = MagicMock()
	client.get = AsyncMock(side_effect=get_handler)
	client.post = AsyncMock(side_effect=post_handler)
	client.cookies = httpx.Cookies()
	client.__aenter__ = AsyncMock(return_value=client)
	client.__aexit__ = AsyncMock(return_value=None)
	return client


@pytest.mark.asyncio
async def test_expired_session_refreshes_credentials_and_continues_checkin():
	service = CheckinService()
	account = {
		'name': 'refreshable',
		'username': 'test-user',
		'password': 'test-password',
		'cookies': {'session': 'expired-session'},
		'api_user': 'old-user-id',
	}
	get_count = 0

	async def get_handler(*args, **kwargs):
		nonlocal get_count
		get_count += 1
		if get_count == 1:
			return MockHttpClient.build_response(status=401)
		return MockHttpClient.build_response(
			status=200,
			json_data={
				'success': True,
				'data': {
					'quota': DEFAULT_QUOTA,
					'used_quota': DEFAULT_USED_QUOTA,
					'display_name': '服务端名称',
					'password': 'must-not-be-forwarded',
				},
			},
		)

	async def post_handler(*args, **kwargs):
		if kwargs['url'] == service.Config.URLs.AUTH_LOGIN:
			client.cookies.set('session', 'refreshed-session')
			return MockHttpClient.build_response(
				status=200,
				json_data={'success': True, 'data': {'id': 12345}},
			)
		return MockHttpClient.build_response(status=200, json_data={'success': True})

	client = build_client(get_handler, post_handler)
	with patch.object(service, '_get_waf_cookies_with_playwright', new=AsyncMock(return_value={'acw_tc': 'waf'})):
		with patch('httpx.AsyncClient', return_value=client):
			success, user_info, fault = await service.check_in_account(account, 0)

	assert success is True
	assert user_info is not None and user_info['success'] is True
	assert fault is None
	assert get_count == 2
	assert service.refreshed_credentials_count == 1
	assert service.updated_accounts_count == 1
	assert account['api_user'] == '12345'
	assert account['cookies'] == {'session': 'refreshed-session'}
	assert account['name'] == '服务端名称'
	assert user_info['account_name'] == '服务端名称'
	assert 'password' not in user_info
	assert client.cookies.get('session') == 'refreshed-session'
	login_call = next(
		call for call in client.post.await_args_list if call.kwargs['url'] == service.Config.URLs.AUTH_LOGIN
	)
	assert login_call.kwargs['params'] == {'turnstile': ''}
	assert set(login_call.kwargs['json']) == {'username', 'password'}


@pytest.mark.asyncio
async def test_non_authentication_failure_does_not_attempt_login_refresh():
	service = CheckinService()
	account = {
		'username': 'test-user',
		'password': 'test-password',
		'cookies': {'session': 'existing-session'},
		'api_user': 'existing-user-id',
	}

	async def get_handler(*args, **kwargs):
		return MockHttpClient.build_response(status=500)

	client = build_client(get_handler, AsyncMock())
	with patch.object(service, '_get_waf_cookies_with_playwright', new=AsyncMock(return_value={'acw_tc': 'waf'})):
		with patch('httpx.AsyncClient', return_value=client):
			success, user_info, fault = await service.check_in_account(account, 0)

	assert success is False
	assert user_info is not None and user_info['reason'] == 'http_error'
	assert fault is None
	client.post.assert_not_awaited()
	assert service.refreshed_credentials_count == 0


@pytest.mark.asyncio
async def test_authentication_failure_without_password_keeps_existing_failure():
	service = CheckinService()
	account = {'cookies': {'session': 'expired-session'}, 'api_user': 'existing-user-id'}

	async def get_handler(*args, **kwargs):
		return MockHttpClient.build_response(status=401)

	client = build_client(get_handler, AsyncMock())
	with patch.object(service, '_get_waf_cookies_with_playwright', new=AsyncMock(return_value={'acw_tc': 'waf'})):
		with patch('httpx.AsyncClient', return_value=client):
			success, user_info, fault = await service.check_in_account(account, 0)

	assert success is False
	assert user_info is not None and user_info['reason'] == 'authentication_failed'
	assert fault is None
	client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_response_requires_session_and_user_id():
	service = CheckinService()
	account = {'username': 'test-user', 'password': 'test-password'}
	client = build_client(
		AsyncMock(),
		AsyncMock(
			return_value=MockHttpClient.build_response(
				status=200,
				json_data={'success': True, 'data': {'id': 12345}},
			)
		),
	)

	error = await service._refresh_account_credentials(client, account)

	assert error == '自动刷新账号凭据失败：登录响应缺少 session 或用户 ID'
	assert 'cookies' not in account
	assert service.refreshed_credentials_count == 0


@pytest.mark.parametrize(
	'message,expected',
	[
		('请先登录', 'authentication_failed'),
		('Unauthorized', 'authentication_failed'),
		('数据库暂时不可用', 'api_error'),
	],
)
def test_user_info_failure_classification(message, expected):
	assert CheckinService()._classify_user_info_failure(message) == expected


@pytest.mark.parametrize(
	'user_data,expected',
	[
		({'display_name': ' 显示名 ', 'username': '用户名'}, '显示名'),
		({'display_name': '', 'username': ' 用户名 '}, '用户名'),
		({'display_name': None, 'username': ''}, None),
	],
)
def test_account_name_is_extracted_from_whitelisted_fields(user_data, expected):
	assert CheckinService._extract_account_name(user_data) == expected


def test_login_only_account_is_valid_for_automatic_bootstrap(monkeypatch):
	monkeypatch.setenv(
		'ANYROUTER_ACCOUNTS',
		json.dumps([{'name': 'bootstrap', 'username': 'test-user', 'password': 'test-password'}]),
	)
	app = Application()

	accounts = app._load_accounts()

	assert len(accounts) == 1
	assert accounts[0]['username'] == 'test-user'


def test_refreshed_accounts_are_exported_with_restricted_permissions(monkeypatch, tmp_path):
	app = Application()
	app.checkin_service.refreshed_credentials_count = 1
	app.checkin_service.updated_accounts_count = 1
	target = tmp_path / 'refreshed-accounts.json'
	monkeypatch.setenv(CheckinService.Config.Env.REFRESHED_ACCOUNTS_FILE, str(target))
	accounts = [
		{
			'name': 'refreshable',
			'username': 'test-user',
			'password': 'test-password',
			'cookies': {'session': 'refreshed-session'},
			'api_user': '12345',
		}
	]

	assert app._export_refreshed_accounts(accounts) is True
	assert json.loads(target.read_text(encoding='utf-8')) == accounts
	assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_refreshed_accounts_are_not_exported_when_prefix_overrides_exist(monkeypatch, tmp_path):
	app = Application()
	app.checkin_service.refreshed_credentials_count = 1
	app.checkin_service.updated_accounts_count = 1
	app.has_prefix_account_configs = True
	target = tmp_path / 'refreshed-accounts.json'
	monkeypatch.setenv(CheckinService.Config.Env.REFRESHED_ACCOUNTS_FILE, str(target))

	assert app._export_refreshed_accounts([{'username': 'test-user', 'password': 'test-password'}]) is False
	assert target.exists() is False
