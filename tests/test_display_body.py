"""展示正文选择（GET /messages/{id}/display-body）测试。

覆盖：
* 叶子根（text 直出 / blob 404）；
* multipart/alternative：prefer 命中、无偏好时末位回退、嵌套 related
  分支按其实际正文参与偏好比较、attachment 分支不可展示、空文本可展示；
* multipart/related：start 命中（含尖括号/百分号容忍）、未配置取首个
  子件、start 指向非展示部分时不静默改选、只匹配直接子件、重复命中
  422（定位 related 节点）、未命中 422；
* multipart/mixed 及其他子类型取首个可展示子件；
* 屏障：attachment disposition（含容器与大写）、inline 不屏蔽、
  message/rfc822 / message/global 内部正文不作为候选；
* 未被选中的 alternative 分支内的 start 错误不阻断展示；全部不可展示
  时首个路径错误上抛为 422；
* 404、prefer 非法值 422、轨迹逐层记录、纯只读不改写旧记录。
"""

import base64

from conftest import make_multipart

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def html_part(html: str = "<p>html body</p>", charset: str = "utf-8") -> bytes:
    return (
        b"Content-Type: text/html; charset=" + charset.encode() + b"\r\n\r\n"
        + html.encode(charset)
    )


def plain_part(text: str = "plain body", charset: str = "utf-8") -> bytes:
    return (
        b"Content-Type: text/plain; charset=" + charset.encode() + b"\r\n\r\n"
        + text.encode(charset)
    )


def text_part_of(subtype: str, body: str) -> bytes:
    return (
        f"Content-Type: text/{subtype}; charset=utf-8\r\n\r\n".encode()
        + body.encode()
    )


def attachment_part(body: str = "attached text", *, filename: str = "f.txt") -> bytes:
    return (
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f'Content-Disposition: attachment; filename="{filename}"\r\n\r\n'
        .encode()
        + body.encode()
    )


def inline_part(body: str = "inline text") -> bytes:
    return (
        b"Content-Type: text/plain\r\nContent-Disposition: inline\r\n\r\n"
        + body.encode()
    )


def image_part(cid: str | None = None, *, data: bytes = b"PNGDATA") -> bytes:
    cid_header = f"Content-ID: <{cid}>\r\n".encode() if cid else b""
    return (
        b"Content-Type: image/png\r\n"
        + cid_header
        + b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(data)
    )


def rfc822_part(inner: bytes, *, ctype: str = "message/rfc822") -> bytes:
    return f"Content-Type: {ctype}\r\n\r\n".encode() + inner


def container(
    ctype: str,
    parts: list[bytes],
    bnd: str,
    *,
    params: str = "",
) -> bytes:
    """multipart 容器作为邮件/部分；params 追加在 Content-Type 行。"""

    return (
        f"Content-Type: {ctype}; boundary={bnd}{params}\r\n\r\n".encode()
        + make_multipart(parts, bnd)
    )


def alternative(parts, bnd="ALT", **kw):
    return container("multipart/alternative", parts, bnd, **kw)


def related(parts, bnd="REL", **kw):
    return container("multipart/related", parts, bnd, **kw)


def mixed(parts, bnd="MIX", **kw):
    return container("multipart/mixed", parts, bnd, **kw)


def post(client, data: bytes) -> int:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def display(client, mid: int, prefer: str | None = None):
    url = f"/messages/{mid}/display-body"
    if prefer is not None:
        url += f"?prefer={prefer}"
    return client.get(url)


def rules(body) -> list[str]:
    return [s["rule"] for s in body["trail"]]


# ---------------------------------------------------------------------------
# 根叶子
# ---------------------------------------------------------------------------


def test_root_plain_text_is_returned_directly(client):
    mid = post(client, b"Subject: s\r\n\r\nhello\r\n")
    r = display(client, mid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["node_path"] == ""
    assert body["content_type"] == "text/plain"
    assert body["text"] == "hello\n"
    assert body["trail"] == []
    assert body["prefer"] == "html"  # 缺省


def test_root_html_respects_prefer_echo(client):
    mid = post(client, html_part("<p>x</p>"))
    body = display(client, mid, "plain").json()
    assert body["prefer"] == "plain"
    assert body["content_type"] == "text/html"  # 根叶子无备选，原样返回


def test_root_blob_has_no_display_body(client):
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nAAEC\r\n"
    )
    mid = post(client, msg)
    assert display(client, mid).status_code == 404


