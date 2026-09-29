"""指纹稳定性与规范化测试。"""

import base64

from conftest import make_multipart, text_part

from app.mimeparser import parse_message, fingerprint


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


def test_simple_plaintext_email():
    msg = (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Hello\r\n"
        b"\r\n"
        b"Hi there\r\n"
    )
    tree = parse_message(msg)
    assert tree.media_type == "text/plain"
    assert tree.path == ""
    assert tree.is_leaf
    assert tree.text == "Hi there\n"
    fp, _ = fingerprint(tree)
    assert len(fp) == 64
    assert fp == fp.lower()


def test_default_content_type_is_text_plain_usascii():
    msg = b"Subject: no body content type\r\n\r\nplain body\n"
    tree = parse_message(msg)
    assert tree.media_type == "text/plain"
    assert tree.text == "plain body\n"


# ---------------------------------------------------------------------------
# 指纹稳定性：换行 / 头大小写 / 折叠 / 空白
# ---------------------------------------------------------------------------


def test_crlf_vs_lf_same_fingerprint():
    crlf = (
        b"From: a@x.com\r\nSubject: S\r\n\r\nBody line\r\n"
    )
    lf = (
        b"From: a@x.com\nSubject: S\n\nBody line\n"
    )
    fp1, _ = fingerprint(parse_message(crlf))
    fp2, _ = fingerprint(parse_message(lf))
    assert fp1 == fp2


def test_bare_cr_body_same_fingerprint():
    crlf_body = b"From: a@x.com\r\n\r\na\r\nb\r\n"
    # 8bit 声明下裸 CR/LF 也应归一
    cr_body = b"From: a@x.com\r\nContent-Transfer-Encoding: 8bit\r\n\r\na\rb\r"
    fp1, _ = fingerprint(parse_message(crlf_body))
    fp2, _ = fingerprint(parse_message(cr_body))
    assert fp1 == fp2


def test_header_name_case_same_fingerprint():
    upper = b"FROM: a@x.com\r\nSUBJECT: S\r\n\r\nb\r\n"
    lower = b"from: a@x.com\r\nsubject: S\r\n\r\nb\r\n"
    mixed = b"From: a@x.com\r\nSuBjEcT: S\r\n\r\nb\r\n"
    fps = {fingerprint(parse_message(m))[0] for m in (upper, lower, mixed)}
    assert len(fps) == 1


def test_header_folding_same_fingerprint():
    folded = (
        b"Subject: This is a\r\n long folded\r\n\tsubject\r\n\r\nbody\r\n"
    )
    flat = b"Subject: This is a long folded subject\r\n\r\nbody\r\n"
    fp1, _ = fingerprint(parse_message(folded))
    fp2, _ = fingerprint(parse_message(flat))
    assert fp1 == fp2


def test_multipart_different_boundary_same_fingerprint():
    p = text_part("hello world")
    m1 = (
        b"Content-Type: multipart/mixed; boundary=AAA\r\n\r\n"
        + make_multipart([p], boundary="AAA")
    )
    m2 = (
        b"Content-Type: multipart/mixed; boundary=BBBBBB\r\n\r\n"
        + make_multipart([p], boundary="BBBBBB")
    )
    fp1, _ = fingerprint(parse_message(m1))
    fp2, _ = fingerprint(parse_message(m2))
    assert fp1 == fp2


def test_multipart_part_order_matters():
    p1 = text_part("first")
    p2 = text_part("second")
    base_head = b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
    fp_ab, _ = fingerprint(parse_message(base_head + make_multipart([p1, p2], "B")))
    fp_ba, _ = fingerprint(parse_message(base_head + make_multipart([p2, p1], "B")))
    assert fp_ab != fp_ba


def test_cte_does_not_affect_fingerprint():
    body = "café ☃".encode("utf-8")
    # 同一段 UTF-8 内容分别用 8bit / base64 / quoted-printable 传输
    raw = (
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n" + body
    )
    b64 = (
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(body) + b"\r\n"
    )
    # quoted-printable 形式必须与上面是同一段字节
    qp = (
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        + b"caf=C3=A9 =E2=98=83"
    )
    fp1, _ = fingerprint(parse_message(raw))
    fp2, _ = fingerprint(parse_message(b64))
    fp3, _ = fingerprint(parse_message(qp))
    assert fp1 == fp2 == fp3


