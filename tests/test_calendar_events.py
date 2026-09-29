"""日历事件汇总（GET /calendar/events，RFC 5545 iCalendar）测试。

覆盖：text/calendar 节点扫描（含封装报文内部）、折叠行展开、METHOD
（REQUEST/CANCEL）、UID/SEQUENCE（缺省 0）/DTSTAMP/DTSTART/DTEND/
SUMMARY、必填字段缺失、UTC 与 IANA TZID 折算 UTC、DATE/浮动/坏
TZID 无效时间、夏令时不存在（春季跳时）与歧义（秋季回拨）诊断、
非法 SEQUENCE、同一节点内重复 UID 逐项诊断（附 message_id/
node_path）、跨邮件同 UID 按 SEQUENCE→DTSTAMP→message_id 选当前
版本、CANCEL 标记并保留来源、按起始时间/UID 稳定排序、只读不改写。
"""

import base64

from conftest import make_multipart


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def calendar_part(
    lines: list[str],
    *,
    charset: str = "utf-8",
    cte: str | None = None,
    content_type: str = "text/calendar",
    newline: str = "\r\n",
) -> bytes:
    body = newline.join(lines) + newline
    raw = body.encode(charset)
    heads = [f"Content-Type: {content_type}; charset={charset}".encode()]
    if cte:
        heads.append(f"Content-Transfer-Encoding: {cte}".encode())
        if cte == "base64":
            raw = base64.b64encode(raw)
        elif cte == "quoted-printable":
            from quopri import encodestring

            raw = encodestring(raw)
    return b"\r\n".join(heads) + b"\r\n\r\n" + raw


def vevent_lines(
    uid: str | None = "evt-1@example.com",
    *,
    method: str | None = "REQUEST",
    sequence: str | int | None = None,
    dtstamp: str | None = "20260920T120000Z",
    dtstart: str = "20260928T100000Z",
    dtend: str = "20260928T110000Z",
    summary: str | None = "Team sync",
    dtstart_params: str = "",
    dtend_params: str = "",
    dtstamp_params: str = "",
    extra_props: list[str] | None = None,
) -> list[str]:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//T//T//EN"]
    if method is not None:
        lines.append(f"METHOD:{method}")
    lines.append("BEGIN:VEVENT")
    if uid is not None:
        lines.append(f"UID:{uid}")
    if sequence is not None:
        lines.append(f"SEQUENCE:{sequence}")
    if dtstamp is not None:
        lines.append(f"DTSTAMP{dtstamp_params}:{dtstamp}")
    lines.append(f"DTSTART{dtstart_params}:{dtstart}")
    lines.append(f"DTEND{dtend_params}:{dtend}")
    if summary is not None:
        lines.append(f"SUMMARY:{summary}")
    if extra_props:
        lines.extend(extra_props)
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return lines


def invite_mail(*parts: bytes, boundary: str = "ICALB") -> bytes:
    return (
        b"Subject: calendar\r\n"
        b"Content-Type: multipart/mixed; boundary=" + boundary.encode() + b"\r\n\r\n"
        + make_multipart(list(parts), boundary)
    )


