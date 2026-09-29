"""投递路径时钟审计（GET /messages/{id}/routing-audit）测试。

覆盖：Received 头自底向上 hop 排序（index/header_index）、by/from/with
子句解析、圆括号注释剥离（含嵌套/时区注释）、date-time 解析（数字/
命名时区、原始时区保留与 UTC 比较）、相邻 hop 时间倒退、重复主机回环
（非相邻、规范化）、缺 by/缺/坏 date 的坏头诊断（不 422、hop 保留）、
first_anomaly 稳定性、无 Received 头空列表、封装报文内 Received 不计、
重复头保序、404、只读不改写。
"""

from app.routingaudit import (
    audit_routing,
    normalize_host,
    parse_received_date,
    parse_received_header,
)


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def received(*, by: str | None = None, frm: str | None = None,
             protocol: str | None = None, date: str | None,
             extra: str = "", comment: str = "") -> bytes:
    """构造一个 CRLF Received 头。by/date 可置 None 模拟坏头。"""

    clause = ""
    if frm is not None:
        clause += f"from {frm} "
    if by is not None:
        clause += f"by {by} "
    if comment:
        clause += f"({comment}) "
    if protocol is not None:
        clause += f"with {protocol} "
    clause += extra
    if date is None:
        return f"Received: {clause}\r\n".encode()
    return f"Received: {clause}; {date}\r\n".encode()


def mail_with_received(headers: list[bytes], body: bytes = b"body\r\n") -> bytes:
    return b"Subject: route\r\n" + b"".join(headers) + b"\r\n" + body


