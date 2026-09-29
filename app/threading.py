"""会话（thread）关联构建：Message-ID / In-Reply-To / References 回复图。

本模块是**纯只读**的确定性计算：输入为 :func:`database.list_thread_sources`
返回的全部归档邮件行（id、归档时间、根节点头字段），不回写任何记录，
对历史归档数据同样适用；同一归档库重复请求得到逐字节稳定的结果。

构建流程：

1. **标识与引用提取**：从每封邮件根节点头中取 ``Message-ID``（多个时取
   第一个）、全部 ``References`` 与 ``In-Reply-To`` 中的 msg-id。引用链
   = References 列表（按序）+ In-Reply-To 中未出现过的 ID（追加在尾）。
   msg-id 规范化：剥一层尖括号、NFC、小写（大小写不敏感匹配）。
2. **父件解析**：父件取引用链中**最后一个能在归档中解析**的 ID（跳过
   解析到自己的自引用，继续向前找）。引用链最后一项（最直接的父件）
   无法解析时记 ``missing_parent`` 断链、只能解析到自己时记 ``cycle``
   断链——即使已借由链上更早的祖先入链，断链同样保留以便定位。
3. **环检测与断开**：候选父边中若存在循环引用（A→B→A 或更长环），
   断开环上排序键（时间、归档 id）**最小**节点的父边——环中最早的
   邮件却引用环内更晚的邮件作为父件，是最可疑的引用；断开后最早
   邮件成为根，符合会话的时间走向。记 ``cycle`` 断链，重复检测
   直到无环——回复图最终是森林。
4. **会话分组**：父子边（无向）的连通分量各为一个会话；没有任何引用
   关系的孤立邮件按**规范化主题**（去 Re:/Fwd:/回复： 等前缀、压缩
   空白、casefold）分组，空主题的孤立邮件各自成组。
5. **排序**：会话内按父子关系拓扑排序（父在子前），并列时按
   （Date 头时间，缺失回退归档时间，归档 id）稳定排序；主题分组的
   成员同样按该键排序。

重复 Message-ID（不同指纹的邮件声明同一 ID）解析到**最早归档**的那封。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .related import json_loads_headers

# ---------------------------------------------------------------------------
# msg-id / 主题 / 日期 规范化
# ---------------------------------------------------------------------------

# 尖括号形式的 msg-id；值中不含空白与其他尖括号
_MSG_ID_RE = re.compile(r"<([^<>\s]+)>")

# 回复/转发主题前缀：re / fw / fwd 与常见中文前缀，半角/全角冒号均可
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(re|fw|fwd|回复|答复|转发)\s*[:：]\s*", re.IGNORECASE
)


def normalize_msg_id(raw: str) -> str:
    """规范化一个 msg-id 以便比较：剥一层尖括号、NFC、小写。"""

    value = raw.strip()
    if len(value) >= 2 and value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return unicodedata.normalize("NFC", value).lower()


def extract_msg_ids(value: str) -> list[str]:
    """从头值中按序提取全部规范化 msg-id（去重，保序）。

    优先匹配 ``<...>`` 形式；整值没有任何尖括号对时回退为按空白切分
    的裸 token（真实邮件中常见的脏写法）。空值返回空列表。
    """

    ids = [m.group(1) for m in _MSG_ID_RE.finditer(value)]
    if not ids:
        ids = value.split()
    result: list[str] = []
    for raw in ids:
        norm = normalize_msg_id(raw)
        if norm and norm not in result:
            result.append(norm)
    return result


def normalize_subject(subject: str) -> str:
    """规范化主题用于分组：循环去回复/转发前缀、压缩空白、casefold。"""

    text = unicodedata.normalize("NFC", subject)
    previous = None
    while previous != text:
        previous = text
        text = _SUBJECT_PREFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


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
# RFC 5322 §4.3 的（废弃）命名时区，单位：相对 UTC 的分钟
_NAMED_ZONES = {
    "ut": 0, "gmt": 0, "z": 0,
    "est": -5 * 60, "edt": -4 * 60,
    "cst": -6 * 60, "cdt": -5 * 60,
    "mst": -7 * 60, "mdt": -6 * 60,
    "pst": -8 * 60, "pdt": -7 * 60,
}


def parse_date_header(value: str) -> Optional[datetime]:
    """解析 RFC 5322 Date 头值为 UTC 时间；任何不合法都返回 None。

    两位年份按 RFC 5322（obs-year）规则：00–49 → 2000s，50–99 → 1900s。
    命名时区按 RFC 5322 §4.3 折算；无法识别的时区/日期（如 2 月 30 日）
    不拒收，仅视为“无可用时间”，排序时回退归档时间。
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
        dt = datetime(
            y, month, int(day), int(hour), int(minute), int(second or 0),
            tzinfo=timezone(timedelta(minutes=offset)),
        )
    except ValueError:
        return None
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadSource:
    """一封归档邮件参与会话构建的规范化输入。"""

    id: int
    message_id: Optional[str]  # 首个规范化 Message-ID；无则 None
    chain: tuple[str, ...]  # 引用链（References + In-Reply-To 追加）
    subject: Optional[str]
    normalized_subject: str
    date: Optional[datetime]  # Date 头解析结果（UTC）
    created_at: str

    @property
    def sort_key(self) -> tuple[datetime, int]:
        """稳定排序键：邮件时间（Date 缺失/非法回退归档时间）+ 归档 id。"""

        if self.date is not None:
            return (self.date, self.id)
        try:
            fallback = datetime.fromisoformat(self.created_at)
            if fallback.tzinfo is None:
                fallback = fallback.replace(tzinfo=timezone.utc)
        except ValueError:
            fallback = datetime.min.replace(tzinfo=timezone.utc)
        return (fallback, self.id)


