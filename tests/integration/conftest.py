import pytest

from core.checkin_service import CheckinService


@pytest.fixture(autouse=True)
def successful_infrastructure_preflight(monkeypatch: pytest.MonkeyPatch):
	"""集成测试默认假设 AnyRouter 登录页可达，避免预检消耗账号请求 mock。"""
	result = CheckinService.InfrastructureCheckResult(
		available=True,
		reason='available',
		message='AnyRouter login page is reachable',
		attempts=1,
		url=CheckinService.Config.URLs.LOGIN,
	)

	async def check_success(self):
		return result

	monkeypatch.setattr(CheckinService, 'check_infrastructure', check_success)
