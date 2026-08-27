#!/usr/bin/env python3
"""通过 AnyRouter 登录接口刷新账号凭据，不执行签到。"""

import argparse
import asyncio
import configparser
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import async_playwright

BASE_URL = 'https://anyrouter.top'
LOGIN_PAGE_URL = f'{BASE_URL}/login'
LOGIN_API_URL = f'{BASE_URL}/api/user/login'
USER_INFO_URL = f'{BASE_URL}/api/user/self'
DEFAULT_OUTPUT = Path('anyrouter-refreshed-accounts.json')
WAF_COOKIE_NAMES = ('acw_tc', 'cdn_sec_tc', 'acw_sc__v2')
USER_AGENT = (
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
)


class CredentialRefreshError(RuntimeError):
	"""可安全展示的凭据刷新错误，不包含账号密码或 Cookie。"""


def resolve_input_path(explicit_path: Path | None, env_file: Path | None = None) -> Path:
	"""解析输入路径；未显式指定时读取 ~/.env 的 [Anyrouter] 配置。"""
	if explicit_path:
		return explicit_path.expanduser().resolve()

	config_path = (env_file or Path.home() / '.env').expanduser()
	parser = configparser.ConfigParser(interpolation=None)
	if not parser.read(config_path, encoding='utf-8') or not parser.has_section('Anyrouter'):
		raise CredentialRefreshError(f'未指定 --input，且 {config_path} 中没有 [Anyrouter] 配置')

	configured_path = parser.get('Anyrouter', 'ANYROUTER_ACCOUNTS_FILE', fallback='').strip()
	if not configured_path:
		raise CredentialRefreshError('[Anyrouter] 中缺少 ANYROUTER_ACCOUNTS_FILE')
	return Path(configured_path).expanduser().resolve()


def load_accounts(path: Path) -> list[dict[str, Any]]:
	"""读取并验证账号数组；异常信息不会包含文件内容。"""
	try:
		data = json.loads(path.read_text(encoding='utf-8'))
	except FileNotFoundError as exc:
		raise CredentialRefreshError(f'账号文件不存在：{path}') from exc
	except (OSError, json.JSONDecodeError) as exc:
		raise CredentialRefreshError(f'无法读取有效的账号 JSON：{path}') from exc

	if not isinstance(data, list) or not data:
		raise CredentialRefreshError('账号配置必须是非空 JSON 数组')
	for index, account in enumerate(data, 1):
		if not isinstance(account, dict):
			raise CredentialRefreshError(f'账号 {index} 必须是 JSON 对象')
		if not account.get('username') or not account.get('password'):
			raise CredentialRefreshError(f'账号 {index} 缺少 username/password')
	return data


def select_account_name(user_data: dict[str, Any], fallback_username: str) -> str:
	"""使用与生产签到一致的服务端名称优先级。"""
	for key in ('display_name', 'username'):
		value = user_data.get(key)
		if isinstance(value, str) and value.strip():
			return value.strip()
	return fallback_username.strip()


def write_accounts_securely(path: Path, accounts: list[dict[str, Any]]) -> None:
	"""以 0600 权限原子写入账号 JSON，拒绝覆盖符号链接。"""
	path = path.expanduser()
	if path.is_symlink():
		raise CredentialRefreshError('拒绝写入符号链接输出路径')
	path = path.resolve(strict=False)
	if not path.parent.is_dir():
		raise CredentialRefreshError(f'输出目录不存在：{path.parent}')

	fd, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
	temporary_path = Path(temporary_name)
	try:
		os.fchmod(fd, 0o600)
		with os.fdopen(fd, 'w', encoding='utf-8') as output_file:
			json.dump(accounts, output_file, ensure_ascii=False, separators=(',', ':'))
			output_file.write('\n')
		os.replace(temporary_path, path)
		os.chmod(path, 0o600)
	except Exception:
		try:
			temporary_path.unlink(missing_ok=True)
		except OSError:
			pass
		raise


def build_headers(api_user: str | None = None, *, login: bool = False) -> dict[str, str]:
	"""构造浏览器已验证的 AnyRouter 同源请求头。"""
	headers = {
		'User-Agent': USER_AGENT,
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Origin': BASE_URL,
		'Referer': LOGIN_PAGE_URL if login else f'{BASE_URL}/console',
	}
	if login:
		headers['Content-Type'] = 'application/json'
	if api_user:
		headers['new-api-user'] = api_user
	return headers


async def acquire_waf_cookies() -> dict[str, str]:
	"""用隔离的无头浏览器访问登录页并提取 WAF Cookie。"""
	async with async_playwright() as playwright:
		browser = await playwright.chromium.launch(
			headless=True,
			args=['--disable-blink-features=AutomationControlled', '--disable-dev-shm-usage', '--no-sandbox'],
		)
		try:
			context = await browser.new_context(user_agent=USER_AGENT)
			page = await context.new_page()
			await page.goto(LOGIN_PAGE_URL, wait_until='domcontentloaded', timeout=30_000)
			await page.wait_for_timeout(1_000)
			cookies = {item['name']: item['value'] for item in await context.cookies()}
		finally:
			await browser.close()

	missing = [name for name in WAF_COOKIE_NAMES if not cookies.get(name)]
	if missing:
		raise CredentialRefreshError(f'登录页未签发完整 WAF Cookie：缺少 {", ".join(missing)}')
	return {name: cookies[name] for name in WAF_COOKIE_NAMES}


