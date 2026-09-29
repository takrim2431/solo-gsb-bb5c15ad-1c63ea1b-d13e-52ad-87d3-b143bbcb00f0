"""严格的 MIME / RFC 5322 解析与稳定指纹生成。

不使用 Python 标准库 ``email`` 包：标准库对损坏输入过于宽容
（自动修复坏边界、吞掉无法解码的字节、重复参数“最后一个生效”），
而归档场景要求 *任何歧义都显式报错、不写半成品*。

解析流程：

1. 按 RFC 5322 把报文切成头块与正文（头名必须合法）；
2. 解析 ``Content-Type`` / ``Content-Disposition`` 及其
   RFC 2045/2231 参数（重复参数即错误）；
3. ``multipart`` 按 RFC 2046 边界扫描正文，缺失结束边界、
   边界字符集非法等都报错；
4. ``message/rfc822`` / ``message/global`` 递归解析；
5. 叶子节点按 CTE 解码（base64 / quoted-printable / 7bit …），
   再按声明字符集解码并 NFC 规范化、统一换行；
6. 用规范化头字段、结构信息与叶子内容生成 JSON 规范形，
   SHA-256 得到稳定指纹。

换行（CRLF/LF/CR）与头字段大小写不影响指纹。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from codecs import lookup
from dataclasses import dataclass, field
from typing import Optional

from .errors import MIMEParseError

# ---------------------------------------------------------------------------
# 词法常量（RFC 5234 / RFC 2045 / RFC 2046）
# ---------------------------------------------------------------------------

# RFC 5322 ftext: 可打印 ASCII，去掉 ':'
_FTEXT = set(range(0x21, 0x7F)) - {0x3A}
# RFC 2045 token: 可打印 ASCII，去掉分隔符
_TSPECIALS = set(b'()<>@,;:\\"/[]?=')
_TOKEN_BYTES = set(range(0x21, 0x7F)) - _TSPECIALS
_TOKEN_STR = {chr(c) for c in _TOKEN_BYTES}
# 参数名位置：token 去掉 '*'，使 RFC 2231 的 name*0* 标记能被识别
_PARAM_NAME_BYTES = _TOKEN_BYTES - {ord("*")}
# RFC 2046 bchars: 可出现在 boundary 中的字节（含空格）
_BCHARS = (
    set(range(ord("a"), ord("z") + 1))
    | set(range(ord("A"), ord("Z") + 1))
    | set(range(ord("0"), ord("9") + 1))
    | set(b"'()+_,-./:=?")
    | {ord(" ")}
)
_WHITESPACE = (ord(" "), ord("\t"))

# RFC 2047 encoded-word: =?charset?encoding?encoded-text?=
# charset 为 RFC 2278 mime-charset。encoded-text 为空白 + 可打印 ASCII
# （兼容真实实现中未编码的 '?'；Q 解码阶段再对 '?' 做严格校验）。
# 字符类直接由 ASCII 码点集合构造，避免手写范围时的 '-' 歧义。
_ETEXT = re.escape(bytes({9, 32} | set(range(33, 127))))
_ENCODED_WORD_RE = re.compile(
    rb"=\?([-!#$%&'*+./0-9A-Z^_`a-z{|}~]+)"
    rb"\?([bBqQ])\?([" + _ETEXT + rb"]+?)\?="
)
_HEX_RE = re.compile(rb"^[0-9A-Fa-f]{2}$")
_STRUCTURAL_HEADERS = {
    "content-type",
    "content-transfer-encoding",
    "content-disposition",
}
_KNOWN_CTE = {"7bit", "8bit", "binary", "quoted-printable", "base64"}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class RawHeader:
    """解析后、解码语义值前的原始头字段。"""

    name: str  # 已小写
    raw_name: bytes  # 原始字节（保留原始大小写供存储/展示）
    value: bytes  # 已去折叠、去掉换行的原始值
    decoded: str = ""  # 节点构建时填充：RFC 2047 解码 + NFC 后的值


@dataclass
class ContentType:
    media_type: str  # 已小写，如 "text/plain"
    params: dict[str, str]  # 解码后的参数（不含重复）
    boundary: Optional[str] = None
    charset: Optional[str] = None
    name: Optional[str] = None


@dataclass
class MIMENode:
    """MIME 树节点。"""

    path: str  # ""（根）/ "1" / "1/2"，1 基
    media_type: str
    type: str
    subtype: str
    content_type_params: dict[str, str]
    disposition: Optional[str] = None
    disposition_params: dict[str, str] = field(default_factory=dict)
    cte: str = "7bit"
    headers: list[RawHeader] = field(default_factory=list)
    children: list["MIMENode"] = field(default_factory=list)
    # 叶子节点：解码并规范化后的文本（text/*），或解码后的原始字节
    text: Optional[str] = None
    blob: Optional[bytes] = None

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def is_text(self) -> bool:
        return self.type == "text"


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------


def parse_message(data: bytes) -> MIMENode:
    """把完整 RFC 5322 邮件解析为 MIME 树。

    任何不合法结构都抛出 :class:`MIMEParseError`，错误节点路径从
    根（``""``）开始标注。
    """

    if not isinstance(data, bytes):
        raise TypeError("邮件内容必须是 bytes")
    return _build_node(data, "", allow_empty=False)


# ---------------------------------------------------------------------------
# 头块切分与字段名校验（RFC 5322 §2.2 / §2.2.3）
# ---------------------------------------------------------------------------


def _split_headers_body(
    data: bytes, path: str, *, allow_empty: bool
) -> tuple[list[RawHeader], bytes]:
    """返回 (头字段列表, 正文字节)。

    RFC 5322 的头块由若干 *行* 构成，每行以 CRLF（这里宽松接受 LF/CR）
    终结；头块之后是空行（第二个行终结符），再之后是正文。因此：

    * ``"H: v"``            —— 最后一个行终结符缺失，正文为空；
    * ``"H: v\\r\\n"``       —— 仅有行终结符，正文同样为空；
    * ``"H: v\\r\\n\\r\\n"`` —— 行终结符 + 空行，正文为空；
    * ``"H: v\\r\\n\\r\\nB"``—— 正文为 ``B``。
    """

    # 先取头块：逻辑上是第一个“空行”之前的内容。
    match = re.search(rb"\r?\n\r?\n", data)
    if match:
        head, body = data[: match.start()], data[match.end():]
    else:
        head, body = data, b""

    headers = _parse_header_block(head, path)
    if not headers and not allow_empty:
        raise MIMEParseError(
            "INVALID_MESSAGE",
            "邮件头为空：至少需要一个头字段",
            path,
        )
    return headers, body


def _parse_header_block(head: bytes, path: str) -> list[RawHeader]:
    if not head:
        return []

    # 仅头块统一换行为 \n（正文保持原始字节不动）。
    text = head.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    raw_lines = text.split(b"\n")

    # RFC 5322 unfolding：以 WSP 开头的物理行折叠到上一逻辑行，
    # 折叠点（CRLF + WSP）按 RFC 替换为一个空格。
    logical: list[bytearray] = []
    for line in raw_lines:
        if not line:
            # 头块内不应出现空物理行（调用方已按空行切分）；忽略之。
            continue
        if line[:1] in (b" ", b"\t"):
            if not logical:
                raise MIMEParseError(
                    "INVALID_HEADER",
                    "邮件块的第一行以空白开头，无法折叠",
                    path,
                    {"line": line.decode("latin-1")[:80]},
                )
            logical[-1].extend(b" ")
            logical[-1].extend(line)
        else:
            logical.append(bytearray(line))

    headers: list[RawHeader] = []
    for idx, raw in enumerate(logical, start=1):
        line = bytes(raw)
        colon = line.find(b":")
        if colon <= 0:
            raise MIMEParseError(
                "INVALID_HEADER",
                f"第 {idx} 个头字段缺少 ':' 或字段名为空",
                path,
                {"line": line.decode("latin-1")[:80]},
            )
        name_bytes = line[:colon]
        if any(c not in _FTEXT for c in name_bytes):
            raise MIMEParseError(
                "INVALID_HEADER_NAME",
                f"头字段名包含非法字节: {name_bytes.decode('latin-1', 'replace')!r}",
                path,
                {"header": name_bytes.decode("latin-1")},
            )
        value = line[colon + 1:].strip(b" \t")
        headers.append(
            RawHeader(
                name=name_bytes.decode("ascii").lower(),
                raw_name=bytes(name_bytes),
                value=value,
            )
        )
    return headers


def _find_header(headers: list[RawHeader], name: str) -> Optional[RawHeader]:
    for h in headers:
        if h.name == name:
            return h
    return None


# ---------------------------------------------------------------------------
# 结构化头参数解析（RFC 2045 / RFC 2231）
# ---------------------------------------------------------------------------


def _parse_parameters(
    value: bytes, path: str, header_name: str
) -> tuple[str, list[tuple[str, Optional[int], bool, bytes]]]:
    """解析 ``主值; p1=v1; p2*0*=...``。

    返回 ``(小写主值, [(参数名, 续片序号或 None, 是否扩展 *, 原始值)])``。
    重复的简单参数、同序号续片、简单/扩展混用都报 ``DUPLICATE_PARAMETER``。
    """

    n = len(value)
    i = 0

    def skip_wsp(pos: int) -> int:
        while pos < n and value[pos] in _WHITESPACE:
            pos += 1
        return pos

    def read_token(pos: int) -> tuple[bytes, int]:
        start = pos
        while pos < n and value[pos] in _PARAM_NAME_BYTES:
            pos += 1
        return value[start:pos], pos

    # 主值读到第一个顶层 ';'
    semi = value.find(b";")
    lead_end = semi if semi >= 0 else n
    lead = value[:lead_end].strip()
    if not lead:
        raise MIMEParseError(
            "INVALID_HEADER_VALUE",
            f"{header_name} 头缺少主值",
            path,
            {"header": value.decode("latin-1", "replace")},
        )
    i = lead_end

    items: list[tuple[str, Optional[int], bool, bytes]] = []
    while i < n:
        i += 1  # 跳过 ';'
        i = skip_wsp(i)
        name_b, i = read_token(i)
        if not name_b:
            raise MIMEParseError(
                "INVALID_HEADER_VALUE",
                f"{header_name} 中存在空参数名",
                path,
                {"offset": i},
            )
        segment: Optional[int] = None
        extended = False
        if i < n and value[i] == ord("*"):
            # RFC 2231 两种形式：
            #   name*=charset'lang'value   —— 整体扩展（无序号）
            #   name*0*=value              —— 扩展续片
            i += 1
            digits = bytearray()
            while i < n and 0x30 <= value[i] <= 0x39:
                digits.append(value[i])
                i += 1
            if digits:
                segment = int(bytes(digits).decode("ascii"))
                # 续片形式必须再跟一个 '*'
                if i < n and value[i] == ord("*"):
                    i += 1
                    extended = True
                else:
                    # name*0= 无尾 '*'：纯续片（非扩展），允许
                    extended = False
            else:
                # name*=：整体扩展
                extended = True
        i = skip_wsp(i)
        if i >= n or value[i] != ord("="):
            raise MIMEParseError(
                "INVALID_HEADER_VALUE",
                f"{header_name} 参数 {name_b.decode('ascii', 'replace')!r} 后缺少 '='",
                path,
                {"parameter": name_b.decode("latin-1")},
            )
        i += 1
        i = skip_wsp(i)
        if i >= n:
            raise MIMEParseError(
                "INVALID_HEADER_VALUE",
                f"{header_name} 参数 {name_b.decode('ascii', 'replace')!r} 缺少值",
                path,
                {"parameter": name_b.decode("latin-1")},
            )
        if value[i] == ord('"') and not extended:
            # quoted-string；头块已去折叠，这里不允许裸换行
            i += 1
            quoted = bytearray()
            closed = False
            while i < n:
                c = value[i]
                if c == ord("\\") and i + 1 < n:
                    quoted.append(value[i + 1])
                    i += 2
                    continue
                if c == ord('"'):
                    closed = True
                    i += 1
                    break
                if c in (ord("\r"), ord("\n")):
                    break
                quoted.append(c)
                i += 1
            if not closed:
                raise MIMEParseError(
                    "INVALID_HEADER_VALUE",
                    f"{header_name} 参数 {name_b.decode('ascii', 'replace')!r} 的引号未闭合",
                    path,
                    {"parameter": name_b.decode("latin-1")},
                )
            raw_val = bytes(quoted)
        elif extended:
            # RFC 2231 扩展值：charset'lang'percent-bytes，
            # 不含引号、分号与空白，读到下一个 ';' 为止。
            start = i
            while i < n and value[i] != ord(";"):
                if value[i] in _WHITESPACE:
                    raise MIMEParseError(
                        "INVALID_PARAMETER",
                        f"{header_name} 扩展参数 "
                        f"{name_b.decode('ascii', 'replace')!r} 的值中不能含空白",
                        path,
                        {"parameter": name_b.decode("latin-1"), "offset": i},
                    )
                i += 1
            raw_val = value[start:i]
            if not raw_val:
                raise MIMEParseError(
                    "INVALID_HEADER_VALUE",
                    f"{header_name} 扩展参数 {name_b.decode('ascii', 'replace')!r} 值为空",
                    path,
                    {"parameter": name_b.decode("latin-1")},
                )
        else:
            start = i
            while i < n and value[i] != ord(";"):
                i += 1
            raw_val = value[start:i].rstrip(b" \t")
            if not raw_val:
                raise MIMEParseError(
                    "INVALID_HEADER_VALUE",
                    f"{header_name} 参数 {name_b.decode('ascii', 'replace')!r} 值为空",
                    path,
                    {"parameter": name_b.decode("latin-1")},
                )
        items.append(
            (name_b.decode("ascii").lower(), segment, extended, raw_val)
        )

    _check_duplicate_parameters(items, header_name, path)
    return lead.decode("ascii").strip().lower(), items


def _check_duplicate_parameters(
    items: list[tuple[str, Optional[int], bool, bytes]],
    header_name: str,
    path: str,
) -> None:
    simple: set[str] = set()
    segments_seen: set[tuple[str, int]] = set()
    extended_names: set[str] = set()
    for name, seg, _ext, _val in items:
        if seg is None:
            if name in simple:
                raise MIMEParseError(
                    "DUPLICATE_PARAMETER",
                    f"{header_name} 参数 {name!r} 重复出现",
                    path,
                    {"parameter": name},
                )
            simple.add(name)
            if name in extended_names:
                raise MIMEParseError(
                    "DUPLICATE_PARAMETER",
                    f"{header_name} 参数 {name!r} 同时以普通与扩展(RFC 2231)形式给出",
                    path,
                    {"parameter": name},
                )
        else:
            if (name, seg) in segments_seen:
                raise MIMEParseError(
                    "DUPLICATE_PARAMETER",
                    f"{header_name} 扩展参数 {name}*{seg}* 重复出现",
                    path,
                    {"parameter": f"{name}*{seg}*"},
                )
            segments_seen.add((name, seg))
            extended_names.add(name)
            if name in simple:
                raise MIMEParseError(
                    "DUPLICATE_PARAMETER",
                    f"{header_name} 参数 {name!r} 同时以普通与扩展(RFC 2231)形式给出",
                    path,
                    {"parameter": name},
                )


def _assemble_parameters(
    items: list[tuple[str, Optional[int], bool, bytes]],
    path: str,
    header_name: str,
) -> dict[str, str]:
    """把 token 流组装成最终参数（处理 RFC 2231 续片与百分号编码）。"""

    grouped: dict[str, list[tuple[Optional[int], bool, bytes]]] = {}
    order: list[str] = []
    for name, seg, ext, raw in items:
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append((seg, ext, raw))

    result: dict[str, str] = {}
    for name in order:
        entries = grouped[name]
        if entries[0][0] is None:
            # 普通单值参数。extended=True 即 RFC 2231 整体扩展形式
            # ``name*=charset'lang'value``（没有续片序号）。
            _, ext, raw = entries[0]
            if ext:
                charset, piece = _split_extended_lead(raw, path, header_name, name)
                result[name] = _percent_decode(piece, charset, path,
                                               header_name, name)
            else:
                result[name] = _decode_plain_param(raw)
            continue

        if any(seg is None for seg, _, _ in entries):
            raise MIMEParseError(
                "INVALID_PARAMETER",
                f"{header_name} 参数 {name!r} 混用了续片与非续片形式",
                path,
                {"parameter": name},
            )
        ordered = sorted(entries, key=lambda e: e[0])
        segs = [seg for seg, _, _ in ordered]
        if segs != list(range(len(segs))):
            raise MIMEParseError(
                "INVALID_PARAMETER",
                f"{header_name} 扩展参数 {name!r} 的续片序号必须从 0 开始连续",
                path,
                {"parameter": name, "segments": segs},
            )

        if not ordered[0][1]:
            # name*0=...; name*1=...：纯续片，拼接后按 UTF-8（失败回退 latin-1）
            joined = b"".join(raw for _, _, raw in ordered)
            result[name] = _decode_plain_param(joined)
            continue

        # RFC 2231 扩展续片：首段 charset'lang'value，其余仅 percent 编码
        charset, piece = _split_extended_lead(ordered[0][2], path,
                                              header_name, name)
        decoded = [_percent_decode(piece, charset, path, header_name, name)]
        for _, _, raw in ordered[1:]:
            decoded.append(_percent_decode(raw, charset, path,
                                           header_name, name))
        result[name] = "".join(decoded)

    return result


def _split_extended_lead(
    raw: bytes, path: str, header_name: str, param: str
) -> tuple[str, bytes]:
    """拆分 RFC 2231 首段 ``charset'language'value``。"""

    first_q = raw.find(b"'")
    if first_q < 0:
        raise MIMEParseError(
            "INVALID_PARAMETER",
            f"{header_name} 扩展参数 {param!r} 缺少 charset 分隔符 \"'\"",
            path,
            {"parameter": param},
        )
    second_q = raw.find(b"'", first_q + 1)
    if second_q < 0:
        raise MIMEParseError(
            "INVALID_PARAMETER",
            f"{header_name} 扩展参数 {param!r} 缺少 language 分隔符 \"'\"",
            path,
            {"parameter": param},
        )
    charset = raw[:first_q].decode("ascii", "replace").strip() or "utf-8"
    return charset, raw[second_q + 1:]


def _percent_decode(
    raw: bytes, charset: str, path: str, header_name: str, param: str
) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == ord("%"):
            if i + 2 >= len(raw) or not _HEX_RE.match(raw[i + 1:i + 3]):
                raise MIMEParseError(
                    "INVALID_PARAMETER",
                    f"{header_name} 扩展参数 {param!r} 的百分号转义损坏",
                    path,
                    {"parameter": param, "offset": i},
                )
            out.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(c)
            i += 1
    return _decode_bytes(bytes(out), charset, path,
                         f"{header_name} 参数 {param!r}")


def _decode_plain_param(raw: bytes) -> str:
    # RFC 2045 普通 token/quoted-string 值：先按 UTF-8，失败回退 latin-1。
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _validate_charset(charset: str, path: str) -> None:
    try:
        lookup(charset)
    except LookupError:
        raise MIMEParseError(
            "UNKNOWN_CHARSET",
            f"无法识别的字符集 {charset!r}",
            path,
            {"charset": charset},
        ) from None


def parse_content_type(value: bytes, path: str) -> ContentType:
    lead, raw_items = _parse_parameters(value, path, "Content-Type")
    parts = lead.split("/")
    if (
        len(parts) != 2
        or not parts[0]
        or not parts[1]
        or any(ch not in _TOKEN_STR for ch in parts[0])
        or any(ch not in _TOKEN_STR for ch in parts[1])
    ):
        raise MIMEParseError(
            "INVALID_CONTENT_TYPE",
            f"非法媒体类型 {lead!r}：应为 type/subtype，且均为合法 token",
            path,
            {"content_type": lead},
        )

    # RFC 2047 §5 明确：Content-Type 参数中不得使用 encoded-word
    if b"=?" in value:
        raise MIMEParseError(
            "INVALID_HEADER_VALUE",
            "Content-Type 参数中不允许 RFC 2047 编码字 (encoded-word)",
            path,
        )

    params = _assemble_parameters(raw_items, path, "Content-Type")
    boundary = params.get("boundary")
    charset = params.get("charset")

    if parts[0] == "multipart":
        if not boundary:
            raise MIMEParseError(
                "INVALID_BOUNDARY",
                f"multipart 媒体类型 {lead!r} 缺少 boundary 参数",
                path,
            )
        _validate_boundary(boundary, path)
    if charset is not None:
        _validate_charset(charset, path)

    return ContentType(
        media_type=lead,
        params=params,
        boundary=boundary,
        charset=charset,
        name=params.get("name"),
    )


def parse_disposition(value: bytes, path: str) -> tuple[str, dict[str, str]]:
    lead, raw_items = _parse_parameters(value, path, "Content-Disposition")
    if any(ch not in _TOKEN_STR for ch in lead):
        raise MIMEParseError(
            "INVALID_DISPOSITION",
            f"非法 disposition {lead!r}：不是合法 token",
            path,
            {"disposition": lead},
        )
    if b"=?" in value:
        raise MIMEParseError(
            "INVALID_HEADER_VALUE",
            "Content-Disposition 参数中不允许 RFC 2047 编码字 (encoded-word)",
            path,
        )
    return lead, _assemble_parameters(raw_items, path, "Content-Disposition")


def _validate_boundary(boundary: str, path: str) -> None:
    # RFC 2046: boundary 1..70 个 bchars 字节（不含前导 --），首尾不得为空格。
    if not boundary.isascii():
        raise MIMEParseError(
            "INVALID_BOUNDARY",
            "boundary 必须只含 ASCII 字符 (RFC 2046 bchars)",
            path,
            {"boundary": boundary},
        )
    b = boundary.encode("ascii")
    if not 1 <= len(b) <= 70:
        raise MIMEParseError(
            "INVALID_BOUNDARY",
            f"boundary 长度为 {len(b)} 字节，超出 RFC 2046 的 1..70 限制",
            path,
            {"length": len(b)},
        )
    if b[:1] == b" " or b[-1:] == b" ":
        raise MIMEParseError(
            "INVALID_BOUNDARY",
            "boundary 首尾不能是空格",
            path,
        )
    if any(c not in _BCHARS for c in b):
        raise MIMEParseError(
            "INVALID_BOUNDARY",
            "boundary 含有非法字节 (RFC 2046 bchars 之外)",
            path,
            {"boundary": boundary},
        )


# ---------------------------------------------------------------------------
# 头值解码：RFC 2047 encoded-word + NFC / 空白规范化
# ---------------------------------------------------------------------------


def _decode_literal(raw: bytes) -> str:
    """encoded-word 之外的字面字节：先 UTF-8 后 latin-1。

    归档要保留真实世界中的“脏”头（如老式 MTA 写出的 latin-1 Subject），
    字面部分不因此拒收；编码字本身仍严格校验。
    """

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _decode_encoded_words(raw: bytes, path: str, header_name: str) -> str:
    matches = list(_ENCODED_WORD_RE.finditer(raw))
    if not matches:
        return _decode_literal(raw)

    # RFC 2047 §6.2：相邻两个 encoded-word 之间若只有空白，则删除该空白。
    drop_spans: list[tuple[int, int]] = []
    for prev, cur in zip(matches, matches[1:]):
        gap = raw[prev.end():cur.start()]
        if gap and not gap.strip():
            drop_spans.append((prev.end(), cur.start()))

    pieces: list[str] = []
    pos = 0
    for m in matches:
        gap = raw[pos:m.start()]
        # 该 gap 若正好是“相邻编码字间的纯空白”，整段丢弃。
        if not any(pos == lo and m.start() == hi
                   for lo, hi in drop_spans):
            pieces.append(_decode_literal(gap))

        charset = m.group(1).decode("ascii")
        encoding = m.group(2)
        token = m.group(3)
        _validate_charset(charset, path)
        try:
            if encoding in (b"b", b"B"):
                payload = _decode_b_token(token, header_name, path)
            else:
                payload = _decode_q_token(token, header_name, path)
        except MIMEParseError:
            raise
        except (binascii.Error, ValueError) as exc:
            raise MIMEParseError(
                "UNDECODABLE_HEADER",
                f"头字段 {header_name!r} 中的编码字无法解码: {exc}",
                path,
                {"header": header_name, "charset": charset},
            ) from None
        pieces.append(
            _decode_bytes(payload, charset, path,
                          f"头字段 {header_name!r} 的编码字")
        )
        pos = m.end()

    pieces.append(_decode_literal(raw[pos:]))
    return "".join(pieces)


def _decode_b_token(token: bytes, header_name: str, path: str) -> bytes:
    core = token
    if len(core) % 4 != 0 or not re.fullmatch(rb"[A-Za-z0-9+/]*={0,2}", core):
        raise MIMEParseError(
            "UNDECODABLE_HEADER",
            f"头字段 {header_name!r} 中 B 编码字的 base64 非法",
            path,
            {"header": header_name},
        )
    try:
        return base64.b64decode(core, validate=True)
    except binascii.Error as exc:
        raise MIMEParseError(
            "UNDECODABLE_HEADER",
            f"头字段 {header_name!r} 中 B 编码字的 base64 非法: {exc}",
            path,
            {"header": header_name},
        ) from None


def _decode_q_token(token: bytes, header_name: str, path: str) -> bytes:
    """RFC 2047 Q 编码（注意：与 quoted-printable 不同，'_' 表示空格，
    '?' 与 '=' 等必须编码）。"""

    out = bytearray()
    i = 0
    while i < len(token):
        c = token[i]
        if c == ord("_"):
            out.append(0x20)
            i += 1
        elif c == ord("="):
            if i + 2 >= len(token) or not _HEX_RE.match(token[i + 1:i + 3]):
                raise MIMEParseError(
                    "UNDECODABLE_HEADER",
                    f"头字段 {header_name!r} 的 Q 编码字损坏：'=' 后不是两位十六进制数",
                    path,
                    {"header": header_name, "offset": i},
                )
            out.append(int(token[i + 1:i + 3], 16))
            i += 3
        elif c == ord("?") or c < 0x20 or c > 0x7E:
            raise MIMEParseError(
                "UNDECODABLE_HEADER",
                f"头字段 {header_name!r} 的 Q 编码字含非法字节 0x{c:02x}",
                path,
                {"header": header_name, "offset": i},
            )
        else:
            out.append(c)
            i += 1
    return bytes(out)


def decode_header_value(raw: bytes, path: str, header_name: str) -> str:
    """解码头值并做 NFC 规范化（折叠已在头解析阶段完成）。"""

    return unicodedata.normalize(
        "NFC", _decode_encoded_words(raw, path, header_name)
    )


def _normalize_header_wsp(text: str) -> str:
    """折叠/续行点的线性空白：连续 WSP 压成一个空格并去两端。"""

    return re.sub(r"[ \t]+", " ", text).strip(" ")


def _decode_bytes(data: bytes, charset: str, path: str, what: str) -> str:
    try:
        return data.decode(charset)
    except (UnicodeDecodeError, LookupError) as exc:
        raise MIMEParseError(
            "UNDECODABLE_CONTENT",
            f"{what} 无法按 {charset!r} 解码: {exc.reason}",
            path,
            {"charset": charset, "offset": getattr(exc, "start", 0)},
        ) from None


# ---------------------------------------------------------------------------
# 节点装配
# ---------------------------------------------------------------------------


def _parse_cte(value: bytes, path: str) -> str:
    text = value.decode("ascii", "replace").strip().lower()
    if not text or any(ch not in _TOKEN_STR for ch in text):
        raise MIMEParseError(
            "INVALID_CTE",
            f"非法 Content-Transfer-Encoding 值: "
            f"{value.decode('latin-1', 'replace')!r}",
            path,
            {"cte": value.decode("latin-1", "replace")},
        )
    if text not in _KNOWN_CTE:
        raise MIMEParseError(
            "UNSUPPORTED_CTE",
            f"不支持的 Content-Transfer-Encoding: {text!r}",
            path,
            {"cte": text},
        )
    return text


def _build_node(data: bytes, path: str, *, allow_empty: bool) -> MIMENode:
    headers, body = _split_headers_body(data, path, allow_empty=allow_empty)

    # 在任何结构校验之前先解码全部头值：编码字非法、字符集未知等错误
    # 必须在解析阶段（而非指纹阶段）就定位到当前节点。
    for h in headers:
        h.decoded = decode_header_value(h.value, path, h.name)

    # 结构头必须唯一（RFC 2045 / RFC 2183）。
    seen: set[str] = set()
    for h in headers:
        if h.name in _STRUCTURAL_HEADERS and h.name in seen:
            raise MIMEParseError(
                "DUPLICATE_HEADER",
                f"结构头字段 {h.raw_name.decode('ascii', 'replace')!r} 只允许出现一次",
                path,
                {"header": h.name},
            )
        seen.add(h.name)

    ct_header = _find_header(headers, "content-type")
    if ct_header is not None:
        ct = parse_content_type(ct_header.value, path)
    else:
        # RFC 2045 §5.2: 缺省 Content-Type: text/plain; charset=us-ascii
        ct = ContentType(
            media_type="text/plain", params={}, charset="us-ascii"
        )

    disposition: Optional[str] = None
    disp_params: dict[str, str] = {}
    disp_header = _find_header(headers, "content-disposition")
    if disp_header is not None:
        disposition, disp_params = parse_disposition(disp_header.value, path)

    cte = "7bit"
    cte_header = _find_header(headers, "content-transfer-encoding")
    if cte_header is not None:
        cte = _parse_cte(cte_header.value, path)

    ptype, psub = ct.media_type.split("/", 1)
    node = MIMENode(
        path=path,
        media_type=ct.media_type,
        type=ptype,
        subtype=psub,
        content_type_params=ct.params,
        disposition=disposition,
        disposition_params=disp_params,
        cte=cte,
        headers=headers,
    )

    if ptype == "multipart":
        node.children = _parse_multipart(body, ct.boundary, path)  # type: ignore[arg-type]
    elif ct.media_type in ("message/rfc822", "message/global"):
        # 容器自身的 CTE 先还原，再递归解析封装报文。
        raw_child = _decode_composite_transfer(body, cte, path)
        child_path = f"{path}/1" if path else "1"
        node.children = [_build_node(raw_child, child_path, allow_empty=False)]
    else:
        _populate_leaf(node, body, ct)

    return node


# ---------------------------------------------------------------------------
# multipart 边界扫描（RFC 2046 §5.1.1）
# ---------------------------------------------------------------------------

_DELIM_TAIL = rb"(--)?[ \t]*(?:\r\n|\n|\r|$)"


def _parse_multipart(body: bytes, boundary: str, path: str) -> list[MIMENode]:
    b = boundary.encode("ascii")
    delim_re = re.compile(
        rb"(?:\r\n|\n|\r|^)--" + re.escape(b) + _DELIM_TAIL
    )
    matches = list(delim_re.finditer(body))
    if not matches:
        raise MIMEParseError(
            "INVALID_BOUNDARY",
            f"multipart 正文中找不到边界 --{boundary}",
            path,
            {"boundary": boundary},
        )

    closing_index: Optional[int] = None
    for idx, m in enumerate(matches):
        if m.group(1) == b"--":
            closing_index = idx
            break
    if closing_index is None:
        raise MIMEParseError(
            "INVALID_BOUNDARY",
            f"multipart 缺少闭合边界 --{boundary}--",
            path,
            {"boundary": boundary},
        )
    # 闭合边界之后是 epilogue，RFC 2046 允许存在，归档忽略。

    part_bodies: list[bytes] = []
    for idx in range(closing_index):
        cur, nxt = matches[idx], matches[idx + 1]
        # 部分正文 = 当前边界行结束 .. 下一边界行起始；
        # 紧接边界前的 CRLF 属于分隔符，而 nxt.start() 正指向它，
        # 所以切片天然不包含该换行。
        part_bodies.append(body[cur.end():nxt.start()])

    if not part_bodies:
        raise MIMEParseError(
            "INVALID_BOUNDARY",
            "multipart 至少要包含一个部分（仅发现闭合边界）",
            path,
            {"boundary": boundary},
        )

    children: list[MIMENode] = []
    for i, part in enumerate(part_bodies, start=1):
        child_path = f"{path}/{i}" if path else str(i)
        # 空子部分（无任何头）按 RFC 2046 允许，缺省 text/plain。
        children.append(_build_node(part, child_path, allow_empty=True))
    return children


# ---------------------------------------------------------------------------
# Content-Transfer-Encoding 解码（RFC 2045 §6）
# ---------------------------------------------------------------------------


def _decode_composite_transfer(body: bytes, cte: str, path: str) -> bytes:
    """容器（message/rfc822）在递归前先还原其 CTE。

    7bit/8bit/binary 直接返回——容器不做 7bit 行检查，因为 RFC 允许
    内部实体自行声明编码，严格行检查会对子部分的二进制字节误报。
    """

    if cte in ("7bit", "8bit", "binary"):
        return body
    if cte == "base64":
        return _decode_base64(body, path)
    if cte == "quoted-printable":
        return _decode_qp(body, path)
    raise MIMEParseError(
        "UNSUPPORTED_CTE", f"不支持的 CTE {cte!r}", path, {"cte": cte}
    )


def _populate_leaf(node: MIMENode, body: bytes, ct: ContentType) -> None:
    decoded = _decode_transfer(body, node.cte, node.path)

    if node.is_text:
        charset = ct.charset or "us-ascii"
        text = _decode_bytes(
            decoded, charset, node.path, f"{node.media_type} 叶子内容"
        )
        # 换行统一为 \n，再做 Unicode NFC（指纹规范化的一部分）。
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        node.text = unicodedata.normalize("NFC", text)
        node.blob = None
    else:
        node.blob = decoded
        node.text = None


def _decode_transfer(body: bytes, cte: str, path: str) -> bytes:
    if cte == "7bit":
        _validate_7bit(body, path)
        return body
    if cte in ("8bit", "binary"):
        return body
    if cte == "base64":
        return _decode_base64(body, path)
    if cte == "quoted-printable":
        return _decode_qp(body, path)
    raise MIMEParseError(
        "UNSUPPORTED_CTE", f"不支持的 CTE {cte!r}", path, {"cte": cte}
    )


def _validate_7bit(body: bytes, path: str) -> None:
    # RFC 2045 意义上的 7bit = 全部字节为 US-ASCII（无 NUL/高位字节）。
    # 这里不强制 CRLF 配对：归档解析对裸 CR/LF 宽容（叶子阶段统一换行），
    # 以满足“不同换行风格得到同一指纹”的要求；真正有判别力的不变量
    # （仅 ASCII）仍严格检查。
    for off, c in enumerate(body):
        if c == 0x00 or c >= 0x80:
            raise MIMEParseError(
                "UNDECODABLE_CONTENT",
                "声明为 7bit 的内容出现非 US-ASCII 或 NUL 字节",
                path,
                {"offset": off, "byte": c},
            )


_BASE64_ALPHABET = set(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def _decode_base64(body: bytes, path: str) -> bytes:
    # RFC 2045: base64 解码忽略所有换行；其余非字母表字符一律报错。
    stripped = bytearray()
    line_breaks: list[int] = []
    for off, c in enumerate(body):
        if c in (0x0D, 0x0A):
            line_breaks.append(off)
            continue
        stripped.append(c)
    data = bytes(stripped)
    if not data:
        return b""
    for idx, c in enumerate(data):
        if c not in _BASE64_ALPHABET:
            raise MIMEParseError(
                "UNDECODABLE_CONTENT",
                f"base64 内容含非法字符 0x{c:02x}",
                path,
                {"offset": _stripped_offset_to_original(body, idx), "byte": c},
            )
    if len(data) % 4 != 0:
        raise MIMEParseError(
            "UNDECODABLE_CONTENT",
            f"base64 长度 {len(data)} 不是 4 的倍数（内容可能被截断）",
            path,
            {"length": len(data)},
        )
    pad = data.count(b"=")
    if pad > 2 or (pad and data[-pad:] != b"=" * pad):
        raise MIMEParseError(
            "UNDECODABLE_CONTENT",
            "base64 padding 非法：'=' 只能出现在末尾且最多两个",
            path,
        )
    try:
        return base64.b64decode(data, validate=True)
    except binascii.Error as exc:
        raise MIMEParseError(
            "UNDECODABLE_CONTENT", f"base64 解码失败: {exc}", path
        ) from None


def _stripped_offset_to_original(body: bytes, stripped_off: int) -> int:
    """把“去掉换行后”的偏移换算回原始字节偏移，供错误定位。"""

    seen = 0
    for off, c in enumerate(body):
        if c in (0x0D, 0x0A):
            continue
        if seen == stripped_off:
            return off
        seen += 1
    return len(body)


def _decode_qp(body: bytes, path: str) -> bytes:
    """严格的 quoted-printable 解码器（RFC 2045 §6.7）。"""

    out = bytearray()
    i = 0
    n = len(body)
    line_no = 1
    while i < n:
        c = body[i]
        if c == ord("="):
            if i + 1 < n and body[i + 1] in (0x0D, 0x0A):
                # 软换行 =(CR)LF，整段删除（折行还原）
                if body[i + 1] == 0x0D and i + 2 < n and body[i + 2] == 0x0A:
                    i += 3
                else:
                    i += 2
                line_no += 1
                continue
            if i + 2 >= n or not _HEX_RE.match(body[i + 1:i + 3]):
                raise MIMEParseError(
                    "UNDECODABLE_CONTENT",
                    "quoted-printable 中 '=' 后必须是两个十六进制数字或软换行",
                    path,
                    {"offset": i, "line": line_no},
                )
            out.append(int(body[i + 1:i + 3], 16))
            i += 3
            continue
        if c in (0x0D, 0x0A):
            if c == 0x0D and i + 1 < n and body[i + 1] == 0x0A:
                out += b"\r\n"
                i += 2
            else:
                out.append(c)
                i += 1
            line_no += 1
            continue
        # 未编码字节必须是可打印 ASCII（33..126）或水平制表符。
        if c < 0x20 and c != 0x09:
            raise MIMEParseError(
                "UNDECODABLE_CONTENT",
                f"quoted-printable 含非法控制字节 0x{c:02x}，应以 ={c:02X} 编码",
                path,
                {"offset": i, "line": line_no},
            )
        if c == 0x7F:
            raise MIMEParseError(
                "UNDECODABLE_CONTENT",
                "quoted-printable 含 DEL (0x7F)，应以 =7F 编码",
                path,
                {"offset": i, "line": line_no},
            )
        out.append(c)
        i += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# 指纹：规范化头 + 树结构 + 叶子内容
# ---------------------------------------------------------------------------


def canonical_header_lines(node: MIMENode) -> list[list[str]]:
    """节点用于指纹的规范化头字段 ``[[name, value], ...]``。

    * 头名小写；值解码 RFC 2047、NFC、线性空白压缩；
    * 三个结构头不在这里（结构信息单独入树）；
    * 保留重复普通头及它们的原始顺序。
    """

    lines: list[list[str]] = []
    for h in node.headers:
        if h.name in _STRUCTURAL_HEADERS:
            continue
        lines.append([h.name, _normalize_header_wsp(h.decoded)])
    return lines


def canonical_tree(node: MIMENode) -> dict:
    """生成指纹输入的规范树。

    刻意排除下列“传输/打包噪音”，使语义相同的邮件指纹一致：

    * multipart ``boundary``（每次组装都可能不同）；
    * text 节点 ``charset``（内容已解码为 Unicode）；
    * ``Content-Transfer-Encoding``（内容已解码为字节/文本）；
    * 头字段大小写、折叠方式与邮件换行风格。

    非文本叶子取解码后字节的 SHA-256；文本叶子直接内联规范化文本。
    """

    entry: dict = {
        "content_type": node.media_type,
        "headers": canonical_header_lines(node),
    }

    if node.children:
        entry["kind"] = "multipart" if node.type == "multipart" else "message"
        entry["children"] = [canonical_tree(c) for c in node.children]
        return entry

    entry["kind"] = "leaf"

    params = {
        key: val
        for key, val in node.content_type_params.items()
        if key not in ("boundary", "charset")
    }
    if params:
        entry["content_type_params"] = dict(sorted(params.items()))
    if node.disposition:
        disp: dict = {"type": node.disposition}
        if node.disposition_params:
            disp["params"] = dict(sorted(node.disposition_params.items()))
        entry["disposition"] = disp

    if node.is_text:
        entry["text"] = node.text if node.text is not None else ""
    else:
        blob = node.blob or b""
        entry["blob_sha256"] = hashlib.sha256(blob).hexdigest()
        entry["blob_length"] = len(blob)
    return entry


def canonical_json(tree: dict) -> bytes:
    """稳定 JSON：键排序、无多余空白、LF、非 ASCII 字符直出。"""

    return json.dumps(
        tree,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint(node: MIMENode) -> tuple[str, bytes]:
    """返回 ``(sha256 指纹, 规范化 JSON 字节)``。"""

    canon = canonical_json(canonical_tree(node))
    return hashlib.sha256(canon).hexdigest(), canon


def raw_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
