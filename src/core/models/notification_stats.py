from dataclasses import dataclass


@dataclass
class NotificationStats:
	"""通知统计信息"""

	# 成功数量
	success_count: int

	# 失败数量（不含上游服务故障）
	failed_count: int

	# 总数量
	total_count: int

	# 因上游服务故障未能签到的数量
	upstream_fault_count: int = 0
