"""cid: 内嵌资源引用审计（GET /messages/{id}/related-resources）测试。

覆盖：四属性提取与文档序、RFC 2392 百分号解码、最近
multipart/related 祖先作用域（相邻不串联、嵌套不串联、非 related
容器透明穿透）、resolved/missing/ambiguous 三态、orphan 列表、
重复 Content-ID 歧义、同 ID 跨作用域合法、404。
"""

from conftest import make_multipart

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def html_part(html: str, cte: str | None = None, charset: str = "utf-8") -> bytes:
    cte_header = (
        f"Content-Transfer-Encoding: {cte}\r\n".encode() if cte else b""
    )
    body = html.encode(charset)
    if cte == "base64":
        import base64

        body = base64.b64encode(body)
    elif cte == "quoted-printable":
        # 测试里不需要真正的 QP：纯 ASCII HTML 保持原样即可
        pass
    return (
        b"Content-Type: text/html; charset=" + charset.encode() + b"\r\n"
        + cte_header
        + b"\r\n"
        + body
    )


def cid_part(cid: str, *, ctype: str = "image/png", data: bytes = b"PNGDATA") -> bytes:
    head = (
        f"Content-Type: {ctype}\r\n"
        f"Content-ID: <{cid}>\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
    ).encode()
    return head + _b64(data)


def cid_part_raw(header_value: str, *, ctype: str = "image/png") -> bytes:
    """Content-ID 头值完全自定义（用于裸 ID、折叠等脏输入）。"""

    head = (
        f"Content-Type: {ctype}\r\n"
        f"Content-ID: {header_value}\r\n\r\n"
    ).encode()
    return head + b"RAWBODY"


def _b64(data: bytes) -> bytes:
    import base64

    return base64.b64encode(data)


def _container(ctype: str, parts: list[bytes], boundary: str) -> bytes:
    """构造“multipart 容器作为邮件/部分”的原始字节。

    conftest.make_multipart 的正文以边界行开头（无前导 CRLF），
    分隔符正则允许 ``^--bnd``，故头块的 ``\\r\\n\\r\\n`` 之后直接
    拼接即可。
    """

    body = make_multipart(parts, boundary)
    assert body.startswith(b"--" + boundary.encode())
    return (
        f"Content-Type: {ctype}; boundary={boundary}\r\n\r\n".encode() + body
    )


def related(parts: list[bytes], boundary: str = "REL") -> bytes:
    return _container("multipart/related", parts, boundary)


def mixed(parts: list[bytes], boundary: str = "MIX") -> bytes:
    return _container("multipart/mixed", parts, boundary)


def post(client, data: bytes) -> int:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def audit(client, mid: int):
    r = client.get(f"/messages/{mid}/related-resources")
    assert r.status_code == 200, r.text
    return r.json()


def ref_index(body) -> dict[tuple[str, str], dict]:
    """按 (源节点, cid) 索引引用（每个 cid 在单测里只出现一次）。"""

    return {(x["source_node_path"], x["cid"]): x for x in body["references"]}


# ---------------------------------------------------------------------------
# 基本解析：四属性 + 文档序
# ---------------------------------------------------------------------------


HTML_ALL_ATTRS = (
    "<html><body>"
    '<img src="cid:logo@x.com">'
    '<a href="cid:doc@x.com">d</a>'
    '<video poster="cid:poster@x.com"></video>'
    '<img srcset="cid:a@x.com 1x, cid:b@x.com 2x">'
    "</body></html>"
)


def test_all_four_attributes_resolved_in_order(client):
    msg = related(
        [
            html_part(HTML_ALL_ATTRS),
            cid_part("logo@x.com"),
            cid_part("doc@x.com", ctype="application/pdf"),
            cid_part("poster@x.com", ctype="image/jpeg"),
            cid_part("a@x.com"),
            cid_part("b@x.com"),
        ]
    )
    mid = post(client, msg)
    body = audit(client, mid)

    refs = body["references"]
    assert len(refs) == 5
    assert [r["attribute"] for r in refs] == [
        "src",
        "href",
        "poster",
        "srcset",
        "srcset",
    ]
    assert [r["cid"] for r in refs] == [
        "logo@x.com",
        "doc@x.com",
        "poster@x.com",
        "a@x.com",
        "b@x.com",
    ]
    assert {r["status"] for r in refs} == {"resolved"}
    assert {r["scope_node_path"] for r in refs} == {""}
    # 目标节点按文档序：HTML 为 1，资源依次 2..6
    assert [r["target_node_path"] for r in refs] == [
        "2",
        "3",
        "4",
        "5",
        "6",
    ]
    assert body["scopes"] == [{"node_path": "", "orphans": []}]


