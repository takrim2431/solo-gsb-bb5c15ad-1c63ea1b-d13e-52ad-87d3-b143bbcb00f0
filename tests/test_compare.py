"""两封已归档邮件 MIME 树比较（POST /messages/compare）测试。

覆盖：内容指纹同口径（根指纹与归档指纹一致）、规范化 facet
（Content-Type 头大小写/空白无关、boundary/charset/CTE 不参与）、
叶子文本/二进制摘要、同层重排跨位置配对、位置兜底对齐、
added/removed 子树、changed 各 facet、同指纹重复的 ambiguous
（2:2 互换、1:2 多份、纯新增不误判）、嵌套递归、400/404/422
错误码、只读与结果稳定性。
"""

import hashlib

from conftest import make_multipart, text_part


def mixed_msg(parts: list[bytes], boundary: str = "B") -> bytes:
    """完整的 multipart/mixed 邮件（根头 + 多部分正文）。"""

    return (
        f"Content-Type: multipart/mixed; boundary={boundary}\r\n\r\n".encode()
        + make_multipart(parts, boundary)
    )


def _archive(client, data: bytes) -> dict:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()


def _compare(client, a: int, b: int):
    r = client.post("/messages/compare", json={"a": a, "b": b})
    assert r.status_code == 200, r.text
    return r.json()


def _diffs(body, kind: str | None = None) -> list[dict]:
    if kind is None:
        return body["diffs"]
    return [d for d in body["diffs"] if d["kind"] == kind]


def _changed_facets(diff: dict) -> dict:
    return {f["facet"]: (f["a"], f["b"]) for f in diff["facets"]}


# ---------------------------------------------------------------------------
# 单元级：内容指纹与 facet 规范化
# ---------------------------------------------------------------------------


def test_root_fingerprint_matches_archived_fingerprint(client):
    from app.compare import _Index
    from app import database
    from app.main import app

    msg = mixed_msg(
        [text_part("alpha"), text_part("beta", charset="us-ascii")],
        "ROOTB",
    )
    created = _archive(client, msg)

    # 直接用独立连接读取存储行，重建根指纹
    conn = database.connect(app.state.db_path)
    try:
        rows = database.list_nodes(conn, created["id"])
        idx = _Index(rows)
        assert idx.fingerprint(idx.root) == created["fingerprint"]
    finally:
        conn.close()


def test_blob_root_fingerprint_matches(client):
    from app.compare import _Index
    from app import database
    from app.main import app

    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nAAECAwQ=\r\n"
    )
    created = _archive(client, msg)
    conn = database.connect(app.state.db_path)
    try:
        rows = database.list_nodes(conn, created["id"])
        idx = _Index(rows)
        assert idx.fingerprint(idx.root) == created["fingerprint"]
    finally:
        conn.close()


def test_canonical_equivalent_messages_deduplicate_to_same_id(client):
    # 头大小写、boundary、charset、CTE 在规范化后等价 → 归档幂等为
    # 同一 id（因此 distinct id 间的 identical=true 只能由重排产生，
    # 见 test_siblings_reordered_recognized）。
    import base64

    m1 = (
        b"Content-Type: multipart/mixed; boundary=AAA\r\n\r\n"
        + make_multipart(
            [
                b"Content-Type: TEXT/PLAIN; charset=utf-8\r\n"
                b"Content-Transfer-Encoding: base64\r\n\r\n"
                + base64.b64encode("hello".encode())
            ],
            "AAA",
        )
    )
    m2 = (
        b"CONTENT-TYPE: Multipart/Mixed; boundary=BBB\r\n\r\n"
        + make_multipart(
            [
                b"content-type:   text/plain ; charset=us-ascii\r\n\r\nhello"
            ],
            "BBB",
        )
    )
    r1 = _archive(client, m1)
    r2 = client.post("/messages", content=m2)
    assert r2.status_code == 201, r2.text
    assert r2.json()["id"] == r1["id"]
    assert r2.json()["created"] is False


