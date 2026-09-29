"""会话关联查询（GET /messages/{id}/thread）测试。

覆盖：References / In-Reply-To 父子边、拓扑排序（归档乱序、兄弟按
Date）、缺失父件断链（含已借祖先入链仍保留断链）、循环引用断链
（双件环、自引用、三环）、规范化主题分组（Re:/Fwd:/中文前缀、大小写、
空白）、空主题各自成组、Message-ID 大小写不敏感、重复 Message-ID
解析到最早归档、非法 Date 回退归档时间、时区归一 UTC、确定性、404。
"""

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_message(
    *,
    msg_id: str | None = None,
    references: list[str] | None = None,
    in_reply_to: str | None = None,
    subject: str | None = None,
    date: str | None = None,
    body: str = "body",
) -> bytes:
    """构造一封带会话相关头字段的简单 text/plain 邮件。"""

    lines: list[str] = []
    if msg_id is not None:
        lines.append(f"Message-ID: <{msg_id}>")
    if references:
        refs = " ".join(f"<{r}>" for r in references)
        lines.append(f"References: {refs}")
    if in_reply_to is not None:
        lines.append(f"In-Reply-To: <{in_reply_to}>")
    if subject is not None:
        lines.append(f"Subject: {subject}")
    if date is not None:
        lines.append(f"Date: {date}")
    if not lines:
        lines.append("X-Placeholder: yes")
    return ("\r\n".join(lines) + "\r\n\r\n" + body + "\r\n").encode()


def post(client, data: bytes) -> int:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def thread(client, mid: int) -> dict:
    r = client.get(f"/messages/{mid}/thread")
    assert r.status_code == 200, r.text
    return r.json()


D1 = "Mon, 01 Jan 2024 10:00:00 +0000"
D2 = "Tue, 02 Jan 2024 10:00:00 +0000"
D3 = "Wed, 03 Jan 2024 10:00:00 +0000"


# ---------------------------------------------------------------------------
# 基本回复链与拓扑排序
# ---------------------------------------------------------------------------


def test_simple_reply_chain_topological_order(client):
    a = post(client, make_message(msg_id="a@x", subject="Hello", date=D1))
    b = post(
        client,
        make_message(
            msg_id="b@x", references=["a@x"], subject="Re: Hello", date=D2
        ),
    )
    c = post(
        client,
        make_message(
            msg_id="c@x", in_reply_to="b@x", subject="Re: Hello", date=D3
        ),
    )

    body = thread(client, c)
    assert body["order"] == [a, b, c]
    assert body["thread_id"] == a
    assert body["edges"] == [
        {"parent_id": a, "child_id": b},
        {"parent_id": b, "child_id": c},
    ]
    assert body["broken_links"] == []

    members = {m["id"]: m for m in body["members"]}
    assert members[a]["depth"] == 0
    assert members[b]["depth"] == 1
    assert members[c]["depth"] == 2
    assert members[a]["parent_id"] is None
    assert members[b]["parent_id"] == a
    assert members[c]["parent_id"] == b
    assert members[a]["message_id"] == "a@x"
    assert members[b]["normalized_subject"] == "hello"
    assert members[a]["date"] == "2024-01-01T10:00:00+00:00"


def test_reply_archived_before_parent_still_orders_parent_first(client):
    # 先归档回复、后归档父件：排序仍按引用关系，父在子前。
    child = post(
        client, make_message(msg_id="c2@x", references=["p2@x"], date=D2)
    )
    parent = post(client, make_message(msg_id="p2@x", date=D1))

    body = thread(client, child)
    assert body["order"] == [parent, child]
    assert body["thread_id"] == parent
    assert body["edges"] == [{"parent_id": parent, "child_id": child}]


def test_siblings_sorted_by_date_not_archive_order(client):
    root = post(client, make_message(msg_id="root@x", date=D1))
    later = post(
        client, make_message(msg_id="later@x", references=["root@x"], date=D3)
    )
    earlier = post(
        client,
        make_message(msg_id="earlier@x", references=["root@x"], date=D2),
    )
    assert later < earlier  # 归档顺序与日期相反

    body = thread(client, root)
    assert body["order"] == [root, earlier, later]


def test_parent_is_last_resolvable_reference(client):
    # References 链尾不可解析时，父件取链上最后一个可解析的 ID。
    grand = post(client, make_message(msg_id="g@x", date=D1))
    child = post(
        client,
        make_message(msg_id="ch@x", references=["g@x", "ghost@x"], date=D2),
    )

    body = thread(client, child)
    assert body["order"] == [grand, child]
    assert body["edges"] == [{"parent_id": grand, "child_id": child}]
    # 直接父件 ghost@x 缺失：虽已借祖父入链，断链仍保留可定位。
    assert body["broken_links"] == [
        {
            "message_id": child,
            "referenced_message_id": "ghost@x",
            "reason": "missing_parent",
        }
    ]


