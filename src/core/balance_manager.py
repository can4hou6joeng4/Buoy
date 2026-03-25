import hashlib
import json
from pathlib import Path

from tools.logger import logger


class BalanceManager:
	"""余额管理器"""

	def __init__(self, balance_hash_file: Path):
		"""
		初始化余额管理器

		Args:
			balance_hash_file: 余额 hash 文件路径
		"""
		self.balance_hash_file = balance_hash_file

	def load_balance_hash(self) -> dict[str, dict] | None:
		"""
		加载余额数据

		Returns:
			字典格式：{api_user_hash: {"hash": str, "quota": float, "used": float}}
			兼容旧格式：{api_user_hash: {"hash": str}}
			加载失败返回 None
		"""
		try:
			if self.balance_hash_file.exists():
				with open(self.balance_hash_file, 'r', encoding='utf-8') as f:
					content = f.read().strip()
					if not content:
						return None
					raw = json.loads(content)
					# 兼容旧格式：值为字符串时转换为字典
					result = {}
					for key, value in raw.items():
						if isinstance(value, str):
							result[key] = {'hash': value}
						else:
							result[key] = value
					return result

		except (OSError, IOError) as e:
			logger.warning(f'加载余额哈希失败：{e}')

		except json.JSONDecodeError as e:
			logger.warning(f'余额哈希文件格式无效：{e}')

		except Exception as e:
			logger.warning(f'加载余额哈希时发生意外错误：{e}')

		return None

	def save_balance_hash(self, balance_data_dict: dict[str, dict]):
		"""
		保存余额数据

		Args:
			balance_data_dict: 字典格式 {api_user_hash: {"hash": str, "quota": float, "used": float}}
		"""
		try:
			# 确保父目录存在
			self.balance_hash_file.parent.mkdir(parents=True, exist_ok=True)
			with open(self.balance_hash_file, 'w', encoding='utf-8') as f:
				json.dump(balance_data_dict, f, ensure_ascii=False, indent=2)

		except (OSError, IOError) as e:
			logger.warning(f'保存余额哈希失败：{e}')

		except Exception as e:
			logger.warning(f'保存余额哈希时发生意外错误：{e}')

	@staticmethod
	def generate_account_key(api_user: str) -> str:
		"""
		生成账号标识的 hash

		Args:
			api_user: API 用户标识

		Returns:
			完整的 SHA256 hash
		"""
		return hashlib.sha256(api_user.encode('utf-8')).hexdigest()

	@staticmethod
	def generate_balance_hash(quota: float, used: float) -> str:
		"""
		生成单个账号余额的 hash

		Args:
			quota: 总额度
			used: 已使用额度

		Returns:
			完整的 SHA256 hash
		"""
		balance_data = f'{quota}_{used}'
		return hashlib.sha256(balance_data.encode('utf-8')).hexdigest()
