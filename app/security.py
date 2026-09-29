"""已归档 MIME 树的附件安全策略评估（``GET /messages/{id}/security-report``）。

本模块是**纯只读**的确定性评估：输入为 :func:`database.list_nodes`
返回的节点行，不回写任何记录、不依赖归档时未保存的派生数据，对历史
归档数据同样适用。评估只依赖节点行与显式传入的阈值/策略集合，因此
同一归档重复请求得到逐字节稳定的结果。

按节点物化路径做**文档序**（先根后子、兄弟按 1..n）遍历，递归累计：

* ``decoded_bytes`` —— 全部**非文本叶子**（``kind == "blob"``，与
  内容寻址附件库口径一致的“附件”节点）的解码后字节数
  （``blob_length``）之和；
* ``attachment_count`` —— 非文本叶子个数（同一内容在一封邮件出现
  多次按多个附件计数）；
* ``max_depth`` —— 树中节点的最大 MIME 深度（根为 0）。

每个附件节点检查：

* **双扩展文件名**（``DOUBLE_EXTENSION``）：basename 含两个及以上
  非空扩展名段（``invoice.pdf.exe`` 的 ``["pdf","exe"]``；
  ``archive.tar.gz`` 这类同样是保守策略下的双扩展命中，首尾的空
  段忽略）；
* **文件名控制字符**（``FILENAME_CONTROL_CHARS``）：文件名含 Unicode
  ``Cc`` 类字符（C0 控制字符含 NUL/TAB/换行、DEL 与 C1 控制字符），
  ``detail.codepoints`` 按出现序列出去重码点；
* **禁止媒体类型**（``FORBIDDEN_MEDIA_TYPE``）：附件节点的
  Content-Type 命中内置可执行类型黑名单（大小写无关的精确匹配）。

另外两类与结构/阈值相关的规则：

* **嵌套封装报文**（``NESTED_MESSAGE``）：深度 ≥ 1 处出现
  ``message/rfc822`` / ``message/global`` 容器（根自身是封装报文
  不算嵌套）；
* 三个**阈值**：``DECODED_BYTES_EXCEEDED`` /
  ``ATTACHMENT_COUNT_EXCEEDED`` / ``MIME_DEPTH_EXCEEDED``。阈值采用
  “严格大于”语义（恰好等于阈值不命中）；每条阈值规则只报告**首次
  越过阈值的节点**——字节/附件数为在文档序上把累计值推过阈值的
  附件节点，深度为文档序第一个深于阈值的节点。

文件名取节点 ``Content-Disposition`` 的 ``filename``（含 RFC 2231
``filename*=``），缺省时回退 ``Content-Type`` 的 ``name``；两者都
没有的附件节点不做文件名类检查。归档只留存了解码头值字符串，这里
用归档器自己的严格参数解析器重新解析取参数——纯读、不碰原始记录。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field

from .mimeparser import parse_content_type, parse_disposition
from .related import json_loads_headers

# ---------------------------------------------------------------------------
# 默认策略
# ---------------------------------------------------------------------------

# 单个邮件全部附件的解码字节合计上限（10 MiB）。
DEFAULT_MAX_DECODED_BYTES = 10 * 1024 * 1024
# 单封邮件的附件数量上限。
DEFAULT_MAX_ATTACHMENT_COUNT = 100
# MIME 树深度上限（根为 0）。
DEFAULT_MAX_MIME_DEPTH = 20

# 内置禁止媒体类型：常见可执行/安装/快捷方式载体（精确匹配，小写）。
DEFAULT_FORBIDDEN_MEDIA_TYPES = frozenset(
    {
        "application/java-archive",
        "application/java-vm",
        "application/vnd.microsoft.portable-executable",
        "application/x-msdos-program",
        "application/x-ms-shortcut",
        "application/x-msi",
        "application/x-msdownload",
    }
)

# 稳定的违规规则码
CODE_DOUBLE_EXTENSION = "DOUBLE_EXTENSION"
CODE_FILENAME_CONTROL_CHARS = "FILENAME_CONTROL_CHARS"
CODE_FORBIDDEN_MEDIA_TYPE = "FORBIDDEN_MEDIA_TYPE"
CODE_NESTED_MESSAGE = "NESTED_MESSAGE"
CODE_DECODED_BYTES_EXCEEDED = "DECODED_BYTES_EXCEEDED"
CODE_ATTACHMENT_COUNT_EXCEEDED = "ATTACHMENT_COUNT_EXCEEDED"
CODE_MIME_DEPTH_EXCEEDED = "MIME_DEPTH_EXCEEDED"


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityViolation:
    """一条策略命中。"""

    code: str
    node_path: str
    detail: dict


@dataclass(frozen=True)
class SecurityTotals:
    """整棵树的递归累计量。"""

    decoded_bytes: int
    attachment_count: int
    max_depth: int


@dataclass
class SecurityReport:
    violations: list[SecurityViolation] = field(default_factory=list)
    totals: SecurityTotals = SecurityTotals(0, 0, 0)

    @property
    def allow(self) -> bool:
        """无任何违规即放行。"""

        return not self.violations


# ---------------------------------------------------------------------------
# 路径排序与文件名解析
# ---------------------------------------------------------------------------


def path_order_key(path: str) -> tuple[int, ...]:
    """物化路径的文档序键：``""`` → ()，``"1/2"`` → (1, 2)。"""

    if not path:
        return ()
    return tuple(int(seg) for seg in path.split("/"))


def attachment_filename(row: sqlite3.Row) -> str | None:
    """取附件节点的解码文件名：CD ``filename`` 优先，回退 CT ``name``。

    复用归档器的严格 RFC 2045/2231 解析器重新解析已保存的头值；
    能入库的头值在归档时已通过同一解析器校验。
    """

    cd_value: str | None = None
    ct_value: str | None = None
    for name, value in json_loads_headers(row["headers_json"]):
        lname = name.lower()
        if lname == "content-disposition":
            cd_value = value
        elif lname == "content-type":
            ct_value = value

    if cd_value is not None:
        _, params = parse_disposition(cd_value.encode("utf-8"), row["path"])
        filename = params.get("filename")
        if filename:
            return filename
    if ct_value is not None:
        ct = parse_content_type(ct_value.encode("utf-8"), row["path"])
        name = ct.params.get("name")
        if name:
            return name
    return None


def split_extensions(filename: str) -> list[str]:
    """取 basename 的全部非空扩展名段。

    ``"invoice.pdf.exe"`` → ``["pdf", "exe"]``；``".bashrc"`` →
    ``["bashrc"]``；``"a."`` → ``[]``。兼容文件名中夹带的路径分隔符
    （只看最后一段）。
    """

    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    parts = basename.split(".")
    return [seg for seg in parts[1:] if seg]


def control_codepoints(filename: str) -> list[str]:
    """按出现序返回文件名中去重后的 Unicode 控制字符（``Cc`` 类）码点。"""

    seen: set[str] = set()
    codepoints: list[str] = []
    for ch in filename:
        if unicodedata.category(ch) == "Cc":
            label = f"U+{ord(ch):04X}"
            if label not in seen:
                seen.add(label)
                codepoints.append(label)
    return codepoints


# ---------------------------------------------------------------------------
# 评估入口
# ---------------------------------------------------------------------------


def evaluate_security_report(
    rows: list[sqlite3.Row],
    *,
    max_decoded_bytes: int = DEFAULT_MAX_DECODED_BYTES,
    max_attachment_count: int = DEFAULT_MAX_ATTACHMENT_COUNT,
    max_mime_depth: int = DEFAULT_MAX_MIME_DEPTH,
    forbidden_media_types: frozenset[str] = DEFAULT_FORBIDDEN_MEDIA_TYPES,
) -> SecurityReport:
    """在一封已归档邮件的节点行上执行确定性安全策略评估。

    ``rows`` 为 :func:`database.list_nodes` 的结果（行序无关，内部按
    物化路径排文档序）。阈值均为“严格大于”才命中。
    """

    violations: list[SecurityViolation] = []
    decoded_bytes = 0
    attachment_count = 0
    max_depth = 0
    depth_exceeded = False
    count_exceeded = False
    bytes_exceeded = False

    for row in sorted(rows, key=lambda r: path_order_key(r["path"])):
        path = row["path"]
        depth = int(row["depth"])
        kind = row["kind"]

        if depth > max_depth:
            max_depth = depth
        if depth > max_mime_depth and not depth_exceeded:
            violations.append(
                SecurityViolation(
                    CODE_MIME_DEPTH_EXCEEDED,
                    path,
                    {"limit": max_mime_depth, "depth": depth},
                )
            )
            depth_exceeded = True

        # 嵌套封装报文（message/rfc822 / message/global）：根自身不算。
        if kind == "message" and depth >= 1:
            violations.append(
                SecurityViolation(
                    CODE_NESTED_MESSAGE,
                    path,
                    {"content_type": row["content_type"].lower()},
                )
            )

        if kind != "blob":
            continue

        # 以下均为附件（非文本叶子）检查。
        attachment_count += 1
        if attachment_count > max_attachment_count and not count_exceeded:
            violations.append(
                SecurityViolation(
                    CODE_ATTACHMENT_COUNT_EXCEEDED,
                    path,
                    {"limit": max_attachment_count, "count": attachment_count},
                )
            )
            count_exceeded = True

        node_bytes = int(row["blob_length"] or 0)
        decoded_bytes += node_bytes
        if decoded_bytes > max_decoded_bytes and not bytes_exceeded:
            violations.append(
                SecurityViolation(
                    CODE_DECODED_BYTES_EXCEEDED,
                    path,
                    {
                        "limit": max_decoded_bytes,
                        "decoded_bytes": decoded_bytes,
                        "node_bytes": node_bytes,
                    },
                )
            )
            bytes_exceeded = True

        media_type = row["content_type"].lower()
        if media_type in forbidden_media_types:
            violations.append(
                SecurityViolation(
                    CODE_FORBIDDEN_MEDIA_TYPE,
                    path,
                    {"content_type": media_type},
                )
            )

        filename = attachment_filename(row)
        if filename is None:
            continue

        codepoints = control_codepoints(filename)
        if codepoints:
            violations.append(
                SecurityViolation(
                    CODE_FILENAME_CONTROL_CHARS,
                    path,
                    {"filename": filename, "codepoints": codepoints},
                )
            )

        extensions = split_extensions(filename)
        if len(extensions) >= 2:
            violations.append(
                SecurityViolation(
                    CODE_DOUBLE_EXTENSION,
                    path,
                    {"filename": filename, "extensions": extensions},
                )
            )

    return SecurityReport(
        violations=violations,
        totals=SecurityTotals(
            decoded_bytes=decoded_bytes,
            attachment_count=attachment_count,
            max_depth=max_depth,
        ),
    )
