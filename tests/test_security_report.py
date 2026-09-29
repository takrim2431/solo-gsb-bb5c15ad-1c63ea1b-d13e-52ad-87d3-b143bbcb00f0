"""附件安全策略评估（GET /messages/{id}/security-report）测试。

覆盖：totals 递归累计（解码字节/附件数/深度）、双扩展与控制字符
文件名、Content-Disposition filename → Content-Type name 回退、
禁止媒体类型、嵌套 message/rfc822、阈值边界与覆盖、默认放行、
404/422、重复请求稳定性与只读不改写。
"""

import base64

from conftest import make_multipart, text_part

TAB = chr(9)
NUL = chr(0)
DEL = chr(0x7F)
C1 = chr(0x85)


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def bin_part(
    data: bytes,
    *,
    ctype: str = "application/octet-stream",
    name: str | None = None,
    disposition: str | None = "attachment",
    filename: str | None = None,
    cte: str | None = "base64",
) -> bytes:
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


def message_with_parts(
    *parts: bytes, subject: str = "security test", boundary: str = "MIX"
) -> bytes:
    return (
        f"Subject: {subject}\r\n".encode()
        + f"Content-Type: multipart/mixed; boundary={boundary}\r\n\r\n".encode()
        + make_multipart(list(parts), boundary)
    )


def post(client, data: bytes) -> int:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def report(client, mid: int, **params):
    r = client.get(f"/messages/{mid}/security-report", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def codes(body) -> list[str]:
    return [v["code"] for v in body["violations"]]


def by_path(body) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for v in body["violations"]:
        out.setdefault(v["node_path"], []).append(v)
    return out


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
TXT = b"just a text attachment"


# ---------------------------------------------------------------------------
# 放行 / totals
# ---------------------------------------------------------------------------


def test_clean_message_is_allowed(client):
    msg = message_with_parts(
        text_part("plain body"),
        bin_part(PNG, ctype="image/png", filename="photo.png"),
        bin_part(TXT, ctype="application/octet-stream", filename="notes.bin",
                 cte=None),
    )
    body = report(client, post(client, msg))
    assert body["allow"] is True
    assert body["violations"] == []
    assert body["totals"] == {
        "decoded_bytes": len(PNG) + len(TXT),
        "attachment_count": 2,
        "max_depth": 1,
    }


def test_text_leaves_are_not_attachments(client):
    # text/* 叶子不计入附件与字节
    msg = b"Subject: plain\r\n\r\nbody\r\n"
    body = report(client, post(client, msg))
    assert body["allow"] is True
    assert body["totals"] == {
        "decoded_bytes": 0,
        "attachment_count": 0,
        "max_depth": 0,
    }


def test_same_blob_twice_counts_twice(client):
    msg = message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"),
        bin_part(PNG, ctype="image/png", filename="b.png"),
    )
    body = report(client, post(client, msg))
    assert body["totals"]["attachment_count"] == 2
    assert body["totals"]["decoded_bytes"] == len(PNG) * 2


def test_totals_recurse_into_encapsulated_message(client):
    inner = message_with_parts(
        bin_part(PNG, ctype="image/png", filename="deep.png"),
        subject="nested",
        boundary="IN",
    )
    enc = b"Content-Type: message/rfc822\r\n\r\n" + inner
    msg = message_with_parts(text_part("outer"), enc)
    body = report(client, post(client, msg))
    totals = body["totals"]
    assert totals["attachment_count"] == 1
    assert totals["decoded_bytes"] == len(PNG)
    # root=0，封装报文=2，其内部 mixed=2/1（深度 2），附件 2/1/1（深度 3）
    assert totals["max_depth"] == 3
    nested = [v for v in body["violations"] if v["code"] == "NESTED_MESSAGE"]
    assert [v["node_path"] for v in nested] == ["2"]
    assert nested[0]["detail"] == {"content_type": "message/rfc822"}


def test_multipart_root_is_not_flagged_as_nested_message(client):
    # 非 message 容器（multipart/mixed）不得被当作嵌套封装报文
    msg = message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"),
    )
    body = report(client, post(client, msg))
    assert "NESTED_MESSAGE" not in codes(body)


