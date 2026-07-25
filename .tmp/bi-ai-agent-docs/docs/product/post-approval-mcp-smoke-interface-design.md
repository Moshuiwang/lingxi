# 审批后 MCP 验证等待态接口设计

> 日期：2026-06-26
> 状态：待实现
> 对应 PRD：`docs/product/post-approval-mcp-smoke-prd.md`

## 1. 用户侧消息

首次开户通过后：

```text
你的 BI AI Agent 权限已通过。

权限已提交同步，通常几分钟内可用，最慢可能需要约 15 分钟。
如果刚开始暂时查不到指标，请稍后再试；系统会在后台自动验证。
```

补充授权通过后：

```text
你的补充授权已通过，权限已提交同步。
新权限通常几分钟内可用，最慢可能需要约 15 分钟。
```

普通用户不直接接收“最终复验失败”，除非管理员决定人工沟通。

## 2. 审批群消息

审批刚通过：

```text
已批准，正在开通账号并发布权限。
```

权限发布完成：

```text
权限已发布到多维表格。
MCP 可能需要最多 15 分钟同步，系统会自动验证。
```

第一次 MCP 未通过：

```text
MCP 第一次验证未通过，当前按“等待同步中”处理。
系统将在约 5 分钟后自动复验；如服务重启，后台补偿任务会继续处理。
```

复验通过：

```text
MCP 已同步，验证通过。
用户可以开始使用。
```

最终未通过：

```text
MCP 复验仍未通过，需要人工检查。
建议检查：MCP 同步、Token 状态、权限多维表格行。
```

后台补偿发现等待态超时：

```text
MCP 自动复验未能在等待窗口内通过，已转为人工处理。
建议检查：MCP 同步、Token 状态、权限多维表格行。
```

## 3. 审批卡摘要

审批卡首屏只放业务判断信息：

```text
权限申请审批

申请类型：首次开通 / 补充授权
申请人：张三（zhangsan@startimes.com.cn）
本次申请：Uganda / 运营
当前已有权限：无 / Rwanda 运营
申请说明：负责 Uganda 日常经营分析

批准后：系统会发布权限；MCP 可能最多 15 分钟同步。
```

弱化或移出首屏：

- open_id
- tenant_key
- record_id
- Linux username
- 原始 smoke 日志

## 4. 内部服务接口

建议在 `app/services/approval_followup.py` 暴露纯函数：

```python
class PostApprovalSmokeOutcome:
    status: str
    admin_message: str
    user_message: str | None
    should_retry: bool
    next_retry_delay_seconds: int | None

def classify_post_approval_smoke(result: SmokeResult, attempt: int) -> PostApprovalSmokeOutcome:
    ...
```

调用方只消费结构化结果，不自己拼判断。

## 5. CLI / 手工补偿接口

自动复验需要后台补偿扫描。若仍需要手工排障，可复用或新增窄命令：

```bash
.venv/bin/python scripts/check_live_run.py --email <user-email> --mcp-smoke
```

第一版不强制新增专用 CLI。已有 live-run smoke 能覆盖手工排障即可。

## 6. 敏感信息规则

所有接口输出不得包含：

- `Authorization`
- `Bearer`
- token 明文
- 私钥
- ZIP 口令
- `.env` 内容
