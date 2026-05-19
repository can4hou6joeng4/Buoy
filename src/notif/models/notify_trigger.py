from enum import Enum


class NotifyTrigger(Enum):
	"""通知触发器枚举"""

	# 检测到实际余额变化
	BALANCE_CHANGED = 'balance_changed'

	# 任意账号失败
	FAILED = 'failed'

	# 任意账号成功
	SUCCESS = 'success'

	# 检测到新增账号首次建立余额基线
	FIRST_SEEN = 'first_seen'

	# 总是发送
	ALWAYS = 'always'

	# 从不发送
	NEVER = 'never'