def test_root_rfc822_inner_body_is_not_a_candidate(client):
    inner = b"Subject: forwarded\r\n\r\ninner body\r\n"
    mid = post(client, rfc822_part(inner))
    r = display(client, mid)
    assert r.status_code == 404
    # 内层报文节点确实存在且文本已解码，但不会被选为外层正文
    nodes = client.get(f"/messages/{mid}/nodes").json()["nodes"]
    assert [n["path"] for n in nodes] == ["", "1"]
    detail = client.get(f"/messages/{mid}/nodes/1").json()["node"]
    assert detail["text"] == "inner body\n"


def test_root_message_global_inner_body_is_not_a_candidate(client):
    inner = b"Subject: fwd global\r\n\r\nglobal body"
    mid = post(client, rfc822_part(inner, ctype="message/global"))
    assert display(client, mid).status_code == 404


def test_root_attachment_text_is_not_a_candidate(client):
    mid = post(client, attachment_part("do not show"))
    r = display(client, mid)
    assert r.status_code == 404


def test_inline_disposition_is_still_displayable(client):
    mid = post(client, inline_part("shown inline"))
    body = display(client, mid).json()
    assert body["text"] == "shown inline"


# ---------------------------------------------------------------------------
# multipart/alternative
# ---------------------------------------------------------------------------


def test_alternative_prefers_html_then_plain(client):
    msg = alternative([plain_part("P"), html_part("<p>H</p>")])
    mid = post(client, msg)

    h = display(client, mid, "html").json()
    assert h["node_path"] == "2"
    assert h["content_type"] == "text/html"
    assert h["text"] == "<p>H</p>"
    (step,) = h["trail"]
    assert step["rule"] == "alternative-prefer"
    assert step["considered"] == ["1", "2"]
    assert step["candidates"] == ["1", "2"]
    assert step["chosen"] == "2"
    assert step["detail"] == {"prefer": "html"}

    p = display(client, mid, "plain").json()
    assert p["node_path"] == "1"
    assert p["content_type"] == "text/plain"
    assert p["text"] == "P"
    assert p["trail"][0]["rule"] == "alternative-prefer"
    assert p["trail"][0]["chosen"] == "1"


def test_alternative_default_prefer_is_html(client):
    msg = alternative([plain_part("P"), html_part("<p>H</p>")])
    mid = post(client, msg)
    assert display(client, mid).json()["node_path"] == "2"


def test_alternative_falls_back_to_last_displayable(client):
    # 无 html：plain + text/enriched，prefer=html 命中不了，
    # 取最后一个可展示分支（RFC 2046 末位最忠实）。
    msg = alternative([plain_part("P"), text_part_of("enriched", "<bold>E</bold>")])
    mid = post(client, msg)
    body = display(client, mid, "html").json()
    assert body["node_path"] == "2"
    assert body["content_type"] == "text/enriched"
    (step,) = body["trail"]
    assert step["rule"] == "alternative-fallback-last"
    assert step["candidates"] == ["1", "2"]
    assert step["chosen"] == "2"
    assert step["detail"] == {"prefer": "html"}


def test_alternative_skips_blob_and_attachment_branches(client):
    # [图片 blob, attachment 文本, html, plain]
    msg = alternative(
        [
            image_part(),
            attachment_part("attached"),
            html_part("<p>H</p>"),
            plain_part("P"),
        ]
    )
    mid = post(client, msg)

    h = display(client, mid, "html").json()
    assert h["node_path"] == "3"
    (step,) = h["trail"]
    assert step["considered"] == ["1", "2", "3", "4"]
    assert step["candidates"] == ["3", "4"]  # 仅 text 叶子分支

    p = display(client, mid, "plain").json()
    assert p["node_path"] == "4"
    assert p["trail"][0]["rule"] == "alternative-prefer"


def test_alternative_empty_text_leaf_is_displayable(client):
    # 分支 2 是空 text/plain：仍是可展示候选（空正文也是正文）
    msg = alternative([image_part(), b"Content-Type: text/plain\r\n\r\n"])
    mid = post(client, msg)
    body = display(client, mid, "html").json()
    assert body["node_path"] == "2"
    assert body["text"] == ""
    assert body["trail"][0]["rule"] == "alternative-fallback-last"


def test_alternative_all_branches_non_displayable_is_404(client):
    msg = alternative([image_part(), attachment_part("a")])
    mid = post(client, msg)
    assert display(client, mid).status_code == 404