def test_compare_message_against_itself_is_identical(client):
    from app.compare import compare_messages
    from app import database
    from app.main import app

    msg = mixed_msg([text_part("a"), text_part("b")])
    mid = _archive(client, msg)["id"]
    conn = database.connect(app.state.db_path)
    try:
        rows = database.list_nodes(conn, mid)
        assert compare_messages(rows, rows) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP：相同/等价
# ---------------------------------------------------------------------------


def test_compare_same_id_rejected(client):
    # 同一 id 自比较是 400（等价性用重排用例与单元层自比较覆盖）。
    mid = _archive(client, b"Subject: hi\r\n\r\nbody\r\n")["id"]
    r = client.post("/messages/compare", json={"a": mid, "b": mid})
    assert r.status_code == 400


def test_reorder_identical_content_reports_identical(client):
    # 节点顺序参与归档指纹（两封是不同归档 id），但比较以内容指纹而非
    # 路径为身份：纯互换 → identical。
    mb1 = (
        b"Content-Type: multipart/mixed; boundary=X\r\n\r\n"
        + make_multipart([text_part("p1"), text_part("p2")], "X")
    )
    mb2 = (
        b"Content-Type: multipart/mixed; boundary=Y\r\n\r\n"
        + make_multipart([text_part("p2"), text_part("p1")], "Y")
    )
    id1 = _archive(client, mb1)["id"]
    id2 = _archive(client, mb2)["id"]
    assert id1 != id2
    body = _compare(client, id1, id2)
    assert body["identical"] is True
    assert body["summary"] == {
        "added": 0,
        "removed": 0,
        "changed": 0,
        "ambiguous": 0,
    }


# ---------------------------------------------------------------------------
# 叶子变更
# ---------------------------------------------------------------------------