def post(client, data: bytes) -> int:
    r = client.post(
        "/messages",
        content=data,
        headers={"Content-Type": "message/rfc822"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def events(client, **filters):
    return client.get("/calendar/events", params=filters).json()


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


def test_basic_utc_event(client):
    post(client, invite_mail(calendar_part(vevent_lines())))
    data = events(client)
    assert data["diagnostics"] == []
    assert len(data["events"]) == 1
    e = data["events"][0]
    assert e["uid"] == "evt-1@example.com"
    assert e["sequence"] == 0  # 缺省 0
    assert e["method"] == "REQUEST"
    assert e["status"] == "confirmed"
    assert e["dtstamp"] == "2026-09-20T12:00:00Z"
    assert e["dtstart"] == "2026-09-28T10:00:00Z"
    assert e["dtend"] == "2026-09-28T11:00:00Z"
    assert e["summary"] == "Team sync"
    assert e["source"] == {"message_id": 1, "node_path": "1"}


def test_sequence_explicit(client):
    post(client, invite_mail(calendar_part(vevent_lines(sequence=3))))
    assert events(client)["events"][0]["sequence"] == 3


def test_method_lowercase_normalized(client):
    post(client, invite_mail(calendar_part(vevent_lines(method="request"))))
    assert events(client)["events"][0]["method"] == "REQUEST"


def test_method_other_value_kept_verbatim_uppercased(client):
    post(client, invite_mail(calendar_part(vevent_lines(method="PUBLISH"))))
    e = events(client)["events"][0]
    assert e["method"] == "PUBLISH"
    assert e["status"] == "confirmed"


def test_no_method_is_null_but_confirmed(client):
    post(client, invite_mail(calendar_part(vevent_lines(method=None))))
    e = events(client)["events"][0]
    assert e["method"] is None
    assert e["status"] == "confirmed"


def test_summary_missing_is_null(client):
    post(client, invite_mail(calendar_part(vevent_lines(summary=None))))
    assert events(client)["events"][0]["summary"] is None


def test_text_unescaping(client):
    lines = vevent_lines(summary="Hello\\, world; all \\n new \\\\ slash")
    post(client, invite_mail(calendar_part(lines)))
    assert events(client)["events"][0]["summary"] == (
        "Hello, world; all \n new \\ slash"
    )


def test_empty_archive_returns_empty(client):
    data = events(client)
    assert data == {"events": [], "diagnostics": []}


def test_non_calendar_mail_produces_nothing(client):
    post(
        client,
        b"Subject: plain\r\nContent-Type: text/plain\r\n\r\nno calendar here",
    )
    assert events(client) == {"events": [], "diagnostics": []}


# ---------------------------------------------------------------------------
# 折叠行展开
# ---------------------------------------------------------------------------


def test_folded_lines_unfolded(client):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:fold@example.com",
        "DTSTAMP:20260920T12",
        " 0000Z",
        "DTSTART;TZI",
        " D=America/New_York:20260928T100000",
        "DTEND;TZID=America/New_York:20260928T1",
        "\t10000",
        "SUMMARY:A long",
        "  folded summary",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    data = events(client)
    assert data["diagnostics"] == []
    e = data["events"][0]
    assert e["uid"] == "fold@example.com"
    assert e["dtstamp"] == "2026-09-20T12:00:00Z"
    assert e["dtstart"] == "2026-09-28T14:00:00Z"
    assert e["dtend"] == "2026-09-28T15:00:00Z"
    assert e["summary"] == "A long folded summary"


def test_folding_with_lf_newlines(client):
    # 归档器会把 LF 统一为 \n，折叠仍须展开。
    lines = vevent_lines()
    body = ("\n".join(lines) + "\n").encode()
    part = b"Content-Type: text/calendar\r\n\r\n" + body
    post(client, invite_mail(part))
    assert len(events(client)["events"]) == 1


def test_base64_cte_calendar(client):
    post(client, invite_mail(calendar_part(vevent_lines(), cte="base64")))
    assert len(events(client)["events"]) == 1


# ---------------------------------------------------------------------------
# IANA TZID 折算 UTC
# ---------------------------------------------------------------------------


def test_tzid_standard_time(client):
    lines = vevent_lines(
        dtstart="20260115T100000",
        dtend="20260115T110000",
        dtstart_params=";TZID=America/New_York",
        dtend_params=";TZID=America/New_York",
    )
    post(client, invite_mail(calendar_part(lines)))
    e = events(client)["events"][0]
    assert e["dtstart"] == "2026-01-15T15:00:00Z"  # EST = UTC-5
    assert e["dtend"] == "2026-01-15T16:00:00Z"


def test_tzid_daylight_time(client):
    lines = vevent_lines(
        dtstart="20260715T100000",
        dtend="20260715T110000",
        dtstart_params=";TZID=America/New_York",
        dtend_params=";TZID=America/New_York",
    )
    post(client, invite_mail(calendar_part(lines)))
    e = events(client)["events"][0]
    assert e["dtstart"] == "2026-07-15T14:00:00Z"  # EDT = UTC-4
    assert e["dtend"] == "2026-07-15T15:00:00Z"


def test_tzid_asia(client):
    lines = vevent_lines(
        dtstart="20260928T103000",
        dtend="20260928T113000",
        dtstart_params=";TZID=Asia/Shanghai",
        dtend_params=";TZID=Asia/Shanghai",
    )
    post(client, invite_mail(calendar_part(lines)))
    e = events(client)["events"][0]
    assert e["dtstart"] == "2026-09-28T02:30:00Z"
    assert e["dtend"] == "2026-09-28T03:30:00Z"


def test_tzid_with_quotes(client):
    lines = vevent_lines(
        dtstart="20260928T100000",
        dtend="20260928T110000",
        dtstart_params=';TZID="Europe/Berlin"',
        dtend_params=';TZID="Europe/Berlin"',
    )
    post(client, invite_mail(calendar_part(lines)))
    e = events(client)["events"][0]
    assert e["dtstart"] == "2026-09-28T08:00:00Z"  # CEST = UTC+2


def test_tzid_europe_dst_gap(client):
    # 2026-03-29 02:30 柏林：02:00→03:00 跳时，02:30 不存在。
    lines = vevent_lines(
        dtstart="20260329T023000",
        dtend="20260329T033000",
        dtstart_params=";TZID=Europe/Berlin",
        dtend_params=";TZID=Europe/Berlin",
    )
    post(client, invite_mail(calendar_part(lines)))
    data = events(client)
    assert data["events"] == []
    codes = sorted(d["code"] for d in data["diagnostics"])
    assert codes == ["CALENDAR_TIME_NONEXISTENT"]
    d = data["diagnostics"][0]
    assert d["message_id"] == 1
    assert d["node_path"] == "1"
    assert d["detail"]["field"] == "DTSTART"


def test_tzid_us_spring_gap(client):
    # 2026-03-08 美国东部 02:00→03:00，02:30 不存在。
    lines = vevent_lines(
        dtstart="20260308T023000",
        dtend="20260308T033000",
        dtstart_params=";TZID=America/New_York",
        dtend_params=";TZID=America/New_York",
    )
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    assert [d["code"] for d in diags] == ["CALENDAR_TIME_NONEXISTENT"]
    assert diags[0]["detail"]["field"] == "DTSTART"


def test_tzid_fall_back_ambiguous(client):
    # 2026-11-01 美国东部 01:00→01:00 回拨，01:30 出现两次。
    lines = vevent_lines(
        dtstart="20261101T013000",
        dtend="20261101T023000",
        dtstart_params=";TZID=America/New_York",
        dtend_params=";TZID=America/New_York",
    )
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    assert [d["code"] for d in diags] == ["CALENDAR_TIME_AMBIGUOUS"]
    assert diags[0]["detail"]["field"] == "DTSTART"
    # DTEND=02:30（回拨后唯一）不应有诊断。
    assert len(diags) == 1


def test_gap_and_missing_field_both_diagnosed(client):
    lines = vevent_lines(
        uid=None,
        dtstart="20260308T023000",
        dtend="20260308T033000",
        dtstart_params=";TZID=America/New_York",
        dtend_params=";TZID=America/New_York",
    )
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    codes = sorted(d["code"] for d in diags)
    assert codes == ["CALENDAR_TIME_NONEXISTENT", "MISSING_CALENDAR_FIELD"]


# ---------------------------------------------------------------------------
# 无效时间
# ---------------------------------------------------------------------------


def test_date_only_value_invalid(client):
    lines = vevent_lines(
        dtstart="20260928",
        dtend="20260929",
        dtstart_params=";VALUE=DATE",
        dtend_params=";VALUE=DATE",
    )
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    assert [d["code"] for d in diags] == [
        "INVALID_CALENDAR_TIME",
        "INVALID_CALENDAR_TIME",
    ]
    assert {d["detail"]["field"] for d in diags} == {"DTSTART", "DTEND"}


def test_floating_time_invalid(client):
    body_lines = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:float@example.com",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:20260928T100000",  # 无 Z 无 TZID
        "DTEND:20260928T110000",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(body_lines)))
    diags = events(client)["diagnostics"]
    assert [d["code"] for d in diags] == [
        "INVALID_CALENDAR_TIME",
        "INVALID_CALENDAR_TIME",
    ]


