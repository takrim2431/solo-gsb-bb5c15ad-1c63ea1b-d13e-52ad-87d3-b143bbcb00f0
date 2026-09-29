"""批量撤销归档（POST /messages/unarchive）端到端测试。

覆盖：
* 凭据校验：空列表、重复 id（422）、邮件缺失（404）、指纹不符（409），
  全部拒绝整批、数据不变；
* 影响计算：目标按 id 升序、每邮件将移除的引用数、仅本批删除后无人
  引用的附件 SHA-256 与可回收字节；预览只读且与执行同口径；
* 快照一致性：预览的指纹校验与全部统计在单个只读数据库快照内完成，
  分次读取期间被并发撤销/归档不会拼出失真估算；
* 执行：单事务删除邮件/节点/引用，仅清理无人引用的附件；批外仍被
  引用的附件字节保留、可查可下载；任何失败回滚。
"""

import hashlib
import sqlite3

import pytest

from conftest import make_multipart, text_part
from test_attachments import PDF, PNG, bin_part, message_with_parts, sha


def post_msg(client, data: bytes) -> dict:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()


def unarchive(client, messages, *, preview=None):
    body = {"messages": messages}
    if preview is not None:
        body["preview"] = preview
    return client.post("/messages/unarchive", json=body)


# ---------------------------------------------------------------------------
# 凭据校验
# ---------------------------------------------------------------------------


def test_empty_batch_rejected_422(client):
    r = unarchive(client, [], preview=True)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "UNARCHIVE_EMPTY_BATCH"
    # 显式 false 同样拒绝，且不删除任何东西
    r2 = unarchive(client, [], preview=False)
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "UNARCHIVE_EMPTY_BATCH"


def test_messages_field_required_422(client):
    r = client.post("/messages/unarchive", json={"preview": True})
    assert r.status_code == 422


def test_item_id_must_be_positive_422(client):
    r = unarchive(client, [{"id": 0, "fingerprint": "x"}], preview=True)
    assert r.status_code == 422


def test_item_fingerprint_required_422(client):
    r = unarchive(client, [{"id": 1}], preview=True)
    assert r.status_code == 422


def test_duplicate_id_rejected_whole_batch_422(client):
    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    m2 = post_msg(client, message_with_parts(bin_part(PDF, filename="b.pdf")))
    before = client.get("/messages").json()["total"]

    r = unarchive(
        client,
        [
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
        ],
        preview=False,
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "UNARCHIVE_DUPLICATE_ID"
    assert detail["id"] == m1["id"]
    assert detail["index"] == 2

    # 整批数据不变
    assert client.get("/messages").json()["total"] == before
    assert client.get(f"/messages/{m1['id']}").status_code == 200
    assert client.get(f"/messages/{m2['id']}").status_code == 200


def test_missing_id_rejected_whole_batch_404(client):
    m1 = post_msg(client, message_with_parts(text_part("hi")))
    r = unarchive(
        client,
        [
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
            {"id": 999, "fingerprint": "a" * 64},
        ],
        preview=True,
    )
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "UNARCHIVE_MESSAGE_NOT_FOUND"
    assert detail["id"] == 999
    # 报告升序第一个缺失项（即使 999 在前）
    r2 = unarchive(
        client,
        [
            {"id": 888, "fingerprint": "a" * 64},
            {"id": 999, "fingerprint": "b" * 64},
        ],
        preview=True,
    )
    assert r2.status_code == 404
    assert r2.json()["detail"]["id"] == 888
    assert client.get(f"/messages/{m1['id']}").status_code == 200


def test_fingerprint_mismatch_rejected_whole_batch_409(client):
    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    m2 = post_msg(client, message_with_parts(bin_part(PDF, filename="b.pdf")))
    wrong = "0" * 64
    r = unarchive(
        client,
        [
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
            {"id": m1["id"], "fingerprint": wrong},
        ],
        preview=False,
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "UNARCHIVE_FINGERPRINT_MISMATCH"
    assert detail["id"] == m1["id"]
    assert detail["provided_fingerprint"] == wrong
    assert detail["current_fingerprint"] == m1["fingerprint"]
    # 整批数据不变：两封邮件与两个附件都在
    assert client.get(f"/messages/{m1['id']}").status_code == 200
    assert client.get(f"/messages/{m2['id']}").status_code == 200
    listing = client.get("/attachments").json()
    assert listing["total"] == 2


def test_rejection_is_idempotent_data_unchanged(client):
    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    # 指纹不符来一次
    unarchive(client, [{"id": m1["id"], "fingerprint": "f" * 64}])
    # 正确凭据仍可预览/执行
    r = unarchive(client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}],
                  preview=True)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 预览：影响计算
