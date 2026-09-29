"""确定性附件 ZIP 归档（``GET /messages/{id}/attachments.zip``）。

把一封已归档邮件的全部**非文本叶子**（``kind == "blob"``）按 **MIME
文档序**打包成 ZIP：每个 blob 的每次出现都写成独立条目（同哈希也不
合并），并内置一个 ``manifest.json`` 逐项记录元信息。

归档是**逐字节确定性**的：条目顺序、文件名、固定时间戳、固定 Unix
权限、固定压缩参数（DEFLATE level 9）与固定 JSON 序列化（键排序、
无空白、无尾换行）均与运行环境/时间无关，同一邮件重复打包得到完全
相同的字节，可用归档自身的 SHA-256 作为强 ETag。

本模块是纯函数：输入为文档序的 ``(node_row, blob_bytes)`` 序列，输出
ZIP 字节与归档名，不接触数据库、不回写记录。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import unicodedata
import zipfile
from dataclasses import dataclass

# manifest.json 在 ZIP 中的固定条目名。
MANIFEST_NAME = "manifest.json"

# 固定 ZIP 条目时间（1980-01-01，DOS 时间起点）。
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)

# 固定 Unix 权限：常规文件 0644 / 目录 0755（本归档无目录条目）。
_ZIP_EXTERNAL_ATTR = (0o644 << 16)

# 固定压缩参数。
_ZIP_COMPRESSION = zipfile.ZIP_DEFLATED
_ZIP_COMPRESS_LEVEL = 9


# ---------------------------------------------------------------------------
# 归档名派生
# ---------------------------------------------------------------------------


def _clean_name(value: str) -> str:
    """移除 Unicode 控制字符（``Cc`` 类），并替换路径分隔符。

    ``/`` 与 ``\\`` 一律替换为 ``_``（保留文件名、避免条目路径穿越）；
    C0（含 NUL/TAB/换行）、DEL、C1 等控制字符直接删除。
    """

    cleaned = "".join(ch for ch in value if unicodedata.category(ch) != "Cc")
    return cleaned.replace("/", "_").replace("\\", "_")


def archive_name_for(original_filename: str | None, node_path: str) -> str:
    """为单个附件出现派生 ZIP 条目名。

    * 取**解码文件名**的 basename（按 POSIX ``/`` 与 Windows ``\\``
      切分，取最后一段），再移除控制字符、替换路径分隔符；
    * 无文件名或清洗后为空名时回退 ``attachment-{节点路径}``（根节点
      路径为空，即 ``attachment-``），回退名同样替换分隔符。
    """

    if original_filename:
        basename = original_filename.replace("\\", "/").rsplit("/", 1)[-1]
        cleaned = _clean_name(basename)
        if cleaned:
            return cleaned
    return _clean_name(f"attachment-{node_path}")


def _conflict_key(name: str) -> str:
    """冲突键：NFC 规范化后 casefold（大小写与兼容折叠不敏感）。"""

    return unicodedata.normalize("NFC", name).casefold()


def _split_extension(name: str) -> tuple[str, str]:
    """按 ``os.path.splitext`` 口径在扩展名前插入序号。

    ``("a.tar.gz", n)`` → ``"a.tar (n).gz"``；``(".bashrc", n)`` →
    ``".bashrc (n)"``（点开头的隐藏文件无扩展名）。
    """

    stem, ext = os.path.splitext(name)
    return stem, ext


def assign_archive_names(
    entries: list["AttachmentEntry"],
) -> list[str]:
    """按文档序为条目分配不冲突的归档名。

    以 **NFC + casefold** 为冲突键；键已被占用时，在扩展名前追加
    `` (2)``、`` (3)``……直到取得空闲键。同哈希不同节点不合并，
    仅按名称去冲突。内置 ``manifest.json`` 预占同名槽位，附件不会
    覆盖清单条目。
    """

    used: set[str] = {_conflict_key(MANIFEST_NAME)}
    names: list[str] = []
    for entry in entries:
        base = archive_name_for(entry.original_filename, entry.node_path)
        candidate = base
        key = _conflict_key(candidate)
        if key in used:
            stem, ext = _split_extension(base)
            n = 2
            while True:
                candidate = f"{stem} ({n}){ext}"
                key = _conflict_key(candidate)
                if key not in used:
                    break
                n += 1
        used.add(key)
        names.append(candidate)
    return names


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentEntry:
    """一次 blob 出现（MIME 文档序中的一个附件叶子）。"""

    node_path: str
    original_filename: str | None  # 归档时保存的解码文件名（可为 None）
    media_type: str
    sha256: str
    size: int
    data: bytes


def _manifest_bytes(entries: list[AttachmentEntry], names: list[str]) -> bytes:
    """固定序列化 ``manifest.json``：键排序、无空白、UTF-8、无尾换行。"""

    manifest = [
        {
            "node_path": entry.node_path,
            "original_filename": entry.original_filename,
            "archive_name": name,
            "media_type": entry.media_type,
            "sha256": entry.sha256,
            "size": entry.size,
        }
        for entry, name in zip(entries, names)
    ]
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    """构造固定元数据的 ZipInfo（时间/权限/系统标记均确定）。"""

    info = zipfile.ZipInfo(filename=name, date_time=_ZIP_DATE_TIME)
    info.create_system = 3  # Unix：external_attr 的权限位才被解释
    info.external_attr = _ZIP_EXTERNAL_ATTR
    info.compress_type = _ZIP_COMPRESSION
    return info


def build_attachments_zip(entries: list[AttachmentEntry]) -> bytes:
    """按文档序构建确定性 ZIP。

    条目顺序固定：``manifest.json`` 在前，随后是每个 blob 的一次出现
    （同哈希多次出现写成同名或冲突改名后的多个独立条目，不合并）。
    无附件时结果是仅含空 manifest（``[]``）的有效 ZIP。
    """

    names = assign_archive_names(entries)

    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf,
        mode="w",
        compression=_ZIP_COMPRESSION,
        compresslevel=_ZIP_COMPRESS_LEVEL,
    ) as zf:
        zf.writestr(
            _zip_info(MANIFEST_NAME),
            _manifest_bytes(entries, names),
            compress_type=_ZIP_COMPRESSION,
            compresslevel=_ZIP_COMPRESS_LEVEL,
        )
        for entry, name in zip(entries, names):
            zf.writestr(
                _zip_info(name),
                entry.data,
                compress_type=_ZIP_COMPRESSION,
                compresslevel=_ZIP_COMPRESS_LEVEL,
            )
    return buf.getvalue()


def archive_sha256(data: bytes) -> str:
    """归档字节的 SHA-256（十六进制），用作强 ETag。"""

    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 从节点行收集条目
# ---------------------------------------------------------------------------


def entries_from_rows(
    rows: list[sqlite3.Row],
    refs_filename: dict[str, str | None],
    blobs: dict[str, bytes],
) -> list[AttachmentEntry]:
    """从文档序节点行收集附件条目。

    :param rows: ``database.list_nodes`` 结果（已按文档序）。
    :param refs_filename: ``node_path`` → 归档时保存的解码文件名。
    :param blobs: ``blob_sha256`` → 解码字节；缺少字节的节点抛
        :class:`KeyError`（调用方据此返回定位节点的 422）。
    """

    entries: list[AttachmentEntry] = []
    for row in rows:
        if row["kind"] != "blob":
            continue
        digest = row["blob_sha256"]
        data = blobs[digest]  # 缺失时 KeyError，由调用方转 422
        entries.append(
            AttachmentEntry(
                node_path=row["path"],
                original_filename=refs_filename.get(row["path"]),
                media_type=row["content_type"],
                sha256=digest,
                size=row["blob_length"],
                data=data,
            )
        )
    return entries