def test_unknown_tzid_invalid(client):
    lines = vevent_lines(
        dtstart="20260928T100000",
        dtend="20260928T110000",
        dtstart_params=";TZID=Not/AZone",
        dtend_params=";TZID=Not/AZone",
    )
    post(client, invite_mail(calendar_part(lines)))
    assert all(
        d["code"] == "INVALID_CALENDAR_TIME" for d in events(client)["diagnostics"]
    )


def test_z_with_tzid_invalid(client):
    lines = vevent_lines(
        dtstart="20260928T100000Z",
        dtend="20260928T110000Z",
        dtstart_params=";TZID=America/New_York",
        dtend_params=";TZID=America/New_York",
    )
    post(client, invite_mail(calendar_part(lines)))
    assert all(
        d["code"] == "INVALID_CALENDAR_TIME" for d in events(client)["diagnostics"]
    )


def test_impossible_calendar_date_invalid(client):
    lines = vevent_lines(
        dtstart="20260230T100000Z", dtend="20260230T110000Z"
    )
    post(client, invite_mail(calendar_part(lines)))
    assert all(
        d["code"] == "INVALID_CALENDAR_TIME" for d in events(client)["diagnostics"]
    )


def test_malformed_time_invalid(client):
    lines = vevent_lines(dtstart="not-a-time", dtend="20260928T110000Z")
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    assert len(diags) == 1
    assert diags[0]["code"] == "INVALID_CALENDAR_TIME"
    assert diags[0]["detail"]["field"] == "DTSTART"
    assert diags[0]["detail"]["value"] == "not-a-time"