# ---------------------------------------------------------------------------


def test_preview_is_read_only(client):
    m1 = post_msg(
        client,
        message_with_parts(
            bin_part(PNG, ctype="image/png", filename="a.png"),
            bin_part(PDF, filename="b.pdf"),
        ),
    )
    before_total = client.get("/messages").json()["total"]

    r = unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}], preview=True
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executed"] is False
    assert body["target_count"] == 1
    assert body["attachment_refs_removed"] == 2
    assert body["orphaned_attachment_count"] == 2
    assert body["reclaimed_bytes"] == len(PNG) + len(PDF)
    assert [t["id"] for t in body["targets"]] == [m1["id"]]
    target = body["targets"][0]
    assert target["fingerprint"] == m1["fingerprint"]
    assert target["content_type"] == "multipart/mixed"
    assert target["size_bytes"] == m1["size_bytes"]
    assert target["attachment_refs_removed"] == 2
    orphans = {o["sha256"]: o["size_bytes"] for o in body["orphaned_attachments"]}
    assert orphans == {sha(PNG): len(PNG), sha(PDF): len(PDF)}

    # 数据原封不动
    assert client.get("/messages").json()["total"] == before_total
    assert client.get(f"/messages/{m1['id']}").status_code == 200
    assert client.get(f"/messages/{m1['id']}/nodes").status_code == 200
    assert client.get(f"/attachments/{sha(PNG)}/content").status_code == 200


def test_targets_sorted_by_id_regardless_of_request_order(client):
    m1 = post_msg(client, message_with_parts(text_part("one")))
    m2 = post_msg(client, message_with_parts(text_part("two")))
    m3 = post_msg(client, message_with_parts(text_part("three")))
    r = unarchive(
        client,
        [
            {"id": m3["id"], "fingerprint": m3["fingerprint"]},
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
        ],
        preview=True,
    )
    assert [t["id"] for t in r.json()["targets"]] == [m1["id"], m2["id"], m3["id"]]


def test_preview_shared_attachment_not_orphaned(client):
    # 两封邮件共享 PDF；各自有独占附件
    m1 = post_msg(
        client,
        message_with_parts(
            bin_part(PNG, ctype="image/png", filename="a.png"),
            bin_part(PDF, filename="shared.pdf"),
        )
    )
    m2 = post_msg(
        client,
        message_with_parts(
            bin_part(PDF, filename="shared-copy.pdf"),
            subject="m2",
        )
    )

    # 只撤销 m1：PDF 仍被 m2 引用，不清理；PNG 独占被清理
    r = unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}], preview=True
    )
    body = r.json()
    assert body["attachment_refs_removed"] == 2
    orphan_hashes = [o["sha256"] for o in body["orphaned_attachments"]]
    assert orphan_hashes == [sha(PNG)]
    assert body["reclaimed_bytes"] == len(PNG)

    # 撤销 m1+m2：PDF 在本批后也无人引用，与 PNG 一起按 sha 升序列出
    r2 = unarchive(
        client,
        [
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
        ],
        preview=True,
    )
    body2 = r2.json()
    assert body2["attachment_refs_removed"] == 3
    assert body2["orphaned_attachment_count"] == 2
    assert [o["sha256"] for o in body2["orphaned_attachments"]] == sorted(
        [sha(PNG), sha(PDF)]
    )
    assert body2["reclaimed_bytes"] == len(PNG) + len(PDF)


