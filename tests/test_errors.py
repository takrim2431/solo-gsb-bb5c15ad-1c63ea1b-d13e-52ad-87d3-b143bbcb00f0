"""损坏输入处理：错误必须定位到节点且不产生半写入。"""

import base64

import pytest

from conftest import make_multipart, text_part

from app.errors import MIMEParseError
from app.mimeparser import parse_message


def expect_error(data: bytes) -> MIMEParseError:
    with pytest.raises(MIMEParseError) as excinfo:
        parse_message(data)
    return excinfo.value


# ---------------------------------------------------------------------------
# 头 / 报文级损坏
# ---------------------------------------------------------------------------


def test_empty_input_errors_at_root():
    err = expect_error(b"")
    assert err.node_path == ""
    assert err.code == "INVALID_MESSAGE"


def test_bad_header_name_errors_at_root():
    err = expect_error(b"Bad Header Name: x\r\n\r\nbody\r\n")
    assert err.code == "INVALID_HEADER_NAME"
    assert err.node_path == ""


def test_missing_colon_errors():
    err = expect_error(b"Subject no colon\r\n\r\nbody\r\n")
    assert err.code == "INVALID_HEADER"


def test_folded_first_line_errors():
    err = expect_error(b" folded value\r\nSubject: x\r\n\r\nb\r\n")
    assert err.code == "INVALID_HEADER"


def test_duplicate_structural_header_errors():
    err = expect_error(
        b"Content-Type: text/plain\r\nContent-Type: text/html\r\n\r\nx\r\n"
    )
    assert err.code == "DUPLICATE_HEADER"


# ---------------------------------------------------------------------------
# 边界损坏：节点路径必须指向具体 multipart 节点
# ---------------------------------------------------------------------------


def test_missing_boundary_parameter():
    err = expect_error(b"Content-Type: multipart/mixed\r\n\r\nx\r\n")
    assert err.code == "INVALID_BOUNDARY"
    assert err.node_path == ""


def test_boundary_not_found_in_body():
    data = (
        b"Content-Type: multipart/mixed; boundary=XYZ\r\n\r\n"
        b"there is no boundary here at all\r\n"
    )
    err = expect_error(data)
    assert err.code == "INVALID_BOUNDARY"
    assert "XYZ" in err.message


def test_missing_closing_boundary():
    data = (
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nonly part, no close\r\n"
    )
    err = expect_error(data)
    assert err.code == "INVALID_BOUNDARY"
    assert err.node_path == ""


def test_nested_broken_boundary_points_to_nested_node():
    # 内层 multipart 声明 boundary=IN，但正文中根本没有 IN 边界
    inner = b"this inner body has no delimiter at all"
    inner_part = (
        b"Content-Type: multipart/alternative; boundary=IN\r\n\r\n" + inner
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=OUT\r\n\r\n"
        + make_multipart([text_part("ok"), inner_part], "OUT")
    )
    err = expect_error(outer)
    assert err.code == "INVALID_BOUNDARY"
    assert err.node_path == "2"


def test_boundary_too_long():
    long_b = "x" * 71
    err = expect_error(
        f"Content-Type: multipart/mixed; boundary={long_b}\r\n\r\n".encode()
    )
    assert err.code == "INVALID_BOUNDARY"


def test_boundary_bad_chars():
    err = expect_error(
        b"Content-Type: multipart/mixed; boundary=\"a b\x01\"\r\n\r\n"
    )
    # 0x01 甚至不是合法 token/quoted-text，先报参数/头值错误或边界错误，
    # 二者都可接受，关键是 422 与节点路径。
    assert err.node_path == ""
    assert err.code in {"INVALID_BOUNDARY", "INVALID_HEADER_VALUE"}


# ---------------------------------------------------------------------------
# 重复参数
# ---------------------------------------------------------------------------


def test_duplicate_simple_parameter():
    err = expect_error(
        b'Content-Type: multipart/mixed; boundary="B"; boundary="C"\r\n\r\n'
    )
    assert err.code == "DUPLICATE_PARAMETER"
    assert err.detail["parameter"] == "boundary"


def test_duplicate_charset_parameter():
    err = expect_error(
        b"Content-Type: text/plain; charset=utf-8; charset=us-ascii\r\n\r\nhi\n"
    )
    assert err.code == "DUPLICATE_PARAMETER"
    assert err.detail["parameter"] == "charset"


def test_duplicate_disposition_filename():
    err = expect_error(
        b'Content-Disposition: attachment; filename="a"; filename="b"\r\n\r\nx'
    )
    assert err.code == "DUPLICATE_PARAMETER"


def test_duplicate_rfc2231_segment():
    err = expect_error(
        b"Content-Disposition: attachment; "
        b"filename*0*=utf-8''a; filename*0*=utf-8''b\r\n\r\nx"
    )
    assert err.code == "DUPLICATE_PARAMETER"


def test_non_contiguous_segments():
    err = expect_error(
        b"Content-Disposition: attachment; "
        b"filename*0*=utf-8''a; filename*2*=utf-8''c\r\n\r\nx"
    )
    assert err.code == "INVALID_PARAMETER"


def test_mixed_plain_and_extended_parameter():
    err = expect_error(
        b"Content-Disposition: attachment; "
        b"filename=a; filename*0*=utf-8''b\r\n\r\nx"
    )
    assert err.code == "DUPLICATE_PARAMETER"


# ---------------------------------------------------------------------------
# 字符集 / 传输编码无法解码
# ---------------------------------------------------------------------------