def test_dtstamp_with_tzid_converted(client):
    # RFC 要求 DTSTAMP 为 UTC，但带 TZID 的合法本地时间仍折算。
    lines = vevent_lines(
        dtstamp="20260920T080000", dtstamp_params=";TZID=America/New_York"
    )
    post(client, invite_mail(calendar_part(lines)))
    data = events(client)
    assert data["diagnostics"] == []
    assert data["events"][0]["dtstamp"] == "2026-09-20T12:00:00Z"


def test_dtstamp_floating_invalid(client):
    lines = vevent_lines(dtstamp="20260920T080000", dtstamp_params="")
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    assert any(
        d["code"] == "INVALID_CALENDAR_TIME" and d["detail"]["field"] == "DTSTAMP"
        for d in diags
    )
    assert events(client)["events"] == []


# ---------------------------------------------------------------------------
# 必填字段 / SEQUENCE
# ---------------------------------------------------------------------------


def test_missing_uid(client):
    post(client, invite_mail(calendar_part(vevent_lines(uid=None))))
    diags = events(client)["diagnostics"]
    assert len(diags) == 1
    assert diags[0]["code"] == "MISSING_CALENDAR_FIELD"
    assert diags[0]["detail"] == {"vevent_index": 0, "field": "UID"}
    assert events(client)["events"] == []


def test_empty_uid(client):
    lines = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:20260928T100000Z",
        "DTEND:20260928T110000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    assert diags[0]["code"] == "MISSING_CALENDAR_FIELD"