def test_duplicate_blob_in_one_message_single_orphan(client):
    # 同一内容在一封邮件出现两次：实体只有一个，删除引用 2 行、实体 1 个
    m1 = post_msg(
        client,
        message_with_parts(
            bin_part(PNG, ctype="image/png", filename="x.png"),
            bin_part(PNG, ctype="image/png", filename="y.png"),
        ),
    )
    r = unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}], preview=True
    )
    body = r.json()
    assert body["attachment_refs_removed"] == 2
    assert body["orphaned_attachment_count"] == 1
    assert body["orphaned_attachments"][0]["sha256"] == sha(PNG)
    assert body["reclaimed_bytes"] == len(PNG)  # 字节只回收一份


def test_preview_message_without_attachments(client):
    m1 = post_msg(client, message_with_parts(text_part("only text")))
    r = unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}], preview=True
    )
    body = r.json()
    assert body["attachment_refs_removed"] == 0
    assert body["orphaned_attachments"] == []
    assert body["reclaimed_bytes"] == 0


def test_attachment_outside_batch_untouched_in_impact(client):
    # 批外 m3 独占的 PDF 永不出现在 orphan 列表
    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    m2 = post_msg(client, message_with_parts(bin_part(PNG, filename="a2.png")))
    m3 = post_msg(
        client, message_with_parts(bin_part(PDF, filename="only-m3.pdf"),
                                   subject="m3")
    )
    r = unarchive(
        client,
        [
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
        ],
        preview=True,
    )
    orphan_hashes = [o["sha256"] for o in r.json()["orphaned_attachments"]]
    assert orphan_hashes == [sha(PNG)]
    assert sha(PDF) not in orphan_hashes


