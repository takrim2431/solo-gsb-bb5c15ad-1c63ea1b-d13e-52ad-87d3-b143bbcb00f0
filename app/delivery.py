"""投递状态报告（DSN, RFC 3464）解析（``GET /messages/{id}/delivery-report``）。

在**已归档 MIME 树**上识别
``Content-Type: multipart/report; report-type="delivery-status"``
（RFC 6522）作用域，解析其**直接** ``message/delivery-status`` 子件。
本模块是**纯只读**的确定性解析：输入为 :func:`database.list_nodes`
返回的节点行与附件库中已保存的状态部分解码字节，不回写任何记录，
对历史归档数据同样适用。

``message/delivery-status``（RFC 3464）正文由若干**字段块**组成，
块间以空行分隔：

* 第 1 块是**每邮件块**（per-message）：``Reporting-MTA``、
  ``Original-Envelope-Id``、``Arrival-Date`` 等，本模块从中取
  ``Original-Envelope-Id`` 作为信封标识；
* 其余每块是一个**逐收件人块**（per-recipient）：必须含
  ``Final-Recipient``、``Action``、``Status``，并保留
  ``Diagnostic-Code``、``Remote-MTA``、``Last-Attempt-Date``。

兼容性：

* 换行兼容 CRLF / LF / 裸 CR；以 WSP 开头的物理行按 RFC 5322
  折叠规则并入上一逻辑行（折叠点替换为一个空格）；
* ``Final-Recipient`` / ``Action`` / ``Status`` 的字段名大小写
  不敏感；``Action`` 规范化为小写；``Status`` 必须是
  RFC 3463 增强状态码（三段、每段 1..3 位数字）；
* ``Final-Recipient`` 形如 ``addr-type; address``，地址类型
  规范化为小写，地址去首尾空白后原样保留（邮箱本地部分可能区分
  大小写）。

结构非法（同一作用域出现多个状态子件、无状态子件、字段块/字段
语法损坏、逐收件人块缺必填字段或字段值非法）一律抛出带
``node_path`` 的 :class:`~app.errors.MIMEParseError`（HTTP 422）：
作用域级问题定位到 ``multipart/report`` 节点，正文级问题定位到
``message/delivery-status`` 节点。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import MIMEParseError
from .mimeparser import parse_content_type
from .related import json_loads_headers

# RFC 3464 §2.3 的合法 Action（字段值大小写不敏感，输出规范化小写）。
_ACTIONS = frozenset({"failed", "delayed", "delivered", "relayed", "expanded"})

# RFC 3463 增强状态码：class . subject . detail，每段 1..3 位数字。
_STATUS_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")

# 稳定的错误码
CODE_MULTIPLE_STATUS_PARTS = "MULTIPLE_DELIVERY_STATUS"
CODE_STATUS_PART_NOT_FOUND = "DELIVERY_STATUS_NOT_FOUND"
CODE_STATUS_PART_UNREADABLE = "DELIVERY_STATUS_UNREADABLE"
CODE_INVALID_DELIVERY_STATUS = "INVALID_DELIVERY_STATUS"
CODE_MISSING_DELIVERY_FIELD = "MISSING_DELIVERY_FIELD"
CODE_INVALID_DELIVERY_FIELD = "INVALID_DELIVERY_FIELD"


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryRecipient:
    """逐收件人块的标准化结果。"""

    final_recipient_type: str  # 小写地址类型，如 "rfc822"
    final_recipient_address: str  # 去首尾空白后的地址（保留原始大小写）
    action: str  # failed | delayed | delivered | relayed | expanded（小写）
    status: str  # RFC 3463 三段数字状态码
    diagnostic_code: Optional[str] = None  # 原始（去折叠、去首尾空白）值
    remote_mta: Optional[str] = None
    last_attempt_date: Optional[str] = None


@dataclass(frozen=True)
class DeliveryReport:
    """一个 multipart/report; report-type=delivery-status 作用域的结果。"""

    scope_node_path: str  # multipart/report 容器（作用域）节点路径
    report_node_path: str  # 直接 message/delivery-status 子件节点路径
    envelope_id: Optional[str]  # Original-Envelope-Id 原始值
    recipients: list[DeliveryRecipient] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _path_key(path: str) -> tuple[int, ...]:
    """物化路径的文档序键：``""`` → ()，``"1/2"`` → (1, 2)。"""

    if not path:
        return ()
    return tuple(int(seg) for seg in path.split("/"))


def _content_type_params(row: sqlite3.Row) -> tuple[str, dict[str, str]]:
    """重新解析已归档节点的 Content-Type 头，返回 (小写媒体类型, 参数)。"""

    for name, value in json_loads_headers(row["headers_json"]):
        if name.lower() == "content-type":
            ct = parse_content_type(value.encode("utf-8"), row["path"])
            return ct.media_type, ct.params
    # 归档解析器保证非根节点都有 Content-Type 头；缺省时按 RFC 2045
    # 为 text/plain（归档树根可能无该头）。
    return "text/plain", {}


def _is_delivery_report_scope(row: sqlite3.Row) -> bool:
    """节点是否为 ``multipart/report; report-type=delivery-status``。"""

    if row["content_type"].lower() != "multipart/report":
        return False
    _media, params = _content_type_params(row)
    return (params.get("report-type") or "").strip().lower() == "delivery-status"


# ---------------------------------------------------------------------------
# message/delivery-status 正文解析
# ---------------------------------------------------------------------------


def _decode_status_body(row: sqlite3.Row, blob: bytes) -> str:
    """按状态部分声明的字符集解码（缺省 us-ascii，RFC 3464 本为 7bit）。"""

    _media, params = _content_type_params(row)
    charset = (params.get("charset") or "us-ascii").strip()
    try:
        # 归档时已完成 CTE 解码；这里容忍个别非声明字符集字节（常见于
        # 老式 MTA 在 Diagnostic-Code 里夹带的本地编码），替换而非拒收，
        # 必填字段的数字/枚举校验不受影响。
        return blob.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError) as exc:
        raise MIMEParseError(
            CODE_STATUS_PART_UNREADABLE,
            f"message/delivery-status 无法按 {charset!r} 解码: {exc}",
            row["path"],
            {"charset": charset},
        ) from None


def _logical_lines(text: str, node_path: str) -> list[str]:
    """统一换行并做 RFC 5322 折叠还原，空行以 ``""`` 标记。"""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    logical: list[str] = []
    for line in text.split("\n"):
        if line == "":
            logical.append("")  # 块分隔符
            continue
        if line[:1] in (" ", "\t"):
            if not logical or logical[-1] == "":
                raise MIMEParseError(
                    CODE_INVALID_DELIVERY_STATUS,
                    "message/delivery-status 块首出现折叠续行（WSP 开头），"
                    "无可并入的上一逻辑行",
                    node_path,
                    {"line": line[:80]},
                )
            # 折叠点替换为一个空格，续行其余线性空白压掉。
            logical[-1] = logical[-1].rstrip(" \t") + " " + line.strip(" \t")
        else:
            logical.append(line)
    return logical


def _split_blocks(logical: list[str], node_path: str) -> list[list[str]]:
    """按一个或多个空行切出字段块，忽略首尾空块。"""

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in logical:
        if line == "":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    if not blocks:
        raise MIMEParseError(
            CODE_INVALID_DELIVERY_STATUS,
            "message/delivery-status 正文为空：至少需要每邮件块与一个"
            "逐收件人块",
            node_path,
        )
    return blocks


def _parse_block_fields(
    block: list[str], node_path: str
) -> dict[str, str]:
    """把一个字段块解析为 ``{小写字段名: 去首尾空白值}``。

    字段语法损坏（无冒号/空字段名）或同一块内字段重复都按结构错误处理。
    """

    fields: dict[str, str] = {}
    for line in block:
        colon = line.find(":")
        if colon <= 0:
            raise MIMEParseError(
                CODE_INVALID_DELIVERY_STATUS,
                "message/delivery-status 字段缺少 ':' 或字段名为空",
                node_path,
                {"line": line[:80]},
            )
        name = line[:colon].strip(" \t").lower()
        if not name or any(ch.isspace() for ch in name):
            raise MIMEParseError(
                CODE_INVALID_DELIVERY_STATUS,
                f"message/delivery-status 字段名非法: {name!r}",
                node_path,
                {"field": name},
            )
        value = line[colon + 1:].strip(" \t")
        if name in fields:
            raise MIMEParseError(
                CODE_INVALID_DELIVERY_STATUS,
                f"message/delivery-status 同一字段块内 {name!r} 重复出现",
                node_path,
                {"field": name},
            )
        fields[name] = value
    return fields


def _require_field(fields: dict[str, str], name: str, node_path: str) -> str:
    value = fields.get(name)
    if value is None or not value:
        raise MIMEParseError(
            CODE_MISSING_DELIVERY_FIELD,
            f"逐收件人块缺少必填字段 {name!r}",
            node_path,
            {"field": name},
        )
    return value


def _parse_recipient(
    fields: dict[str, str], node_path: str
) -> DeliveryRecipient:
    raw_recipient = _require_field(fields, "final-recipient", node_path)
    addr_type, sep, address = raw_recipient.partition(";")
    addr_type = addr_type.strip(" \t").lower()
    address = address.strip(" \t")
    if not sep or not addr_type or not address:
        raise MIMEParseError(
            CODE_INVALID_DELIVERY_FIELD,
            "Final-Recipient 必须形如 'addr-type; address'，且地址类型与"
            "地址均不能为空",
            node_path,
            {"field": "final-recipient", "value": raw_recipient},
        )

    action_raw = _require_field(fields, "action", node_path).lower()
    if action_raw not in _ACTIONS:
        raise MIMEParseError(
            CODE_INVALID_DELIVERY_FIELD,
            f"Action 值非法: {action_raw!r}，应为 "
            "failed/delayed/delivered/relayed/expanded 之一",
            node_path,
            {"field": "action", "value": action_raw},
        )

    status = _require_field(fields, "status", node_path)
    if not _STATUS_RE.match(status):
        raise MIMEParseError(
            CODE_INVALID_DELIVERY_FIELD,
            f"Status 值非法: {status!r}，应为 RFC 3463 增强状态码"
            "（三段、每段 1..3 位数字，如 5.1.1）",
            node_path,
            {"field": "status", "value": status},
        )

    def optional(name: str) -> Optional[str]:
        value = fields.get(name)
        return value if value else None

    return DeliveryRecipient(
        final_recipient_type=addr_type,
        final_recipient_address=address,
        action=action_raw,
        status=status,
        diagnostic_code=optional("diagnostic-code"),
        remote_mta=optional("remote-mta"),
        last_attempt_date=optional("last-attempt-date"),
    )


def _parse_status_part(
    row: sqlite3.Row, blob: bytes
) -> tuple[Optional[str], list[DeliveryRecipient]]:
    """解析单个 message/delivery-status 节点的解码字节。"""

    node_path = row["path"]
    text = _decode_status_body(row, blob)
    blocks = _split_blocks(_logical_lines(text, node_path), node_path)

    message_fields = _parse_block_fields(blocks[0], node_path)
    envelope_raw = message_fields.get("original-envelope-id")
    envelope_id = envelope_raw if envelope_raw else None

    if len(blocks) < 2:
        raise MIMEParseError(
            CODE_INVALID_DELIVERY_STATUS,
            "message/delivery-status 缺少逐收件人块：每邮件块之后至少"
            "需要一个逐收件人块",
            node_path,
        )

    recipients: list[DeliveryRecipient] = []
    for block in blocks[1:]:
        fields = _parse_block_fields(block, node_path)
        recipients.append(_parse_recipient(fields, node_path))
    return envelope_id, recipients


# ---------------------------------------------------------------------------
# 作用域识别与入口
# ---------------------------------------------------------------------------


def _status_child_paths(
    scope: sqlite3.Row, by_path: dict[str, sqlite3.Row]
) -> list[str]:
    """作用域的直接子件中所有 message/delivery-status 节点路径（文档序）。"""

    found: list[str] = []
    for child_path in json.loads(scope["children_json"]):
        child = by_path.get(child_path)
        if child is not None and child["content_type"].lower() == (
            "message/delivery-status"
        ):
            found.append(child_path)
    return found


def extract_delivery_reports(
    rows: list[sqlite3.Row],
    *,
    load_blob: Callable[[str], Optional[bytes]],
) -> list[DeliveryReport]:
    """在一封已归档邮件的节点行上提取全部 DSN 报告（按 MIME 文档序）。

    ``rows`` 为 :func:`database.list_nodes` 的结果（行序无关，内部按
    物化路径排文档序；包含 ``message/rfc822`` 封装报文内部的节点）。
    ``load_blob`` 按附件 SHA-256 取回归档时保存的解码字节，缺失返回
    ``None``。任何结构/必填字段错误都抛出带 ``node_path`` 的
    :class:`MIMEParseError`。
    """

    ordered = sorted(rows, key=lambda r: _path_key(r["path"]))
    by_path = {r["path"]: r for r in rows}

    reports: list[DeliveryReport] = []
    for scope in ordered:
        if not _is_delivery_report_scope(scope):
            continue

        status_paths = _status_child_paths(scope, by_path)
        if len(status_paths) > 1:
            raise MIMEParseError(
                CODE_MULTIPLE_STATUS_PARTS,
                "multipart/report; report-type=delivery-status 作用域的直接"
                "子件中出现多个 message/delivery-status（RFC 3464 要求"
                "恰好一个）",
                scope["path"],
                {"candidates": status_paths, "count": len(status_paths)},
            )
        if not status_paths:
            raise MIMEParseError(
                CODE_STATUS_PART_NOT_FOUND,
                "multipart/report; report-type=delivery-status 作用域缺少"
                "直接 message/delivery-status 子件",
                scope["path"],
            )

        status_row = by_path[status_paths[0]]
        blob_sha = status_row["blob_sha256"]
        blob = load_blob(blob_sha) if blob_sha else None
        if blob is None:
            raise MIMEParseError(
                CODE_STATUS_PART_UNREADABLE,
                "message/delivery-status 节点的解码字节不在附件库中，无法"
                "解析（可能归档于附件库升级之前）",
                status_row["path"],
                {"blob_sha256": blob_sha},
            )

        envelope_id, recipients = _parse_status_part(status_row, blob)
        reports.append(
            DeliveryReport(
                scope_node_path=scope["path"],
                report_node_path=status_row["path"],
                envelope_id=envelope_id,
                recipients=recipients,
            )
        )

    return reports
