"""两封已归档邮件的只读 MIME 树比较（``POST /messages/compare``）。

输入为两封邮件各自的 :func:`database.list_nodes` 节点行（按 MIME
文档序），输出稳定的节点差异（新增 / 删除 / 变更 / 歧义）。比较是
**纯只读**的：不回写任何记录，同一对邮件重复请求结果逐字节稳定。

比较口径与指纹规范化（:mod:`app.mimeparser`）保持一致：

* 节点身份不靠物化路径——兄弟节点重排时路径会整体改变，路径不是
  身份。每层按**同层内容指纹**配对：内容指纹即
  :func:`app.mimeparser.canonical_tree` 语义下该节点子树的规范 JSON
  的 SHA-256（规范化 Content-Type、规范化头字段、解码叶子摘要，剔除
  boundary / charset / CTE 等打包噪音）；
* 某指纹在双方各出现一次 → 跨位置唯一配对（识别重排），随后递归
  比较该对子树；
* 同层双方都还在、但该指纹在任一方出现多次（如两个字节完全相同的
  附件互换），无法唯一配对 → 整组标记 ``ambiguous``，列出双方全部
  候选路径，不强行按位置落对；
* 唯一配对消费后剩余的节点按 MIME 文档序（1..n）对齐：同位置递归
  配对（叶子不同时表现为 ``changed``），数量多出的一侧依次记
  ``added`` / ``removed``（只报子树根，并给子树节点数）。

配对后的节点逐面（facet）比较四个规范化侧面：

* ``content_type`` —— ``{media_type, params}``，参数排序并剔除
  ``boundary``（容器）与 ``charset``（内容已解码）；
* ``disposition`` —— ``{type, params}`` 或 ``null``，参数为
  RFC 2231 解码后排序结果；
* ``headers`` —— 规范化头字段 ``[[小写名, 线性空白压缩后的解码值],
  ...]``，三个结构头（Content-Type / Content-Disposition /
  Content-Transfer-Encoding）不在这里，CTE 不参与比较；
* ``leaf`` —— 文本叶子 ``{kind:"text", text_sha256, length}``，
  非文本叶子 ``{kind:"blob", blob_sha256, length}``，容器为 ``null``。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from .mimeparser import (
    MIMEParseError,
    canonical_json,
    parse_content_type,
    parse_disposition,
)

_STRUCTURAL_HEADERS = {
    "content-type",
    "content-transfer-encoding",
    "content-disposition",
}
_NOISY_CT_PARAMS = {"boundary", "charset"}

FACET_CONTENT_TYPE = "content_type"
FACET_DISPOSITION = "disposition"
FACET_HEADERS = "headers"
FACET_LEAF = "leaf"
# changed 项中各侧面的稳定输出顺序
FACET_ORDER = (FACET_CONTENT_TYPE, FACET_DISPOSITION, FACET_HEADERS, FACET_LEAF)

DIFF_ADDED = "added"
DIFF_REMOVED = "removed"
DIFF_CHANGED = "changed"
DIFF_AMBIGUOUS = "ambiguous"
_DIFF_RANK = {
    DIFF_CHANGED: 0,
    DIFF_AMBIGUOUS: 1,
    DIFF_REMOVED: 2,
    DIFF_ADDED: 3,
}


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass
class Diff:
    """一条节点差异。

    ``changed``：双方各一个节点（``a_path`` / ``b_path``），
    ``facets`` 给出有差异的侧面；
    ``added`` / ``removed``：只在一侧出现的子树根，``node_count``
    为该子树的节点总数；
    ``ambiguous``：同层同指纹但任一侧出现多次、无法唯一配对，
    ``a_paths`` / ``b_paths`` 列出双方全部候选。
    """

    kind: str
    a_paths: list[str] = field(default_factory=list)
    b_paths: list[str] = field(default_factory=list)
    facets: list[dict] = field(default_factory=list)
    node_count: Optional[int] = None
    fingerprint: Optional[str] = None
    content_type: Optional[str] = None

    @property
    def a_path(self) -> Optional[str]:
        return self.a_paths[0] if len(self.a_paths) == 1 else None

    @property
    def b_path(self) -> Optional[str]:
        return self.b_paths[0] if len(self.b_paths) == 1 else None


@dataclass
class Comparison:
    diffs: list[Diff]


# ---------------------------------------------------------------------------
# 规范化辅助
# ---------------------------------------------------------------------------


def _normalize_wsp(text: str) -> str:
    """连续线性空白压成一个空格并去两端（与指纹口径一致）。"""

    return re.sub(r"[ \t]+", " ", text).strip(" ")


def _raw_header_bytes(value: str) -> bytes:
    """把归档时已解码的结构头值还原为可重新解析的原始字节。

    结构头（Content-Type / Content-Disposition）按 RFC 只含 ASCII
    标记与 RFC 2231 百分号字节，latin-1 可无损往返；仅当历史脏数据
    在结构头里直接出现非 latin-1 字符时回退 UTF-8（重新解析会失败，
    调用方改用兜底口径）。
    """

    try:
        return value.encode("latin-1")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace")


def _stored_header(row: sqlite3.Row, name: str) -> Optional[str]:
    for raw_name, value in json.loads(row["headers_json"]):
        if raw_name.lower() == name:
            return value
    return None


def _canon_headers(row: sqlite3.Row) -> list[list[str]]:
    """规范化头字段：名小写、值线性空白压缩；剔除三个结构头。"""

    lines: list[list[str]] = []
    for raw_name, value in json.loads(row["headers_json"]):
        name = raw_name.lower()
        if name in _STRUCTURAL_HEADERS:
            continue
        lines.append([name, _normalize_wsp(value)])
    return lines


def _parsed_content_type(row: sqlite3.Row):
    """重新解析存储的 Content-Type 头（含 RFC 2231 参数）。

    无该头时按 RFC 2045 缺省 text/plain；脏头无法重新解析时返回
    None，调用方退回只含媒体类型的兜底口径。
    """

    value = _stored_header(row, "content-type")
    if value is None:
        class _DefaultCT:
            media_type = "text/plain"
            params: dict[str, str] = {}

        return _DefaultCT()
    try:
        return parse_content_type(_raw_header_bytes(value), row["path"])
    except MIMEParseError:
        return None


def _parsed_disposition(row: sqlite3.Row) -> Optional[tuple[str, dict[str, str]]]:
    """重新解析 Content-Disposition 头。

    无 disposition（节点行 ``disposition`` 为空）返回 None；脏头无法
    重新解析时退回 ``(存储的 disposition 主值, {})`` 兜底口径。
    """

    if not row["disposition"]:
        return None
    value = _stored_header(row, "content-disposition")
    if value is None:  # 理论不可达：disposition 来自该头解析
        return row["disposition"], {}
    try:
        return parse_disposition(_raw_header_bytes(value), row["path"])
    except MIMEParseError:
        return row["disposition"], {}


# ---------------------------------------------------------------------------
# 比较侧面（facet）
# ---------------------------------------------------------------------------


def _content_type_facet(row: sqlite3.Row) -> dict:
    ct = _parsed_content_type(row)
    if ct is None:
        return {"media_type": row["content_type"], "params": {}}
    params = {
        key: val
        for key, val in ct.params.items()
        if key not in _NOISY_CT_PARAMS
    }
    return {"media_type": ct.media_type, "params": dict(sorted(params.items()))}


def _disposition_facet(row: sqlite3.Row) -> Optional[dict]:
    if not row["disposition"]:
        return None
    parsed = _parsed_disposition(row)
    if parsed is None:  # 理论不可达：disposition 非空必有对应头
        return {"type": row["disposition"], "params": {}}
    disp_type, params = parsed
    return {"type": disp_type, "params": dict(sorted(params.items()))}


def _leaf_facet(row: sqlite3.Row) -> Optional[dict]:
    kind = row["kind"]
    if kind == "text":
        text = row["text_content"] if row["text_content"] is not None else ""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {"kind": "text", "text_sha256": digest, "length": len(text)}
    if kind == "blob":
        return {
            "kind": "blob",
            "blob_sha256": row["blob_sha256"],
            "length": row["blob_length"],
        }
    return None  # multipart / message 容器


def _facets(row: sqlite3.Row) -> dict[str, Any]:
    return {
        FACET_CONTENT_TYPE: _content_type_facet(row),
        FACET_DISPOSITION: _disposition_facet(row),
        FACET_HEADERS: _canon_headers(row),
        FACET_LEAF: _leaf_facet(row),
    }


# ---------------------------------------------------------------------------
# 子树内容指纹（canonical_tree 同口径，从存储行重建）
# ---------------------------------------------------------------------------


def _canonical_subtree(row: sqlite3.Row, by_path: dict[str, sqlite3.Row]) -> dict:
    """按 :func:`app.mimeparser.canonical_tree` 的结构重建规范子树。

    根指纹因此与归档时保存的 ``messages.fingerprint`` 完全一致；
    内部节点也可独立取指纹用于同层配对。
    """

    entry: dict = {
        "content_type": row["content_type"],
        "headers": _canon_headers(row),
    }
    children_paths = json.loads(row["children_json"])
    if children_paths:
        entry["kind"] = (
            "multipart" if row["kind"] == "multipart" else "message"
        )
        entry["children"] = [
            _canonical_subtree(by_path[p], by_path) for p in children_paths
        ]
        return entry

    entry["kind"] = "leaf"
    ct = _parsed_content_type(row)
    if ct is not None:
        params = {
            key: val
            for key, val in ct.params.items()
            if key not in _NOISY_CT_PARAMS
        }
        if params:
            entry["content_type_params"] = dict(sorted(params.items()))
    if row["disposition"]:
        disp: dict = {"type": row["disposition"]}
        parsed = _parsed_disposition(row)
        if parsed is not None:
            _disp_type, disp_params = parsed
            if disp_params:
                disp["params"] = dict(sorted(disp_params.items()))
        entry["disposition"] = disp

    if row["kind"] == "text":
        entry["text"] = (
            row["text_content"] if row["text_content"] is not None else ""
        )
    else:
        entry["blob_sha256"] = row["blob_sha256"]
        entry["blob_length"] = row["blob_length"]
    return entry


class _Index:
    """一封邮件节点行的只读索引，缓存每节点的子树指纹与侧面值。"""

    def __init__(self, rows: list[sqlite3.Row]):
        self.by_path = {r["path"]: r for r in rows}
        self.root = next(r for r in rows if r["path"] == "")
        self._fps: dict[str, str] = {}
        self._facets: dict[str, dict] = {}

    def children(self, row: sqlite3.Row) -> list[sqlite3.Row]:
        return [self.by_path[p] for p in json.loads(row["children_json"])]

    def fingerprint(self, row: sqlite3.Row) -> str:
        fp = self._fps.get(row["path"])
        if fp is None:
            canon = canonical_json(_canonical_subtree(row, self.by_path))
            fp = hashlib.sha256(canon).hexdigest()
            self._fps[row["path"]] = fp
        return fp

    def facets(self, row: sqlite3.Row) -> dict:
        facets = self._facets.get(row["path"])
        if facets is None:
            facets = _facets(row)
            self._facets[row["path"]] = facets
        return facets

    def subtree_count(self, row: sqlite3.Row) -> int:
        total = 0
        stack = [row]
        while stack:
            cur = stack.pop()
            total += 1
            children = self.children(cur)
            stack.extend(reversed(children))
        return total


# ---------------------------------------------------------------------------
# 递归对齐
# ---------------------------------------------------------------------------


def _compare_pair(
    ra: sqlite3.Row,
    rb: sqlite3.Row,
    ia: _Index,
    ib: _Index,
    out: list[Diff],
) -> None:
    """比较一对已配对节点；子树完全相同时静默返回。"""

    if ia.fingerprint(ra) == ib.fingerprint(rb):
        return

    facet_a = ia.facets(ra)
    facet_b = ib.facets(rb)
    changes = [
        {"facet": name, "a": facet_a[name], "b": facet_b[name]}
        for name in FACET_ORDER
        if facet_a[name] != facet_b[name]
    ]
    if changes:
        out.append(
            Diff(
                kind=DIFF_CHANGED,
                a_paths=[ra["path"]],
                b_paths=[rb["path"]],
                facets=changes,
                content_type=ra["content_type"],
            )
        )

    children_a = ia.children(ra)
    children_b = ib.children(rb)
    # 容器 ↔ 叶子（或容器类型改变）的跨结构配对：只有一侧有子节点时，
    # 该侧整层子节点在对齐中全部成为新增/删除。
    if children_a or children_b:
        _align_children(children_a, children_b, ia, ib, out)


def _align_children(
    children_a: list[sqlite3.Row],
    children_b: list[sqlite3.Row],
    ia: _Index,
    ib: _Index,
    out: list[Diff],
) -> None:
    """同层配对：先按内容指纹唯一配对，再按文档序位置兜底。"""

    groups_a: dict[str, list[sqlite3.Row]] = {}
    groups_b: dict[str, list[sqlite3.Row]] = {}
    for row in children_a:
        groups_a.setdefault(ia.fingerprint(row), []).append(row)
    for row in children_b:
        groups_b.setdefault(ib.fingerprint(row), []).append(row)

    common = sorted(set(groups_a) & set(groups_b))
    used_a: set[str] = set()
    used_b: set[str] = set()

    # 1) 无法唯一配对的同指纹组：任一侧出现多次即歧义，列出双方全部
    #    候选，不再按位置强行落对（组内节点从后续位置对齐中剔除）。
    for fp in common:
        ga, gb = groups_a[fp], groups_b[fp]
        if len(ga) == 1 and len(gb) == 1:
            continue
        out.append(
            Diff(
                kind=DIFF_AMBIGUOUS,
                a_paths=[r["path"] for r in ga],
                b_paths=[r["path"] for r in gb],
                fingerprint=fp,
                content_type=ga[0]["content_type"],
            )
        )
        used_a.update(r["path"] for r in ga)
        used_b.update(r["path"] for r in gb)

    # 2) 双方各一次的同指纹：跨位置唯一配对（识别重排），递归比较。
    for fp in common:
        ga, gb = groups_a[fp], groups_b[fp]
        if not (len(ga) == 1 and len(gb) == 1):
            continue
        ra, rb = ga[0], gb[0]
        used_a.add(ra["path"])
        used_b.add(rb["path"])
        _compare_pair(ra, rb, ia, ib, out)

    # 3) 剩余节点按 MIME 文档序（1..n）位置对齐：同位置配对递归，
    #    数量多出的一侧依次为删除 / 新增子树。
    leftover_a = [r for r in children_a if r["path"] not in used_a]
    leftover_b = [r for r in children_b if r["path"] not in used_b]
    for i in range(max(len(leftover_a), len(leftover_b))):
        if i < len(leftover_a) and i < len(leftover_b):
            _compare_pair(leftover_a[i], leftover_b[i], ia, ib, out)
        elif i < len(leftover_a):
            row = leftover_a[i]
            out.append(
                Diff(
                    kind=DIFF_REMOVED,
                    a_paths=[row["path"]],
                    node_count=ia.subtree_count(row),
                    content_type=row["content_type"],
                )
            )
        else:
            row = leftover_b[i]
            out.append(
                Diff(
                    kind=DIFF_ADDED,
                    b_paths=[row["path"]],
                    node_count=ib.subtree_count(row),
                    content_type=row["content_type"],
                )
            )


# ---------------------------------------------------------------------------
# 稳定排序与入口
# -----------


def _path_segments(path: str) -> tuple[int, ...]:
    return tuple(int(seg) for seg in path.split("/")) if path else ()


def _sort_key(diff: Diff) -> tuple:
    """全局稳定序：按所在节点的文档序，同层 changed → ambiguous →
    removed → added。新增项无 a 路径，借 b 路径定位。"""

    rank = _DIFF_RANK[diff.kind]
    primary_paths = diff.a_paths or diff.b_paths
    secondary_paths = diff.b_paths or diff.a_paths
    primary = _path_segments(primary_paths[0])
    secondary = _path_segments(secondary_paths[0])
    return (primary, rank, secondary)


def compare_messages(
    rows_a: list[sqlite3.Row], rows_b: list[sqlite3.Row]
) -> list[Diff]:
    """比较两封已归档邮件的节点行，返回排好序的稳定差异列表。

    两邮件整体指纹相同时返回空列表（所有子树递归均无差异）。
    """

    ia, ib = _Index(rows_a), _Index(rows_b)
    out: list[Diff] = []
    _compare_pair(ia.root, ib.root, ia, ib, out)
    out.sort(key=_sort_key)
    return out