def test_missing_dtstart_and_dtend(client):
    lines = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:no-times@example.com",
        "DTSTAMP:20260920T120000Z",
        "SUMMARY:x",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    fields = sorted(d["detail"]["field"] for d in diags)
    assert fields == ["DTEND", "DTSTART"]
    assert all(d["code"] == "MISSING_CALENDAR_FIELD" for d in diags)


def test_invalid_sequence(client):
    post(client, invite_mail(calendar_part(vevent_lines(sequence="abc"))))
    diags = events(client)["diagnostics"]
    assert len(diags) == 1
    assert diags[0]["code"] == "INVALID_CALENDAR_SEQUENCE"
    assert diags[0]["detail"]["value"] == "abc"
    assert events(client)["events"] == []


def test_negative_sequence_invalid(client):
    post(client, invite_mail(calendar_part(vevent_lines(sequence="-2"))))
    diags = events(client)["diagnostics"]
    assert diags[0]["code"] == "INVALID_CALENDAR_SEQUENCE"


# ---------------------------------------------------------------------------
# 同一节点内重复 UID
# ---------------------------------------------------------------------------


def test_duplicate_uid_within_same_node(client):
    lines = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:dup@example.com",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:20260928T100000Z",
        "DTEND:20260928T110000Z",
        "SUMMARY:first",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:dup@example.com",
        "DTSTAMP:20260921T120000Z",
        "DTSTART:20260929T100000Z",
        "DTEND:20260929T110000Z",
        "SUMMARY:second",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:other@example.com",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:20260930T100000Z",
        "DTEND:20260930T110000Z",
        "SUMMARY:ok",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    data = events(client)
    # 两个重复 UID 事件逐项诊断，均不入选；third event 正常。
    dup_diags = [d for d in data["diagnostics"] if d["code"] == "DUPLICATE_CALENDAR_UID"]
    assert len(dup_diags) == 2
    assert {d["detail"]["vevent_index"] for d in dup_diags} == {0, 1}
    assert all(d["detail"]["uid"] == "dup@example.com" for d in dup_diags)
    assert all(d["detail"]["count"] == 2 for d in dup_diags)
    assert [e["uid"] for e in data["events"]] == ["other@example.com"]


def test_same_uid_in_different_nodes_is_not_duplicate(client):
    post(client, invite_mail(calendar_part(vevent_lines(uid="x@example.com"))))
    post(client, invite_mail(calendar_part(vevent_lines(uid="x@example.com"))))
    data = events(client)
    assert data["diagnostics"] == []
    assert len(data["events"]) == 1


# ---------------------------------------------------------------------------
# 跨邮件版本选择
# ---------------------------------------------------------------------------


def _version_mail(uid, method, sequence, dtstamp, summary="M"):
    return invite_mail(
        calendar_part(
            vevent_lines(
                uid=uid, method=method, sequence=sequence,
                dtstamp=dtstamp, summary=summary,
            )
        )
    )


def test_higher_sequence_wins(client):
    post(client, _version_mail("v@x", "REQUEST", 0, "20260925T000000Z"))
    post(client, _version_mail("v@x", "REQUEST", 1, "20260901T000000Z"))
    data = events(client)
    assert len(data["events"]) == 1
    e = data["events"][0]
    assert e["sequence"] == 1
    assert e["source"]["message_id"] == 2


def test_same_sequence_newer_dtstamp_wins(client):
    post(client, _version_mail("v@x", "REQUEST", 1, "20260901T000000Z"))
    post(client, _version_mail("v@x", "REQUEST", 1, "20260925T000000Z"))
    e = events(client)["events"][0]
    assert e["dtstamp"] == "2026-09-25T00:00:00Z"
    assert e["source"]["message_id"] == 2