def test_in_reply_to_appended_after_references(client):
    # In-Reply-To 追加在 References 之后，是最直接父件。
    a = post(client, make_message(msg_id="ia@x", date=D1))
    b = post(client, make_message(msg_id="ib@x", date=D2))
    c = post(
        client,
        make_message(
            msg_id="ic@x",
            references=["ia@x"],
            in_reply_to="ib@x",
            date=D3,
        ),
    )

    body = thread(client, c)
    # 父件是 In-Reply-To 指向的 b；a 无引用关系，是同分量外的孤立根。
    assert body["edges"] == [{"parent_id": b, "child_id": c}]
    assert body["order"].index(b) < body["order"].index(c)
    assert a not in body["order"]


# ---------------------------------------------------------------------------
# 断链：缺失父件
# ---------------------------------------------------------------------------


def test_missing_parent_recorded_as_broken_link(client):
    orphan = post(
        client,
        make_message(
            msg_id="o@x", references=["ghost@x"], subject="Alone", date=D1
        ),
    )

    body = thread(client, orphan)
    assert [m["id"] for m in body["members"]] == [orphan]
    assert body["edges"] == []
    assert body["broken_links"] == [
        {
            "message_id": orphan,
            "referenced_message_id": "ghost@x",
            "reason": "missing_parent",
        }
    ]


def test_missing_parent_with_child_stays_in_thread(client):
    # 父件缺失的邮件仍可作为别人的父件，会话不因此断开。
    mid = post(client, make_message(msg_id="m@x", references=["gone@x"], date=D1))
    child = post(
        client, make_message(msg_id="mc@x", references=["m@x"], date=D2)
    )

    body = thread(client, child)
    assert body["order"] == [mid, child]
    assert body["edges"] == [{"parent_id": mid, "child_id": child}]
    assert body["broken_links"] == [
        {
            "message_id": mid,
            "referenced_message_id": "gone@x",
            "reason": "missing_parent",
        }
    ]


def test_broken_links_scoped_to_queried_thread(client):
    # 其他会话的断链不出现在本会话响应中。
    post(
        client,
        make_message(msg_id="x1@x", references=["ghost-a@x"], subject="TA"),
    )
    clean_root = post(client, make_message(msg_id="x2@x", subject="TB"))
    clean_child = post(
        client, make_message(msg_id="x3@x", references=["x2@x"], subject="TB")
    )

    body = thread(client, clean_child)
    assert body["broken_links"] == []
    assert set(body["order"]) == {clean_root, clean_child}


# ---------------------------------------------------------------------------
# 断链：循环引用
# ---------------------------------------------------------------------------


def test_two_message_cycle_broken_deterministically(client):
    g = post(
        client,
        make_message(msg_id="g@x", in_reply_to="h@x", subject="Cy", date=D1),
    )
    h = post(
        client,
        make_message(msg_id="h@x", in_reply_to="g@x", subject="Cy", date=D2),
    )

    body = thread(client, g)
    # 环上时间最早的 g 的父边被断开：g 成为根，h 为其子件。
    assert body["order"] == [g, h]
    assert body["edges"] == [{"parent_id": g, "child_id": h}]
    assert body["broken_links"] == [
        {"message_id": g, "referenced_message_id": "h@x", "reason": "cycle"}
    ]
    # 从环上另一封查询得到同一会话（信封 id/fingerprint 回显被查询邮件，
    # 会话内容完全一致）。
    other = thread(client, h)
    for key in ("thread_id", "members", "edges", "broken_links", "order"):
        assert other[key] == body[key]


def test_self_reference_is_cycle(client):
    solo = post(
        client,
        make_message(msg_id="s@x", in_reply_to="s@x", subject="Self", date=D1),
    )

    body = thread(client, solo)
    assert body["edges"] == []
    assert body["broken_links"] == [
        {"message_id": solo, "referenced_message_id": "s@x", "reason": "cycle"}
    ]


def test_three_message_cycle(client):
    a = post(client, make_message(msg_id="c1@x", references=["c3@x"], date=D1))
    b = post(client, make_message(msg_id="c2@x", references=["c1@x"], date=D2))
    c = post(client, make_message(msg_id="c3@x", references=["c2@x"], date=D3))

    body = thread(client, a)
    # 断开最早邮件 a 的父边后无环：b 的父件是 a、c 的父件是 b。
    assert body["order"] == [a, b, c]
    assert body["edges"] == [
        {"parent_id": a, "child_id": b},
        {"parent_id": b, "child_id": c},
    ]
    assert body["broken_links"] == [
        {"message_id": a, "referenced_message_id": "c3@x", "reason": "cycle"}
    ]


# ---------------------------------------------------------------------------
# 规范化主题分组
# ---------------------------------------------------------------------------


def test_unrelated_messages_grouped_by_normalized_subject(client):
    m1 = post(client, make_message(subject="Project Update", date=D1))
    m2 = post(client, make_message(subject="Re: Project Update", date=D2))
    m3 = post(client, make_message(subject="FW: Project Update", date=D3))

    body = thread(client, m2)
    assert body["order"] == [m1, m2, m3]
    assert body["edges"] == []
    assert body["broken_links"] == []
    for m in body["members"]:
        assert m["normalized_subject"] == "project update"
        assert m["depth"] == 0


