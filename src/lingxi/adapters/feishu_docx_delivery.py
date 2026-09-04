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

六个会发起真实调用的方法（:meth:`LarkDocxDelivery.create_document`、
:meth:`LarkDocxDelivery.create_document_with_markdown`、
:meth:`LarkDocxDelivery.write_paragraphs`、
:meth:`LarkDocxDelivery.grant_full_access`、:meth:`LarkDocxDelivery.read_members`、
:meth:`LarkDocxDelivery.read_body_children`）都不捕获任何未预期异常。飞书业务错误码明确非 0 时抛出
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

**这条判据依赖的两个假设已在 2026-09-03 stage 受控探针实测确认**（Trace #544
P-docs_ai 探针四，Bot-Test 真实调用，受控文档用后删除并回读确认；此前本节登记
为"尚未在任何真实调用中实测"，现予关闭）：（a）老路径 ``POST /docx/v1/documents``
新建的空文档，根 block 子块数确实 ``= 0``——标题是文档元数据、不占子块，"子块
非空"精确对应"正文已经写过"；（b）``GET /docx/v1/documents/{id}/blocks/{id}/
children`` 的响应键确实是 ``has_more`` ＋ ``items``（同 :meth:`read_members`
的形状口径）。同一次探针还确认这条判据在**服务端一次建档**（见下节）建出来的
文档上照常可用：综合样本文档的根 block 返回 15 个一级子块。假设不成立时的后果
（把"刚建档、从未写过正文"误判成"已经写过"从而跳过首次写正文）因此不再是敞口。

## 服务端一次建档写全文（``docs_ai``；Trace #544 S-7c，2026-09-03 stage 受控探针实证）

正文交付的**写入机制**在本批整条换掉：此前是「客户端调 ``blocks/convert`` 把
markdown 转成块 → 客户端按 ``children`` 拼树 → 调 ``children``/``descendant``
把块写进文档」，现在是**一次调用**——把整段 markdown 原文交给飞书，由服务端
建档并排版：

- 端点：``POST /open-apis/docs_ai/v1/documents``，请求体
  ``{"format": "markdown", "content": <正文>}``；**标题以 ``<title>…</title>``
  拼在正文最前面**，不是单独的字段。
- 响应：``data.document = {document_id, revision_id, url}``，另有可选的
  ``result``（``success``/``partial_success``/``failed``）与 ``warnings``。
- 身份：``tenant_access_token``（应用身份）**直接可用，不需要新增任何 scope**
  （探针一实测 ``code=0``，无 scope 类错误码）。反过来，``docx:document.block:
  convert`` 这个 scope 随 convert 端点一起**不再被本模块使用**。

**换路的依据不是"更短更好看"，是这条路径把排版责任交回飞书**：客户端拼树那条
路每支持一种嵌套形态都要自己实测一次（Issue #538 只对表格做过写入探针，引用块／
嵌套列表／代码块从未验证，一旦被写入端点拒绝就是整次交付失败）。服务端一次
建档对综合样本（标题 1–3 级、三层嵌套无序列表、两层嵌套有序列表、引用块、代码
块、待办、加粗、链接、含 ``-12.85%`` 与 ``3-5%`` 的表格、含 ``|`` 的单元格、
分隔线）实测**十种形态一个不多一个不少**、负号/区间/竖线逐字保真、零 ``warnings``
（探针二）。落点、所有权、授权档位、链接形态与老路径**逐字相同**（探针三：同一
个云空间目录、``owner_id`` 同为机器人、``document_url`` 本地拼接结果与响应
``url`` 一致），**授权与投递环节零改动**。

### 坑一：超时会留下拿不到 id 的完整孤儿文档，**一律不重试建档**

探针五实测：200 000 字符正文 → **HTTP 504 ``code=2200``**，耗时 31.3 s；但事后
列目录发现**服务端其实已经把这篇完整文档建出来了**，调用方没拿到
``document_id``。含义：``docs_ai`` 建档超时 ＝ **结果不明且不可回读**——重试会
产生第二篇**完整**文档（老路径的孤儿只是一篇空文档，这里的孤儿带着全文）。

