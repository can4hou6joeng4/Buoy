# AnyRouter 账号密码换取签到凭据流程

本文记录 Buoy 当前已通过浏览器和 GitHub Actions 验证的账号凭据恢复链路。目标是让每日签到继续由
GitHub Actions 执行，同时在 `session/api_user` 缺失或明确失效时，使用长期登录凭据自动恢复。

## 结论

账号密码不能直接用于签到。完整链路由四段组成：

1. Playwright 访问登录页并取得 WAF Cookie。
2. 调用登录接口，以 `username/password` 换取 `session` 和用户 ID。
3. 调用 `/api/user/self` 验证新凭据，并只提取真实名称等白名单字段。
4. 使用新凭据调用签到接口；账号配置发生变化时，写回 GitHub `production` Environment Secret。

```text
账号 JSON
  -> GET /login（Playwright，仅获取 WAF Cookie）
  -> POST /api/user/login?turnstile=
  -> Set-Cookie: session + response.data.id
  -> GET /api/user/self（验证 session/api_user，提取 display_name）
  -> POST /api/user/sign_in（由 GitHub Actions 执行每日签到）
  -> 更新 production/ANYROUTER_ACCOUNTS
```

## 输入和持久字段

首次输入只需要：

```json
[
  {
    "username": "登录用户名或邮箱",
    "password": "登录密码"
  }
]
```

刷新后的持久格式为：

```json
[
  {
    "name": "AnyRouter 服务端显示名称",
    "username": "登录用户名或邮箱",
    "password": "登录密码",
    "cookies": {
      "session": "登录接口签发的 Session"
    },
    "api_user": "登录响应中的用户 ID"
  }
]
```

`username/password` 必须继续保留，因为 `session` 过期后只能依靠长期认证材料重新登录。账号 JSON、临时输出和
本地隔离文件必须使用 `0600` 权限，且不得进入 Git。

## 接口协议

### 1. 获取 WAF Cookie

- 页面：`GET https://anyrouter.top/login`
- 执行器：隔离的 Playwright Chromium Context
- 需要的 Cookie：`acw_tc`、`cdn_sec_tc`、`acw_sc__v2`
- 目的：通过站点入口建立 WAF 请求上下文，不读取浏览器密码、Cookie 存储或既有用户配置

缺少任意 WAF Cookie 时终止，不继续提交账号密码。

### 2. 登录换取凭据

- 方法：`POST`
- URL：`https://anyrouter.top/api/user/login?turnstile=`
- `Content-Type`：`application/json`
- `Origin`：`https://anyrouter.top`
- `Referer`：`https://anyrouter.top/login`
- Cookie：上一步的 WAF Cookie；发送前删除旧 `session`，避免重复 Cookie 冲突

请求体：

```json
{
  "username": "...",
  "password": "..."
}
```

只读取以下结果：

- JSON `success`
- JSON `data.id`，保存为 `api_user`
- 响应 Cookie `session`，保存为 `cookies.session`

不得记录请求体、完整响应体、`Set-Cookie` 或 Session 值。

### 3. 验证凭据和真实名称

- 方法：`GET`
- URL：`https://anyrouter.top/api/user/self`
- 请求头：`new-api-user: <data.id>`
- Cookie：WAF Cookie 和新 `session`

2026-08-27 的内置浏览器验证表明，该接口响应含有 `display_name`、`username`、`id`、额度字段，同时还可能包含
`password`、`original_password`、`access_token` 等敏感字段。因此实现采用严格白名单：

- 名称：优先 `display_name`，为空时使用 `username`
- 额度：只读取 `quota` 和 `used_quota`
- 不返回、不打印、不持久化完整 `/api/user/self` 数据

生产日志通过 `SHOW_ACCOUNT_NAMES=true` 单独展示真实名称。该开关不等同于
`SHOW_SENSITIVE_INFO=true`，不会因此公开余额。

### 4. 签到

- 方法：`POST`
- URL：`https://anyrouter.top/api/user/sign_in`
- 请求头：`new-api-user: <api_user>`、`X-Requested-With: XMLHttpRequest`
- Cookie：WAF Cookie 和有效 `session`