def test_unknown_charset_errors():
    err = expect_error(
        b"Content-Type: text/plain; charset=x-no-such-charset-xyz\r\n\r\nhi\r\n"
    )
    assert err.code == "UNKNOWN_CHARSET"
    assert err.node_path == ""


def test_invalid_utf8_bytes_error_at_leaf_node():
    # 子部分（节点 1）声明 UTF-8 但给出非法序列
    bad_part = (
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: binary\r\n\r\n"
        b"\xff\xfe\xfd garbage"
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        + make_multipart([bad_part], "B")
    )
    err = expect_error(outer)
    assert err.code == "UNDECODABLE_CONTENT"
    assert err.node_path == "1"
    assert err.detail["charset"] == "utf-8"
    assert "offset" in err.detail


def test_bad_base64_illegal_char_at_node():
    part = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"!!!not base64!!!\r\n"
    )
    outer = (
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        + make_multipart([text_part("ok"), part], "B")
    )
    err = expect_error(outer)
    assert err.code == "UNDECODABLE_CONTENT"
    assert err.node_path == "2"
    assert "offset" in err.detail


def test_bad_base64_length():
    err = expect_error(
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"abc\r\n"
    )
    assert err.code == "UNDECODABLE_CONTENT"


def test_bad_base64_padding():
    err = expect_error(
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"ab=d\r\n"
    )
    assert err.code == "UNDECODABLE_CONTENT"


def test_qp_bad_escape():
    err = expect_error(
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"hello =ZZ world\r\n"
    )
    assert err.code == "UNDECODABLE_CONTENT"


def test_7bit_with_high_byte():
    err = expect_error(
        b"Content-Transfer-Encoding: 7bit\r\n\r\nhi \xff there\r\n"
    )
    assert err.code == "UNDECODABLE_CONTENT"
    assert err.detail["byte"] == 0xFF


def test_7bit_with_bare_lf_is_tolerated():
    # 归档解析对换行风格宽容（叶子阶段统一换行），裸 LF 不报错；
    # 7bit 真正的不变量“仅 US-ASCII”仍由 test_7bit_with_high_byte 保证。
    tree = parse_message(
        b"Content-Transfer-Encoding: 7bit\r\n\r\nhi\nthere\r\n"
    )
    assert tree.text == "hi\nthere\n"


def test_unknown_cte():
    err = expect_error(
        b"Content-Transfer-Encoding: x-weird-unknown\r\n\r\nhi\r\n"
    )
    # x- token 是 RFC 允许的扩展形式，但我们无法解码 -> UNSUPPORTED_CTE
    assert err.code in {"UNSUPPORTED_CTE", "INVALID_CTE"}


def test_encoded_word_bad_base64():
    err = expect_error(
        b"Subject: =?utf-8?B?@@@?=\r\n\r\nbody\r\n"
    )
    assert err.code == "UNDECODABLE_HEADER"
    assert err.node_path == ""


def test_encoded_word_unknown_charset():
    err = expect_error(
        b"Subject: =?x-bogus?B?aGVsbG8=?=\r\n\r\nbody\r\n"
    )
    assert err.code in {"UNKNOWN_CHARSET", "UNDECODABLE_HEADER"}


# ---------------------------------------------------------------------------
# 媒体类型 / disposition
# ---------------------------------------------------------------------------


def test_invalid_content_type():
    err = expect_error(b"Content-Type: not-a-type\r\n\r\nx\r\n")
    assert err.code == "INVALID_CONTENT_TYPE"


def test_encoded_word_forbidden_in_content_type_param():
    err = expect_error(
        b'Content-Type: text/plain; name="=?utf-8?B?aGVsbG8=?="\r\n\r\nx\r\n'
    )
    assert err.code == "INVALID_HEADER_VALUE"


# ---------------------------------------------------------------------------
# 错误对象序列化
# ---------------------------------------------------------------------------


def test_error_to_dict_shape():
    err = expect_error(b"")
    d = err.to_dict()
    assert set(d["error"]) == {"code", "message", "node_path", "detail"}
    assert d["error"]["node_path"] == ""


# ---------------------------------------------------------------------------
# 合法但边缘的情况应当通过
# ---------------------------------------------------------------------------


def test_valid_base64_with_whitespace_and_padding():
    data = bytes(range(256))
    encoded = base64.encodebytes(data)  # 自带换行
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + encoded
    )
    tree = parse_message(msg)
    assert tree.blob == data


def test_qp_soft_line_break_decodes():
    msg = (
        b"Content-Type: text/plain; charset=us-ascii\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"abc=\r\ndef\r\n"
    )
    tree = parse_message(msg)
    assert tree.text == "abcdef\n"


def test_rfc2231_encoded_filename_accepted():
    msg = (
        b'Content-Type: application/octet-stream; name="t.bin"\r\n'
        b"Content-Disposition: attachment;\r\n"
        b" filename*0*=utf-8''%E4%BD%A0;\r\n"
        b" filename*1*=%E5%A5%BD.bin\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"AAEC\r\n"
    )
    tree = parse_message(msg)
    assert tree.disposition_params["filename"] == "你好.bin"


def test_multipart_with_preamble_and_epilogue():
    body = make_multipart(
        [text_part("real content")],
        "B",
        preamble=b"This is a preamble, ignore me",
        epilogue=b"epilogue here",
    )
    msg = b"Content-Type: multipart/mixed; boundary=B\r\n\r\n" + body
    tree = parse_message(msg)
    assert tree.children[0].text == "real content"
