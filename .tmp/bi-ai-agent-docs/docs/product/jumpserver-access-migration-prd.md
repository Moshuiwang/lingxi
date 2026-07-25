# JumpServer 接入迁移影响与用户体验方案

> 日期：2026-06-25
> 状态：待实现
> 输入材料：`/Users/wangzhipeng/Downloads/jumpserver跳板机登录方式.pdf`
> 范围：只覆盖“高级数据分析工作台”用户从直连 EC2 SSH 迁移到 JumpServer 的影响。不覆盖默认飞书聊天准入改造；该部分见 `docs/product/default-feishu-chat-access-prd.md`。

## 一、背景事实

当前 BI AI Agent 的高级工作台开户链路默认是：

1. 用户在飞书私聊提交权限申请；
2. 审批通过后系统自动创建 EC2 Linux 用户；
3. 系统生成 SSH key pair；
4. 邮件发送加密 ZIP，飞书私聊发送 ZIP 解压口令；
5. 用户按邮件里的主机、端口、私钥说明直接 SSH 到 EC2；
6. 登录后自动进入 `~/bi-agent-work`，运行 `claude` 查询 BI。

新的外部要求是：**需要进入 EC2 高级工作台的普通用户，不能再直接 SSH 进入 EC2，必须通过 JumpServer 跳板机进入。**

PDF 中给出的 JumpServer 关键信息：

| 环境 | Web 地址 | SSH 域名 | 端口 | 账号/密码 | SSH 客户端 key |
|---|---|---|---|---|---|
| 外网 JumpServer（用户生产环境） | `https://jumpserver.startimes.me` | `jumpserver-ssh.startimes.me` | `4222` | LDAP | 仅本地 SSH 客户端需要，Web 终端不需要 |
| 内网 JumpServer（一般用户测试环境） | `https://jumpserver-local.startimes.me` | `jumpserver-local-ssh.startimes.me` | `2222` | LDAP | 仅本地 SSH 客户端需要，Web 终端不需要 |

首次使用 JumpServer 需要：

- 使用 LDAP 登录 Web；
- 绑定 Google Authenticator 二次认证；
- 在 Web 端填写个人信息；
- 默认可直接通过浏览器 Web 终端进入授权机器，不需要配置本地 SSH key；
- 只有选择本地终端、Xshell、Termius 等 SSH 客户端时，才需要在 Web 端生成 SSH 密钥或上传个人公钥；
- 如果登录后看不到机器，需要联系运维添加机器权限；
- 本地 SSH 客户端登录 JumpServer 时还需要输入 Google Authenticator 动态码。

## 二、边界说明

这份文档只处理高级工作台的登录方式迁移。

不在本文中定义：

- 用户申请时是否默认开通飞书聊天；
- 高级工作台是否作为可选权限；
- 飞书聊天权限如何审批、通知、补开或回收。

这些属于准入权限模型改造，单独沉淀在 `docs/product/default-feishu-chat-access-prd.md`。

## 三、核心判断

JumpServer 不是新的数据权限入口，而是高级工作台的登录入口变化。

用户能看到什么数据，仍由 BI AI Agent / MCP 权限控制。不能让用户产生“飞书查不到的数据，可以去 JumpServer 绕过”的理解。

对高级工作台用户来说，变化是：

- 从“系统给我 EC2 私钥，我直连服务器”；
- 变成“我通过 JumpServer Web 终端进入自己的高级工作台”。

## 四、目标体验

高级工作台用户应感受到：

1. 我不再需要保存 EC2 直连私钥；
2. 我不需要知道 EC2 公网地址；
3. 我默认用浏览器进入 JumpServer，不需要先配置 SSH key；
4. 我进入机器后仍然落在自己的工作目录，可以继续运行 `claude`；
5. 如果 JumpServer 看不到机器，我知道这是高级工作台授权问题，不是 BI 数据权限没批。

管理员应感受到：

1. 能区分“高级工作台已准备好”和“JumpServer 访问已开通”；
2. 能知道失败卡在 EC2 工作区、MCP 工作区配置，还是 JumpServer 授权；
3. 停用用户时能回收高级工作台和 JumpServer 资产权限。

## 五、对高级工作台开通的影响

### 5.1 审批通过后的状态

高级工作台开通建议拆成三层：

