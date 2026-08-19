# 问数 MCP `list_metrics` 真实响应形状

> 最后核对：2026-08-19，来源为编排者在受控环境对真实问数 MCP 发起的第一次真实调用（只读、使用测试 MCP 令牌）。
>
> 本文回答一个长期问题：**真实问数 MCP 对 `list_metrics` 的返回，逐字长什么样，能不能从中数出指标条数。** 适用范围：`list_metrics` 一个工具的成功路径与无效令牌下的拒绝路径。
> 不适用范围：本文**不**证明 `query_metric` 与 `search_dimension` 的返回形状——这两个工具本次一次都没有调用过，未测就是未测，不得据此推断它们的形状与 `list_metrics` 相同。就绪探针的判定语义（`ready`/`waiting`/`technical_failure` 怎么分流）不在本文，见[技术设计/接口设计](../技术设计/接口设计.md#五问数-mcp消费方)与 `src/lingxi/core/permission/mcp_readiness.py`。

## 服务端事实

- 服务端自报：`MCP Metric Query Server 1.27.2`。
- 协议版本：`initialize` 握手中的 `protocolVersion` 为 `2025-06-18`。
- 传输：MCP Streamable HTTP + JSON-RPC 2.0，`tools/call` 方法——与本仓库此前按接口设计编写的假设一致（此前从未实测，见下方「此前的假设与现在的差异」）。
- `tools/list` 返回四个工具：`list_metrics`、`describe_metric`、`search_dimension`、`query_metric`。本文只实测了其中的 `list_metrics`。

## `list_metrics` 成功响应的逐字形状

`tools/call` 调 `list_metrics`（有效令牌）的 `result` 字段：

```json
{
  "content": [
    {
      "type": "text",
      "text": "{\n  \"metrics\": [\n    {\n      \"metric_id\": \"sub_new_count\",\n      \"name\": \"新增用户数\",\n      \"name_en\": \"New User Count\"\n    }\n  ]\n}"
    }
  ],
  "isError": false
}
```

也就是说：

1. `result` 里**没有 `structuredContent`**——本仓库此前唯一实现的 `default_metrics_reader` 只认这个键，因此在真实 MCP 上永远读不出指标数。
2. `content` 是一个只含**一个**元素的列表，该元素 `type == "text"`。
3. 该元素的 `text` 字段是**一段 JSON 字符串**（不是已解析的对象），解开后顶层只有一个键 `metrics`，值是对象列表。
4. 每个指标对象含三个字段：`metric_id`（英文标识）、`name`（中文名）、`name_en`（英文名）。

## 实测到的指标全集（9 条）

有效令牌下 `list_metrics` 返回的完整指标集合，`metric_id` → 中文名：

| `metric_id` | 中文名 |
|---|---|
| `channel_market_sharing` | 频道市占率 |
| `channel_rate` | 频道收视率 |
| `exchange_rate` | 汇率 |
| `sub_deduction_count` | 扣费用户数 |
| `sub_deduction_money` | 扣费金额 |
| `sub_new_count` | 新增用户数 |
| `sub_recharge_count` | 充值用户数 |
| `sub_recharge_money` | 充值金额 |
| `vat_rate` | 增值税率 |

上表 `metric_id` 与中文名逐字来自本次实测；`name_en` 只有 `sub_new_count` 一条（`New User Count`）在上方样本中逐字可见，其余 8 条的 `name_en` 未被本次实测逐字记录——测试断言中据此推断的英文名只是合理翻译，不作为已验证事实。

**与 [#227](https://github.com/Moshuiwang/lingxi/issues/227) 映射内容的关系**：今日实测的 9 个 `metric_id` 与 #227 登记的映射全集（取自 2026-07-28）逐一比对，**无缺失、无新增**——那条映射内容不再只有 2026-07-28 单次快照的支撑，现在有 2026-08-19 的独立复测。

## 拒绝路径：无效令牌

无效令牌调用 `list_metrics`：HTTP 状态码 **401**，响应体是 JSON-RPC `error`：

```json
{"error": {"message": "Unauthorized: invalid token"}}
```

`adapters/query_mcp_probe.py` 的分类只按**状态码**走：401 命中 `DEFAULT_DENIED_STATUS_CODES`，在解析 JSON-RPC 载荷之前就已经判定为「明确拒绝」→ 就绪判定层落 `waiting`（即「同步中」）。这与现有默认判据逐位一致，**本次实测不要求改动这条判据**，只是确认它在真实服务端上成立。JSON-RPC 错误码白名单、`result.isError` 语义仍按 L1 保守处理——这两条本次未涉及。

## 此前的假设与现在的差异

`adapters/query_mcp_probe.py` 模块文档此前登记「真实 MCP 协议面未实测（L1）」，按接口设计的假设编写：端点形态、协议版本、`list_metrics` 返回形状、"权限还没同步"的错误形态，四项全部未经证实。本次实测（Issue #253）确认了前两项，且第三项（返回形状）与假设**不一致**——假设的是 `result.structuredContent.metrics`，实测到的是 `result.content[0].text` 内层 JSON 的 `metrics`。第四项里的"无效令牌"子情形已确认为 HTTP 401，其余错误形态（JSON-RPC 错误码、`isError`）仍未实测。

`adapters/query_mcp_probe.py` 的 `default_metrics_reader` 未按本次实测改动，仍只认 `structuredContent`——它是"真实形状还没实测时"的收窄兜底与历史语义。装配层（`apps/scheduler/assembly.py` 的 `_build_readiness_follow_up`）改为显式注入一个新增的、按本文实测形状实现的 `content_text_metrics_reader`，让就绪探针在真实 MCP 上能够正确数出指标条数。

## 诚实边界

- 本次只实测了 `list_metrics`；`query_metric` 与 `search_dimension` **一次都没有调用过**，其返回形状、错误形态与 `list_metrics` 是否一致均未知。
- 只测试了两条路径：有效令牌的成功响应、无效令牌的拒绝响应。权限已发布但 MCP 尚未拉取到的"同步中"状态、工具执行失败（`isError: true`）等路径未实测。
- 多个 `content` 块、`type` 非 `text` 的块等真实是否会出现，未实测——`list_metrics` 目前观察到的响应只有一个文本块。