def test_message_rfc822_root_itself_is_not_nested(client):
    # 顶层 Content-Type 就是 message/rfc822：根（深度 0）不算嵌套，
    # 但其唯一子件是内部邮件根（kind=multipart，不是 message 容器）
    inner = message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"),
        boundary="IN",
    )
    msg = b"Content-Type: message/rfc822\r\n\r\n" + inner
    body = report(client, post(client, msg))
    assert "NESTED_MESSAGE" not in codes(body)
    assert body["totals"]["attachment_count"] == 1


def test_response_thresholds_echo_and_fingerprint(client):
    mid = post(client, message_with_parts(text_part("x")))
    body = report(client, mid)
    assert body["id"] == mid
    assert body["thresholds"] == {
        "max_decoded_bytes": 10 * 1024 * 1024,
        "max_attachment_count": 100,
        "max_mime_depth": 20,
    }
    assert isinstance(body["fingerprint"], str) and len(body["fingerprint"]) == 64


# ---------------------------------------------------------------------------
# 双扩展
# ---------------------------------------------------------------------------


def test_double_extension_violation(client):
    msg = message_with_parts(
        text_part("body"),
        bin_part(b"EXE", filename="invoice.pdf.exe",
                 ctype="application/octet-stream"),
    )
    body = report(client, post(client, msg))
    (v,) = body["violations"]
    assert v["code"] == "DOUBLE_EXTENSION"
    assert v["node_path"] == "2"
    assert v["detail"] == {
        "filename": "invoice.pdf.exe",
        "extensions": ["pdf", "exe"],
    }
    assert body["allow"] is False


def test_triple_extension_violates(client):
    msg = message_with_parts(
        bin_part(b"X", filename="a.tar.gz.gz",
                 ctype="application/octet-stream")
    )
    v = report(client, post(client, msg))["violations"][0]
    assert v["code"] == "DOUBLE_EXTENSION"
    assert v["detail"]["extensions"] == ["tar", "gz", "gz"]


def test_single_extension_and_dotfiles_are_clean(client):
    msg = message_with_parts(
        bin_part(b"X", filename="notes.txt",
                 ctype="application/octet-stream"),
        bin_part(b"Y", filename=".bashrc",
                 ctype="application/octet-stream"),
        bin_part(b"Z", filename="README",
                 ctype="application/octet-stream"),
    )
    body = report(client, post(client, msg))
    assert "DOUBLE_EXTENSION" not in codes(body)


def test_filename_falls_back_to_content_type_name(client):
    part = bin_part(
        b"X",
        ctype="application/octet-stream",
        name="report.doc.js",
        disposition=None,
        cte=None,
    )
    body = report(client, post(client, message_with_parts(part)))
    (v,) = body["violations"]
    assert v["code"] == "DOUBLE_EXTENSION"
    assert v["detail"]["filename"] == "report.doc.js"


def test_attachment_without_filename_skips_filename_checks(client):
    part = b"Content-Type: application/octet-stream\r\n\r\nraw"
    body = report(client, post(client, message_with_parts(part)))
    assert body["allow"] is True
    assert body["totals"]["attachment_count"] == 1


# ---------------------------------------------------------------------------
# 控制字符
# ---------------------------------------------------------------------------


def test_control_chars_in_filename(client):
    filename = f"invoice{TAB}pdf{NUL}.pdf.exe"
    msg = message_with_parts(
        text_part("body"),
        bin_part(b"X", filename=filename, ctype="application/octet-stream"),
    )
    body = report(client, post(client, msg))
    found = [v for v in body["violations"] if v["code"] == "FILENAME_CONTROL_CHARS"]
    (v,) = found
    assert v["node_path"] == "2"
    assert v["detail"]["filename"] == filename
    assert v["detail"]["codepoints"] == ["U+0009", "U+0000"]
    # 同一文件名同时也是双扩展
    assert "DOUBLE_EXTENSION" in codes(body)


def test_del_and_c1_control_chars_detected(client):
    filename = f"a{DEL}b{C1}.txt"
    msg = message_with_parts(
        bin_part(b"X", filename=filename, ctype="application/octet-stream")
    )
    body = report(client, post(client, msg))
    v = next(v for v in body["violations"] if v["code"] == "FILENAME_CONTROL_CHARS")
    assert v["detail"]["codepoints"] == ["U+007F", "U+0085"]


