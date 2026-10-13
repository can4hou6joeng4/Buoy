"""Exercise the actual Secret sync shell offline, without real GitHub credentials."""

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / '.github/workflows/checkin.yml'
FAKE_TOKEN = 'fictional-sync-token'
ACCOUNTS = json.dumps([
	{
		'name': 'fictional-name',
		'username': 'fictional-user',
		'password': 'fictional-password',
		'cookies': {'session': 'fictional-session'},
		'api_user': '987654321',
	}
])
FORBIDDEN = [
	ACCOUNTS,
	FAKE_TOKEN,
	'fictional-name',
	'fictional-user',
	'fictional-password',
	'fictional-session',
	'987654321',
]


@pytest.fixture
def sync_script():
	workflow = WORKFLOW.read_text(encoding='utf-8')
	# Deliberately support only this workflow's literal run block; fail if its shape changes.
	steps = re.split(r'^      - name: ', workflow, flags=re.MULTILINE)
	matches = [step for step in steps if step.splitlines()[0].endswith('持久化自动刷新的账号凭据')]
	assert len(matches) == 1
	step = matches[0]
	assert '        if: always()\n' in step
	assert 'continue-on-error:' not in step
	assert 'GH_TOKEN: ${{ secrets.BUOY_SECRET_SYNC_TOKEN }}' in step
	assert step.count('        run: |\n') == 1
	block = step.split('        run: |\n', 1)[1]
	assert all(not line.strip() or line.startswith('          ') for line in block.splitlines())
	return textwrap.dedent(block)