def test_non_cid_urls_are_ignored(client):
    html = (
        '<img src="http://example.com/a.png">'
        '<img src="data:image/png;base64,AAA">'
        '<a href="mailto:x@y.com">m</a>'
        '<img src="cid:only@x.com">'
    )
    msg = related([html_part(html), cid_part("only@x.com")])
    body = audit(client, post(client, msg))
    cids = [r["cid"] for r in body["references"]]
    assert cids == ["only@x.com"]


# ---------------------------------------------------------------------------
# RFC 2392 百分号解码
# ---------------------------------------------------------------------------


def test_percent_encoded_cid_is_decoded(client):
    html = '<img src="cid:lo%67o%40x.com">'
    msg = related([html_part(html), cid_part("logo@x.com")])
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["cid"] == "logo@x.com"
    assert r["status"] == "resolved"
    assert r["target_node_path"] == "2"


def test_percent_encoded_utf8_cid(client):
    # cid:图%20片@x.com → "图 片@x.com"；Content-ID 头里直接放 UTF-8
    html = '<img src="cid:%E5%9B%BE%20%E7%89%87@x.com">'
    msg = related(
        [
            html_part(html),
            (
                "Content-Type: image/png\r\n"
                "Content-ID: <图 片@x.com>\r\n\r\nX"
            ).encode("utf-8"),
        ]
    )
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["cid"] == "图 片@x.com"
    assert r["status"] == "resolved"


def test_cid_scheme_case_insensitive(client):
    html = '<img SRC="CID:logo@x.com">'
    msg = related([html_part(html), cid_part("logo@x.com")])
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["status"] == "resolved"


def test_cid_with_angle_brackets_tolerated(client):
    # RFC 2392 允许尖括号作为 cid: URL 分隔符出现在属性里
    html = '<img src="cid:<logo@x.com>">'
    msg = related([html_part(html), cid_part("logo@x.com")])
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["cid"] == "logo@x.com"
    assert r["status"] == "resolved"


def test_cid_percent_encoded_angle_brackets_tolerated(client):
    html = '<img src="cid:%3Clogo@x.com%3E">'
    msg = related([html_part(html), cid_part("logo@x.com")])
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["cid"] == "logo@x.com"
    assert r["status"] == "resolved"


def test_bare_content_id_header_matches(client):
    # 不符合 RFC 的裸 Content-ID（无尖括号）也按字面参与匹配
    msg = related(
        [html_part('<img src="cid:bare-1234">'), cid_part_raw("bare-1234")]
    )
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["status"] == "resolved"


# ---------------------------------------------------------------------------
# 作用域：相邻不串联、嵌套不串联、非 related 透明
# ---------------------------------------------------------------------------


def test_adjacent_related_scopes_do_not_share_cids(client):
    # multipart/mixed 下两个相邻 multipart/related，各含同 ID 资源；
    # 第一个 related 的 HTML 只能命中 1/2，不能串到第二个 related。
    rel1 = related(
        [
            html_part('<img src="cid:logo@x.com">'),
            cid_part("logo@x.com"),
        ],
        boundary="R1",
    )
    rel2 = related(
        [
            html_part('<img src="cid:other@x.com">'),
            cid_part("logo@x.com"),
            cid_part("other@x.com"),
        ],
        boundary="R2",
    )
    msg = mixed([rel1, rel2])
    body = audit(client, post(client, msg))
    idx = ref_index(body)

    r1 = idx[("1/1", "logo@x.com")]
    assert r1["status"] == "resolved"
    assert r1["target_node_path"] == "1/2"
    assert r1["scope_node_path"] == "1"

    r2 = idx[("2/1", "other@x.com")]
    assert r2["status"] == "resolved"
    assert r2["target_node_path"] == "2/3"
    assert r2["scope_node_path"] == "2"

    # 两个作用域都无 orphan（2/2 的 logo 被 2/1 引用吗？没有！）
    scopes = {s["node_path"]: s for s in body["scopes"]}
    assert scopes["1"]["orphans"] == []
    # 作用域 2 中 logo 未被其 HTML 引用 → orphan
    assert [o["node_path"] for o in scopes["2"]["orphans"]] == ["2/2"]


