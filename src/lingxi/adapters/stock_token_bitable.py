"""存量令牌只读源的飞书 bitable 适配器（Issue #281 载体，Trace #304 批次 3）。

实现 :class:`lingxi.core.identity.stock_token_source.StockTokenSource`：按用户邮箱精确查
正式权限多维表格 ``user_company_permissions`` 的一行，翻成四态之一交给开通链（`core/
identity/onboarding_runner.py` 的 ``_issue_token``）。**全程只读**——本模块没有任何写
方法，也不修改正式表的任何字段（不做批量预登记、不做轮换/覆写，边界见 #281 改道裁定）。

## 两层，一个文件

- :class:`BitableStockTokenSource`：**纯 I/O 层**，只读字段、不碰密钥。构造与
  ``adapters/feishu_permission_bitable.BitablePermissionTable`` 同姿态（不 import SDK、
  不建 client、不发请求；``access_token`` 由调用方以"已就绪短期令牌"注入），查找同样是
  **整表分页**而不是 search 接口（同一份 G-BIT 2026-08-17 回源实测覆盖的读路径；理由见
  ``feishu_permission_bitable`` 模块文档「为什么查找是『整表分页』」，本模块不复述）。
  ``lookup_raw`` 只返回「查无此行 / 有行无密文 / 有行有密文」三种原始事实
  （:class:`RawStockTokenRow` 或 ``None``），**不尝试解密**——core 不 import 加解密适配器，
  三态读端口因此必须是一个不需要主密钥就能独立测试的纯读取动作。
- :class:`DecryptingStockTokenSource`：**组合层**，包一个 :class:`BitableStockTokenSource`
  与一个 ``McpTokenCipher``，把三态原始事实翻成 core 端口要的四态
  :class:`~lingxi.core.identity.stock_token_source.StockTokenLookup`
  （``NO_ROW``/``NO_CIPHER`` 原样透传；``有密文`` 尝试解密，成功→``ADOPTABLE`` 且带上
  明文，失败→``DECRYPT_FAILED``，**不向上抛异常**——解密失败是本端口要表达的一个合法
  状态，不是"读取失败"）。这是唯一对 ``core`` 暴露的实现：装配层
  （``apps/scheduler/onboarding.build_stock_token_source``）只构造这一个类。

## 只读需要的三个字段

正式表 7 个字段全部是单行文本（``core/permission/publish_row.py`` 模块文档「发布表的
通道事实」），本模块只取其中三个：``token_cipher``（有没有密文、密文本身）、
``status``（供调用方审计标注"是否非 approved"，不参与本模块的判定）与
``permissions``（rc25 S-1，Issue #540：存量用户首聊时把「旧行权限 − 银河当前翻译」
落成本地授权的唯一输入，只在有密文可采纳时才向上透传）。**不读
``record_key``/``name``/``updated_at``**——匹配只需要 ``email``，其余字段一次都不进
本模块的返回值，减少可识别数据的暴露面（同 ``feishu_roster_bitable`` 模块文档
「数据范围没有因为本次新增而扩大」的同一条纪律）。

## 多行命中：失败关闭，不猜

正式表理论上不该有重复邮箱（``record_key``/``email`` 是发布链的 upsert 键），但本模块
不假设这件事永远成立——命中不止一行时 ``lookup_raw`` 抛 :class:`StockTokenSourceError`
（``code="multiple_rows_matched"``），交给调用方按"本侧故障"收口，绝不挑一行返回。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, NamedTuple
from urllib.parse import quote, urlencode

from lingxi.adapters.feishu_directory import FeishuDirectoryError, urllib_transport
from lingxi.adapters.mcp_token_cipher import McpTokenCipher, McpTokenCipherError
from lingxi.core.identity.stock_token_source import (
    ADOPTABLE,
    DECRYPT_FAILED,
    NO_CIPHER,
    NO_ROW,
    StockTokenLookup,
)
from lingxi.core.permission.account_match import normalize_email
from lingxi.core.permission.publish_row import readback_text

logger = logging.getLogger(__name__)

# 同 ``feishu_permission_bitable``/``feishu_roster_bitable``：上界是防御，不是容量规划。
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 500
DEFAULT_MAX_PAGES = 50


class StockTokenSourceError(RuntimeError):
    """存量令牌源只读查询失败。``code`` 供程序判断，消息里不含邮箱、密文或 Base 标识。"""

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"存量令牌源查询失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


class RawStockTokenRow(NamedTuple):
    """正式表命中的一行，只保留本模块要用的三列（模块文档「只读需要的三个字段」）。"""

    token_cipher: str
    status: str
    permissions: str = ""


def _require_https(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ValueError("飞书 base_url 必须以 https:// 开头（不回显收到的值）")
    return base_url.rstrip("/")


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须由配置注入，不得为空（不回显收到的值）")
    text = value.strip()
    if any(character.isspace() for character in text):
        raise ValueError(f"{label}不得包含空白字符（不回显收到的值）")
    return text


class BitableStockTokenSource:
    """存量令牌正式表的只读传输。构造只存参数，不做任何 I/O（同全仓其余 bitable 适配器）。"""

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
        self._app_token = _require_identifier(app_token, "存量令牌源 Base app_token")
        self._table_id = _require_identifier(table_id, "存量令牌源表 table_id")
        if not callable(access_token):
            raise ValueError("access_token 必须是返回已就绪短期令牌的可调用对象")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE
        ):
            raise ValueError(f"page_size 必须是 1 到 {MAX_PAGE_SIZE} 之间的整数")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages 必须是正整数")
        self._access_token = access_token
        self._transport: Callable[..., Any] = transport or urllib_transport
        self._page_size = page_size
        self._max_pages = max_pages

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

    def _token(self) -> str:
        # provider 自己抛出的异常原样上抛，不折成 StockTokenSourceError：拿不到凭据是
        # 本侧配置问题，不是"存量令牌源异常"（同 feishu_permission_bitable._token）。
        token = self._access_token()
        if not isinstance(token, str) or not token:
            raise StockTokenSourceError("access_token_missing", definite=False)
        return token

    def lookup_raw(self, email: str) -> RawStockTokenRow | None:
        """按邮箱整表分页查找。零命中返回 ``None``；多命中失败关闭（模块文档）。"""

        wanted = normalize_email(email)
        if not wanted:
            raise ValueError("按邮箱查存量令牌源必须提供非空邮箱")
        matched: list[RawStockTokenRow] = []
        page_token: str | None = None
        for _ in range(self._max_pages):
            data = self._call(self._list_url(page_token))
            items = data.get("items")
            if items is None:
                items = []
            if not isinstance(items, list):
                raise StockTokenSourceError("items_missing", definite=False)
            for item in items:
                if not isinstance(item, dict):
                    # 静默丢弃非对象项会让被丢的那一行躲过多行命中检查（同
                    # feishu_permission_bitable/feishu_roster_bitable 的同一处教训）。
                    raise StockTokenSourceError("invalid_page_item", definite=False)
                fields = item.get("fields")
                if not isinstance(fields, dict):
                    raise StockTokenSourceError("invalid_record_fields", definite=False)
                if normalize_email(readback_text(fields.get("email"))) == wanted:
                    matched.append(
                        RawStockTokenRow(
                            token_cipher=readback_text(fields.get("token_cipher")).strip(),
                            status=readback_text(fields.get("status")).strip(),
                            permissions=readback_text(fields.get("permissions")).strip(),
                        )
                    )
            if data.get("has_more") is not True:
                break
            candidate = data.get("page_token")
            if not isinstance(candidate, str) or not candidate or candidate == page_token:
                raise StockTokenSourceError("pagination_stalled", definite=False)
            page_token = candidate
        else:
            raise StockTokenSourceError("pagination_limit", definite=False)
        if not matched:
            return None
        if len(matched) > 1:
            # 命中不止一行：不猜是哪一行，交给调用方按本侧故障收口（模块文档）。
            raise StockTokenSourceError("multiple_rows_matched")
        return matched[0]

    def _call(self, url: str) -> dict[str, Any]:
        token = self._token()
        try:
            response = self._transport("GET", url, body=None, token=token)
        except FeishuDirectoryError as error:
            raise StockTokenSourceError(error.code, definite=error.definite) from error
        if not isinstance(response, dict):
            raise StockTokenSourceError("invalid_response_shape", definite=False)
        code = response.get("code")
        if code not in (None, 0, "0"):
            raise StockTokenSourceError(f"feishu_code_{code}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise StockTokenSourceError("invalid_response_shape", definite=False)
        return data


class DecryptingStockTokenSource:
    """把 :class:`BitableStockTokenSource` 的三态原始事实翻成 core 端口的四态。

    实现 ``lookup(email) -> StockTokenLookup``（结构匹配
    :class:`lingxi.core.identity.stock_token_source.StockTokenSource`）。**解密失败不
    向上抛异常**——它是四态里合法的一态（:data:`~lingxi.core.identity.
    stock_token_source.DECRYPT_FAILED`），由调用方（``AutoOnboardingRunner``）负责响亮
    失败、绝不回退签新；本类只负责把"密文解不开"这件事**翻译**成状态，不吞掉、也不升级
    成本模块自己的异常。
    """

    def __init__(self, reader: BitableStockTokenSource, *, cipher: McpTokenCipher) -> None:
        if not isinstance(cipher, McpTokenCipher):
            # 同 PostgresMcpTokenStore 的同一条纪律：只接受已经校验过主密钥的对象。
            raise TypeError("存量令牌源必须注入已校验主密钥的 McpTokenCipher")
        self._reader = reader
        self._cipher = cipher

    def lookup(self, email: str) -> StockTokenLookup:
        raw = self._reader.lookup_raw(email)
        if raw is None:
            return StockTokenLookup(state=NO_ROW)
        if not raw.token_cipher:
            return StockTokenLookup(state=NO_CIPHER, status=raw.status)
        try:
            secret = self._cipher.decrypt(raw.token_cipher)
        except McpTokenCipherError:
            logger.warning("存量令牌解密失败：主密钥配错或数据损坏（不回显密文）")
            return StockTokenLookup(state=DECRYPT_FAILED, status=raw.status)
        return StockTokenLookup(
            state=ADOPTABLE, secret=secret, status=raw.status, permissions=raw.permissions
        )


__all__ = [
    "BitableStockTokenSource",
    "DecryptingStockTokenSource",
    "RawStockTokenRow",
    "StockTokenSourceError",
]
