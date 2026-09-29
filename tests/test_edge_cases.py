"""解析器边界场景补充测试。"""

import pytest

from conftest import make_multipart, text_part

from app.errors import MIMEParseError
from app.mimeparser import canonical_json, canonical_tree, parse_message


def test_message_without_blank_line_has_empty_body():
    # 没有空行：全部视为头块，正文为空（结尾单个换行只是行终结符）。
    for data in (b"Subject: headers only", b"Subject: headers only\r\n"):
        tree = parse_message(data)
        assert tree.text == ""
        assert tree.media_type == "text/plain"


def test_lf_boundary_delimiters():
    body = make_multipart(
        [text_part("a"), text_part("b")], "B", crlf=False
    )
    tree = parse_message(
        b"Content-Type: multipart/mixed; boundary=B\n\n" + body
    )
    assert [c.text for c in tree.children] == ["a", "b"]


def test_message_global_is_recursive():
    # message/global 与 message/rfc822 同样递归
    inner = b"From: x@y\r\nSubject: g\r\n\r\nglobal body"
    wrapped = (
        b"Content-Type: multipart/mixed; boundary=W\r\n\r\n"
        + make_multipart(
            [b"Content-Type: message/global\r\n\r\n" + inner], "W"
        )
    )
    tree = parse_message(wrapped)
    assert tree.children[0].media_type == "message/global"
    assert tree.children[0].children[0].text == "global body"


def test_base64_encoded_message_rfc822_container():
    # 容器声明 base64：编码内容必须是“完整报文（含头/正文分隔空行）”
    inner = b"Subject: encoded container\r\n\r\ninner text\r\n"
    import base64

    encoded = base64.b64encode(inner)
    wrapped = (
        b"Content-Type: message/rfc822\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + encoded
    )
    tree = parse_message(wrapped)
    # 容器 children[0] 即解码后递归出的内层报文（默认 text/plain 叶子）
    inner_node = tree.children[0]
    assert inner_node.media_type == "text/plain"
    assert inner_node.path == "1"
    assert inner_node.text == "inner text\n"


def test_quoted_parameter_value():
    msg = (
        b'Content-Type: multipart/mixed; boundary="the boundary"\r\n\r\n'
        + make_multipart([text_part("z")], "the boundary")
    )
    tree = parse_message(msg)
    assert tree.media_type == "multipart/mixed"
    assert tree.children[0].text == "z"


def test_escaped_quote_in_parameter():
    msg = (
        b'Content-Disposition: attachment; filename="a\\"b.txt"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\nX"
    )
    tree = parse_message(msg)
    assert tree.disposition_params["filename"] == 'a"b.txt'


def test_rfc2231_charset_in_filename():
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment;\r\n"
        b" filename*=iso-8859-1''caf%E9.txt\r\n\r\nX"
    )
    tree = parse_message(msg)
    assert tree.disposition_params["filename"] == "café.txt"


def test_multipart_with_no_parts_errors():
    msg = b"Content-Type: multipart/mixed; boundary=B\r\n\r\n--B--\r\n"
    with pytest.raises(MIMEParseError) as exc:
        parse_message(msg)
    assert exc.value.code == "INVALID_BOUNDARY"


def test_multipart_subtype_is_token_validated():
    # type/subtype 必须都是 token
    with pytest.raises(MIMEParseError) as exc:
        parse_message(b"Content-Type: text/html bogus\r\n\r\nx")
    assert exc.value.code in {"INVALID_CONTENT_TYPE", "INVALID_HEADER_VALUE"}


def test_unknown_charset_in_text_errors():
    with pytest.raises(MIMEParseError) as exc:
        parse_message(
            b"Content-Type: text/plain; charset=bogus-xyz\r\n\r\nhi"
        )
    assert exc.value.code == "UNKNOWN_CHARSET"


def test_blob_and_binary_content_preserved():
    raw = bytes(range(1, 255))
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: binary\r\n\r\n" + raw
    )
    tree = parse_message(msg)
    assert tree.blob == raw
    assert tree.text is None


def test_empty_leaf_body():
    msg = b"Content-Type: text/plain\r\n\r\n"
    tree = parse_message(msg)
    assert tree.text == ""


def test_empty_base64_body():
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
    )
    tree = parse_message(msg)
    assert tree.blob == b""


def test_repeated_non_structural_headers_kept_in_order():
    msg = b"Received: a\r\nReceived: b\r\n\r\nbody\r\n"
    tree = parse_message(msg)
    received = [h.decoded for h in tree.headers if h.name == "received"]
    assert received == ["a", "b"]


def test_canonical_json_is_stable_and_sorted():
    tree = parse_message(b"Zebra: z\r\nAlpha: a\r\n\r\nb\r\n")
    c1 = canonical_json(canonical_tree(tree))
    c2 = canonical_json(canonical_tree(tree))
    assert c1 == c2
    # sort_keys=True：content_type 出现在 headers 前
    assert c1.index(b"content_type") < c1.index(b"headers")


def test_node_paths_for_three_parts():
    body = make_multipart(
        [text_part("1"), text_part("2"), text_part("3")], "B"
    )
    tree = parse_message(
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n" + body
    )
    assert [c.path for c in tree.children] == ["1", "2", "3"]


def test_deeply_nested_paths():
    # 三层嵌套 multipart
    inner = make_multipart([text_part("deep")], "I")
    middle = (
        b"Content-Type: multipart/alternative; boundary=M\r\n\r\n"
        + make_multipart(
            [
                b"Content-Type: multipart/related; boundary=I\r\n\r\n"
                + inner
            ],
            "M",
        )
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=O\r\n\r\n"
        + make_multipart([middle], "O")
    )
    tree = parse_message(outer)
    leaf = tree.children[0].children[0].children[0]
    assert leaf.path == "1/1/1"
    assert leaf.text == "deep"


def test_qp_encodes_high_bytes_for_latin1():
    msg = (
        b"Content-Type: text/plain; charset=iso-8859-1\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"caf=E9"
    )
    tree = parse_message(msg)
    assert tree.text == "café"


def test_8bit_identity():
    msg = (
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\nhi \xe2\x98\x83"
    )
    tree = parse_message(msg)
    assert tree.text == "hi ☃"


def test_header_literal_latin1_fallback_not_rejected():
    # 老式非 encoded-word 的 latin-1 字面头不应导致拒收
    msg = b"Subject: caf\xe9\r\n\r\nx\r\n"
    tree = parse_message(msg)
    subj = next(h for h in tree.headers if h.name == "subject")
    assert "caf" in subj.decoded  # latin-1 回退解码


def test_multipart_root_missing_boundary_param_nested():
    inner_msg = (
        b"Content-Type: multipart/mixed\r\n\r\nno boundary declared"
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=O\r\n\r\n"
        + make_multipart([inner_msg], "O")
    )
    with pytest.raises(MIMEParseError) as exc:
        parse_message(outer)
    assert exc.value.node_path == "1"
    assert exc.value.code == "INVALID_BOUNDARY"