def test_control_chars_dedup_in_order(client):
    filename = f"a{TAB}b{TAB}c{NUL}d{NUL}"
    msg = message_with_parts(
        bin_part(b"X", filename=filename, ctype="application/octet-stream")
    )
    body = report(client, post(client, msg))
    v = next(v for v in body["violations"] if v["code"] == "FILENAME_CONTROL_CHARS")
    assert v["detail"]["codepoints"] == ["U+0009", "U+0000"]


# ---------------------------------------------------------------------------
# 禁止媒体类型
# ---------------------------------------------------------------------------


def test_forbidden_media_type_violation(client):
    msg = message_with_parts(
        text_part("body"),
        bin_part(b"MZ", ctype="application/x-msdownload", filename="setup.exe"),
    )
    body = report(client, post(client, msg))
    assert "FORBIDDEN_MEDIA_TYPE" in codes(body)
    hits = [v for v in by_path(body)["2"] if v["code"] == "FORBIDDEN_MEDIA_TYPE"]
    assert hits[0]["detail"] == {"content_type": "application/x-msdownload"}


def test_forbidden_match_is_case_insensitive(client):
    part = b"Content-Type: Application/X-msDownload\r\n\r\nX"
    body = report(
        client, post(client, message_with_parts(text_part("body"), part))
    )
    assert "FORBIDDEN_MEDIA_TYPE" in codes(body)
    # detail 中规范化为小写
    assert by_path(body)["2"][0]["detail"]["content_type"] == (
        "application/x-msdownload"
    )


def test_benign_media_types_are_allowed(client):
    msg = message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"),
        bin_part(b"x", ctype="application/pdf", filename="a.pdf"),
        bin_part(b"x", ctype="application/zip", filename="a.zip"),
    )
    body = report(client, post(client, msg))
    assert "FORBIDDEN_MEDIA_TYPE" not in codes(body)


# ---------------------------------------------------------------------------
# 嵌套 message
# ---------------------------------------------------------------------------


def test_nested_message_global_violates(client):
    inner = message_with_parts(text_part("wrapped"), boundary="IN")
    enc = b"Content-Type: message/global\r\n\r\n" + inner
    body = report(
        client, post(client, message_with_parts(text_part("outer"), enc))
    )
    v = next(v for v in body["violations"] if v["code"] == "NESTED_MESSAGE")
    assert v["node_path"] == "2"
    assert v["detail"] == {"content_type": "message/global"}


# ---------------------------------------------------------------------------
# 阈值：边界、覆盖、只报首个越界节点
# ---------------------------------------------------------------------------


def test_threshold_boundary_equal_is_allowed(client):
    payload = b"X" * 10
    msg = message_with_parts(
        bin_part(payload, ctype="image/png", filename="a.png")
    )
    body = report(
        client, post(client, msg),
        max_decoded_bytes=10, max_attachment_count=1, max_mime_depth=1,
    )
    assert body["allow"] is True
    assert body["violations"] == []


def test_decoded_bytes_threshold_override(client):
    msg = message_with_parts(
        text_part("body"),
        bin_part(b"12345", ctype="image/png", filename="a.png"),
        bin_part(b"67890", ctype="image/png", filename="b.png"),
    )
    body = report(client, post(client, msg), max_decoded_bytes=7)
    v = next(v for v in body["violations"] if v["code"] == "DECODED_BYTES_EXCEEDED")
    # 文档序第二个附件（节点 3）把累计 5→10 推过阈值 7
    assert v["node_path"] == "3"
    assert v["detail"] == {"limit": 7, "decoded_bytes": 10, "node_bytes": 5}
    assert body["thresholds"]["max_decoded_bytes"] == 7


def test_attachment_count_threshold_reports_only_first(client):
    msg = message_with_parts(
        text_part("body"),
        bin_part(b"a", ctype="image/png", filename="a.png"),
        bin_part(b"b", ctype="image/png", filename="b.png"),
        bin_part(b"c", ctype="image/png", filename="c.png"),
    )
    body = report(client, post(client, msg), max_attachment_count=1)
    hit = [v for v in body["violations"] if v["code"] == "ATTACHMENT_COUNT_EXCEEDED"]
    assert len(hit) == 1
    # 第二个附件（节点 3）使计数 1→2，越过阈值 1
    assert hit[0]["node_path"] == "3"
    assert hit[0]["detail"] == {"limit": 1, "count": 2}


