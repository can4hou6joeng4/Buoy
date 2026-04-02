from dataclasses import dataclass


@dataclass
class AccountResult:
	"""单个账号的处理结果"""

	# 账号名称
	name: str

	# 处理状态：success 或 failed
	status: str

	# 当前余额，成功时才有
	quota: float | None = None

	# 已使用余额，成功时才有
	used: float | None = None

	# 余额是否发生变化，None 表示获取余额失败无法判断，False 表示未变化或无历史数据
	balance_changed: bool | None = None

	# 变动前余额（仅 balance_changed=True 时有值）
	prev_quota: float | None = None

	# 变动前已使用（仅 balance_changed=True 时有值）
	prev_used: float | None = None

	# 额度变化值（当前额度 - 变动前额度）
	quota_delta: float | None = None

	# 已使用变化值（当前已使用 - 变动前已使用）
	used_delta: float | None = None

	# 带正负号的额度变化展示值
	quota_delta_display: str | None = None

	# 带正负号的已使用变化展示值
	used_delta_display: str | None = None

	# 错误信息，失败时才有
	error: str | None = None
