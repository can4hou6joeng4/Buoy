import hashlib
import json
from pathlib import Path

from core.balance_manager import BalanceManager


class TestBalanceManager:
	"""测试 BalanceManager 类"""

	def test_generate_hashes(self) -> None:
		"""测试哈希生成的正确性（包括边界情况）"""
		# 测试普通账号
		api_user = 'test_user_123'
		expected_account_key = hashlib.sha256(api_user.encode('utf-8')).hexdigest()
		actual_account_key = BalanceManager.generate_account_key(api_user)
		assert actual_account_key == expected_account_key

		# 测试余额 hash
		quota, used = 50.0, 10.0
		balance_data = f'{quota}_{used}'
		expected_balance_hash = hashlib.sha256(balance_data.encode('utf-8')).hexdigest()
		actual_balance_hash = BalanceManager.generate_balance_hash(quota=quota, used=used)
		assert actual_balance_hash == expected_balance_hash

		# 测试不同余额数据生成不同 hash
		hash1 = BalanceManager.generate_balance_hash(quota=50.0, used=10.0)
		hash2 = BalanceManager.generate_balance_hash(quota=60.0, used=10.0)
		assert hash1 != hash2

		# 测试边界情况（中文、Emoji、空字符串）
		assert BalanceManager.generate_account_key('用户名中文') != ''
		assert BalanceManager.generate_account_key('😀emoji') != ''
		assert BalanceManager.generate_account_key('') != ''

	def test_file_operations(self, tmp_path: Path):
		"""测试文件加载和保存操作"""
		balance_file = tmp_path / 'balance_hash.txt'
		manager = BalanceManager(balance_hash_file=balance_file)

		# 测试加载不存在的文件
		result = manager.load_balance_hash()
		assert result is None

		# 测试保存和加载
		test_data = {
			'user1_hash': {'hash': 'balance1_hash', 'quota': 25.0, 'used': 5.0},
			'user2_hash': {'hash': 'balance2_hash'},
		}
		manager.save_balance_hash(test_data)
		assert balance_file.exists()

		loaded_data = manager.load_balance_hash()
		assert loaded_data == test_data

		# 测试父目录自动创建
		nested_file = tmp_path / 'nested' / 'dir' / 'balance.txt'
		nested_manager = BalanceManager(balance_hash_file=nested_file)
		nested_manager.save_balance_hash(test_data)
		assert nested_file.exists()

		# 测试覆盖写入
		new_data = {'user3_hash': {'hash': 'balance3_hash', 'quota': 30.0, 'used': 8.0}}
		manager.save_balance_hash(new_data)
		loaded_new_data = manager.load_balance_hash()
		assert loaded_new_data == new_data
		assert loaded_new_data is not None
		assert 'user1_hash' not in loaded_new_data  # 旧数据被覆盖

		# 测试兼容旧格式（值为字符串）
		legacy_data = {'legacy_user': 'legacy_hash'}
		balance_file.write_text(json.dumps(legacy_data), encoding='utf-8')
		assert manager.load_balance_hash() == {'legacy_user': {'hash': 'legacy_hash'}}

	def test_file_error_handling(self, tmp_path: Path):
		"""测试文件读写异常处理"""
		balance_file = tmp_path / 'balance_hash.txt'
		manager = BalanceManager(balance_hash_file=balance_file)

		# 测试空文件
		balance_file.touch()
		result = manager.load_balance_hash()
		assert result is None

		# 测试无效 JSON
		balance_file.write_text('invalid json content')
		result = manager.load_balance_hash()
		assert result is None

		# 测试加载后文件仍然存在（没有被破坏）
		assert balance_file.exists()

		# 测试超大 JSON（边界测试）
		large_data = {
			f'user_{i}': {
				'hash': f'hash_{i}' * 100,
				'quota': float(i),
				'used': float(i) / 10,
			}
			for i in range(1000)
		}
		manager.save_balance_hash(large_data)
		loaded_large = manager.load_balance_hash()
		assert loaded_large == large_data

		# 测试空字典
		manager.save_balance_hash({})
		loaded_empty = manager.load_balance_hash()
		assert loaded_empty == {}