本模块的硬规则，两条一起才成立：

1. **超时/5xx 一律判「结果不明」，永不重试建档。** :func:`urllib_transport` 对
   HTTP **5xx 一律不解析响应体**，直接抛 ``FeishuDocxDeliveryError(definite=
   False)``——不能让响应体里那个 ``code=2200`` 把它伪装成"飞书明确拒绝"
   （``definite=True``）：网关超时不证明请求没有生效，判成 definite 会让上层
   把一次"可能已经建好文档"的调用记成确定性失败。调用方
   （``apps/gateway/document_delivery.py``）据此落 ``uncertain``——按
   ``V-交付-03``，``uncertain`` **不自动重试**，转人工核对，这正是"不重试建档"
   在状态机层面的落实。本模块自身与默认传输层都不做任何重试。
2. **正文长度前置守卫**（:data:`MAX_MARKDOWN_CHARS`）：与其拿一份超长正文去撞
   504，不如**在发出请求之前**就判定这条路走不通，改走两步的段落路径并明示
   降级。见该常量的取值依据。

### 坑二：``warnings`` **不覆盖全部降级**，不能只看它

``data.warnings`` 是字符串数组、每项形如 ``degrade_code=2108,msg=…``，**只在
部分降级时出现**；但探针二同时实测：原始 HTML 块（``<div>…</div>``）被**静默
丢弃且不产生任何 warning**。所以「有 warnings 即全部告知」**不成立**，
「如实告知格式已简化」这个用户承诺不能**只**建立在 ``warnings`` 上。

:meth:`LarkDocxDelivery.create_document_with_markdown` 因此把 ``result`` 与
``warnings`` **一起**判，并且在拿不准时**倒向多说一句**（见
:func:`_degraded_reason`）：``result="failed"`` → 确定性失败；``warnings`` 非空
→ 降级；``result`` 是除 ``success`` 之外的任何取值（含 ``partial_success`` 与
未登记的新取值）→ 降级；``result`` **不存在** → 不降级（探针二实测：干净成功
时 ``data`` 只有 ``document`` 一个键，"键不存在"就是零告警）。

**如实标注的残余边界**：服务端静默丢弃的形态（原始 HTML 块、``~~删除线~~``、
``==高亮==``、``$$…$$``）既不出现在 ``warnings`` 里、``result`` 也仍是
``success``——这类丢弃**本模块无法感知**，因此不会告知用户。模型产出的是普通
markdown，实际暴露面很低，但这是一条**已知的、未关闭的**边界，不得声称"所有
降级都会被如实告知"。

### 开关保留为止损闸：``markdown_convert_enabled``

``LINGXI_DOCX_MARKDOWN_CONVERT``（装配层解析成构造函数参数
``markdown_convert_enabled``，``adapters/`` 不直接读 ``os.environ``）**刻意不
随 convert 端点一起退役**：它现在的含义从"要不要调官方转换接口"变成"要不要走
服务端一次建档这条路"。这个接口在飞书开放平台**没有公开文档页**（两轮检索未
命中），限流、长度上限、SLA 官方无契约——留一个不需要改代码、不需要重新构建
镜像就能退回纯段落路径的止损闸，代价只是一个已经存在的配置项，收益是生产上
出问题时有一条立即可用的退路。语义与取值解析逐字不变，见
``apps/gateway/config.py::_markdown_convert_enabled``。

### 与检查点状态机的关系：**换机制，不动机械**

「建档」这一步现在顺带把正文写完了，但 ``apps/gateway/document_delivery.py``
的检查点、幂等判据与崩溃恢复路径**沿用原来那一套**，四步（建档 → 写正文 →
授权 → 读回）一步不减。理由是探针四实测的两条事实：

