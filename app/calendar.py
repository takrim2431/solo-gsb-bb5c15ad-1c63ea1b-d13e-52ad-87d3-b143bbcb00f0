"""日历事件解析（``GET /calendar/events``，iCalendar / RFC 5545）。

在**全部归档邮件**的 ``text/calendar`` 叶子节点（含
``message/rfc822`` / ``message/global`` 封装报文内部）上**只读**解析
iCalendar 数据，提取其中**单个 VEVENT**（本模块刻意忽略 RRULE 等
重复性事件）。不回写任何记录，对历史归档数据同样适用；同一归档库
重复请求结果逐字节稳定。

解析流程：

1. **展开折叠行**：归档时换行已统一为 ``\\n``；RFC 5545 §3.1 规定
   逻辑行可在任意字符处折行——下一行以 SP/HT 开头。展开时删除换行
   及其后的**一个**空白字符，保留其余空白；
2. **组件遍历**：``BEGIN:VCALENDAR`` 内的 ``METHOD``（值大写化，典型
   为 ``REQUEST`` / ``CANCEL``）作为其范围内 VEVENT 的方法；
   ``VTIMEZONE`` 等非 VEVENT 组件整体跳过；
3. **逐事件字段**：读取 ``UID``、``SEQUENCE``（缺省 0，非负整数）、
   ``DTSTAMP``、``DTSTART``、``DTEND``、``SUMMARY``。每个事件必须有
   UID / DTSTAMP / DTSTART / DTEND；
4. **时间归一**：``UTC``（``Z`` 后缀）直接采用；带 ``TZID`` 的本地
   时间经 **IANA 时区数据库**（``zoneinfo``）折算 UTC；夏令时转换中
   **不存在**（春季跳时落入空隙）或**有歧义**（秋季回拨重复）的本地
   时间逐项诊断；
5. **诊断**：无效时间、缺字段、非法 SEQUENCE 以及**同一节点内重复
   UID** 的每个事件逐项给出诊断，附 ``message_id`` / ``node_path``；
   被诊断的事件不进入跨邮件版本合并；
6. **版本选择**：跨邮件同一 UID 依次按（``SEQUENCE`` 降序、
   ``DTSTAMP`` 降序、``message_id`` 降序、``node_path`` 文档序降序）
   取当前版本；``METHOD:CANCEL`` 的胜出版本标记 ``status=cancelled``
   并保留其来源，其余为 ``confirmed``。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# 稳定诊断码
# ---------------------------------------------------------------------------

CODE_MISSING_FIELD = "MISSING_CALENDAR_FIELD"
CODE_INVALID_TIME = "INVALID_CALENDAR_TIME"
CODE_NONEXISTENT_TIME = "CALENDAR_TIME_NONEXISTENT"
CODE_AMBIGUOUS_TIME = "CALENDAR_TIME_AMBIGUOUS"
CODE_INVALID_SEQUENCE = "INVALID_CALENDAR_SEQUENCE"
CODE_DUPLICATE_UID = "DUPLICATE_CALENDAR_UID"

# 同事件多诊断时的稳定优先级（影响诊断列表排序，不影响“是否入选”）。
_CODE_RANK = {
    CODE_MISSING_FIELD: 0,
    CODE_INVALID_SEQUENCE: 1,
    CODE_INVALID_TIME: 2,
    CODE_NONEXISTENT_TIME: 3,
    CODE_AMBIGUOUS_TIME: 4,
    CODE_DUPLICATE_UID: 5,
}

# RFC 5545 §3.3.5 DATE-TIME：YYYYMMDDTHHMMSS[Z]
_DATETIME_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z)?$"
)

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarDiagnostic:
    """一条逐项诊断（始终带 message_id / node_path 定位）。"""

    message_id: int
    node_path: str
    code: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CalendarSource:
    """事件胜出版本（CANCEL 同样保留）的归档来源。"""

    message_id: int
    node_path: str


@dataclass(frozen=True)
class CalendarEvent:
    """跨邮件版本选择后的当前事件视图。"""

    uid: str
    sequence: int
    method: str | None  # 大写 METHOD，如 REQUEST / CANCEL；无 METHOD 时 None
    status: str  # confirmed | cancelled（METHOD:CANCEL）
    dtstamp: datetime  # UTC
    dtstart: datetime  # UTC
    dtend: datetime  # UTC
    summary: str | None
    source: CalendarSource


@dataclass
class _RawEvent:
    """单个 VEVENT 的解析中间结果（含原始字符串与逐项诊断）。"""

    index: int  # 节点内 VEVENT 文档序（0 基）
    method_cell: list[str | None] | None = None  # 所属 VCALENDAR 的 METHOD 单元
    uid: str | None = None
    sequence_raw: str | None = None
    sequence: int | None = None
    dtstamp_raw: str | None = None
    dtstart_raw: str | None = None
    dtend_raw: str | None = None
    summary: str | None = None
    datetimes: dict[str, datetime] = field(default_factory=dict)
    diagnostics: list[tuple[str, dict]] = field(default_factory=list)

    @property
    def method(self) -> str | None:
        return self.method_cell[0] if self.method_cell is not None else None


# ---------------------------------------------------------------------------
# RFC 5545 词法：折叠展开、内容行、文本反转义
# ---------------------------------------------------------------------------


def unfold_lines(text: str) -> list[str]:
    """展开 RFC 5545 §3.1 折叠行并按物理行切分。

    归档文本已统一为 LF 换行。续行以 SP/HT 开头：把换行与续行**首个**
    空白字符一并删除（若换行前一行以空白续接，则没有任何拼接间隙），
    续行其余空白保留。
    """

    lines: list[str] = []
    for raw in text.split("\n"):
        if raw[:1] in (" ", "\t"):
            if lines:
                lines[-1] += raw[1:]
            else:
                # 首行即续行属于坏数据，宽容地去掉引导空白。
                lines.append(raw[1:])
        else:
            lines.append(raw)
    return lines


def _split_contentline(line: str) -> tuple[str, dict[str, str], str] | None:
    """把一行解析为 ``(大写属性名, 参数表, 原始值)``。

    参数按 RFC 5545 §3.1 以 ``;`` 分隔（尊重双引号字符串），同名参数
    取首次出现；值分隔 ``:`` 同样不允许出现在引号内。任何无分隔符的
    坏行返回 None（跳过，不影响其他事件）。
    """

    params: dict[str, str] = {}
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes and ch == ":":
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None

    segments = head.split(";")
    name = segments[0].strip().upper()
    if not name:
        return None
    for seg in segments[1:]:
        if "=" not in seg:
            continue
        pname, _, pvalue = seg.partition("=")
        pname = pname.strip().upper()
        if pname and pname not in params:
            params[pname] = pvalue.strip()
    return name, params, value


def unescape_text(value: str) -> str:
    """RFC 5545 §3.3.11 TEXT 反转义（``\\\\,`` ``\\\\;`` ``\\\\\\\\`` ``\\\\n``/``\\\\N``）。"""

    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in (",", ";", "\\"):
                out.append(nxt)
            elif nxt in ("n", "N"):
                out.append("\n")
            else:
                out.append(ch + nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# DATE / DATE-TIME 解析与时区折算
# ---------------------------------------------------------------------------


def _local_fold(naive: datetime, tz: ZoneInfo) -> int:
    """判断本地时间在 DST 转换中的 fold 值（0=较早偏移，1=较晚偏移）。

    RFC 5545 不带 fold 信息，故先做规范判定：

    * 两个 fold 的 UTC 偏移相同 → 唯一合法时间（取 fold=0）；
    * 偏移不同，但 fold=0 的 UTC 时刻折算回该时区后**墙钟改变** →
      **不存在**（春季跳时，墙钟落入空隙）；
    * 否则墙钟往返一致但有两种偏移 → **歧义**（秋季回拨重复）。
    """

    aware_early = naive.replace(tzinfo=tz)
    aware_late = naive.replace(fold=1, tzinfo=tz)
    if aware_early.utcoffset() == aware_late.utcoffset():
        return 0
    roundtrip_wall = (
        aware_early.astimezone(_UTC)
        .astimezone(tz)
        .replace(tzinfo=None)
    )
    return -2 if roundtrip_wall != naive else -1


def parse_date_time(
    value: str, params: dict[str, str]
) -> tuple[datetime | None, str | None]:
    """解析单个 iCalendar 时间值为 UTC。

    返回 ``(utc_datetime, error_code)``：成功时错误码为 None；失败时
    datetime 为 None，错误码为 :data:`CODE_INVALID_TIME`、
    :data:`CODE_NONEXISTENT_TIME` 或 :data:`CODE_AMBIGUOUS_TIME`。

    * 仅接受 UTC（``Z``）或带 ``TZID`` 的 DATE-TIME；DATE 值
      （``VALUE=DATE``）、浮动时间（无 Z 无 TZID）均为无效；
    * ``TZID`` 必须能在 IANA 时区数据库中解析；
    * 夏令时不存在 / 歧义本地时间分别给对应错误码。
    """

    value_type = params.get("VALUE", "").upper()
    tzid_raw = params.get("TZID")
    tzid = tzid_raw.strip().strip('"') if tzid_raw is not None else None

    if value_type == "DATE":
        return None, CODE_INVALID_TIME

    m = _DATETIME_RE.match(value)
    if not m:
        return None, CODE_INVALID_TIME
    year, mon, day, hour, minute, second, zuluf = m.groups()

    # Z 与 TZID 互斥；任一带 TZID 却写 Z 的组合按无效处理。
    if zuluf and tzid:
        return None, CODE_INVALID_TIME

    try:
        naive = datetime(
            int(year), int(mon), int(day),
            int(hour), int(minute), int(second),
        )
    except ValueError:
        return None, CODE_INVALID_TIME

    if zuluf:
        return naive.replace(tzinfo=_UTC), None

    if not tzid:
        # 浮动本地时间无法确定为 UTC。
        return None, CODE_INVALID_TIME

    try:
        tz = ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None, CODE_INVALID_TIME

    kind = _local_fold(naive, tz)
    if kind == -1:
        return None, CODE_AMBIGUOUS_TIME
    if kind == -2:
        return None, CODE_NONEXISTENT_TIME
    return naive.replace(tzinfo=tz).astimezone(_UTC), None


def _format_utc(dt: datetime) -> str:
    """统一为带 ``Z`` 的 RFC 5545 风格 UTC ISO 串。"""

    return dt.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 节点解析
# ---------------------------------------------------------------------------


def _path_key(path: str) -> tuple[int, ...]:
    """物化路径的文档序键：``""`` → ()，``"1/2"`` → (1, 2)。"""

    if not path:
        return ()
    return tuple(int(seg) for seg in path.split("/"))


def parse_calendar_node(
    text: str,
) -> list[_RawEvent]:
    """解析一个 ``text/calendar`` 叶子的归档文本为原始 VEVENT 列表。

    折叠已展开、字符集已解码（归档口径）。坏行/坏组件一律宽容跳过；
    每个 VEVENT 的字段级问题（含后续时间校验）挂在其
    :class:`_RawEvent.diagnostics` 上。
    """

    # 组件栈只需要两类信息：是否在 VCALENDAR（取 METHOD）、是否在
    # 非 VEVENT 组件（整体跳过其内容）。
    stack: list[str] = []
    # 每个 VCALENDAR 对应一个可变 METHOD 单元——事件关闭时记下引用，
    # 这样 METHOD 写在 VEVENT 之后（宽容位置）也能取到。
    method_stack: list[list[str | None]] = []
    current: _RawEvent | None = None
    # 当前 VEVENT 是否已见某属性（RFC 属性在 VEVENT 内唯一，宽容地取
    # 首次出现——重复属性属脏数据，不再逐项诊断）。
    seen: set[str] = set()
    events: list[_RawEvent] = []

    def enclosing_method_cell() -> list[str | None] | None:
        return method_stack[-1] if method_stack else None

    def enclosing_non_event() -> bool:
        """是否处于 VTIMEZONE 等非 VEVENT 组件内部（当前 VEVENT 除外）。"""

        return any(
            comp not in ("VCALENDAR", "VEVENT") for comp in stack
        )

    def close_event(event: _RawEvent) -> None:
        event.method_cell = enclosing_method_cell()
        events.append(event)

    for line in unfold_lines(text):
        if not line.strip():
            continue
        parsed = _split_contentline(line)
        if parsed is None:
            continue
        name, params, value = parsed

        if name == "BEGIN":
            comp = value.strip().upper()
            if comp == "VCALENDAR":
                method_stack.append([None])
            elif comp == "VEVENT" and current is None and not enclosing_non_event():
                current = _RawEvent(index=len(events))
                seen = set()
            stack.append(comp)
            continue

        if name == "END":
            comp = value.strip().upper()
            if (
                comp == "VEVENT"
                and current is not None
                and stack
                and stack[-1] == "VEVENT"
            ):
                close_event(current)
                current = None
            if comp in stack:
                # 弹到匹配的 BEGIN（宽容处理不成对的 END）。
                while stack:
                    top = stack.pop()
                    if top == "VCALENDAR":
                        method_stack.pop()
                    if top == comp:
                        break
            continue

        if name == "METHOD" and "VCALENDAR" in stack and current is None:
            cell = enclosing_method_cell()
            if cell is not None and cell[0] is None:
                method_value = value.strip().upper()
                cell[0] = method_value or None
            continue

        if current is None or enclosing_non_event():
            continue

        if name == "UID" and "UID" not in seen:
            seen.add("UID")
            current.uid = unescape_text(value.strip()) or None
        elif name == "SEQUENCE" and "SEQUENCE" not in seen:
            seen.add("SEQUENCE")
            current.sequence_raw = value.strip()
        elif name == "DTSTAMP" and "DTSTAMP" not in seen:
            seen.add("DTSTAMP")
            current.dtstamp_raw = value
            dt, err = parse_date_time(value, params)
            if dt is not None:
                current.datetimes["DTSTAMP"] = dt
            else:
                current.diagnostics.append(
                    (err, {"field": "DTSTAMP", "value": value})
                )
        elif name == "DTSTART" and "DTSTART" not in seen:
            seen.add("DTSTART")
            current.dtstart_raw = value
            dt, err = parse_date_time(value, params)
            if dt is not None:
                current.datetimes["DTSTART"] = dt
            else:
                current.diagnostics.append(
                    (err, {"field": "DTSTART", "value": value})
                )
        elif name == "DTEND" and "DTEND" not in seen:
            seen.add("DTEND")
            current.dtend_raw = value
            dt, err = parse_date_time(value, params)
            if dt is not None:
                current.datetimes["DTEND"] = dt
            else:
                current.diagnostics.append(
                    (err, {"field": "DTEND", "value": value})
                )
        elif name == "SUMMARY" and "SUMMARY" not in seen:
            seen.add("SUMMARY")
            current.summary = unescape_text(value)

    # 未闭合的 VEVENT（EOF）同样参与字段校验。
    if current is not None:
        close_event(current)
    return events


def _validate_event(
    event: _RawEvent, message_id: int, node_path: str
) -> list[CalendarDiagnostic]:
    """对单个原始 VEVENT 做必填字段 / SEQUENCE 校验，返回诊断列表。"""

    diagnostics: list[CalendarDiagnostic] = []

    def add(code: str, detail: dict) -> None:
        diagnostics.append(
            CalendarDiagnostic(
                message_id=message_id,
                node_path=node_path,
                code=code,
                detail={"vevent_index": event.index, **detail},
            )
        )

    if not event.uid:
        add(CODE_MISSING_FIELD, {"field": "UID"})
    for field_name, raw in (
        ("DTSTAMP", event.dtstamp_raw),
        ("DTSTART", event.dtstart_raw),
        ("DTEND", event.dtend_raw),
    ):
        # 属性完全缺失 → MISSING；属性存在但值非法（含空值、DATE 值、
        # 浮动/坏时区/夏令时问题）时，节点解析阶段已记录具体错误码。
        if raw is None:
            add(CODE_MISSING_FIELD, {"field": field_name})

    if event.sequence_raw is None:
        event.sequence = 0
    elif event.sequence_raw.isdigit():
        event.sequence = int(event.sequence_raw)
    else:
        event.sequence = None
        add(
            CODE_INVALID_SEQUENCE,
            {"field": "SEQUENCE", "value": event.sequence_raw},
        )

    # 时间类诊断（无效 / 不存在 / 歧义）在节点解析时收集，此处补齐定位。
    for code, detail in event.diagnostics:
        add(code, detail)

    return diagnostics


# ---------------------------------------------------------------------------
# 全库入口：节点扫描 + 跨邮件版本选择
# ---------------------------------------------------------------------------


def _is_calendar_leaf(row: sqlite3.Row) -> bool:
    return row["kind"] == "text" and row["content_type"].lower() == "text/calendar"


def collect_calendar_events(
    rows: list[sqlite3.Row],
) -> tuple[list[CalendarEvent], list[CalendarDiagnostic]]:
    """在全部归档邮件的节点行上只读汇总当前日历事件。

    ``rows`` 为 :func:`database.list_calendar_nodes` 的结果（含每条
    消息的 ``message_id``）。返回 ``(events, diagnostics)``：

    * 被诊断（缺字段/时间非法/夏令时问题/非法 SEQUENCE/节点内 UID 重复）
      的事件只出现在 ``diagnostics``，不参与版本选择；
    * 跨邮件同 UID 按（SEQUENCE、DTSTAMP、message_id、node_path 文档序）
      取当前版本；CANCEL 胜出时 ``status=cancelled``；
    * 事件按（UTC DTSTART、UID）稳定排序。
    """

    diagnostics: list[CalendarDiagnostic] = []
    # UID → 候选（全部有效、且在各自节点内 UID 唯一）
    candidates: dict[str, list[tuple[_RawEvent, int, str]]] = {}

    # 节点内重复 UID 的检测需要在单节点解析后立即进行。
    grouped: dict[tuple[int, str], list[sqlite3.Row]] = {}
    order: list[tuple[int, str]] = []
    for row in rows:
        if not _is_calendar_leaf(row) or row["text_content"] is None:
            continue
        key = (row["message_id"], row["path"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    # 不依赖 SQL 行序：按 message_id 与物化路径文档序稳定处理。
    order.sort(key=lambda key: (key[0], _path_key(key[1])))
    for message_id, node_path in order:
        node_rows = grouped[(message_id, node_path)]
        text = node_rows[0]["text_content"]
        raws = parse_calendar_node(text)

        # 节点内 UID 重复：每个具有该 UID 的事件逐项诊断。
        uid_counts: dict[str, int] = {}
        for raw in raws:
            if raw.uid:
                uid_counts[raw.uid] = uid_counts.get(raw.uid, 0) + 1
        duplicate_uids = {uid for uid, count in uid_counts.items() if count > 1}

        # 逐事件汇总其全部诊断（字段/时间 + 重复 UID），判定是否出局。
        diag_by_index: dict[int, list[CalendarDiagnostic]] = {}
        for raw in raws:
            diag_by_index[raw.index] = _validate_event(raw, message_id, node_path)
            if raw.uid in duplicate_uids:
                diag_by_index[raw.index].append(
                    CalendarDiagnostic(
                        message_id=message_id,
                        node_path=node_path,
                        code=CODE_DUPLICATE_UID,
                        detail={
                            "vevent_index": raw.index,
                            "uid": raw.uid,
                            "count": uid_counts[raw.uid],
                        },
                    )
                )

        diagnostics.extend(
            diag for group in diag_by_index.values() for diag in group
        )

        for raw in raws:
            if diag_by_index[raw.index]:
                continue  # 任一诊断 → 出局
            candidates.setdefault(raw.uid, []).append(
                (raw, message_id, node_path)
            )

    # 跨邮件版本选择。
    events: list[CalendarEvent] = []
    for uid, options in candidates.items():
        winner, message_id, node_path = max(
            options,
            key=lambda item: (
                item[0].sequence,
                item[0].datetimes["DTSTAMP"].timestamp(),
                item[1],
                _path_key(item[2]),
            ),
        )
        events.append(
            CalendarEvent(
                uid=uid,
                sequence=winner.sequence,
                method=winner.method,
                status="cancelled" if winner.method == "CANCEL" else "confirmed",
                dtstamp=winner.datetimes["DTSTAMP"],
                dtstart=winner.datetimes["DTSTART"],
                dtend=winner.datetimes["DTEND"],
                summary=winner.summary,
                source=CalendarSource(
                    message_id=message_id, node_path=node_path
                ),
            )
        )

    events.sort(key=lambda e: (_format_utc(e.dtstart), e.uid))
    diagnostics.sort(
        key=lambda d: (
            d.message_id,
            _path_key(d.node_path),
            d.detail.get("vevent_index", 0),
            _CODE_RANK.get(d.code, 99),
        )
    )
    return events, diagnostics