@dataclass(frozen=True)
class BrokenLink:
    """一条可定位的断链：哪封邮件引用了什么、为何无法落成父边。"""

    message_id: int
    referenced_message_id: str  # 规范化后的被引用 msg-id
    reason: str  # missing_parent | cycle


@dataclass
class ThreadResult:
    """一个会话的完整视图。"""

    thread_id: int  # 排序结果首成员的归档 id（确定性标识）
    members: list[ThreadSource]
    parents: dict[int, int]  # child 归档 id -> parent 归档 id
    broken_links: list[BrokenLink]
    order: list[int]  # 拓扑/稳定排序后的归档 id 序列


# ---------------------------------------------------------------------------
# 输入装配
# ---------------------------------------------------------------------------


def _source_from_row(row: sqlite3.Row) -> ThreadSource:
    message_id: Optional[str] = None
    references: list[str] = []
    in_reply_to: list[str] = []
    subject: Optional[str] = None
    date: Optional[datetime] = None

    for name, value in json_loads_headers(row["headers_json"]):
        lname = name.lower()
        if lname == "message-id":
            if message_id is None:
                ids = extract_msg_ids(value)
                message_id = ids[0] if ids else None
        elif lname == "references":
            for rid in extract_msg_ids(value):
                if rid not in references:
                    references.append(rid)
        elif lname == "in-reply-to":
            for rid in extract_msg_ids(value):
                if rid not in in_reply_to:
                    in_reply_to.append(rid)
        elif lname == "subject":
            if subject is None:
                subject = value
        elif lname == "date":
            if date is None:
                date = parse_date_header(value)

    # 引用链：References 按序在前，In-Reply-To 独有的追加在尾（RFC 5322
    # 建议的语义：In-Reply-To 是回复目标的补充声明）。
    chain = list(references)
    for rid in in_reply_to:
        if rid not in chain:
            chain.append(rid)

    return ThreadSource(
        id=int(row["id"]),
        message_id=message_id,
        chain=tuple(chain),
        subject=subject,
        normalized_subject=normalize_subject(subject) if subject else "",
        date=date,
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# 回复图构建
# ---------------------------------------------------------------------------


def _resolve_parents(
    sources: list[ThreadSource],
) -> tuple[dict[int, int], dict[int, str], list[BrokenLink]]:
    """为每封邮件解析候选父件。

    返回 ``(父指针 child->parent, 父边对应的被引用 ID, 断链列表)``。
    父件 = 引用链中最后一个可解析到归档内邮件的 ID（跳过自引用）；
    重复 Message-ID 解析到最早归档的邮件。
    """

    # 规范化 msg-id -> 声明它的归档 id 列表（升序；取第一个即最早归档）
    id_index: dict[str, list[int]] = {}
    for src in sources:
        if src.message_id:
            id_index.setdefault(src.message_id, []).append(src.id)

    def resolve(ref: str) -> Optional[int]:
        ids = id_index.get(ref)
        return ids[0] if ids else None

    parents: dict[int, int] = {}
    parent_refs: dict[int, str] = {}
    broken: list[BrokenLink] = []

    for src in sources:
        if not src.chain:
            continue
        chosen: Optional[str] = None
        chosen_target: Optional[int] = None
        for ref in reversed(src.chain):
            target = resolve(ref)
            if target is not None and target != src.id:
                chosen = ref
                chosen_target = target
                break
        if chosen is not None:
            parents[src.id] = chosen_target  # type: ignore[assignment]
            parent_refs[src.id] = chosen
            # 链尾（最直接的父件）未能落成父边：即使已借由链上更早的
            # 祖先入链，也保留断链以便定位。
            last = src.chain[-1]
            if last != chosen:
                target = resolve(last)
                if target is None:
                    broken.append(BrokenLink(src.id, last, "missing_parent"))
                elif target == src.id:
                    broken.append(BrokenLink(src.id, last, "cycle"))
            continue
        # 引用链整体无法落成父边：按链尾（最直接父件）判定断链原因。
        last = src.chain[-1]
        if resolve(last) == src.id:
            broken.append(BrokenLink(src.id, last, "cycle"))  # 自引用
        else:
            broken.append(BrokenLink(src.id, last, "missing_parent"))
    return parents, parent_refs, broken


def _find_cycle(parents: dict[int, int]) -> Optional[list[int]]:
    """在父指针森林中找一个环（返回环上节点列表），无环返回 None。

    起点按归档 id 升序遍历，保证检测结果确定。
    """

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in parents}
    for start in sorted(parents):
        if color[start] != WHITE:
            continue
        path: list[int] = []
        cur: Optional[int] = start
        while cur is not None and color.get(cur, BLACK) == WHITE:
            color[cur] = GRAY
            path.append(cur)
            cur = parents.get(cur)
        if cur is not None and color.get(cur) == GRAY:
            return path[path.index(cur):]
        for node in path:
            color[node] = BLACK
    return None