1. **现行判据在 docs_ai 建出来的文档上照常可用**：``read_body_children`` 读的
   那个坐标（根 block 的 children）在综合样本文档上返回 15 个一级子块，"非空
   ＝ 正文已写"语义不变，该方法一行不改；
2. **「文档存在即正文已写」作为普适命题不成立**——只有 ``<title>``、没有正文
   的 content 建出来的文档，根 block 子块数 ``= 0``。

而且本模块保留的**段落路径仍然是两步**（:meth:`create_document` +
:meth:`write_paragraphs`，服务于开关关闭、``markdown`` 列为 ``NULL``、以及
前置守卫命中这三种情况），"建了档、正文还没写"这个中间态在生产上真实存在，
所以那条判据**继续有判别力，不是恒真**。压缩状态机是安全性改动、不是重构
（#353 正是在这里出过事故），本批不做。

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
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

#: 服务端一次建档写全文端点（Trace #544 S-7c，见模块文档同名一节）。**不带
#: ``/docx`` 前缀**——它是另一个接口族（``docs_ai``），不是 docx 块 API 的子路径。
_DOCS_AI_DOCUMENTS_PATH = "/docs_ai/v1/documents"

#: 一次建档的正文格式。另一个合法取值是 ``xml``（带块 id 的结构化形式），本模块
#: 只发 markdown——模型产出的就是 markdown，转成 xml 等于把刚交回服务端的排版
#: 责任又拿回来一次。
_MARKDOWN_FORMAT = "markdown"

#: 标题在一次建档里的承载方式：拼在正文最前面的一个标签，不是独立字段。
_TITLE_OPEN_TAG = "<title>"
_TITLE_CLOSE_TAG = "</title>"

#: 服务端对这次建档的自评（``data.result``）。只有 ``success`` 与"该键不存在"
#: 算作"没有降级"，其余一切取值都倒向降级——见 :func:`_degraded_reason`。
_RESULT_SUCCESS = "success"
_RESULT_FAILED = "failed"

#: ``result="failed"`` 的原因码：服务端明确说这次建档失败。判 ``definite``——
#: 这是服务端给出的结论，不是传输层的猜测。
DOCS_AI_RESULT_FAILED = "docs_ai_result_failed"

#: 正文长度**前置守卫**阈值（字符数）。超过它就不去调一次建档端点，改走两步的
#: 段落路径并明示降级——见模块文档「坑一」。
#:
#: **这个数字来自 2026-09-03 stage 实测，不是官方文档**：该接口在飞书开放平台
#: 没有公开文档页（两轮检索未命中），长度上限与限流**官方无契约**。实测四个
#: 锚点：
#:
#: 1. 50 043 字符 → ``code=0`` 建档成功，但服务端耗时 **11.2 s**；
#: 2. 200 044 字符 → **HTTP 504 ``code=2200``**（31.3 s），且**文档其实已经建
#:    出来了**、调用方拿不到 id（模块文档「坑一」）；
#: 3. 本模块自己的出站超时 :data:`REQUEST_TIMEOUT_SECONDS` 是 **20 秒**——真正
#:    的天花板是这一条，不是那个 200 000 字符的网关超时点：50 000 字符已经用掉
#:    11.2 s，只剩不到一半余量，而服务端耗时随负载浮动、也随正文结构复杂度
#:    （表格、嵌套列表）变化，不是字符数的线性函数；
#: 4. 生产实测的真实正文规模是 **3 268–5 800 字符**（Trace #544 W0-10，生产
#:    首日三篇 docx）。
#:
#: 取 20 000：比实测最大真实正文大约 4 倍（守卫不会误伤正常交付），比实测安全点
#: 低 60%、比实测失败点低 90%（撞 504 的概率被压到很低），与登记侧
#: ``core.execution.document_delivery.MAX_TOTAL_CHARS`` 同一量级但**独立取值**
#: ——那一条是产品规则"正文不该这么长"，这一条是外部接口的实测安全带，两者互不
#: 依赖：登记侧上限（``MAX_RAW_MARKDOWN_CHARS`` = 40 000）将来若放宽，这道守卫
#: 仍然独立成立。
MAX_MARKDOWN_CHARS = 20_000

