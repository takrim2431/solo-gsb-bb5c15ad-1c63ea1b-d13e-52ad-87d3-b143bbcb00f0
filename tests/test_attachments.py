"""内容寻址附件库测试：归档去重、检索、Range 下载。"""

import base64
import hashlib

from conftest import make_multipart, text_part


def bin_part(
    data: bytes,
    *,
    ctype: str = "application/octet-stream",
    name: str | None = None,
    disposition: str | None = "attachment",
    filename: str | None = None,
    cte: str | None = "base64",
) -> bytes:
    """构造一个非文本叶子部分（默认 base64 编码的附件）。"""

    ct = f"Content-Type: {ctype}"
    if name is not None:
        ct += f'; name="{name}"'
    lines = [ct]
    if disposition is not None:
        disp = f"Content-Disposition: {disposition}"
        if filename is not None:
            disp += f'; filename="{filename}"'
        lines.append(disp)
    if cte is not None:
        lines.append(f"Content-Transfer-Encoding: {cte}")
    head = "\r\n".join(lines).encode() + b"\r\n\r\n"
    body = base64.b64encode(data) if cte == "base64" else data
    return head + body


def message_with_parts(*parts: bytes, subject: str = "attach test") -> bytes:
    return (
        f"Subject: {subject}\r\n".encode()
        + b"Content-Type: multipart/mixed; boundary=MIX\r\n\r\n"
        + make_multipart(list(parts), "MIX")
    )


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
PDF = b"%PDF-1.4 fake" * 3


def post(client, data: bytes) -> dict:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 归档与去重
# ---------------------------------------------------------------------------


def test_blob_leaves_become_attachments_text_does_not(client):
    msg = message_with_parts(
        text_part("plain body"),
        bin_part(PNG, ctype="image/png", filename="a.png"),
        bin_part(PDF, filename="b.pdf"),
    )
    mid = post(client, msg)["id"]

    listing = client.get("/attachments").json()
    assert listing["total"] == 2
    by_hash = {a["sha256"]: a for a in listing["items"]}
    assert set(by_hash) == {sha(PNG), sha(PDF)}

    png = by_hash[sha(PNG)]
    assert png["size_bytes"] == len(PNG)
    assert png["media_type"] == "image/png"
    assert png["reference_count"] == 1
    assert png["occurrences"] == [
        {
            "message_id": mid,
            "node_path": "2",
            "filename": "a.png",
            "disposition": "attachment",
            "media_type": "image/png",
        }
    ]
    pdf = by_hash[sha(PDF)]
    assert pdf["occurrences"][0]["node_path"] == "3"
    assert pdf["occurrences"][0]["filename"] == "b.pdf"


def test_identical_blob_twice_in_one_message_single_entity(client):
    msg = message_with_parts(
        bin_part(PNG, ctype="image/png", filename="x.png"),
        bin_part(PNG, ctype="image/png", filename="y.png"),
    )
    mid = post(client, msg)["id"]

    listing = client.get("/attachments").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["sha256"] == sha(PNG)
    assert item["reference_count"] == 2
    occs = item["occurrences"]
    assert [o["node_path"] for o in occs] == ["1", "2"]
    assert [o["filename"] for o in occs] == ["x.png", "y.png"]
    assert all(o["message_id"] == mid for o in occs)


def test_same_blob_across_messages_shares_entity(client):
    m1 = message_with_parts(bin_part(PDF, filename="one.pdf"), subject="m1")
    m2 = message_with_parts(
        text_part("hi"), bin_part(PDF, filename="two.pdf"), subject="m2"
    )
    id1 = post(client, m1)["id"]
    id2 = post(client, m2)["id"]
    assert id1 != id2

    listing = client.get("/attachments").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["reference_count"] == 2
    by_message = {o["message_id"]: o for o in item["occurrences"]}
    assert by_message[id1]["node_path"] == "1"
    assert by_message[id1]["filename"] == "one.pdf"
    assert by_message[id2]["node_path"] == "2"
    assert by_message[id2]["filename"] == "two.pdf"


def test_rearchive_same_message_adds_nothing(client):
    msg = message_with_parts(bin_part(PNG, ctype="image/png"))
    first = post(client, msg)
    assert first["created"] is True
    before = client.get("/attachments").json()

    second = post(client, msg)
    assert second["created"] is False
    assert second["id"] == first["id"]

    after = client.get("/attachments").json()
    assert after["total"] == before["total"] == 1
    assert after["items"][0]["reference_count"] == 1
    assert after["items"][0]["occurrences"] == before["items"][0]["occurrences"]


