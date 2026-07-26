# 飞书关联组织身份、注册与鉴权参考证据

> 记录日期：2026-07-23
> 状态：已完成接口探索的历史参考材料，不是现行产品规则，也不是持续维护的需求文档。已经确认、仍影响用户预期的结论以[当前能力](../当前能力.md)为准；待确认选择应转入 GitHub Issue。
> 本文不保存 App Secret、访问令牌、个人邮箱、`open_id`、`user_id` 或 `union_id`。
>
> 现行规则：产品负责人于 2026-07-26 确认 Lingxi 数据库是用户权限的唯一权威来源，飞书多维表格只是数据库向 MCP 发布权限的下游通道；首次自动准入按数据库中的当前有效权限记录、以姓名匹配处理。请以[产品合同](../产品合同与外部边界.md)及[自动准入与数据库权限发布边界决策](../决策记录/2026-07-26-自动准入与数据库权限发布边界.md)为准。
>
> 本文各节的现行状态：
>
> - **第 2 节（实测的飞书事实）仍然有效**，是现行开通流程的证据基础。其中 2.3「用户授权后可取得三类 ID」是产品合同中开通流程取得用户标识的有效路径；2.4 说明的应用身份限制正是开通流程必须包含一次用户授权的原因。
> - **第 3 节的注册与鉴权设计已被取代**：其中以工作邮箱对齐员工目录与银河账号、并把银河记录直接作为授权依据的方案不再适用。3.2 中“用户完成一次飞书授权”这一步被现行流程保留，其余匹配环节改为按姓名匹配 Lingxi 数据库当前有效权限记录。
> - **第 4 至 6 节是当时的实施设想和待确认项**，不是现行规则；其中仍未验证的问题已收敛到[当前能力](../当前能力.md)的「产品合同已依赖但尚未验证的外部能力」。

## 1. 要解决的问题

Lingxi 只使用 `StarTimes` 付费租户内的一个飞书自建应用和 Bot 提供服务。真实员工分布在多个关联的免费租户；银河系统已经拥有他们的数据权限，但尚未与飞书身份建立对应关系。

需要让用户能够：

1. 在飞书私聊中心 Bot；
2. 可靠地绑定自己的银河账号与飞书身份；
3. 只获得已有银河权限范围内的数据；
4. 接收 Bot 消息以及 Bot 创建后授权给自己的飞书文档或电子表格。

## 2. 已实测的飞书事实

### 2.1 应用身份：取得关联组织成员的 Bot `open_id`

中心 Bot 使用 `tenant_access_token` 调用：

```text
GET /open-apis/directory/v1/share_entities
```

关键参数：

```text
target_tenant_key=<关联组织 tenant_key>
target_department_id=0 或下级 open_department_id
is_select_subject=false
page_size=100
```

从根部门开始递归下钻，可获得共享部门和成员。成员记录包含名称（多语言对象，应读取 `name.default_value`）和 `open_user_id`。已在“四达集团”的截图组织路径中定位到测试成员，并已用该 ID 成功发送中心 Bot 的跨租户单聊消息。

