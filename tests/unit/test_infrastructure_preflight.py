from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.checkin_service import CheckinService


def build_response(status: int):
	response = MagicMock()
	response.status_code = status
	return response


@pytest.mark.asyncio
async def test_preflight_success_on_reachable_status_without_retry():
	service = CheckinService()
	success = service.InfrastructureCheckResult(
		available=True,
		reason='available',
		message='AnyRouter login page is reachable',
		attempts=1,
	)

	with patch.object(service, '_check_login_page_once', new=AsyncMock(return_value=success)) as check_once:
		result = await service.check_infrastructure(max_attempts=3, delay_seconds=0)

	assert result.available is True
	assert result.attempts == 1
	assert check_once.await_count == 1


@pytest.mark.asyncio
async def test_preflight_retries_once_then_succeeds():
	service = CheckinService()
	first = service.InfrastructureCheckResult(
		available=False,
		reason='dns_resolution_failed',
		message='DNS resolution failed for https://anyrouter.top/login',
		attempts=1,
	)
	second = service.InfrastructureCheckResult(
		available=True,
		reason='available',
		message='AnyRouter login page is reachable',
		attempts=2,
	)

	with patch.object(service, '_check_login_page_once', new=AsyncMock(side_effect=[first, second])) as check_once:
		with patch('asyncio.sleep', new=AsyncMock()) as sleep_mock:
			result = await service.check_infrastructure(max_attempts=3, delay_seconds=60)

	assert result.available is True
	assert result.attempts == 2
	assert check_once.await_count == 2
	sleep_mock.assert_awaited_once_with(60)


@pytest.mark.asyncio
async def test_preflight_exhausts_retries_for_dns_failure():
	service = CheckinService()
	failures = [
		service.InfrastructureCheckResult(
			available=False,
			reason='dns_resolution_failed',
			message='DNS resolution failed for https://anyrouter.top/login',
			attempts=attempt,
		)
		for attempt in (1, 2, 3)
	]

	with patch.object(service, '_check_login_page_once', new=AsyncMock(side_effect=failures)) as check_once:
		with patch('asyncio.sleep', new=AsyncMock()) as sleep_mock:
			result = await service.check_infrastructure(max_attempts=3, delay_seconds=60)

	assert result.available is False
	assert result.reason == 'dns_resolution_failed'
	assert result.attempts == 3
	assert check_once.await_count == 3
	assert sleep_mock.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 302, 401, 403, 404])
async def test_login_page_reachable_statuses_are_available(status):
	service = CheckinService()

	async def get_handler(*args, **kwargs):
		return build_response(status)

	mock_client = MagicMock()
	mock_client.get = get_handler
	mock_client.__aenter__ = AsyncMock(return_value=mock_client)
	mock_client.__aexit__ = AsyncMock(return_value=None)

	with patch('httpx.AsyncClient', return_value=mock_client):
		result = await service._check_login_page_once(attempt=1)

	assert result.available is True
	assert result.reason == 'available'
	assert result.attempts == 1


@pytest.mark.asyncio
async def test_login_page_5xx_is_infrastructure_failure():
	service = CheckinService()

	async def get_handler(*args, **kwargs):
		return build_response(503)

	mock_client = MagicMock()
	mock_client.get = get_handler
	mock_client.__aenter__ = AsyncMock(return_value=mock_client)
	mock_client.__aexit__ = AsyncMock(return_value=None)

	with patch('httpx.AsyncClient', return_value=mock_client):
		result = await service._check_login_page_once(attempt=1)

	assert result.available is False
	assert result.reason == 'server_error'
	assert 'HTTP 503' in result.message


@pytest.mark.asyncio
async def test_dns_connect_error_is_classified():
	service = CheckinService()

	async def get_handler(*args, **kwargs):
		raise httpx.ConnectError('[Errno 8] nodename nor servname provided, or not known')

	mock_client = MagicMock()
	mock_client.get = get_handler
	mock_client.__aenter__ = AsyncMock(return_value=mock_client)
	mock_client.__aexit__ = AsyncMock(return_value=None)

	with patch('httpx.AsyncClient', return_value=mock_client):
		result = await service._check_login_page_once(attempt=1)

	assert result.available is False
	assert result.reason == 'dns_resolution_failed'
	assert result.attempts == 1