def test_charset_label_difference_same_decoded_text_same_fingerprint():
    # 纯 ASCII 文本无论声明 us-ascii 还是 utf-8，指纹相同
    ascii_part = b"Content-Type: text/plain; charset=us-ascii\r\n\r\nhello\n"
    utf8_part = b"Content-Type: text/plain; charset=utf-8\r\n\r\nhello\n"
    fp1, _ = fingerprint(parse_message(ascii_part))
    fp2, _ = fingerprint(parse_message(utf8_part))
    assert fp1 == fp2


def test_rfc2047_encoded_subject_equals_decoded_subject():
    # 编码字解码后即 “Hello 你好”
    encoded = (
        b"Subject: =?utf-8?B?SGVsbG8g5L2g5aW9?=\r\n\r\nbody\r\n"
    )
    decoded = "Subject: Hello 你好".encode("utf-8") + b"\r\n\r\nbody\r\n"
    fp1, _ = fingerprint(parse_message(encoded))
    fp2, _ = fingerprint(parse_message(decoded))
    assert fp1 == fp2


def test_adjacent_encoded_words_whitespace_removed():
    # RFC 2047 §6.2: =?..?= 和 =?..?= 之间的空白应删除。
    # 注意 token 本身不含会解码成空格的 '_'。
    msg = b"Subject: =?us-ascii?Q?a?= \t =?us-ascii?Q?b?=\r\n\r\nx\r\n"
    tree = parse_message(msg)
    subj = next(h for h in tree.headers if h.name == "subject")
    assert subj.value == b"=?us-ascii?Q?a?= \t =?us-ascii?Q?b?="
    from app.mimeparser import decode_header_value

    assert decode_header_value(subj.value, "", "subject") == "ab"


def test_encoded_word_internal_underscore_decodes_to_space():
    # '_' 是 Q 编码内部的空格；相邻编码字间空白仍删除，故为 "a b"
    msg = b"Subject: =?us-ascii?Q?a_b?= =?us-ascii?Q?c?=\r\n\r\nx\r\n"
    from app.mimeparser import decode_header_value

    val = next(h for h in parse_message(msg).headers
               if h.name == "subject").value
    assert decode_header_value(val, "", "subject") == "a bc"


def test_nfc_normalization():
    # é 单一码点 U+00E9 (NFC) vs U+0065 + U+0301 组合序列 (NFD)
    nfc = "\u00e9".encode("utf-8")          # é
    nfd = "e\u0301".encode("utf-8")          # e + combining acute
    assert nfc != nfd
    head = (
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
    )
    a = head + nfc + b"\n"
    b = head + nfd + b"\n"
    assert fingerprint(parse_message(a))[0] == fingerprint(parse_message(b))[0]


# ---------------------------------------------------------------------------
# 递归 multipart / message/rfc822
# ---------------------------------------------------------------------------


def test_nested_multipart_paths_and_content():
    inner = make_multipart([text_part("inner1"), text_part("inner2")], "IN")
    inner_part = (
        b"Content-Type: multipart/alternative; boundary=IN\r\n\r\n" + inner
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=OUT\r\n\r\n"
        + make_multipart([text_part("outer text"), inner_part], "OUT")
    )
    tree = parse_message(outer)
    assert tree.media_type == "multipart/mixed"
    assert [c.path for c in tree.children] == ["1", "2"]
    nested = tree.children[1]
    assert nested.media_type == "multipart/alternative"
    assert [c.path for c in nested.children] == ["2/1", "2/2"]
    assert nested.children[0].text == "inner1"
    assert nested.children[1].text == "inner2"


def test_message_rfc822_recursive(tmp_path=False):
    inner_msg = (
        b"From: nested@x.com\r\nSubject: Inner\r\n\r\nnested body\r\n"
    )
    wrapped = (
        b"Content-Type: multipart/mixed; boundary=W\r\n\r\n"
        + make_multipart(
            [
                text_part("wrapper"),
                b"Content-Type: message/rfc822\r\n\r\n" + inner_msg,
            ],
            "W",
        )
    )
    tree = parse_message(wrapped)
    rfc = tree.children[1]
    assert rfc.media_type == "message/rfc822"
    assert len(rfc.children) == 1
    inner = rfc.children[0]
    assert inner.path == "2/1"
    assert inner.headers[0].name == "from"
    assert inner.text == "nested body\n"
