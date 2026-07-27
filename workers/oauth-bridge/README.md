# Bot-Test OAuth Bridge

仅供 #16 在 `biai-stage` 与 Bot-Test 的受控验收使用。

## 用户路径

飞书浏览器 OAuth 回调到 `https://biai.chunbai.com/oauth/callback`。Worker 只将一次性结果即时转发给已认证的 `biai-stage` WebSocket；身份换取、校验和建档均留在 `biai-stage`。回调页面仅等待 `identity_confirmed` 或 `retry` 两种无身份结果，不显示或保存身份资料。

## 不可破坏的边界

- 仅部署精确的 `/oauth/*` Worker 路由；不得影响该域名既有入口。
- `STAGE_BRIDGE_TOKEN` 是 Worker secret，同时仅存在于 `biai-stage` 的 0600 环境文件；不提交、不打印、不写日志。
- Durable Object 只保留桥接连接的无身份角色标记，不保存授权码、身份、权限或令牌。
- 无在线桥接时回调返回重试提示，不暂存结果。

## 部署前置

1. Cloudflare 中确认 `biai.chunbai.com` 现有路由不会与 `/oauth/*` 冲突。
2. 创建 Worker secret，并仅写入 Worker 与 `biai-stage` 的测试环境文件。
3. 将 `https://biai.chunbai.com/oauth/callback` 登记到 Bot-Test 的重定向 URL。
4. 只在受控验收中验证；不连接 Bot-Prod、生产数据库或生产发布。