def test_root_level_blob_message_is_archived(client):
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="raw.bin"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(PNG)
    )
    mid = post(client, msg)["id"]
    listing = client.get("/attachments").json()
    assert listing["total"] == 1
    occ = listing["items"][0]["occurrences"][0]
    assert occ["message_id"] == mid
    assert occ["node_path"] == ""  # 根节点
    assert occ["filename"] == "raw.bin"


def test_blob_inside_encapsulated_message_is_archived(client):
    inner = (
        b"Content-Type: multipart/mixed; boundary=IN\r\n\r\n"
        + make_multipart(
            [text_part("inner"), bin_part(PDF, filename="deep.pdf")], "IN"
        )
    )
    outer = b"Content-Type: message/rfc822\r\n\r\n" + inner
    mid = post(client, outer)["id"]
    listing = client.get("/attachments").json()
    assert listing["total"] == 1
    occ = listing["items"][0]["occurrences"][0]
    assert occ["message_id"] == mid
    assert occ["node_path"] == "1/2"
    assert occ["filename"] == "deep.pdf"


def test_rfc2231_filename_stored_decoded(client):
    part = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment;\r\n"
        b" filename*=utf-8''caf%C3%A9.txt\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(PDF)
    )
    post(client, message_with_parts(part))
    item = client.get("/attachments").json()["items"][0]
    assert item["occurrences"][0]["filename"] == "café.txt"


def test_content_type_name_used_as_filename_fallback(client):
    post(
        client,
        message_with_parts(bin_part(PNG, ctype="image/png", name="n.png")),
    )
    item = client.get("/attachments").json()["items"][0]
    assert item["occurrences"][0]["filename"] == "n.png"


def test_no_disposition_no_filename(client):
    post(
        client,
        message_with_parts(bin_part(PNG, ctype="image/png", disposition=None)),
    )
    item = client.get("/attachments").json()["items"][0]
    occ = item["occurrences"][0]
    assert occ["filename"] is None
    assert occ["disposition"] is None


def test_failed_parse_writes_no_attachments(client):
    post(client, message_with_parts(bin_part(PNG, ctype="image/png")))
    before = client.get("/attachments").json()["total"]

    bad = b"Content-Type: multipart/mixed; boundary=B\r\n\r\nno boundary here"
    r = client.post("/messages", content=bad)
    assert r.status_code == 422
    assert client.get("/attachments").json()["total"] == before


# ---------------------------------------------------------------------------
# 检索与分页
# ---------------------------------------------------------------------------


def test_filter_by_sha256_prefix_and_full(client):
    post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="photo.png"), subject="s1"
    ))
    post(client, message_with_parts(bin_part(PDF, filename="doc.pdf"), subject="s2"))

    full = sha(PDF)
    r = client.get(f"/attachments?sha256={full}").json()
    assert r["total"] == 1
    assert r["items"][0]["sha256"] == full

    prefix = client.get(f"/attachments?sha256={full[:12]}").json()
    assert prefix["total"] == 1
    assert prefix["items"][0]["sha256"] == full

    none = client.get(f"/attachments?sha256={'0' * 64}").json()
    assert none["total"] == 0
    assert none["items"] == []


def test_filter_by_filename_substring(client):
    post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="photo.png"), subject="s1"
    ))
    post(client, message_with_parts(bin_part(PDF, filename="doc.pdf"), subject="s2"))

    r = client.get("/attachments?filename=doc").json()
    assert r["total"] == 1
    assert r["items"][0]["sha256"] == sha(PDF)

    # 子串匹配且大小写不敏感（ASCII）
    r2 = client.get("/attachments?filename=PHOTO").json()
    assert r2["total"] == 1
    assert r2["items"][0]["sha256"] == sha(PNG)


def test_filter_by_media_type(client):
    post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="p.png"), subject="s1"
    ))
    post(client, message_with_parts(bin_part(PDF, filename="d.pdf"), subject="s2"))

    r = client.get("/attachments?media_type=image/png").json()
    assert r["total"] == 1
    assert r["items"][0]["sha256"] == sha(PNG)

    r2 = client.get("/attachments?media_type=application/octet-stream").json()
    assert r2["total"] == 1
    assert r2["items"][0]["sha256"] == sha(PDF)


