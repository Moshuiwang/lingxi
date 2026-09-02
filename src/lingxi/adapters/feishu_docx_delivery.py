"""飞书 docx 文档交付适配器（Issue #341 S-ES-1：生产适配器，只做 API 面，不接线）。

来源：#341 S0 探针（2026-08-27，四步全通，[评论](
https://github.com/Moshuiwang/lingxi/issues/341#issuecomment-5385558864)，证据
等级 6，Bot-Test 真实调用）——建文档、写正文段落、对个人 ``open_id`` 授予文档级
``full_access``（未被平台降级）、协作者读回，四个动作全部用应用身份令牌
（``tenant_access_token``）真实调通。交付形态依据决策记录
``docs/决策记录/2026-08-23-正式产物路由为随对话文档与文档级可管理授权.md``：
随对话交付给个人 + 文档级 ``full_access`` + 所有权留机器人。

本模块只做协议细节——"什么时候该建文档、发给谁、失败要不要重试、幂等"都不是这里
的职责，那些属于 S-ES-3 的投递链路（``core.execution``/``apps.gateway`` 的对应
Protocol 实现）；这里只保证一件事：调用形态与飞书契约完全对齐，失败不静默。

## 姿态选择：裸 HTTP，不用 lark-oapi

与 :mod:`lingxi.adapters.feishu_directory` / :mod:`lingxi.adapters.
feishu_tenant_token` 同一习惯：标准库 ``urllib``、零新增依赖、构造函数只存参数、
不建 client、不发请求；传输层可注入，默认实现在函数内部延迟 import（本模块用的
是标准库 ``urllib``，没有第三方 SDK 需要延迟导入，仍保留"构造函数不做 I/O"这条
纪律）。理由与 :mod:`lingxi.adapters.feishu_group_message` 相同：这个能力目前
没有接线的调用方（S-ES-3 未做），不该现在就把 ``lark-oapi`` 拉进任何常驻进程的
运行时依赖。``scripts/probe_drive_folder_permissions.py`` 的 ``LarkDriveTransport``
已经用同一条裸 HTTP 路径验证过 ``/drive/v1/permissions/{token}/members`` 与
``/docx/v1/documents`` 两个端点的形状，这里额外补上 S0 新验证过的写正文端点
（``blocks/{document_id}/children``）。

## 令牌供给：``Callable[[], str]``

构造函数接收的是 ``tenant_access_token: Callable[[], str]``，不是
``app_id``/``app_secret``——形状对齐
:class:`~lingxi.core.permission.tenant_token_supply.TenantAccessTokenSupply`
（同样是"要一份当下能用的令牌"这一个动作）。上层缓存与续期节奏已经是一个独立、
被测试钉住的组件，本模块不重新发明"要不要现在去换一次令牌"的判断，每次调用只管
去要一份。

## 失败语义：不静默

八个会发起真实调用的方法（Issue #408 新增 :meth:`LarkDocxDelivery.
convert_markdown_to_body`/:meth:`LarkDocxDelivery.write_blocks`，Issue #538 新增
:meth:`LarkDocxDelivery.write_descendant_blocks`，见「markdown 官方转换开关」与
「嵌套块写入路径」两节）都不捕获任何未预期异常。飞书业务错误码明确非 0 时抛出
:class:`FeishuDocxDeliveryError`（``definite=True``，判别口径同
:class:`lingxi.adapters.feishu_directory.FeishuDirectoryError`）；响应本身成功
（``code`` 为 0）但缺失可回读标识（``document_id``/``items`` 字段缺失或形状
不对；``read_members``/``read_body_children`` 详见各自方法文档字符串里的真实
响应形状说明）时抛出 ``LookupError``——这种"结果不明"不属于飞书明确拒绝，同
:mod:`lingxi.adapters.feishu_delivery` 模块文档字符串里的既有分类（成功响应缺
标识 → ``LookupError``，业务错误码 → 专用异常）。传输层异常（连接失败、超时、
JSON 解析失败）由默认传输 :func:`urllib_transport` 分类为
``FeishuDocxDeliveryError(definite=False)``。

## 幂等判据新增方法：``read_body_children``（Issue #353）

原四步（建档、写正文、授权、读回协作者）之外新增的第五个真实调用，只服务一件事：
让 ``apps/gateway/document_delivery.py`` 的检查点恢复路径能在**重驱"写正文"步
之前**先问一句"这篇文档是不是已经写过正文了"，而不是无条件重放
:meth:`write_paragraphs`（S-F-3 修复 #353：检查点恢复会把正文再追加一遍）。

选它而不是新增数据库检查点列的理由：飞书写入与本地检查点提交是分布式非原子的
两次独立往返——如果幂等判据只看本地是否已经记录过"写过正文"，"外部写成功但
本地检查点还没来得及推进"这个崩溃窗口永远封不死（这类崩溃发生在两次独立往返
之间的任意时刻，本地状态天然滞后于外部真实状态，无法通过让本地记录"更快"来
消除）。判据必须直接问外部系统本身的真实状态，而不是本地对这个状态的一份缓存。

选它而不是读文档基本信息（``revision_id`` 之类）的理由：:meth:`write_paragraphs`
写入的位置精确是"``document_id`` 自身这个根 block 的 children"（S0 探针实测，
见该方法文档字符串），``read_body_children`` 读的是完全相同的一个位置——两者
读写同一个坐标，不依赖任何第二个信号（例如某个计数器在写入后是否必然变化）来
间接推断，语义上不存在错位空间。

**已知未被本仓库任何 S0/S-ES-1 探针验证过的假设**（如实标注，不静默宣称已验证）：
飞书文档标题（``document.title``）在创建时写入的是文档元数据，不是根 block 的
子块——因此一篇刚建好、从未调用过 :meth:`write_paragraphs` 的文档，其根 block
应当没有任何子块，"子块非空"精确对应"正文已经写过"，不存在"标题占位"与"正文
已写"的混淆空间。这个假设来自飞书开放平台 docx 数据模型的公开文档口径，与本
模块既有的 ``block_type=2`` 文本段落约定一致，但**尚未在本仓库任何真实调用中
实测确认**（本 Story 明确不接触真实飞书端点）。如果这个假设不成立（例如标题
真的作为一个子块存在），后果是检查点恢复会把"刚建档、从未写过正文"误判成"已经
写过"，从而**跳过本该发生的首次写正文**——比本次修复要解决的"重复写"更严重。
因此**在这条判据依赖真实飞书接口投入生产前，应当补一次 L4a 真实探针**：确认
（a）新建文档的根 block children 确实为空，（b）
``GET /docx/v1/documents/{id}/blocks/{id}/children`` 的响应形状确实是
``data.items``（同 ``read_members`` 现有形状口径）。补探针前，这条判据只有
L1（代码 + 假传输层测试）证据，不是 L4a。

## markdown 官方转换开关（Issue #408；Issue #467／rc22 S-4 起代码默认开启）

正文交付此前把模型产出的 markdown 逐字符剥离成纯文本段落写入（``core.execution.
document_delivery.normalize_markdown``），代价是正文里的连字符会被一并吃掉——
「周环比 -12.85%」被剥成「周环比 12.85%」，负号丢失，属于数据正确性缺陷。产品
负责人 2026-08-29 裁定分两步修：立即停止字符剥离（小修，`core` 侧已完成，见该
模块），正式排版走飞书官方转换接口——本节是正式方案在本模块的落点。管线接线
（迁移 0079 持久化原始 markdown、gateway 配置读取
``LINGXI_DOCX_MARKDOWN_CONVERT``）随 Issue #408 批次完成时开关默认关闭，接线
本身不改变现网行为；rc21 stage 探针（Issue #442）验证转换路径可用后，Issue
#467／rc22 S-4 把 ``apps/gateway/config.py`` 的默认值翻转为**代码默认开启**，
未配置该环境变量即等价于开启，显式关闭改用精确值 ``"0"``，翻转前唯一的开启值
``"1"`` 继续解析成开启（既有 stage 配置零迁移成本），完整语义见
``apps/gateway/config.py::_markdown_convert_enabled``。

- :meth:`LarkDocxDelivery.convert_markdown_to_body`：``POST /docx/v1/documents/
  blocks/convert``（``content_type=markdown``），把一段 markdown 转换成飞书官方
  block 结构，并整理成一份 :class:`ConvertedBody`。这个端点只做转换、不写入任何
  文档，失败或重试都不产生外部副作用。
  **Issue #442 受控探针实证**（2026-08-30，Bot-Test 真实调用，见该 issue 正文）
  纠正了本节此前的假设：响应体 ``data.blocks`` **不是文档顺序**（实测「标题→
  两列表项→正文」返回的 ``block_types`` 顺序是 ``[12, 2, 3, 12]``），真实的
  文档顺序由响应体的 ``data.first_level_block_ids``（block_id 字符串数组）
  给出；响应另含 ``data.block_id_to_image_urls`` 键，与 blocks 无关，不得误当
  成块处理。因此本方法在返回前按 ``first_level_block_ids`` 重排 ``blocks``
  （建 ``block_id`` → block 的映射后按顺序取出），并做两条防御性失败关闭
  （**绝不静默丢块或乱序交付**，两条都是 ``FeishuDocxDeliveryError
  (definite=True)``——转换端点无副作用、同一份 markdown 重放结果确定性相同，
  同 :meth:`write_blocks` 的 ``too_many_blocks`` 走同一类"发起写入前的确定性
  拒绝"）：

  1. ``first_level_block_ids`` 缺失、不是列表或为空 → ``markdown_convert_
     missing_first_level_block_ids``；
  2. ``first_level_block_ids`` 自身出现重复 block_id →
     ``markdown_convert_duplicate_first_level_block_ids``（复现：同一段正文
     在文档里被重复交付两次）；某个块的 ``block_id`` 缺失或不是非空字符串
     → :data:`UNSUPPORTED_NESTED_BLOCKS`（无法确认它在树里的位置）；
     ``blocks`` 数组里出现重复 block_id → ``markdown_convert_block_count_
     mismatch``（复现：建映射的字典推导式静默用后一个覆盖前一个，前一个块
     的内容凭空消失）。前两条对账各自独立、互不替代——两种成因在计数上恰好
     互相抵消时（``blocks`` 与 ``first_level_block_ids`` 对同一个 block_id
     都重复了同样的次数），只留其中一条会漏判。
  3. 然后按 ``children`` 把父子关系还原成一棵树（从 ``first_level_block_ids``
     出发、显式栈深度优先展开，见「嵌套块写入路径」一节）。展开中引用了一个
     在 ``blocks`` 数组里找不到的 ``block_id``（响应内部不自洽，理论上不应
     发生），归类为「结果不明」``LookupError``，同 :meth:`read_body_children`
     既有的"响应形状不对但不是飞书明确拒绝"分类口径——这与上面几条"明确知道
     拒绝原因"的 ``definite`` 分支不同：这里连"为什么不一致"都无法确定。同一
     个块被两个父块认领（或 ``children`` 成环）→
     ``markdown_convert_shared_child_block``。
  4. 展开完成后仍有块没被任何父块认领（既不是一级块、也不在任何可达块的
     ``children`` 里）→ :data:`UNSUPPORTED_NESTED_BLOCKS`。**这个码是全模块
     唯一会被 :meth:`LarkDocxDelivery.write_body` 捕获并转成明示降级的原因
     码**（Issue #499），其余原因码一律照旧向上抛出。

  返回的 :class:`ConvertedBody` 里，``descendants`` 每个块都剔除了
  ``descendant`` 端点的只读字段（``parent_id``、``table.cells``、
  ``table.property.merge_info``，见 :func:`_strip_readonly_descendant_fields`
  与各常量的实测记录）、但**保留** ``block_id`` 与 ``children``——这两个字段
  在嵌套路径上是父子关系的唯一载体。走扁平 ``children`` 端点时再由
  :meth:`ConvertedBody.flat_children` 额外剔除这两个字段（
  :func:`_strip_readonly_block_fields`），结果与 Issue #538 之前逐字一致。
- :meth:`LarkDocxDelivery.write_blocks`：把已经是飞书 block 形状的数组沿用与
  :meth:`write_paragraphs` 完全相同的 children 插入端点写入（同一坐标、同一
  ``index=0`` 单次写入语义）。只服务"这份正文是一份纯一级块序列"的情况；含
  嵌套块的正文走 :meth:`LarkDocxDelivery.write_descendant_blocks`。
  **超过 :data:`MAX_CONVERTED_BLOCKS`（1000）一律整体拒绝，不做分批插入**——分批会打破
  :meth:`read_body_children` 判据依赖的"正文一次写入"假设（见上文「幂等判据
  新增方法」一节）：分批写入的中途状态（例如已经插入前 1000 个 block、还剩
  部分未插入）会被下一次检查点恢复误判成"已经写完"，从而跳过本该继续的写入，
  比本次要解决的问题更严重；因此选择在超限时直接失败关闭，用 ``definite`` 原因
  码 ``too_many_blocks`` 让调用方明确知道这是一次确定性拒绝，不需要重试。这个
  取舍的代价是超长 markdown 无法交付带格式的文档——本模块认为"明确拒绝"优于
  "悄悄改变幂等语义"，是否需要为超限场景另设计分批状态机留给未来 Story。
- :meth:`LarkDocxDelivery.write_body`：写正文的唯一装配入口，把「开关」变成
  实际可执行的分支——``markdown_convert_enabled=False``（构造函数自身的参数
  默认值；真正生效的值由装配层 ``apps/gateway/config.py`` 显式传入，见下一条）
  时逐字调用 :meth:`write_paragraphs`；为 ``True`` 时改走
  :meth:`convert_markdown_to_body` + :meth:`write_blocks`（纯一级块）或
  :meth:`write_descendant_blocks`（含嵌套块）。返回
  :class:`WriteBodyOutcome`（Issue #499）——调用方据此知道这次是不是降级写的。
  开关打开时的失败**只有一个例外会被捕获**：
  :data:`UNSUPPORTED_NESTED_BLOCKS`（含表格等嵌套结构）时改走纯文本段落路径
  并在返回值里明示降级；其余一切（业务错误码、结果不明、超过 block 数上限、
  其它 ``markdown_convert_*`` 对账码）仍然直接向上抛出。**降级必须明示，不得
  静默**——静默降级会制造"用户以为拿到了带格式的文档，实际收到的是转换失败前
  的另一种内容"这种更难排查的假象；产品负责人 2026-08-31 裁定用"降级 + 如实
  告知"取代"整次失败"，取代的是失败结论，不是"必须让用户知道"这条纪律，完整
  理由与安全前提见 :meth:`LarkDocxDelivery.write_body` 文档字符串。
- **开关本身不是环境变量**：`adapters/` 不直接读 ``os.environ``（[代码框架
  「三、横切约定」](../../../../docs/技术设计/代码框架.md)的硬性约束），
  ``markdown_convert_enabled`` 是构造函数参数——真正的环境变量
  ``LINGXI_DOCX_MARKDOWN_CONVERT`` 由装配层 ``apps/gateway/config.py`` 读取后
  作为普通布尔值传进来（Issue #467／rc22 S-4 起代码默认开启：未配置或精确值
  ``"1"``＝开启，精确值 ``"0"``＝显式关闭，其余值启动即失败，与
  ``apps/worker/config.py`` 既有开关同一「错配失败关闭」姿态）。**接线已完成**（迁移 0079、
  Issue #408 正式方案接线批次）：``task_document_delivery_request`` 新增可空
  ``markdown`` 列持久化原始 markdown 全文，``apps/gateway/document_delivery.py``
  的 :meth:`DocumentDeliveryConsumer._process_docx_claim` 按这一列是否非
  ``None`` 决定要不要调用 :meth:`write_body`（非 ``None`` 才调用；``None`` 无
  条件回退 :meth:`write_paragraphs`，与开关是否打开无关）——完整接线细节见该
  模块文档「markdown 官方转换路径的接线」一节。

## 嵌套块写入路径（Issue #538；2026-09-03 stage 受控探针实证）

**这一节修的是一个生产缺陷，不是新增能力**：Issue #538 之前，只要回答里出现
**一张** markdown 表格，官方转换返回的单元格与单元格内文字就是嵌套块，
``convert_markdown_to_body`` 的「非一级块即拒」守卫就把**整篇**转换结果作废、
退回纯文本段落——标题、列表、表格一起丢。生产 2026-09-02 首日实测 3 篇 docx
**3/3 全部命中**（经营数据类问题的答案天然是表格）。修法是改用飞书为 convert
配套的嵌套块端点，把守卫收窄成「真的无处安放才拒」。

- 端点：``POST /docx/v1/documents/{document_id}/blocks/{block_id}/descendant``。
  请求体 ``{"children_id": [...一级块临时 id...], "index": 0, "descendants":
  [...全部块...]}``。``block_id`` 取 ``document_id`` 本身（根 block），**与
  ``children`` 插入端点是同一个坐标**——这是幂等判据继续成立的前提。
- **convert 的输出与 descendant 的输入本就是一对**：``data.blocks`` 里每个块
  携带的 ``block_id`` 是**临时** id，``data.first_level_block_ids`` 就是
  ``children_id``，块自己的 ``children`` 就是父子关系。响应的
  ``data.block_id_relations`` 把临时 id 映射成真实 block_id。

**受控探针实测（Bot-Test，stage，受控测试文档用完即删并回读确认）**：

1. **只读字段**：必须剥掉的只有 ``table.property.merge_info`` ——带着它调用，
   飞书以 ``1770001 invalid param`` **整体拒绝**（变体 V5 复现）；剥掉后同一
   份载荷的四个变体（留/不留 ``parent_id``、留/不留 ``table.cells``）全部
   ``code=0``。本模块另外把 ``parent_id``（convert 输出里恒为空串）与
   ``table.cells``（与 ``children`` 完全重复的服务端计算值）一并剥掉，取
   "描述位置/关系的字段一律不回插"这条既有纪律的保守侧。**``block_id`` 与
   ``children`` 绝不能剥**——扁平路径上它们是只读字段，嵌套路径上它们是唯一
   的结构信息。
2. **读回结构**：把一份「标题＋二级标题＋正文＋两条列表＋3×3 表格＋结尾段」
   的 convert 输出一次写入后，全量读回 26 个块（1 个根块 + convert 的 25 个），
   表格是货真价实的 ``block_type=31``、``row_size=3``/``column_size=3``、9 个
   单元格，单元格文字逐字保真（含负号「-12.85%」）；标题与列表原样在位。
3. **幂等判据不变**：同一篇文档写入前 ``read_body_children`` 返回 0 个子块、
   写入后返回 **7** 个——恰好是那 7 个一级块（``block_type`` 依次
   ``[3, 4, 2, 12, 12, 31, 2]``），**嵌套块不出现在根 block 的 children 里**。
   因此 Issue #353 的"子块非空 = 正文已写过"判据在嵌套写入下逐字继续成立，
   不需要重建。
4. **单次块数**：200 / 500 / 1000 个块单次 ``descendant`` 写入全部 ``code=0``，
   与 :data:`MAX_CONVERTED_BLOCKS` 一致；超限仍然失败关闭，不分批。

**保留降级路径、只收窄触发条件**：:data:`UNSUPPORTED_NESTED_BLOCKS` 不再是
"出现了嵌套块"，而是"展开完成后仍有块没有任何父块认领"——响应里真的存在一个
无处安放的块时，本模块仍然按 Issue #499 的裁定降级交付纯文本段落并**明示**。

**如实标注的边界**：探针只覆盖 markdown 表格这一种嵌套形态；引用块、嵌套列表、
代码块等其它形态是否也能被 ``descendant`` 原样写入，**未做逐形态探针**，不得
声称"所有 markdown 结构都已支持"。

## 凭据与内容边界

日志与异常消息不落 ``tenant_access_token``、请求/响应正文（文档标题、段落文字、
``open_id``）。业务错误码只以货真价实的 ``int`` 形式拼进 ``code`` 字段（同
``feishu_directory._safe_feishu_code`` 的注入防护理由：响应体是不可信的外部
数据），不透传飞书 ``msg`` 原文。

## 文档 URL 的构造

飞书 ``docx/v1/documents`` 建文档响应未见 ``url`` 字段（S0 探针实测响应只有
``document.document_id``/``document.revision_id``/``document.title``），真实
链接形如 ``https://<tenant>.feishu.cn/docx/{document_id}``——``<tenant>`` 是与
API host（``open.feishu.cn``）无关的租户子域，无法从 ``base_url`` 推出。因此
:meth:`LarkDocxDelivery.document_url` 在构造时单独接收 ``tenant_domain``（S0
探针实测的值是 ``gv3qfk4q2rp.feishu.cn``；生产租户域名留给 S-ES-3 接线时从配置
注入，本模块不猜测、不写死）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

#: 出站超时。与其它裸 HTTP 飞书适配器同量级（``feishu_tenant_token``/
#: ``feishu_directory``）：一次调用挂死不该无界占住调用方。
REQUEST_TIMEOUT_SECONDS = 20

#: 权限接口的 ``type`` 查询参数：docx 文档（区别于 folder/sheet 等其它对象类型）。
DOCX_PERMISSION_TYPE = "docx"

#: 决策记录 2026-08-23 裁定的授予档位：文档级「可管理」。
FULL_ACCESS_PERM = "full_access"

#: 授权的成员标识类型：飞书用户 ``open_id``（区别于 ``email``/``unionid`` 等）。
OPENID_MEMBER_TYPE = "openid"

#: 飞书用户 ``open_id`` 前缀，用于入口形状校验（同
#: :data:`lingxi.adapters.feishu_user_message.USER_OPEN_ID_PREFIX` 的理由：
#: 把群/租户标识误传成用户 open_id，要在**发出去之前**失败）。
USER_OPEN_ID_PREFIX = "ou_"

#: docx 正文段落 block 的 ``block_type``：S0 探针实测的纯文本段落类型。
_TEXT_PARAGRAPH_BLOCK_TYPE = 2

_DOCX_DOCUMENTS_PATH = "/docx/v1/documents"

#: markdown 官方转换端点（Issue #408，见模块文档「markdown 官方转换开关」）。
_BLOCKS_CONVERT_PATH = "/docx/v1/documents/blocks/convert"
_MARKDOWN_CONTENT_TYPE = "markdown"

#: markdown 官方转换配套的**嵌套块**创建端点后缀（Issue #538）。完整路径是
#: ``/docx/v1/documents/{doc}/blocks/{doc}/descendant``——与 ``children`` 插入
#: 端点写的是同一个坐标（根 block 的 children），区别只在于它额外接受"块与块
#: 之间的父子关系"，因此能一次写下表格这类嵌套结构。
_BLOCKS_DESCENDANT_PATH_SUFFIX = "descendant"

#: **扁平**（一级块）插入端点的请求体不接受的只读字段（Issue #442）。
#: ``block_id``/``children`` 在那条路径上没有任何用处（它本来就只写一层），
#: 随请求带回会被拒绝——见 :meth:`LarkDocxDelivery.write_blocks` 与模块文档
#: 「markdown 官方转换开关」一节。
_CONVERT_RESPONSE_READONLY_BLOCK_KEYS = ("block_id", "parent_id", "children")

#: **嵌套块**（``descendant``）端点的只读字段（Issue #538，2026-09-03 stage
#: 受控探针实测，见模块文档「嵌套块写入路径」一节）。**与扁平路径的关键差别**：
#: ``block_id`` 与 ``children`` 在这条路径上**不是**只读字段，恰恰是父子关系的
#: 唯一载体（``children_id`` 与每个块的 ``children`` 引用的都是 convert 返回的
#: 临时 block_id），**绝不能剥**；``parent_id`` 在 convert 输出里恒为空串、
#: 对 ``descendant`` 没有意义，剥掉（探针变体 V3/V4 实测 ``code=0``）。
_DESCENDANT_READONLY_BLOCK_KEYS = ("parent_id",)

#: 表格块 ``table`` 字段里由服务端计算、不随请求回插的只读键（Issue #538
#: 实测）：``cells`` 是单元格 block_id 清单，与该块的 ``children`` 完全重复。
_DESCENDANT_READONLY_TABLE_KEYS = ("cells",)

#: 表格块 ``table.property`` 里必须剥掉的只读键（Issue #538 实测，**这一条是
#: 唯一硬性的**）：带着 ``merge_info`` 调 ``descendant`` 端点，飞书直接以
#: ``1770001 invalid param`` **整体拒绝**（受控探针变体 V5 复现；同一份载荷
#: 只剥掉它之后，变体 V1–V4 全部 ``code=0``）。markdown 表格不含合并单元格
#: （convert 返回的 ``merge_info`` 逐项都是 ``row_span=col_span=1``），剥掉它
#: 不损失任何来自 markdown 的信息。
_DESCENDANT_READONLY_TABLE_PROPERTY_KEYS = ("merge_info",)

#: 单次写入端点的 block 数上限。超过时 :meth:`LarkDocxDelivery.write_blocks` /
#: :meth:`LarkDocxDelivery.write_descendant_blocks` 整体拒绝、不分批插入——
#: 理由见模块文档「markdown 官方转换开关」一节：分批会打破
#: :meth:`LarkDocxDelivery.read_body_children` 判据依赖的"正文一次写入"假设。
#: ``children`` 端点这个值取自官方文档口径（未做真实调用探针）；``descendant``
#: 端点 Issue #538 受控探针实测 200/500/1000 个块单次写入全部 ``code=0``，
#: 因此同一个上限对两条路径都成立。
MAX_CONVERTED_BLOCKS = 1000

#: 官方转换结果里出现**无法定位**的块（响应里存在某个块，既不在
#: ``first_level_block_ids`` 里、也没有被任何一个可达块的 ``children`` 认领，
#: 因此没有任何位置可以把它写进文档）时的原因码。**独立成常量而不是散落的
#: 字面量**：它同时是抛出点（:meth:`LarkDocxDelivery.convert_markdown_to_body`）
#: 与唯一捕获点（:meth:`LarkDocxDelivery.write_body` 的明示降级分支，
#: Issue #499）的判据，两处必须逐字一致——写成两个字面量时，任何一侧改名都会
#: 让降级分支悄悄失效、退回"整次交付失败"，而没有任何东西会红。
#:
#: **Issue #538 收窄了它的含义**：此前它是"``blocks`` 里出现了任何一个非一级
#: 块"，于是**只要回答里有一张表格就整篇作废**（生产实测 3/3 命中）；现在
#: 嵌套结构本身由 ``descendant`` 端点正常写入，这个码只在"这个块连父块都找
#: 不到、真的无处安放"时才触发。取值刻意不改名——迁移 ``0082`` 已经把它写进
#: 生产数据、管理台词表也认它，改名会让历史行变成未登记码。
UNSUPPORTED_NESTED_BLOCKS = "unsupported_nested_blocks"


@dataclass(frozen=True)
class ConvertedBody:
    """:meth:`LarkDocxDelivery.convert_markdown_to_body` 的返回值：一份已经
    对账过、可以直接交给写入端点的正文（Issue #538）。

    ``children_id``：一级块的**临时** block_id，按文档真实顺序（来自响应的
    ``first_level_block_ids``）。
    ``descendants``：这次要写的**全部**块（含表格单元格这类嵌套块），已剥掉
    :data:`_DESCENDANT_READONLY_BLOCK_KEYS` 等只读字段、但**保留** ``block_id``
    与 ``children``——它们是父子关系的唯一载体。顺序是"按 ``children_id`` 深度
    优先展开"，与 ``descendant`` 端点无关（该端点按 id 引用而不是按数组下标），
    只为让请求体可读、可 diff。

    :attr:`nested` 为 ``False`` 时这份正文是一份纯一级块序列，
    :meth:`flat_children` 给出与 Issue #442 之前**逐字相同**的 ``children``
    端点请求体——含表格的修复不改变不含表格那条路径的任何一个字节。
    """

    children_id: tuple[str, ...]
    descendants: tuple[Mapping[str, Any], ...]

    @property
    def nested(self) -> bool:
        """这份正文里是否存在嵌套块（一级块之外还有别的块）。"""

        return len(self.descendants) != len(self.children_id)

    def flat_children(self) -> list[dict[str, Any]]:
        """扁平路径（``children`` 插入端点）的请求体块序列。

        只在 :attr:`nested` 为 ``False`` 时有意义——此时 ``descendants`` 恰好
        就是一级块本身，按 ``children_id`` 取出后再剥掉 ``block_id``/``children``
        （那条端点不接受这两个字段），结果与 Issue #538 之前的
        ``convert_markdown_to_blocks``（Issue #538 前身）返回值逐字一致。
        """

        if self.nested:
            # 失败关闭而不是"顺手只返回一级块"：那会把表格单元格一类嵌套块
            # 静默丢掉，正是 Issue #538 要修的那种"内容凭空消失"。
            raise ValueError("含嵌套块的正文不能走扁平 children 端点，应调用 write_descendant_blocks")
        by_block_id = {block["block_id"]: block for block in self.descendants}
        return [_strip_readonly_block_fields(by_block_id[block_id]) for block_id in self.children_id]


@dataclass(frozen=True)
class WriteBodyOutcome:
    """:meth:`LarkDocxDelivery.write_body` 的返回值：这次正文到底是按哪条路径
    写进去的（Issue #499 明示降级）。

    ``degraded_reason``：``None`` = 按调用方要求的路径原样写入（转换开关关时
    的段落路径，或开关开时的官方转换路径），没有任何降级；非 ``None`` = 官方
    转换被飞书**确定性拒绝**（当前唯一取值 :data:`UNSUPPORTED_NESTED_BLOCKS`），
    正文已经改用纯文本段落路径写入，**用户拿到的排版与他本该拿到的不同**。

    **为什么必须有返回值、而不是让适配器自己把降级咽下去**：本模块此前的姿态
    是"转换失败一律向上抛，绝不静默退回段落路径"，理由是静默降级会制造"用户
    以为拿到了带格式的文档、实际收到另一种内容"的假象。产品负责人 2026-08-31
    就 Issue #499 裁定改为**降级交付**（含表格的回答此前 18.2% 整次交付失败，
    实测见该 issue W0-1 评论），但这条裁定的成立条件是**把"静默降级"换成
    "明示降级"**——调用方必须知道这次降级了，才能如实告知用户格式已简化。
    这个返回值就是那条跨模块信号；调用方（``apps/gateway/document_delivery.py``）
    丢掉它，等于把裁定退化成当初被明令禁止的静默降级。
    """

    degraded_reason: str | None = None

    @property
    def degraded(self) -> bool:
        return self.degraded_reason is not None


class FeishuDocxDeliveryError(RuntimeError):
    """飞书 docx 交付失败。``code`` 供程序判断，消息里不含凭据、正文或标识符。

    ``definite``：``True`` 表示飞书明确拒绝（收到业务错误码），``False`` 表示
    结果不明（传输层异常、超时、响应形状不对）。判别口径同
    :class:`lingxi.adapters.feishu_directory.FeishuDirectoryError`。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"飞书 docx 交付失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


