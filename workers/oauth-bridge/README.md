# Bot-Test OAuth Bridge

仅供 #16 在 `biai-stage` 与 Bot-Test 的受控验收使用。

## 用户路径

飞书浏览器 OAuth 回调到 `https://biai-test.chunbai.com/oauth/callback`。Worker 只将一次性结果即时转发给已认证的 `biai-stage` WebSocket；身份换取、校验、建档和加密续期凭据保存均留在 `biai-stage`。默认回调页面仅等待 `identity_confirmed` 或 `retry` 两种无身份结果，不显示或保存身份资料或凭据。

进行“用户身份组织查询”验收时，`LINGXI_OAUTH_ORGANIZATION_PROBE_ONLY=enabled` 使 Bot-Test 只校验“私聊人与授权人一致”，并在当前回调页显示该次用户身份可读取的组织资料；不创建用户记录。默认不保存凭据；只有产品负责人明确开启 `LINGXI_OAUTH_PERSIST_PROBE_CREDENTIAL=enabled` 后，才会加密保存可轮换的 `refresh_token`，短期 `user_access_token` 仍绝不保存。此开关不属于正式用户路径。

受控调试时，只有以下两个条件同时成立，回调页才经**实时** WebSocket 显示本次得到的身份 ID，以及一份“资料可得性报告”：姓名、邮箱、手机、组织 ID/名称、按层级嵌套的部门 ID/名称、职务、职级和工作序列。报告会把未返回、无权限或不可见的项目明确标出：

1. Bot-Test 环境变量 `LINGXI_OAUTH_DEBUG_IDENTITY_DISPLAY=enabled`，且回跳域名严格为 `biai-test.chunbai.com`；
2. 测试 Worker 的 `DEBUG_IDENTITY_DISPLAY=enabled`。

这不是正式用户体验。身份资料不写日志、不入测试数据库，也不写入 Durable Object；浏览器尚未连接时，Worker 只保留无身份的成功/重试状态。

## 不可破坏的边界

- 仅部署精确的 `/oauth/*` Worker 路由；不得影响该域名既有入口。
- `STAGE_BRIDGE_TOKEN` 是 Worker secret，同时仅存在于 `biai-stage` 的 0600 环境文件；不提交、不打印、不写日志。
- Durable Object 只保留桥接连接和无身份结果，不保存授权码、身份、权限或令牌。
- 无在线桥接时回调返回重试提示，不暂存结果。

## 部署前置

1. Cloudflare 中确认 `biai-test.chunbai.com` 仅有 `/oauth/*` 测试路由。
2. 创建 Worker secret，并仅写入 Worker 与 `biai-stage` 的测试环境文件。
3. 将 `https://biai-test.chunbai.com/oauth/callback` 登记到 Bot-Test 的重定向 URL。
4. 只在受控验收中验证；不连接 Bot-Prod、生产数据库或生产发布。