def test_alternative_nested_related_branch_resolves_actual_body(client):
    # 分支 2 是 multipart/related[html, image]：其实际正文是 html，
    # 应作为 prefer=html 的命中分支并逐层给出轨迹。
    rel = related([html_part("<p>H</p>"), image_part("logo")])
    msg = alternative([plain_part("P"), rel])
    mid = post(client, msg)

    h = display(client, mid, "html").json()
    assert h["node_path"] == "2/1"
    assert h["content_type"] == "text/html"
    assert rules(h) == ["alternative-prefer", "related-first"]
    alt_step, rel_step = h["trail"]
    assert alt_step["node_path"] == ""
    assert alt_step["chosen"] == "2"
    assert alt_step["candidates"] == ["1", "2"]
    assert rel_step["node_path"] == "2"
    assert rel_step["chosen"] == "2/1"

    # prefer=plain 走分支 1
    p = display(client, mid, "plain").json()
    assert p["node_path"] == "1"
    assert rules(p) == ["alternative-prefer"]


# ---------------------------------------------------------------------------
# multipart/related
# ---------------------------------------------------------------------------


def test_related_without_start_takes_first_child(client):
    msg = related([html_part("<p>H</p>"), image_part("logo")])
    mid = post(client, msg)
    body = display(client, mid).json()
    assert body["node_path"] == "1"
    assert body["content_type"] == "text/html"
    (step,) = body["trail"]
    assert step["rule"] == "related-first"
    assert step["chosen"] == "1"
    assert step["candidates"] == ["1"]
    assert "start" not in step["detail"]


def test_related_without_start_first_child_non_displayable_is_404(client):
    # 首个子件是图片：缺省根部分不可展示，不静默改选第 2 个 html
    msg = related([image_part("logo"), html_part("<p>H</p>")])
    mid = post(client, msg)
    r = display(client, mid)
    assert r.status_code == 404


def test_related_without_start_first_child_attachment_is_404(client):
    msg = related([attachment_part("a"), html_part("<p>H</p>")])
    mid = post(client, msg)
    assert display(client, mid).status_code == 404


def test_related_start_matches_direct_child_content_id(client):
    msg = related(
        [
            image_part("logo"),
            b"Content-Type: text/html\r\nContent-ID: <body@x>\r\n\r\n"
            b"<p>H</p>",
        ],
        params='; start="body@x"',
    )
    mid = post(client, msg)
    body = display(client, mid).json()
    assert body["node_path"] == "2"
    assert body["content_type"] == "text/html"
    (step,) = body["trail"]
    assert step["rule"] == "related-start"
    assert step["chosen"] == "2"
    assert step["candidates"] == ["2"]
    assert step["detail"] == {"start": "body@x"}


def test_related_start_tolerates_angle_brackets(client):
    # start 值带尖括号，Content-ID 也带尖括号，剥一层后相等
    msg = related(
        [
            image_part("logo"),
            b"Content-Type: text/html\r\nContent-ID: <body@x>\r\n\r\n"
            b"<p>H</p>",
        ],
        params="; start=<body@x>",
    )
    mid = post(client, msg)
    body = display(client, mid).json()
    assert body["node_path"] == "2"
    assert body["trail"][0]["detail"]["start"] == "body@x"


def test_related_start_rfc2231_percent_encoded(client):
    msg = related(
        [
            b"Content-Type: text/html\r\nContent-ID: <body@x.com>\r\n\r\n"
            b"<p>H</p>",
            image_part("logo"),
        ],
        params="; start*=us-ascii''body%40x.com",
    )
    mid = post(client, msg)
    body = display(client, mid).json()
    assert body["node_path"] == "1"
    assert body["trail"][0]["rule"] == "related-start"
    assert body["trail"][0]["detail"]["start"] == "body@x.com"


def test_related_start_target_non_displayable_is_404(client):
    # start 权威指向图片：不可展示即 404，不回退首个 html
    msg = related(
        [html_part("<p>H</p>"), image_part("img")],
        params='; start="img"',
    )
    mid = post(client, msg)
    assert display(client, mid).status_code == 404


def test_related_start_not_found_is_422_located_at_related(client):
    msg = related(
        [html_part("<p>H</p>"), image_part("logo")],
        params='; start="missing@x"',
    )
    mid = post(client, msg)
    r = display(client, mid)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "RELATED_START_NOT_FOUND"
    assert err["node_path"] == ""  # 根 related
    assert err["detail"]["start"] == "missing@x"