def test_attachments_pagination(client):
    for i in range(3):
        post(client, message_with_parts(
            bin_part(f"payload-{i}".encode(), filename=f"f{i}.bin"),
            subject=f"s{i}",
        ))
    page1 = client.get("/attachments?limit=2&offset=0").json()
    page2 = client.get("/attachments?limit=2&offset=2").json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    # 两页不重叠且覆盖全部
    hashes = {a["sha256"] for a in page1["items"] + page2["items"]}
    assert len(hashes) == 3


def test_invalid_sha256_filter_rejected(client):
    r = client.get("/attachments?sha256=not-hex!")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 下载与 Range
# ---------------------------------------------------------------------------


def _archive_one(client, data: bytes = PNG) -> str:
    post(client, message_with_parts(
        bin_part(data, ctype="image/png", filename="x.png")
    ))
    return sha(data)


def test_download_full_content(client):
    h = _archive_one(client)
    r = client.get(f"/attachments/{h}/content")
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == str(len(PNG))


def test_download_unknown_hash_404(client):
    r = client.get(f"/attachments/{'0' * 64}/content")
    assert r.status_code == 404


def test_download_range_basic(client):
    h = _archive_one(client)
    r = client.get(f"/attachments/{h}/content", headers={"Range": "bytes=1-3"})
    assert r.status_code == 206
    assert r.content == PNG[1:4]
    assert r.headers["content-range"] == f"bytes 1-3/{len(PNG)}"
    assert r.headers["accept-ranges"] == "bytes"


def test_download_range_open_end(client):
    h = _archive_one(client)
    r = client.get(f"/attachments/{h}/content", headers={"Range": "bytes=5-"})
    assert r.status_code == 206
    assert r.content == PNG[5:]
    assert r.headers["content-range"] == f"bytes 5-{len(PNG) - 1}/{len(PNG)}"


def test_download_range_suffix(client):
    h = _archive_one(client)
    r = client.get(f"/attachments/{h}/content", headers={"Range": "bytes=-4"})
    assert r.status_code == 206
    assert r.content == PNG[-4:]
    n = len(PNG)
    assert r.headers["content-range"] == f"bytes {n - 4}-{n - 1}/{n}"


def test_download_range_end_beyond_size_is_clamped(client):
    h = _archive_one(client)
    n = len(PNG)
    r = client.get(
        f"/attachments/{h}/content", headers={"Range": f"bytes=0-{n + 100}"}
    )
    assert r.status_code == 206
    assert r.content == PNG
    assert r.headers["content-range"] == f"bytes 0-{n - 1}/{n}"


def test_download_range_full_span(client):
    h = _archive_one(client)
    n = len(PNG)
    r = client.get(
        f"/attachments/{h}/content", headers={"Range": f"bytes=0-{n - 1}"}
    )
    assert r.status_code == 206
    assert r.content == PNG


def test_range_out_of_bounds_416(client):
    h = _archive_one(client)
    n = len(PNG)
    for spec in (f"bytes={n}-", f"bytes={n + 5}-{n + 10}"):
        r = client.get(f"/attachments/{h}/content", headers={"Range": spec})
        assert r.status_code == 416, spec
        assert r.headers["content-range"] == f"bytes */{n}"


def test_range_multi_part_416(client):
    h = _archive_one(client)
    r = client.get(
        f"/attachments/{h}/content", headers={"Range": "bytes=0-1,3-4"}
    )
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(PNG)}"


def test_range_malformed_416(client):
    h = _archive_one(client)
    n = len(PNG)
    for spec in ("bytes=3-1", "bytes=-0", "bytes=abc-def", "items=0-1", "bytes="):
        r = client.get(f"/attachments/{h}/content", headers={"Range": spec})
        assert r.status_code == 416, spec
        assert r.headers["content-range"] == f"bytes */{n}"


def test_empty_attachment_download_and_range(client):
    h = _archive_one(client, b"")
    r = client.get(f"/attachments/{h}/content")
    assert r.status_code == 200
    assert r.content == b""

    r2 = client.get(f"/attachments/{h}/content", headers={"Range": "bytes=0-"})
    assert r2.status_code == 416
    assert r2.headers["content-range"] == "bytes */0"
