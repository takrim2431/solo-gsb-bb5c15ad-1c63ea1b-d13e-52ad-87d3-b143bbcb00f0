"""投递状态报告（GET /messages/{id}/delivery-report，RFC 3464 DSN）测试。

覆盖：multipart/report; report-type=delivery-status 作用域识别、直接
message/delivery-status 子件解析、每邮件/逐收件人块切分、CRLF/LF/CR
与折叠行兼容、必填字段校验（Final-Recipient/Action/Status）、可选
字段保留、多作用域文档序、封装报文内嵌套、多/零状态子件 422 与
node_path、404、无报告空列表、只读不改写。
"""

import base64

from conftest import make_multipart


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def status_body(
    recipients: list[str],
    *,
    envelope_id: str | None = "envelope-12345",
    per_message: dict[str, str] | None = None,
) -> bytes:
    """构造标准 CRLF 形式的 message/delivery-status 正文字节。"""

    fields = {"Reporting-MTA": "dns; relay.example.com"}
    if envelope_id is not None:
        fields["Original-Envelope-Id"] = envelope_id
    fields["Arrival-Date"] = "Mon, 22 Sep 2026 10:00:00 +0000"
    if per_message:
        fields.update(per_message)
    chunks = []
    for name, value in fields.items():
        chunks.append(f"{name}: {value}")
    for rcp in recipients:
        chunks.append("")  # 块间空行
        chunks.append(rcp)
    return ("\r\n".join(chunks) + "\r\n").encode()


RECIPIENT_FAILED = "\r\n".join(
    [
        "Final-Recipient: rfc822; bob@example.com",
        "Action: failed",
        "Status: 5.1.1",
        "Diagnostic-Code: smtp; 550 5.1.1 No such user",
        "Remote-MTA: dns; mx.example.com",
        "Last-Attempt-Date: Mon, 22 Sep 2026 11:00:00 +0000",
    ]
)

RECIPIENT_DELAYED = "\r\n".join(
    [
        "Final-Recipient: RFC822; Carol@Example.COM",
        "Action: delayed",
        "Status: 4.4.1",
        "Diagnostic-Code: smtp; 421 try later",
    ]
)

RECIPIENT_DELIVERED_MINIMAL = "\r\n".join(
    [
        "Final-Recipient: rfc822; alice@example.org",
        "Action: delivered",
        "Status: 2.0.0",
    ]
)


def part(head: bytes, body: bytes) -> bytes:
    return head + b"\r\n\r\n" + body


def text_plain_part(text: str = "bounce notice") -> bytes:
    return part(b"Content-Type: text/plain", text.encode())


def status_part(
    body: bytes,
    *,
    cte: str | None = None,
    charset: str | None = None,
    ctype: str = "message/delivery-status",
) -> bytes:
    ct = f"Content-Type: {ctype}".encode()
    if charset is not None:
        ct += f"; charset={charset}".encode()
    heads = [ct]
    encoded = body
    if cte is not None:
        heads.append(f"Content-Transfer-Encoding: {cte}".encode())
        if cte == "base64":
            encoded = base64.b64encode(body)
    return b"\r\n".join(heads) + b"\r\n\r\n" + encoded


def report_mail(
    *parts: bytes,
    report_type: bytes = b'report-type="delivery-status"',
    boundary: str = "RPT",
    extra_headers: bytes = b"",
) -> bytes:
    ct = b"Content-Type: multipart/report"
    if report_type:
        ct += b"; " + report_type
    ct += f'; boundary="{boundary}"'.encode()
    return (
        b"Subject: Delivery Status Notification\r\n"
        + extra_headers
        + ct
        + b"\r\n\r\n"
        + make_multipart(list(parts), boundary)
    )


def mixed_mail(*parts: bytes, boundary: str = "MIX") -> bytes:
    return (
        b"Subject: wrapper\r\n"
        + f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n'.encode()
        + make_multipart(list(parts), boundary)
    )


def standard_dsn(*recipients: str, **kw) -> bytes:
    body = status_body(list(recipients), **kw)
    return report_mail(text_plain_part(), status_part(body))


