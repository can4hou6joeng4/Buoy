import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from playwright.async_api import async_playwright

from core.privacy_handler import PrivacyHandler
from tools.logger import logger


class CheckinService:
	"""AnyRouter 签到服务"""

	class Config:
		"""服务配置"""

		class URLs:
			"""URL 配置"""

			BASE = 'https://anyrouter.top'
			LOGIN = f'{BASE}/login'
			API_BASE = f'{BASE}/api'
			AUTH_LOGIN = f'{API_BASE}/user/login'
			USER_INFO = f'{API_BASE}/user/self'
			CHECKIN = f'{API_BASE}/user/sign_in'
			CONSOLE = f'{BASE}/console'

		class Env:
			"""环境变量配置"""

			ACCOUNTS_KEY = 'ANYROUTER_ACCOUNTS'
			ACCOUNT_PREFIX = 'ANYROUTER_ACCOUNT_'
			SHOW_SENSITIVE_INFO = 'SHOW_SENSITIVE_INFO'
			REPO_VISIBILITY = 'REPO_VISIBILITY'
			ACTIONS_RUNNER_DEBUG = 'ACTIONS_RUNNER_DEBUG'
			GITHUB_STEP_SUMMARY = 'GITHUB_STEP_SUMMARY'
			REFRESHED_ACCOUNTS_FILE = 'ANYROUTER_REFRESHED_ACCOUNTS_FILE'
			CI = 'CI'
			GITHUB_ACTIONS = 'GITHUB_ACTIONS'

		class File:
			"""文件配置"""

			BALANCE_HASH_NAME = 'balance_hash.txt'

		class Browser:
			"""浏览器配置"""

			USER_AGENT_PARTS = [
				'Mozilla/5.0',
				'(Windows NT 10.0; Win64; x64)',
				'AppleWebKit/537.36',
				'(KHTML, like Gecko)',
				'Chrome/138.0.0.0',
				'Safari/537.36',
			]
			ARGS = [
				'--disable-blink-features=AutomationControlled',
				'--disable-dev-shm-usage',
				'--disable-web-security',
				'--disable-features=VizDisplayCompositor',
				'--no-sandbox',
			]

		class WAF:
			"""WAF 配置"""

			COOKIE_NAMES = ['acw_tc', 'cdn_sec_tc', 'acw_sc__v2']

		class Retry:
			"""瞬时故障重试配置"""

			# 签到请求最大尝试次数（含首次）
			CHECKIN_MAX_ATTEMPTS = 3

			# WAF cookies 最大尝试次数（含首次），浏览器启动开销大，次数更保守
			WAF_MAX_ATTEMPTS = 2

			# 每次重试前的等待秒数
			DELAY_SECONDS = 5

		class Upstream:
			"""上游服务故障识别配置"""

			# 签到接口返回 HTTP 200，但响应体里携带的服务端错误特征
			# 命中任意一项即判定为 AnyRouter 服务端故障，而非账号问题
			ERROR_MARKERS = (
				'lock_write_growth',
				'hy000',
				'mysql server',
				'read-only',
				'read only',
				'database is locked',
				'too many connections',
				'deadlock found',
				'bad gateway',
				'service unavailable',
				'gateway timeout',
			)

		class Authentication:
			"""用于识别 session 失效的服务端错误文案。"""

			ERROR_MARKERS = (
				'unauthorized',
				'authentication failed',
				'invalid user',
				'user not found',
				'not logged in',
				'未登录',
				'请先登录',
				'登录已过期',
				'登录状态无效',
				'用户不存在',
				'无效的用户',
			)

	@dataclass(frozen=True)
	class UpstreamFault:
		"""上游服务故障（AnyRouter 服务端问题，不应记为账号失败）"""

		# 稳定的机器可读原因，如 upstream_database_error、upstream_server_error
		reason: str

		# 供日志、通知和 summary 使用的可读描述
		message: str

	@dataclass(frozen=True)
	class InfrastructureCheckResult:
		"""AnyRouter 基础设施预检结果"""

		available: bool
		reason: str
		message: str
		attempts: int
		url: str = 'https://anyrouter.top/login'

	def __init__(self):
		self.refreshed_credentials_count = 0

	async def check_infrastructure(
		self,
		max_attempts: int = 3,
		delay_seconds: int = 60,
	) -> InfrastructureCheckResult:
		"""检查 AnyRouter 登录页可用性，避免基础设施故障被记为账号失败。"""
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
					f'基础设施预检失败（{last_result.message}），{delay_seconds} 秒后重试 {attempt + 1}/{max_attempts}',
					tag='基础设施',
				)
				await asyncio.sleep(delay_seconds)

		return last_result

	async def _check_login_page_once(self, attempt: int) -> InfrastructureCheckResult:
		"""执行一次 AnyRouter 登录页可达性探测。"""
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
		"""将登录页可达性异常分类为稳定的通知和 summary 字段。"""
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

	def _detect_upstream_fault(self, error_msg: str) -> UpstreamFault | None:
		"""
		判断签到接口的业务错误是否来自 AnyRouter 服务端

		签到接口在服务端数据库只读、网关异常时仍会返回 HTTP 200，
		把原始错误透传在响应体里。这类错误与账号凭据无关，不应记为账号失败。

		Args:
		    error_msg: 签到响应中的错误文案

		Returns:
		    UpstreamFault | None: 命中上游故障特征时返回故障详情，否则返回 None
		"""
		lowered = error_msg.lower()
		if any(marker in lowered for marker in self.Config.Upstream.ERROR_MARKERS):
			return self.UpstreamFault(
				reason='upstream_database_error',
				message=error_msg,
			)

		return None

	async def check_in_account(
		self,
		account_info: dict[str, Any],
		account_index: int,
	) -> tuple[bool, dict[str, Any] | None, UpstreamFault | None]:
		"""
		为单个账号执行签到操作

		Args:
		    account_info: 账号配置信息
		    account_index: 账号索引

		Returns:
		    tuple[bool, dict[str, Any] | None, UpstreamFault | None]:
		        (是否签到成功, 用户信息, 上游服务故障；无上游故障时为 None)
		"""
		privacy_handler = PrivacyHandler(PrivacyHandler.should_show_sensitive_info())
		account_name = privacy_handler.get_safe_account_name(account_info, account_index)
		logger.processing(f'开始处理 {account_name}')

		api_user = str(account_info.get('api_user', ''))
		user_cookies = self._parse_cookies(account_info.get('cookies', {}))
		has_login_credentials = bool(account_info.get('username') and account_info.get('password'))

		if (not api_user or not user_cookies) and not has_login_credentials:
			logger.error('缺少有效的 session/api_user，且未配置 username/password', account_name=account_name)
			return False, None, None

		# 步骤1：获取 WAF cookies
		waf_cookies = await self._get_waf_cookies_with_playwright(account_name)
		if not waf_cookies:
			logger.error('无法获取 WAF cookies', account_name=account_name)
			return False, None, None

		# 步骤2：使用 httpx 进行 API 请求
		async with httpx.AsyncClient(http2=True, timeout=30.0) as client:
			try:
				# 合并 WAF cookies 和仍可能有效的用户 cookies
				all_cookies = {**waf_cookies, **user_cookies}
				client.cookies.update(all_cookies)

				headers = self._build_api_headers(api_user)
				user_info = None
				needs_refresh = not api_user or not user_cookies

				if not needs_refresh:
					user_info = await self._get_user_info(
						client=client,
						headers=headers,
						privacy_handler=privacy_handler,
					)
					needs_refresh = user_info.get('reason') == 'authentication_failed'

				if needs_refresh:
					if not has_login_credentials:
						logger.warning(
							user_info.get('error', '账号凭据已失效') if user_info else '账号凭据已失效',
							account_name=account_name,
						)
						return False, user_info, None

					logger.warning('检测到账号凭据缺失或已失效，尝试自动刷新', account_name=account_name)
					refresh_error = await self._refresh_account_credentials(
						client=client,
						account_info=account_info,
					)
					if refresh_error:
						logger.error(refresh_error, account_name=account_name)
						return (
							False,
							{'success': False, 'error': refresh_error, 'reason': 'credential_refresh_failed'},
							None,
						)

					api_user = str(account_info['api_user'])
					headers = self._build_api_headers(api_user)
					user_info = await self._get_user_info(
						client=client,
						headers=headers,
						privacy_handler=privacy_handler,
					)

				if user_info and user_info.get('success'):
					logger.info(user_info['display'], account_name)
				elif user_info:
					logger.warning(user_info.get('error', '未知错误'), account_name)
					return False, user_info, None

				logger.debug(
					message='执行签到',
					tag='网络',
					account_name=account_name,
				)

				# 更新签到请求头
				checkin_headers = headers.copy()
				checkin_headers.update({
					'Content-Type': 'application/json',
					'X-Requested-With': 'XMLHttpRequest'
				})  # fmt: skip

				response = await self._post_checkin_with_retry(
					client=client,
					headers=checkin_headers,
					account_name=account_name,
				)

				logger.debug(
					message=f'响应状态码 {response.status_code}',
					tag='响应',
					account_name=account_name,
				)

				# 签到接口 5xx 属于服务端故障，与账号凭据无关
				if response.status_code >= 500:
					fault = self.UpstreamFault(
						reason='upstream_server_error',
						message=f'签到接口返回 HTTP {response.status_code}',
					)
					logger.warning(f'上游服务故障 - {fault.message}', account_name=account_name)
					return False, user_info, fault

				# HTTP 请求失败
				if response.status_code != 200:
					logger.error(f'签到失败 - HTTP {response.status_code}', account_name)
					return False, user_info, None

				# 处理响应结果
				try:
					result = response.json()
					if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
						logger.success('签到成功!', account_name)
						return True, user_info, None

					# 签到失败
					error_msg = str(result.get('msg', result.get('message', '未知错误')))

					# 响应体透传了服务端数据库/网关错误，归类为上游故障
					fault = self._detect_upstream_fault(error_msg)
					if fault:
						logger.warning(f'上游服务故障 - {error_msg}', account_name=account_name)
						return False, user_info, fault

					logger.error(f'签到失败 - {error_msg}', account_name)
					return False, user_info, None

				except json.JSONDecodeError:
					# 如果不是 JSON 响应，检查是否包含成功标识
					if 'success' in response.text.lower():
						logger.success('签到成功!', account_name)
						return True, user_info, None

					# 签到失败
					logger.error('签到失败 - 无效响应格式', account_name)
					return False, user_info, None

			except Exception as e:
				logger.error(
					message=f'签到过程中发生错误 - {str(e)[:50]}...',
					account_name=account_name,
					exc_info=True,
				)
				return False, None, None

	def _build_api_headers(self, api_user: str = '') -> dict[str, str]:
		"""构造 AnyRouter 同源 API 请求头。"""
		headers = {
			'User-Agent': ' '.join(self.Config.Browser.USER_AGENT_PARTS),
			'Referer': self.Config.URLs.CONSOLE,
			'Origin': self.Config.URLs.BASE,
			'Accept': 'application/json, text/plain, */*',
			'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
			'Accept-Encoding': 'gzip, deflate, br, zstd',
			'Connection': 'keep-alive',
			'Sec-Fetch-Dest': 'empty',
			'Sec-Fetch-Mode': 'cors',
			'Sec-Fetch-Site': 'same-origin',
		}
		if api_user:
			headers['new-api-user'] = api_user
		return headers

	async def _refresh_account_credentials(
		self,
		client: httpx.AsyncClient,
		account_info: dict[str, Any],
	) -> str | None:
		"""使用长期登录凭据刷新 session 和 API 用户标识；成功时原地更新账号。"""
		username = account_info.get('username')
		password = account_info.get('password')
		if not username or not password:
			return '未配置 username/password，无法自动刷新账号凭据'

		login_headers = self._build_api_headers()
		login_headers.update({
			'Referer': self.Config.URLs.LOGIN,
			'Content-Type': 'application/json',
		})
		for cookie in list(client.cookies.jar):
			if cookie.name == 'session':
				client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)

		try:
			response = await client.post(
				url=self.Config.URLs.AUTH_LOGIN,
				params={'turnstile': ''},
				headers=login_headers,
				json={'username': username, 'password': password},
				timeout=30,
			)
		except httpx.TimeoutException:
			return '自动刷新账号凭据失败：登录请求超时'
		except httpx.RequestError:
			return '自动刷新账号凭据失败：登录网络错误'

		if response.status_code != 200:
			return f'自动刷新账号凭据失败：登录接口返回 HTTP {response.status_code}'

		try:
			result = response.json()
		except json.JSONDecodeError:
			return '自动刷新账号凭据失败：登录接口未返回有效 JSON，可能被 WAF 拒绝'

		if not result.get('success'):
			message = str(result.get('message', '账号或密码错误'))
			return f'自动刷新账号凭据失败：{message}'

		user_data = result.get('data')
		api_user = str(user_data.get('id', '')) if isinstance(user_data, dict) else ''
		try:
			session = client.cookies.get('session')
		except httpx.CookieConflict:
			session = next((cookie.value for cookie in client.cookies.jar if cookie.name == 'session'), None)

		if not api_user or not session:
			return '自动刷新账号凭据失败：登录响应缺少 session 或用户 ID'

		account_info['api_user'] = api_user
		account_info['cookies'] = {'session': session}
		self.refreshed_credentials_count += 1
		logger.success('账号凭据已自动刷新')
		return None

	async def _post_checkin_with_retry(
		self,
		client,
		headers: dict[str, str],
		account_name: str,
	):
		"""
		执行签到请求，对超时这类瞬时网络故障做有限重试

		基础设施预检只覆盖签到开始前的可达性，单个账号的签到请求仍可能撞上
		网络抖动。这里只重试超时，凭据错误、业务错误一律交由调用方按原逻辑处理。

		Args:
		    client: httpx 客户端
		    headers: 签到请求头
		    account_name: 账号名称（用于日志）

		Returns:
		    httpx 响应对象

		Raises:
		    httpx.TimeoutException: 所有尝试都超时时抛出最后一次异常
		"""
		max_attempts = self.Config.Retry.CHECKIN_MAX_ATTEMPTS
		delay_seconds = self.Config.Retry.DELAY_SECONDS
		last_error: httpx.TimeoutException | None = None

		for attempt in range(1, max_attempts + 1):
			try:
				return await client.post(
					url=self.Config.URLs.CHECKIN,
					headers=headers,
					timeout=30,
				)
			except httpx.TimeoutException as exc:
				last_error = exc
				if attempt < max_attempts:
					logger.warning(
						message=f'签到请求超时，{delay_seconds} 秒后重试 {attempt + 1}/{max_attempts}',
						account_name=account_name,
					)
					await asyncio.sleep(delay_seconds)

		assert last_error is not None
		raise last_error

	async def _get_waf_cookies_with_playwright(self, account_name: str) -> dict[str, str] | None:
		"""
		获取 WAF cookies，浏览器超时等瞬时故障会有限重试

		Args:
		    account_name: 账号名称（用于日志）

		Returns:
		    dict[str, str] | None: WAF cookies 字典，重试耗尽仍失败返回 None
		"""
		max_attempts = self.Config.Retry.WAF_MAX_ATTEMPTS
		delay_seconds = self.Config.Retry.DELAY_SECONDS

		for attempt in range(1, max_attempts + 1):
			waf_cookies = await self._fetch_waf_cookies_once(account_name)
			if waf_cookies:
				return waf_cookies

			if attempt < max_attempts:
				logger.warning(
					message=f'获取 WAF cookies 失败，{delay_seconds} 秒后重试 {attempt + 1}/{max_attempts}',
					account_name=account_name,
				)
				await asyncio.sleep(delay_seconds)

		return None

	async def _fetch_waf_cookies_once(self, account_name: str) -> dict[str, str] | None:
		"""
		使用 Playwright 获取 WAF cookies（无痕模式），单次尝试

		Args:
		    account_name: 账号名称（用于日志）

		Returns:
		    dict[str, str] | None: WAF cookies 字典，失败返回 None
		"""
		logger.processing('正在启动浏览器获取 WAF cookies...', account_name)

		browser = None
		context = None

		try:
			async with async_playwright() as p:
				# 检测是否在 CI 环境中运行
				is_ci = any(
					os.getenv(env) == 'true'
					for env in (self.Config.Env.CI, self.Config.Env.GITHUB_ACTIONS)
				)  # fmt: skip

				# 使用标准无痕模式，避免临时目录的潜在问题
				# CI 环境使用 headless 模式，本地开发可以看到浏览器界面
				browser = await p.chromium.launch(
					headless=is_ci,
					args=self.Config.Browser.ARGS,
				)

				context = await browser.new_context(
					user_agent=' '.join(self.Config.Browser.USER_AGENT_PARTS),
					viewport={'width': 1920, 'height': 1080},
				)

				page = await context.new_page()

				logger.processing('步骤 1: 访问登录页面获取初始 cookies...', account_name)

				await page.goto(self.Config.URLs.LOGIN, wait_until='networkidle')

				try:
					await page.wait_for_function('document.readyState === "complete"', timeout=5000)
				except Exception:
					await page.wait_for_timeout(3000)

				cookies = await context.cookies()

				waf_cookies = {}
				for cookie in cookies:
					cookie_name = cookie.get('name')
					cookie_value = cookie.get('value')
					if cookie_name in self.Config.WAF.COOKIE_NAMES and cookie_value is not None:
						waf_cookies[cookie_name] = cookie_value

				logger.info(f'步骤 1 后获得 {len(waf_cookies)} 个 WAF cookies', account_name)

				missing_cookies = [c for c in self.Config.WAF.COOKIE_NAMES if c not in waf_cookies]

				if missing_cookies:
					logger.error(f'缺少 WAF cookies: {missing_cookies}', account_name)
					return None

				logger.success('成功获取所有 WAF cookies', account_name)

				return waf_cookies

		except Exception as e:
			logger.error(
				message=f'获取 WAF cookies 时发生错误：{e}',
				account_name=account_name,
				exc_info=True,
			)
			return None

		finally:
			# 确保资源被正确释放
			if context:
				try:
					await context.close()
				except Exception:
					pass
			if browser:
				try:
					await browser.close()
				except Exception:
					pass

	async def _get_user_info(
		self,
		client,
		headers: dict[str, str],
		privacy_handler: PrivacyHandler,
	) -> dict[str, Any]:
		"""
		获取用户信息

		Args:
		    client: httpx 客户端
		    headers: 请求头
		    privacy_handler: 隐私处理器

		Returns:
		    dict[str, Any]: 用户信息字典
		"""
		try:
			response = await client.get(
				url=self.Config.URLs.USER_INFO,
				headers=headers,
				timeout=30,
			)

			# 认证失败需要向上层暴露稳定原因，才能只在 session 失效时刷新。
			if response.status_code in (401, 403):
				return {
					'success': False,
					'error': f'获取用户信息失败：HTTP {response.status_code}',
					'reason': 'authentication_failed',
				}

			if response.status_code != 200:
				return {
					'success': False,
					'error': f'获取用户信息失败：HTTP {response.status_code}',
					'reason': 'http_error',
				}

			# JSON 解析失败
			try:
				data = response.json()
			except json.JSONDecodeError:
				return {
					'success': False,
					'error': '获取用户信息失败：无效的 JSON 响应',
					'reason': 'invalid_response',
				}

			# API 响应失败
			if not data.get('success'):
				message = str(data.get('message', '获取用户信息失败：API 错误'))
				return {
					'success': False,
					'error': message,
					'reason': self._classify_user_info_failure(message),
				}

			# 成功获取用户信息
			user_data = data.get('data', {})
			quota = round(user_data.get('quota', 0) / 500000, 2)
			used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
			return {
				'success': True,
				'quota': quota,
				'used_quota': used_quota,
				'display': privacy_handler.get_safe_balance_display(quota=quota, used=used_quota),
			}

		except httpx.TimeoutException:
			return {
				'success': False,
				'error': '获取用户信息失败：请求超时',
				'reason': 'timeout',
			}

		except httpx.RequestError:
			return {
				'success': False,
				'error': '获取用户信息失败：网络错误',
				'reason': 'network_error',
			}

		except Exception as e:
			return {
				'success': False,
				'error': f'获取用户信息失败：{str(e)[:50]}...',
				'reason': 'unexpected_error',
			}

	def _classify_user_info_failure(self, message: str) -> str:
		lowered = message.lower()
		if any(marker in lowered for marker in self.Config.Authentication.ERROR_MARKERS):
			return 'authentication_failed'
		return 'api_error'

	@staticmethod
	def _parse_cookies(cookies_data) -> dict[str, str]:
		"""
		解析 cookies 数据

		Args:
		    cookies_data: cookies 数据（字符串或字典格式）

		Returns:
		    dict[str, str]: cookies 字典
		"""
		# 已经是字典格式
		if isinstance(cookies_data, dict):
			return cookies_data

		# 不是字符串格式
		if not isinstance(cookies_data, str):
			return {}

		# 解析字符串格式的 cookies
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			# cookie 格式不正确
			if '=' not in cookie:
				continue

			key, value = cookie.strip().split('=', 1)
			cookies_dict[key] = value

		return cookies_dict