| 层级 | 含义 | 用户/审批群表达 |
|---|---|---|
| 高级工作台 | EC2 Linux 用户、Claude 环境、MCP 工作区配置已准备 | “高级工作台已准备好” |
| JumpServer 访问 | 用户在 JumpServer 中能看到并进入目标机器 | “JumpServer 访问已开通 / 待运维开通” |
| 数据权限 | 用户在工作台里能查询哪些指标 | “可查询指标已开通” |

只有三层都满足，才适合对用户说“高级工作台可以开始使用”。

第一版如果还不能自动调用 JumpServer API，不要假装自动完成。应明确显示：

```text
高级工作台已准备好。JumpServer 机器权限如未出现，请联系运维添加。
```

### 5.2 登录材料交付

当前“邮件 ZIP + 飞书口令”的 EC2 私钥交付方式需要重新评估。

目标态：

- 不再向普通用户发送 EC2 直连私钥；
- 对已申请并获批高级工作台的用户，默认推荐通过 JumpServer 浏览器 Web 终端登录授权机器；
- 用户只有在选择本地终端、Xshell、Termius 等 SSH 客户端时，才需要由 JumpServer 生成个人 SSH key，或自行上传公钥；
- EC2 目标账号的密钥/认证关系由 JumpServer 和运维侧托管，不暴露给普通用户；
- BI Agent 只告诉用户 JumpServer Web 入口、MFA、如何选择目标机器、登录后运行 `claude`。

如果短期仍需要系统生成 EC2 key 供 JumpServer 资产账号使用，也不应发给普通用户。它属于后台/运维材料，不是用户交付材料。

## 六、对用户日常使用的影响

### 6.1 默认路径：浏览器 Web 终端

高级工作台用户日常路径应改为：

1. 打开 JumpServer Web，完成 LDAP 登录和 Google Authenticator 绑定；
2. 确认自己能看到 BI AI Agent 目标机器；
3. 在浏览器里打开目标机器的 Web 终端；
4. 进入被授权的 EC2 目标账号；
5. 自动进入 `~/bi-agent-work`；
6. 运行 `claude` 开始查询。

用户不应再看到“直接 ssh 到 `biai.chunbai.com`”作为主路径。

用户也不应在默认路径里被要求配置：

- `~/.ssh/config`；
- SSH 私钥文件；
- `chmod 400`；
- Xshell/Termius key 文件。

### 6.2 帮助文档

高级工作台帮助文档应优先讲浏览器路径：

1. 打开 JumpServer Web 地址；
2. 用 LDAP 账号密码登录；
3. 绑定或输入 Google Authenticator 动态码；
4. 在资产列表中找到自己的机器；
5. 点击进入浏览器 Web 终端；
6. 进入 `~/bi-agent-work` 后执行 `claude`。

文档第一屏不应出现 `HostName`、`Port`、`IdentityFile` 这类 SSH 客户端配置。

### 6.3 可选路径：本地终端/SSH 客户端

如果用户明确想用 Mac 终端、Xshell、Termius 等本地 SSH 客户端，可以在帮助文档附录里给出配置。

Mac 可选示例：

```sshconfig
Host jumpserver-online
  User <LDAP用户名>
  Port 4222
  HostName jumpserver-ssh.startimes.me
  IdentityFile ~/.ssh/<你的JumpServer私钥>.pem
```

然后：

```bash
chmod 400 ~/.ssh/<你的JumpServer私钥>.pem
ssh jumpserver-online
```

登录时输入 Google Authenticator 动态码。

Windows 可选说明：

- 使用 Xshell/Termius 等终端工具；
- 主机填 `jumpserver-ssh.startimes.me`；
- 端口填 `4222`；
- 用户名填 LDAP 用户名；
- 认证方式选择 Public Key；
- 私钥选择 JumpServer Web 中生成或下载的最新密钥；
- 不需要输入 SSH 密码；
- 弹出的动态码输入 Google Authenticator 中的 6 位数字。

本地 SSH 客户端不应出现在高级工作台帮助文档的第一屏，也不应让用户误以为“配置 SSH key 是必做步骤”。

## 七、对现有系统流程的影响清单

### 7.1 飞书用户通知

高级工作台开通后，不能再使用这些旧表达：

- “邮件已发送 SSH 私钥 ZIP”；
- “飞书已发送 ZIP 解压口令”；
- “按邮件里的主机、端口、私钥登录 EC2”。

应替换为：