def test_related_start_does_not_match_nested_descendants(client):
    # 同 ID 只出现在内层 related 中：外层 start 不得向后代串联
    inner = related(
        [
            b"Content-Type: text/html\r\nContent-ID: <x@y>\r\n\r\n"
            b"<p>inner</p>"
        ],
        bnd="RIN",
    )
    msg = related(
        [inner, plain_part("P")],
        "ROUT",
        params='; start="x@y"',
    )
    mid = post(client, msg)
    r = display(client, mid)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "RELATED_START_NOT_FOUND"
    assert err["node_path"] == ""

    # 直接子件与内层后代同 ID：唯一直接命中，忽略内层
    msg2 = related(
        [
            b"Content-Type: text/html\r\nContent-ID: <x@y>\r\n\r\n"
            b"<p>direct</p>",
            inner,
        ],
        "ROUT2",
        params='; start="x@y"',
    )
    mid2 = post(client, msg2)
    body = display(client, mid2).json()
    assert body["node_path"] == "1"


def test_related_duplicate_start_is_422_with_candidates(client):
    msg = related(
        [
            html_part("<p>H</p>"),
            b"Content-Type: image/png\r\nContent-ID: <dup@x>\r\n\r\nX",
            b"Content-Type: image/gif\r\nContent-ID: <dup@x>\r\n\r\nY",
        ],
        params='; start="dup@x"',
    )
    mid = post(client, msg)
    r = display(client, mid)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "AMBIGUOUS_RELATED_START"
    assert err["node_path"] == ""
    assert err["detail"]["start"] == "dup@x"
    assert err["detail"]["candidates"] == ["2", "3"]


def test_related_duplicate_start_nested_is_located_at_related_node(client):
    # mixed[1: related(start 重复), 2: plain]：错误定位到 related 节点 "1"
    broken = related(
        [
            b"Content-Type: image/png\r\nContent-ID: <dup@x>\r\n\r\nX",
            b"Content-Type: image/gif\r\nContent-ID: <dup@x>\r\n\r\nY",
        ],
        "RBAD",
        params='; start="dup@x"',
    )
    msg = mixed([broken, plain_part("P")], "MIX")
    mid = post(client, msg)
    r = display(client, mid)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "AMBIGUOUS_RELATED_START"
    assert err["node_path"] == "1"
    assert err["detail"]["candidates"] == ["1/1", "1/2"]


# ---------------------------------------------------------------------------
# multipart/mixed 与其他子类型
# ---------------------------------------------------------------------------


def test_mixed_takes_first_displayable_child(client):
    msg = mixed(
        [
            image_part(),  # blob：跳过
            attachment_part("a"),  # attachment：跳过
            html_part("<p>H</p>"),
            plain_part("P"),
        ]
    )
    mid = post(client, msg)
    body = display(client, mid).json()
    assert body["node_path"] == "3"
    assert body["content_type"] == "text/html"
    (step,) = body["trail"]
    assert step["rule"] == "first-displayable"
    assert step["considered"] == ["1", "2", "3"]
    assert step["candidates"] == ["3"]
    assert step["chosen"] == "3"


def test_mixed_skips_rfc822_encapsulated_body(client):
    inner = b"Subject: fwd\r\n\r\ninner body\r\n"
    msg = mixed(
        [
            rfc822_part(inner),  # 封装报文：屏障，内部 html 不算候选
            plain_part("outer"),
        ]
    )
    mid = post(client, msg)
    body = display(client, mid).json()
    assert body["node_path"] == "2"
    assert body["text"] == "outer"
    assert body["trail"][0]["considered"] == ["1", "2"]


def test_mixed_only_attachments_and_rfc822_is_404(client):
    inner = b"Subject: fwd\r\n\r\ninner\r\n"
    msg = mixed([rfc822_part(inner), attachment_part("a"), image_part()])
    mid = post(client, msg)
    assert display(client, mid).status_code == 404


def test_mixed_skips_attached_alternative_container(client):
    attached_alt = (
        b"Content-Type: multipart/alternative; boundary=A\r\n"
        b"Content-Disposition: attachment\r\n\r\n"
        + make_multipart([plain_part("secret"), html_part("<p>secret</p>")], "A")
    )
    msg = mixed([attached_alt, plain_part("visible")], "MIX")
    mid = post(client, msg)
    body = display(client, mid).json()
    assert body["node_path"] == "2"
    assert body["text"] == "visible"
    (step,) = body["trail"]
    assert step["considered"] == ["1", "2"]
    assert step["chosen"] == "2"


