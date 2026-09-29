"""``cid:`` 内嵌资源引用审计（RFC 2392 / HTML 4 与 HTML 5）。

归档邮件里的 HTML 叶子常用 ``cid:`` URL 引用同一
``multipart/related``（RFC 2387）结构中的兄弟部分，后者以
``Content-ID`` 头声明自己的标识符。本模块在一封已归档邮件的
MIME 节点表上完成纯静态审计：

1. 从 **text/html 叶子** 的 ``src`` / ``href`` / ``poster`` /
   ``srcset`` 属性（按文档序）提取 ``cid:`` 引用，并按 RFC 2392
   §2 做百分号解码（先按 ``%HH`` 还原字节，再按 UTF-8 解码，
   非法序列以 U+FFFD 兜底——审计场景不拒收脏引用）；
2. 作用域严格限定为引用节点 **最近的 ``multipart/related`` 祖先**：
   只在该祖先的其他后代中匹配 ``Content-ID``，不向相邻兄弟
   ``multipart/related``、也不向内层嵌套的 ``multipart/related``
   串联；
3. 同作用域内同一 Content-ID 出现多次（歧义重名）一律判
   ``ambiguous``；未命中判 ``missing``；唯一命中判 ``resolved``；
4. 每个作用域额外列出未被该作用域内任何 HTML 叶子引用的
   Content-ID 节点（orphan）。

``Content-ID`` 头的规范化：RFC 2045 要求其值为
``"<" addr-spec ">"``，故去掉首尾空白与一层尖括号；实际邮件中
裸 ID 也常见，裸值按原样使用。``cid:`` 一侧做对称的尖括号容忍，
使 ``cid:foo``、``cid:<foo>``、``cid:%3Cfoo%3E`` 都能匹配
``<foo>``。
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import unquote_to_bytes

# 需要扫描的 HTML 属性（HTML 4/HTML 5 中可承载资源 URL 的四个属性）。
_CID_ATTRS = ("src", "href", "poster", "srcset")
_CID_PREFIX = "cid:"


# ---------------------------------------------------------------------------
# Content-ID / cid: 规范化
# ---------------------------------------------------------------------------


def normalize_content_id(raw: str) -> str:
    """规范化 ``Content-ID`` 头值以便比较。

    去线性空白与首尾空白，再剥掉 RFC 2045 规定的一层尖括号；
    裸 ID 按原样接受。结果再做 NFC。空串表示“无有效 ID”。
    """

    value = raw.strip()
    if len(value) >= 2 and value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return unicodedata.normalize("NFC", value)


def parse_cid_ref(value: str) -> Optional[str]:
    """解析一个属性值中的 ``cid:`` 引用，返回规范化后的 Content-ID。

    * 不是 ``cid:`` scheme（含 ``http:``、``data:``、``mailto:`` 等）
      返回 ``None``；
    * 按 RFC 2392 §2 百分号解码（``unquote_to_bytes`` 对非法转义
      保持字面），字节按 UTF-8 解码（非法字节以 U+FFFD 兜底），
      再 NFC；
    * 对称容忍一层尖括号，使 ``cid:<foo>`` 与裸 ``<foo>``
      Content-ID 也能匹配。
    """

    stripped = value.strip()
    if stripped[:4].lower() != _CID_PREFIX:
        return None
    target = stripped[4:]
    raw_bytes = unquote_to_bytes(target)
    decoded = raw_bytes.decode("utf-8", "replace")
    decoded = decoded.strip()
    if len(decoded) >= 2 and decoded.startswith("<") and decoded.endswith(">"):
        decoded = decoded[1:-1].strip()
    return unicodedata.normalize("NFC", decoded)


def split_srcset(value: str) -> list[str]:
    """从 ``srcset`` 属性值切出候选 URL。

    HTML 5 §4.8.4.7：候选以逗号分隔，``URL descriptor`` 形态为
    ``URL [宽度/像素密度描述符]``。``cid:`` URL 本身不含裸逗号
    （逗号会被百分号编码），故按逗号切分、取每段首个空白前的
    token 即足够覆盖归档审计场景。
    """

    urls: list[str] = []
    for candidate in value.split(","):
        token = candidate.strip()
        if not token:
            continue
        url = token.split()[0] if token.split() else token
        urls.append(url)
    return urls


# ---------------------------------------------------------------------------
# HTML 扫描
# ---------------------------------------------------------------------------


class _CidHTMLParser(HTMLParser):
    """按文档序收集四个属性中的全部 ``cid:`` 引用（含重复属性）。

    输出为 ``(属性名, 解码后的 cid)``，保留属性名以便审计响应展示。
    """

    def __init__(self) -> None:
        # convert_charrefs=True（默认）：属性中的 HTML 实体先被解码，
        # 例如 &amp; 会还原成 &；百分号序列不受实体解码影响。
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        for name, value in attrs:
            if value is None or name not in _CID_ATTRS:
                continue
            if name == "srcset":
                for url in split_srcset(value):
                    self._collect(name, url)
            else:
                self._collect(name, value)

    # XHTML 自闭合标签：显式复用开始标签逻辑，不依赖父类默认转发。
    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def _collect(self, attribute: str, value: str) -> None:
        cid = parse_cid_ref(value)
        if cid is not None:
            self.found.append((attribute, cid))


def _extract_cids_with_attrs(html: str) -> list[tuple[str, str]]:
    """从 HTML 文本中按文档序提取 ``(属性名, 解码后 cid)``。"""

    parser = _CidHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.found


def extract_cids_from_html(html: str) -> list[str]:
    """从 text/html 文本中按文档序提取全部 ``cid:`` 目标 ID。"""

    return [cid for _attr, cid in _extract_cids_with_attrs(html)]


# ---------------------------------------------------------------------------
# 作用域审计
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelatedReference:
    """一条 cid: 引用的审计结果。"""

    source_node_path: str
    attribute: str
    cid: str  # RFC 2392 百分号解码后的目标 Content-ID
    scope_node_path: Optional[str]  # 最近 multipart/related 祖先；无则 None
    status: str  # resolved | missing | ambiguous
    target_node_path: Optional[str] = None  # resolved 时
    candidates: list[str] = field(default_factory=list)  # ambiguous 时全部候选


@dataclass
class _Scope:
    node_path: str
    # Content-ID -> 声明它的节点路径（文档序）；长度>1 即歧义重名
    cid_index: dict[str, list[str]] = field(default_factory=dict)
    # 作用域内每个带 Content-ID 的节点及其全部（去重后）ID
    resource_cids: dict[str, list[str]] = field(default_factory=dict)
    referenced: set[str] = field(default_factory=set)


def _is_related(content_type: str) -> bool:
    return content_type.lower() == "multipart/related"


def _nearest_related_ancestor(
    path: str, related_paths: list[str]
) -> Optional[str]:
    """返回物化路径 ``path`` 最近的 multipart/related 祖先路径。

    祖先链包含根（``""``）：``"1/2/3"`` 的链为
    ``""``、``"1"``、``"1/2"``。取链上（严格祖先）最深的命中者；
    路径自身命中（容器自身的 Content-ID）不算——容器属于其外层
    作用域，不属于它自己开启的作用域。
    """

    chain: list[str] = [""]
    segments = path.split("/") if path else []
    for depth in range(1, len(segments)):
        chain.append("/".join(segments[:depth]))
    best: Optional[str] = None
    for ancestor in chain:
        if ancestor in related_paths and ancestor != path:
            best = ancestor  # 链由浅入深遍历，后命中者更近
    return best


def audit_related_resources(
    rows: list[sqlite3.Row],
) -> tuple[list[RelatedReference], list[dict]]:
    """审计一封邮件全部节点中的 cid: 引用。

    ``rows`` 为 ``database.list_nodes`` 的结果（按文档序）。
    返回 ``(引用结果列表（文档序）, 作用域列表（文档序）)``，
    作用域 dict 形如
    ``{"node_path": ..., "orphans": [{"node_path", "content_type",
    "content_ids": [...]}]}``。
    """

    by_path = {r["path"]: r for r in rows}

    # 1) 找全部 multipart/related 容器（自身也是作用域边界，其后代
    #    只向最近祖先归属，因此容器自身的 Content-ID 不属于该作用域）。
    related_paths = [r["path"] for r in rows if _is_related(r["content_type"])]
    scopes: dict[str, _Scope] = {p: _Scope(node_path=p) for p in related_paths}

    # 2) 节点归属：收集 Content-ID 资源与 text/html 叶子。
    html_in_scope: dict[str, list[str]] = {p: [] for p in related_paths}
    unscoped_html: list[str] = []
    for row in rows:
        path = row["path"]
        scope_path = _nearest_related_ancestor(path, related_paths)

        cids: list[str] = []
        for name, value in json_loads_headers(row["headers_json"]):
            if name.lower() == "content-id":
                cid = normalize_content_id(value)
                if cid and cid not in cids:
                    cids.append(cid)

        if cids and scope_path is not None:
            scope = scopes[scope_path]
            scope.resource_cids[path] = cids
            for cid in cids:
                scope.cid_index.setdefault(cid, []).append(path)
        # 带 Content-ID 但不在任何 related 作用域中的节点：无可匹配
        # 的 HTML（HTML 也必须在同作用域），不进入任何 orphan 列表。

        if row["kind"] == "text" and row["content_type"].lower() == "text/html":
            if scope_path is not None:
                html_in_scope[scope_path].append(path)
            else:
                unscoped_html.append(path)

    # 3) 逐作用域解析引用。
    references: list[RelatedReference] = []
    scope_results: list[dict] = []

    for scope_path in related_paths:
        scope = scopes[scope_path]
        for html_path in html_in_scope[scope_path]:
            html = by_path[html_path]["text_content"] or ""
            for attribute, cid in _extract_cids_with_attrs(html):
                targets = scope.cid_index.get(cid, [])
                if len(targets) == 1:
                    references.append(
                        RelatedReference(
                            source_node_path=html_path,
                            attribute=attribute,
                            cid=cid,
                            scope_node_path=scope_path,
                            status="resolved",
                            target_node_path=targets[0],
                        )
                    )
                    scope.referenced.add(cid)
                elif len(targets) > 1:
                    references.append(
                        RelatedReference(
                            source_node_path=html_path,
                            attribute=attribute,
                            cid=cid,
                            scope_node_path=scope_path,
                            status="ambiguous",
                            candidates=targets,
                        )
                    )
                    # 被引用但无法唯一落定：歧义候选不算 orphan
                    scope.referenced.add(cid)
                else:
                    references.append(
                        RelatedReference(
                            source_node_path=html_path,
                            attribute=attribute,
                            cid=cid,
                            scope_node_path=scope_path,
                            status="missing",
                        )
                    )

        # orphan = 作用域内带 Content-ID 且其全部 ID 均未被本作用域
        # HTML 引用过的节点。ambiguous 的候选被“引用”过（只是无法
        # 唯一落定），不算 orphan。
        orphans: list[dict] = []
        for node_path in scope.resource_cids:  # 插入序即文档序
            cids = scope.resource_cids[node_path]
            if not any(c in scope.referenced for c in cids):
                orphans.append(
                    {
                        "node_path": node_path,
                        "content_type": by_path[node_path]["content_type"],
                        "content_ids": cids,
                    }
                )
        scope_results.append({"node_path": scope_path, "orphans": orphans})

    # 4) 不属于任何 related 作用域的 HTML 叶子：其引用一律 missing，
    #    不产生作用域条目。
    for html_path in unscoped_html:
        html = by_path[html_path]["text_content"] or ""
        for attribute, cid in _extract_cids_with_attrs(html):
            references.append(
                RelatedReference(
                    source_node_path=html_path,
                    attribute=attribute,
                    cid=cid,
                    scope_node_path=None,
                    status="missing",
                )
            )

    return references, scope_results


def json_loads_headers(raw: str) -> list[tuple[str, str]]:
    """节点 headers_json 形如 ``[["Name", "value"], ...]``。"""

    return [(str(name), str(value)) for name, value in json.loads(raw)]