def post(client, data: bytes) -> int:
    r = client.post(
        "/messages",
        content=data,
        headers={"Content-Type": "message/rfc822"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# 物理顺序（最新在前）：
#   mx3 13:00Z <- mx2 12:00Z <- mx1 11:00Z
GOOD_HEADERS = [
    received(frm="mx2.example.com", by="mx3.example.org", protocol="ESMTPS",
             date="Tue, 22 Sep 2026 13:00:00 +0000", comment="Postfix"),
    received(frm="mx1.example.net", by="mx2.example.com", protocol="ESMTP",
             date="Tue, 22 Sep 2026 07:00:00 -0500"),  # == 12:00Z
    received(frm="sender.example", by="mx1.example.net", protocol="SMTP",
             date="Tue, 22 Sep 2026 11:00:00 +0000"),
]


# ---------------------------------------------------------------------------
# 纯解析单元测试
# ---------------------------------------------------------------------------


def test_parse_received_full_clauses():
    p = parse_received_header(
        "from relay.example.net ([10.0.0.1]) by mx.example.com (Postfix) "
        "with ESMTPS id ABC123; Mon, 22 Sep 2026 12:00:00 +0200"
    )
    assert p.host == "mx.example.com"
    assert p.host_raw == "mx.example.com"
    assert p.from_host == "relay.example.net"
    assert p.protocol == "esmtps"
    assert p.time.isoformat() == "2026-09-22T12:00:00+02:00"
    assert p.time_raw == "Mon, 22 Sep 2026 12:00:00 +0200"
    assert p.reasons == []


def test_comments_stripped_including_nested_and_timezone_comment():
    # 圆括号里含 ';' 与 'by' 字样，以及嵌套括号与转义，必须被整体剥掉。
    p = parse_received_header(
        "by mx.example.com (qmail 2.0 (nested \\) quote); (PDT)) "
        "with SMTP; Mon, 22 Sep 2026 12:00:00 -0700 (PDT)"
    )
    assert p.host == "mx.example.com"
    assert p.protocol == "smtp"
    assert p.reasons == []


def test_received_minimal_by_and_date_only():
    p = parse_received_header(
        "by mx.example.com; Mon, 22 Sep 2026 12:00:00 +0000"
    )
    assert p.host == "mx.example.com"
    assert p.from_host is None
    assert p.protocol is None
    assert p.reasons == []


def test_missing_by_and_missing_date():
    p = parse_received_header("from somewhere.example with ESMTP")
    assert p.host is None
    assert p.time is None
    assert p.time_raw is None
    assert p.reasons == ["missing_by", "missing_date"]


def test_missing_by_with_valid_date():
    p = parse_received_header(
        "from somewhere.example; Mon, 22 Sep 2026 12:00:00 +0000"
    )
    assert p.host is None
    assert p.time is not None
    assert p.reasons == ["missing_by"]


def test_valid_by_but_invalid_date():
    p = parse_received_header(
        "by mx.example.com; not a date at all"
    )
    assert p.host == "mx.example.com"
    assert p.time is None
    assert p.time_raw == "not a date at all"
    assert p.reasons == ["invalid_date"]


def test_valid_by_but_impossible_calendar_date():
    p = parse_received_header(
        "by mx.example.com; Mon, 30 Feb 2026 12:00:00 +0000"
    )
    assert p.host == "mx.example.com"
    assert p.time is None
    assert p.reasons == ["invalid_date"]


def test_unknown_named_timezone_is_invalid():
    p = parse_received_header(
        "by mx.example.com; Mon, 22 Sep 2026 12:00:00 XYZ"
    )
    assert p.reasons == ["invalid_date"]


def test_date_parser_timezones_and_two_digit_year():
    assert parse_received_date("Mon, 22 Sep 2026 12:00:00 GMT").utcoffset().total_seconds() == 0
    assert parse_received_date("Mon, 22 Sep 26 12:00:00 +0530").year == 2026
    assert parse_received_date("Mon, 22 Sep 99 12:00:00 +0000").year == 1999
    # 保留原始偏移，而非统一转 UTC
    dt = parse_received_date("Mon, 22 Sep 2026 12:00:00 -0500")
    assert dt.isoformat() == "2026-09-22T12:00:00-05:00"
    assert parse_received_date("garbage") is None


def test_normalize_host_variants():
    assert normalize_host("MX.Example.COM") == "mx.example.com"
    assert normalize_host("mx.example.com.") == "mx.example.com"
    assert normalize_host("[10.0.0.1]") == "10.0.0.1"
    assert normalize_host("[IPv6:2001:DB8::1]") == "2001:db8::1"
    assert normalize_host("   ") is None


# ---------------------------------------------------------------------------
# hop 顺序
# ---------------------------------------------------------------------------


def test_hops_ordered_bottom_up_with_dual_indices(client):
    mid = post(client, mail_with_received(GOOD_HEADERS))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    assert body["hop_count"] == 3
    hosts = [h["host"] for h in body["hops"]]
    assert hosts == ["mx1.example.net", "mx2.example.com", "mx3.example.org"]
    assert [h["index"] for h in body["hops"]] == [0, 1, 2]
    # 物理顺序倒序：最新一跳 header_index=0 对应最后一个 hop
    assert [h["header_index"] for h in body["hops"]] == [2, 1, 0]
    assert body["anomalies"] == []
    assert body["first_anomaly"] is None


def test_hops_protocol_normalized_and_timezone_preserved(client):
    mid = post(client, mail_with_received(GOOD_HEADERS))
    hops = client.get(f"/messages/{mid}/routing-audit").json()["hops"]
    assert [h["protocol"] for h in hops] == ["smtp", "esmtp", "esmtps"]
    assert hops[0]["from_host"] == "sender.example"
    # 第二跳头中写 -0500，响应保留 -05:00（不偷偷转 UTC）
    assert hops[1]["time"] == "2026-09-22T07:00:00-05:00"
    assert hops[1]["time_raw"] == "Tue, 22 Sep 2026 07:00:00 -0500"


def test_no_received_headers_yields_empty_audit(client):
    r = client.post("/messages", content=b"Subject: plain\r\n\r\nhi\r\n")
    mid = r.json()["id"]
    body = client.get(f"/messages/{mid}/routing-audit").json()
    assert body["hop_count"] == 0
    assert body["hops"] == []
    assert body["anomalies"] == []
    assert body["first_anomaly"] is None


def test_duplicate_received_headers_all_kept_in_order(client):
    heads = [
        received(by="mx.example.com", date="Tue, 22 Sep 2026 13:00:00 +0000"),
        received(by="mx.example.com", date="Tue, 22 Sep 2026 12:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    hops = client.get(f"/messages/{mid}/routing-audit").json()["hops"]
    assert len(hops) == 2
    assert hops[0]["header_index"] == 1
    assert hops[1]["header_index"] == 0


# ---------------------------------------------------------------------------
# 时间倒退
# ---------------------------------------------------------------------------


def test_time_reversal_detected_between_adjacent_hops(client):
    heads = [
        # 最新一跳却比上一跳早了 1 小时
        received(by="mx2.example.com", date="Tue, 22 Sep 2026 11:00:00 +0000"),
        received(by="mx1.example.net", date="Tue, 22 Sep 2026 12:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    assert len(body["anomalies"]) == 1
    a = body["anomalies"][0]
    assert a["type"] == "time_reversal"
    assert a["hop_index"] == 1
    assert a["header_index"] == 0
    assert a["detail"]["previous"] == "2026-09-22T12:00:00+00:00"
    assert a["detail"]["current"] == "2026-09-22T11:00:00+00:00"
    assert a["detail"]["delta_seconds"] == -3600
    assert body["first_anomaly"] == {
        "hop_index": 1, "header_index": 0, "type": "time_reversal"
    }


def test_equal_timestamps_are_not_reversal(client):
    heads = [
        received(by="mx2.example.com", date="Tue, 22 Sep 2026 12:00:00 +0000"),
        received(by="mx1.example.net", date="Tue, 22 Sep 2026 12:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    assert body["anomalies"] == []


def test_timezone_aware_comparison_different_offsets(client):
    # 12:00-0500 == 17:00Z，晚于 13:00Z，不算倒退
    heads = [
        received(by="mx2.example.com", date="Tue, 22 Sep 2026 12:00:00 -0500"),
        received(by="mx1.example.net", date="Tue, 22 Sep 2026 13:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    assert body["anomalies"] == []


def test_unparseable_middle_hop_skips_reversal_pair(client):
    # hop0 时间可解析，hop1 日期损坏，hop2 时间早于 hop0——
    # 1 与 2 不相邻（坏头夹在中间），且坏头自身时间不可比，故无倒退。
    heads = [
        received(by="mx3.example.org", date="Tue, 22 Sep 2026 09:00:00 +0000"),
        b"Received: by mx2.example.com; broken-date\r\n",
        received(by="mx1.example.net", date="Tue, 22 Sep 2026 11:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    types = [a["type"] for a in body["anomalies"]]
    assert "time_reversal" not in types
    assert types == ["unparseable_header"]


def test_reversal_each_adjacent_pair_reported(client):
    heads = [
        received(by="mx3.example.org", date="Tue, 22 Sep 2026 10:00:00 +0000"),
        received(by="mx2.example.com", date="Tue, 22 Sep 2026 12:00:00 +0000"),
        received(by="mx1.example.net", date="Tue, 22 Sep 2026 11:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    reversals = [a for a in body["anomalies"] if a["type"] == "time_reversal"]
    # hop1(12:00) vs hop2(10:00) 倒退；hop0(11:00) vs hop1(12:00) 正常
    assert len(reversals) == 1
    assert reversals[0]["hop_index"] == 2


# ---------------------------------------------------------------------------
# 重复主机回环
# ---------------------------------------------------------------------------


def test_host_loop_detected_non_adjacent(client):
    heads = [
        received(by="mx.example.com", date="Tue, 22 Sep 2026 14:00:00 +0000"),
        received(by="relay.example.org", date="Tue, 22 Sep 2026 13:00:00 +0000"),
        received(by="mx.example.com", date="Tue, 22 Sep 2026 12:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    loops = [a for a in body["anomalies"] if a["type"] == "host_loop"]
    assert len(loops) == 1
    assert loops[0]["hop_index"] == 2
    assert loops[0]["header_index"] == 0
    assert loops[0]["detail"] == {
        "host": "mx.example.com",
        "first_index": 0,
        "first_header_index": 2,
    }


def test_host_loop_normalizes_case_trailing_dot_and_ip_literal(client):
    heads = [
        received(by="[10.0.0.1]", date="Tue, 22 Sep 2026 14:00:00 +0000"),
        received(by="Relay.Example.ORG.", date="Tue, 22 Sep 2026 13:00:00 +0000"),
        received(by="[10.0.0.1]", date="Tue, 22 Sep 2026 12:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    loops = [a for a in body["anomalies"] if a["type"] == "host_loop"]
    assert len(loops) == 1
    assert loops[0]["detail"]["host"] == "10.0.0.1"


def test_host_loop_immediate_back_and_forth(client):
    # A -> B -> A 相邻回环：最后一跳（最新）命中
    heads = [
        received(by="b.example.com", date="Tue, 22 Sep 2026 14:00:00 +0000"),
        received(by="a.example.com", date="Tue, 22 Sep 2026 13:00:00 +0000"),
        received(by="b.example.com", date="Tue, 22 Sep 2026 12:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    loops = [a for a in body["anomalies"] if a["type"] == "host_loop"]
    assert [l["hop_index"] for l in loops] == [2]


def test_bad_header_without_host_not_considered_in_loop(client):
    heads = [
        b"Received: from mystery with ESMTP; Tue, 22 Sep 2026 14:00:00 +0000\r\n",
        received(by="mx.example.com", date="Tue, 22 Sep 2026 13:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    assert [a["type"] for a in body["anomalies"]] == ["unparseable_header"]


# ---------------------------------------------------------------------------
# 坏头诊断
# ---------------------------------------------------------------------------


def test_bad_headers_listed_as_diagnostics_not_422(client):
    heads = [
        received(by="mx2.example.com", date="Tue, 22 Sep 2026 13:00:00 +0000"),
        b"Received: totally garbled header without structure\r\n",
        received(by="mx1.example.net", date="Tue, 22 Sep 2026 11:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    r = client.get(f"/messages/{mid}/routing-audit")
    assert r.status_code == 200
    body = r.json()
    bad = body["hops"][1]
    assert bad["host"] is None
    assert bad["time"] is None
    assert bad["time_raw"] is None
    assert bad["raw"] == "totally garbled header without structure"
    anomalies = body["anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["type"] == "unparseable_header"
    assert anomalies[0]["hop_index"] == 1
    assert anomalies[0]["header_index"] == 1
    assert anomalies[0]["detail"]["reasons"] == ["missing_by", "missing_date"]
    assert body["first_anomaly"]["type"] == "unparseable_header"
    assert body["first_anomaly"]["hop_index"] == 1


def test_invalid_date_only_is_single_reason(client):
    heads = [
        received(by="mx.example.com", date="Tue, 31 Sep 2026 25:99:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    hop = body["hops"][0]
    assert hop["host"] == "mx.example.com"
    assert hop["time"] is None
    assert hop["time_raw"] == "Tue, 31 Sep 2026 25:99:00 +0000"
    assert body["anomalies"][0]["detail"]["reasons"] == ["invalid_date"]


def test_first_anomaly_is_earliest_hop_with_rank_order(client):
    # hop0 主机回环（首次出现在自身不算——再造更早重复）
    heads = [
        # header 0 / hop3: 时间倒退且主机重复 mx1（一个 hop 可同时命中）
        received(by="mx1.example.net", date="Tue, 22 Sep 2026 08:00:00 +0000"),
        # header 1 / hop2
        received(by="mx3.example.org", date="Tue, 22 Sep 2026 14:00:00 +0000"),
        # header 2 / hop1：坏头
        b"Received: garbage\r\n",
        # header 3 / hop0
        received(by="mx1.example.net", date="Tue, 22 Sep 2026 10:00:00 +0000"),
    ]
    mid = post(client, mail_with_received(heads))
    body = client.get(f"/messages/{mid}/routing-audit").json()
    # 最早异常在 hop1（坏头）
    assert body["first_anomaly"] == {
        "hop_index": 1, "header_index": 2, "type": "unparseable_header"
    }
    # hop3 同时有时间倒退与回环：同 hop 内 unparseable < reversal < loop
    at3 = [a for a in body["anomalies"] if a["hop_index"] == 3]
    assert [a["type"] for a in at3] == ["time_reversal", "host_loop"]


def test_anomalies_stable_across_repeated_requests(client):
    mid = post(client, mail_with_received(GOOD_HEADERS))
    b1 = client.get(f"/messages/{mid}/routing-audit").content
    b2 = client.get(f"/messages/{mid}/routing-audit").content
    assert b1 == b2


# ---------------------------------------------------------------------------
# 作用域：只用根邮件、404、只读
# ---------------------------------------------------------------------------


def test_received_inside_encapsulated_message_not_counted(client):
    from conftest import make_multipart

    inner = (
        b"Received: by inner.example.com; Tue, 22 Sep 2026 09:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\ninner\r\n"
    )
    wrapped = (
        b"Received: by outer.example.com; Tue, 22 Sep 2026 10:00:00 +0000\r\n"
        b"Content-Type: multipart/mixed; boundary=MIX\r\n\r\n"
        + make_multipart(
            [b"Content-Type: message/rfc822\r\n\r\n" + inner], "MIX"
        )
    )
    mid = post(client, wrapped)
    body = client.get(f"/messages/{mid}/routing-audit").json()
    assert body["hop_count"] == 1
    assert body["hops"][0]["host"] == "outer.example.com"


def test_404_for_missing_message(client):
    r = client.get("/messages/4242/routing-audit")
    assert r.status_code == 404
    assert "4242" in r.json()["detail"]


def test_audit_is_read_only(client):
    mid = post(client, mail_with_received(GOOD_HEADERS))
    before = client.get(f"/messages/{mid}").json()
    for _ in range(2):
        client.get(f"/messages/{mid}/routing-audit")
    after = client.get(f"/messages/{mid}").json()
    assert before == after
    nodes = client.get(f"/messages/{mid}/nodes").json()["nodes"]
    # 根节点头字段原样保留（坏头也不改写）
    bad = post(
        client,
        mail_with_received(
            [b"Received: garbage\r\n",
             received(by="mx.example.com",
                      date="Tue, 22 Sep 2026 10:00:00 +0000")]
        ),
    )
    root = client.get(f"/messages/{bad}/nodes/").json()["node"]
    received_headers = [h for h in root["headers"] if h[0].lower() == "received"]
    assert received_headers[0][1] == "garbage"


def test_folded_received_header_unfolded_by_archive(client):
    # 归档解析器负责折叠还原；date-time 写在续行上也应可解析
    raw = (
        b"Received: from a.example.com by b.example.com with ESMTP\r\n"
        b" id FOLD123; Tue, 22 Sep 2026 12:00:00 +0000\r\n"
        b"Subject: folded\r\n\r\nbody\r\n"
    )
    mid = post(client, raw)
    body = client.get(f"/messages/{mid}/routing-audit").json()
    assert body["hop_count"] == 1
    hop = body["hops"][0]
    assert hop["host"] == "b.example.com"
    assert hop["from_host"] == "a.example.com"
    assert hop["protocol"] == "esmtp"
    assert hop["time"] == "2026-09-22T12:00:00+00:00"
    assert body["anomalies"] == []


def test_module_entry_directly_on_root_row(client):
    # 直接对数据库根节点行跑审计模块，确认纯函数口径与 HTTP 一致。
    import sqlite3

    from app import database
    mid = post(client, mail_with_received(GOOD_HEADERS))
    # 借用应用的连接配置
    conn = sqlite3.connect(client.app.state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = database.get_node(conn, mid, "")
        result = audit_routing(row)
        assert [h.host for h in result.hops] == [
            "mx1.example.net", "mx2.example.com", "mx3.example.org"
        ]
        assert result.anomalies == []
    finally:
        conn.close()
