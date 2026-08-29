# Changelog

本文件记录 Lingxi 对用户可见与外部边界有影响的变化，遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 约定；版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。版本规则、发布 tag 与镜像 tag 的关系见 [deploy/README.md「版本号规则」](deploy/README.md#版本号规则)。

条目按发布批次维护，不逐 PR 记录；每条引用对应 Issue 便于追溯。当前能力的完整事实、证据层级与已知边界以 [docs/当前能力.md](docs/当前能力.md) 为准，本文件不重复其细节。

## [Unreleased]

## [2.0.0] - 2026-08-30

首个正式版本号；本仓库此前无对外版本号（起版即从 2.0 开始，见 Issue #417）。以下为 2.0 发布前已实现、并在 `biai-stage` + `Bot-Test` 受控环境完成 L4a 及以上验收的核心能力（生产环境 `biplus-prod`/`Bot-Prod` 仍未部署）。

### Added

- **问数**：飞书私聊自然语言问数闭环，多轮上下文追问、`/new` 重置当前话题、忙碌态与并发提示、结果安全过滤（`withheld`/`masked`）、`/stop` 停止（Issue #154、#90、#189）。
- **首次开通链**：花名册与银河权限自动匹配、开通编排、权限发布后 MCP 就绪确认、开通中途失败的可追溯与自愈重发（Issue #65、#89、#203、#280、#282）。
- **文档交付**：正式产物随对话在线文档路由，文档级可管理授权（Issue #97）。
- **表格交付**：飞书电子表格产物，复用文档交付同构路由与检查点架构（Issue #354）。
- **用户记忆**：`/memory remember`·`list`·`forget` 显式登记式记忆（术语映射/口径偏好/惯例模板），跨用户隔离，停用即清（Issue #357）。
- **admin 命令面**：`/admin help`·`user`·`audit` 只读查询、`/admin suspend`·`resume` 确认卡片闭环、`/admin trace <追溯号>` 开通失败追溯查询（Issue #95、#96、#337）。
- **权限发布**：银河翻译 ∪ 本地授权 ∪ 存量沿用 − 本地抑制四源聚合，未翻译标签发布闸（Issue #226、#227、#328）。
- **监控告警与部署运维**：四进程镜像发布、compose 资源限制、30 天日志留存、最小监控告警（unhealthy 注入→管理群告警+恢复通知）、生产部署 runbook、备份恢复演练（Issue #62、#153、#343、#135、#369）。
- **版本号管理**：引入 SemVer、整仓单一版本、发布随批次打 `v<版本>` git tag、CHANGELOG 随发布批次维护（Issue #417）。

### Removed

- JumpServer 高级工作台入口：不再属于产品范围，飞书私聊成为唯一用户入口（Issue #368，[决策记录](docs/决策记录/2026-08-28-取消JumpServer高级工作台.md)）。
