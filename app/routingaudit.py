"""投递路径时钟审计（``GET /messages/{id}/routing-audit``）。

邮件每经过一个 MTA，接收方就会在报文**顶部**追加一个 ``Received`` 头
（RFC 5321 §4.4），因此一封邮件中这些头在物理顺序上是**最新的在前、
最旧的在后**。本模块从**根邮件**（path 为 ``""`` 的节点，不含
``message/rfc822`` 封装报文内部）读取全部 ``Received`` 头，按邮件
传输方向**自底向上**（物理顺序倒序）重建 hop 列表，并在相邻 hop 间
做确定性的时钟/路径异常检测：

1. **逐头解析**：先剥掉 RFC 5321 圆括号注释（``(Postfix)``、
   ``(PDT)``、IP 补充信息等，支持嵌套与反斜杠转义），最后一个 ``;``
   之后是 date-time（RFC 5322，兼容数字/命名时区），之前按
   ``from`` / ``by`` / ``via`` / ``with`` / ``id`` / ``for`` 关键字
   切子句，取 ``by`` 主机与 ``with`` 协议；
2. **原始顺序可追溯**：每个 hop 同时给出自底向上的 ``index``（0 基，
   与传输方向一致）与原始头顺序 ``header_index``（0 基，物理顺序，
   二者数值上互为倒序）；
3. **时间倒退**（``time_reversal``）：相邻两个时间均可解析的 hop，
   后一 hop（更靠近接收端）时间早于前一 hop——时钟回拨或头被伪造；
4. **重复主机回环**（``host_loop``）：``by`` 规范化主机（小写、剥
   地址字面量方括号/IPv6 标签与根点）在更早的 hop 已出现过，
   ``detail`` 给出首次出现位置，不要求两 hop 相邻；
5. **无法解析的头**（``unparseable_header``）：缺 ``by`` 主机或缺/
   损坏 date-time 时**不拒收、不改写归档数据**，该 hop 仍保留在列表
   中（缺失字段为 ``null``），并列入诊断 ``anomalies``。

检测是**纯只读**的：输入为已归档根节点头字段，不回写任何记录，对
历史归档数据同样适用；``anomalies`` 按（hop 顺序、类型固定优先级）
稳定排序，``first_anomaly`` 给出首个异常的位置。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .related import json_loads_headers

# 子句关键字（RFC 5321 Received 语法，大小写不敏感）。
_CLAUSE_KEYWORDS = frozenset({"from", "by", "via", "with", "id", "for"})

# RFC 5322 date-time：[星期 ","] 日 月名 年 时:分[:秒] 时区
_DATE_RE = re.compile(
    r"^\s*(?:[A-Za-z]{3,9}\s*,\s*)?"
    r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2,4})\s+"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*"
    r"([+-]\d{4}|[A-Za-z]{1,5})\s*$"
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# RFC 5322 §4.3 的（废弃）命名时区 + 实际邮件中常见的 UTC，单位：分钟
_NAMED_ZONES = {
    "ut": 0, "gmt": 0, "z": 0, "utc": 0,
    "est": -5 * 60, "edt": -4 * 60,
    "cst": -6 * 60, "cdt": -5 * 60,
    "mst": -7 * 60, "mdt": -6 * 60,
    "pst": -8 * 60, "pdt": -7 * 60,
}

# 异常类型固定优先级（同一 hop 上多种异常的稳定排序依据）。
ANOMALY_UNPARSEABLE = "unparseable_header"
ANOMALY_TIME_REVERSAL = "time_reversal"
ANOMALY_HOST_LOOP = "host_loop"
_ANOMALY_RANK = {
    ANOMALY_UNPARSEABLE: 0,
    ANOMALY_TIME_REVERSAL: 1,
    ANOMALY_HOST_LOOP: 2,
}


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingHop:
    """一个 ``Received`` 头重建出的一跳。"""

    index: int  # 自底向上（传输方向）hop 序号，0 基
    header_index: int  # 原始 Received 头物理顺序序号，0 基（最新一跳为 0）
    host: Optional[str]  # by 主机规范化值（小写/剥括号与根点）；不可解析为 None
    host_raw: Optional[str]  # by 后的原始 token
    from_host: Optional[str]  # from 后的原始 token（可选子句，缺失为 None）
    protocol: Optional[str]  # with 协议规范化为小写；缺失为 None
    time: Optional[datetime]  # 带原始时区偏移的解析结果；不可解析为 None
    time_raw: Optional[str]  # 最后一个 ';' 之后的原始 date-time 文本
    raw: str  # 去折叠后的完整头值（归档口径），诊断用


@dataclass(frozen=True)
class RoutingAnomaly:
    """一条投递路径异常。``detail`` 内容随 ``type`` 不同：

    * ``unparseable_header``：``{"reasons": [...], "date_raw": ...}``；
    * ``time_reversal``：``{"previous": ISO, "current": ISO,
      "delta_seconds": 负整数}``（相邻 hop，后一跳时间更早）；
    * ``host_loop``：``{"host": 规范化主机, "first_index": 首次 hop,
      "first_header_index": 首次头序号}``。
    """

    type: str
    hop_index: int
    header_index: int
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingAuditResult:
    """一封邮件的投递路径重建与异常检测结果。"""

    hops: list[RoutingHop]
    anomalies: list[RoutingAnomaly]


# ---------------------------------------------------------------------------
# Received 头解析
# ---------------------------------------------------------------------------


def _strip_comments(value: str) -> str:
    """剥掉 RFC 5321/5322 圆括号注释（支持嵌套与 ``\\`` 转义）。

    注释外的反斜杠在真实 Received 头里不出现，原样保留；未配对的右括号
    视为普通字符（审计场景不拒收脏头）。
    """

    out: list[str] = []
    depth = 0
    escape = False
    for ch in value:
        if escape:
            if depth == 0:
                out.append(ch)
            escape = False
            continue
        if ch == "\\":
            if depth == 0:
                out.append(ch)
            escape = True
            continue
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def parse_received_date(value: str) -> Optional[datetime]:
    """解析 date-time 为**带原始时区偏移**的 datetime；任何不合法返回 None。

    两位年份按 RFC 5322（obs-year）：00–49 → 2000s，50–99 → 1900s。
    命名时区（GMT/EST/UTC 等）折算为固定偏移；非法日期（如 2 月 30 日）
    返回 None。与 :func:`app.threading.parse_date_header` 的区别是保留
    头中声明的时区偏移（用于审计展示），比较时再统一转 UTC。
    """

    m = _DATE_RE.match(value)
    if not m:
        return None
    day, month_name, year, hour, minute, second, zone = m.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    y = int(year)
    if y < 100:
        y += 2000 if y < 50 else 1900
    if zone.startswith(("+", "-")):
        sign = 1 if zone[0] == "+" else -1
        offset = sign * (int(zone[1:3]) * 60 + int(zone[3:5]))
    else:
        offset = _NAMED_ZONES.get(zone.lower())
        if offset is None:
            return None
    try:
        return datetime(
            y, month, int(day), int(hour), int(minute), int(second or 0),
            tzinfo=timezone(timedelta(minutes=offset)),
        )
    except ValueError:
        return None


def normalize_host(raw: str) -> Optional[str]:
    """规范化 ``by`` 主机以便回环比较。

    去首尾空白并小写；剥掉地址字面量方括号（``[1.2.3.4]`` →
    ``1.2.3.4``，``[IPv6:2001:db8::1]`` → ``2001:db8::1``）；去掉
    FQDN 末尾根点。空串返回 None。
    """

    host = raw.strip().lower()
    if len(host) >= 2 and host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        if host.startswith("ipv6:"):
            host = host[5:]
    host = host.rstrip(".")
    return host or None


def _parse_clauses(
    clause_part: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """从 date-time 之前的子句文本取 ``(by 原始主机, from 原始主机, with 协议)``。

    按空白切 token，遇到 RFC 5321 子句关键字时消费紧随其后的一个 token；
    无法识别的游离 token 忽略（审计场景不拒收脏头）。每个子句取首次
    出现（合法头中各关键字至多一个）。
    """

    tokens = clause_part.split()
    by_host: Optional[str] = None
    from_host: Optional[str] = None
    protocol: Optional[str] = None
    i = 0
    while i < len(tokens):
        keyword = tokens[i].lower()
        i += 1
        if keyword not in _CLAUSE_KEYWORDS or i >= len(tokens):
            continue
        token = tokens[i]
        i += 1
        if keyword == "by" and by_host is None:
            by_host = token
        elif keyword == "from" and from_host is None:
            from_host = token
        elif keyword == "with" and protocol is None:
            protocol = token.lower()
    return by_host, from_host, protocol


@dataclass(frozen=True)
class _ParsedReceived:
    """单个 Received 头的结构化解析中间结果（index 由调用方补齐）。"""

    host: Optional[str]  # by 主机规范化值；不可解析为 None
    host_raw: Optional[str]
    from_host: Optional[str]
    protocol: Optional[str]
    time: Optional[datetime]
    time_raw: Optional[str]
    reasons: list[str]  # 无法解析原因：missing_by / missing_date / invalid_date


def parse_received_header(raw: str) -> _ParsedReceived:
    """解析单个已去折叠的 Received 头值（不感知 hop 顺序）。"""

    cleaned = _strip_comments(raw)
    if ";" in cleaned:
        clause_part, _, date_raw = cleaned.rpartition(";")
        date_raw = date_raw.strip()
    else:
        clause_part, date_raw = cleaned, None

    by_host, from_host, protocol = _parse_clauses(clause_part)
    host = normalize_host(by_host) if by_host is not None else None
    parsed_time = parse_received_date(date_raw) if date_raw else None

    reasons: list[str] = []
    if host is None:
        reasons.append("missing_by")
    if not date_raw:
        reasons.append("missing_date")
    elif parsed_time is None:
        reasons.append("invalid_date")
    return _ParsedReceived(
        host=host,
        host_raw=by_host,
        from_host=from_host,
        protocol=protocol,
        time=parsed_time,
        time_raw=date_raw,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _root_received_values(root_row: sqlite3.Row) -> list[str]:
    """从根节点 headers_json 取全部 Received 头值（物理顺序，含重复）。"""

    values: list[str] = []
    for name, value in json_loads_headers(root_row["headers_json"]):
        if name.lower() == "received":
            values.append(value)
    return values


def audit_routing(root_row: sqlite3.Row) -> RoutingAuditResult:
    """在一封邮件的**根节点行**上重建投递路径并检测异常（纯只读）。

    hop 按邮件传输方向自底向上排列：物理顺序最后的 Received（最早
    一跳，通常是发件方 MSA）为 ``index=0``。
    """

    values = _root_received_values(root_row)

    hops: list[RoutingHop] = []
    # 物理顺序倒序遍历：header_index 最新为 n-1 … 0，index 从 0 递增。
    parsed_by_header: dict[int, _ParsedReceived] = {}
    for index, header_index in enumerate(range(len(values) - 1, -1, -1)):
        raw = values[header_index]
        parsed = parse_received_header(raw)
        parsed_by_header[header_index] = parsed
        hops.append(
            RoutingHop(
                index=index,
                header_index=header_index,
                host=parsed.host,
                host_raw=parsed.host_raw,
                from_host=parsed.from_host,
                protocol=parsed.protocol,
                time=parsed.time,
                time_raw=parsed.time_raw,
                raw=raw,
            )
        )

    anomalies: list[RoutingAnomaly] = []

    # 1) 无法解析的头（逐 hop 独立）。
    for hop in hops:
        reasons = parsed_by_header[hop.header_index].reasons
        if reasons:
            anomalies.append(
                RoutingAnomaly(
                    type=ANOMALY_UNPARSEABLE,
                    hop_index=hop.index,
                    header_index=hop.header_index,
                    detail={"reasons": reasons, "date_raw": hop.time_raw},
                )
            )

    # 2) 相邻 hop 时间倒退：两者时间都必须可解析，中间不跳过坏头。
    for prev, cur in zip(hops, hops[1:]):
        if prev.time is not None and cur.time is not None:
            prev_utc = prev.time.astimezone(timezone.utc)
            cur_utc = cur.time.astimezone(timezone.utc)
            if cur_utc < prev_utc:
                anomalies.append(
                    RoutingAnomaly(
                        type=ANOMALY_TIME_REVERSAL,
                        hop_index=cur.index,
                        header_index=cur.header_index,
                        detail={
                            "previous": prev.time.isoformat(),
                            "current": cur.time.isoformat(),
                            "previous_index": prev.index,
                            "delta_seconds": int(
                                (cur_utc - prev_utc).total_seconds()
                            ),
                        },
                    )
                )

    # 3) 重复主机回环：规范化 by 主机在更早 hop（不必相邻）已出现。
    first_seen: dict[str, int] = {}
    for hop in hops:
        if hop.host is None:
            continue
        if hop.host in first_seen:
            first_index = first_seen[hop.host]
            first_hop = hops[first_index]
            anomalies.append(
                RoutingAnomaly(
                    type=ANOMALY_HOST_LOOP,
                    hop_index=hop.index,
                    header_index=hop.header_index,
                    detail={
                        "host": hop.host,
                        "first_index": first_index,
                        "first_header_index": first_hop.header_index,
                    },
                )
            )
        else:
            first_seen[hop.host] = hop.index

    anomalies.sort(
        key=lambda a: (a.hop_index, _ANOMALY_RANK[a.type], a.header_index)
    )
    return RoutingAuditResult(hops=hops, anomalies=anomalies)
