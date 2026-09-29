"""批量撤销归档：凭据校验与影响计算（预览/执行共用同一口径）。

调用方（HTTP 层）传入一个或多个 ``{id, fingerprint}`` 目标。本模块
**不做任何写入**，只负责：

1. 凭据校验——空列表、重复 id、邮件不存在、指纹与**当前**归档指纹
   不符都拒绝整批（抛出 :class:`UnarchiveValidationError`）；
2. 影响计算——按 id 升序列出目标邮件与各自将移除的附件引用数，并按
   SHA-256 升序列出**仅在本批删除后才无人引用**的附件实体（预览看到
   的集合与执行时真正清理的集合完全一致）；仍被批外邮件引用的附件不
   在其中，字节保持可查可下载。

真正的删除在 :mod:`app.database` 的单个事务内完成
（``delete_messages_cascade``），任何失败整体回滚。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class UnarchiveValidationError(Exception):
    """整批撤销被拒绝（凭据问题），不修改任何数据。

    ``status_code`` 映射到 HTTP：空列表 / 重复 id → 422，邮件不存在 →
    404，指纹不符 → 409（乐观并发语义）。
    """

    def __init__(self, code: str, message: str, status_code: int, detail: dict):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class UnarchiveTargetImpact:
    """单个目标邮件的撤销影响。"""

    id: int
    fingerprint: str
    content_type: str
    size_bytes: int
    created_at: str
    attachment_refs_removed: int


@dataclass(frozen=True)
class UnarchiveOrphan:
    """本批删除后才失去全部引用的附件实体。"""

    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class UnarchiveImpact:
    """整批撤销的影响（预览与执行共用）。"""

    targets: list[UnarchiveTargetImpact]
    orphans: list[UnarchiveOrphan]

    @property
    def refs_removed(self) -> int:
        return sum(t.attachment_refs_removed for t in self.targets)

    @property
    def reclaimed_bytes(self) -> int:
        return sum(o.size_bytes for o in self.orphans)


def validate_and_compute_impact(
    conn: sqlite3.Connection,
    items: list[tuple[int, str]],
) -> UnarchiveImpact:
    """校验 ``{id, fingerprint}`` 凭据并计算撤销影响。

    ``items`` 为请求中的目标（保持请求顺序，用于报告重复出现位置）。
    校验全部通过才返回影响；任一失败抛出
    :class:`UnarchiveValidationError`，不触碰数据。
    """

    # 1) 空列表拒绝。
    if not items:
        raise UnarchiveValidationError(
            code="UNARCHIVE_EMPTY_BATCH",
            message="撤销目标列表为空：至少需要一个 {id, fingerprint}",
            status_code=422,
            detail={},
        )

    # 2) 重复 id 拒绝（报告第一个重复的 id 及其在请求中的下标）。
    seen: set[int] = set()
    for index, (message_id, _fingerprint) in enumerate(items):
        if message_id in seen:
            raise UnarchiveValidationError(
                code="UNARCHIVE_DUPLICATE_ID",
                message=f"撤销目标中的邮件 id {message_id} 重复出现",
                status_code=422,
                detail={"id": message_id, "index": index},
            )
        seen.add(message_id)

    ordered_ids = sorted(message_id for message_id, _ in items)
    expected = {message_id: fingerprint for message_id, fingerprint in items}

    # 3) 邮件必须全部存在（按 id 升序报告第一个缺失项）。
    rows = _fetch_messages(conn, ordered_ids)
    by_id = {int(r["id"]): r for r in rows}
    for message_id in ordered_ids:
        if message_id not in by_id:
            raise UnarchiveValidationError(
                code="UNARCHIVE_MESSAGE_NOT_FOUND",
                message=f"邮件 {message_id} 不存在或已被撤销",
                status_code=404,
                detail={"id": message_id},
            )

    # 4) 指纹必须与当前归档指纹逐一相符（按 id 升序报告第一个不符项）。
    for message_id in ordered_ids:
        current = by_id[message_id]["fingerprint"]
        provided = expected[message_id]
        if provided != current:
            raise UnarchiveValidationError(
                code="UNARCHIVE_FINGERPRINT_MISMATCH",
                message=(
                    f"邮件 {message_id} 的指纹与当前归档不符"
                    "（邮件可能已被重新归档或凭据过期）"
                ),
                status_code=409,
                detail={
                    "id": message_id,
                    "provided_fingerprint": provided,
                    "current_fingerprint": current,
                },
            )

    # 5) 全部凭据通过：计算影响（同一口径供预览与执行使用）。
    ref_counts = _attachment_ref_counts(conn, ordered_ids)
    targets = [
        UnarchiveTargetImpact(
            id=message_id,
            fingerprint=by_id[message_id]["fingerprint"],
            content_type=by_id[message_id]["content_type"],
            size_bytes=int(by_id[message_id]["size_bytes"]),
            created_at=by_id[message_id]["created_at"],
            attachment_refs_removed=ref_counts.get(message_id, 0),
        )
        for message_id in ordered_ids
    ]
    orphans = _orphaned_attachments(conn, ordered_ids)
    return UnarchiveImpact(targets=targets, orphans=orphans)


def _fetch_messages(
    conn: sqlite3.Connection, ordered_ids: list[int]
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in ordered_ids)
    return list(
        conn.execute(
            f"""
            SELECT id, fingerprint, content_type, size_bytes, created_at
            FROM messages WHERE id IN ({placeholders})
            ORDER BY id
            """,
            ordered_ids,
        ).fetchall()
    )


def _attachment_ref_counts(
    conn: sqlite3.Connection, ordered_ids: list[int]
) -> dict[int, int]:
    """每个目标邮件将移除的附件引用行数（blob 的每次出现计一行）。"""

    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        SELECT message_id, COUNT(*) AS c
        FROM attachment_refs
        WHERE message_id IN ({placeholders})
        GROUP BY message_id
        """,
        ordered_ids,
    ).fetchall()
    return {int(r["message_id"]): int(r["c"]) for r in rows}


def _orphaned_attachments(
    conn: sqlite3.Connection, ordered_ids: list[int]
) -> list[UnarchiveOrphan]:
    """找出**仅在本批删除后**才无人引用的附件实体（按 sha256 升序）。

    判定：附件的全部引用都落在目标邮件集合内（至少一条，天然满足）。
    批外仍有任何一条引用的附件不在结果中——这些字节必须保留、可查可
    下载。JOIN ``attachments`` 同时保证只统计仍存在的实体（历史库中
    可能有无实体的遗留引用，它们不产生可回收字节，也无需删除）。
    """

    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        SELECT a.sha256 AS sha256, a.size_bytes AS size_bytes
        FROM attachments a
        WHERE EXISTS (
                  SELECT 1 FROM attachment_refs r
                  WHERE r.attachment_sha256 = a.sha256
                    AND r.message_id IN ({placeholders})
              )
          AND NOT EXISTS (
                  SELECT 1 FROM attachment_refs r
                  WHERE r.attachment_sha256 = a.sha256
                    AND r.message_id NOT IN ({placeholders})
              )
        ORDER BY a.sha256
        """,
        (*ordered_ids, *ordered_ids),
    ).fetchall()
    return [
        UnarchiveOrphan(sha256=r["sha256"], size_bytes=int(r["size_bytes"]))
        for r in rows
    ]