def test_depth_threshold_reports_first_deep_node(client):
    # mixed 内先文本、再嵌套一个 message/rfc822（其自身也是嵌套违规）
    inner = message_with_parts(text_part("wrapped"), boundary="IN")
    enc = b"Content-Type: message/rfc822\r\n\r\n" + inner
    msg = message_with_parts(text_part("outer"), enc)
    body = report(client, post(client, msg), max_mime_depth=1)
    depth_hits = [
        v for v in body["violations"] if v["code"] == "MIME_DEPTH_EXCEEDED"
    ]
    assert len(depth_hits) == 1
    # 封装报文节点 2 深度 1（恰好等于阈值，允许）；其内部报文根 2/1
    # 深度 2，是文档序第一个深于 1 的节点
    assert depth_hits[0]["node_path"] == "2/1"
    assert depth_hits[0]["detail"] == {"limit": 1, "depth": 2}
    assert body["totals"]["max_depth"] == 3


def test_zero_thresholds_flag_everything_relevant(client):
    msg = message_with_parts(
        bin_part(b"X", ctype="image/png", filename="a.png")
    )
    body = report(
        client, post(client, msg),
        max_decoded_bytes=0, max_attachment_count=0, max_mime_depth=0,
    )
    assert set(codes(body)) == {
        "DECODED_BYTES_EXCEEDED",
        "ATTACHMENT_COUNT_EXCEEDED",
        "MIME_DEPTH_EXCEEDED",
    }


# ---------------------------------------------------------------------------
# 确定性与只读
# ---------------------------------------------------------------------------


def test_repeated_requests_are_stable(client):
    msg = message_with_parts(
        bin_part(b"MZ", ctype="application/x-msdownload",
                 filename="invoice.pdf.exe"),
        bin_part(b"X", ctype="image/png", filename="pic.png"),
    )
    mid = post(client, msg)
    first = report(client, mid, max_decoded_bytes=1)
    second = report(client, mid, max_decoded_bytes=1)
    assert first == second


def test_report_does_not_modify_database(client):
    msg = message_with_parts(
        bin_part(b"MZ", ctype="application/x-msdownload",
                 filename="invoice.pdf.exe")
    )
    mid = post(client, msg)
    before_tree = client.get(f"/messages/{mid}/tree").json()
    before_list = client.get("/messages").json()
    before_attachments = client.get("/attachments").json()
    client.get(f"/messages/{mid}/security-report")
    client.get(f"/messages/{mid}/security-report?max_attachment_count=0")
    assert client.get(f"/messages/{mid}/tree").json() == before_tree
    assert client.get("/messages").json() == before_list
    assert client.get("/attachments").json() == before_attachments


def test_rearchiving_same_mail_still_same_report(client):
    msg = message_with_parts(
        bin_part(b"EXE", ctype="application/x-msi", filename="a.msi")
    )
    mid1 = post(client, msg)
    r2 = client.post("/messages", content=msg)
    assert r2.json()["id"] == mid1
    assert report(client, mid1) == report(client, r2.json()["id"])


# ---------------------------------------------------------------------------
# 404 / 422 / 响应结构
# ---------------------------------------------------------------------------


def test_message_not_found_returns_404(client):
    r = client.get("/messages/999/security-report")
    assert r.status_code == 404
    assert "999" in r.json()["detail"]


def test_invalid_threshold_params_return_422(client):
    mid = post(client, message_with_parts(text_part("x")))
    for param, value in [
        ("max_decoded_bytes", -1),
        ("max_attachment_count", -5),
        ("max_mime_depth", -1),
        ("max_decoded_bytes", "abc"),
        ("max_attachment_count", 1.5),
    ]:
        r = client.get(f"/messages/{mid}/security-report", params={param: value})
        assert r.status_code == 422, (param, value, r.status_code)


def test_violation_shape_and_document_order(client):
    # 文档序 2（干净 png）、3（禁止类型 + 双扩展）
    msg = message_with_parts(
        text_part("body"),
        bin_part(PNG, ctype="image/png", filename="ok.png"),
        bin_part(b"EXE", ctype="application/x-msi", filename="doc.pdf.msi"),
    )
    body = report(client, post(client, msg))
    paths = [v["node_path"] for v in body["violations"]]
    assert paths == sorted(
        paths, key=lambda p: tuple(int(s) for s in p.split("/"))
    )
    assert all(
        set(v) == {"code", "node_path", "detail"} for v in body["violations"]
    )
    assert {v["node_path"] for v in body["violations"]} == {"3"}