- “高级数据分析工作台已通过”；
- “请按 JumpServer 帮助文档进入高级工作台”；
- “如果 JumpServer 中看不到机器，请联系运维开通机器权限”；
- “进入机器后会自动到 BI 工作目录，运行 `claude` 即可”。

### 7.2 邮件正文

如果高级工作台获批并需要邮件，邮件不应再把 EC2 主机、端口和私钥作为主内容。

建议高级工作台邮件只保留：

- 你的高级数据分析工作台已通过；
- 你的目标机器/账号显示名；
- JumpServer Web 地址；
- 帮助文档链接：浏览器 Web 终端默认路径；
- 本地 SSH 客户端可选附录链接；
- 常见问题：看不到机器、动态码错误、本地 SSH 客户端密钥重置。

邮件正文不建议直接展开 JumpServer SSH 地址、端口、SSH key、`chmod` 等内容，避免用户误解为必做步骤。

### 7.3 飞书私聊口令

如果不再发 EC2 私钥 ZIP，就不应该再发 ZIP 解压口令。

这会影响当前 `ssh_delivery_status` 的语义：它现在表示 ZIP 和口令双通道交付。JumpServer 目标态下，它应被替换或扩展为“高级工作台待准备 / JumpServer 权限待确认 / 高级工作台已开通”。

### 7.4 自动开户

高级工作台用户：

- EC2 Linux 用户仍可能需要创建，因为 Claude Code、`~/.mcp.json`、`CLAUDE.md`、`bi-agent-work` 都在 EC2 用户环境中；
- 需要准备高级工作台所需的 MCP 工作区配置；
- 需要触发 JumpServer 资产授权流程，或给运维团队产生明确待办。

高级工作台相关变化点是：

- 创建 Linux 用户仍保留；
- 但普通用户不再直接拿 EC2 私钥；
- JumpServer 需要知道这个目标账号/资产权限；
- 是否还需要写入 `~/.ssh/authorized_keys`，取决于 JumpServer 接入 EC2 的方式。

如果 JumpServer 使用托管资产账号，`authorized_keys` 的 owner 和用途会发生变化。此处实现前必须由运维确认。

### 7.5 文件下载/上传边界

PDF 中提到 JumpServer 支持通过 SFTP 到目标主机 `/tmp` 上传/下载文件。

这对 BI AI Agent 不能直接作为默认用户能力开放。原因：

- 当前产品默认文件出口是 BI Plus / 飞书文件发送能力；
- 普通业务用户原设计不支持 SFTP、scp、rsync、端口转发；
- 直接教用户用 JumpServer SFTP 取文件，会绕开我们正在收敛的数据出口体验和审计边界。

因此用户帮助文档中应避免把 SFTP 作为 BI 查询结果下载主路径。若公司安全策略允许 JumpServer SFTP，需要单独定义“什么文件允许通过 `/tmp` 下载、谁审批、如何审计”，不要混进普通登录教程。

### 7.6 live run 验收

现有验收里“SSH port reachable = OK”和“用户按邮件私钥直连登录”不再是用户视角的最终验收。

未来验收要拆成：

| 验收项 | 说明 |
|---|---|
| EC2 账号准备 | Linux home、Claude、MCP 工作区配置按现有脚本验 |
| JumpServer 可见性 | 用户 LDAP 登录后能看到目标机器 |
| JumpServer Web 终端登录 | 用户在浏览器中能进入目标机器 |
| JumpServer SSH 客户端登录 | 仅作为可选验收，用户通过 `jumpserver-ssh.startimes.me:4222` 能进入目标机器 |
| 登录落点 | 用户进入后自动在 `~/bi-agent-work` |
| Claude 可用 | 用户运行 `claude` 不弹登录，能查可见指标 |
| 文件出口 | 默认使用 BI Plus / 飞书文件发送，不要求用户 SFTP |

### 7.7 停用/离职

禁用开通过高级工作台的用户时，不应只停 EC2 Linux shell 和 MCP 权限，还要回收 JumpServer 访问。

未来停用应包含：

- BI Agent 用户状态禁用；
- 数据权限发布为空或 disabled；
- EC2 目标账号封锁；
- JumpServer 资产授权回收；
- 如由 LDAP/HR 管理离职，则与 HR/LDAP 状态对齐。

## 八、需要向运维确认的问题

实现前必须确认以下问题，不能靠猜：

