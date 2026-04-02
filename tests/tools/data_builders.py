from datetime import datetime
from zoneinfo import ZoneInfo

from application import Application
from core.models import AccountResult, NotificationData, NotificationStats


def build_account_result(
	name: str = '测试账号',
	status: str = 'success',
	quota: float | None = None,
	used: float | None = None,
	balance_changed: bool | None = None,
	prev_quota: float | None = None,
	prev_used: float | None = None,
	quota_delta: float | None = None,
	used_delta: float | None = None,
	quota_delta_display: str | None = None,
	used_delta_display: str | None = None,
	error: str | None = None,
) -> AccountResult:
	"""
	构建账号结果数据

	Args:
		name: 账号名称
		status: 状态（success/failed）
		quota: 总额度
		used: 已使用额度
		balance_changed: 余额是否变化
		prev_quota: 变动前额度
		prev_used: 变动前已使用
		quota_delta: 额度变化值
		used_delta: 已使用变化值
		quota_delta_display: 带符号的额度变化展示值
		used_delta_display: 带符号的已使用变化展示值
		error: 错误信息

	Returns:
		AccountResult 对象
	"""
	return AccountResult(
		name=name,
		status=status,
		quota=quota if status == 'success' and quota is not None else (25.0 if status == 'success' else None),
		used=used if status == 'success' and used is not None else (5.0 if status == 'success' else None),
		balance_changed=balance_changed,
		prev_quota=prev_quota,
		prev_used=prev_used,
		quota_delta=quota_delta,
		used_delta=used_delta,
		quota_delta_display=quota_delta_display,
		used_delta_display=used_delta_display,
		error=error,
	)


def build_notification_data(
	accounts: list[AccountResult],
	timestamp: str | None = None,
	timezone: str | None = None,
) -> NotificationData:
	"""
	构建通知数据

	Args:
		accounts: 账号结果列表
		timestamp: 时间戳
		timezone: 时区缩写

	Returns:
		NotificationData 对象
	"""
	success_count = sum(1 for acc in accounts if acc.status == 'success')
	failed_count = len(accounts) - success_count

	stats = NotificationStats(
		success_count=success_count,
		failed_count=failed_count,
		total_count=len(accounts),
	)

	# 如果没有提供 timezone，则使用默认时区生成
	if timezone is None:
		default_tz = ZoneInfo(Application.DEFAULT_TIMEZONE)
		timezone = datetime.now(default_tz).strftime('%Z')

	return NotificationData(
		accounts=accounts,
		stats=stats,
		timestamp=timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
		timezone=timezone,
	)