def _require_https(base_url: str) -> str:
    """飞书出站必须 HTTPS：误配 ``http://`` 会把 Bearer token 明文上路。"""

    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url 必须由配置注入，不得写死在代码里")
    text = base_url.strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError("飞书 base_url 必须使用不含凭据的 HTTPS 地址")
    if parsed.fragment:
        raise ValueError("飞书 base_url 不得包含 URL fragment")
    return text.rstrip("/")


def _require_tenant_domain(value: str) -> str:
    """校验用于拼文档链接的裸域名（不是 API base_url，见模块文档「文档 URL 的
    构造」一节）：不含协议、路径或空白，避免把一段可注入的值悄悄拼进对外链接。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("tenant_domain 必须由配置注入，不得写死在代码里")
    text = value.strip()
    if "://" in text or "/" in text or any(character.isspace() for character in text):
        raise ValueError("tenant_domain 必须是裸域名（不含协议、路径或空白），例如 example.feishu.cn")
    return text


def _require_document_id(document_id: str) -> str:
    text = (document_id or "").strip()
    if not text:
        raise ValueError("document_id 不能为空")
    if any(character.isspace() for character in text):
        raise ValueError("document_id 不得包含空白字符，不回显收到的值")
    return text


def _require_user_open_id(open_id: str) -> str:
    """校验用户 ``open_id`` 形状；不合法就快速失败，且不回显取到的值——理由同
    :func:`lingxi.adapters.feishu_user_message.validate_user_open_id`（把群/
    租户标识误传成用户 open_id，要在**发出去之前**失败，而不是把「可管理」权限
    授予一个错误的收件人）。"""

    text = (open_id or "").strip()
    if not text.startswith(USER_OPEN_ID_PREFIX) or len(text) <= len(USER_OPEN_ID_PREFIX):
        raise ValueError(f"open_id 必须是飞书用户 open_id（以 {USER_OPEN_ID_PREFIX} 开头），不回显收到的值")
    if any(character.isspace() for character in text):
        raise ValueError("open_id 不得包含空白字符，不回显收到的值")
    return text


def _safe_feishu_code(value: object) -> str:
    """把飞书业务错误码渲染成审计安全的分类标签。理由与
    ``feishu_directory._safe_feishu_code`` 相同：响应体是不可信的外部数据，只在
    ``value`` 是货真价实的 ``int``（排除 ``bool``，它是 ``int`` 子类）时插值，
    否则退化成固定标签，防止响应内容注入进异常消息/审计行。
    """

    if isinstance(value, int) and not isinstance(value, bool):
        return f"feishu_code_{value}"
    return "feishu_code_invalid"


def _strip_readonly_block_fields(block: Mapping[str, Any]) -> dict[str, Any]:
    """剔除 :data:`_CONVERT_RESPONSE_READONLY_BLOCK_KEYS` 里列出的只读字段，
    返回可以原样传给**扁平** ``children`` 插入端点的浅拷贝（不修改入参）。"""

    return {key: value for key, value in block.items() if key not in _CONVERT_RESPONSE_READONLY_BLOCK_KEYS}


def _strip_readonly_descendant_fields(block: Mapping[str, Any]) -> dict[str, Any]:
    """剔除 ``descendant`` 端点不接受的只读字段，返回可以原样放进
    ``descendants`` 数组的深拷贝（不修改入参；``table`` 子结构也要改，浅拷贝
    会写穿到调用方手里的响应对象）。

    **保留** ``block_id`` 与 ``children``：在这条路径上它们不是只读字段，而是
    父子关系的唯一载体（见 :data:`_DESCENDANT_READONLY_BLOCK_KEYS`）。
    """

    stripped = {key: value for key, value in block.items() if key not in _DESCENDANT_READONLY_BLOCK_KEYS}
    table = stripped.get("table")
    if isinstance(table, Mapping):
        table_copy = {key: value for key, value in table.items() if key not in _DESCENDANT_READONLY_TABLE_KEYS}
        table_property = table_copy.get("property")
        if isinstance(table_property, Mapping):
            table_copy["property"] = {
                key: value
                for key, value in table_property.items()
                if key not in _DESCENDANT_READONLY_TABLE_PROPERTY_KEYS
            }
        stripped["table"] = table_copy
    return stripped


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, *, body: Mapping[str, Any] | None = ..., token: str | None = ...
    ) -> Any: ...


def urllib_transport(method: str, url: str, *, body: Mapping[str, Any] | None = None, token: str | None = None) -> Any:
    """默认传输层：只发 HTTPS，不重试有副作用的请求（同
    :func:`lingxi.adapters.feishu_tenant_token.urllib_transport` 的姿态：飞书
    调用失败按已知分类抛出，交由调用方决定要不要重试）。
    """

    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - 地址来自受控配置且已校验 https
            return json.loads(response.read())
    except HTTPError as error:
        try:
            return json.loads(error.read())
        except Exception as parse_error:
            raise FeishuDocxDeliveryError(f"http_{error.code}", definite=False) from parse_error
    except (URLError, OSError, TimeoutError) as error:
        raise FeishuDocxDeliveryError("transport_error", definite=False) from error
    except ValueError as error:
        raise FeishuDocxDeliveryError("invalid_json", definite=False) from error


class LarkDocxDelivery:
    """飞书 docx 文档交付：建文档、写正文、授予「可管理」、协作者读回。

    构造函数**只存参数**：不发请求、不缓存令牌（纪律同
    :class:`lingxi.adapters.feishu_group_message.FeishuGroupMessages`）。传输层
    由 ``transport`` 注入，默认是本模块的 :func:`urllib_transport`。
    """

    def __init__(
        self,
        *,
        base_url: str,
        tenant_access_token: Callable[[], str],
        tenant_domain: str,
        transport: Callable[..., Any] | None = None,
        markdown_convert_enabled: bool = False,
    ) -> None:
        self._base_url = _require_https(base_url)
        if not callable(tenant_access_token):
            raise ValueError("tenant_access_token 必须是返回令牌字符串的可调用对象")
        self._tenant_access_token = tenant_access_token
        self._tenant_domain = _require_tenant_domain(tenant_domain)
        self._transport: Callable[..., Any] = transport or urllib_transport
        # Issue #408「markdown 官方转换开关」：默认 False（零行为变化）；不是
        # 本类自己读环境变量（adapters/ 不直接读 os.environ），由装配层把解析
        # 好的布尔值传进来——见 :meth:`write_body` 与模块文档对应小节。
        self._markdown_convert_enabled = bool(markdown_convert_enabled)

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(dict(params))}"
        token = self._tenant_access_token()
        if not isinstance(token, str) or not token:
            # 结果不明：令牌供给没有按约定返回非空字符串。不是飞书拒绝，是调用方
            # 传入的供给本身坏了——同样不能静默，必须让调用方看见。
            raise FeishuDocxDeliveryError("tenant_access_token_missing", definite=False)
        return self._transport(method, url, body=body, token=token)

    def _data(self, response: Any) -> Mapping[str, Any]:
        """飞书业务错误码非 0 → 抛出 :class:`FeishuDocxDeliveryError`
        （``definite=True``）；这是本模块唯一判定"飞书明确拒绝"的位置，**刻意
        不做静默降级**——一旦这里被改成"记日志后继续"，所有写操作都会在飞书拒绝
        的情况下被上层误判为成功。"""

        if not isinstance(response, Mapping):
            raise FeishuDocxDeliveryError("invalid_response_shape", definite=False)
        code = response.get("code")
        if code not in (None, 0, "0"):
            raise FeishuDocxDeliveryError(_safe_feishu_code(code), definite=True)
        data = response.get("data")
        return data if isinstance(data, Mapping) else {}

    def create_document(self, title: str) -> str:
        """建一篇新文档，返回 ``document_id``。

        ``POST /docx/v1/documents``，S0 探针实测的请求体只有 ``title`` 一个字段
        （不传 ``folder_token`` 时飞书把文档建在应用的默认位置）。
        """

        text = (title or "").strip()
        if not text:
            raise ValueError("文档标题不能为空")
        data = self._data(self._call("POST", _DOCX_DOCUMENTS_PATH, body={"title": text}))
        document = data.get("document")
        if not isinstance(document, Mapping):
            raise LookupError("建文档响应缺少 document 字段：结果不明，不能确定文档是否已建好")
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise LookupError("建文档响应缺少可回读标识 document_id：结果不明")
        logger.info("飞书 docx 文档已建 document_id_len=%s", len(document_id))
        return document_id

    def write_paragraphs(self, document_id: str, paragraphs: Sequence[str]) -> None:
        """把 ``paragraphs`` 逐段写成正文，一次调用消费掉一次外部写请求预算。

        ``POST /docx/v1/documents/{document_id}/blocks/{document_id}/children``：
        S0 探针实测根 block 的 ``block_id`` 就是 ``document_id`` 本身，多段正文
        对应 ``children`` 数组里的多个 ``block_type=2``（文本段落）block，一次
        请求的 ``index`` 固定为 0（本模块只服务"整篇正文一次写完"这个场景，不
        提供中途插入）。
        """

        doc_id = _require_document_id(document_id)
        texts = list(paragraphs) if paragraphs is not None else []
        if not texts:
            raise ValueError("正文段落不能为空")
        for index, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"第 {index + 1} 段正文不能为空")
        children = [
            {"block_type": _TEXT_PARAGRAPH_BLOCK_TYPE, "text": {"elements": [{"text_run": {"content": text}}]}}
            for text in texts
        ]
        self._data(
            self._call(
                "POST",
                f"{_DOCX_DOCUMENTS_PATH}/{doc_id}/blocks/{doc_id}/children",
                body={"children": children, "index": 0},
            )
        )
        logger.info("飞书 docx 正文已写入 document_id_len=%s 段落数=%s", len(doc_id), len(texts))

    def convert_markdown_to_body(self, markdown: str) -> ConvertedBody:
        """把一段 markdown 转换成飞书官方 block 结构，并整理成一份可以直接写入
        的 :class:`ConvertedBody`（Issue #408 正式方案，Issue #467／rc22 S-4 起
        代码默认开启，Issue #538 起支持嵌套结构——见 :meth:`write_body` 与模块
        文档「markdown 官方转换开关」「嵌套块写入路径」两节）。

        ``POST /docx/v1/documents/blocks/convert``，请求体 ``{"content_type":
        "markdown", "content": markdown}``。这个端点只做转换、不写入任何文档，
        失败或重试都不产生外部副作用。**响应体 ``data.blocks`` 不是文档顺序**
        （Issue #442 受控探针实证）——一级块的真实顺序由
        ``data.first_level_block_ids`` 给出；``data.block_id_to_image_urls`` 是
        响应里的另一个键，与 blocks 无关，本方法不读取它。

        **Issue #538 的改动**：此前本方法要求 ``blocks`` 里**每一个**块都出现在
        ``first_level_block_ids`` 里，否则整体拒绝——于是只要回答里有一张表格
        （单元格与单元格内的文字都是嵌套块），整篇转换结果就作废、退回纯文本
        段落，标题与列表跟着一起丢（生产 2026-09-02 实测 3/3 命中）。现在改成
        **按 ``children`` 把父子关系还原成一棵树**：从 ``first_level_block_ids``
        出发深度优先展开，能被展开到的块都原样保留、交给 ``descendant`` 端点
        一次写下。:data:`UNSUPPORTED_NESTED_BLOCKS` 因此收窄成"这个块没有任何
        父块认领、真的无处安放"这一种情况。

        防御性失败关闭（**绝不静默丢块、乱序或重复交付**）：

        1. ``first_level_block_ids`` 缺失/不是列表/为空 →
           ``markdown_convert_missing_first_level_block_ids``（definite）；
        2. ``first_level_block_ids`` 自身出现重复 block_id →
           ``markdown_convert_duplicate_first_level_block_ids``（definite）；
           复现：同一段正文在文档里被重复交付两次；
        3. 某个块缺少非空字符串 ``block_id`` → 无法确认它在树里的位置，
           :data:`UNSUPPORTED_NESTED_BLOCKS`（definite）；
        4. ``blocks`` 里出现重复 block_id → ``markdown_convert_block_count_
           mismatch``（definite）；复现：建映射的字典推导式静默用后一个覆盖
           前一个，前一个块的内容凭空消失；
        5. ``first_level_block_ids`` 或某个块的 ``children`` 引用了一个在
           ``blocks`` 数组里找不到的 block_id → 响应内部不自洽，连"为什么不
           一致"都无法确定，归类为结果不明 ``LookupError``（同
           :meth:`read_body_children` 既有分类口径）；
        6. 同一个块被两个父块认领（或 ``children`` 成环）→
           ``markdown_convert_shared_child_block``（definite）；复现：同一段
           内容在文档两处各出现一次，或展开过程无限递归；
        7. 展开完成后仍有块没被任何父块认领 →
           :data:`UNSUPPORTED_NESTED_BLOCKS`（definite），交给
           :meth:`write_body` 转成明示降级。
        """

        text = (markdown or "").strip()
        if not text:
            raise ValueError("markdown 正文不能为空")
        data = self._data(
            self._call(
                "POST",
                _BLOCKS_CONVERT_PATH,
                body={"content_type": _MARKDOWN_CONTENT_TYPE, "content": markdown},
            )
        )
        blocks = data.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise LookupError("markdown 转换响应缺少可用的 blocks 字段：结果不明")
        mapping_blocks = [block for block in blocks if isinstance(block, Mapping)]
        if not mapping_blocks:
            # P2 顺手（独立审查）：转换请求本身已经成功拿到响应（走到这里说明
            # `data.get("blocks")` 是非空列表，上面那条 `LookupError` 分支没有
            # 触发），只是列表里每一项都不是期望的 block 形状（例如飞书返回了
            # 一串字符串/数字，或本仓库对响应形状的假设本身有误）——这与"入参
            # 校验，还没发出任何请求"的 :class:`ValueError` 是两类不同的问题：
            # 这里已经真实调用了转换接口、已经拿到一个响应，只是内容形状不对。
            # 这个失败是确定性的（转换端点不写入任何文档、没有外部副作用，同一
            # 份 markdown 重放会得到同样的转换结果），因此标 ``definite=True``。
            raise FeishuDocxDeliveryError("markdown_convert_blocks_not_mapping", definite=True)

        # Issue #442：`blocks` 数组不是文档顺序，一级块的真实顺序在
        # `first_level_block_ids` 里。缺失/为空一律 definite 拒绝——没有这份
        # 顺序清单就无法保证交付顺序正确，宁可拒绝也不猜测顺序。
        first_level_block_ids = data.get("first_level_block_ids")
        if not isinstance(first_level_block_ids, list) or not first_level_block_ids:
            raise FeishuDocxDeliveryError(
                "markdown_convert_missing_first_level_block_ids", definite=True
            )
        # 条目必须先是非空字符串，判重才做得下去：`set()` 碰上不可哈希的条目
        # （响应里塞了 list/dict）会抛一个**裸 `TypeError`**，那不属于本模块
        # 任何一条失败分类，调用方的白名单接不住。非法条目按"响应内部不自洽"
        # 归成结果不明，与展开阶段引用不存在的块同一口径。
        for block_id in first_level_block_ids:
            if not isinstance(block_id, str) or not block_id:
                raise LookupError("markdown 转换响应 first_level_block_ids 含非法条目：结果不明")
        # rc21 修复包 B（opus 审查发现）：`first_level_block_ids` 自身重复会让
        # 同一段正文在文档里出现两次。这条对账必须排在"块数对账"之前——两种
        # 成因在计数上恰好互相抵消时（`blocks` 与 `first_level_block_ids` 对
        # 同一个 block_id 都重复了同样的次数），只留计数那一条会漏判。
        if len(first_level_block_ids) != len(set(first_level_block_ids)):
            raise FeishuDocxDeliveryError(
                "markdown_convert_duplicate_first_level_block_ids", definite=True
            )

        # 每个块都必须携带非空字符串 block_id，否则无法确认它在树里的位置——
        # 不做静默丢弃或猜测，走与"无处安放"同一个降级码。
        for block in mapping_blocks:
            block_id = block.get("block_id")
            if not isinstance(block_id, str) or not block_id:
                raise FeishuDocxDeliveryError(UNSUPPORTED_NESTED_BLOCKS, definite=True)
        by_block_id: dict[str, Mapping[str, Any]] = {block["block_id"]: block for block in mapping_blocks}
        # rc21 修复包 B（opus 审查发现）：`blocks` 里出现重复 block_id 时，上面
        # 那个字典推导式会静默用后一个覆盖前一个——前一个块的内容从此凭空消失，
        # 且不留任何痕迹。字典条数与原始块数对不上即拒绝。
        if len(by_block_id) != len(mapping_blocks):
            raise FeishuDocxDeliveryError("markdown_convert_block_count_mismatch", definite=True)

        # Issue #538：按 `children` 深度优先展开成一棵树。`claimed` 同时充当
        # 环检测与"同一个块被两个父块认领"的检测——两者都会让展开结果不是一棵
        # 树，前者还会无限递归。
        # 展开用显式栈而不是递归：单次块数上限是 :data:`MAX_CONVERTED_BLOCKS`
        # （1000），一条足够深的链会撞上解释器递归上限——那会是一个**没有被
        # 本模块任何失败分类接住**的 ``RecursionError``；外部响应不该有能力
        # 决定本进程用哪种方式崩。
        ordered: list[Mapping[str, Any]] = []
        claimed: set[str] = set()
        stack: list[object] = list(reversed(first_level_block_ids))
        while stack:
            block_id = stack.pop()
            if not isinstance(block_id, str) or block_id not in by_block_id:
                # 引用了一个在 `blocks` 数组里找不到的 block_id：响应内部不
                # 自洽，理论上不应发生。这与上面几条"明确知道拒绝原因"的
                # definite 分支不同——这里连"为什么不一致"都无法确定，归类为
                # 结果不明，同 :meth:`read_body_children` 既有的分类口径。
                raise LookupError("markdown 转换响应引用了一个不存在的块：结果不明")
            if block_id in claimed:
                raise FeishuDocxDeliveryError("markdown_convert_shared_child_block", definite=True)
            claimed.add(block_id)
            block = by_block_id[block_id]
            ordered.append(block)
            children = block.get("children")
            if isinstance(children, list):
                stack.extend(reversed(children))

        if len(ordered) != len(mapping_blocks):
            # 展开完成后仍有块没被任何父块认领——它既不是一级块、也不出现在
            # 任何可达块的 `children` 里，本仓库没有任何位置可以把它写进文档。
            # 这才是"真的处理不了"，交给 :meth:`write_body` 转成明示降级。
            raise FeishuDocxDeliveryError(UNSUPPORTED_NESTED_BLOCKS, definite=True)

        return ConvertedBody(
            children_id=tuple(first_level_block_ids),
            descendants=tuple(_strip_readonly_descendant_fields(block) for block in ordered),
        )

    def write_blocks(self, document_id: str, blocks: Sequence[Mapping[str, Any]]) -> None:
        """把已经是飞书 block 形状的 ``blocks`` 写进正文，沿用与
        :meth:`write_paragraphs` 完全相同的 children 插入端点（同一坐标、同一
        ``index=0`` 单次写入语义）——供 :meth:`write_body` 在开关打开时调用，
        也可单独测试。

        超过 :data:`MAX_CONVERTED_BLOCKS` 一律在发起任何请求之前整体拒绝，
        不做分批插入；理由见模块文档「markdown 官方转换开关」一节（分批会打破
        :meth:`read_body_children` 判据依赖的"正文一次写入"假设）。这是一次
        确定性拒绝（``definite=True``），沿用 ``too_many_blocks`` 这个专用原因
        码，不与飞书返回的业务错误码混用。
        """

        doc_id = _require_document_id(document_id)
        children = list(blocks) if blocks is not None else []
        if not children:
            raise ValueError("blocks 不能为空")
        if len(children) > MAX_CONVERTED_BLOCKS:
            raise FeishuDocxDeliveryError("too_many_blocks", definite=True)
        self._data(
            self._call(
                "POST",
                f"{_DOCX_DOCUMENTS_PATH}/{doc_id}/blocks/{doc_id}/children",
                body={"children": children, "index": 0},
            )
        )
        logger.info(
            "飞书 docx 正文已按官方转换写入 document_id_len=%s block数=%s", len(doc_id), len(children)
        )

    def write_descendant_blocks(self, document_id: str, body: ConvertedBody) -> None:
        """把一份含嵌套结构的 :class:`ConvertedBody` 一次写进正文（Issue #538）。

        ``POST /docx/v1/documents/{document_id}/blocks/{document_id}/descendant``
        ——**与 :meth:`write_blocks` 写的是同一个坐标**（根 block 自身的
        children、``index=0``、单次写入），区别只在于请求体额外带上块与块之间的
        父子关系：``children_id`` 是一级块的临时 block_id（文档顺序），
        ``descendants`` 是全部块，每个块用自己的 ``children`` 指向它的子块。
        飞书在响应的 ``block_id_relations`` 里把临时 id 映射成真实 block_id。

        **同一个坐标这件事是幂等判据成立的前提**：写完之后
        :meth:`read_body_children` 读到的恰好是这些一级块（Issue #538 stage
        受控探针实测：写前 0 个子块、写后 7 个，且嵌套块不出现在根 block 的
        children 里），因此检查点恢复路径的"子块非空 = 正文已写过"判据在嵌套
        写入下**逐字继续成立**，不需要重建。

        超过 :data:`MAX_CONVERTED_BLOCKS`（按 ``descendants`` 总块数计，不是
        一级块数）一律在发起任何请求之前整体拒绝，不做分批——理由与
        :meth:`write_blocks` 完全相同：分批会打破 :meth:`read_body_children`
        判据依赖的"正文一次写入"假设，中途状态会被下一次检查点恢复误判成
        "已经写完"。同样沿用 ``too_many_blocks`` 这个 definite 原因码。
        """

        doc_id = _require_document_id(document_id)
        descendants = list(body.descendants)
        children_id = list(body.children_id)
        if not descendants or not children_id:
            raise ValueError("descendants 与 children_id 都不能为空")
        if len(descendants) > MAX_CONVERTED_BLOCKS:
            raise FeishuDocxDeliveryError("too_many_blocks", definite=True)
        self._data(
            self._call(
                "POST",
                f"{_DOCX_DOCUMENTS_PATH}/{doc_id}/blocks/{doc_id}/{_BLOCKS_DESCENDANT_PATH_SUFFIX}",
                body={"children_id": children_id, "index": 0, "descendants": descendants},
            )
        )
        logger.info(
            "飞书 docx 正文已按官方转换写入（含嵌套块）document_id_len=%s 一级块数=%s 总块数=%s",
            len(doc_id),
            len(children_id),
            len(descendants),
        )

    def write_body(
        self, document_id: str, *, paragraphs: Sequence[str], markdown: str
    ) -> WriteBodyOutcome:
        """写正文的唯一装配入口：把「markdown 官方转换开关」变成实际可执行的
        分支（模块文档同名一节），并把"这次是不是降级写的"作为
        :class:`WriteBodyOutcome` 返回给调用方。

        ``markdown_convert_enabled=False``（构造函数默认值）时逐字调用
        :meth:`write_paragraphs`，返回 ``degraded_reason=None``——不是降级，
        是这套部署本来就要求的路径。为 ``True`` 时改走
        :meth:`convert_markdown_to_body`，再按这份正文里**有没有嵌套块**分派
        写入端点：纯一级块走既有的 :meth:`write_blocks`（``children`` 端点，
        请求体与 Issue #538 之前逐字相同）；含嵌套块（表格等）走
        :meth:`write_descendant_blocks`（``descendant`` 端点，同一个坐标）。
        **两支分开而不是统一走 descendant**：不含表格的排版今天在生产上是好
        的，本次修复只增加一条新路径，不动那条已经在跑的路径。

        **唯一的降级分支（Issue #499，产品负责人 2026-08-31 裁定）**：
        :meth:`convert_markdown_to_body` 抛出
        :data:`UNSUPPORTED_NESTED_BLOCKS` 这**一个**原因码时，改用
        :meth:`write_paragraphs` 交付纯文本段落，并返回
        ``degraded_reason=UNSUPPORTED_NESTED_BLOCKS``。裁定依据：翻转
        Issue #467 的默认值之后，含表格的回答从"降级但拿得到"变成"整次交付
        失败"，实测 22 次投递里 4 次（18.2%）命中，其中 1 次来自最简单的
        单公司/单指标/单月问题——用户要的是内容，格式损失可以如实告知，空手
        而归的代价明显更大。

        **Issue #538 收窄了这个码的触发条件**：它此前是"转换结果里出现了任何
        一个非一级块"，因此**含表格的回答 100% 命中**（生产 2026-09-02 实测
        3/3），标题与列表跟着表格一起被作废。现在嵌套结构由 ``descendant``
        端点正常写入，这个码只在"某个块连父块都找不到、真的无处安放"时触发。
        降级机制本身**原样保留**——降级仍然发生、仍然明示。

        **这条捕获必须窄到这一个码，且必须只包住转换调用本身**，两条都是安全
        前提，不是风格偏好：

        1. 只捕这一个码——``too_many_blocks``/``markdown_convert_*``/飞书业务
           错误码/``LookupError`` 全部维持原样向上抛。泛化成"捕获所有异常都
           降级"会把真实故障（限流、权限缺失、响应形状不对）一并吞成"交付
           成功"，那比原来的整次失败更糟。
        2. ``try`` 只包 :meth:`convert_markdown_to_body`——转换端点**不写入
           任何文档、没有外部副作用**，因此在它失败之后改走段落路径是安全的
           （这篇文档此刻还是空的）。:meth:`write_blocks` /
           :meth:`write_descendant_blocks` 一旦发起就有副作用，它们的失败
           **绝不能**触发第二次写入：那会把同一份正文写两遍。把 ``try`` 缩到
           转换这一步，即使将来写入端点也开始抛同一个码，这条约束也仍然成立。

        **降级不等于可以不告诉用户。** 本方法此前的文档字符串写的是"绝不捕获
        后静默退回纯文本段落路径"，理由是静默降级会制造"用户以为拿到了带格式
        的文档、实际收到的是另一种内容"的假象。那条理由**至今成立**——被推翻
        的只是"因此宁可整次失败"这个结论，不是"降级必须让用户知道"这条纪律。
        返回值就是为此存在：调用方（``apps/gateway/document_delivery.py``）
        必须接住它并改用明示降级的用户文案。**丢掉这个返回值 = 恢复成当初被
        明令禁止的静默降级。**

        **降级后的观感如实登记**：``paragraphs`` 由
        ``core/execution/document_delivery.py::normalize_markdown`` 产出，它按
        空行切段、段内换行折叠成空格且不剥离 markdown 语法字符——因此表格会被
        拍平成一段长文本，``|---|`` 这类分隔行会原样留在正文里。**本降级解决
        的是"拿不到"，不保证"好看"**，用户文案不得暗示格式完好。
        """

        if not self._markdown_convert_enabled:
            self.write_paragraphs(document_id, paragraphs)
            return WriteBodyOutcome()

        try:
            body = self.convert_markdown_to_body(markdown)
        except FeishuDocxDeliveryError as error:
            if error.code != UNSUPPORTED_NESTED_BLOCKS:
                raise
            # 到这里为止没有发起过任何写入（转换端点无副作用），这篇文档仍然
            # 是空的——改走段落路径不会重复交付内容。
            logger.warning(
                "飞书 docx 正文降级为纯文本段落路径 document_id_len=%s reason=%s",
                len(document_id),
                error.code,
            )
            self.write_paragraphs(document_id, paragraphs)
            return WriteBodyOutcome(degraded_reason=error.code)

        # Issue #538：含嵌套块（表格等）走 descendant 端点；纯一级块继续走既有
        # 的 children 端点。**刻意分两支、不是"反正 descendant 两种都能写"就
        # 统一走一条**：不含嵌套块的排版今天在生产上是好的，这条路径的请求体
        # 必须与本次修复之前逐字相同，修复只增加一条新路径、不改动既有那条。
        if body.nested:
            self.write_descendant_blocks(document_id, body)
        else:
            self.write_blocks(document_id, body.flat_children())
        return WriteBodyOutcome()

    def grant_full_access(self, document_id: str, open_id: str) -> None:
        """对 ``open_id`` 这个人授予文档级「可管理」（决策记录 2026-08-23 裁定的
        唯一授予档位）。

        ``POST /drive/v1/permissions/{document_id}/members?type=docx``，S0 探针
        实测：对个人 openid 原样接受、无降级。
        """

        doc_id = _require_document_id(document_id)
        member_id = _require_user_open_id(open_id)
        self._data(
            self._call(
                "POST",
                f"/drive/v1/permissions/{doc_id}/members",
                params={"type": DOCX_PERMISSION_TYPE},
                body={"member_type": OPENID_MEMBER_TYPE, "member_id": member_id, "perm": FULL_ACCESS_PERM},
            )
        )
        logger.info("飞书 docx 已授予可管理 document_id_len=%s", len(doc_id))

    def read_members(self, document_id: str) -> list[dict[str, Any]]:
        """读回协作者列表，供调用方判定"真实创建 + 权限读回后才算成功"。

        ``GET /drive/v1/permissions/{document_id}/members?type=docx``。真实响应
        把协作者数组放在 ``data.items``（编排者 2026-08-27 stage 真实调用实测：
        ``data`` 只有一个键 ``items``，每一项形状是
        ``{member_id, member_type, perm, perm_type}``），**不是**
        ``scripts/probe_drive_folder_permissions.py`` 探针文档里记的
        ``members``——那份探针针对的是 folder 权限对象类型，docx 类型的真实响应
        形状与其不同，此前的实现照抄了探针的字段名，导致读回在真实调用里必然
        ``LookupError``（四步全成功后仍判定 ``uncertain``）。优先读 ``items``；
        取不到时降级读一次 ``members``（兼容旧探针形状或未来可能的回归，不代表
        它是当前真实形状）。返回的每一项只保留 ``member_type``/``member_id``/
        ``perm`` 三个字段（同 ``scripts/probe_drive_folder_permissions.py`` 的
        ``_member_signature`` 取值口径），不透传飞书响应里可能携带的其它字段
        （例如真实响应额外带的 ``perm_type``）。
        """

        doc_id = _require_document_id(document_id)
        data = self._data(
            self._call("GET", f"/drive/v1/permissions/{doc_id}/members", params={"type": DOCX_PERMISSION_TYPE})
        )
        members = data.get("items")
        if not isinstance(members, list):
            members = data.get("members")
        if not isinstance(members, list):
            raise LookupError("读回协作者响应缺少 items/members 字段：结果不明")
        return [
            {
                "member_type": member.get("member_type"),
                "member_id": member.get("member_id"),
                "perm": member.get("perm"),
            }
            for member in members
            if isinstance(member, Mapping)
        ]

    def read_body_children(self, document_id: str) -> list[dict[str, Any]]:
        """读回正文根 block（``document_id`` 自身）当前的子块列表（Issue #353）。

        ``GET /docx/v1/documents/{document_id}/blocks/{document_id}/children``：
        与 :meth:`write_paragraphs` 写入的是同一个坐标（同一个根 block、同一个
        ``children`` 集合），这里只是把同一个位置反过来读一遍，不做任何推断。
        调用方（``apps/gateway/document_delivery.py``）据此判断"这篇文档是否
        已经写过正文"，非空即跳过重驱写正文步——完整理由、已知未验证的假设与
        真实链路验证建议见模块文档字符串「幂等判据新增方法」一节。

        真实响应形状比照 :meth:`read_members`（本模块另一处"读列表"调用）：
        协作者列表放在 ``data.items``，这里假定同一接口族同一口径，一并抛
        ``LookupError`` 归类为结果不明（成功响应缺可回读结构 ≠ 确定为空）。
        """

        doc_id = _require_document_id(document_id)
        data = self._data(
            self._call("GET", f"{_DOCX_DOCUMENTS_PATH}/{doc_id}/blocks/{doc_id}/children")
        )
        children = data.get("items")
        if not isinstance(children, list):
            raise LookupError("读回正文根 block 子块响应缺少 items 字段：结果不明")
        return [child for child in children if isinstance(child, Mapping)]

    def document_url(self, document_id: str) -> str:
        """拼出用户可直接打开的文档链接（见模块文档「文档 URL 的构造」一节）。

        纯本地拼接，不发起任何请求。
        """

        doc_id = _require_document_id(document_id)
        return f"https://{self._tenant_domain}/docx/{doc_id}"


__all__ = [
    "DOCX_PERMISSION_TYPE",
    "FULL_ACCESS_PERM",
    "MAX_CONVERTED_BLOCKS",
    "OPENID_MEMBER_TYPE",
    "UNSUPPORTED_NESTED_BLOCKS",
    "USER_OPEN_ID_PREFIX",
    "ConvertedBody",
    "FeishuDocxDeliveryError",
    "LarkDocxDelivery",
    "WriteBodyOutcome",
    "REQUEST_TIMEOUT_SECONDS",
    "Transport",
    "urllib_transport",
]

# 说明：`read_body_children`/`convert_markdown_to_body`/`write_blocks`/
# `write_descendant_blocks`/`write_body` 都是 `LarkDocxDelivery` 的实例方法，
# 不单独导出符号——同
# `create_document`/`write_paragraphs`/`grant_full_access`/`read_members`
# 既有方法一样，只通过类本身暴露。