def test_preview_uses_single_snapshot_against_concurrent_unarchive(
    client, monkeypatch
):
    """预览分次读取期间目标被另一请求撤销：全部数字仍取自同一快照。

    旧实现三条 SELECT 各自自动提交读取：第一条读到删除前的目标行，随后
    另一连接提交撤销，引用计数与孤儿统计就读到删除后的状态，拼出
    “目标存在但 refs/孤儿/字节全为 0”的失真估算。修复后预览在单个
    BEGIN DEFERRED 读快照内完成全部读取，仍按删除前状态自洽返回。
    """

    from app import database
    from app import unarchive as unarchive_mod

    m1 = post_msg(
        client,
        message_with_parts(
            bin_part(PNG, ctype="image/png", filename="a.png"),
            bin_part(PDF, filename="b.pdf"),
        ),
    )
    png_sha, pdf_sha = sha(PNG), sha(PDF)

    writer = database.connect(client.app.state.db_path)
    # 在第一条 SELECT（目标行）之后、附件统计之前撤销 m1
    original = unarchive_mod._fetch_messages

    def racy_fetch(conn, ordered_ids):
        rows = original(conn, ordered_ids)
        with database.immediate_transaction(writer):
            database.delete_messages_cascade(
                writer, ordered_ids,
                [o.sha256 for o in unarchive_mod._orphaned_attachments(
                    writer, ordered_ids
                )],
            )
        return rows

    monkeypatch.setattr(unarchive_mod, "_fetch_messages", racy_fetch)
    try:
        r = unarchive(
            client,
            [{"id": m1["id"], "fingerprint": m1["fingerprint"]}],
            preview=True,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["executed"] is False
        assert body["target_count"] == 1
        # 快照内一致：不能是“目标在、引用/字节却为 0”的拼接
        assert body["attachment_refs_removed"] == 2
        assert body["orphaned_attachment_count"] == 2
        assert body["reclaimed_bytes"] == len(PNG) + len(PDF)
        assert [o["sha256"] for o in body["orphaned_attachments"]] == sorted(
            [png_sha, pdf_sha]
        )
        assert body["targets"][0]["attachment_refs_removed"] == 2
        assert body["targets"][0]["fingerprint"] == m1["fingerprint"]
    finally:
        writer.close()
        monkeypatch.undo()

    # 并发撤销已落库：快照只影响预览读，不会把数据回滚回来
    assert client.get(f"/messages/{m1['id']}").status_code == 404
    assert client.get(f"/attachments/{png_sha}/content").status_code == 404

    # 快照已释放：再次预览当前数据，目标确实已不存在 → 既有 404
    r2 = unarchive(
        client,
        [{"id": m1["id"], "fingerprint": m1["fingerprint"]}],
        preview=True,
    )
    assert r2.status_code == 404
    assert r2.json()["detail"]["code"] == "UNARCHIVE_MESSAGE_NOT_FOUND"


def test_preview_snapshot_remains_read_only_when_concurrent_archive_commits(
    client, monkeypatch
):
    """持有读快照期间另一连接可以提交写入（WAL 不互相阻塞）。"""

    from app import database, unarchive as unarchive_mod

    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    writer = database.connect(client.app.state.db_path)

    original = unarchive_mod._fetch_messages

    def racy_fetch(conn, ordered_ids):
        rows = original(conn, ordered_ids)
        # 快照持有期间另一连接直接提交一条新邮件（占位行即可）
        with writer:
            cur = writer.execute(
                "INSERT INTO messages"
                " (fingerprint, raw_sha256, canonical_json, content_type,"
                "  size_bytes, created_at)"
                " VALUES (?, ?, '{}', 'text/plain', 1, ?)",
                ("f" * 64, "r" * 64, "2026-01-01T00:00:00+00:00"),
            )
            assert int(cur.lastrowid) > 0
        return rows

    monkeypatch.setattr(unarchive_mod, "_fetch_messages", racy_fetch)
    try:
        r = unarchive(
            client,
            [{"id": m1["id"], "fingerprint": m1["fingerprint"]}],
            preview=True,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # 预览仍只含快照中的 m1 一个目标，看不到快照之后的新行
        assert body["target_count"] == 1
        assert [t["id"] for t in body["targets"]] == [m1["id"]]
    finally:
        writer.close()
        monkeypatch.undo()

    # 并发提交在预览后可见
    assert client.get("/messages").json()["total"] == 2


def test_read_only_snapshot_blocks_writes_and_releases(client):
    """快照块内禁止写入；ROLLBACK 退出后同一连接恢复可写。"""

    from app import database

    insert_sql = (
        "INSERT INTO messages"
        " (fingerprint, raw_sha256, canonical_json, content_type,"
        "  size_bytes, created_at)"
        " VALUES (?, ?, '{}', 'text/plain', 0, '2026-01-01T00:00:00+00:00')"
    )
    conn = database.connect(client.app.state.db_path)
    try:
        with database.read_only_snapshot(conn):
            with pytest.raises(sqlite3.Error):
                # query_only 兜底：快照内任何写都被拒绝
                conn.execute(insert_sql, ("a" * 64, "b" * 64))
        # 快照释放、query_only 恢复：连接照常读写
        with database.transaction(conn):
            conn.execute(insert_sql, ("a" * 64, "b" * 64))
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE fingerprint = ?",
                ("a" * 64,),
            ).fetchone()["c"]
            == 1
        )
    finally:
        conn.close()


