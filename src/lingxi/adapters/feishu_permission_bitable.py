"""当前权限多维表格的写入与逐字段读回（正式壳，Issue #156 / S-C-01）。

实现 :class:`lingxi.core.permission.publish.PermissionTableTransport`：查找、新建、
更新、按记录读回四个动作，全部走飞书多维表格 v1 的记录接口。判定（该更新还是该新建、
读回一致不一致、算明确失败还是结果不明）都不在这里，在
:mod:`lingxi.core.permission.publish`；本模块只负责把协议细节做对，并在边界上把飞书的
错误码翻译成本仓库的语义。

**真实调用未验证（证据等级 L1）**，与 ``adapters/feishu_roster_bitable.py`` 同一姿态：
下面全部断言跑在注入的假传输层上。构造时**不 import SDK、不建 client、不发请求、不读
凭据文件**；``access_token`` 由调用方以「已就绪短期令牌」的可调用对象注入，本类既不知道
令牌从哪来，也没有任何读取凭据的路径。令牌只走 ``Authorization`` 请求头（由传输层负责），
**不进 URL、不进日志、不进异常消息**；Base 与表标识同样不回显。

## G-BIT（2026-08-17 回源实测）覆盖了哪几条路径

编排者在正式表所在 Base 上完成了六步写链路验证（建隔离测试表 → 写行 → 逐字段读回 →
删行 → 删表 → 回读清零）。因此下面三条**读/建/读回**路径的形态有实测支撑：

- 分页列举记录（``GET .../records``）；
- 新建记录（``POST .../records``）；
- 按记录标识读回（``GET .../records/{record_id}``）。

**``PUT .../records/{record_id}``（字段级更新）不在 G-BIT 覆盖范围内**（Gate 评论明确
把「字段级 update、并发写、批量写、写入冲突/幂等语义」列为本 Story 自带的验证点）。
它的请求形态按官方文档编写，真实行为留给受控窗口的 L4a——这是本模块唯一一条未被实测
触碰过的外部路径，PR 正文按未验证项登记。

## 为什么查找是「整表分页」而不是 search 接口

``find_rows`` 要回答两个问题：该更新哪一行、这个人是不是已经以另一种 ``record_key``
口径存在（见 ``core/permission/publish`` 的 CONFLICT 分支）。用 search 接口能少读几页，
但那条路径 G-BIT 没有验证过，而整表分页是已经实测成立的读路径。正式表当前 26 行，整表
读一遍的代价可以忽略。**行数增长到几千时应当改用 search 接口，改之前需要一次回源验证**
——这是本模块已知的伸缩上限，不是可以顺手改掉的实现细节。

## 只写自己的字段

``update_row`` 只把调用方给的字段放进请求体。飞书的记录更新是**部分更新**：没出现在
``fields`` 里的列保持原值。这正是**更新既有行时** ``token_cipher`` 不会被清空的机制
所在——更新集里从来没有那一列（见 ``core/permission/publish_row.PUBLISHED_FIELD_NAMES``）。

**新建行则必须带上它**（``CREATED_FIELD_NAMES``，2026-08-17 裁定）：没有令牌的新行对
问数 MCP 毫无意义。本模块对此**没有任何分支**——写哪些字段完全由
``core/permission/publish`` 决定并逐字传下来，传输层不增删、不改写、不补齐。这条边界
是刻意的：一旦传输层开始"帮忙补字段"，"更新时不碰某一列"就不再是可以在纯单测里证伪的
判定，而变成一个要读两个模块才能确认的行为。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable
from urllib.parse import quote, urlencode

from lingxi.adapters.feishu_directory import FeishuDirectoryError, urllib_transport
from lingxi.core.permission.publish import ExistingPermissionRow, PermissionTableError
from lingxi.core.permission.publish_row import readback_text

logger = logging.getLogger(__name__)

# 单页记录数与翻页安全上界。语义与 ``feishu_roster_bitable`` 的同名常量一致：
# 上界是**防御**而不是容量规划，撞上它是错误而不是"读完了"。
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 500
DEFAULT_MAX_PAGES = 50


def _require_https(base_url: object) -> str:
    """飞书出站必须 HTTPS；误配 http:// 会把 Bearer token 明文上路。不回显收到的值。"""

    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ValueError("飞书 base_url 必须以 https:// 开头（不回显收到的值）")
    return base_url.rstrip("/")


