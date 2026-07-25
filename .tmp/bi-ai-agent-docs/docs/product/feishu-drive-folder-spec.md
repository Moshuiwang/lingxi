# 飞书云盘用户文件夹功能 — 设计与交接文档

更新时间：2026-07-23

---

## 一、背景与决策

**问题：** 原有 `feishu_file_sender` 把文件以消息附件（file message）形式发送到用户飞书聊天，用户必须手动下载、无法在线预览、文件散落在聊天记录里，历史文件无法追溯。

**决策：** 改为"上传飞书云盘 + 发链接通知"策略：
- 每个 Linux 用户在指定文件夹下拥有专属子文件夹
- 文件保存到云盘（可预览、可追溯、可撤权）
- 向用户发飞书文本消息，附带文件夹链接

**兼容策略：** 保留消息附件作 fallback（`drive_folder_token` 为空时退回旧行为），确保已开账用户不受影响。

---

## 二、飞书 API 能力验证结论

> 验证日期：2026-06-06，App ID：`cli_aa90705ae7789bc4`

| 能力 | API | 结论 |
|------|-----|------|
| 列云盘文件 | `GET /drive/v1/files` | ✅ |
| 在指定文件夹创建子文件夹 | `POST /drive/v1/files/create_folder` | ✅ |
| 上传原始文件（xlsx/csv/pdf 等） | `POST /drive/v1/files/upload_all` | ✅ |
| 创建飞书表格（Sheet） | `POST /sheets/v3/spreadsheets` | ✅ |
| 创建多维表格（Bitable） | `POST /bitable/v1/apps` | ✅ |
| 创建飞书云文档（Docx） | `POST /docx/v1/documents` | 代码已接入；当前应用需开通 `docx:document` / `docx:document:create` 权限 |
| 授权协作者（可编辑） | `POST /drive/v1/permissions/{token}/members` | ✅（接口可达，真实 open_id 验证通过） |
| 设置文件安全策略 | `PATCH /drive/v1/permissions/{token}/public` | ✅ 支持 doc/sheet/file/bitable/docx，不支持 folder |
| 删除文件/文件夹 | `DELETE /drive/v1/files/{token}` | ✅ |

**关键约束：** 飞书 `security_entity` 设置不支持 `type=folder`，只能在具体文件上设置。因此每个文件创建/上传后，须单独调一次 `PATCH /permissions/public` 设置 `security_entity: anyone_can_edit`（含义：仅可编辑用户能复制、下载、导出）。

---

## 三、文件夹结构

```
飞书云盘根（NIdLfpf2AlETd7dfAs5cbyINnef）
└── {linux_username}/          ← 每个用户一个子文件夹
    ├── report_2026-06.xlsx
    ├── summary.md
    └── ...
```

根文件夹 URL（可浏览器打开）：
```
https://gv3qfk4q2rp.feishu.cn/drive/folder/NIdLfpf2AlETd7dfAs5cbyINnef
```

用户子文件夹 URL 格式：
```
https://gv3qfk4q2rp.feishu.cn/drive/folder/{drive_folder_token}
```

---

## 四、代码结构

### 新增文件

**`app/feishu/drive.py`** — Drive API 封装层

| 函数 | 作用 |
|------|------|
| `infer_drive_action(file_name)` | 根据扩展名返回 `'docx'` / `'sheet'` / `'upload'` |
| `resolve_or_create_user_subfolder(linux_username, token)` | 完整列取根目录后精确复用同名文件夹；仅在确认不存在时创建，返回 token、来源和匹配数 |
| `grant_user_subfolder(folder_token, open_id, token)` | 为已确定的用户文件夹授予用户 edit 权限 |
| `upload_file_to_drive(subfolder_token, file_name, file_obj, token, size=)` | 上传原始文件，设置 security，返回 `{file_token}` |
| `create_sheet_in_folder(subfolder_token, title, token)` | 创建飞书表格，返回 `{spreadsheet_token, url}` |
| `import_sheet_from_file_in_folder(subfolder_token, file_name, file_obj, token, size=)` | 将 CSV/XLS/XLSX 导入为飞书原生表格，返回 `{spreadsheet_token, url}` |
| `create_docx_in_folder(subfolder_token, title, token)` | 创建飞书文档（需 `docx:document:create` 权限） |
| `create_bitable_in_folder(subfolder_token, title, token)` | 创建多维表格，返回 `{app_token, url}` |
| `_set_security(file_token, file_type, token)` | 设置 `security_entity=anyone_can_edit`，失败只记 warning 不抛异常 |

### 修改文件