1. JumpServer 中 BI AI Agent EC2 资产的名称是什么？
2. 普通用户进入资产后，是映射到自己的 Linux 用户，还是使用 JumpServer 托管系统用户？
3. JumpServer 是否有 API 可供 BI Agent 审批通过后自动授权资产？
4. 如果没有 API，第一版是否由审批群提示“待运维开通 JumpServer 权限”？
5. JumpServer 授权以 LDAP 用户名、邮箱还是 open_id 对应？
6. EC2 `authorized_keys` 未来由谁维护：BI Agent、JumpServer，还是运维？
7. 普通 BI 用户是否允许使用 JumpServer SFTP？如果允许，范围是否仅限 `/tmp`，如何审计？
8. EC2 安全组何时关闭普通用户直连 SSH，只保留 JumpServer / 管理员来源？
9. 现有真实用户迁移时，是否需要逐个通知并作登录复验？
10. 高级工作台申请获批后，给 JumpServer 运营团队的授权信息应包含哪些字段？

## 九、后续实现任务包

### 任务 A：高级工作台帮助文档替换

目标：

- 高级工作台用户看到 JumpServer 登录路径；
- 邮件/飞书不再以 EC2 私钥直连为主；
- 高级工作台帮助文档覆盖浏览器 Web 终端、MFA、看不到机器；
- Mac/Windows 本地 SSH 客户端作为可选附录，包含密钥生成、上传和重置。

验收：

- 高级工作台用户不需要理解 EC2 公网地址；
- 高级工作台用户知道自己要用 LDAP + Google Authenticator 登录 JumpServer；
- 高级工作台用户知道默认可以在浏览器里进入机器，不需要先配置 SSH key；
- 高级工作台用户知道看不到机器时是 JumpServer 权限问题。

### 任务 B：高级工作台交付状态重定义

目标：

- 把当前 `ssh_delivery_status` 从“ZIP/口令交付”调整到高级工作台和 JumpServer 分层语义；
- 不再把 EC2 私钥作为普通用户交付物；
- 审批群能看到高级工作台是否准备好、JumpServer 权限是否待处理。

验收：

- 高级工作台用户收到的是 JumpServer 指引；
- 不发送无意义的 ZIP 口令；
- 管理员能看出卡在哪一层。

### 任务 C：JumpServer 授权流程

目标：

- 明确 JumpServer 授权是自动还是人工；
- 若自动，接入 JumpServer API；
- 若人工，审批群给出稳定的运维待办提示；
- 只有高级工作台申请获批后，才触发 JumpServer 授权流程。

验收：

- 申请高级工作台的用户能在 JumpServer 看到目标机器；
- 未申请高级工作台的用户不会产生 JumpServer 待办；
- 授权失败不会被误报为 BI 权限失败；
- 停用用户时 JumpServer 权限也被回收。

### 任务 D：live run 和 runbook 更新

目标：

- `live-run-checklist` 从直连 EC2 SSH 改为 JumpServer 登录验收；
- `runbook` 明确管理员直连和普通用户 JumpServer 的边界；
- preflight 不再用“公网 22 端口可达”作为用户登录成功标准。

验收：

- 高级工作台用户真实从 JumpServer 登录成功；
- 高级工作台用户登录后自动进入 `bi-agent-work`；
- 高级工作台用户 `claude` 和 MCP smoke 通过；
- 直连 EC2 SSH 不再作为普通用户路径。

## 十、暂不做

本文件只固定影响和方案，不立即要求：

- 不修改代码；
- 不修改当前 runbook 的现行命令；
- 不关闭 EC2 SSH；
- 不删除现有 SSH key 交付逻辑；
- 不承诺 JumpServer API 已可用；
- 不把 PDF 原文完整复制进仓库；
- 不处理“默认飞书聊天开通”的申请/审批改造。

## 十一、成功标准

迁移完成后，高级工作台用户的体验应是：

- “我不用保存 EC2 私钥，也不用知道 EC2 公网地址”；
- “我按 JumpServer 文档进入高级工作台”；
- “我默认用浏览器进入机器，不需要先配置 SSH key”；
- “我进到机器后可以直接运行 `claude` 做持续深度分析”；
- “如果 JumpServer 看不到机器，我知道这是高级工作台授权问题，不是 BI 数据权限没批”。

管理员的体验应是：

- 能清楚区分高级工作台准备状态和 JumpServer 授权状态；
- 能知道失败卡在哪一层；
- 停用用户时能按需收回高级工作台、EC2 登录和 JumpServer 资产权限。
