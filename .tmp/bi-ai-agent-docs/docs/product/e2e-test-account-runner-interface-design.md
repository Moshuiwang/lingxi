# E2E 测试账号批量 Runner 接口设计

> 日期：2026-06-12
> 状态：方案草案

## 一、CLI

入口：

```bash
.venv/bin/python scripts/run_e2e_test.py [options]
```

核心参数：

| 参数 | 说明 |
|------|------|
| `--accounts-file <path>` | 指定账号规格 JSON |
| `--account <id>` | 只跑某个账号，可重复传入 |
| `--list-accounts` | 只列出账号，不执行 |
| `--retain-accounts` | 跑完后保留账号，不执行收回 |
| `--revoke-after` | 跑完后执行收回 |
| `--skip-phase0` | 跳过前置清理 |
| `--skip-imap` | 跳过邮件 ZIP 读回 |
| `--skip-ssh` | 跳过 SSH 登录 |
| `--skip-claude` | 跳过 Claude/MCP/Plugin smoke |
| `--json` | 额外输出机器可读 JSON 摘要 |

默认行为：

- 未指定账号时跑账号规格里的全部账号。
- 默认按账号规格决定是否保留。
- 默认不对 fake 邮箱做 IMAP 读回，也不会继续跑依赖私钥的 SSH/Claude smoke。
- 默认启用真实 DB/Bitable 校验。

## 二、飞书测试命令

继续复用现有测试群命令：

```text
测试申请 name=<姓名> email=<邮箱> company=<公司> functions=<职能1,职能2>
测试审批通过 record_id=<申请ID>
测试收回 email=<邮箱> company=<公司>
```

约束：

- 只在 `LARK_TEST_CHAT_ID` 生效。
- 只处理 `E2E_TEST_OPEN_ID` 创建的测试记录。
- 回复文案保持稳定，Runner 依赖 `record_id=<N>` 解析。

## 三、环境变量

Runner 需要的环境变量：

| 变量 | 用途 |
|------|------|
| `LARK_TEST_APP_ID` | 测试机器人 app id |
| `LARK_TEST_APP_SECRET` | 测试机器人 app secret |
| `LARK_TEST_CHAT_ID` | 测试群 chat_id |
| `E2E_TEST_OPEN_ID` | 测试申请人 open_id |
| `E2E_SSH_HOST` | SSH 验证目标 host |
| `E2E_SSH_PORT` | SSH 验证目标 port |

涉及 token、密码、私钥的变量和输出不得打印。

## 四、控制台输出

单账号摘要：

```text
========== E2E 账号 benchugops ==========
PASS Preflight 账号保护
PASS Phase 0 前置清理
PASS Phase 1 申请 record_id=123
PASS Phase 2 审批通过
PASS Phase 3 DB/Bitable
SKIP Phase 4 邮件+私钥 fake 邮箱默认跳过
SKIP Phase 5 SSH 登录 依赖 Phase 4
PASS Phase 7 收回授权
RESULT benchugops PASS
```

批量摘要：

```text
========== E2E 批量测试汇总 ==========
PASS mcpmonall
PASS benchall1
PASS benchall2
PASS benchugops
PASS benchnigops
OVERALL PASS
```

## 五、错误口径

错误信息面向用户体验和运维判断：

- 申请失败：说明测试机器人或 Bot 入口不可用。
- 审批失败：说明审批命令或开户后置流程不可用。
- DB/Bitable 失败：说明账号状态或权限发布不可用。
- IMAP 失败：说明邮件读回不可用，不等于账号不可用。
- SSH 失败：说明用户无法真实登录。
- MCP/Plugin 失败：说明日常查询体验不可用。

## 六、后续定时任务关系

本次不直接建立定时任务。已有账号的日常 MCP 健康检查和 Benchmark 应由准入巡检/评测入口调用这些固定账号执行，不由本 onboarding runner 反复重新开户。

本 runner 可以作为定时任务前置准备命令：

```bash
.venv/bin/python scripts/run_e2e_test.py \
  --accounts-file config/e2e-test-accounts.json \
  --retain-accounts \
  --json
```

这个模式用于建立或重置固定测试账号矩阵，并把账号保留下来给后续准入巡检和 Benchmark 使用。