async def refresh_account(
	account: dict[str, Any],
	index: int,
	waf_cookies: dict[str, str],
	timeout: float,
) -> dict[str, Any]:
	"""登录、验证并返回仅包含必要持久字段的账号副本。"""
	async with httpx.AsyncClient(http2=True, timeout=timeout, cookies=waf_cookies) as client:
		response = await client.post(
			LOGIN_API_URL,
			params={'turnstile': ''},
			headers=build_headers(login=True),
			json={'username': account['username'], 'password': account['password']},
		)
		if response.status_code != 200:
			raise CredentialRefreshError(f'账号 {index} 登录接口返回 HTTP {response.status_code}')
		try:
			login_payload = response.json()
		except json.JSONDecodeError as exc:
			raise CredentialRefreshError(f'账号 {index} 登录接口未返回有效 JSON') from exc
		if not login_payload.get('success'):
			message = str(login_payload.get('message', '登录失败')).replace('\n', ' ')[:120]
			raise CredentialRefreshError(f'账号 {index} 登录失败：{message}')

		login_data = login_payload.get('data') if isinstance(login_payload.get('data'), dict) else {}
		api_user = str(login_data.get('id', '')).strip()
		session = next((cookie.value for cookie in client.cookies.jar if cookie.name == 'session'), '')
		if not api_user or not session:
			raise CredentialRefreshError(f'账号 {index} 登录响应缺少 session 或用户 ID')

		self_response = await client.get(USER_INFO_URL, headers=build_headers(api_user))
		if self_response.status_code != 200:
			raise CredentialRefreshError(f'账号 {index} 凭据验证返回 HTTP {self_response.status_code}')
		try:
			self_payload = self_response.json()
		except json.JSONDecodeError as exc:
			raise CredentialRefreshError(f'账号 {index} 凭据验证未返回有效 JSON') from exc
		if not self_payload.get('success') or not isinstance(self_payload.get('data'), dict):
			raise CredentialRefreshError(f'账号 {index} 凭据验证失败')

		user_data = self_payload['data']
		return {
			'name': select_account_name(user_data, str(account['username'])),
			'username': account['username'],
			'password': account['password'],
			'cookies': {'session': session},
			'api_user': api_user,
		}


async def refresh_accounts(
	accounts: list[dict[str, Any]],
	excluded_indexes: set[int],
	timeout: float,
	show_account_names: bool,
) -> list[dict[str, Any]]:
	"""刷新未排除账号；任何一个失败时不生成部分输出文件。"""
	waf_cookies = await acquire_waf_cookies()
	refreshed: list[dict[str, Any]] = []
	for index, account in enumerate(accounts, 1):
		if index in excluded_indexes:
			print(f'跳过账号 {index}')
			continue
		updated = await refresh_account(account, index, waf_cookies, timeout)
		label = updated['name'] if show_account_names else f'账号 {index}'
		print(f'{label}: 登录与凭据验证成功')
		refreshed.append(updated)
	if not refreshed:
		raise CredentialRefreshError('没有可写出的有效账号')
	return refreshed


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='通过 AnyRouter 接口登录并整理 GitHub Actions 所需账号凭据')
	parser.add_argument('--input', type=Path, help='输入账号 JSON；默认读取 ~/.env 的 [Anyrouter] 配置')
	parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT, help=f'输出 JSON（默认 {DEFAULT_OUTPUT}）')
	parser.add_argument('--exclude-index', type=int, action='append', default=[], help='排除 1 开始的账号序号，可重复')
	parser.add_argument('--timeout', type=float, default=30.0, help='单个 HTTP 请求超时秒数（默认 30）')
	parser.add_argument('--show-account-names', action='store_true', help='在终端成功行显示服务端真实名称')
	return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
	input_path = resolve_input_path(args.input)
	output_path = args.output.expanduser().resolve(strict=False)
	if input_path == output_path:
		raise CredentialRefreshError('输出路径必须与输入路径不同，避免意外覆盖原始登录凭据')

	accounts = load_accounts(input_path)
	excluded = set(args.exclude_index)
	invalid_indexes = sorted(index for index in excluded if index < 1 or index > len(accounts))
	if invalid_indexes:
		raise CredentialRefreshError(f'排除序号越界：{invalid_indexes}')

	refreshed = await refresh_accounts(accounts, excluded, args.timeout, args.show_account_names)
	write_accounts_securely(output_path, refreshed)
	print(f'已安全写出 {len(refreshed)} 个账号：{output_path}（权限 0600）')
	return 0


def main() -> int:
	args = parse_args()
	try:
		return asyncio.run(async_main(args))
	except (CredentialRefreshError, httpx.RequestError, OSError) as exc:
		print(f'错误：{exc}', file=sys.stderr)
		return 1


if __name__ == '__main__':
	raise SystemExit(main())