def test_preview_404_when_target_already_unarchived_before_snapshot(client):
    """目标在快照建立之前已撤销：仍按既有 404 拒绝整批。"""

    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    unarchive(client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}])
    r = unarchive(
        client,
        [{"id": m1["id"], "fingerprint": m1["fingerprint"]}],
        preview=True,
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "UNARCHIVE_MESSAGE_NOT_FOUND"
    assert detail["id"] == m1["id"]


def test_preview_fingerprint_mismatch_409_inside_snapshot(client):
    """快照中指纹不符仍按既有 409 处理（只读、数据不变）。"""

    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    before_total = client.get("/messages").json()["total"]
    r = unarchive(
        client,
        [{"id": m1["id"], "fingerprint": "f" * 64}],
        preview=True,
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "UNARCHIVE_FINGERPRINT_MISMATCH"
    assert detail["id"] == m1["id"]
    assert detail["provided_fingerprint"] == "f" * 64
    assert detail["current_fingerprint"] == m1["fingerprint"]
    assert client.get("/messages").json()["total"] == before_total
    assert client.get(f"/messages/{m1['id']}").status_code == 200
    # 只读快照已正常释放，同一请求连接关闭后新请求不受影响
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# 执行：事务删除
# ---------------------------------------------------------------------------


def test_execute_deletes_messages_nodes_refs_and_orphans(client):
    m1 = post_msg(
        client,
        message_with_parts(
            bin_part(PNG, ctype="image/png", filename="a.png"),
            bin_part(PDF, filename="b.pdf"),
        ),
    )
    png_sha, pdf_sha = sha(PNG), sha(PDF)

    r = unarchive(client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executed"] is True
    assert body["orphaned_attachment_count"] == 2

    # 邮件、节点、引用、独占附件全部消失
    assert client.get(f"/messages/{m1['id']}").status_code == 404
    assert client.get(f"/messages/{m1['id']}/nodes").status_code == 404
    assert client.get(f"/attachments/{png_sha}/content").status_code == 404
    assert client.get(f"/attachments/{pdf_sha}/content").status_code == 404
    listing = client.get("/attachments").json()
    assert listing["total"] == 0
    assert client.get("/messages").json()["total"] == 0


def test_execute_shared_attachment_bytes_remain_downloadable(client):
    m1 = post_msg(
        client,
        message_with_parts(
            bin_part(PNG, ctype="image/png", filename="a.png"),
            bin_part(PDF, filename="shared.pdf"),
        ),
    )
    m2 = post_msg(
        client,
        message_with_parts(bin_part(PDF, filename="shared-copy.pdf"),
                           subject="m2"),
    )
    pdf_sha, png_sha = sha(PDF), sha(PNG)

    r = unarchive(client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}])
    body = r.json()
    assert body["executed"] is True
    assert [o["sha256"] for o in body["orphaned_attachments"]] == [png_sha]

    # m1 删除；共享 PDF 字节保留且可查可下载，引用只余 m2
    assert client.get(f"/messages/{m1['id']}").status_code == 404
    pdf_item = client.get(f"/attachments?sha256={pdf_sha}").json()
    assert pdf_item["total"] == 1
    assert pdf_item["items"][0]["reference_count"] == 1
    assert pdf_item["items"][0]["occurrences"] == [
        {
            "message_id": m2["id"],
            "node_path": "1",
            "filename": "shared-copy.pdf",
            "disposition": "attachment",
            "media_type": "application/octet-stream",
        }
    ]
    dl = client.get(f"/attachments/{pdf_sha}/content")
    assert dl.status_code == 200
    assert dl.content == PDF
    assert hashlib.sha256(dl.content).hexdigest() == pdf_sha
    # 独占的 PNG 已清理
    assert client.get(f"/attachments/{png_sha}/content").status_code == 404

    # m2 自身查询完好（整封 ZIP、节点等仍可用）
    assert client.get(f"/messages/{m2['id']}").status_code == 200
    z = client.get(f"/messages/{m2['id']}/attachments.zip")
    assert z.status_code == 200
    assert z.content  # 非空 ZIP（含 PDF 条目与 manifest）


def test_batch_execute_mixed_shared_and_unique(client):
    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    m2 = post_msg(
        client,
        message_with_parts(bin_part(PNG, filename="a2.png"), subject="m2"),
    )
    m3 = post_msg(
        client,
        message_with_parts(bin_part(PDF, filename="only-m3.pdf"), subject="m3"),
    )
    r = unarchive(
        client,
        [
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
        ],
    )
    body = r.json()
    assert body["executed"] is True
    assert [t["id"] for t in body["targets"]] == [m1["id"], m2["id"]]
    assert body["attachment_refs_removed"] == 2
    assert [o["sha256"] for o in body["orphaned_attachments"]] == [sha(PNG)]
    assert body["reclaimed_bytes"] == len(PNG)

    remaining = {x["id"] for x in client.get("/messages").json()["items"]}
    assert remaining == {m3["id"]}
    attachments = client.get("/attachments").json()
    assert {i["sha256"] for i in attachments["items"]} == {sha(PDF)}


def test_preview_then_execute_same_impact(client):
    m1 = post_msg(
        client,
        message_with_parts(
            bin_part(PNG, ctype="image/png", filename="a.png"),
            bin_part(PDF, filename="b.pdf"),
        ),
    )
    preview = unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}], preview=True
    ).json()
    executed = unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}]
    ).json()
    preview.pop("executed")
    executed_flag = executed.pop("executed")
    assert executed_flag is True
    assert preview == executed

    # 再次撤销同一 id：已不存在，拒绝且无副作用
    again = unarchive(client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}])
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "UNARCHIVE_MESSAGE_NOT_FOUND"