def test_other_multipart_subtype_uses_first_displayable(client):
    # multipart/signed：[正文, 签名]，正文在首件
    sig = b"Content-Type: application/pgp-signature\r\n\r\nSIG"
    msg = container(
        "multipart/signed",
        [plain_part("signed body"), sig],
        "S",
        params='; protocol="application/pgp-signature"',
    )
    mid = post(client, msg)
    body = display(client, mid).json()
    assert body["node_path"] == "1"
    assert body["text"] == "signed body"
    assert body["trail"][0]["rule"] == "first-displayable"


def test_deep_nesting_records_full_trail(client):
    # mixed[图片, alternative[plain, related[html, 图片]]] prefer=html
    inner_rel = related([html_part("<p>H</p>"), image_part("logo")], "RIN")
    alt = alternative([plain_part("P"), inner_rel], "A")
    msg = mixed([image_part(), alt], "M")
    mid = post(client, msg)
    body = display(client, mid, "html").json()
    assert body["node_path"] == "2/2/1"
    assert body["content_type"] == "text/html"
    assert rules(body) == [
        "first-displayable",
        "alternative-prefer",
        "related-first",
    ]
    paths = [s["node_path"] for s in body["trail"]]
    assert paths == ["", "2", "2/2"]


# ---------------------------------------------------------------------------
# 未选中分支的 start 错误不阻断；全不可展示时上抛
# ---------------------------------------------------------------------------


def test_start_error_in_unselected_alternative_branch_is_ignored(client):
    # 分支 1 plain 正常；分支 2 related start 重复命中（配置错误）。
    broken_rel = related(
        [
            b"Content-Type: image/png\r\nContent-ID: <dup@x>\r\n\r\nX",
            b"Content-Type: image/gif\r\nContent-ID: <dup@x>\r\n\r\nY",
        ],
        "RBAD",
        params='; start="dup@x"',
    )
    msg = alternative([plain_part("P"), broken_rel])
    mid = post(client, msg)

    # prefer=plain：直接选分支 1，不触碰分支 2 的错误
    p = display(client, mid, "plain")
    assert p.status_code == 200
    assert p.json()["node_path"] == "1"

    # prefer=html：分支 2 解析失败被跳过，回退末位可展示分支（分支 1）
    h = display(client, mid, "html")
    assert h.status_code == 200
    hj = h.json()
    assert hj["node_path"] == "1"
    assert hj["trail"][0]["rule"] == "alternative-fallback-last"
    # 分支 2 被评估但不在候选中
    assert hj["trail"][0]["considered"] == ["1", "2"]
    assert hj["trail"][0]["candidates"] == ["1"]


def test_start_error_raises_when_no_displayable_branch(client):
    # 分支 1 related start 重复且无正文；分支 2 图片。
    # 没有任何可展示分支 → 路径上唯一的配置错误上抛为 422。
    broken_rel = related(
        [
            b"Content-Type: image/png\r\nContent-ID: <dup@x>\r\n\r\nX",
            b"Content-Type: image/gif\r\nContent-ID: <dup@x>\r\n\r\nY",
        ],
        "RBAD",
        params='; start="dup@x"',
    )
    msg = alternative([broken_rel, image_part()])
    mid = post(client, msg)
    r = display(client, mid)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "AMBIGUOUS_RELATED_START"
    assert err["node_path"] == "1"


# ---------------------------------------------------------------------------
# 参数 / 404 / 只读
# ---------------------------------------------------------------------------


def test_invalid_prefer_value_is_422(client):
    mid = post(client, b"Subject: s\r\n\r\nx\r\n")
    r = display(client, mid, "rtf")
    assert r.status_code == 422


def test_message_not_found_is_404(client):
    r = display(client, 999)
    assert r.status_code == 404
    assert "999" in r.json()["detail"]


def test_response_carries_fingerprint_and_metadata(client):
    created = client.post(
        "/messages",
        content=alternative([plain_part("P"), html_part("<p>H</p>")]),
    ).json()
    body = display(client, created["id"]).json()
    assert body["id"] == created["id"]
    assert body["fingerprint"] == created["fingerprint"]


def test_display_body_is_read_only(client):
    msg = alternative([plain_part("P"), html_part("<p>H</p>")])
    mid = post(client, msg)
    before = client.get(f"/messages/{mid}/nodes").json()
    total_before = client.get("/messages").json()["total"]

    r1 = display(client, mid, "html")
    r2 = display(client, mid, "plain")
    assert r1.status_code == r2.status_code == 200

    after = client.get(f"/messages/{mid}/nodes").json()
    assert after == before  # 节点记录未被改写
    assert client.get("/messages").json()["total"] == total_before
