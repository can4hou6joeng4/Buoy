import json
import stat

import pytest

from scripts.refresh_anyrouter_credentials import (
	CredentialRefreshError,
	load_accounts,
	resolve_input_path,
	select_account_name,
	write_accounts_securely,
)


def test_resolve_input_path_from_anyrouter_section(tmp_path):
	accounts_path = tmp_path / 'accounts.json'
	env_path = tmp_path / '.env'
	env_path.write_text(f'[Anyrouter]\nANYROUTER_ACCOUNTS_FILE={accounts_path}\n', encoding='utf-8')

	assert resolve_input_path(None, env_path) == accounts_path.resolve()


def test_load_accounts_requires_login_credentials(tmp_path):
	path = tmp_path / 'accounts.json'
	path.write_text(json.dumps([{'username': 'test'}]), encoding='utf-8')

	with pytest.raises(CredentialRefreshError, match='缺少 username/password'):
		load_accounts(path)


@pytest.mark.parametrize(
	'user_data,fallback,expected',
	[
		({'display_name': ' Display ', 'username': 'user'}, 'fallback', 'Display'),
		({'display_name': '', 'username': ' user '}, 'fallback', 'user'),
		({}, ' fallback ', 'fallback'),
	],
)
def test_select_account_name(user_data, fallback, expected):
	assert select_account_name(user_data, fallback) == expected


def test_write_accounts_securely_uses_restricted_permissions(tmp_path):
	path = tmp_path / 'refreshed.json'
	accounts = [{'name': 'account', 'username': 'user', 'password': 'password'}]

	write_accounts_securely(path, accounts)

	assert json.loads(path.read_text(encoding='utf-8')) == accounts
	assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_accounts_securely_rejects_symlink(tmp_path):
	target = tmp_path / 'target.json'
	target.write_text('[]', encoding='utf-8')
	link = tmp_path / 'link.json'
	link.symlink_to(target)

	with pytest.raises(CredentialRefreshError, match='符号链接'):
		write_accounts_securely(link, [])
