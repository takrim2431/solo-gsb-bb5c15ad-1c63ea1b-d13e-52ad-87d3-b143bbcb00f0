"""整邮件附件 ZIP 归档测试。

覆盖：文档序逐 blob 出现打包（同哈希不合并）、归档名派生（basename、
控制字符、路径分隔符、空名回退）、NFC+casefold 冲突改名、内置
manifest.json 内容与序列化、字节确定性与强 ETag / If-None-Match、
无附件空 ZIP、404 与内置 manifest 名占用。
"""

import base64
import hashlib
import io
import json
import zipfile

from conftest import make_multipart, text_part
from test_attachments import bin_part, message_with_parts, PNG, PDF


def post(client, data: bytes) -> dict:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()


def get_zip(client, mid: int, **headers):
    return client.get(f"/messages/{mid}/attachments.zip", **headers)


def open_zip(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None  # 完整性校验
    return zf


# ---------------------------------------------------------------------------
# 基本打包
# ---------------------------------------------------------------------------


def test_zip_writes_each_blob_in_document_order(client):
    mid = post(client, message_with_parts(
        text_part("body"),
        bin_part(PNG, ctype="image/png", filename="a.png"),
        bin_part(PDF, filename="b.pdf"),
    ))["id"]

    r = get_zip(client, mid)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]

    zf = open_zip(r.content)
    # manifest 在前，随后按文档序；文本叶子不出现。
    assert zf.namelist() == ["manifest.json", "a.png", "b.pdf"]
    assert zf.read("a.png") == PNG
    assert zf.read("b.pdf") == PDF

    manifest = json.loads(zf.read("manifest.json"))
    assert [m["node_path"] for m in manifest] == ["2", "3"]
    assert manifest[0] == {
        "node_path": "2",
        "original_filename": "a.png",
        "archive_name": "a.png",
        "media_type": "image/png",
        "sha256": hashlib.sha256(PNG).hexdigest(),
        "size": len(PNG),
    }
    assert manifest[1]["original_filename"] == "b.pdf"
    assert manifest[1]["media_type"] == "application/octet-stream"
    assert manifest[1]["size"] == len(PDF)


def test_same_hash_twice_is_written_twice_not_merged(client):
    mid = post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="x.png"),
        bin_part(PNG, ctype="image/png", filename="y.png"),
    ))["id"]

    r = get_zip(client, mid)
    zf = open_zip(r.content)
    assert zf.namelist() == ["manifest.json", "x.png", "y.png"]
    assert zf.read("x.png") == zf.read("y.png") == PNG

    manifest = json.loads(zf.read("manifest.json"))
    digests = [m["sha256"] for m in manifest]
    assert digests == [hashlib.sha256(PNG).hexdigest()] * 2
    assert [m["node_path"] for m in manifest] == ["1", "2"]


def test_blob_inside_encapsulated_message_included(client):
    inner = (
        b"Content-Type: multipart/mixed; boundary=IN\r\n\r\n"
        + make_multipart(
            [text_part("inner"), bin_part(PDF, filename="deep.pdf")], "IN"
        )
    )
    outer = b"Content-Type: message/rfc822\r\n\r\n" + inner
    mid = post(client, outer)["id"]

    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "deep.pdf"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest[0]["node_path"] == "1/2"


