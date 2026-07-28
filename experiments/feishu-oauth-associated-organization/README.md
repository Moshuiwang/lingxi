# 飞书 OAuth 关联组织测试项目

> 状态：测试参考代码，不属于正式 Lingxi 用户路径。
> 唯一事实证据：[飞书关联组织用户身份受控验证](../../docs/参考证据/飞书关联组织用户身份受控验证.md)。

该项目用于复验：Bot-Test 私聊授权后，以用户身份读取关联组织、逐部门下钻、读取成员详情，以及加密轮换 `refresh_token`。它只允许在 `biai-stage`、Bot-Test 和独立测试数据库中运行。

## 代码入口

| 位置 | 作用 |
| --- | --- |
| [`src/lingxi/adapters/oauth_bridge.py`](../../src/lingxi/adapters/oauth_bridge.py) | OAuth 授权码换取、关联组织下钻、成员与部门详情读取。 |
| [`src/lingxi/adapters/refresh_tokens.py`](../../src/lingxi/adapters/refresh_tokens.py) | `refresh_token` 密文保存与单次轮换。 |
| [`src/lingxi/adapters/feishu_onboarding.py`](../../src/lingxi/adapters/feishu_onboarding.py) | Bot-Test 长连接入口，仅在测试开关开启时运行组织探针。 |
| [`scripts/probe_saved_feishu_associated_organization.py`](../../scripts/probe_saved_feishu_associated_organization.py) | 用已加密保存的测试授权复验组织下钻。 |
| [`tests/test_feishu_oauth_v3.py`](../../tests/test_feishu_oauth_v3.py) | 无网络回归测试。 |
| [`tests/test_refresh_token_postgres.py`](../../tests/test_refresh_token_postgres.py) | CI 测试库中验证密文保存、轮换替换与删除；不访问飞书。 |
| [`migrations/003_create_feishu_user_refresh_token.sql`](../../migrations/003_create_feishu_user_refresh_token.sql) | 测试库的加密续期凭据表。 |

## 不可当作正式实现的部分

- 依赖 `LINGXI_OAUTH_ORGANIZATION_PROBE_ONLY=enabled` 与身份资料调试页面；这些都是测试开关。
- 不创建正式用户、不会匹配权限或发布权限。
- 测试数据库中的凭据保存和轮换仅证明飞书 OAuth 行为；正式环境需要独立的密钥管理、撤销与删除设计和验收。
- 任何复验不得打印或提交授权码、access token、refresh token、完整身份标识或成员资料。
