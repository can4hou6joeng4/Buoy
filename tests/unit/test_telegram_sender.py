import json
from unittest.mock import patch

import httpx
import pytest

from core.models import AccountResult
from notif import NotificationKit
from notif.models import NotificationTemplate, TelegramConfig
from notif.senders import TelegramSender
from tests.tools.data_builders import build_notification_data


@pytest.mark.asyncio
async def test_rich_markdown_sends_native_table_with_supported_options():
	config = TelegramConfig(
		bot_token='test_token',
		chat_id='123456',
		platform_settings={
			'message_type': 'RichMarkdown',
			'disable_notification': True,
			'protect_content': True,
			'reply_to_message_id': 42,
			'disable_web_page_preview': True,
		},
	)
	content = '| 账号 | 目前额度 | 已用额度 |\n| :--- | ---: | ---: |\n' + r'| A | \$10.00 | \$1.00 |'
	with patch('httpx.AsyncClient.post', return_value=httpx.Response(200, json={'ok': True})) as post:
		await TelegramSender(config).send('额度_<b>汇总</b>', content)
	post.assert_awaited_once_with(
		'https://api.telegram.org/bottest_token/sendRichMessage',
		json={
			'chat_id': '123456',
			'rich_message': {
				'markdown': '**额度\\_&lt;b&gt;汇总&lt;/b&gt;**\n\n' + content,
				'skip_entity_detection': True,
			},
			'disable_notification': True,
			'protect_content': True,
			'reply_parameters': {'message_id': 42},
		},
	)


@pytest.mark.asyncio
@pytest.mark.parametrize('title', [None, ''])
async def test_rich_markdown_without_title_preserves_content(title):
	config = TelegramConfig('test_token', '123456', {'message_type': 'RichMarkdown'})
	with patch('httpx.AsyncClient.post', return_value=httpx.Response(200, json={'ok': True})) as post:
		await TelegramSender(config).send(title, '**内容**')
	assert post.call_args.kwargs['json']['rich_message']['markdown'] == '**内容**'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 401, 429, 500])
async def test_rich_errors_are_reported_without_blind_resend(status):
	config = TelegramConfig('test_token', '123456', {'message_type': 'RichMarkdown'})
	with patch('httpx.AsyncClient.post', return_value=httpx.Response(status, text="can't parse entities")) as post:
		with pytest.raises(Exception, match=f'Telegram 推送失败.*{status}'):
			await TelegramSender(config).send('标题', '表格')
	assert post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'mode,title,expected_title',
	[
		('HTML', 'A&B<foo>', '<b>A&amp;B&lt;foo&gt;</b>'),
		('MarkdownV2', 'A_B.!', r'*A\_B\.\!*'),
	],
)
async def test_legacy_modes_keep_send_message_and_correct_title_markup(mode, title, expected_title):
	config = TelegramConfig('test_token', '123456', {'message_type': mode})
	with patch('httpx.AsyncClient.post', return_value=httpx.Response(200, json={'ok': True})) as post:
		await TelegramSender(config).send(title, '内容')
	post.assert_awaited_once_with(
		'https://api.telegram.org/bottest_token/sendMessage',
		json={'chat_id': '123456', 'text': expected_title + '\n\n内容', 'parse_mode': mode},
	)


@pytest.mark.asyncio
async def test_legacy_html_parse_failure_still_retries_readable_plain_text():
	config = TelegramConfig('test_token', '123456', {'message_type': 'HTML'})
	with patch(
		'httpx.AsyncClient.post',
		side_effect=[
			httpx.Response(400, text="can't parse entities"),
			httpx.Response(200, json={'ok': True}),
		],
	) as post:
		await TelegramSender(config).send('A&B', '<b>账号</b> & 余额')
	assert post.await_count == 2
	assert post.call_args.kwargs['json'] == {'chat_id': '123456', 'text': 'A&B\n\n账号 & 余额'}


@pytest.mark.parametrize(
	'overrides,expected_mode,expected_content',
	[
		({}, 'RichMarkdown', '{{ telegram_balance_table }}'),
		({'template': {'content': '<b>旧内容</b>'}}, 'HTML', '<b>旧内容</b>'),
		({'template': '旧模板'}, 'HTML', '旧模板'),
		({'template': {'title': '标题'}}, 'HTML', '<b>{{ account.name }}</b>'),
		({'platform_settings': {'message_type': 'HTML'}}, 'HTML', '<b>{{ account.name }}</b>'),
		(
			{'platform_settings': {'message_type': 'MarkdownV2'}, 'template': {'content': '*旧内容*'}},
			'MarkdownV2',
			'*旧内容*',
		),
		(
			{'platform_settings': {'message_type': 'RichMarkdown'}, 'template': {'title': '新标题'}},
			'RichMarkdown',
			'{{ telegram_balance_table }}',
		),
	],
)
def test_format_defaults_preserve_existing_custom_templates(
	monkeypatch, clean_notification_env, overrides, expected_mode, expected_content
):
	monkeypatch.setenv('TELEGRAM_NOTIF_CONFIG', json.dumps({'bot_token': 'test', 'chat_id': '123', **overrides}))
	config = NotificationKit().telegram_config
	assert config.platform_settings['message_type'] == expected_mode
	assert expected_content in config.template.content


def test_dynamic_names_cannot_break_table_or_inject_formatting(monkeypatch, clean_notification_env):
	monkeypatch.setenv('TELEGRAM_NOTIF_CONFIG', '{"bot_token":"test","chat_id":"123"}')
	kit = NotificationKit()
	name = r'A|B_*<b>&$\name' + '\nC'
	account = AccountResult(name=name, status='success', quota=10.0, used=0.0)
	context = kit._build_context_data(build_notification_data([account]))
	_, content = kit._render_template(kit.telegram_config.template, context)
	assert content.splitlines() == [
		'| 账号 | 目前额度 | 已用额度 |',
		'| :--- | ---: | ---: |',
		r'| A\|B\_\*&lt;b&gt;&amp;\$\\name C | \$10.00 | \$0.00 |',
		r'| **合计** | **\$10.00** | **\$0.00** |',
	]
	assert account.name == name
	assert context['accounts'][0].name == name


@pytest.mark.parametrize('template', [None, {'title': '旧标题', 'content': '<b>旧内容</b>'}])
def test_explicit_table_layout_overrides_legacy_format_and_preserves_credentials_and_options(
	monkeypatch, clean_notification_env, template
):
	monkeypatch.setenv('TELEGRAM_LAYOUT', 'table')
	monkeypatch.setenv(
		'TELEGRAM_NOTIF_CONFIG',
		json.dumps({
			'bot_token': 'test_token',
			'chat_id': '123456',
			'platform_settings': {'message_type': 'HTML', 'disable_notification': True, 'protect_content': True},
			'template': template,
		}),
	)
	config = NotificationKit().telegram_config
	assert config.bot_token == 'test_token'
	assert config.chat_id == '123456'
	assert config.platform_settings['message_type'] == 'RichMarkdown'
	assert config.platform_settings['disable_notification'] is True
	assert config.platform_settings['protect_content'] is True
	assert '{{ telegram_balance_table }}' in config.template.content
	assert '旧内容' not in config.template.content


def test_template_newline_escape_does_not_rewrite_dynamic_text(clean_notification_env):
	kit = NotificationKit()
	template = NotificationTemplate(title=None, content=r'第一行\n{{ name }}')
	_, content = kit._render_template(template, {'name': r'A\name'})
	assert content == '第一行\n' + r'A\name'
