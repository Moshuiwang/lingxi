# JumpServer 登录迁移接口设计

> 日期：2026-06-26
> 状态：待实现
> 对应 PRD：`docs/product/jumpserver-access-migration-prd.md`

## 1. 用户通知

高级工作台已准备、JumpServer 权限待确认：

```text
你的高级数据分析工作台已准备好。

请通过 JumpServer Web 终端进入：
https://jumpserver.startimes.me

首次登录需要使用 LDAP 账号并绑定 Google Authenticator。
如果登录后看不到 BI AI Agent 机器，说明 JumpServer 资产权限还未开通，请联系运维或等待审批群处理。
进入机器后会自动进入 BI 工作目录，运行 claude 即可开始分析。
```

不得默认发送：

- EC2 公网地址。
- EC2 直连端口。
- EC2 私钥 ZIP。
- ZIP 解压口令。

## 2. 审批群/运维群提示

```text
高级工作台准备完成，JumpServer 权限待开通。

用户：张三（zhangsan@startimes.com.cn）
EC2 账号：zhangsan
目标：BI AI Agent 高级工作台

请在 JumpServer 中为该用户开通目标机器访问权限。
数据权限已按审批结果发布，JumpServer 仅负责登录入口。
```

如果未来接入 API，消息应改为：

```text
JumpServer 访问已开通。
用户可通过 Web 终端进入高级工作台。
```

## 3. 帮助文档第一屏

第一屏只讲 Web 终端：

1. 打开 JumpServer Web 地址。
2. 使用 LDAP 登录。
3. 绑定或输入 Google Authenticator。
4. 在资产列表中找到 BI AI Agent 机器。
5. 点击进入浏览器 Web 终端。
6. 进入后运行 `claude`。

本地 SSH 客户端、Xshell、Termius 放附录，不放主流程。

## 4. 内部接口

建议新增 service 结果对象：

```python
class JumpServerAccessPlan:
    status: str
    user_message: str
    admin_message: str | None
    requires_ops_action: bool

def build_jumpserver_access_plan(user, approval_record, *, api_available: bool = False) -> JumpServerAccessPlan:
    ...
```

调用方只展示结果对象，不散落拼接文案。

## 5. live-run 接口

验收口径从“用户能直连 EC2 SSH”改为：

```text
用户登录 JumpServer Web
  -> 能看到目标机器
  -> 能进入 Web 终端
  -> 默认落到 ~/bi-agent-work
  -> 运行 claude
  -> MCP smoke 通过
```

第一版如果无法自动验 JumpServer Web，可把这一步标为人工 live-run 项，不伪造自动通过。

