# Lingxi

本仓库用于建设一个以飞书为用户入口、以受控 AI 能力完成业务分析与交付的产品。

任何开发任务先阅读 [AGENTS.md](AGENTS.md)。只有涉及产品规则、能力、长期决策或文档治理时，才按其中的路由进入[产品文档索引](docs/README.md)及相关文档。

仓库文档是长期产品事实的唯一正文；动态工作统一管理：Issue 记录需求、缺陷、研究与待决策事项，PR 记录实现和验证，具体 Trace 的优先级、负责人和进度记录在 [docs/traces/](docs/traces/README.md)（对应 `[tracking]` Issue 只作瘦指针）。唯一例外是长期执行方法：完整正文与后续修订只在 GitHub [Issue #147](https://github.com/Moshuiwang/lingxi/issues/147) 维护（现行版本见该 Issue 顶部）。关闭 Issue 前，仍有效的结论必须回写到仓库文档。

## 命令速查（产品负责人/管理员日常用）

### 管理员命令（在你与机器人的私聊里发；写动作会出确认卡，**10 分钟内点击有效**）

```
/admin help                                            查看可用命令
/admin user <open_id>                                  查用户状态与本地权限覆盖（含覆盖ID）
/admin audit <open_id> <小时数>                         查该用户近 N 小时审计
/admin trace <追溯号>                                   查开通失败原因
/admin suspend <open_id> <原因>                         停用用户（确认卡）
/admin resume <open_id> <原因>                          恢复用户（确认卡）
/admin revoke_permission <覆盖ID> <原因>                 收回一条本地覆盖（覆盖ID 用 /admin user 查，lpo_ 开头；确认卡）
```

注意事项（2026-08-30 实测校准）：
- 同一用户同时只允许一条待确认操作；若提示「已有一条待确认操作在途」，找到**旧确认卡点任意按钮**即可释放（改进中，见 issue）。
- 补授生效时点：当前随每日权限重算（UTC 零点/进程重启）批量发布；「确认后即时生效」改进中，见 issue。
- 若用户挂全公司通配角（如 513），补授会被判冗余跳过（有审计）；「有限指标的通配形态误判」修复中，见 issue。

### 用户命令（业务会话内）

```
/memory remember <类型> <内容>    登记记忆（类型：term_mapping 等三类）
/memory list                     查看已登记记忆
/memory forget <id>              删除一条记忆
/memory clear                    清空记忆
/new                             开新会话
```

（本节为速查入口；权威语义以 docs/产品合同与外部边界.md 与代码为准。）