def test_same_sequence_same_dtstamp_later_message_wins(client):
    # 同 SEQUENCE 同 DTSTAMP：归档 id（message_id）更大者胜出。
    post(client, _version_mail("v@x", "REQUEST", 1, "20260901T000000Z", "first"))
    post(client, _version_mail("v@x", "REQUEST", 1, "20260901T000000Z", "second"))
    e = events(client)["events"][0]
    assert e["source"]["message_id"] == 2
    assert e["summary"] == "second"


def test_cancel_marks_cancelled_and_keeps_source(client):
    post(client, _version_mail("v@x", "REQUEST", 1, "20260901T000000Z"))
    post(client, _version_mail("v@x", "CANCEL", 2, "20260902T000000Z"))
    data = events(client)
    assert len(data["events"]) == 1
    e = data["events"][0]
    assert e["status"] == "cancelled"
    assert e["method"] == "CANCEL"
    assert e["source"] == {"message_id": 2, "node_path": "1"}


def test_older_cancel_loses_to_newer_sequence_request(client):
    # SEQUENCE 更高的 CANCEL 胜出；SEQUENCE 低的 CANCEL 不胜出。
    post(client, _version_mail("v@x", "CANCEL", 1, "20261001T000000Z"))
    post(client, _version_mail("v@x", "REQUEST", 3, "20260101T000000Z"))
    e = events(client)["events"][0]
    assert e["status"] == "confirmed"
    assert e["sequence"] == 3


def test_different_uids_both_kept(client):
    post(client, _version_mail("a@x", "REQUEST", 0, "20260901T000000Z"))
    post(client, _version_mail("b@x", "REQUEST", 0, "20260901T000000Z"))
    assert sorted(e["uid"] for e in events(client)["events"]) == [
        "a@x",
        "b@x",
    ]


# ---------------------------------------------------------------------------
# 排序与定位
# ---------------------------------------------------------------------------


def test_events_sorted_by_start_then_uid(client):
    def mail(uid, start, end):
        return invite_mail(
            calendar_part(vevent_lines(uid=uid, dtstart=start, dtend=end))
        )

    post(client, mail("later@x", "20261001T100000Z", "20261001T110000Z"))
    post(client, mail("early@x", "20260901T100000Z", "20260901T110000Z"))
    post(client, mail("same-b@x", "20260901T080000Z", "20260901T090000Z"))
    # 与 early 同起始时间、UID 更大的事件。
    post(client, mail("zzz@x", "20260901T100000Z", "20260901T103000Z"))
    order = [(e["dtstart"], e["uid"]) for e in events(client)["events"]]
    assert order == [
        ("2026-09-01T08:00:00Z", "same-b@x"),
        ("2026-09-01T10:00:00Z", "early@x"),
        ("2026-09-01T10:00:00Z", "zzz@x"),
        ("2026-10-01T10:00:00Z", "later@x"),
    ]


def test_diagnostic_message_id_and_node_path_set(client):
    # 邮件 1：根节点直接就是 text/calendar（path ""）。
    root_ical = ("\r\n".join(vevent_lines(uid=None)) + "\r\n").encode()
    post(client, b"Content-Type: text/calendar\r\n\r\n" + root_ical)
    # 邮件 2：multipart 中的第 1 个日历部分。
    post(client, invite_mail(calendar_part(vevent_lines(uid=None))))
    diags = events(client)["diagnostics"]
    locations = sorted((d["message_id"], d["node_path"]) for d in diags)
    assert (1, "") in locations
    assert (2, "1") in locations


def test_multiple_diagnostics_sorted_stably(client):
    # 同一封里两个坏事件，验证按 (message_id, path, vevent_index, code) 排序。
    lines = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:a@x",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:20260928T100000Z",
        "DTEND:20260928T110000Z",
        "SEQUENCE:nope",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:b@x",
        "DTSTAMP:bad",
        "DTSTART:20260928T100000Z",
        "DTEND:20260928T110000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    assert diags[0]["detail"]["vevent_index"] == 0
    assert diags[0]["code"] == "INVALID_CALENDAR_SEQUENCE"
    assert diags[1]["detail"]["vevent_index"] == 1
    assert diags[1]["code"] == "INVALID_CALENDAR_TIME"


