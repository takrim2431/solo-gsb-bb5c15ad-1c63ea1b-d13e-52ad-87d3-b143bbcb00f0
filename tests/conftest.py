"""pytest 共享夹具：临时数据库 + TestClient。"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("MIME_DB_PATH", db_path)

    # 延迟导入；lifespan 启动时读取 MIME_DB_PATH 并建表。
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_multipart(
    parts: list[bytes],
    boundary: str = "BOUNDARY123",
    *,
    crlf: bool = True,
    preamble: bytes | None = None,
    epilogue: bytes | None = None,
) -> bytes:
    nl = b"\r\n" if crlf else b"\n"
    chunks: list[bytes] = []
    if preamble is not None:
        chunks.append(preamble + nl)
    for part in parts:
        chunks.append(b"--" + boundary.encode() + nl)
        chunks.append(part)
        chunks.append(nl)
    chunks.append(b"--" + boundary.encode() + b"--")
    if epilogue is not None:
        chunks.append(nl + epilogue)
    return b"".join(chunks)


def text_part(
    body: str,
    charset: str = "utf-8",
    cte: str | None = None,
    *,
    headers: bytes = b"",
) -> bytes:
    extra = headers
    cte_header = b""
    if cte:
        cte_header = f"Content-Transfer-Encoding: {cte}\r\n".encode()
    head = (
        f"Content-Type: text/plain; charset={charset}\r\n".encode()
        + cte_header
        + extra
        + b"\r\n"
    )
    return head + body.encode(charset)