def test_subject_normalization_case_whitespace_and_chinese_prefix(client):
    m1 = post(client, make_message(subject="Weekly  Report", date=D1))
    m2 = post(client, make_message(subject="RE:   weekly report", date=D2))
    m3 = post(client, make_message(subject="回复： Weekly Report", date=D3))

    body = thread(client, m1)
    assert body["order"] == [m1, m2, m3]
    assert {m["normalized_subject"] for m in body["members"]} == {
        "weekly report"
    }


def test_distinct_subjects_form_distinct_threads(client):
    m1 = post(client, make_message(subject="Alpha", date=D1))
    m2 = post(client, make_message(subject="Beta", date=D2))

    assert [m["id"] for m in thread(client, m1)["members"]] == [m1]
    assert [m["id"] for m in thread(client, m2)["members"]] == [m2]


def test_empty_subject_messages_each_form_own_thread(client):
    m1 = post(client, make_message(body="first"))
    m2 = post(client, make_message(body="second"))

    body1 = thread(client, m1)
    body2 = thread(client, m2)
    assert [m["id"] for m in body1["members"]] == [m1]
    assert [m["id"] for m in body2["members"]] == [m2]
    assert body1["thread_id"] == m1
    assert body2["thread_id"] == m2


def test_reference_linkage_wins_over_subject(client):
    # 有引用关系的邮件不与会话外同主题邮件合并。
    a = post(client, make_message(msg_id="ra@x", subject="Same", date=D1))
    b = post(
        client,
        make_message(
            msg_id="rb@x", references=["ra@x"], subject="Same", date=D2
        ),
    )
    outsider = post(client, make_message(subject="Re: Same", date=D3))

    body = thread(client, b)
    assert set(body["order"]) == {a, b}
    assert [m["id"] for m in thread(client, outsider)["members"]] == [outsider]


# ---------------------------------------------------------------------------
# msg-id 规范化与重复
# ---------------------------------------------------------------------------


def test_message_id_matching_is_case_insensitive(client):
    parent = post(client, make_message(msg_id="MixedCase@X.COM", date=D1))
    child = post(
        client,
        make_message(msg_id="low@x", references=["mixedcase@x.com"], date=D2),
    )

    body = thread(client, child)
    assert body["edges"] == [{"parent_id": parent, "child_id": child}]
    members = {m["id"]: m for m in body["members"]}
    assert members[parent]["message_id"] == "mixedcase@x.com"


def test_duplicate_message_id_resolves_to_earliest_archive(client):
    first = post(client, make_message(msg_id="dup@x", subject="D", body="one"))
    second = post(client, make_message(msg_id="dup@x", subject="D", body="two"))
    reply = post(
        client, make_message(msg_id="dr@x", references=["dup@x"], subject="D")
    )

    body = thread(client, reply)
    assert body["edges"] == [{"parent_id": first, "child_id": reply}]
    assert second not in body["order"]


# ---------------------------------------------------------------------------
# 时间解析与回退
# ---------------------------------------------------------------------------


def test_invalid_date_falls_back_to_archive_time(client):
    parent = post(client, make_message(msg_id="pd@x", date="not a date"))
    child = post(
        client,
        make_message(
            msg_id="cd@x",
            references=["pd@x"],
            date="32 Jan 2024 10:00:00 +0000",
        ),
    )

    body = thread(client, child)
    members = {m["id"]: m for m in body["members"]}
    assert members[parent]["date"] is None
    assert members[child]["date"] is None
    # 非法日期不阻断排序：拓扑序仍父在子前。
    assert body["order"] == [parent, child]


def test_timezone_offset_normalized_to_utc(client):
    msg = post(
        client,
        make_message(msg_id="tz@x", date="Mon, 01 Jan 2024 12:00:00 +0530"),
    )
    body = thread(client, msg)
    assert body["members"][0]["date"] == "2024-01-01T06:30:00+00:00"


# ---------------------------------------------------------------------------
# 确定性与错误
# ---------------------------------------------------------------------------


def test_repeated_requests_are_byte_identical(client):
    a = post(client, make_message(msg_id="da@x", subject="Det", date=D1))
    post(
        client,
        make_message(msg_id="db@x", references=["da@x", "gone@x"], date=D2),
    )
    post(client, make_message(subject="Re: Det", date=D3))

    first = thread(client, a)
    second = thread(client, a)
    assert first == second


def test_unknown_message_returns_404(client):
    r = client.get("/messages/999/thread")
    assert r.status_code == 404
    assert "999" in r.json()["detail"]


def test_response_envelope_fields(client):
    mid = post(client, make_message(msg_id="env@x", subject="Env"))
    created = client.get(f"/messages/{mid}").json()

    body = thread(client, mid)
    assert body["id"] == mid
    assert body["fingerprint"] == created["fingerprint"]
    assert body["thread_id"] == mid
    assert body["order"] == [mid]
    member = body["members"][0]
    assert member["created_at"] == created["created_at"]
    assert member["subject"] == "Env"
    assert member["parent_id"] is None
    assert member["depth"] == 0