def test_execute_then_rearchive_same_content_new_id(client):
    data = message_with_parts(bin_part(PNG, filename="a.png"))
    m1 = post_msg(client, data)
    unarchive(client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}])
    # 重新归档同一封邮件：同一指纹 -> 复用为新行（SQLite 自增 id 递增）
    m2 = post_msg(client, data)
    assert m2["fingerprint"] == m1["fingerprint"]
    assert m2["id"] != m1["id"]
    assert m2["created"] is True
    assert client.get(f"/attachments/{sha(PNG)}/content").status_code == 200


def test_execute_failure_rolls_back_whole_batch(client, monkeypatch):
    m1 = post_msg(client, message_with_parts(bin_part(PNG, filename="a.png")))
    m2 = post_msg(
        client, message_with_parts(bin_part(PDF, filename="b.pdf"), subject="m2")
    )
    total_before = client.get("/messages").json()["total"]

    # 让删除节点这一步失败：事务必须回滚，引用删除也不得落库
    from app import database

    real = database.delete_messages_cascade
    calls = {"n": 0}

    def flaky(conn, message_ids, orphan_sha256s):
        calls["n"] += 1
        real(conn, message_ids, orphan_sha256s)
        raise RuntimeError("boom after delete")

    monkeypatch.setattr(database, "delete_messages_cascade", flaky)
    with pytest.raises(Exception):
        unarchive(
            client,
            [
                {"id": m1["id"], "fingerprint": m1["fingerprint"]},
                {"id": m2["id"], "fingerprint": m2["fingerprint"]},
            ],
        )
    assert calls["n"] == 1

    # 整批数据不变：邮件、引用、附件都在
    assert client.get("/messages").json()["total"] == total_before
    assert client.get(f"/messages/{m1['id']}").status_code == 200
    assert client.get(f"/messages/{m2['id']}").status_code == 200
    listing = client.get("/attachments").json()
    assert listing["total"] == 2
    for digest in (sha(PNG), sha(PDF)):
        item = client.get(f"/attachments?sha256={digest}").json()["items"][0]
        assert item["reference_count"] == 1
        assert client.get(f"/attachments/{digest}/content").status_code == 200
    assert client.get(f"/messages/{m1['id']}/nodes").json()["nodes"]


def test_execute_default_preview_is_false(client):
    m1 = post_msg(client, message_with_parts(text_part("hi")))
    r = client.post(
        "/messages/unarchive",
        json={"messages": [{"id": m1["id"], "fingerprint": m1["fingerprint"]}]},
    )
    assert r.status_code == 200
    assert r.json()["executed"] is True
    assert client.get(f"/messages/{m1['id']}").status_code == 404


def test_execute_with_attachment_referenced_by_three_messages(client):
    # PDF 被 m1/m2/m3 引用；逐个撤销，直到最后一个才清理实体
    msgs = []
    for i in range(3):
        msgs.append(
            post_msg(
                client,
                message_with_parts(bin_part(PDF, filename=f"{i}.pdf"),
                                   subject=f"m{i}"),
            )
        )
    pdf_sha = sha(PDF)
    for m in msgs[:-1]:
        r = unarchive(client, [{"id": m["id"], "fingerprint": m["fingerprint"]}])
        assert r.status_code == 200
        assert r.json()["orphaned_attachments"] == []
        item = client.get(f"/attachments?sha256={pdf_sha}").json()["items"][0]
        assert item["reference_count"] >= 1
        assert client.get(f"/attachments/{pdf_sha}/content").status_code == 200

    last = msgs[-1]
    r = unarchive(client, [{"id": last["id"], "fingerprint": last["fingerprint"]}])
    assert [o["sha256"] for o in r.json()["orphaned_attachments"]] == [pdf_sha]
    assert client.get(f"/attachments/{pdf_sha}/content").status_code == 404
