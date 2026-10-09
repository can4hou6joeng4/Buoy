# AGENTS.md

Buoy：基于 Python 的 AnyRouter 多账号自动签到工具，支持多种通知渠道与日志脱敏。既能 fork 后定时运行，也能作为 Composite Action（根目录 `action.yml`）被其他仓库调用。

## 目录

- `src/main.py` — 入口（`autocheck-anyrouter`）；`src/application.py` 编排各服务
- `src/core/` — 签到、余额追踪、隐私脱敏、GitHub Step Summary 报告
- `src/notif/` — 通知系统：`senders/`（各平台发送器）、`models/`（配置模型）、`configs/`（默认 json5 模板）、`trigger_manager.py`（触发条件）
- `scripts/refresh_anyrouter_credentials.py` — 只刷新登录凭据
- `tests/` — `unit/`、`integration/`、`fixtures/`、`tools/`
- `.github/workflows/checkin.yml`（每 6 小时）与 `.github/actions/*`（环境与 Playwright 安装）

## 常用命令（mise）

```bash
mise install && mise run setup   # Python 3.11 + 依赖 + Playwright Chromium
mise run test                    # pytest tests/
mise run lint                    # ruff check src tests
mise run fmt                     # ruff format（tab 缩进、单引号）
```

## 约定

- Python ≥ 3.11；ruff 配置见 `pyproject.toml`：tab 缩进、单引号、行宽 120。
- 新增通知渠道：`src/notif/senders/` 加发送器、`src/notif/models/` 加配置模型、在通知编排中注册，并补测试。
- 公开仓库的日志和 Step Summary 必须保持脱敏（账号名、余额），不要削弱 `privacy_handler`。

## 不要做

- 不要提交真实 session cookie、API 用户 ID、webhook、token 或 `.env.test`。
- 默认测试必须离线；会真正发通知的集成测试只能由 `ENABLE_REAL_TEST=true` 显式开启，不要改默认值。
- 不要在本地真实签到或刷新凭据，除非用户明确要求。