#: 明示降级的原因码。三个取值都会让调用方把 ``body_degraded_reason`` 落库并改用
#: 「格式已简化」的用户文案（``apps/gateway/document_delivery.py``），区别只在
#: 于**为什么**降级，供运维按 ``task_id`` 查因：
#:
#: - :data:`BODY_TOO_LONG` / :data:`TITLE_NOT_EMBEDDABLE`：**发出任何请求之前**
#:   就判定这份正文不适合走一次建档，改走两步段落路径。这两个码由
#:   :meth:`LarkDocxDelivery.create_document_with_markdown` 抛出，是调用方唯一
#:   允许捕获并改走段落路径的两个码（它们抛出时**尚未发生任何外部副作用**，
#:   这是改路安全的前提）；
#: - :data:`SERVER_SIMPLIFIED_BODY`：一次建档**已经成功**，但服务端自陈这次
#:   排版有降级（``result`` 非 ``success``，或 ``warnings`` 非空）。文档已经
#:   建好也写好了，不改路、不重试，只是如实告知。
#:
#: 历史取值 ``unsupported_nested_blocks``（Issue #499，客户端转换路径的降级码）
#: 随 convert 路径一起退役，**不再产生新行**；生产库里已经落下的历史行不受影响
#: ——``apps/gateway/document_delivery.py`` 的通知分派只判"这一列是不是非空"，
#: 不枚举取值，迁移 0082 的 CHECK 也只约束"只有 docx 行可以有取值"、不约束取值
#: 本身。
BODY_TOO_LONG = "body_too_long"
TITLE_NOT_EMBEDDABLE = "title_not_embeddable"
SERVER_SIMPLIFIED_BODY = "server_simplified_body"

#: :meth:`LarkDocxDelivery.create_document_with_markdown` 在**发出请求之前**
#: 抛出的原因码集合。独立成常量而不是散落的字面量：它同时是抛出点与唯一捕获点
#: （``apps/gateway/document_delivery.py`` 的改路分支）的判据，两处必须逐字
#: 一致——写成两处字面量时，任何一侧改名都会让改路分支悄悄失效、把一次本可
#: 降级交付的请求变成整次失败，而没有任何东西会红。
PRE_FLIGHT_DEGRADE_REASONS = frozenset({BODY_TOO_LONG, TITLE_NOT_EMBEDDABLE})