def test_nested_related_scopes_do_not_chain(client):
    # 外层 related：HTML(引用 logo) + 资源 logo + 内层 related(HTML+资源)
    # 外层 HTML 不得匹配内层资源；内层资源对外层而言是 orphan。
    inner = related(
        [
            html_part('<img src="cid:inner@x.com">'),
            cid_part("inner@x.com"),
        ],
        boundary="RIN",
    )
    msg = related(
        [
            html_part('<img src="cid:logo@x.com">'),
            cid_part("logo@x.com"),
            inner,
        ],
        boundary="ROUT",
    )
    # 根邮件本身就是 multipart/related：路径 ""（外层作用域）与 "3"（内层）
    body = audit(client, post(client, msg))
    idx = ref_index(body)

    outer = idx[("1", "logo@x.com")]
    assert outer["status"] == "resolved"
    assert outer["target_node_path"] == "2"
    assert outer["scope_node_path"] == ""

    inn = idx[("3/1", "inner@x.com")]
    assert inn["status"] == "resolved"
    assert inn["target_node_path"] == "3/2"
    assert inn["scope_node_path"] == "3"

    scopes = {s["node_path"]: s for s in body["scopes"]}
    # 外层作用域：节点 3 是 message/multipart 容器，无 Content-ID，
    # 内层资源路径 3/2 不属于外层作用域，故外层无 orphan。
    assert scopes[""]["orphans"] == []
    assert scopes["3"]["orphans"] == []


def test_nested_related_outer_reference_does_not_reach_inner(client):
    # 仅内层存在 logo：外层 HTML 引用必须 missing（禁止向内串联）
    # 结构：root related = [1 html, 2 inner related [2/1 html, 2/2 logo]]
    inner = related(
        [
            html_part("<p>inner</p>"),
            cid_part("logo@x.com"),
        ],
        boundary="RIN",
    )
    msg = related(
        [html_part('<img src="cid:logo@x.com">'), inner],
        boundary="ROUT",
    )
    body = audit(client, post(client, msg))
    refs = [r for r in body["references"] if r["cid"] == "logo@x.com"]
    assert len(refs) == 1
    assert refs[0]["status"] == "missing"
    assert refs[0]["target_node_path"] is None

    # 内层资源在其作用域内未被引用 → orphan
    scopes = {s["node_path"]: s for s in body["scopes"]}
    assert [o["node_path"] for o in scopes["2"]["orphans"]] == ["2/2"]


def test_multipart_mixed_is_a_transparent_pass_through(client):
    # related → mixed → [html, resource]：最近 related 祖先仍是根
    msg = related(
        [
            mixed(
                [
                    html_part('<img src="cid:logo@x.com">'),
                    cid_part("logo@x.com"),
                ],
                boundary="M",
            )
        ],
        boundary="R",
    )
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["status"] == "resolved"
    assert r["source_node_path"] == "1/1"
    assert r["target_node_path"] == "1/2"
    assert r["scope_node_path"] == ""


def test_html_outside_related_scope_is_missing(client):
    # multipart/mixed 中的 HTML 引用：无 related 祖先，一律 missing，
    # 且响应不产生任何 scope 条目。
    msg = mixed(
        [
            html_part('<img src="cid:logo@x.com">'),
            cid_part("logo@x.com"),
        ]
    )
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["status"] == "missing"
    assert r["scope_node_path"] is None
    assert body["scopes"] == []


# ---------------------------------------------------------------------------
# 三态：resolved / missing / ambiguous
# ---------------------------------------------------------------------------


def test_missing_reference(client):
    msg = related(
        [
            html_part('<img src="cid:nope@x.com">'),
            cid_part("real@x.com"),
        ]
    )
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["status"] == "missing"
    assert r["target_node_path"] is None
    assert r["candidates"] == []
    # 未被引用的 real → orphan
    (scope,) = body["scopes"]
    assert [o["node_path"] for o in scope["orphans"]] == ["2"]
    assert scope["orphans"][0]["content_ids"] == ["real@x.com"]
    assert scope["orphans"][0]["content_type"] == "image/png"


def test_duplicate_content_id_in_same_scope_is_ambiguous(client):
    msg = related(
        [
            html_part('<img src="cid:dup@x.com">'),
            cid_part("dup@x.com"),
            cid_part("dup@x.com"),
        ]
    )
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["status"] == "ambiguous"
    assert r["target_node_path"] is None
    assert r["candidates"] == ["2", "3"]
    # 歧义候选被引用过，不算 orphan
    assert body["scopes"][0]["orphans"] == []


