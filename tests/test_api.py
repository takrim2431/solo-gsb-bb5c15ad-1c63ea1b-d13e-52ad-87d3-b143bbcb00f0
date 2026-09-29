"""HTTP API 端到端测试。"""

from conftest import make_multipart, text_part


SIMPLE = (
    b"From: alice@example.com\r\n"
    b"Subject: API test\r\n\r\n"
    b"body content\r\n"
)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_and_get_message(client):
    r = client.post(
        "/messages",
        content=SIMPLE,
        headers={"Content-Type": "message/rfc822"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert body["node_count"] == 1
    assert body["content_type"] == "text/plain"
    mid = body["id"]

    r2 = client.get(f"/messages/{mid}")
    assert r2.status_code == 200
    assert r2.json()["fingerprint"] == body["fingerprint"]


def test_duplicate_submission_is_idempotent(client):
    r1 = client.post("/messages", content=SIMPLE)
    r2 = client.post("/messages", content=SIMPLE)
    assert r1.status_code == 201
    assert r2.status_code == 201
    b1, b2 = r1.json(), r2.json()
    assert b1["fingerprint"] == b2["fingerprint"]
    assert b1["id"] == b2["id"]
    assert b1["created"] is True
    assert b2["created"] is False

    listing = client.get("/messages").json()
    assert listing["total"] == 1
    assert len(listing["items"]) == 1


def test_empty_body_rejected_400(client):
    r = client.post("/messages", content=b"")
    assert r.status_code == 400


def test_parse_error_returns_422_with_node_path(client):
    bad = (
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"no boundary lines anywhere in here"
    )
    r = client.post("/messages", content=bad)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_BOUNDARY"
    assert err["node_path"] == ""
    assert "B" in err["message"]


def test_nested_error_locates_node(client):
    bad_inner = b"Content-Type: multipart/alternative; boundary=IN\r\n\r\nbroken"
    outer = (
        b"Content-Type: multipart/mixed; boundary=OUT\r\n\r\n"
        + make_multipart([text_part("ok"), bad_inner], "OUT")
    )
    r = client.post("/messages", content=outer)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_BOUNDARY"
    assert err["node_path"] == "2"


def test_failed_parse_writes_nothing(client):
    # 先存一封正常邮件
    ok = client.post("/messages", content=SIMPLE).json()
    total_before = client.get("/messages").json()["total"]

    # 再提交损坏邮件（多种损坏，逐一验证列表总数不变）
    bad_messages = [
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\nnope",
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n\xff\xff",
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n@@@\r\n",
        b"Broken Header without colon\r\n\r\nx\r\n",
    ]
    for bad in bad_messages:
        r = client.post("/messages", content=bad)
        assert r.status_code == 422, r.text

    listing = client.get("/messages").json()
    assert listing["total"] == total_before == 1

    # 已存在的邮件及其节点仍可正常查询
    nodes = client.get(f"/messages/{ok['id']}/nodes").json()["nodes"]
    assert len(nodes) == 1


def test_tree_and_nodes_endpoints(client):
    inner = make_multipart([text_part("a"), text_part("b")], "IN")
    inner_part = (
        b"Content-Type: multipart/alternative; boundary=IN\r\n\r\n" + inner
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=OUT\r\n\r\n"
        + make_multipart([text_part("top"), inner_part], "OUT")
    )
    mid = client.post("/messages", content=outer).json()["id"]

    # 嵌套树
    tree = client.get(f"/messages/{mid}/tree").json()["tree"]
    assert tree["path"] == ""
    assert tree["kind"] == "multipart"
    assert [c["path"] for c in tree["children"]] == ["1", "2"]
    nested = tree["children"][1]
    assert [c["path"] for c in nested["children"]] == ["2/1", "2/2"]

    # 扁平节点列表
    nodes = client.get(f"/messages/{mid}/nodes").json()["nodes"]
    assert [n["path"] for n in nodes] == ["", "1", "2", "2/1", "2/2"]

    # 单节点详情：根
    root = client.get(f"/messages/{mid}/nodes/").json()["node"]
    assert root["content_type"] == "multipart/mixed"
    assert root["children"] == ["1", "2"]
    assert root["text"] is None

    # 叶子节点文本
    leaf = client.get(f"/messages/{mid}/nodes/2/2").json()["node"]
    assert leaf["kind"] == "text"
    assert leaf["text"] == "b"
    assert leaf["headers"]  # 至少带 Content-Type 头


def test_node_detail_404(client):
    mid = client.post("/messages", content=SIMPLE).json()["id"]
    r = client.get(f"/messages/{mid}/nodes/9/9")
    assert r.status_code == 404


def test_message_404(client):
    assert client.get("/messages/999").status_code == 404
    assert client.get("/messages/999/tree").status_code == 404


def test_pagination(client):
    for i in range(3):
        msg = f"Subject: m{i}\r\n\r\nbody {i}\r\n".encode()
        client.post("/messages", content=msg)
    page1 = client.get("/messages?limit=2&offset=0").json()
    page2 = client.get("/messages?limit=2&offset=2").json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1


def test_header_case_and_line_ending_same_fingerprint_via_api(client):
    m1 = b"FROM: a@x.com\r\nSUBJECT: S\r\n\r\nb\r\n"
    m2 = b"from: a@x.com\nsubject: S\n\nb\n"
    r1 = client.post("/messages", content=m1).json()
    r2 = client.post("/messages", content=m2).json()
    assert r1["id"] == r2["id"]
    assert r1["fingerprint"] == r2["fingerprint"]


def test_blob_node_has_hash(client):
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"AAECAwQ=\r\n"
    )
    mid = client.post("/messages", content=msg).json()["id"]
    node = client.get(f"/messages/{mid}/nodes/").json()["node"]
    assert node["kind"] == "blob"
    assert node["blob_length"] == 5
    assert len(node["blob_sha256"]) == 64


def test_rfc2047_header_stored_decoded(client):
    msg = b"Subject: =?utf-8?B?5L2g5aW9?=\r\n\r\nx\r\n"
    mid = client.post("/messages", content=msg).json()["id"]
    node = client.get(f"/messages/{mid}/nodes/").json()["node"]
    headers = dict(node["headers"])
    assert headers["Subject"] == "你好"