def _break_cycles(
    parents: dict[int, int],
    parent_refs: dict[int, str],
    sort_keys: dict[int, tuple[datetime, int]],
    broken: list[BrokenLink],
) -> None:
    """逐个检测并断开循环引用，断开的边记为 ``cycle`` 断链。

    每个环断开**排序键最小**（时间最早、id 最小）节点的父边——环中
    最早的邮件却引用环内更晚的邮件作为父件，是最可疑的引用；断开后
    最早邮件成为根，符合会话的时间走向。选择规则固定，结果确定。
    """

    while True:
        cycle = _find_cycle(parents)
        if cycle is None:
            return
        victim = min(cycle, key=lambda i: (sort_keys[i], i))
        ref = parent_refs.pop(victim)
        parents.pop(victim)
        broken.append(BrokenLink(victim, ref, "cycle"))


def _components(
    member_ids: list[int], parents: dict[int, int]
) -> list[list[int]]:
    """父子边（视为无向）的连通分量；分量内按归档 id 升序。"""

    adjacency: dict[int, set[int]] = {i: set() for i in member_ids}
    for child, parent in parents.items():
        adjacency[child].add(parent)
        adjacency[parent].add(child)

    seen: set[int] = set()
    groups: list[list[int]] = []
    for start in sorted(member_ids):
        if start in seen:
            continue
        group: list[int] = []
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            group.append(node)
            for nxt in sorted(adjacency[node], reverse=True):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        groups.append(sorted(group))
    return groups


def _topological_order(
    member_ids: list[int],
    parents: dict[int, int],
    sort_keys: dict[int, tuple[datetime, int]],
) -> list[int]:
    """会话内拓扑排序：父在子前；并列按 (时间, 归档 id) 稳定排序。

    Kahn 算法，就绪集用排序键最小优先（等价于对森林逐层取最小键）。
    输入保证无环（环已在 :func:`_break_cycles` 中断开）。
    """

    members = set(member_ids)
    children: dict[int, list[int]] = {i: [] for i in member_ids}
    for child, parent in parents.items():
        if child in members and parent in members:
            children[parent].append(child)
    for parent in children:
        children[parent].sort(key=lambda i: (sort_keys[i], i))

    ready = sorted(
        (i for i in member_ids if i not in parents),
        key=lambda i: (sort_keys[i], i),
    )
    order: list[int] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for child in children[node]:
            # 树结构中子件只在父件出队后就绪
            ready.append(child)
        ready.sort(key=lambda i: (sort_keys[i], i))
    return order


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_thread(rows: list[sqlite3.Row], target_id: int) -> ThreadResult:
    """在全部归档邮件上构建回复图，返回 ``target_id`` 所在的会话。

    ``rows`` 为 :func:`database.list_thread_sources` 的结果（含全部
    归档邮件；行序无关）。``target_id`` 必须存在于 ``rows`` 中。
    """

    sources = [_source_from_row(row) for row in rows]
    # 与行序无关：统一按归档 id 升序，保证重复 Message-ID 解析到最早归档、
    # 断链检测顺序等全部确定。
    sources.sort(key=lambda s: s.id)
    by_id = {s.id: s for s in sources}
    sort_keys = {s.id: s.sort_key for s in sources}

    parents, parent_refs, broken = _resolve_parents(sources)
    _break_cycles(parents, parent_refs, sort_keys, broken)

    # 有引用关系的邮件按连通分量分组；孤立邮件按规范化主题分组，
    # 空主题的孤立邮件各自成组（不把所有无主题邮件混为一组）。
    involved = set(parents) | set(parents.values())
    groups = _components(sorted(involved), parents)

    subject_groups: dict[str, list[int]] = {}
    for src in sources:
        if src.id in involved:
            continue
        if src.normalized_subject:
            subject_groups.setdefault(src.normalized_subject, []).append(src.id)
        else:
            groups.append([src.id])
    for key in sorted(subject_groups):
        groups.append(sorted(subject_groups[key]))

    target_group = next(g for g in groups if target_id in g)
    order = _topological_order(target_group, parents, sort_keys)

    member_set = set(target_group)
    group_broken = sorted(
        (b for b in broken if b.message_id in member_set),
        key=lambda b: (b.message_id, b.referenced_message_id, b.reason),
    )
    return ThreadResult(
        thread_id=order[0],
        members=[by_id[i] for i in order],
        parents={c: p for c, p in parents.items() if c in member_set},
        broken_links=group_broken,
        order=order,
    )
