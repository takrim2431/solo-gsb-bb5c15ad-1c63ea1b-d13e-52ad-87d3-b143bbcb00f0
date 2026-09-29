"""解析邮件时抛出的错误。

所有错误都带有 :attr:`node_path`，指明邮件 MIME 树上的问题节点，
调用方据此向客户端返回“定位到节点”的 422 响应。
"""

from __future__ import annotations


class MIMEParseError(Exception):
    """原始 RFC 5322 邮件无法被严格解析为 MIME 树。

    :param code: 稳定的机器可读错误码
    :param message: 人类可读的错误说明
    :param node_path: 出错节点路径。根节点为 ``""``，
        子节点为 ``"1"``、``"1/2"`` 这样的 1 基路径
    :param detail: 附加上下文（如字节偏移、参数名）
    """

    def __init__(
        self,
        code: str,
        message: str,
        node_path: str = "",
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_path = node_path
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "node_path": self.node_path,
                "detail": self.detail,
            }
        }