def test_root_blob_fallback_name(client):
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(PNG)
    )
    mid = post(client, msg)["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "attachment-"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest[0]["original_filename"] is None
    assert manifest[0]["archive_name"] == "attachment-"
    assert manifest[0]["node_path"] == ""


def test_nested_node_fallback_uses_path(client):
    # 有 filename* 也能解析；这里构造无文件名的嵌套附件。
    inner = (
        b"Content-Type: multipart/mixed; boundary=IN\r\n\r\n"
        + make_multipart(
            [bin_part(PDF, disposition=None)], "IN"
        )
    )
    outer = b"Content-Type: message/rfc822\r\n\r\n" + inner
    mid = post(client, outer)["id"]
    zf = open_zip(get_zip(client, mid).content)
    # 回退名 attachment-{路径} 中的路径分隔符同样替换，避免子目录。
    assert zf.namelist() == ["manifest.json", "attachment-1_1"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest[0]["archive_name"] == "attachment-1_1"


# ---------------------------------------------------------------------------
# 归档名派生
# ---------------------------------------------------------------------------


def test_basename_is_taken_from_decoded_filename(client):
    mid = post(client, message_with_parts(
        bin_part(PDF, filename="docs/report.pdf"),
        bin_part(PNG, ctype="image/png", filename="images/pic.PNG"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "report.pdf", "pic.PNG"]


def test_rfc2231_decoded_filename(client):
    part = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment;\r\n"
        b" filename*=utf-8''caf%C3%A9.txt\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(PDF)
    )
    mid = post(client, message_with_parts(part))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "café.txt"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest[0]["original_filename"] == "café.txt"
    assert manifest[0]["archive_name"] == "café.txt"


def test_control_chars_removed_and_slashes_replaced(client):
    # 解码后文件名含 TAB(0x09)、LF(0x0A)、DEL(0x7f) 与两种路径分隔符；
    # 用 RFC 2231 filename* 传入（裸控制字符无法出现在合法头中）。
    import urllib.parse

    # basename 为 "a\tb\nc\x7fd.bin"（控制字符在末段内），目录段被剥离。
    decoded = "dir\tsub/a\tb\nc\x7fd.bin"
    encoded = urllib.parse.quote(decoded, safe="")
    part = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment;\r\n"
        + f" filename*=utf-8''{encoded}\r\n".encode()
        + b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(PDF)
    )
    mid = post(client, message_with_parts(part))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "abcd.bin"]

    # basename 内的反斜杠同样按目录分隔符处理（先取末段，再清洗）。
    decoded2 = "top\\a\tb\x7fc.bin"
    encoded2 = urllib.parse.quote(decoded2, safe="")
    part2 = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment;\r\n"
        + f" filename*=utf-8''{encoded2}\r\n".encode()
        + b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(PDF)
    )
    mid2 = post(client, message_with_parts(part2))["id"]
    zf2 = open_zip(get_zip(client, mid2).content)
    assert zf2.namelist() == ["manifest.json", "abc.bin"]


def test_only_control_chars_falls_back(client):
    part = bin_part(PDF, filename="\t\x7f")
    mid = post(client, message_with_parts(part))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "attachment-1"]
    manifest = json.loads(zf.read("manifest.json"))
    # original_filename 原样保留，archive_name 是回退名。
    assert manifest[0]["original_filename"] == "\t\x7f"
    assert manifest[0]["archive_name"] == "attachment-1"


def test_no_filename_nested_part_fallback(client):
    mid = post(client, message_with_parts(bin_part(PDF, disposition=None)))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "attachment-1"]


# ---------------------------------------------------------------------------
# 冲突改名
# ---------------------------------------------------------------------------


def test_casefold_collision_gets_suffix(client):
    mid = post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"),
        bin_part(PNG, ctype="image/png", filename="A.PNG"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "a.png", "A (2).PNG"]
    manifest = json.loads(zf.read("manifest.json"))
    assert [m["archive_name"] for m in manifest] == ["a.png", "A (2).PNG"]
    # 条目内容仍分别写入（同哈希不合并）。
    assert zf.read("a.png") == zf.read("A (2).PNG") == PNG