# ---------------------------------------------------------------------------
# 多节点 / 封装报文 / 多 VCALENDAR / VTIMEZONE
# ---------------------------------------------------------------------------


def test_multiple_calendar_parts_in_one_mail(client):
    mail = invite_mail(
        calendar_part(vevent_lines(uid="one@x", dtstart="20260928T080000Z",
                                   dtend="20260928T090000Z")),
        calendar_part(vevent_lines(uid="two@x", dtstart="20260928T100000Z",
                                   dtend="20260928T110000Z")),
    )
    post(client, mail)
    data = events(client)
    assert data["diagnostics"] == []
    assert [e["uid"] for e in data["events"]] == ["one@x", "two@x"]
    assert {e["source"]["node_path"] for e in data["events"]} == {"1", "2"}


def test_calendar_inside_message_rfc822(client):
    ical_body = ("\r\n".join(vevent_lines()) + "\r\n").encode()
    inner = (
        b"Subject: inner\r\nContent-Type: text/calendar\r\n\r\n" + ical_body
    )
    outer = (
        b"Subject: outer\r\nContent-Type: multipart/mixed; boundary=O\r\n\r\n"
        + make_multipart(
            [
                b"Content-Type: text/plain\r\n\r\ncover note",
                b"Content-Type: message/rfc822\r\n\r\n" + inner,
            ],
            "O",
        )
    )
    post(client, outer)
    data = events(client)
    assert data["diagnostics"] == []
    assert len(data["events"]) == 1
    assert data["events"][0]["source"]["node_path"] == "2/1"


def test_vtimezone_blocks_skipped_but_tzid_still_resolves(client):
    # VTIMEZONE 内容不应当作 VEVENT；IANA TZID 仍按系统时区库解析。
    lines = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VTIMEZONE",
        "TZID:America/New_York",
        "BEGIN:STANDARD",
        "DTSTART:19701101T020000",
        "END:STANDARD",
        "END:VTIMEZONE",
        "BEGIN:VEVENT",
        "UID:tz@x",
        "DTSTAMP:20260920T120000Z",
        "DTSTART;TZID=America/New_York:20260928T100000",
        "DTEND;TZID=America/New_York:20260928T110000",
        "SUMMARY:with vtimezone",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    data = events(client)
    assert data["diagnostics"] == []
    e = data["events"][0]
    assert e["uid"] == "tz@x"
    assert e["dtstart"] == "2026-09-28T14:00:00Z"


def test_non_utf8_charset_calendar(client):
    lines = vevent_lines(summary="会议邀请 ✓")
    post(
        client,
        invite_mail(calendar_part(lines, charset="gb18030", cte="base64")),
    )
    assert events(client)["events"][0]["summary"] == "会议邀请 ✓"


# ---------------------------------------------------------------------------
# 只读 / 确定性
# ---------------------------------------------------------------------------


def test_endpoint_is_read_only(client):
    post(client, invite_mail(calendar_part(vevent_lines())))
    before = client.get("/messages").json()
    client.get("/calendar/events")
    client.get("/calendar/events")
    after = client.get("/messages").json()
    assert before == after


def test_repeated_requests_byte_identical(client):
    post(client, invite_mail(calendar_part(vevent_lines())))
    first = client.get("/calendar/events").content
    second = client.get("/calendar/events").content
    assert first == second


# ---------------------------------------------------------------------------
# 其他边界
# ---------------------------------------------------------------------------


