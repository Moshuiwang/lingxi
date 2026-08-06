# 飞书 OAuth 关联组织测试项目

> 状态：测试参考代码，不属于正式 Lingxi 用户路径，也不是普通员工开通方案。
> 唯一事实证据：[飞书关联组织用户身份受控验证](../../docs/参考证据/飞书关联组织用户身份受控验证.md)。

该项目用于复验：专用用户“四达文档会议助手”授权后，以用户身份读取关联组织、逐部门下钻、读取成员详情，以及加密轮换 `refresh_token`。它只允许在 `biai-stage`、`tz`（见[协作约定](../../docs/协作约定.md)固定名称表）、Bot-Test 和独立测试数据库中运行。普通员工正式开通不使用本项目中的授权卡片流程，正式设计见[关联组织专用授权与普通员工自动开通边界](../../docs/决策记录/2026-07-28-关联组织专用授权与普通员工自动开通边界.md)。

## `tz` 上的本地测试数据库（例外记录）

2026-08-05 在 `tz` 上用本机 Docker 新起了一个独立 PostgreSQL 容器（`lingxi-test-pg`，仅监听本机 `15432` 端口，非公网可达）复验本流程，因为当时无法从 `tz` 连接 `biai-stage` 内部的测试数据库。结果与 `biai-stage` 2026-07-28 原始验证一致（见[飞书关联组织用户身份受控验证](../../docs/参考证据/飞书关联组织用户身份受控验证.md)）。

- 该容器与其中的加密 `refresh_token` 按产品负责人要求保留在 `tz`，作为 `biai-stage` 之外另一个常驻研发验收落点，不再随手清理。
- 复验入口与 `biai-stage` 相同（`scripts/probe_saved_feishu_associated_organization.py` 或重启 `feishu_onboarding.run_long_connection_bot`），仅需把 `LINGXI_POSTGRES_DSN` 换成 `tz` 本地库地址；`.env` 中其余飞书应用凭据、桥接地址不变。
- **`refresh_token` 有效期是硬约束，不是长期凭据**：当次授权换到的 `refresh_token_expires_at` 为 2026-08-12 05:36 UTC。自动轮换只在 `run_long_connection_bot` 常驻进程运行时才会发生（每 60 秒检查一次到期凭据）；本次验证完成后该进程已停止，过期前若无人工重启或手动触发一次刷新，凭据会在 2026-08-12 后失效，需要“四达文档会议助手”重新完成一次飞书授权同意。

## 代码入口

| 位置 | 作用 |
| --- | --- |
| [`src/lingxi/adapters/oauth_bridge.py`](../../src/lingxi/adapters/oauth_bridge.py) | OAuth 授权码换取、关联组织下钻、成员与部门详情读取。 |
| [`src/lingxi/adapters/refresh_tokens.py`](../../src/lingxi/adapters/refresh_tokens.py) | `refresh_token` 密文保存与单次轮换。 |
| [`src/lingxi/adapters/feishu_onboarding.py`](../../src/lingxi/adapters/feishu_onboarding.py) | Bot-Test 长连接入口，仅在测试开关开启时运行组织探针。 |
| [`scripts/probe_saved_feishu_associated_organization.py`](../../scripts/probe_saved_feishu_associated_organization.py) | 用已加密保存的测试授权复验组织下钻。 |
| [`tests/test_feishu_oauth_v3.py`](../../tests/test_feishu_oauth_v3.py) | 无网络回归测试。 |
| [`tests/test_refresh_token_postgres.py`](../../tests/test_refresh_token_postgres.py) | CI 测试库中验证密文保存、轮换替换与删除；不访问飞书。 |
| [`migrations/testing/003_create_feishu_user_refresh_token.sql`](../../migrations/testing/003_create_feishu_user_refresh_token.sql) | 测试库的加密续期凭据表。 |

## 不可当作正式实现的部分

- 依赖 `LINGXI_OAUTH_ORGANIZATION_PROBE_ONLY=enabled` 与身份资料调试页面；这些都是测试开关。
- 不创建正式用户、不会匹配权限或发布权限。
- 测试数据库中的凭据保存和轮换仅证明飞书 OAuth 行为；正式环境需要独立的密钥管理、撤销与删除设计和验收。
- 任何复验不得打印或提交授权码、access token、refresh token、完整身份标识或成员资料。