直接使用脚本只负责刷新和验证凭据，不调用该接口。签到仍由 `.github/workflows/checkin.yml` 的计划任务执行。

## 何时自动刷新

以下情况允许使用账号密码自动登录：

- `session` 或 `api_user` 尚未生成。
- `/api/user/self` 返回 HTTP `401/403`。
- `/api/user/self` 返回明确的未登录、登录过期或用户无效文案。

以下情况不得误判为凭据过期：

- HTTP `5xx`、超时、网络错误。
- 数据库只读、连接数过多、网关错误等上游服务故障。
- 无法解析的临时响应。

登录接口的“用户名或密码错误，或用户已被封禁”无法从协议层继续拆分。只有在账号所有者确认封禁后，才应从活动
账号数组移除；建议把原对象放入本地 `0600` 隔离文件，而不是不可恢复地删除。

## 直接使用

安装项目依赖和 Playwright Chromium：

```bash
uv sync
uv run playwright install chromium
```

默认从 `~/.env` 读取：

```ini
[Anyrouter]
ANYROUTER_ACCOUNTS_FILE=/absolute/path/to/anyrouter-accounts.json
```

刷新并显示服务端真实名称：

```bash
uv run python scripts/refresh_anyrouter_credentials.py \
  --output anyrouter-refreshed-accounts.json \
  --show-account-names
```

排除已由账号所有者确认封禁的第 9 个账号：

```bash
uv run python scripts/refresh_anyrouter_credentials.py \
  --exclude-index 9 \
  --output anyrouter-refreshed-accounts.json \
  --show-account-names
```

也可以显式提供输入文件：

```bash
uv run python scripts/refresh_anyrouter_credentials.py \
  --input examples/anyrouter-accounts.example.json \
  --output anyrouter-refreshed-accounts.json
```

脚本行为：

- 不执行签到。
- 不覆盖输入文件。
- 任意活动账号失败时退出并且不生成部分结果。
- 输出使用临时文件加原子替换，最终权限固定为 `0600`。
- 终端不显示密码、Session、Cookie、完整登录响应或完整用户资料响应。

确认输出后写入 GitHub Environment Secret：

```bash
gh secret set ANYROUTER_ACCOUNTS \
  --env production \
  --repo can4hou6joeng4/Buoy \
  < anyrouter-refreshed-accounts.json
```

## GitHub Actions 写回

生产环境需要：

- Secret `ANYROUTER_ACCOUNTS`：账号数组。
- Secret `BUOY_SECRET_SYNC_TOKEN`：限当前仓库、具有 Environments 读写权限的 fine-grained PAT。
- Variable `SHOW_ACCOUNT_NAMES=true`：显示账号真实名称，但继续隐藏余额。

工作流执行顺序：

1. 使用 Secret 中已有 `session/api_user` 验证账号。
2. 仅对缺失或明确失效的凭据调用登录接口。
3. 本次运行立即继续签到。
4. 凭据或真实名称发生变化时，生成 Runner 临时 JSON。
5. 使用 `BUOY_SECRET_SYNC_TOKEN` 更新 `production/ANYROUTER_ACCOUNTS`。
6. 清空 Runner 临时文件。

旧的 `ANYROUTER_ACCOUNT_*` Secret 可以继续保留，但内置工作流不会读取它们，避免旧 Session 覆盖刷新结果。

## 安全边界

- 仓库是公开的，任何真实账号名称都会出现在公开 Actions 日志中；这是显式启用
  `SHOW_ACCOUNT_NAMES=true` 后的预期行为。
- 不要设置 `SHOW_SENSITIVE_INFO=true`，除非同时接受公开余额等信息。
- 不要把账号 JSON、Runner 临时文件或失败响应正文添加到构建产物。
- 不要用完整 `/api/user/self` 响应替代白名单字段整理。
- 同步 Token 应使用最小权限 fine-grained PAT，不应长期使用宽权限 OAuth Token。