def test_method_appearing_after_vevent_still_attributed(client):
    # 宽容：METHOD 在 VCALENDAR 中的位置不做要求。
    lines = [
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT",
        "UID:late-method@x",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:20260928T100000Z",
        "DTEND:20260928T110000Z",
        "END:VEVENT",
        "METHOD:CANCEL",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    e = events(client)["events"][0]
    assert e["method"] == "CANCEL"
    assert e["status"] == "cancelled"


def test_empty_time_value_invalid_not_double_diagnostic(client):
    lines = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:empty-time@x",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:",
        "DTEND:20260928T110000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    diags = events(client)["diagnostics"]
    assert len(diags) == 1
    assert diags[0]["code"] == "INVALID_CALENDAR_TIME"
    assert diags[0]["detail"]["field"] == "DTSTART"


def test_recurring_rrule_event_ignored_but_its_vevent_still_read(client):
    # 需求只解析“单次 VEVENT”：RRULE 不影响字段提取（重复事件仍按单次读）。
    lines = vevent_lines(
        uid="rrule@x",
        extra_props=["RRULE:FREQ=WEEKLY;COUNT=5"],
    )
    post(client, invite_mail(calendar_part(lines)))
    assert [e["uid"] for e in events(client)["events"]] == ["rrule@x"]


def test_two_vcalendars_in_one_node(client):
    lines = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:first-cal@x",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:20260928T080000Z",
        "DTEND:20260928T090000Z",
        "END:VEVENT",
        "END:VCALENDAR",
        "BEGIN:VCALENDAR",
        "METHOD:CANCEL",
        "BEGIN:VEVENT",
        "UID:second-cal@x",
        "DTSTAMP:20260921T120000Z",
        "DTSTART:20260928T100000Z",
        "DTEND:20260928T110000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    data = events(client)
    assert data["diagnostics"] == []
    by_uid = {e["uid"]: e for e in data["events"]}
    assert by_uid["first-cal@x"]["method"] == "REQUEST"
    assert by_uid["second-cal@x"]["method"] == "CANCEL"
    assert by_uid["second-cal@x"]["status"] == "cancelled"


def test_garbage_lines_skipped_but_valid_event_survives(client):
    lines = [
        "this is not an ical line",
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "METHOD:REQUEST",
        "another-garbage-line-no-colon",
        "BEGIN:VEVENT",
        "UID:garbled@x",
        "DTSTAMP:20260920T120000Z",
        "DTSTART:20260928T100000Z",
        "DTEND:20260928T110000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(lines)))
    data = events(client)
    assert data["diagnostics"] == []
    assert [e["uid"] for e in data["events"]] == ["garbled@x"]


def test_cancel_with_older_sequence_is_replaced_by_newer_request(client):
    # 1) CANCEL seq 0；2) REQUEST seq 1（重新安排）→ 当前版本为 confirmed。
    post(client, _version_mail("resched@x", "CANCEL", 0, "20260901T000000Z"))
    post(client, _version_mail("resched@x", "REQUEST", 1, "20260910T000000Z"))
    e = events(client)["events"][0]
    assert e["sequence"] == 1
    assert e["status"] == "confirmed"
    assert e["source"]["message_id"] == 2


def test_invalid_event_does_not_block_version_selection(client):
    # 邮件 1 同 UID 事件缺 DTEND → 诊断、出局；邮件 2 同 UID 完整 → 入选。
    bad = [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:mix@x",
        "DTSTAMP:20260901T000000Z",
        "DTSTART:20260928T100000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    post(client, invite_mail(calendar_part(bad)))
    post(
        client,
        invite_mail(
            calendar_part(
                vevent_lines(
                    uid="mix@x", sequence=0, dtstamp="20260902T000000Z"
                )
            )
        ),
    )
    data = events(client)
    assert len(data["events"]) == 1
    assert data["events"][0]["uid"] == "mix@x"
    assert data["events"][0]["source"]["message_id"] == 2
    assert len(data["diagnostics"]) == 1
    assert data["diagnostics"][0]["message_id"] == 1