@pytest.mark.parametrize(
	'file_content,token,gh_status,gh_error,expected_status',
	[
		pytest.param(None, '', 0, '', 0, id='missing-file'),
		pytest.param('', '', 0, '', 0, id='empty-file'),
		pytest.param(ACCOUNTS, '', 0, '', 1, id='missing-token'),
		pytest.param(
			ACCOUNTS,
			FAKE_TOKEN,
			1,
			'failed to fetch public key: HTTP 403: Resource not accessible by personal access token '
			'(https://api.github.com/repos/example/offline/environments/production/secrets/public-key)',
			1,
			id='public-key-403',
		),
		pytest.param(
			ACCOUNTS, FAKE_TOKEN, 2, 'failed to fetch public key: HTTP 500: Server Error', 1, id='other-error'
		),
		pytest.param(ACCOUNTS, FAKE_TOKEN, 0, '', 0, id='success'),
	],
)
def test_secret_sync_branches(tmp_path, sync_script, file_content, token, gh_status, gh_error, expected_status):
	accounts_file = tmp_path / 'refreshed accounts.json'
	if file_content is not None:
		accounts_file.write_text(file_content, encoding='utf-8')
		accounts_file.chmod(0o600)

	bin_dir = tmp_path / 'bin'
	bin_dir.mkdir()
	calls_file = tmp_path / 'calls.jsonl'
	fake_gh = bin_dir / 'gh'
	fake_gh.write_text(
		f'#!{sys.executable}\n'
		'import json, os, sys\n'
		'from pathlib import Path\n'
		'with Path(os.environ["CALLS_FILE"]).open("a", encoding="utf-8") as calls:\n'
		'    calls.write(json.dumps({"args": sys.argv[1:], "stdin": sys.stdin.read(), '
		'"token_ok": os.environ.get("GH_TOKEN") == "fictional-sync-token"}) + "\\n")\n'
		'if sys.argv[1:2] == ["api"]:\n'
		'    if os.environ["GH_STATUS"] == "2":\n'
		'        print("fictional-password", file=sys.stderr)\n'
		'        sys.exit(2)\n'
		'    status = "403 Forbidden" if sys.argv[-1].endswith("public-key") else "200 OK"\n'
		'    print("HTTP/2.0 " + status + "\\r")\n'
		'    print("X-Accepted-GitHub-Permissions: environments=read\\r")\n'
		'    print("X-Unrelated-Header: fictional-password\\r")\n'
		'    print("\\r")\n'
		'    print("HTTP/2.0 fictional-session " + os.environ["GH_TOKEN"])\n'
		'    sys.exit(1 if status.startswith("403") else 0)\n'
		'if os.environ["GH_ERROR"]:\n'
		'    print(os.environ["GH_ERROR"], file=sys.stderr)\n'
		'sys.exit(int(os.environ["GH_STATUS"]))\n',
		encoding='utf-8',
	)
	fake_gh.chmod(0o700)
	# Do not inherit HOME, tokens, shell startup hooks, or a PATH containing a real gh.
	env = {
		'PATH': str(bin_dir),
		'HOME': str(tmp_path),
		'GH_CONFIG_DIR': str(tmp_path / 'gh-config'),
		'GH_TOKEN': token,
		'GITHUB_REPOSITORY': 'example/offline',
		'REFRESHED_ACCOUNTS_FILE': str(accounts_file),
		'CALLS_FILE': str(calls_file),
		'GH_STATUS': str(gh_status),
		'GH_ERROR': gh_error,
	}
	result = subprocess.run(
		['/bin/bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', sync_script],
		cwd=tmp_path,
		env=env,
		capture_output=True,
		text=True,
		timeout=10,
	)
	assert result.returncode == expected_status
	stdout_lines = result.stdout.splitlines()
	masks = [line for line in stdout_lines if line.startswith('::add-mask::')]
	assert masks == ([f'::add-mask::{ACCOUNTS}'] if file_content else [])
	ordinary_logs = '\n'.join(line for line in stdout_lines if not line.startswith('::add-mask::')) + result.stderr
	for secret in FORBIDDEN:
		assert secret not in ordinary_logs
	assert FAKE_TOKEN not in result.stdout + result.stderr

	calls = (
		[json.loads(line) for line in calls_file.read_text(encoding='utf-8').splitlines()]
		if calls_file.exists()
		else []
	)
	should_call = bool(file_content and token)
	assert len(calls) == (4 if should_call and gh_status else int(should_call))
	if should_call:
		assert calls[0]['args'] == [
			'secret',
			'set',
			'ANYROUTER_ACCOUNTS',
			'--env',
			'production',
			'--repo',
			'example/offline',
		]
		assert calls[0]['stdin'] == ACCOUNTS
		assert calls[0]['token_ok'] is True
		for secret in FORBIDDEN:
			assert secret not in json.dumps(calls[0]['args'])
		if gh_status:
			assert [call['args'] for call in calls[1:]] == [
				['api', '--include', '--method', 'GET', endpoint]
				for endpoint in [
					'user',
					'repos/example/offline',
					'repos/example/offline/environments/production/secrets/public-key',
				]
			]
			assert all(call['stdin'] == '' and call['token_ok'] for call in calls[1:])

	if file_content is None:
		assert not accounts_file.exists()
	else:
		assert accounts_file.read_bytes() == b''

	if not file_content:
		assert '无需更新 GitHub Environment Secret' in result.stdout
	elif not token:
		assert '账号配置已更新但未持久化：缺少 BUOY_SECRET_SYNC_TOKEN' in result.stdout
	elif gh_status:
		assert result.stderr == gh_error + '\n'
		assert '账号配置已更新但未持久化' in result.stdout
		assert '若公钥 API 返回 HTTP 403' in result.stdout
		assert 'Environments 是否为 Read and write' in result.stdout
		assert '其他错误请按原始错误排查' in result.stdout
		assert result.stdout.count('同步 Token 权限诊断：GET ') == 3
		if gh_status == 1:
			assert 'HTTP/2.0 200 OK' in result.stdout
			assert 'HTTP/2.0 403 Forbidden' in result.stdout
			assert 'X-Accepted-GitHub-Permissions: environments=read' in result.stdout
		else:
			assert 'X-Accepted-GitHub-Permissions' not in result.stdout
	else:
		assert '已更新 production 环境的 ANYROUTER_ACCOUNTS' in result.stdout
		assert result.stderr == ''
	assert ('已更新 production 环境的 ANYROUTER_ACCOUNTS' in result.stdout) == (should_call and gh_status == 0)