官方文档：[获取关联组织双方共享成员范围](https://open.feishu.cn/document/trust_party-v1/-collaboraiton-organization/list-3)。

### 2.2 应用身份：不能将关联组织 `open_id` 反查为邮箱

以下尝试均未能获得对方成员邮箱：

| 接口 | 结果 | 结论 |
|---|---|---|
| `POST /contact/v3/users/batch_get_id`，以工作邮箱查询 | 返回 0 个成员 | 中心租户的邮箱检索不解析关联组织成员。 |
| `GET /contact/v3/users/{open_id}` | `41050 no user authority error` | 标准通讯录资料接口受中心应用自己的数据范围限制。 |
| `GET /directory/v1/share_entities` | 成员记录无邮箱字段 | 它只用于共享范围和可交付身份。 |

因此，不能把“中心 Bot 实时通过邮箱查询关联组织成员”作为产品承诺。

### 2.3 用户身份：可取得三类关联组织 ID，但不返回邮箱

用户完成飞书授权后，以其 `user_access_token` 调用：

```text
GET /open-apis/trust_party/v1/collaboration_tenants/{target_tenant_key}/visible_organization
```

在用户可见的关联组织部门内，响应成员实体包含：

```text
user_id
open_user_id
union_user_id
user_name
```

再用用户身份调用：

```text
GET /open-apis/trust_party/v1/collaboration_tenants/{target_tenant_key}/collaboration_users/{target_user_id}
?target_user_id_type=user_id
```

也可取得同一成员的三类 ID、名称、头像、状态及父部门。实测响应仍不含标准 `email` 字段。

官方文档：[获取对方关联组织可见的部门和成员](https://open.feishu.cn/document/trust_party-v1/-collaboraiton-organization/visible_organization)、[获取关联组织成员信息](https://open.feishu.cn/document/trust_party-v1/-collaboraiton-organization/get-3)。

### 2.4 `trust_party` 应用身份的额外限制

同一 `visible_organization` / `collaboration_users` 接口使用中心应用的 `tenant_access_token` 返回：

```text
1971007: App not visible to target tenant.
```

这不影响 `directory/v1/share_entities` 读取共享范围，也不影响 Bot 向已经取得的 `open_id` 发送消息。飞书公开资料未找到该错误码的配置说明；不要把它当作可由普通“通讯录权限范围”解决的问题。需要时向飞书支持提交完整错误码、接口和关联组织关系确认。

### 2.5 用户令牌处理规则

`user_access_token` 会过期。本次探索使用的一次性用户令牌已从本地 `.env` 删除。

生产系统默认规则：

- 不记录用户访问令牌到日志、Issue、聊天、文档或代码仓库；
- 绑定流程只在内存中短暂使用令牌；
- 成功后只保存经确认的身份映射和绑定审计；
- 如果未来确有持续代表用户调用的场景，必须单独设计 OAuth 刷新令牌的加密保管、撤销和最小权限规则，不能沿用本次探索做法。

## 3. 推荐的注册与鉴权设计

### 3.1 两层身份模型

不要试图用一个 ID 同时解决飞书投递和银河授权。应保存两层、各司其职的绑定：

| 层 | 推荐主键 | 用途 |
|---|---|---|
| 飞书投递身份 | `关联组织 tenant_key + open_user_id` | 私聊、消息、文档/表格授权与交付。 |
| 员工目录身份 | `关联组织 tenant_key + user_id` | 与免费租户导出的员工目录精确对齐。 |
| 跨应用辅助标识 | `union_user_id` | 保存作诊断与后续验证；未经跨租户稳定性验证前，不作唯一主键。 |
| 银河授权身份 | 银河账号或已确认的工作邮箱 | 查询银河既有权限，不创建或扩大权限。 |

工作邮箱是员工目录和银河账号对齐的业务字段，不是中心 Bot 从飞书实时反查的字段。

### 3.2 推荐用户流程：首次绑定后无感使用

```text
用户私聊中心 Bot
        ↓
Bot 识别该会话的 open_id，提示“完成一次身份绑定”
        ↓
用户点击飞书授权链接，获得短期 user_access_token
        ↓
调用 visible_organization / collaboration_users，取得目标租户的 user_id、open_id、union_user_id
        ↓
以 (tenant_key, user_id) 精确匹配免费租户周期导出的员工目录
        ↓
目录记录中的工作邮箱匹配银河账号
        ↓
绑定成功：记录映射、启用既有银河权限、欢迎用户开始问数
```

用户看到的文案应是：

```text
为确认你的企业账号并加载已获批准的数据范围，请完成一次飞书身份确认。
不会新增或扩大你的数据权限。
```

不要向用户展示 `open_id`、`user_id`、`union_id`、令牌、租户键或“关联组织”等实现概念。

### 3.3 无法自动匹配时的体验

| 情况 | 系统动作 | 用户看到 |
|---|---|---|
| 员工目录唯一匹配且银河账号已存在 | 自动绑定 | “身份已确认，可以开始使用。” |
| 目录中无匹配 | 不开通数据权限，生成管理员待办 | “暂时无法确认你的企业账号，已提交人工核对。” |
| 目录或银河账号多条匹配 | 不猜测，生成管理员待办 | “发现多个可能账号，需要管理员确认后才能开通。” |
| 飞书用户授权取消、过期或失败 | 不保存不完整绑定 | “身份确认未完成；你可以重新确认。” |

管理员待办必须展示脱敏后的候选信息、来源租户、组织路径和原因；管理员确认后才可写入绑定。人工确认只是身份对齐，不得自动扩大银河权限。

### 3.4 已绑定用户的鉴权与交付

每次用户发起请求时：

1. 从飞书事件的发送者身份取得 Bot 侧 `open_id`；
2. 查找有效的飞书投递身份绑定；
3. 用已绑定的银河账号读取现有权限；
4. 通过 MCP 在该权限范围内执行；
5. 用同一 `open_id` 发送结果、创建并授权飞书文档/电子表格。

找不到绑定、绑定已禁用或银河权限无法确认时，禁止查询和交付敏感结果，并进入“重新确认 / 人工处理”状态。

## 4. 首期数据与审计要求

最小映射记录建议包含：

```text
binding_id
status: pending_claim | active | needs_review | disabled
source_tenant_key
source_user_id
bot_open_id
union_user_id (optional; not unique)
directory_record_id
galaxy_principal_id
verification_method: user_oauth + directory_match | admin_review
verified_at
disabled_at / disabled_reason
```

邮箱如需保存，应使用现有员工目录的最小必要字段并遵守公司保留政策；日志仅记录内部记录 ID 和状态，不记录明文邮箱、飞书 ID 或令牌。

每次绑定、重绑、人工确认、禁用、权限查询和文档交付都需要审计发起人、时间、结果和原因，但不记录访问令牌或查询结果正文。

## 5. 明天的实施顺序

1. **确认员工目录导出字段**：每个免费租户至少提供 `tenant_key`、`user_id`、工作邮箱、在职状态和更新时间；可选提供组织路径。仅有姓名与邮箱不足以无歧义自动绑定。
2. **确认银河映射来源**：定义“工作邮箱 → 银河账号/权限主体”的人工初始对齐和后续变更责任人。
3. **设计一次性飞书授权回调**：用户私聊后打开授权链接；回调仅在内存中使用 `user_access_token` 补齐 `user_id`，成功即丢弃令牌。
4. **实现绑定状态机与管理员待办**：先覆盖唯一匹配、无匹配、多匹配、授权失败和禁用五种状态。
5. **接入问数与交付门禁**：任何 MCP 查询和文档/表格分享都必须先通过有效绑定与银河权限检查。
6. **再评估 `union_user_id`**：用至少两个租户、同一员工的真实样本验证其稳定性后，才决定是否用于辅助去重。

## 6. 待产品负责人确认

1. 免费租户的周期导出是否可包含该租户的 `user_id`，以及更新频率和责任人。
2. 首次绑定是否允许用户主动输入工作邮箱；若允许，如何完成邮箱所有权验证。
3. 银河账号与邮箱无法唯一匹配时，哪个管理员角色负责确认，目标时效是多少。
4. 用户离职、换租户、换邮箱或换岗时，目录同步如何触发禁用或重新绑定。
5. 用户授权页面是否可对所有关联租户用户打开；如不行，是否采用管理员生成的一次性绑定链接。

## 7. 本地探索材料

本次只读/验证脚本位于 `scripts/`，仅用于后续受控复验：

- `verify_feishu_association.sh`
- `verify_feishu_user_message.sh`
- `verify_feishu_email_message.sh`
- `verify_feishu_org_path_email_message.sh`

它们依赖本地、被 Git 忽略的 `.env`；不得提交 `.env`，不得在脚本输出中添加密钥、令牌或员工个人资料。