def _require_identifier(value: object, label: str) -> str:
    """校验 Base / 表 / 记录标识非空且不含空白。**不回显值**：它们是外部标识，一旦进
    错误消息就会被日志、CI 输出和工单一路复制出去。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须由配置注入，不得为空（不回显收到的值）")
    text = value.strip()
    if any(character.isspace() for character in text):
        raise ValueError(f"{label}不得包含空白字符（不回显收到的值）")
    return text


def _matches(value: Any, wanted: str) -> bool:
    """单元格值是否命中给定的键（去空白 + 大小写不敏感）。

    归一走 :func:`lingxi.core.permission.publish_row.readback_text`，与「写入后逐字段
    读回比对」**同一把尺子**（二级独立审查 P3-2）。此前这里是 ``str(value)``：多维表格
    的文本列在分段形态下会回来一个 ``[{"text": "a"}, {"text": "b"}]``，``str()`` 得到的
    是 Python 的 repr，永远匹配不上——而漏命中的后果不是查不到，是**判成"这个人还没有
    行"于是新建第二行权限**。两处归一分叉早晚会让其中一处出错，因此只保留一份实现。

    宽松的方向是**安全**的：多命中一行只会让发布走进 CONFLICT 失败关闭，而漏命中会让
    同一个人被写出第二行权限。
    """

    return bool(wanted) and readback_text(value).strip().casefold() == wanted.strip().casefold()


class BitablePermissionTable:
    """当前权限多维表格的写读回传输。构造只存参数，不做任何 I/O。"""

    def __init__(
        self,
        *,
        base_url: str,
        app_token: str,
        table_id: str,
        access_token: Callable[[], str],
        transport: Callable[..., Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self._base_url = _require_https(base_url)
        self._app_token = _require_identifier(app_token, "权限发布 Base app_token")
        self._table_id = _require_identifier(table_id, "权限发布表 table_id")
        if not callable(access_token):
            raise ValueError("access_token 必须是返回已就绪短期令牌的可调用对象")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size 必须是 1 到 {MAX_PAGE_SIZE} 之间的整数")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages 必须是正整数")
        self._access_token = access_token
        self._transport: Callable[..., Any] = transport or urllib_transport
        self._page_size = page_size
        self._max_pages = max_pages

    # ------------------------------------------------------------------
    # 内部：URL、令牌、响应解析
    # ------------------------------------------------------------------

    @property
    def _records_path(self) -> str:
        return (
            f"/bitable/v1/apps/{quote(self._app_token, safe='')}"
            f"/tables/{quote(self._table_id, safe='')}/records"
        )

    def _list_url(self, page_token: str | None) -> str:
        parameters: dict[str, Any] = {"page_size": self._page_size}
        if page_token:
            parameters["page_token"] = page_token
        return f"{self._base_url}{self._records_path}?{urlencode(parameters)}"

    def _record_url(self, record_id: str) -> str:
        identifier = _require_identifier(record_id, "权限发布表记录标识")
        return f"{self._base_url}{self._records_path}/{quote(identifier, safe='')}"

    def _token(self) -> str:
        """取已就绪令牌。

        **provider 自己抛出的异常原样上抛，不折成 :class:`PermissionTableError`**：
        拿不到凭据是本侧的配置 / 授权问题，不是"发布表异常"。把两者混在一起，会让告警
        指向错误的地方（去查多维表格，而问题在凭据）。只有"provider 正常返回却给了空值"
        才算调用失败——那时确实无法判断外部状态。
        """

        token = self._access_token()
        if not isinstance(token, str) or not token:
            raise PermissionTableError("access_token_missing", definite=False)
        return token

    def _call(self, method: str, url: str, *, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """发一次调用并把响应解析成 ``data``；错误码翻译在这里，只有这里。"""

        token = self._token()
        try:
            response = self._transport(method, url, body=body, token=token)
        except FeishuDirectoryError as error:
            # 共用同一份 urllib 传输（全仓库只有一份），但它的错误类型属于目录 adapter；
            # 在边界上翻译成发布通道自己的错误类型，``definite`` 语义原样保留。
            raise PermissionTableError(error.code, definite=error.definite) from error

        if not isinstance(response, Mapping):
            raise PermissionTableError("invalid_response_shape", definite=False)
        code = response.get("code")
        if code not in (None, 0, "0"):
            # 服务端完整返回并明确拒绝：明确失败（白名单口径，同 feishu_delivery）。
            raise PermissionTableError(f"feishu_code_{code}")
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise PermissionTableError("invalid_response_shape", definite=False)
        return dict(data)

    @staticmethod
    def _record(data: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """从 ``data.record`` 取出记录标识与字段。

        缺可回读标识属**结果不明**而不是明确失败：响应本身说成功，但我们无法确认写进去
        的是哪一行，也就无法读回核对（纪律同 ``feishu_delivery`` 对缺 ``card_id`` 的处理）。
        """

        record = data.get("record")
        if not isinstance(record, Mapping):
            raise PermissionTableError("record_missing", definite=False)
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise PermissionTableError("record_id_missing", definite=False)
        fields = record.get("fields")
        return record_id, dict(fields) if isinstance(fields, Mapping) else {}

    # ------------------------------------------------------------------
    # 传输面
    # ------------------------------------------------------------------

    def find_rows(self, *, record_key: str, email: str) -> tuple[ExistingPermissionRow, ...]:
        """整表分页查找 ``record_key`` 或 ``email`` 命中的行（理由见模块文档）。"""

        matched: list[ExistingPermissionRow] = []
        page_token: str | None = None
        for _ in range(self._max_pages):
            data = self._call("GET", self._list_url(page_token), body=None)
            items = data.get("items")
            if items is None:
                # 空表在飞书的响应里可能没有 items 键；这与"响应形状不对"不同。
                items = []
            if not isinstance(items, list):
                raise PermissionTableError("items_missing", definite=False)
            for item in items:
                if not isinstance(item, Mapping):
                    # 静默丢弃非对象项会让被丢的那一行躲过冲突检查，于是同一个人被写出
                    # 第二行（``feishu_directory``/``feishu_roster_bitable`` 的同一处教训）。
                    raise PermissionTableError("invalid_page_item", definite=False)
                record_id = item.get("record_id")
                fields = item.get("fields")
                if not isinstance(record_id, str) or not record_id:
                    raise PermissionTableError("record_id_missing", definite=False)
                if not isinstance(fields, Mapping):
                    raise PermissionTableError("invalid_record_fields", definite=False)
                if _matches(fields.get("record_key"), record_key) or _matches(
                    fields.get("email"), email
                ):
                    matched.append(ExistingPermissionRow(record_id, dict(fields)))
            if data.get("has_more") is not True:
                break
            candidate = data.get("page_token")
            if not isinstance(candidate, str) or not candidate or candidate == page_token:
                # 源头说还有数据却没给可用游标：这一轮读到多少行是未知的，而"没找到"
                # 会被下游读成"可以新建"——必须失败，不能当成读完了。
                raise PermissionTableError("pagination_stalled", definite=False)
            page_token = candidate
        else:
            raise PermissionTableError("pagination_limit", definite=False)
        return tuple(matched)

    def create_row(self, fields: Mapping[str, str]) -> str:
        data = self._call("POST", f"{self._base_url}{self._records_path}", body={"fields": dict(fields)})
        record_id, _ = self._record(data)
        logger.info("权限发布行已新建 record=%s", record_id)
        return record_id

    def update_row(self, record_id: str, fields: Mapping[str, str]) -> None:
        """部分更新：只改给定字段，未列出的列保持原值（模块文档「只写自己的字段」）。"""

        self._call("PUT", self._record_url(record_id), body={"fields": dict(fields)})
        logger.info("权限发布行已更新 record=%s", record_id)

    def read_row(self, record_id: str) -> dict[str, Any]:
        data = self._call("GET", self._record_url(record_id), body=None)
        _, fields = self._record(data)
        return fields
