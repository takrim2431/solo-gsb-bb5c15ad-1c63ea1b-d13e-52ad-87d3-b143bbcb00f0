"""从已归档 MIME 树选出客户端应展示的正文（``GET /messages/{id}/display-body``）。

本模块是**纯只读**的静态选择：输入为 :func:`database.list_nodes`
返回的节点行（含 ``children_json`` / ``headers_json``），不回写任何
记录、不依赖归档时未保存的派生数据，因此对历史归档数据同样适用。

递归选择规则（沿实际选中路径逐层进行）：

* ``multipart/alternative`` —— 在可展示分支中优先选择其*实际正文*
  匹配 ``prefer``（``text/html`` / ``text/plain``）的分支（文档序
  第一个命中者）；没有分支能给出偏好类型时，选择**最后一个可展示
  分支**（RFC 2046：备选按保真度从低到高排列，末位最忠实）；
* ``multipart/related`` —— 按 RFC 2387 的 ``start`` 参数（容器
  Content-Type 上的参数）匹配**直接子件**的 Content-ID；未配置
  ``start`` 时取**首个子件**（RFC 2387 的缺省根部分）；
* ``multipart/mixed`` 与其他 multipart 子类型 —— 取**首个可展示
  子件**（``multipart/signed`` 的正文在首件、``multipart/parallel``
  取任一可展示部分，同一规则均可覆盖）。

“可展示”指递归到一个 **text 叶子**（``kind == "text"``）。以下节点
及其整棵子树不作为候选：

* 自身 ``Content-Disposition: attachment`` 的节点（``inline`` 不受
  影响）——附件不是邮件正文；
* ``message/rfc822`` / ``message/global`` 容器——**封装报文的内部
  正文**不得作为外层邮件的展示正文（屏障，不向内递归）；
* 非文本（``blob``）叶子（如内嵌图片）。

``start`` 配置错误在**实际选中路径上**显式报错：

* 同一 Content-ID 命中多个直接子件 → ``AMBIGUOUS_RELATED_START``，
  ``node_path`` 定位到该 related 容器，``detail.candidates`` 给出
  全部命中子件；
* 配置了 ``start`` 但没有任何子件声明该 Content-ID →
  ``RELATED_START_NOT_FOUND``，同样定位到 related 容器。

未被选中的 alternative 分支即使内部含上述配置错误，也不会阻断其他
正常分支的展示（错误只在它成为实际路径时上抛）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal, Optional

from .errors import MIMEParseError
from .mimeparser import parse_content_type
from .related import json_loads_headers, normalize_content_id

Prefer = Literal["html", "plain"]

# prefer 查询值 → 偏好的叶子媒体类型
_PREFER_CONTENT_TYPE: dict[str, str] = {
    "html": "text/html",
    "plain": "text/plain",
}

# 选择轨迹中使用的稳定规则码
RULE_ALTERNATIVE_PREFER = "alternative-prefer"
RULE_ALTERNATIVE_LAST = "alternative-fallback-last"
RULE_RELATED_START = "related-start"
RULE_RELATED_FIRST = "related-first"
RULE_FIRST_DISPLAYABLE = "first-displayable"

_ENCAPSULATED_MESSAGE_TYPES = ("message/rfc822", "message/global")


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass
class DisplayBodyStep:
    """容器节点上的一次逐层选择（轨迹中的一项）。"""

    node_path: str  # 做出选择的容器节点
    content_type: str
    rule: str  # RULE_* 之一
    considered: list[str]  # 该层实际评估过的直接子件路径（文档序）
    candidates: list[str]  # 规则下合格的候选子件路径（文档序）
    chosen: Optional[str]  # 实际进入的子件路径
    detail: dict = field(default_factory=dict)  # prefer / start 等规则参数


@dataclass
class DisplayBodySelection:
    """选择结果：``leaf`` 为 None 表示该子树无可展示正文。"""

    leaf: Optional[sqlite3.Row]
    steps: list[DisplayBodyStep]


class _RelatedStartError(Exception):
    """选择路径上的 multipart/related ``start`` 配置错误。

    内部控制流用：:func:`select_display_body` 在边界统一转换为
    :class:`MIMEParseError`（422 响应体）。
    """

    def __init__(
        self,
        related_path: str,
        code: str,
        message: str,
        detail: dict,
    ) -> None:
        super().__init__(message)
        self.related_path = related_path
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def select_display_body(
    rows: list[sqlite3.Row],
    prefer: Prefer,
) -> DisplayBodySelection:
    """在一封已归档邮件的节点行上选出展示正文。

    ``rows`` 为 :func:`database.list_nodes` 的结果。
    成功返回 :class:`DisplayBodySelection`（``leaf`` 可能为 None，
    调用方据此回 404）；``start`` 配置错误抛 :class:`MIMEParseError`。
    """

    by_path = {r["path"]: r for r in rows}
    root = by_path[""]
    try:
        return _select(by_path, root, prefer)
    except _RelatedStartError as exc:
        raise MIMEParseError(
            exc.code,
            exc.message,
            exc.related_path,
            exc.detail,
        ) from None


# ---------------------------------------------------------------------------
# 递归选择
# ---------------------------------------------------------------------------


def _select(
    by_path: dict[str, sqlite3.Row],
    row: sqlite3.Row,
    prefer: Prefer,
) -> DisplayBodySelection:
    # 屏障 1：attachment 节点及其整棵子树都不是正文候选。
    if (row["disposition"] or "").lower() == "attachment":
        return DisplayBodySelection(None, [])

    kind = row["kind"]

    # 叶子：text 可展示；blob（图片等二进制）不是正文。
    if kind in ("text", "blob"):
        return DisplayBodySelection(row if kind == "text" else None, [])

    content_type = row["content_type"].lower()

    # 屏障 2：封装报文（message/rfc822 / message/global）——不向内
    # 递归，其内部正文不得作为外层邮件的展示正文。
    if kind == "message" or content_type in _ENCAPSULATED_MESSAGE_TYPES:
        return DisplayBodySelection(None, [])

    # 余下均为 multipart 容器
    children = _children(by_path, row)
    if content_type == "multipart/alternative":
        return _select_alternative(by_path, row, children, prefer)
    if content_type == "multipart/related":
        return _select_related(by_path, row, children, prefer)
    return _select_first_displayable(by_path, row, children, prefer)


def _is_displayable(selection: DisplayBodySelection) -> bool:
    return selection.leaf is not None and selection.leaf["kind"] == "text"


def _children(
    by_path: dict[str, sqlite3.Row],
    row: sqlite3.Row,
) -> list[sqlite3.Row]:
    return [by_path[p] for p in json.loads(row["children_json"])]


def _select_alternative(
    by_path: dict[str, sqlite3.Row],
    row: sqlite3.Row,
    children: list[sqlite3.Row],
    prefer: Prefer,
) -> DisplayBodySelection:
    """multipart/alternative：优先 prefer，否则最后一个可展示分支。

    每个分支都按完整规则尝试解析（alternative 的 html 分支常被包成
    multipart/related）。分支内部的 start 配置错误在该分支*未被选中*
    时不阻断其他分支；全部分支都不可展示且存在配置错误时，把文档序
    第一个错误上抛（该错误本就位于唯一可能的展示路径上）。
    """

    wanted = _PREFER_CONTENT_TYPE[prefer]
    considered = [c["path"] for c in children]

    displayable: list[tuple[sqlite3.Row, DisplayBodySelection]] = []
    first_error: Optional[_RelatedStartError] = None
    for child in children:
        try:
            sub = _select(by_path, child, prefer)
        except _RelatedStartError as exc:
            if first_error is None:
                first_error = exc
            continue
        if _is_displayable(sub):
            displayable.append((child, sub))

    if not displayable:
        if first_error is not None:
            raise first_error
        return DisplayBodySelection(None, [])

    candidates = [c["path"] for c, _ in displayable]

    # 1) 可展示分支中，实际正文匹配偏好类型的第一个（文档序）。
    for child, sub in displayable:
        if sub.leaf["content_type"].lower() == wanted:
            step = DisplayBodyStep(
                node_path=row["path"],
                content_type=row["content_type"],
                rule=RULE_ALTERNATIVE_PREFER,
                considered=considered,
                candidates=candidates,
                chosen=child["path"],
                detail={"prefer": prefer},
            )
            return DisplayBodySelection(sub.leaf, [step] + sub.steps)

    # 2) 无偏好命中：最后一个可展示分支（最忠实表示）。
    child, sub = displayable[-1]
    step = DisplayBodyStep(
        node_path=row["path"],
        content_type=row["content_type"],
        rule=RULE_ALTERNATIVE_LAST,
        considered=considered,
        candidates=candidates,
        chosen=child["path"],
        detail={"prefer": prefer},
    )
    return DisplayBodySelection(sub.leaf, [step] + sub.steps)


def _select_related(
    by_path: dict[str, sqlite3.Row],
    row: sqlite3.Row,
    children: list[sqlite3.Row],
    prefer: Prefer,
) -> DisplayBodySelection:
    """multipart/related：start 参数匹配 Content-ID，否则取首个子件。"""

    start = _related_start(row)

    if start is not None:
        considered = [c["path"] for c in children]
        matches = [c for c in children if start in _content_ids(c)]
        if not matches:
            raise _RelatedStartError(
                row["path"],
                "RELATED_START_NOT_FOUND",
                "multipart/related 的 start 参数没有匹配到任何子件 Content-ID",
                {"start": start},
            )
        if len(matches) > 1:
            raise _RelatedStartError(
                row["path"],
                "AMBIGUOUS_RELATED_START",
                "multipart/related 的 start 参数命中了多个同 Content-ID 子件",
                {
                    "start": start,
                    "candidates": [c["path"] for c in matches],
                },
            )
        chosen = matches[0]
        step = DisplayBodyStep(
            node_path=row["path"],
            content_type=row["content_type"],
            rule=RULE_RELATED_START,
            considered=considered,
            candidates=[c["path"] for c in matches],
            chosen=chosen["path"],
            detail={"start": start},
        )
    else:
        # 未配置 start：首个子件即根部分（RFC 2387 缺省规则）。
        # 解析器保证 multipart 至少一个部分，故 children 非空。
        chosen = children[0]
        step = DisplayBodyStep(
            node_path=row["path"],
            content_type=row["content_type"],
            rule=RULE_RELATED_FIRST,
            considered=[chosen["path"]],
            candidates=[chosen["path"]],
            chosen=chosen["path"],
        )

    # start/首个子件是权威选择：即使目标不可展示（图片、attachment…），
    # 也不静默改选其他兄弟；不可展示由上层按规则跳过或最终 404。
    sub = _select(by_path, chosen, prefer)
    return DisplayBodySelection(sub.leaf, [step] + sub.steps)


def _select_first_displayable(
    by_path: dict[str, sqlite3.Row],
    row: sqlite3.Row,
    children: list[sqlite3.Row],
    prefer: Prefer,
) -> DisplayBodySelection:
    """multipart/mixed 等：按文档序取首个可展示子件。"""

    considered: list[str] = []
    for child in children:
        considered.append(child["path"])
        sub = _select(by_path, child, prefer)
        if _is_displayable(sub):
            step = DisplayBodyStep(
                node_path=row["path"],
                content_type=row["content_type"],
                rule=RULE_FIRST_DISPLAYABLE,
                considered=considered,
                candidates=[child["path"]],
                chosen=child["path"],
            )
            return DisplayBodySelection(sub.leaf, [step] + sub.steps)
    return DisplayBodySelection(None, [])


# ---------------------------------------------------------------------------
# Content-ID / start 提取
# ---------------------------------------------------------------------------


def _content_ids(row: sqlite3.Row) -> list[str]:
    """节点声明的全部规范化 Content-ID（去重，保留头字段文档序）。"""

    cids: list[str] = []
    for name, value in json_loads_headers(row["headers_json"]):
        if name.lower() == "content-id":
            cid = normalize_content_id(value)
            if cid and cid not in cids:
                cids.append(cid)
    return cids


def _related_start(row: sqlite3.Row) -> Optional[str]:
    """读取 multipart/related 容器 Content-Type 上的 RFC 2387 ``start``。

    归档只留存了解码后的头值字符串（headers_json），这里用归档器自己
    的严格 Content-Type 解析器重新解析取参数——纯读、不碰原始记录。
    结构参数（boundary/start）均为 ASCII，重新编码解析与归档时等价。
    未配置或没有 Content-Type 头时返回 None。
    """

    for name, value in json_loads_headers(row["headers_json"]):
        if name.lower() != "content-type":
            continue
        ct = parse_content_type(value.encode("utf-8"), row["path"])
        start = ct.params.get("start")
        if start is None:
            return None
        return normalize_content_id(start) or None
    return None