@dataclass(frozen=True)
class CreatedDocument:
    """:meth:`LarkDocxDelivery.create_document_with_markdown` 的返回值：这次
    一次建档到底建出了什么（Trace #544 S-7c）。

    ``document_id``：新文档的标识，正文**已经**随这次调用写完（不存在"建了档、
    正文还没写"的中间态——见模块文档「与检查点状态机的关系」）。

    ``degraded_reason``：``None`` ＝ 服务端未自陈任何降级；非 ``None`` ＝ 服务端
    说这次排版有简化（当前唯一取值 :data:`SERVER_SIMPLIFIED_BODY`），**用户拿到
    的排版与他本该拿到的不同**。

    **为什么必须有返回值、而不是让适配器自己把降级咽下去**：静默降级会制造
    "用户以为拿到了带格式的文档、实际收到另一种内容"的假象。产品负责人
    2026-08-31 就 Issue #499 裁定用「降级 ＋ 如实告知」取代「整次失败」，取代的
    是失败结论，**不是"必须让用户知道"这条纪律**。这个字段就是那条跨模块信号；
    调用方（``apps/gateway/document_delivery.py``）丢掉它，等于把裁定退化成当初
    被明令禁止的静默降级。
    """

    document_id: str
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
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
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
        raise ValueError(
            "tenant_domain 必须是裸域名（不含协议、路径或空白），例如 example.feishu.cn"
        )
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
    授予一个错误的收件人）。
    """
    text = (open_id or "").strip()
    if not text.startswith(USER_OPEN_ID_PREFIX) or len(text) <= len(USER_OPEN_ID_PREFIX):
        raise ValueError(
            f"open_id 必须是飞书用户 open_id（以 {USER_OPEN_ID_PREFIX} 开头），不回显收到的值"
        )
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


def _build_markdown_content(title: str, markdown: str) -> str:
    """把标题与正文拼成一次建档的 ``content``：``<title>…</title>`` ＋ 空行 ＋
    正文（Trace #544 S-7c，与官方 CLI 同一形态）。

    只做拼接，不改写正文一个字符——本仓库 2026-08-29 裁定停止字符级剥离的理由
    仍然成立（正文里的 ``-12.85%``/``3-5%`` 属于数据本身，不是语法噪音）。
    标题里若含尖括号，拼接会破坏这个标签的边界，由调用方在拼接**之前**拦下
    （见 :meth:`LarkDocxDelivery.create_document_with_markdown`），这里不做
    任何静默转义。
    """
    return f"{_TITLE_OPEN_TAG}{title}{_TITLE_CLOSE_TAG}\n\n{markdown}"


def _degraded_reason(data: Mapping[str, Any]) -> str | None:
    """按服务端自陈判定这次建档有没有降级；``result="failed"`` 直接抛确定性
    失败（模块文档「坑二」）。

    判定顺序与"拿不准倒向多说一句"的方向都是刻意的：

    1. ``result="failed"`` → :data:`DOCS_AI_RESULT_FAILED`（``definite=True``）。
       与官方 CLI 同一口径：服务端说失败就是失败，不猜"也许文档其实建出来了"。
    2. ``warnings`` 非空 → 降级。**不看 ``result``**：服务端可能同时给出
       ``result="success"`` 与一串 ``degrade_code=…`` 警告，这时以警告为准。
    3. ``result`` 是除 ``success`` 之外的任何取值（``partial_success``、以及
       将来可能新增的任何未登记取值）→ 降级。**不枚举白名单**：一个我们没见过
       的取值最可能的含义是"出了点什么事"，倒向"多说一句格式可能已简化"，
       不能因为不认识就当成干净成功。
    4. ``result`` 键不存在 → 不降级。探针二实测：干净成功时 ``data`` 只有
       ``document`` 一个键，"键不存在"就是零告警，不是"这个版本不返回"。

    **这个函数不是"全部降级都会被发现"的保证**：探针实测原始 HTML 块被静默
    丢弃且既不产生 ``warnings``、``result`` 也仍是 ``success``——那一类丢弃在
    响应里没有任何痕迹，本函数看不见（模块文档「坑二」末段如实登记）。
    """
    result = data.get("result")
    if isinstance(result, str) and result.strip().lower() == _RESULT_FAILED:
        raise FeishuDocxDeliveryError(DOCS_AI_RESULT_FAILED, definite=True)
    warnings = data.get("warnings")
    if isinstance(warnings, (list, tuple)) and any(warning for warning in warnings):
        return SERVER_SIMPLIFIED_BODY
    if result is None:
        return None
    if isinstance(result, str) and result.strip().lower() == _RESULT_SUCCESS:
        return None
    return SERVER_SIMPLIFIED_BODY


class Transport(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any] | None = ...,
        token: str | None = ...,
    ) -> Any: ...


def urllib_transport(
    method: str, url: str, *, body: Mapping[str, Any] | None = None, token: str | None = None
) -> Any:
    """默认传输层：只发 HTTPS，**不重试**任何请求（同
    :func:`lingxi.adapters.feishu_tenant_token.urllib_transport` 的姿态：飞书
    调用失败按已知分类抛出，交由调用方决定要不要重试）。

    **HTTP 5xx 一律不解析响应体，直接判"结果不明"（``definite=False``）**
    ——Trace #544 S-7c 实测坐实的一条硬规则（模块文档「坑一」）：一次建档超时
    返回的是 HTTP 504 ＋ 响应体 ``{"code": 2200, "msg": "Gateway timeout…"}``，
    而**服务端其实已经把整篇文档建出来了**。如果照常解析响应体，那个非 0 的
    ``code`` 会让 :meth:`LarkDocxDelivery._data` 判成"飞书明确拒绝"
    （``definite=True``），上层据此落 ``failed``——把一次"可能已经建好文档"的
    调用记成确定性失败，与真实世界相反。5xx 是服务端/网关侧的故障或超时，**永远
    不证明请求没有生效**，因此响应体里那个业务码在这里不具备判别力，不读。
    4xx 与 2xx 仍然照常解析：飞书的业务错误码走这两类状态码返回。
    """
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:  # 地址来自受控配置且已校验 https
            return json.loads(response.read())
    except HTTPError as error:
        if error.code >= 500:
            # 见本函数文档字符串：5xx 不解析响应体，结果不明。
            raise FeishuDocxDeliveryError(f"http_{error.code}", definite=False) from error
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
        # 止损闸（Issue #408 起就有的 `LINGXI_DOCX_MARKDOWN_CONVERT`，Trace
        # #544 S-7c 改变了它的含义、保留了它本身）：默认 False（构造函数自身的
        # 默认值＝零行为变化；真正生效的值由装配层显式传入）。不是本类自己读
        # 环境变量（adapters/ 不直接读 os.environ），由装配层把解析好的布尔值
        # 传进来——见 :attr:`markdown_convert_enabled` 与模块文档对应小节。
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
        的情况下被上层误判为成功。
        """
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
            {
                "block_type": _TEXT_PARAGRAPH_BLOCK_TYPE,
                "text": {"elements": [{"text_run": {"content": text}}]},
            }
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

    @property
    def markdown_convert_enabled(self) -> bool:
        """这套部署要不要走「服务端一次建档写全文」这条路（模块文档「开关保留
        为止损闸」一节）。

        **只读、不带副作用**，供调用方在决定走哪条路之前问一句。刻意做成属性
        而不是让 :meth:`create_document_with_markdown` 在开关关闭时抛一个原因码
        ——开关关闭时走段落路径**不是降级**（那是这套部署本来就要求的排版），
        用降级机制去表达它会让调用方把一次正常交付告知成"格式已简化"。
        """
        return self._markdown_convert_enabled

    def create_document_with_markdown(self, title: str, markdown: str) -> CreatedDocument:
        """**一次调用**建档并写完整篇正文，返回 :class:`CreatedDocument`
        （Trace #544 S-7c，见模块文档「服务端一次建档写全文」一节）。

        ``POST /open-apis/docs_ai/v1/documents``，请求体 ``{"format":
        "markdown", "content": "<title>标题</title>\\n\\n正文"}``。不传
        ``parent_token``：探针三实测不传时文档落在应用云空间的同一个目录，与
        历史真实交付文档并列，所有权仍归机器人——落点、授权、链接形态与老路径
        逐字相同，因此本模块不引入一个需要额外配置的目录标识。

        **发出请求之前的两道守卫**（都抛
        :data:`PRE_FLIGHT_DEGRADE_REASONS` 里的原因码，``definite=True``）：

        1. 正文长度超过 :data:`MAX_MARKDOWN_CHARS` → :data:`BODY_TOO_LONG`。
           不拿超长正文去撞 504（模块文档「坑一」）：那次超时**结果不明且不可
           回读**，而这里失败关闭时**一个请求都还没发出去**，调用方改走两步
           段落路径是安全的。
        2. 标题含 ``<`` 或 ``>`` → :data:`TITLE_NOT_EMBEDDABLE`。标题是拼在
           正文最前面的一个标签、不是独立字段，尖括号会破坏标签边界（典型后果：
           标题被提前截断、剩下的半截标题混进正文）。**不做静默转义或剥离**
           ——那正是 2026-08-29 裁定停止的那类"替用户改写内容"；改走两步路径
           时标题走的是 JSON 字段，没有这个问题，用户拿到的标题逐字完整。

        这两个码是调用方**唯一**允许捕获并改路的两个码；其余一切（业务错误码、
        :data:`DOCS_AI_RESULT_FAILED`、结果不明、``LookupError``）都必须原样
        向上抛。捕获范围必须窄到这两个码，且必须只包住本方法：泛化成"捕获所有
        异常都改走段落路径"会把真实故障（限流、权限缺失、超时）吞成"交付成功"；
        而**超时之后改走段落路径会真的建出第二篇文档**——第一篇很可能已经建好
        且带着全文，只是我们拿不到它的 id。

        **绝不重试**：本方法不重试、默认传输层不重试、结果不明由调用方按
        ``V-交付-03`` 落 ``uncertain``（不自动重发，转人工核对）。

        返回值里的 ``degraded_reason`` 由 :func:`_degraded_reason` 按服务端自陈
        判定（``result`` ＋ ``warnings`` 一起看，拿不准倒向多说一句），调用方
        必须接住并如实告知用户——丢掉它等于静默降级。
        """
        text = (title or "").strip()
        if not text:
            raise ValueError("文档标题不能为空")
        body_text = markdown if isinstance(markdown, str) else ""
        if not body_text.strip():
            raise ValueError("文档正文不能为空")
        if _TITLE_OPEN_TAG[0] in text or _TITLE_CLOSE_TAG[-1] in text:
            raise FeishuDocxDeliveryError(TITLE_NOT_EMBEDDABLE, definite=True)
        content = _build_markdown_content(text, body_text)
        if len(content) > MAX_MARKDOWN_CHARS:
            raise FeishuDocxDeliveryError(BODY_TOO_LONG, definite=True)

        data = self._data(
            self._call(
                "POST",
                _DOCS_AI_DOCUMENTS_PATH,
                body={"format": _MARKDOWN_FORMAT, "content": content},
            )
        )
        degraded_reason = _degraded_reason(data)
        document = data.get("document")
        if not isinstance(document, Mapping):
            raise LookupError("一次建档响应缺少 document 字段：结果不明，不能确定文档是否已建好")
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise LookupError("一次建档响应缺少可回读标识 document_id：结果不明")
        logger.info(
            "飞书 docx 文档已一次建档并写入正文 document_id_len=%s content_len=%s degraded_reason=%s",
            len(document_id),
            len(content),
            degraded_reason,
        )
        return CreatedDocument(document_id=document_id, degraded_reason=degraded_reason)

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
                body={
                    "member_type": OPENID_MEMBER_TYPE,
                    "member_id": member_id,
                    "perm": FULL_ACCESS_PERM,
                },
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
            self._call(
                "GET",
                f"/drive/v1/permissions/{doc_id}/members",
                params={"type": DOCX_PERMISSION_TYPE},
            )
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
    "BODY_TOO_LONG",
    "DOCS_AI_RESULT_FAILED",
    "DOCX_PERMISSION_TYPE",
    "FULL_ACCESS_PERM",
    "MAX_MARKDOWN_CHARS",
    "OPENID_MEMBER_TYPE",
    "PRE_FLIGHT_DEGRADE_REASONS",
    "SERVER_SIMPLIFIED_BODY",
    "TITLE_NOT_EMBEDDABLE",
    "USER_OPEN_ID_PREFIX",
    "CreatedDocument",
    "FeishuDocxDeliveryError",
    "LarkDocxDelivery",
    "REQUEST_TIMEOUT_SECONDS",
    "Transport",
    "urllib_transport",
]

# 说明：`create_document_with_markdown`/`read_body_children` 都是
# `LarkDocxDelivery` 的实例方法，不单独导出符号——同
# `create_document`/`write_paragraphs`/`grant_full_access`/`read_members`
# 既有方法一样，只通过类本身暴露。