def post(client, data: bytes) -> int:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def fetch(client, mid: int):
    r = client.get(f"/messages/{mid}/delivery-report")
    return r


# ---------------------------------------------------------------------------
# 基本解析
# ---------------------------------------------------------------------------


def test_basic_dsn_single_recipient(client):
    mid = post(client, standard_dsn(RECIPIENT_FAILED))
    r = fetch(client, mid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == mid
    assert len(body["fingerprint"]) == 64
    (report,) = body["reports"]
    assert report["scope_node_path"] == ""
    assert report["report_node_path"] == "2"
    assert report["envelope_id"] == "envelope-12345"
    (rcp,) = report["recipients"]
    assert rcp == {
        "final_recipient_type": "rfc822",
        "final_recipient_address": "bob@example.com",
        "action": "failed",
        "status": "5.1.1",
        "diagnostic_code": "smtp; 550 5.1.1 No such user",
        "remote_mta": "dns; mx.example.com",
        "last_attempt_date": "Mon, 22 Sep 2026 11:00:00 +0000",
    }


def test_multiple_recipients_in_document_order_and_normalization(client):
    mid = post(
        client,
        standard_dsn(RECIPIENT_FAILED, RECIPIENT_DELAYED, RECIPIENT_DELIVERED_MINIMAL),
    )
    reports = fetch(client, mid).json()["reports"]
    (report,) = reports
    recipients = report["recipients"]
    assert [r["final_recipient_address"] for r in recipients] == [
        "bob@example.com",
        "Carol@Example.COM",  # 地址大小写保留
        "alice@example.org",
    ]
    # 地址类型规范化小写；Action 规范化小写
    assert recipients[1]["final_recipient_type"] == "rfc822"
    assert recipients[1]["action"] == "delayed"
    assert recipients[2]["action"] == "delivered"
    # 缺失的可选字段为 null
    minimal = recipients[2]
    assert minimal["diagnostic_code"] is None
    assert minimal["remote_mta"] is None
    assert minimal["last_attempt_date"] is None
    assert minimal["status"] == "2.0.0"


def test_envelope_id_absent_is_null(client):
    body = status_body([RECIPIENT_FAILED], envelope_id=None)
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    (report,) = fetch(client, mid).json()["reports"]
    assert report["envelope_id"] is None


def test_action_case_insensitive_and_other_actions(client):
    variants = ["FAILED", "Delayed", "DELIVERED", "relayed", "Expanded"]
    recipients = []
    for i, action in enumerate(variants):
        recipients.append(
            "\r\n".join(
                [
                    f"Final-Recipient: rfc822; user{i}@example.com",
                    f"Action: {action}",
                    "Status: 2.0.0",
                ]
            )
        )
    mid = post(client, standard_dsn(*recipients))
    actions = [
        r["action"]
        for r in fetch(client, mid).json()["reports"][0]["recipients"]
    ]
    assert actions == ["failed", "delayed", "delivered", "relayed", "expanded"]


def test_extra_per_message_and_per_recipient_fields_ignored(client):
    body = status_body(
        [
            RECIPIENT_FAILED
            + "\r\nX-Experimental: value\r\nX-Anomaly: 5.1.1 extra note"
        ],
        per_message={"X-Gateway": "gw1", "Received-From-MTA": "dns; in.example.com"},
    )
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    rcp = fetch(client, mid).json()["reports"][0]["recipients"][0]
    assert set(rcp) == {
        "final_recipient_type",
        "final_recipient_address",
        "action",
        "status",
        "diagnostic_code",
        "remote_mta",
        "last_attempt_date",
    }
    assert rcp["status"] == "5.1.1"


# ---------------------------------------------------------------------------
# 换行与折叠兼容
# ---------------------------------------------------------------------------


def test_lf_only_line_endings(client):
    body = (
        "Reporting-MTA: dns; relay.example.com\r\n"
        "Original-Envelope-Id: lf-env\r\n\r\n"
        "Final-Recipient: rfc822; lf@example.com\r\n"
        "Action: failed\r\n"
        "Status: 5.0.0\r\n"
    ).encode()
    lf_body = body.replace(b"\r\n", b"\n")
    mid = post(client, report_mail(text_plain_part(), status_part(lf_body)))
    report = fetch(client, mid).json()["reports"][0]
    assert report["envelope_id"] == "lf-env"
    assert report["recipients"][0]["final_recipient_address"] == "lf@example.com"


def test_cr_only_line_endings_in_status_body(client):
    # 外层 multipart 仍为 CRLF；状态部分正文内部全部用裸 CR 换行。
    body = (
        "Reporting-MTA: dns; relay.example.com\r\n"
        "Original-Envelope-Id: cr-env\r\n\r\n"
        "Final-Recipient: rfc822; cr@example.com\r\n"
        "Action: failed\r\n"
        "Status: 5.2.1\r\n"
    ).encode().replace(b"\r\n", b"\r")
    part_bytes = (
        b"Content-Type: message/delivery-status\r\n\r\n" + body
    )
    mid = post(client, report_mail(text_plain_part(), part_bytes))
    report = fetch(client, mid).json()["reports"][0]
    assert report["envelope_id"] == "cr-env"
    rcp = report["recipients"][0]
    assert rcp["final_recipient_address"] == "cr@example.com"
    assert rcp["status"] == "5.2.1"


def test_folded_headers_are_unfolded(client):
    folded = (
        "Reporting-MTA: dns; relay.example.com\r\n"
        "Original-Envelope-Id:\r\n folded-env\r\n"
        "\r\n"
        "Final-Recipient: rfc822;\r\n"
        "    fold@example.com\r\n"
        "Action:\r\n \tfailed\r\n"
        "Status: 5.1.1\r\n"
        "Diagnostic-Code: smtp;\r\n"
        "\t550 mailbox\r\n gone\r\n"
    ).encode()
    mid = post(client, report_mail(text_plain_part(), status_part(folded)))
    report = fetch(client, mid).json()["reports"][0]
    assert report["envelope_id"] == "folded-env"
    rcp = report["recipients"][0]
    assert rcp["final_recipient_address"] == "fold@example.com"
    assert rcp["action"] == "failed"
    assert rcp["diagnostic_code"] == "smtp; 550 mailbox gone"


def test_blank_line_runs_and_trailing_blanks_are_tolerated(client):
    body = (
        "Reporting-MTA: dns; relay.example.com\r\n"
        "\r\n\r\n"  # 连续空行
        "Final-Recipient: rfc822; a@example.com\r\n"
        "Action: failed\r\n"
        "Status: 5.1.1\r\n"
        "\r\n\r\n"  # 块间多个空行
        "Final-Recipient: rfc822; b@example.com\r\n"
        "Action: delivered\r\n"
        "Status: 2.0.0\r\n"
        "\r\n"  # 结尾空行
    ).encode()
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    recipients = fetch(client, mid).json()["reports"][0]["recipients"]
    assert [r["final_recipient_address"] for r in recipients] == [
        "a@example.com",
        "b@example.com",
    ]


def test_base64_transfer_encoded_status_part(client):
    body = status_body([RECIPIENT_FAILED])
    mid = post(
        client,
        report_mail(text_plain_part(), status_part(body, cte="base64")),
    )
    rcp = fetch(client, mid).json()["reports"][0]["recipients"][0]
    assert rcp["status"] == "5.1.1"
    assert rcp["diagnostic_code"] == "smtp; 550 5.1.1 No such user"


def test_utf8_charset_in_status_part(client):
    recipients = [
        RECIPIENT_FAILED.replace(
            "smtp; 550 5.1.1 No such user",
            "smtp; 550 邮箱不存在",
        )
    ]
    body = status_body(recipients)
    mid = post(
        client,
        report_mail(
            text_plain_part(),
            status_part(body, charset="utf-8", cte="8bit"),
        ),
    )
    rcp = fetch(client, mid).json()["reports"][0]["recipients"][0]
    assert rcp["diagnostic_code"] == "smtp; 550 邮箱不存在"


# ---------------------------------------------------------------------------
# 作用域识别
# ---------------------------------------------------------------------------


def test_scope_nested_inside_mixed_mail(client):
    dsn = standard_dsn(RECIPIENT_FAILED)
    enc = part(b"Content-Type: message/rfc822", dsn)
    mid = post(client, mixed_mail(text_plain_part("outer"), enc))
    reports = fetch(client, mid).json()["reports"]
    (report,) = reports
    # root=""(0) -> "2" message/rfc822(1) -> "2/1" 内部 report 根(2)
    assert report["scope_node_path"] == "2/1"
    assert report["report_node_path"] == "2/1/2"
    assert report["recipients"][0]["status"] == "5.1.1"


def test_multiple_scopes_returned_in_document_order(client):
    first = standard_dsn(RECIPIENT_FAILED)
    second = report_mail(
        text_plain_part("n2"),
        status_part(status_body([RECIPIENT_DELIVERED_MINIMAL], envelope_id="env-2")),
        boundary="RPT2",
    )
    mid = post(client, mixed_mail(first, second))
    reports = fetch(client, mid).json()["reports"]
    assert [r["scope_node_path"] for r in reports] == ["1", "2"]
    assert [r["envelope_id"] for r in reports] == ["envelope-12345", "env-2"]
    assert [r["report_node_path"] for r in reports] == ["1/2", "2/2"]
    assert reports[1]["recipients"][0]["action"] == "delivered"


def test_report_type_other_than_delivery_status_is_ignored(client):
    # multipart/report 但 report-type=disposition-notification（MDN）：
    # 不是 DSN 作用域；即使内含 message/delivery-status 也不解析。
    mdn = report_mail(
        text_plain_part(),
        status_part(status_body([RECIPIENT_FAILED])),
        report_type=b'report-type="disposition-notification"',
        boundary="MDN",
    )
    mid = post(client, mdn)
    assert fetch(client, mid).json()["reports"] == []


def test_missing_report_type_parameter_is_ignored(client):
    mail = report_mail(
        text_plain_part(),
        status_part(status_body([RECIPIENT_FAILED])),
        report_type=b"",
    )
    mid = post(client, mail)
    assert fetch(client, mid).json()["reports"] == []


def test_report_type_case_insensitive(client):
    mail = report_mail(
        text_plain_part(),
        status_part(status_body([RECIPIENT_FAILED])),
        report_type=b'Report-Type="Delivery-Status"',
    )
    mid = post(client, mail)
    assert len(fetch(client, mid).json()["reports"]) == 1


def test_non_report_mail_returns_empty_list(client):
    mid = post(client, b"Subject: hi\r\n\r\nplain body\r\n")
    body = fetch(client, mid).json()
    assert body["reports"] == []
    assert body["id"] == mid


def test_status_part_must_be_direct_child(client):
    # report 作用域：human + multipart/mixed(内嵌 status)。status 不是
    # 直接子件 → 该作用域没有直接状态子件 → 422。
    inner_mixed = mixed_mail(
        text_plain_part("x"), status_part(status_body([RECIPIENT_FAILED])),
        boundary="IN",
    )
    mail = report_mail(text_plain_part(), inner_mixed)
    mid = post(client, mail)
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "DELIVERY_STATUS_NOT_FOUND"
    assert err["node_path"] == ""


def test_human_readable_part_after_status_is_fine(client):
    # RFC 6522 虽要求状态部分在第二位；本接口只按内容类型识别直接子件，
    # 状态部分位于其他位置仍可解析。
    mail = report_mail(status_part(status_body([RECIPIENT_FAILED])),
                       text_plain_part())
    mid = post(client, mail)
    reports = fetch(client, mid).json()["reports"]
    assert reports[0]["report_node_path"] == "1"


# ---------------------------------------------------------------------------
# 422：结构 / 必填字段非法
# ---------------------------------------------------------------------------


def test_multiple_direct_status_parts_is_422(client):
    mail = report_mail(
        text_plain_part(),
        status_part(status_body([RECIPIENT_FAILED])),
        status_part(status_body([RECIPIENT_DELIVERED_MINIMAL])),
    )
    mid = post(client, mail)
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "MULTIPLE_DELIVERY_STATUS"
    # 作用域即根；node_path 指向 multipart/report 节点
    assert err["node_path"] == ""
    assert err["detail"]["count"] == 2
    assert err["detail"]["candidates"] == ["2", "3"]


def test_no_direct_status_part_is_422_with_scope_path(client):
    # report 在 mixed 的第 2 个部分，缺状态子件：node_path 指向作用域 "2"
    inner = report_mail(text_plain_part(), text_plain_part("no status"),
                        boundary="RIN")
    mid = post(client, mixed_mail(text_plain_part("outer"), inner))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "DELIVERY_STATUS_NOT_FOUND"
    assert err["node_path"] == "2"


def test_missing_required_recipient_fields_are_422(client):
    base = ["Final-Recipient: rfc822; x@example.com", "Action: failed",
            "Status: 5.1.1"]
    for dropped in ("Final-Recipient", "Action", "Status"):
        lines = [ln for ln in base if not ln.lower().startswith(dropped.lower())]
        body = status_body(["\r\n".join(lines)])
        mid = post(client, report_mail(text_plain_part(), status_part(body)))
        r = fetch(client, mid)
        assert r.status_code == 422, (dropped, r.text)
        err = r.json()["error"]
        assert err["code"] == "MISSING_DELIVERY_FIELD"
        assert err["detail"]["field"] == dropped.lower()
        # 正文级错误定位到 message/delivery-status 节点
        assert err["node_path"] == "2"


def test_missing_recipient_block_is_422(client):
    body = b"Reporting-MTA: dns; relay.example.com\r\n"
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "INVALID_DELIVERY_STATUS"
    assert err["node_path"] == "2"


def test_empty_status_body_is_422(client):
    mid = post(client, report_mail(text_plain_part(), status_part(b"")))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_DELIVERY_STATUS"


def test_status_must_be_three_numeric_segments(client):
    bad_values = [
        "5.1",            # 两段
        "5.1.1.0",        # 四段
        "5.1.a",          # 非数字
        "5.1.",           # 空段
        ".1.1",
        "5.1.1000",       # 超过 3 位
        "5555.1.1",
        "x1.1.1",
        "5. 1.1",         # 段内空白
    ]
    for bad in bad_values:
        lines = [
            "Final-Recipient: rfc822; x@example.com",
            "Action: failed",
            f"Status: {bad}",
        ]
        body = status_body(["\r\n".join(lines)])
        mid = post(client, report_mail(text_plain_part(), status_part(body)))
        r = fetch(client, mid)
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "INVALID_DELIVERY_FIELD"
        assert err["detail"]["field"] == "status"
        assert err["detail"]["value"] == bad
        assert err["node_path"] == "2"


def test_valid_status_boundaries(client):
    for value in ("0.0.0", "255.255.255", "9.99.999"):
        lines = [
            "Final-Recipient: rfc822; x@example.com",
            "Action: failed",
            f"Status: {value}",
        ]
        body = status_body(["\r\n".join(lines)])
        mid = post(client, report_mail(text_plain_part(), status_part(body)))
        r = fetch(client, mid)
        assert r.status_code == 200, (value, r.text)
        assert r.json()["reports"][0]["recipients"][0]["status"] == value


def test_bad_action_is_422(client):
    lines = [
        "Final-Recipient: rfc822; x@example.com",
        "Action: bounced",
        "Status: 5.1.1",
    ]
    body = status_body(["\r\n".join(lines)])
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "INVALID_DELIVERY_FIELD"
    assert err["detail"] == {"field": "action", "value": "bounced"}


def test_bad_final_recipient_is_422(client):
    bad_values = [
        "rfc822bob@example.com",   # 无分号
        "; bob@example.com",       # 空类型
        "rfc822; ",                # 空地址
        " ; bob@example.com",
    ]
    for bad in bad_values:
        lines = [
            f"Final-Recipient: {bad}",
            "Action: failed",
            "Status: 5.1.1",
        ]
        body = status_body(["\r\n".join(lines)])
        mid = post(client, report_mail(text_plain_part(), status_part(body)))
        r = fetch(client, mid)
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "INVALID_DELIVERY_FIELD"
        assert err["detail"]["field"] == "final-recipient"


def test_duplicate_field_in_block_is_422(client):
    block = "\r\n".join(
        [
            "Final-Recipient: rfc822; a@example.com",
            "Final-Recipient: rfc822; b@example.com",
            "Action: failed",
            "Status: 5.1.1",
        ]
    )
    body = status_body([block])
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "INVALID_DELIVERY_STATUS"
    assert err["detail"]["field"] == "final-recipient"


def test_malformed_field_line_is_422(client):
    block = "\r\n".join(
        [
            "Final-Recipient rfc822; x@example.com",  # 缺冒号
            "Action: failed",
            "Status: 5.1.1",
        ]
    )
    body = status_body([block])
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_DELIVERY_STATUS"


def test_continuation_line_at_block_start_is_422(client):
    body = (
        "Reporting-MTA: dns; relay.example.com\r\n\r\n"
        " folded@example.com\r\n"
        "Action: failed\r\n"
        "Status: 5.1.1\r\n"
    ).encode()
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_DELIVERY_STATUS"


def test_empty_required_field_value_is_422(client):
    block = (
        "Final-Recipient: rfc822; x@example.com\r\n"
        "Action:\r\n"
        "Status: 5.1.1"
    )
    body = status_body([block])
    mid = post(client, report_mail(text_plain_part(), status_part(body)))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "MISSING_DELIVERY_FIELD"


# ---------------------------------------------------------------------------
# 404 / 确定性 / 只读
# ---------------------------------------------------------------------------


def test_message_not_found_returns_404(client):
    r = client.get("/messages/999/delivery-report")
    assert r.status_code == 404
    assert "999" in r.json()["detail"]


def test_repeated_requests_are_stable(client):
    mid = post(client, standard_dsn(RECIPIENT_FAILED, RECIPIENT_DELAYED))
    first = fetch(client, mid).json()
    second = fetch(client, mid).json()
    assert first == second


def test_report_endpoint_does_not_modify_database_on_success(client):
    mid = post(client, standard_dsn(RECIPIENT_FAILED))
    before_tree = client.get(f"/messages/{mid}/tree").json()
    before_list = client.get("/messages").json()
    before_attachments = client.get("/attachments").json()
    fetch(client, mid)
    assert client.get(f"/messages/{mid}/tree").json() == before_tree
    assert client.get("/messages").json() == before_list
    assert client.get("/attachments").json() == before_attachments


def test_report_endpoint_does_not_modify_database_on_422(client):
    bad_block = "\r\n".join(
        [
            "Final-Recipient: rfc822; x@example.com",
            "Action: bounced",
            "Status: 5.1.1",
        ]
    )
    mid = post(
        client,
        report_mail(text_plain_part(), status_part(status_body([bad_block]))),
    )
    before_tree = client.get(f"/messages/{mid}/tree").json()
    before_list = client.get("/messages").json()
    r = fetch(client, mid)
    assert r.status_code == 422
    assert client.get(f"/messages/{mid}/tree").json() == before_tree
    assert client.get("/messages").json() == before_list


def test_422_in_first_scope_aborts_whole_request(client):
    bad_block = "\r\n".join(
        [
            "Final-Recipient: rfc822; x@example.com",
            "Action: failed",
            "Status: 9.9",
        ]
    )
    bad = report_mail(
        text_plain_part(),
        status_part(status_body([bad_block])),
        boundary="BAD",
    )
    good = report_mail(
        text_plain_part(),
        status_part(status_body([RECIPIENT_FAILED])),
        boundary="GOOD",
    )
    mid = post(client, mixed_mail(bad, good))
    r = fetch(client, mid)
    assert r.status_code == 422, r.text
    # 错误定位到文档序第一个（坏掉的）作用域
    assert r.json()["error"]["node_path"] == "1/2"
