import html
import re

from core.models import AccountResult


def escape_rich_markdown(value: str) -> str:
	"""将动态文本限制为单行，并转义 Rich Markdown 和内联 HTML。"""
	text = html.escape(' '.join(value.splitlines()), quote=False)
	return re.sub(r'([\\`*_{}\[\]()#+.!|~=$-])', r'\\\1', text)


def build_balance_table(accounts: list[AccountResult], total_quota: str, total_used: str) -> str:
	"""生成三列表格；美元符号转义，避免被 Rich Markdown 当作公式。"""
	lines = ['| 👤 账号 | 💵 目前额度 | 📊 已用额度 |', '| :--- | ---: | ---: |']
	for account in accounts:
		quota = account.quota_display.replace('$', r'\$')
		used = account.used_display.replace('$', r'\$')
		lines.append(f'| {escape_rich_markdown(account.name)} | {quota} | {used} |')
	total_quota = total_quota.replace('$', r'\$')
	total_used = total_used.replace('$', r'\$')
	lines.append(f'| **🧮 合计** | **{total_quota}** | **{total_used}** |')
	return '\n'.join(lines)
