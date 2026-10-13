from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.models import AccountResult
from notif import NotificationKit
from notif.models import NotificationHandler, NotificationTemplate
from tests.tools.data_builders import build_account_result, build_notification_data


class TestNotificationKit:
	"""测试 NotificationKit 类"""

	@pytest.mark.asyncio
	async def test_push_raw_message_returns_true_when_handler_sends(self):
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
	async def test_push_raw_message_returns_false_without_handlers(self):
		kit = NotificationKit()
		kit._handlers = []

		result = await kit.push_raw_message('Title', 'Content')

		assert result is False

	@pytest.mark.parametrize(
		'env_key,env_value,expected_config_attr,expected_value_or_key',
		[
			# Email 平台（字典格式）
			(
				'EMAIL_NOTIF_CONFIG',
				'{"user": "test@example.com", "pass": "password", "to": "recipient@example.com"}',
				'email_config',
				('user', 'test@example.com'),
			),
			# Bark 平台（字典格式）
			(
				'BARK_NOTIF_CONFIG',
				'{"server_url": "https://api.day.app", "device_key": "test_key"}',
				'bark_config',
				('device_key', 'test_key'),
			),
			# PushPlus 平台（字符串格式）
			('PUSHPLUS_NOTIF_CONFIG', 'test_token_123', 'pushplus_config', ('token', 'test_token_123')),
			# ServerPush 平台（字符串格式）
			('SERVERPUSH_NOTIF_CONFIG', 'test_send_key_456', 'serverpush_config', ('send_key', 'test_send_key_456')),
			# DingTalk 平台（字符串格式）
			(
				'DINGTALK_NOTIF_CONFIG',
				'https://oapi.dingtalk.com/robot/send?access_token=test',
				'dingtalk_config',
				('webhook', 'https://oapi.dingtalk.com/robot/send?access_token=test'),
			),
			# Feishu 平台（字典格式，包含模板）
			(
				'FEISHU_NOTIF_CONFIG',
				'{"webhook": "https://open.feishu.cn/hook", "template": {"title": "自定义标题", "content": "自定义内容"}}',
				'feishu_config',
				('template.title', '自定义标题'),
			),
			# WeCom 平台（字典格式）
			(
				'WECOM_NOTIF_CONFIG',
				'{"webhook": "https://qyapi.weixin.qq.com/hook"}',
				'wecom_config',
				('webhook', 'https://qyapi.weixin.qq.com/hook'),
			),
		],
	)
	def test_config_parsing_all_platforms(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
		env_key: str,
		env_value: str,
		expected_config_attr: str,
		expected_value_or_key: tuple[str, str],
	) -> None:
		"""测试所有平台的配置解析（字典和字符串格式）"""
		# clean_notification_env fixture 用于清理环境变量，无需在函数体内显式使用
		monkeypatch.setenv(env_key, env_value)
		kit = NotificationKit()

		config = getattr(kit, expected_config_attr)
		assert config is not None, f'{expected_config_attr} 应该不为 None'

		# 验证配置值
		attr_path, expected_value = expected_value_or_key
		if '.' in attr_path:
			# 嵌套属性（如 template.title）
			parts = attr_path.split('.')
			obj = config
			for part in parts:
				obj = getattr(obj, part)
			assert obj == expected_value, f'{attr_path} 的值不匹配'
		else:
			# 普通属性
			actual_value = getattr(config, attr_path)
			assert actual_value == expected_value, f'{attr_path} 的值不匹配'

	def test_template_merging_and_rendering(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
	) -> None:
		"""测试默认模板与用户模板的合并和渲染"""
		# clean_notification_env fixture 用于清理环境变量，无需在函数体内显式使用
		# 测试完全自定义模板
		monkeypatch.setenv(
			'PUSHPLUS_NOTIF_CONFIG',
			'{"token": "test", "template": {"title": "自定义标题", "content": "自定义内容"}}',
		)
		kit = NotificationKit()
		assert kit.pushplus_config is not None
		assert kit.pushplus_config.template is not None
		assert kit.pushplus_config.template.title == '自定义标题'
		assert kit.pushplus_config.template.content == '自定义内容'

		# 测试只自定义 content（title 使用默认值）
		monkeypatch.setenv(
			'PUSHPLUS_NOTIF_CONFIG',
			'{"token": "test", "template": {"content": "只有内容"}}',
		)
		kit = NotificationKit()
		assert kit.pushplus_config is not None
		assert kit.pushplus_config.template is not None
		assert kit.pushplus_config.template.content == '只有内容'

		# 测试 NotificationTemplate.from_value() 的各种格式
		# 1. 字典格式
		template_dict = NotificationTemplate.from_value({'title': '标题', 'content': '内容'})
		assert template_dict is not None
		assert template_dict.title == '标题'
		assert template_dict.content == '内容'

		# 2. 字符串格式（向后兼容）
		template_str = NotificationTemplate.from_value('旧格式内容')
		assert template_str is not None
		assert template_str.title == 'AnyRouter 签到提醒'
		assert template_str.content == '旧格式内容'

		# 3. None 值
		template_none = NotificationTemplate.from_value(None)
		assert template_none is None

		# 4. 空 title
		template_no_title = NotificationTemplate.from_value({'content': '只有内容'})
		assert template_no_title is not None
		assert template_no_title.title is None
		assert template_no_title.content == '只有内容'

		# 测试模板渲染（通过 NotificationKit）
		kit_for_render = NotificationKit()
		template_with_vars = NotificationTemplate(
			title='{% if all_success %}全部成功{% else %}部分成功{% endif %}',
			content='共 {{ stats.total_count }} 个账号, 成功 {{ stats.success_count }} 个',
		)

		# 构造上下文数据（使用对象而不是字典，因为 stencil 不支持字典的点访问）
		Stats = type('Stats', (), {'total_count': 10, 'success_count': 8})
		context_data = {
			'all_success': True,
			'stats': Stats(),
		}

		# 通过 _render_template 测试渲染
		rendered_title, rendered_content = kit_for_render._render_template(template_with_vars, context_data)
		assert rendered_title == '全部成功'
		assert rendered_content == '共 10 个账号, 成功 8 个'

	@pytest.mark.parametrize(
		'accounts,expected_flags',
		[
			# 全部成功
			(
				[
					build_account_result(name='账号 1', quota=25.0, used=5.0, balance_changed=False),
					build_account_result(name='账号 2', quota=30.0, used=10.0, balance_changed=False),
				],
				{
					'has_success': True,
					'has_failed': False,
					'all_success': True,
					'all_failed': False,
					'partial_success': False,
				},
			),
			# 全部失败
			(
				[
					build_account_result(name='账号 1', status='failed', error='错误 1'),
					build_account_result(name='账号 2', status='failed', error='错误 2'),
				],
				{
					'has_success': False,
					'has_failed': True,
					'all_success': False,
					'all_failed': True,
					'partial_success': False,
				},
			),
			# 部分成功
			(
				[
					build_account_result(name='账号 1', quota=25.0, used=5.0),
					build_account_result(name='账号 2', status='failed', error='错误'),
				],
				{
					'has_success': True,
					'has_failed': True,
					'all_success': False,
					'all_failed': False,
					'partial_success': True,
				},
			),
			# 余额变化测试
			(
				[
					build_account_result(name='账号 1', balance_changed=True),
					build_account_result(name='账号 2', balance_changed=False),
				],
				{
					'has_balance_changed': True,
					'has_balance_unchanged': True,
					'all_balance_changed': False,
					'all_balance_unchanged': False,
				},
			),
			# 存在失败账号时，即使成功账号都变化，也不应视为全部完成
			(
				[
					build_account_result(name='账号 1', balance_changed=True),
					build_account_result(name='账号 2', status='failed', error='错误'),
				],
				{
					'has_balance_changed': True,
					'has_failed': True,
					'all_balance_changed': False,
				},
			),
		],
	)
	def test_context_data_building(
		self,
		clean_notification_env: None,
		accounts: list[Any],
		expected_flags: dict[str, bool],
	) -> None:
		"""测试上下文数据构建（用于模板渲染）"""
		# clean_notification_env fixture 用于清理环境变量，无需在函数体内显式使用
		kit = NotificationKit()
		data = build_notification_data(accounts)
		context = kit._build_context_data(data)

		# 验证所有预期的标志
		for flag_name, expected_value in expected_flags.items():
			actual_value = context.get(flag_name)
			assert actual_value == expected_value, f'{flag_name} 应该是 {expected_value}, 实际是 {actual_value}'

	def test_balance_change_template_rendering(
		self,
		clean_notification_env: None,
	) -> None:
		"""测试余额变动模板可正确访问变动前后金额与 delta 展示值"""
		kit = NotificationKit()
		data = build_notification_data([
			build_account_result(
				name='账号 1',
				quota=30.0,
				used=10.0,
				balance_changed=True,
				prev_quota=25.0,
				prev_used=5.0,
				quota_delta=5.0,
				used_delta=5.0,
				quota_delta_display='+5.0',
				used_delta_display='+5.0',
			)
		])
		context = kit._build_context_data(data)
		template = NotificationTemplate(
			title='{% if has_balance_changed %}AnyRouter 余额变动提醒{% else %}AnyRouter 签到提醒{% endif %}',
			content='{% for account in balance_changed_accounts %}{{ account.name }}|{{ account.prev_quota }}|{{ account.quota }}|{{ account.quota_delta_display }}|{{ account.used_delta_display }}{% endfor %}',
		)

		rendered_title, rendered_content = kit._render_template(template, context)

		assert rendered_title == 'AnyRouter 余额变动提醒'
		assert rendered_content == '账号 1|25.0|30.0|+5.0|+5.0'

	def test_change_account_groups_for_template(
		self,
		clean_notification_env: None,
	) -> None:
		"""测试模板上下文中可区分总额度变化与已使用变化的账号分组"""
		kit = NotificationKit()
		data = build_notification_data([
			build_account_result(
				name='额度变化账号',
				quota=35.0,
				used=5.0,
				balance_changed=True,
				prev_quota=30.0,
				prev_used=5.0,
				quota_delta=5.0,
				used_delta=0.0,
				quota_delta_display='+5.0',
				used_delta_display='+0.0',
			),
			build_account_result(
				name='已用变化账号',
				quota=35.0,
				used=8.0,
				balance_changed=True,
				prev_quota=35.0,
				prev_used=5.0,
				quota_delta=0.0,
				used_delta=3.0,
				quota_delta_display='+0.0',
				used_delta_display='+3.0',
			),
		])

		context = kit._build_context_data(data)

		assert context['has_balance_changed'] is True
		assert context['has_quota_changed'] is True
		assert context['has_used_changed'] is True
		assert [acc.name for acc in context['quota_changed_accounts']] == ['额度变化账号']
		assert [acc.name for acc in context['used_changed_accounts']] == ['已用变化账号']

	def test_default_telegram_template_prioritizes_failure_and_marks_partial_totals(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
	) -> None:
		"""失败优先显示异常标题，并明确标注部分额度合计。"""
		monkeypatch.setenv(
			'TELEGRAM_NOTIF_CONFIG',
			'{"bot_token": "test_token", "chat_id": "123456"}',
		)
		kit = NotificationKit()
		assert kit.telegram_config is not None
		assert kit.telegram_config.template is not None

		data = build_notification_data([
			build_account_result(
				name='账号 A',
				quota=30.0,
				used=8.0,
				balance_changed=True,
				prev_quota=25.0,
				prev_used=5.0,
				quota_delta=5.0,
				used_delta=3.0,
				quota_delta_display='+5.0',
				used_delta_display='+3.0',
			),
			build_account_result(
				name='账号 B',
				quota=20.0,
				used=2.0,
				balance_changed=False,
			),
			build_account_result(name='账号 C', status='failed', error='登录失效'),
		])
		context = kit._build_context_data(data)

		rendered_title, rendered_content = kit._render_template(kit.telegram_config.template, context)

		assert rendered_title == '❌ AnyRouter 签到异常'
		assert r'| **合计** | **\$50.00** | **\$10.00** |' in rendered_content
		assert '仅汇总已获取额度的 2/3 个账号；未获取：账号 C。' in rendered_content
		assert '**❌ 账号 C：**登录失效' in rendered_content
		assert r'| 账号 A | \$30.00 | \$8.00 |' in rendered_content
		assert r'| 账号 B | \$20.00 | \$2.00 |' in rendered_content
		assert '| 账号 C | 暂不可用 | 暂不可用 |' in rendered_content
		assert '+5.0' not in rendered_content
		assert '+3.0' not in rendered_content

	def test_default_telegram_template_shows_account_balances_and_totals_for_nine_accounts(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
	) -> None:
		"""9 个账号逐一展示两项额度，底部合计，不混入消耗或变化明细。"""
		monkeypatch.setenv(
			'TELEGRAM_NOTIF_CONFIG',
			'{"bot_token": "test_token", "chat_id": "123456"}',
		)
		kit = NotificationKit()
		assert kit.telegram_config is not None
		assert kit.telegram_config.template is not None

		accounts = [
			build_account_result(
				name=f'账号 {index}',
				quota=50.0,
				used=5.0,
				balance_changed=True,
				prev_quota=25.0,
				prev_used=5.0,
				quota_delta=25.0,
				used_delta=0.0,
				quota_delta_display='+25.0',
				used_delta_display='+0.0',
			)
			for index in range(1, 10)
		]
		context = kit._build_context_data(
			build_notification_data(accounts, timestamp='2026-09-21 10:00:00', timezone='CST')
		)

		rendered_title, rendered_content = kit._render_template(kit.telegram_config.template, context)
		content_lines = rendered_content.splitlines()

		assert rendered_title == '💰 AnyRouter 额度汇总'
		assert content_lines == [
			'| 账号 | 目前额度 | 已用额度 |',
			'| :--- | ---: | ---: |',
			*[rf'| 账号 {index} | \$50.00 | \$5.00 |' for index in range(1, 10)],
			r'| **合计** | **\$450.00** | **\$45.00** |',
		]
		assert '+25.0' not in rendered_content
		assert '2026-09-21' not in rendered_content

	def test_default_telegram_template_omits_uniform_quota_and_used_changes(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
	) -> None:
		"""共同额度变化与消耗不再显示，只展示累计额度。"""
		monkeypatch.setenv(
			'TELEGRAM_NOTIF_CONFIG',
			'{"bot_token": "test_token", "chat_id": "123456"}',
		)
		kit = NotificationKit()
		assert kit.telegram_config is not None
		assert kit.telegram_config.template is not None

		accounts = [
			build_account_result(
				name=f'账号 {index}',
				quota=30.0,
				used=8.0,
				balance_changed=True,
				prev_quota=25.0,
				prev_used=5.0,
				quota_delta=5.0,
				used_delta=3.0,
				quota_delta_display='+5.0',
				used_delta_display='+3.0',
			)
			for index in range(1, 4)
		]
		context = kit._build_context_data(build_notification_data(accounts))

		rendered_title, rendered_content = kit._render_template(kit.telegram_config.template, context)
		assert rendered_title == '💰 AnyRouter 额度汇总'
		assert rendered_content.splitlines() == [
			'| 账号 | 目前额度 | 已用额度 |',
			'| :--- | ---: | ---: |',
			*[rf'| 账号 {index} | \$30.00 | \$8.00 |' for index in range(1, 4)],
			r'| **合计** | **\$90.00** | **\$24.00** |',
		]

	def test_default_telegram_template_omits_non_uniform_changes(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
	) -> None:
		"""变化模式不同也只展示当前两项金额，账号与合计均保留两位小数。"""
		monkeypatch.setenv(
			'TELEGRAM_NOTIF_CONFIG',
			'{"bot_token": "test_token", "chat_id": "123456"}',
		)
		kit = NotificationKit()
		assert kit.telegram_config is not None
		assert kit.telegram_config.template is not None

		data = build_notification_data([
			build_account_result(
				name='账号 A',
				quota=30.15,
				used=8.42,
				balance_changed=True,
				quota_delta=5.0,
				used_delta=3.0,
				quota_delta_display='+5.0',
				used_delta_display='+3.0',
			),
			build_account_result(
				name='账号 B',
				quota=35.27,
				used=5.33,
				balance_changed=True,
				quota_delta=10.0,
				used_delta=0.0,
				quota_delta_display='+10.0',
				used_delta_display='+0.0',
			),
		])
		context = kit._build_context_data(data)

		_, rendered_content = kit._render_template(kit.telegram_config.template, context)

		assert rendered_content.splitlines() == [
			'| 账号 | 目前额度 | 已用额度 |',
			'| :--- | ---: | ---: |',
			r'| 账号 A | \$30.15 | \$8.42 |',
			r'| 账号 B | \$35.27 | \$5.33 |',
			r'| **合计** | **\$65.42** | **\$13.75** |',
		]

	@pytest.mark.parametrize('quota,used', [(10.0, 1.0), (0.0, 0.0)])
	def test_default_telegram_template_keeps_first_seen_and_zero_balances_compact(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
		quota: float,
		used: float,
	) -> None:
		"""新增账号和真实零余额均展示两项额度，仍保持简洁。"""
		monkeypatch.setenv(
			'TELEGRAM_NOTIF_CONFIG',
			'{"bot_token": "test_token", "chat_id": "123456"}',
		)
		kit = NotificationKit()
		assert kit.telegram_config is not None
		assert kit.telegram_config.template is not None

		data = build_notification_data([
			build_account_result(
				name='账号 D',
				quota=quota,
				used=used,
				balance_changed=False,
				first_seen=True,
			),
		])
		context = kit._build_context_data(data)

		rendered_title, rendered_content = kit._render_template(kit.telegram_config.template, context)

		assert rendered_title == '💰 AnyRouter 额度汇总'
		assert context['has_first_seen'] is True
		assert context['first_seen_accounts'][0].name == '账号 D'
		assert rendered_content.splitlines() == [
			'| 账号 | 目前额度 | 已用额度 |',
			'| :--- | ---: | ---: |',
			rf'| 账号 D | \${quota:.2f} | \${used:.2f} |',
			rf'| **合计** | **\${quota:.2f}** | **\${used:.2f}** |',
		]

	@pytest.mark.parametrize('quota,used', [(None, None), (None, 5.0), (25.0, None)])
	def test_default_telegram_template_does_not_treat_missing_balances_as_zero(
		self,
		monkeypatch: pytest.MonkeyPatch,
		clean_notification_env: None,
		quota: float | None,
		used: float | None,
	) -> None:
		"""签到成功但余额缺失时显示不可用，避免生成误导的零合计。"""
		monkeypatch.setenv('TELEGRAM_NOTIF_CONFIG', '{"bot_token": "test_token", "chat_id": "123456"}')
		kit = NotificationKit()
		assert kit.telegram_config is not None
		assert kit.telegram_config.template is not None
		data = build_notification_data([AccountResult(name='账号 A', status='success', quota=quota, used=used)])

		_, rendered_content = kit._render_template(kit.telegram_config.template, kit._build_context_data(data))

		assert rendered_content.splitlines() == [
			'| 账号 | 目前额度 | 已用额度 |',
			'| :--- | ---: | ---: |',
			'| 账号 A | '
			+ (rf'\${quota:.2f}' if quota is not None else '暂不可用')
			+ ' | '
			+ (rf'\${used:.2f}' if used is not None else '暂不可用')
			+ ' |',
			'| **合计** | **暂不可用** | **暂不可用** |',
			'',
			'⚠️ 仅汇总已获取额度的 0/1 个账号；未获取：账号 A。',
		]
