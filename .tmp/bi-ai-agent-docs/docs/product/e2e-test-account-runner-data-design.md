# E2E 测试账号批量 Runner 数据设计

> 日期：2026-06-12
> 状态：方案草案

## 一、账号规格

账号规格使用 JSON 文件，默认不包含密钥：

```json
{
  "accounts": [
    {
      "id": "mcpmonall",
      "name": "MCP Monitor All",
      "email": "mcpmonall@startimes.com.cn",
      "company": "Nigeria",
      "functions": ["财务", "运营"],
      "linux_username": "mcpmonall",
      "expected_permissions": {"*": ["*"]},
      "protected": false,
      "retain_by_default": true,
      "checks": {
        "imap": false,
        "ssh": false,
        "claude": false,
        "mcp": false,
        "plugin": false
      }
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| `id` | Runner 选择账号时使用的稳定 ID |
| `name` | 飞书申请里的姓名快照 |
| `email` | 开户邮箱，必须是 `startimes.com.cn` |
| `company` | 申请公司名 |
| `functions` | 申请职能 |
| `linux_username` | 预期 Linux 用户名 |
| `expected_permissions` | 该账号应看到的 MCP 权限 |
| `protected` | 是否禁止清理 |
| `retain_by_default` | 批量创建后是否默认保留 |
| `checks` | 该账号默认启用哪些验证阶段 |

## 二、默认账号规格

| id | email | company | functions | expected_permissions |
|----|-------|---------|-----------|----------------------|
| `mcpmonall` | `mcpmonall@startimes.com.cn` | `Nigeria` | `财务,运营` | `{"*":["*"]}` |
| `benchall1` | `benchall1@startimes.com.cn` | `Nigeria` | `财务,运营` | `{"*":["*"]}` |
| `benchall2` | `benchall2@startimes.com.cn` | `Nigeria` | `财务,运营` | `{"*":["*"]}` |
| `benchugops` | `benchugops@startimes.com.cn` | `Uganda` | `运营` | `{"7":["sub_recharge_count"]}` |
| `benchnigops` | `benchnigops@startimes.com.cn` | `Nigeria` | `运营` | `{"13":["*"]}` |

说明：

- Nigeria 的物理 `company_id` 当前为 `13`。
- 全量账号的申请公司只用于 onboarding 快照；实际权限以管理员审批配置后的 `expected_permissions` 为准。
- fake 邮箱默认不做 IMAP 读回，也默认不做 SSH/Claude smoke。原因是 runner 拿不到本轮新生成的 SSH 私钥。后续若配置真实邮箱别名或 catch-all，再打开 `imap/ssh/claude` 检查。

## 三、运行结果数据

Runner 只输出本次运行摘要，不新增业务表。

建议 JSON 输出结构：

```json
{
  "overall": "PASS",
  "accounts": [
    {
      "id": "benchugops",
      "email": "benchugops@startimes.com.cn",
      "record_id": 123,
      "user_id": 456,
      "linux_username": "benchugops",
      "phases": [
        {"name": "Phase 1 申请", "status": "PASS", "detail": ""}
      ]
    }
  ]
}
```

控制台摘要面向 PM/运维阅读，JSON 面向 CI 或后续定时任务读取。

## 四、数据库影响

Runner 本身不新增表。

它会通过现有标准流程写入：

- `users`
- `approval_records`
- `user_functions`
- `mcp_tokens`
- Bitable 权限表
- Linux 用户目录

清理时复用 `scripts/cleanup_live_run.py` 或测试群 `测试收回` 命令，不能直接手写删除逻辑。

## 五、数据保护

保护名单固定内置，同时允许从账号规格补充：

- `wangzp`
- `wangzhipeng`
- `maosy`
- `zhaojx`
- `liuds`

任何清理、收回、覆盖前，如果目标 email 或 Linux username 命中保护名单，Runner 必须失败并停止该账号。
