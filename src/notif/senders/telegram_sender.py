import html
import re

import httpx

from notif.models import TelegramConfig
from notif.telegram_formatting import escape_rich_markdown


class TelegramSender:
	def __init__(self, config: TelegramConfig):
		"""
		初始化 Telegram 发送器

		Args:
			config: Telegram 配置
		"""
		self.config = config

	async def send(self, title: str | None, content: str, context_data: dict | None = None):
		"""
		发送 Telegram 消息

		Args:
			title: 消息标题
			content: 消息内容
			context_data: 模板渲染的上下文数据

		Raises:
			Exception: 当 HTTP 响应状态码不是 2xx 时抛出异常
		"""
		settings = self.config.platform_settings or {}
		message_type = settings.get('message_type', 'HTML')
		is_rich = message_type == 'RichMarkdown'

		if is_rich:
			message = f'**{escape_rich_markdown(title)}**\n\n{content}' if title else content
			data = {
				'chat_id': self.config.chat_id,
				'rich_message': {'markdown': message, 'skip_entity_detection': True},
			}
			method = 'sendRichMessage'
		else:
			message = self._escape_html(content) if message_type == 'HTML' else content
			if title:
				if message_type == 'HTML':
					formatted_title = f'<b>{html.escape(title)}</b>'
				elif message_type in ('Markdown', 'MarkdownV2'):
					escaped_title = re.sub(r'([\\_*\[\]()~`>#+=|{}.!-])', r'\\\1', title)
					formatted_title = f'*{escaped_title}*'
				else:
					formatted_title = title
				message = f'{formatted_title}\n\n{message}'
			data = {'chat_id': self.config.chat_id, 'text': message, 'parse_mode': message_type}
			method = 'sendMessage'

		for param in ('disable_notification', 'protect_content'):
			if param in settings:
				data[param] = settings[param]
		if is_rich:
			if 'reply_to_message_id' in settings:
				data['reply_parameters'] = {'message_id': settings['reply_to_message_id']}
		else:
			for param in ('disable_web_page_preview', 'reply_to_message_id'):
				if param in settings:
					data[param] = settings[param]

		api_url = f'https://api.telegram.org/bot{self.config.bot_token}/{method}'

		# 发送请求（含解析失败自动降级）
		async with httpx.AsyncClient(timeout=30.0) as client:
			response = await client.post(api_url, json=data)

			if not is_rich and not response.is_success and "can't parse entities" in response.text:
				# 解析模式不匹配，去掉格式标签后以纯文本重发
				plain_text = html.unescape(re.sub(r'<[^>]+>', '', message)) if message_type == 'HTML' else message
				data['text'] = plain_text
				data.pop('parse_mode', None)
				response = await client.post(api_url, json=data)

			# 检查最终响应状态码
			if not response.is_success:
				raise Exception(
					f'Telegram 推送失败，HTTP 状态码：{response.status_code}，响应内容：{response.text[:200]}'
				)

	def _escape_html(self, text: str) -> str:
		"""
		转义 HTML 特殊字符，同时保留 Telegram 支持的 HTML 标签

		Telegram Bot API 的 HTML 模式要求文本中的 <, >, & 必须转义，
		但 <b>, <i>, <code> 等合法标签需要保留。
		"""
		parts = re.split(r'(<[^>]+>)', text)
		result = []
		for part in parts:
			if part.startswith('<') and part.endswith('>'):
				result.append(part)
			else:
				result.append(html.escape(part))
		return ''.join(result)