| 文件 | 改动摘要 |
|------|---------|
| `app/models/user.py` | `User` 表新增 `drive_folder_token VARCHAR(128) NULLABLE` |
| `app/db.py` | `_ensure_sqlite_schema()` 自动迁移 `users.drive_folder_token` 列 |
| `app/config.py` | 新增 `get_drive_root_folder()` / `get_feishu_tenant_domain()` |
| `app/services/provisioning.py` | 新增 `provision_drive_folder(db, record)` — 非致命，失败记日志不阻塞开账 |
| `app/services/ssh_delivery.py` | 开账流程在 `provision_user_environment` 之后调用 `provision_drive_folder` |
| `app/services/feishu_file_sender.py` | `send_feishu_file` 路由：有 `drive_folder_token` → `_send_via_drive`；无 → `_send_via_message`（原逻辑 fallback） |

### 测试文件

`tests/test_feishu_drive.py`：覆盖所有 drive.py 公开函数及错误路径，包括云盘上传、文档创建、表格导入任务和安全策略。

---

## 五、配置说明

`.env` 需要新增两个变量：

```bash
# 所有用户文件夹的根目录（飞书云盘 folder_token）
FEISHU_DRIVE_ROOT_FOLDER=NIdLfpf2AlETd7dfAs5cbyINnef

# 飞书租户域名，用于拼接文件夹链接（通知消息里的 URL）
FEISHU_TENANT_DOMAIN=gv3qfk4q2rp.feishu.cn
```

`FEISHU_TENANT_DOMAIN` 缺失时，普通文件夹通知仍可降级为不带链接的纯文字提示；Markdown→Docx 会在创建文档前以 `validate_configuration/missing_tenant_domain` 失败，避免产生无法交付链接的文档副作用。

---

## 六、完整流程说明

### 6.1 开账时（一次性）

```
审批通过 → ssh_delivery.py 执行
  ↓
provision_user_environment(db, record)     # Linux 用户创建（原有）
  ↓
provision_drive_folder(db, record)          # 新增步骤
  ├── resolve_or_create_user_subfolder(linux_username, token)
  │     ├── 分页 GET /drive/v1/files，精确匹配 name + type=folder
  │     ├── 已存在 → 复用创建时间最早的匹配项；同一时间按 token 升序
  │     └── 确认不存在 → POST /drive/v1/files/create_folder 一次
  ├── grant_user_subfolder(folder_token, user.open_id, token)
  │     └── POST /drive/v1/permissions/{folder_token}/members
  │           type=folder, perm=edit, member=open_id
  └── 仅授权成功后将 folder_token 写入 users.drive_folder_token，commit
```

目录解析遵循 fail-closed：只有完整分页查询成功且确认没有同名文件夹时才允许创建；查询失败、响应异常、分页异常或创建结果不确定时，本次不再创建或重试。多个同名文件夹只记录数量，并按 `created_time` 升序、token 升序确定性复用第一项；本流程不自动删除或合并历史目录。

失败处理：`provision_drive_folder` 捕获稳定、脱敏的 Drive 错误，rollback，并返回 `{skipped: True, reason: "drive_error", stage, error_code}`。授权失败时不保存 `drive_folder_token`，文件交付继续使用原有消息附件兜底；下次触发会先查找并复用同一目录，不会再次创建。开账主流程（SSH 密钥、密码邮件）不受影响。

每次处理写入 `drive_folder_event` 结构化服务日志，覆盖开始、目录解析、复用或创建、授权、数据库保存和最终结果。日志固定记录 `source=ssh_delivery`、运行 profile、应用版本、`approval_id`、`delivery_run_id`、`operation_id`、内部用户 ID、阶段、结果、匹配数、HTTP 状态、飞书错误码、飞书 `logid`（若存在）、耗时以及目录/open_id 的不可逆短指纹；不得记录 access token、App Secret、原始请求/响应、完整资源 token 或 `.env` 内容。

只有同时确认目标版本在事件发生前已部署、`start` 事件合同已经生效、目标 service/profile/时区正确且 journal 完整覆盖目标时间窗时，“飞书侧有新目录但无对应事件”才可排除这条已部署路径。任一前提无法确认时只能标记为“证据不足”；即使排除本路径，也不能据此断言具体外部来源。

### 6.2 发送文件时（每次）

