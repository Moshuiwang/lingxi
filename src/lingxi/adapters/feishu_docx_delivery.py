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

七个会发起真实调用的方法（Issue #408 新增 :meth:`LarkDocxDelivery.
convert_markdown_to_blocks`/:meth:`LarkDocxDelivery.write_blocks`，见「markdown
官方转换开关」一节）都不捕获任何未预期异常。飞书业务错误码明确非 0 时抛出
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

## markdown 官方转换开关（Issue #408，默认关闭）

正文交付此前把模型产出的 markdown 逐字符剥离成纯文本段落写入（``core.execution.
document_delivery.normalize_markdown``），代价是正文里的连字符会被一并吃掉——
「周环比 -12.85%」被剥成「周环比 12.85%」，负号丢失，属于数据正确性缺陷。产品
负责人 2026-08-29 裁定分两步修：立即停止字符剥离（小修，`core` 侧已完成，见该
模块），正式排版走飞书官方转换接口——本节是正式方案在本模块的落点，**默认
关闭**。管线接线（迁移 0079 持久化原始 markdown、gateway 配置读取
``LINGXI_DOCX_MARKDOWN_CONVERT``）已随本批完成，见
``apps/gateway/document_delivery.py`` 模块文档「markdown 官方转换路径的接线」
一节与 ``apps/gateway/config.py``——开关默认值仍是 ``False``，接线本身不改变
现网行为，只是把"开关打开后会发生什么"从"能力已就绪但够不到"变成"配置一个
环境变量即可生效"。

- :meth:`LarkDocxDelivery.convert_markdown_to_blocks`：``POST /docx/v1/documents/
  blocks/convert``（``content_type=markdown``），把一段 markdown 转换成飞书官方
  block 结构。这个端点只做转换、不写入任何文档，失败或重试都不产生外部副作用。
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
  2. 存在任意一个块的 ``block_id`` 不在 ``first_level_block_ids`` 内（典型
     场景：表格等嵌套结构——表格自身是一级块，但它的单元格是作为独立元素
     出现在 ``blocks`` 数组里、却不出现在 ``first_level_block_ids`` 里的
     子块）→ ``unsupported_nested_blocks``。本仓库当前只支持"结果是一份
     纯一级块序列"的 markdown（标题、列表、正文段落等），**不支持任何带
     嵌套结构的 markdown**（表格是已知的第一个例子）——这是一个已登记的
     后续扩展点，不是本次修复的交付范围。
     若 ``first_level_block_ids`` 引用了一个在 ``blocks`` 数组里找不到的
     ``block_id``（响应内部不自洽，理论上不应发生），归类为「结果不明」
     ``LookupError``，同 :meth:`read_body_children` 既有的"响应形状不对但
     不是飞书明确拒绝"分类口径——这与上面两条"明确知道拒绝原因"的
     ``definite`` 分支不同：这里连"为什么不一致"都无法确定。
  3. 重排完成后再补两道对账（rc21 修复包 B，opus 审查发现，同上面两条一样
     ``definite=True``）：``first_level_block_ids`` 自身出现重复
     block_id → ``markdown_convert_duplicate_first_level_block_ids``（复现：
     同一段正文在文档里被重复交付两次）；重排后的块数与 ``mapping_blocks``
     原始块数对不上 → ``markdown_convert_block_count_mismatch``（复现：
     ``mapping_blocks`` 里出现重复 block_id 时，建映射的字典推导式会静默
     用后一个覆盖前一个，前一个块的内容凭空消失）。两条对账各自独立，互不
     替代——两种成因在计数上恰好互相抵消时（`mapping_blocks` 与
     `first_level_block_ids` 对同一个 block_id 都重复了同样的次数），只留
     其中一条会漏判。

  返回前还会剔除每个块里的只读字段（``block_id``/``parent_id``/``children``，
  见 :func:`_strip_readonly_block_fields`）——这些字段描述"这个块在文档里的
  位置/身份"，是服务器生成的，插入端点（``blocks/{document_id}/children``）
  不接受随插入请求带回同名字段（同官方"重新插入表格 block 前须剔除只读
  ``merge_info``"一类约束：响应里凡是描述块的位置/关系的字段都不可回插，只有
  描述块长什么样的字段才能原样写回）。

  **仍未被本仓库任何真实探针验证的假设**（如实标注，不静默宣称已验证）：
  每个返回的块都携带非空字符串 ``block_id``（重排映射的前提）；剔除的三个
  字段是插入端点全部拒绝的只读字段全集（目前只确认 ``block_id`` 一定是只读
  的，``parent_id``/``children`` 是同类推断，未见真实插入报错佐证或证伪）。
  这两条假设在本次 Issue #442 的受控探针范围之外，留给「验证条件补强」一节
  要求的自证闭环真实探针核实。
- :meth:`LarkDocxDelivery.write_blocks`：把已经是飞书 block 形状的数组沿用与
  :meth:`write_paragraphs` 完全相同的 children 插入端点写入（同一坐标、同一
  ``index=0`` 单次写入语义）。**超过 :data:`MAX_CONVERTED_BLOCKS`（1000，飞书
  单次插入上限）一律整体拒绝，不做分批插入**——分批会打破
  :meth:`read_body_children` 判据依赖的"正文一次写入"假设（见上文「幂等判据
  新增方法」一节）：分批写入的中途状态（例如已经插入前 1000 个 block、还剩
  部分未插入）会被下一次检查点恢复误判成"已经写完"，从而跳过本该继续的写入，
  比本次要解决的问题更严重；因此选择在超限时直接失败关闭，用 ``definite`` 原因
  码 ``too_many_blocks`` 让调用方明确知道这是一次确定性拒绝，不需要重试。这个
  取舍的代价是超长 markdown 无法交付带格式的文档——本模块认为"明确拒绝"优于
  "悄悄改变幂等语义"，是否需要为超限场景另设计分批状态机留给未来 Story。
- :meth:`LarkDocxDelivery.write_body`：写正文的唯一装配入口，把「开关」变成
  实际可执行的分支——``markdown_convert_enabled=False``（构造函数默认值，即
  本 Story 交付后的零行为变化状态）时逐字调用 :meth:`write_paragraphs`；为
  ``True`` 时改走 :meth:`convert_markdown_to_blocks` + :meth:`write_blocks`。
  开关打开时任何一步失败（业务错误码、结果不明、超过 block 数上限）都直接向
  上抛出，**绝不捕获后静默退回纯文本段落路径**——静默降级会制造"用户以为拿到
  了带格式的文档，实际收到的是转换失败前的另一种内容"这种更难排查的假象，
  与本模块 :meth:`_data` 一贯的"绝不静默降级"姿态一致。
- **开关本身不是环境变量**：`adapters/` 不直接读 ``os.environ``（[代码框架
  「三、横切约定」](../../../../docs/技术设计/代码框架.md)的硬性约束），
  ``markdown_convert_enabled`` 是构造函数参数——真正的环境变量
  ``LINGXI_DOCX_MARKDOWN_CONVERT`` 由装配层 ``apps/gateway/config.py`` 读取后
  作为普通布尔值传进来（默认关，非空且不精确等于 ``"1"`` 时启动即失败，与
  ``apps/worker/config.py`` 既有开关同一姿态）。**接线已完成**（迁移 0079、
  Issue #408 正式方案接线批次）：``task_document_delivery_request`` 新增可空
  ``markdown`` 列持久化原始 markdown 全文，``apps/gateway/document_delivery.py``
  的 :meth:`DocumentDeliveryConsumer._process_docx_claim` 按这一列是否非
  ``None`` 决定要不要调用 :meth:`write_body`（非 ``None`` 才调用；``None`` 无
  条件回退 :meth:`write_paragraphs`，与开关是否打开无关）——完整接线细节见该
  模块文档「markdown 官方转换路径的接线」一节。

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

#: 官方转换响应里每个块携带的只读字段（Issue #442），插入端点不接受随请求体
#: 带回——见 :meth:`LarkDocxDelivery.convert_markdown_to_blocks` 文档字符串
#: 「markdown 官方转换开关」一节对应小节的完整理由与已知未验证假设。
_CONVERT_RESPONSE_READONLY_BLOCK_KEYS = ("block_id", "parent_id", "children")

#: 单次 children 插入端点的 block 数上限（模块文档「官方能力事实」核实口径，
#: 未做真实调用探针）。超过时 :meth:`LarkDocxDelivery.write_blocks` 整体拒绝、
#: 不分批插入——理由见模块文档「markdown 官方转换开关」一节：分批会打破
#: :meth:`LarkDocxDelivery.read_body_children` 判据依赖的"正文一次写入"假设。
MAX_CONVERTED_BLOCKS = 1000


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
    返回可以原样传给插入端点的浅拷贝（不修改入参）。"""

    return {key: value for key, value in block.items() if key not in _CONVERT_RESPONSE_READONLY_BLOCK_KEYS}


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

    def convert_markdown_to_blocks(self, markdown: str) -> list[dict[str, Any]]:
        """把一段 markdown 转换成飞书官方 block 结构、按文档真实顺序排好
        （Issue #408 正式方案，默认关闭——见 :meth:`write_body` 与模块文档
        「markdown 官方转换开关」一节；重排与防御性拒绝的完整理由见该节
        Issue #442 更新的段落）。

        ``POST /docx/v1/documents/blocks/convert``，请求体 ``{"content_type":
        "markdown", "content": markdown}``。这个端点只做转换、不写入任何文档，
        失败或重试都不产生外部副作用。**响应体 ``data.blocks`` 不是文档顺序**
        （Issue #442 受控探针实证）——真实顺序由 ``data.first_level_block_ids``
        给出，本方法据此重排后才返回；``data.block_id_to_image_urls`` 是响应里
        的另一个键，与 blocks 无关，本方法不读取它。
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
            # 之前这里静默返回空列表，让空列表流进
            # :meth:`write_blocks`，那里再触发一条与"未发起任何请求的入参校验"
            # 同型的裸 ``ValueError("blocks 不能为空")``——把"飞书响应形状不对"
            # 误归进了 gateway 消费循环白名单里"发出请求前的确定性入参校验"
            # 那一类（模块 `apps/gateway/document_delivery.py` 文档「四步的
            # 失败分类只有两种」a 项），而它其实应该走同一个白名单里的
            # `FeishuDocxDeliveryError` 分支。这个失败是确定性的（转换端点
            # 不写入任何文档、没有外部副作用，同一份 markdown 重放会得到同样
            # 的转换结果），因此标 ``definite=True``——与
            # :meth:`write_blocks` 的 ``too_many_blocks`` 走同一类"转换/写入
            # 前置校验发现的确定性失败"。
            raise FeishuDocxDeliveryError("markdown_convert_blocks_not_mapping", definite=True)

        # Issue #442：`blocks` 数组不是文档顺序，真实顺序在
        # `first_level_block_ids` 里。缺失/为空一律 definite 拒绝——没有这份
        # 顺序清单就无法保证交付顺序正确，宁可拒绝也不猜测顺序。
        first_level_block_ids = data.get("first_level_block_ids")
        if not isinstance(first_level_block_ids, list) or not first_level_block_ids:
            raise FeishuDocxDeliveryError(
                "markdown_convert_missing_first_level_block_ids", definite=True
            )

        # 每个块必须携带非空字符串 block_id 且必须出现在
        # first_level_block_ids 里，否则一律 definite 拒绝——这既拦住表格一类
        # 嵌套结构（表格自身是一级块，但它的单元格作为独立元素出现在
        # `blocks` 里、却不在 `first_level_block_ids` 内），也拦住"块缺
        # block_id 因而无法确认层级"这种更基础的形状不对，两者都无法安全
        # 判断该块该不该、该按什么顺序交付，不做静默丢弃或猜测。
        first_level_ids = {
            block_id for block_id in first_level_block_ids if isinstance(block_id, str) and block_id
        }
        for block in mapping_blocks:
            block_id = block.get("block_id")
            if not isinstance(block_id, str) or block_id not in first_level_ids:
                raise FeishuDocxDeliveryError("unsupported_nested_blocks", definite=True)

        by_block_id = {block["block_id"]: block for block in mapping_blocks}
        ordered_blocks: list[Mapping[str, Any]] = []
        for block_id in first_level_block_ids:
            block = by_block_id.get(block_id) if isinstance(block_id, str) else None
            if block is None:
                # `first_level_block_ids` 引用了一个在 `blocks` 数组里找不到
                # 的 block_id：响应内部不自洽，理论上不应发生。这与上面两条
                # "明确知道拒绝原因"的 definite 分支不同——这里连"为什么不
                # 一致"都无法确定，归类为结果不明，同
                # :meth:`read_body_children` 既有的分类口径。
                raise LookupError(
                    "markdown 转换响应 first_level_block_ids 引用了不存在的块：结果不明"
                )
            ordered_blocks.append(block)

        # 重排后两道对账（rc21 修复包 B，opus 审查发现，与上面「缺失/为空」
        # 「引用不存在的块」两道既有防线同型同码风格——definite 拒绝，不静默
        # 丢块或重复交付）：
        #
        # 1. `first_level_block_ids` 本身出现重复 block_id：`by_block_id` 是
        #    按 `block_id` 建的字典，重复的 id 在这一步已经把同一个块对象
        #    在 `ordered_blocks` 里放了不止一次——复现："first_level 重复静默
        #    重复交付"（同一段正文在飞书文档里出现两次）。到这里为止所有
        #    entries 都已经确认是有效字符串（否则上面的循环早就因为
        #    `block is None` 抛出 `LookupError`），因此直接用 `set` 判重複，
        #    不需要再过滤一遍。
        if len(first_level_block_ids) != len(set(first_level_block_ids)):
            raise FeishuDocxDeliveryError(
                "markdown_convert_duplicate_first_level_block_ids", definite=True
            )
        #
        # 2. 重排后的块数与 `mapping_blocks` 原始块数对不上：`by_block_id`
        #    是字典推导式，`mapping_blocks` 里出现重复 block_id 时后一个会
        #    静默覆盖前一个——复现："重复 block_id 静默丢块"（前一个块的内容
        #    从此在返回结果里凭空消失，且不留任何痕迹）。上面第 1 条已经
        #    挡住"`first_level_block_ids` 自身重复"这一种成因，这一条挡的
        #    是"`mapping_blocks` 自身重复、而 `first_level_block_ids` 无重复"
        #    这一种成因——两种成因互相独立，其中一种恰好在计数上抵消另一种
        #    时（`mapping_blocks` 与 `first_level_block_ids` 对同一个
        #    block_id 都重复了同样的次数），单靠这一条计数对账会漏判，所以
        #    两条对账都必须做，不能只留一条。
        if len(ordered_blocks) != len(mapping_blocks):
            raise FeishuDocxDeliveryError("markdown_convert_block_count_mismatch", definite=True)

        return [_strip_readonly_block_fields(block) for block in ordered_blocks]

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

    def write_body(self, document_id: str, *, paragraphs: Sequence[str], markdown: str) -> None:
        """写正文的唯一装配入口：把「markdown 官方转换开关」变成实际可执行的
        分支（模块文档同名一节）。

        ``markdown_convert_enabled=False``（构造函数默认值）时逐字调用
        :meth:`write_paragraphs`——本 Story 交付后各现网调用路径的默认行为
        零变化。为 ``True`` 时改走 :meth:`convert_markdown_to_blocks` +
        :meth:`write_blocks`：这条路径上任何一步失败（业务错误码、结果不明、
        超过 block 数上限）都直接向上抛出，**绝不捕获后静默退回纯文本段落
        路径**——静默降级会制造"用户以为拿到了带格式的文档，实际收到的是
        转换失败前的另一种内容"这种更难排查的假象，与 :meth:`_data` 一贯的
        "绝不静默降级"姿态一致。
        """

        if self._markdown_convert_enabled:
            blocks = self.convert_markdown_to_blocks(markdown)
            self.write_blocks(document_id, blocks)
        else:
            self.write_paragraphs(document_id, paragraphs)

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
    "USER_OPEN_ID_PREFIX",
    "FeishuDocxDeliveryError",
    "LarkDocxDelivery",
    "REQUEST_TIMEOUT_SECONDS",
    "Transport",
    "urllib_transport",
]

# 说明：`read_body_children`/`convert_markdown_to_blocks`/`write_blocks`/
# `write_body` 都是 `LarkDocxDelivery` 的实例方法，不单独导出符号——同
# `create_document`/`write_paragraphs`/`grant_full_access`/`read_members`
# 既有方法一样，只通过类本身暴露。