def test_leaf_text_content_changed(client):
    m1 = mixed_msg([text_part("one"), text_part("two")])
    m2 = mixed_msg([text_part("one"), text_part("CHANGED")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    assert body["identical"] is False
    changed = _diffs(body, "changed")
    assert len(changed) == 1
    d = changed[0]
    assert d["a_path"] == "2" and d["b_path"] == "2"
    facets = _changed_facets(d)
    assert set(facets) == {"leaf"}
    leaf_a, leaf_b = facets["leaf"]
    assert leaf_a["kind"] == leaf_b["kind"] == "text"
    assert leaf_a["text_sha256"] == hashlib.sha256(b"two").hexdigest()
    assert leaf_b["text_sha256"] == hashlib.sha256(b"CHANGED").hexdigest()
    assert leaf_a["length"] == 3 and leaf_b["length"] == 7


def test_leaf_blob_changed(client):
    import base64

    def blob_part(data: bytes) -> bytes:
        return (
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + base64.b64encode(data)
        )

    m1 = mixed_msg([text_part("x"), blob_part(b"AAAA")])
    m2 = mixed_msg([text_part("x"), blob_part(b"BBBB")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    changed = _diffs(_compare(client, id1, id2), "changed")
    assert len(changed) == 1
    facets = _changed_facets(changed[0])
    assert list(facets) == ["leaf"]
    leaf_a, leaf_b = facets["leaf"]
    assert leaf_a["kind"] == leaf_b["kind"] == "blob"
    assert leaf_a["blob_sha256"] == hashlib.sha256(b"AAAA").hexdigest()
    assert leaf_b["length"] == 4


def test_same_blob_different_cte_deduplicates(client):
    import base64

    encoded = base64.b64encode(b"\x00\x01\x02\x03")
    m1 = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + encoded
    )
    m2 = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        + b"\x00\x01\x02\x03"
    )
    r1 = _archive(client, m1)
    r2 = client.post("/messages", content=m2)
    assert r2.status_code == 201, r2.text
    # CTE 差异规范化后等价 → 同一归档
    assert r2.json()["id"] == r1["id"]
    assert r2.json()["created"] is False


# ---------------------------------------------------------------------------
# 头字段 / Content-Type / disposition 变更
# ---------------------------------------------------------------------------


def test_header_changed_subject(client):
    m1 = b"Subject: one\r\n\r\nbody\r\n"
    m2 = b"Subject: two\r\n\r\nbody\r\n"
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    changed = _diffs(_compare(client, id1, id2), "changed")
    assert len(changed) == 1
    facets = _changed_facets(changed[0])
    assert set(facets) == {"headers"}
    ha, hb = facets["headers"]
    assert ["subject", "one"] in ha
    assert ["subject", "two"] in hb


def test_header_name_case_and_folding_equivalent(client):
    # 头名大小写与线性空白在规范化后等价 → 归档幂等为同一 id。
    m1 = b"X-Long-Header: value\r\n\r\nx\r\n"
    m2 = b"x-long-header:   value  \r\n\r\nx\r\n"
    id1 = _archive(client, m1)["id"]
    r2 = client.post("/messages", content=m2)
    assert r2.status_code == 201
    assert r2.json()["id"] == id1
    assert r2.json()["created"] is False


def test_content_type_changed(client):
    m1 = mixed_msg(
        [
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nhello",
            text_part("z"),
        ],
        "B",
    )
    m2 = mixed_msg(
        [
            b"Content-Type: text/html; charset=utf-8\r\n\r\nhello",
            text_part("z"),
        ],
        "B",
    )
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    changed = _diffs(_compare(client, id1, id2), "changed")
    assert len(changed) == 1
    facets = _changed_facets(changed[0])
    # Content-Type 主类型变了；叶子内容同为 "hello" 不变
    assert "content_type" in facets
    cta, ctb = facets["content_type"]
    assert cta == {"media_type": "text/plain", "params": {}}
    assert ctb == {"media_type": "text/html", "params": {}}
    assert "leaf" not in facets


def test_content_type_param_changed(client):
    m1 = b'Content-Type: text/plain; charset=utf-8; format=flowed\r\n\r\nhi'
    m2 = b'Content-Type: text/plain; charset=utf-8\r\n\r\nhi'
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    changed = _diffs(_compare(client, id1, id2), "changed")
    assert len(changed) == 1
    facets = _changed_facets(changed[0])
    cta, ctb = facets["content_type"]
    assert cta["params"] == {"format": "flowed"}
    assert ctb["params"] == {}


def test_charset_only_change_is_equivalent(client):
    # charset 在 content_type facet 中被剔除，结构头不进 headers facet；
    # 内容按声明字符集解码后相同 → 规范化等价，归档幂等为同一 id。
    m1 = b"Content-Type: text/plain; charset=utf-8\r\n\r\nhello"
    m2 = b"Content-Type: text/plain; charset=us-ascii\r\n\r\nhello"
    id1 = _archive(client, m1)["id"]
    r2 = client.post("/messages", content=m2)
    assert r2.status_code == 201
    assert r2.json()["id"] == id1
    assert r2.json()["created"] is False


def test_disposition_changed(client):
    inline = (
        b"Content-Disposition: inline\r\n"
        b"Content-Type: application/octet-stream\r\n\r\nabc"
    )
    attach = (
        b'Content-Disposition: attachment; filename="f.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\nabc"
    )
    id1 = _archive(client, inline)["id"]
    id2 = _archive(client, attach)["id"]
    changed = _diffs(_compare(client, id1, id2), "changed")
    facets = _changed_facets(changed[0])
    da, db = facets["disposition"]
    assert da == {"type": "inline", "params": {}}
    assert db == {"type": "attachment", "params": {"filename": "f.bin"}}


# ---------------------------------------------------------------------------
# 新增 / 删除
# ---------------------------------------------------------------------------


def test_part_added_and_removed(client):
    m1 = mixed_msg([text_part("a"), text_part("b")])
    m2 = mixed_msg([text_part("a"), text_part("b"), text_part("c")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]

    body_ab = _compare(client, id1, id2)
    added = _diffs(body_ab, "added")
    assert len(added) == 1
    assert added[0]["b_path"] == "3"
    assert added[0]["a_path"] is None
    assert added[0]["node_count"] == 1
    assert added[0]["content_type"] == "text/plain"
    assert body_ab["summary"] == {"added": 1, "removed": 0, "changed": 0,
                                  "ambiguous": 0}

    # 反向比较：added/removed 与 a/b 方向一致
    body_ba = _compare(client, id2, id1)
    removed = _diffs(body_ba, "removed")
    assert len(removed) == 1
    assert removed[0]["a_path"] == "3"
    assert removed[0]["b_path"] is None


def test_added_subtree_node_count(client):
    inner = make_multipart([text_part("x"), text_part("y")], "IN")
    inner_part = (
        b"Content-Type: multipart/alternative; boundary=IN\r\n\r\n" + inner
    )
    m1 = mixed_msg([text_part("only")])
    m2 = mixed_msg([text_part("only"), inner_part])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    added = _diffs(_compare(client, id1, id2), "added")
    assert len(added) == 1
    assert added[0]["b_path"] == "2"
    assert added[0]["node_count"] == 3  # 2 容器 + 2/1 + 2/2


# ---------------------------------------------------------------------------
# 重排：内容指纹配对
# ---------------------------------------------------------------------------


def test_siblings_reordered_recognized(client):
    m1 = mixed_msg([text_part("AAA"), text_part("BBB")])
    m2 = mixed_msg([text_part("BBB"), text_part("AAA")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    # 纯互换、无内容差异：不产生任何 diff（路径不是身份）
    assert body["identical"] is True
    assert body["diffs"] == []


def test_reorder_with_change_uses_fingerprint_identity(client):
    # A 端：[相同部分, 变更部分]；B 端顺序互换 [变更部分', 相同部分]。
    # 同指纹跨位置配对后，变更必须落在各自的 "2"/"1" 路径上，而不是
    # 被位置对齐误判成两个 changed。
    m1 = mixed_msg([text_part("SAME"), text_part("v1")])
    m2 = mixed_msg([text_part("v2"), text_part("SAME")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    changed = _diffs(body, "changed")
    assert len(changed) == 1
    assert changed[0]["a_path"] == "2"
    assert changed[0]["b_path"] == "1"
    assert _diffs(body, "added") == []
    assert _diffs(body, "removed") == []


def test_reorder_with_added_part(client):
    # A: [A, B]  B 端: [C(新增), B, A] —— A/B 靠指纹跨位置配对，
    # 位置兜底只剩 C，判 added 而非把 A、B 错判成 changed。
    m1 = mixed_msg([text_part("A"), text_part("B")])
    m2 = mixed_msg([text_part("C"), text_part("B"), text_part("A")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    assert _diffs(body, "changed") == []
    added = _diffs(body, "added")
    assert len(added) == 1 and added[0]["b_path"] == "1"


def test_nested_container_change_paired_across_reorder(client):
    inner_a = (
        b"Content-Type: multipart/alternative; boundary=IA\r\n\r\n"
        + make_multipart([text_part("t1"), text_part("t2")], "IA")
    )
    inner_b = (
        b"Content-Type: multipart/alternative; boundary=IB\r\n\r\n"
        + make_multipart([text_part("t1"), text_part("DIFFERENT")], "IB")
    )
    # A 端：[plain, alternative]；B 端把 alternative 放前面
    m1 = (
        b"Content-Type: multipart/mixed; boundary=OUT\r\n\r\n"
        + make_multipart([text_part("plain"), inner_a], "OUT")
    )
    m2 = (
        b"Content-Type: multipart/mixed; boundary=OUT\r\n\r\n"
        + make_multipart([inner_b, text_part("plain")], "OUT")
    )
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    changed = _diffs(body, "changed")
    # 容器本身未变，内部叶子 2/2 ↔ 1/2 的 leaf 变更
    paths = {(d["a_path"], d["b_path"]) for d in changed}
    assert ("2/2", "1/2") in paths
    assert all(
        "leaf" in _changed_facets(d) or set(_changed_facets(d)) != set()
        for d in changed
    )
    assert _diffs(body, "added") == []
    assert _diffs(body, "removed") == []


# ---------------------------------------------------------------------------
# 歧义：同层同指纹重复
# ---------------------------------------------------------------------------


def test_ambiguous_two_identical_parts_swapped(client):
    # 双方各有两个内容完全相同的叶子，且顺序无法区分：整组 ambiguous。
    # 用 UNIQ 的位置重排使两封邮件整体指纹不同（否则会被归档幂等
    # 合并成同一 id）；DUP 组仍是双方各两份、无法区分。
    m1 = mixed_msg([text_part("DUP"), text_part("DUP"), text_part("UNIQ")])
    m2 = mixed_msg([text_part("UNIQ"), text_part("DUP"), text_part("DUP")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    assert id1 != id2
    body = _compare(client, id1, id2)
    amb = _diffs(body, "ambiguous")
    assert len(amb) == 1
    d = amb[0]
    assert d["a_paths"] == ["1", "2"]
    assert d["b_paths"] == ["2", "3"]
    assert len(d["fingerprint"]) == 64
    assert d["content_type"] == "text/plain"
    # 歧义组不再进入 added/removed/changed；UNIQ 唯一配对无差异
    assert _diffs(body, "changed") == []
    assert _diffs(body, "added") == []
    assert _diffs(body, "removed") == []


def test_ambiguous_one_vs_two_copies(client):
    # A 端 1 份 DUP（另有一个唯一项），B 端 2 份 DUP：DUP 组歧义，
    # 不把多出来的一份简单判 added（无法确定哪份是“新增”）。
    m1 = mixed_msg([text_part("DUP"), text_part("UNIQ")])
    m2 = mixed_msg([text_part("DUP"), text_part("DUP"), text_part("UNIQ")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    amb = _diffs(body, "ambiguous")
    assert len(amb) == 1
    assert amb[0]["a_paths"] == ["1"]
    assert amb[0]["b_paths"] == ["1", "2"]
    assert body["summary"]["ambiguous"] == 1
    # UNIQ 仍靠指纹唯一配对，没有任何 added/changed
    assert _diffs(body, "added") == []
    assert _diffs(body, "changed") == []


def test_two_vs_one_copies_reverse(client):
    m1 = mixed_msg([text_part("DUP"), text_part("DUP"), text_part("UNIQ")])
    m2 = mixed_msg([text_part("DUP"), text_part("UNIQ")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    amb = _diffs(_compare(client, id1, id2), "ambiguous")
    assert len(amb) == 1
    assert amb[0]["a_paths"] == ["1", "2"]
    assert amb[0]["b_paths"] == ["1"]


def test_duplicate_fingerprint_but_unique_extra_not_ambiguous(client):
    # A 端两份 DUP；B 端两份 DUP + 一份 NEW。DUP 组歧义，NEW 判 added。
    m1 = mixed_msg([text_part("DUP"), text_part("DUP")])
    m2 = mixed_msg([text_part("DUP"), text_part("DUP"), text_part("NEW")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    assert len(_diffs(body, "ambiguous")) == 1
    added = _diffs(body, "added")
    assert len(added) == 1 and added[0]["b_path"] == "3"


def test_distinct_same_count_not_ambiguous(client):
    # 两侧各两个、内容互不相同：位置对齐 + 指纹配对都能唯一落对，
    # 不得误报歧义。
    m1 = mixed_msg([text_part("a1"), text_part("a2")])
    m2 = mixed_msg([text_part("b1"), text_part("b2")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    assert _diffs(body, "ambiguous") == []
    changed = _diffs(body, "changed")
    assert {d["a_path"] for d in changed} == {"1", "2"}


# ---------------------------------------------------------------------------
# 结构差异：容器 ↔ 叶子
# ---------------------------------------------------------------------------


def test_container_replaced_by_leaf(client):
    inner = make_multipart([text_part("x"), text_part("y")], "IN")
    inner_part = (
        b"Content-Type: multipart/alternative; boundary=IN\r\n\r\n" + inner
    )
    m1 = mixed_msg([inner_part, text_part("z")])
    m2 = mixed_msg([text_part("now plain"), text_part("z")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    # 位置 1 配对：节点本身 content_type facet 变更；A 端容器的两个
    # 子节点在该层对齐中全部 removed（B 端 1 是叶子，无对应子节点）。
    changed = _diffs(body, "changed")
    assert any(d["a_path"] == "1" and d["b_path"] == "1" for d in changed)
    removed = _diffs(body, "removed")
    assert {d["a_path"] for d in removed} == {"1/1", "1/2"}


def test_leaf_replaced_by_container(client):
    inner = make_multipart([text_part("x"), text_part("y")], "IN")
    inner_part = (
        b"Content-Type: multipart/alternative; boundary=IN\r\n\r\n" + inner
    )
    m1 = mixed_msg([text_part("now plain"), text_part("z")])
    m2 = mixed_msg([inner_part, text_part("z")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    changed = _diffs(body, "changed")
    assert any(d["a_path"] == "1" and d["b_path"] == "1" for d in changed)
    added = _diffs(body, "added")
    assert {d["b_path"] for d in added} == {"1/1", "1/2"}


# ---------------------------------------------------------------------------
# 封装报文（message/rfc822）与二进制歧义
# ---------------------------------------------------------------------------


def _rfc822_part(subject: str, body: str = "inner body") -> bytes:
    inner = f"Subject: {subject}\r\n\r\n{body}\r\n".encode()
    return (
        b"Content-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment\r\n\r\n" + inner
    )


def test_encapsulated_message_recursed(client):
    m1 = mixed_msg([text_part("cover"), _rfc822_part("old")])
    m2 = mixed_msg([text_part("cover"), _rfc822_part("new")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    changed = _diffs(body, "changed")
    # 封装件在 2，内部根节点路径为 2/1，其 headers facet（Subject）变更
    assert any(
        d["a_path"] == "2/1" and d["b_path"] == "2/1"
        and "headers" in _changed_facets(d)
        for d in changed
    )
    # 外层 cover、封装容器本身均无差异
    assert not any(
        d["a_path"] in ("", "1", "2") and d["b_path"] in ("", "1", "2")
        for d in changed
    )


def test_encapsulated_inner_leaf_changed(client):
    m1 = mixed_msg([text_part("cover"), _rfc822_part("s", "body-a")])
    m2 = mixed_msg([text_part("cover"), _rfc822_part("s", "body-b")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    changed = _diffs(_compare(client, id1, id2), "changed")
    leaf_diffs = [d for d in changed if d["a_path"] == "2/1"]
    assert leaf_diffs
    assert "leaf" in _changed_facets(leaf_diffs[0])


def test_ambiguous_identical_blobs(client):
    import base64

    def png_part(data: bytes = b"PNG") -> bytes:
        return (
            b"Content-Type: image/png\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + base64.b64encode(data)
        )

    # 双方各两份完全相同的 PNG，用一个不同的文本叶子位置区分整体指纹
    m1 = mixed_msg([text_part("COVER"), png_part(), png_part()])
    m2 = mixed_msg([png_part(), png_part(), text_part("COVER")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    amb = _diffs(body, "ambiguous")
    assert len(amb) == 1
    assert amb[0]["a_paths"] == ["2", "3"]
    assert amb[0]["b_paths"] == ["1", "2"]
    assert amb[0]["content_type"] == "image/png"
    assert len(amb[0]["fingerprint"]) == 64
    assert _diffs(body, "changed") == []


# ---------------------------------------------------------------------------
# 响应结构与方向
# ---------------------------------------------------------------------------


def test_response_refs_and_summary(client):
    m1 = mixed_msg([text_part("a")])
    m2 = mixed_msg([text_part("b")])
    r1 = _archive(client, m1)
    r2 = _archive(client, m2)
    body = _compare(client, r1["id"], r2["id"])
    assert body["a"] == {"id": r1["id"], "fingerprint": r1["fingerprint"]}
    assert body["b"] == {"id": r2["id"], "fingerprint": r2["fingerprint"]}
    assert body["summary"]["changed"] == 1
    # 每个 changed 同时携带双方路径
    d = body["diffs"][0]
    assert d["a_path"] == d["b_path"] == "1"


def test_diff_order_is_stable_document_order(client):
    # 三处叶子差异 1/3/5；同时构造一个重排 + 新增，验证排序确定性。
    m1 = mixed_msg(
        [text_part("c1"), text_part("x"), text_part("c3"),
         text_part("y"), text_part("c5")]
    )
    m2 = mixed_msg(
        [text_part("C1"), text_part("x"), text_part("C3"),
         text_part("y"), text_part("C5")]
    )
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]
    body = _compare(client, id1, id2)
    changed_paths = [d["a_path"] for d in _diffs(body, "changed")]
    assert changed_paths == ["1", "3", "5"]
    # 重复请求逐字节稳定
    again = _compare(client, id1, id2)
    assert again == body


# ---------------------------------------------------------------------------
# 错误与只读
# ---------------------------------------------------------------------------


def test_same_id_returns_400(client):
    mid = _archive(client, b"Subject: s\r\n\r\nb\r\n")["id"]
    r = client.post("/messages/compare", json={"a": mid, "b": mid})
    assert r.status_code == 400
    assert str(mid) in r.json()["detail"]


def test_missing_ids_return_404(client):
    mid = _archive(client, b"Subject: s\r\n\r\nb\r\n")["id"]
    r1 = client.post("/messages/compare", json={"a": 999, "b": mid})
    assert r1.status_code == 404
    assert "999" in r1.json()["detail"]
    r2 = client.post("/messages/compare", json={"a": mid, "b": 998})
    assert r2.status_code == 404
    assert "998" in r2.json()["detail"]
    r3 = client.post("/messages/compare", json={"a": 999, "b": 998})
    assert r3.status_code == 404


def test_invalid_request_returns_422(client):
    assert client.post("/messages/compare", json={"a": 1}).status_code == 422
    assert client.post("/messages/compare", json={"a": "x", "b": 2}).status_code == 422
    assert client.post("/messages/compare", json={"a": 0, "b": 2}).status_code == 422
    assert client.post("/messages/compare", json={}).status_code == 422


def test_compare_is_read_only(client):
    m1 = mixed_msg([text_part("a")])
    m2 = mixed_msg([text_part("bb")])
    id1 = _archive(client, m1)["id"]
    id2 = _archive(client, m2)["id"]

    before_msgs = client.get("/messages").json()
    before_nodes_1 = client.get(f"/messages/{id1}/nodes").json()
    before_nodes_2 = client.get(f"/messages/{id2}/nodes").json()

    for _ in range(3):
        r = client.post("/messages/compare", json={"a": id1, "b": id2})
        assert r.status_code == 200

    assert client.get("/messages").json() == before_msgs
    assert client.get(f"/messages/{id1}/nodes").json() == before_nodes_1
    assert client.get(f"/messages/{id2}/nodes").json() == before_nodes_2


def test_compare_root_single_node_messages(client):
    id1 = _archive(client, b"Subject: s\r\n\r\nbody-a\r\n")["id"]
    id2 = _archive(client, b"Subject: s\r\n\r\nbody-b\r\n")["id"]
    body = _compare(client, id1, id2)
    changed = _diffs(body, "changed")
    assert len(changed) == 1
    assert changed[0]["a_path"] == ""
    assert changed[0]["b_path"] == ""
    facets = _changed_facets(changed[0])
    assert set(facets) == {"leaf"}