```
Linux 用户运行 bi-plus:send-feishu-file ~/reports/xxx.xlsx
  ↓
Unix socket → bi_plus_file_sender_daemon
  ↓
feishu_file_sender.send_feishu_file()
  ├── 身份校验（SO_PEERCRED → linux_username）
  ├── 绑定用户查找（ApprovalRecord + User）
  ├── 文件合法性校验（目录白名单、symlink、敏感文件名、大小上限）
  └── 获取 tenant_access_token
        ↓
        user.drive_folder_token 存在？
        ├── YES → _send_via_drive()
        │     ├── .md 文件：创建飞书文档，写入 Markdown 内容，私聊发送文档链接
        │     ├── .csv / .xls / .xlsx 文件：导入为飞书原生表格，私聊发送表格链接
        │     ├── 其他文件：upload_file_to_drive(drive_folder_token, file_name, file_obj, size=)
        │     │     ├── POST /drive/v1/files/upload_all
        │     │     └── PATCH /permissions/{file_token}/public  security_entity=anyone_can_edit
        │     └── send_text_message(open_id, "文件已保存...：{url}")
        └── NO  → _send_via_message()（原文件消息，兼容旧账号）
```

---

## 七、文件类型映射策略

`infer_drive_action(file_name)` 返回：

| 扩展名 | 动作 | 说明 |
|--------|------|------|
| `.md` | `docx` | 创建飞书云文档，校验/切分文本后按最多 20 个 Block 分批写入并核对逐批回执，再单聊发送文档链接；失败时不上传原 Markdown |
| `.xlsx` / `.xls` / `.csv` | `sheet` | 优先通过 `/drive/v1/import_tasks` 导入为飞书原生表格，并单聊发送表格链接；导入失败时降级上传原文件 |
| 其他 | `upload` | 上传原始文件到云盘 |

> **当前实现：** `feishu_file_sender` 对 `.md` 调 `create_docx_from_markdown_in_folder` 创建飞书文档；文档 URL 由已校验的 `FEISHU_TENANT_DOMAIN + document_id` 构造，不读取 API 不存在的 `document_url` 字段。写入按最多 20 个 Block 分批，每段最多 2,000 字符，限频仅做最多两次有界退避，并要求 API 返回匹配的 children receipt。Docx 安全设置也采用稳定、脱敏的失败合同；任何阶段失败均返回 `docx_failed`，附脱敏 `stage/upstream_code`，不上传原 Markdown，并要求用户先核对飞书避免重复发送。文档链接通知只有严格整数 `code=0` 且带非空 `message_id` receipt 才能宣告 `saved_as_docx`；非零、异常或 receipt 缺失均使用 `notify_document` 阶段进入未知状态，不自动重建或重发，上游日志只记录稳定 code/type。对 `.csv` / `.xls` / `.xlsx` 仍优先调 `import_sheet_from_file_in_folder` 导入为飞书表格，失败时降级上传原文件；其他文件仍调 `upload_file_to_drive`。

---

## 八、权限限制说明

### 子文件夹权限

用户拥有自己子文件夹的 **edit**（可编辑）权限。飞书文件夹不支持安全策略设置（`type=folder` 不被 `PATCH /permissions/public` 接受），因此文件夹层面不设额外限制。

### 文件级安全策略

每个上传/创建的文件，调用 `_set_security(file_token, file_type, token)` 设置：

```json
{ "security_entity": "anyone_can_edit" }
```

含义：**仅拥有编辑权限的用户，才能复制、下载、导出该文件**。用户本人有 edit 权限（继承自文件夹），所以可以下载自己的文件；其他人如拿到链接仅有 view，不能下载。

---

## 九、待办事项

### 可选增强（Phase 2）

- 已有用户（`drive_folder_token` 为空）批量创建文件夹（运维脚本，按需）
- 文件保留策略：云盘文件 30 天后自动清理（定时任务）

---

## 十、验证方法

### 单元测试

```bash
.venv/bin/python -m pytest tests/test_feishu_drive.py -v
```

### 集成验证（需真实飞书环境）

本仓库不提供可直接复制执行的云盘写入脚本，避免只凭注释误在生产根目录创建测试文件。任何真实写入必须在取得 tenant token 前完成以下人工门禁：

1. 运行 profile 已验证为 `bi-plus-test-bot`；
2. Company Ops 已独立确认该 profile 的 `FEISHU_DRIVE_ROOT_FOLDER` 是隔离测试根目录；
3. 本次使用测试用户和测试审批记录，且不会触达生产用户或生产云盘。

三项全部通过后，才可通过测试机器人的正常审批/开户路径触发一次真实事件，并核对目录只创建一次、授权成功后才落库以及 `drive_folder_event` 全链路。任一项无法确认时不得取得 token 或发出写请求，也不得改用生产云盘验证；此时只验证运行版本、日志格式和离线测试，等待真实事件补齐证据。

### 开账流程验证

在 `provision_drive_folder` 返回值中检查 `skipped=False`，并在数据库中确认 `users.drive_folder_token` 已写入：

```bash
sqlite3 data/app.db "SELECT id, name, drive_folder_token FROM users WHERE drive_folder_token IS NOT NULL;"
```