def test_same_content_id_in_different_scopes_is_resolved_not_ambiguous(client):
    rel1 = related(
        [html_part('<img src="cid:logo@x.com">'), cid_part("logo@x.com")],
        boundary="R1",
    )
    rel2 = related(
        [html_part('<img src="cid:logo@x.com">'), cid_part("logo@x.com")],
        boundary="R2",
    )
    msg = mixed([rel1, rel2])
    body = audit(client, post(client, msg))
    assert len(body["references"]) == 2
    assert {r["status"] for r in body["references"]} == {"resolved"}
    targets = {r["target_node_path"] for r in body["references"]}
    assert targets == {"1/2", "2/2"}


def test_multiple_missing_and_one_resolved(client):
    html = (
        '<img src="cid:a@x.com">'
        '<img src="cid:missing1@x.com">'
        '<img src="cid:missing2@x.com">'
    )
    msg = related([html_part(html), cid_part("a@x.com")])
    body = audit(client, post(client, msg))
    statuses = {r["cid"]: r["status"] for r in body["references"]}
    assert statuses == {
        "a@x.com": "resolved",
        "missing1@x.com": "missing",
        "missing2@x.com": "missing",
    }


# ---------------------------------------------------------------------------
# orphan
# ---------------------------------------------------------------------------


def test_orphan_lists_only_unreferced_resources(client):
    html = '<img src="cid:used@x.com">'
    msg = related(
        [
            html_part(html),
            cid_part("used@x.com"),
            cid_part("orphan1@x.com"),
            cid_part("orphan2@x.com", ctype="image/gif"),
        ]
    )
    body = audit(client, post(client, msg))
    (scope,) = body["scopes"]
    assert [o["node_path"] for o in scope["orphans"]] == ["3", "4"]
    assert scope["orphans"][1]["content_type"] == "image/gif"


def test_empty_scope_has_no_orphans(client):
    # related 中只有 HTML，没有任何 Content-ID 资源
    msg = related([html_part("<p>x</p>")])
    body = audit(client, post(client, msg))
    assert body["references"] == []
    assert body["scopes"] == [{"node_path": "", "orphans": []}]


def test_scope_without_html_all_resources_are_orphans(client):
    plain = b"Content-Type: text/plain\r\n\r\nplain"
    msg = related([plain, cid_part("a@x.com")])
    body = audit(client, post(client, msg))
    assert body["references"] == []
    (scope,) = body["scopes"]
    assert [o["node_path"] for o in scope["orphans"]] == ["2"]


# ---------------------------------------------------------------------------
# 扫描边界
# ---------------------------------------------------------------------------


def test_only_text_html_leaves_are_scanned(client):
    # 同作用域内一个 text/plain 叶子的 cid: 文本不得被当作引用
    plain = (
        b"Content-Type: text/plain\r\n\r\n"
        b'<img src="cid:secret@x.com">'
    )
    html = '<img src="cid:logo@x.com">'
    msg = related(
        [
            plain,
            html_part(html),
            cid_part("logo@x.com"),
            cid_part("secret@x.com"),
        ]
    )
    body = audit(client, post(client, msg))
    cids = {r["cid"] for r in body["references"]}
    assert cids == {"logo@x.com"}
    # secret 未被 HTML 引用 → orphan
    assert [o["node_path"] for o in body["scopes"][0]["orphans"]] == ["4"]


def test_html_with_base64_cte_is_decoded_before_scan(client):
    msg = related(
        [
            html_part('<img src="cid:logo@x.com">', cte="base64"),
            cid_part("logo@x.com"),
        ]
    )
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["status"] == "resolved"


def test_empty_cid_reference_is_missing(client):
    msg = related(
        [html_part('<img src="cid:">'), cid_part("logo@x.com")]
    )
    body = audit(client, post(client, msg))
    (r,) = body["references"]
    assert r["cid"] == ""
    assert r["status"] == "missing"


def test_simple_non_multipart_message_has_empty_audit(client):
    msg = b"Subject: plain\r\n\r\nbody\r\n"
    body = audit(client, post(client, msg))
    assert body["references"] == []
    assert body["scopes"] == []


# ---------------------------------------------------------------------------
# 404 与响应元数据
# ---------------------------------------------------------------------------


def test_message_not_found_returns_404(client):
    r = client.get("/messages/999/related-resources")
    assert r.status_code == 404
    assert "999" in r.json()["detail"]


def test_response_carries_message_fingerprint(client):
    msg = related([html_part("<p>x</p>")])
    created = client.post("/messages", content=msg).json()
    body = audit(client, created["id"])
    assert body["id"] == created["id"]
    assert body["fingerprint"] == created["fingerprint"]