def test_collision_counter_inserts_before_extension(client):
    # os.path.splitext 口径：扩展名是最后一个点段（"archive.tar.gz" →
    # stem "archive.tar"，ext ".gz"）。
    mid = post(client, message_with_parts(
        bin_part(b"0", filename="archive.tar.gz"),
        bin_part(b"1", filename="ARCHIVE.tar.gz"),
        bin_part(b"2", filename="archive.TAR.gz"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == [
        "manifest.json",
        "archive.tar.gz",
        "ARCHIVE.tar (2).gz",
        "archive.TAR (3).gz",
    ]


def test_collision_without_extension(client):
    mid = post(client, message_with_parts(
        bin_part(b"0", filename="README"),
        bin_part(b"1", filename="readme"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", "README", "readme (2)"]


def test_dotfile_has_no_extension_suffix(client):
    mid = post(client, message_with_parts(
        bin_part(b"0", filename=".bashrc"),
        bin_part(b"1", filename=".BASHRC"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json", ".bashrc", ".BASHRC (2)"]


def test_generated_suffix_collision_advances_counter(client):
    # "a (2).bin" 已存在时，与 "a.bin" 冲突的第二个占用 (3)。
    mid = post(client, message_with_parts(
        bin_part(b"0", filename="a.bin"),
        bin_part(b"1", filename="a (2).bin"),
        bin_part(b"2", filename="A.bin"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == [
        "manifest.json",
        "a.bin",
        "a (2).bin",
        "A (3).bin",
    ]


def test_builtin_manifest_name_is_reserved(client):
    mid = post(client, message_with_parts(
        bin_part(PDF, filename="manifest.json"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    # 附件不覆盖内置清单；内置 manifest.json 仍是清单本体。
    assert zf.namelist() == ["manifest.json", "manifest (2).json"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest[0]["archive_name"] == "manifest (2).json"
    assert zf.read("manifest (2).json") == PDF


# ---------------------------------------------------------------------------
# 确定性与 ETag
# ---------------------------------------------------------------------------


def test_repeated_downloads_are_byte_identical_with_strong_etag(client):
    mid = post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"),
        bin_part(PDF, filename="b.pdf"),
    ))["id"]

    first = get_zip(client, mid)
    second = get_zip(client, mid)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    etag = first.headers["etag"]
    # 强 ETag：引号包裹，无 W/ 前缀；值为归档字节 SHA-256。
    assert etag.startswith('"') and etag.endswith('"')
    assert not etag.startswith('"W/')
    digest = etag.strip('"')
    assert digest == hashlib.sha256(first.content).hexdigest()
    assert second.headers["etag"] == etag


def test_fixed_entry_metadata(client):
    mid = post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    for info in zf.infolist():
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert info.create_system == 3  # Unix
        assert info.external_attr >> 16 == 0o644
        assert info.compress_type == zipfile.ZIP_DEFLATED


def test_manifest_json_is_deterministically_serialized(client):
    mid = post(client, message_with_parts(
        bin_part(PDF, filename="z.bin"),
        bin_part(PNG, ctype="image/png", filename="a.png"),
    ))["id"]
    zf = open_zip(get_zip(client, mid).content)
    raw = zf.read("manifest.json")
    # 键排序、无多余空白、无尾换行。
    assert raw.endswith(b"]}") or raw.endswith(b"]")
    assert not raw.endswith(b"\n")
    assert b": " not in raw and b", " not in raw
    # 紧凑重排后必须与原字节一致。
    assert json.dumps(
        json.loads(raw), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode() == raw


def test_if_none_match_returns_304(client):
    mid = post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"),
    ))["id"]
    etag = get_zip(client, mid).headers["etag"]

    r = get_zip(client, mid, headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == etag

    # 多值列表中命中也返回 304；* 对存在资源恒命中。
    assert get_zip(
        client, mid, headers={"If-None-Match": f'"{"0" * 64}", {etag}'}
    ).status_code == 304
    assert get_zip(client, mid, headers={"If-None-Match": "*"}).status_code == 304

    # 不命中返回完整内容。
    r2 = get_zip(client, mid, headers={"If-None-Match": f'"{"0" * 64}"'})
    assert r2.status_code == 200
    assert len(r2.content) > 0


def test_different_messages_have_different_archives(client):
    m1 = post(client, message_with_parts(
        bin_part(PNG, ctype="image/png", filename="a.png"), subject="s1"
    ))["id"]
    m2 = post(client, message_with_parts(
        bin_part(PDF, filename="a.png"), subject="s2"
    ))["id"]
    assert get_zip(client, m1).content != get_zip(client, m2).content


# ---------------------------------------------------------------------------
# 无附件 / 404
# ---------------------------------------------------------------------------


def test_no_attachments_returns_valid_zip_with_empty_manifest(client):
    mid = post(client, b"Subject: plain\r\n\r\njust text body\r\n")["id"]
    r = get_zip(client, mid)
    assert r.status_code == 200
    zf = open_zip(r.content)
    assert zf.namelist() == ["manifest.json"]
    assert zf.read("manifest.json") == b"[]"

    # 同样确定、带 ETag，If-None-Match 返回 304。
    assert get_zip(client, mid).content == r.content
    assert get_zip(
        client, mid, headers={"If-None-Match": r.headers["etag"]}
    ).status_code == 304


def test_multipart_text_only_message_empty_zip(client):
    msg = (
        b"Content-Type: multipart/mixed; boundary=M\r\n\r\n"
        + make_multipart([text_part("a"), text_part("b")], "M")
    )
    mid = post(client, msg)["id"]
    zf = open_zip(get_zip(client, mid).content)
    assert zf.namelist() == ["manifest.json"]
    assert zf.read("manifest.json") == b"[]"


def test_message_not_found_404(client):
    r = get_zip(client, 999)
    assert r.status_code == 404
