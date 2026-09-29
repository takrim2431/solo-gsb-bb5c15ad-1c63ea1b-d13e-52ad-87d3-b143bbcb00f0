"""FastAPI 应用入口。

启动时初始化 SQLite（``MIME_DB_PATH`` 指定路径，默认
``/data/mime.db``），监听 8000 端口（见 Dockerfile / compose）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response

from . import database
from .archive import (
    AttachmentEntry,
    archive_sha256,
    build_attachments_zip,
    entries_from_rows,
)
from .calendar import _format_utc, collect_calendar_events
from .compare import compare_messages
from .delivery import extract_delivery_reports
from .displaybody import select_display_body
from .errors import MIMEParseError
from .mimeparser import fingerprint, parse_message, raw_sha256
from .related import audit_related_resources
from .routingaudit import audit_routing
from .threading import build_thread
from .unarchive import (
    UnarchiveValidationError,
    validate_and_compute_impact,
)
from .schemas import (
    AttachmentListResponse,
    AttachmentOccurrence,
    AttachmentSummary,
    CalendarDiagnostic,
    CalendarEvent,
    CalendarEventSource,
    CalendarEventsResponse,
    CompareDiff,
    CompareFacetChange,
    CompareRef,
    CompareRequest,
    CompareResponse,
    CompareSummary,
    DeliveryRecipient,
    DeliveryReport,
    DeliveryReportListResponse,
    DisplayBodyResponse,
    DisplayBodyStep,
    MessageCreated,
    MessageListResponse,
    MessageSummary,
    NodeDetail,
    NodeDetailResponse,
    NodeListResponse,
    NodeSummary,
    RelatedOrphan,
    RelatedReference,
    RelatedResourcesResponse,
    RelatedScope,
    RoutingAnomaly,
    RoutingAuditResponse,
    RoutingFirstAnomaly,
    RoutingHop,
    SecurityReportResponse,
    SecurityThresholds,
    SecurityTotals,
    SecurityViolation,
    ThreadBrokenLink,
    ThreadEdge,
    ThreadMember,
    ThreadResponse,
    TreeNode,
    TreeResponse,
    UnarchiveItemRequest,
    UnarchiveOrphanedAttachment,
    UnarchiveRequest,
    UnarchiveResponse,
    UnarchiveTarget,
)
from .security import (
    DEFAULT_MAX_ATTACHMENT_COUNT,
    DEFAULT_MAX_DECODED_BYTES,
    DEFAULT_MAX_MIME_DEPTH,
    evaluate_security_report,
)

DEFAULT_DB_PATH = "/data/mime.db"


def _resolve_db_path() -> str:
    """运行时读取，便于环境变量在启动/测试时覆盖。"""

    return os.environ.get("MIME_DB_PATH", DEFAULT_DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库（幂等）。
    db_path = _resolve_db_path()
    database.init_db(db_path)
    app.state.db_path = db_path
    yield


app = FastAPI(
    title="MIME 邮件归档指纹 API",
    description=(
        "接收原始 RFC 5322 邮件，递归解析 MIME 树，按规范化头字段与"
        "叶子内容生成稳定指纹并入库。相同内容不同换行/头字段大小写"
        "得到同一指纹；损坏输入返回定位到节点的 422 错误且不写入。"
        "非文本叶子的解码字节按 SHA-256 去重进入内容寻址附件库，"
        "支持分页检索与带 Range 的下载。另提供 /messages/{id}/"
        "related-resources 审计 text/html 对 cid: 内嵌资源的引用，"
        "以最近 multipart/related 祖先为作用域匹配 Content-ID。"
        "/messages/{id}/display-body 则按 multipart/alternative、"
        "multipart/related（start 参数）与 multipart/mixed 的递归"
        "规则选出客户端应展示的正文，并返回逐层选择轨迹。"
        "/messages/{id}/security-report 在已保存 MIME 树上做确定性的"
        "只读附件安全策略评估（双扩展/控制字符文件名、禁止媒体类型、"
        "嵌套封装报文与规模阈值），不改写任何记录。"
        "/messages/{id}/thread 在全部归档记录上按 Message-ID / "
        "In-Reply-To / References 构建确定性回复图：缺失父件与循环"
        "引用保留为可定位断链，同一会话按引用关系拓扑排序，无法关联"
        "时按规范化主题与时间稳定分组。"
        "/messages/{id}/delivery-report 在已保存 MIME 树上只读识别"
        "multipart/report; report-type=delivery-status 作用域并按 "
        "RFC 3464 解析其直接 message/delivery-status 子件（兼容"
        "CRLF/LF 与折叠行，逐收件人校验 Final-Recipient/Action/Status）。"
        "POST /messages/compare 只读比较两封已归档邮件：按 MIME 文档序"
        "递归对齐节点，同层按内容指纹配对（重排可识别、重复指纹无法"
        "唯一配对时标记歧义），给出规范化 Content-Type、头字段与解码"
        "叶子摘要的新增/删除/变更差异及双方路径，不修改数据库。"
        "GET /messages/{id}/attachments.zip 按 MIME 文档序把每个 blob "
        "出现分别打包（同哈希不合并），内置固定序列化的 manifest.json，"
        "条目时间/权限/压缩参数固定，重复下载字节一致并以归档 SHA-256 "
        "给出强 ETag；无附件返回仅含空 manifest 的有效 ZIP。"
        "GET /messages/{id}/routing-audit 从根邮件 Received 头自底向上"
        "重建投递 hop（主机、协议、带原始时区的时间与原始顺序），检测"
        "相邻 hop 时间倒退、重复主机回环与无法解析的头，坏头不改写归档"
        "数据而列入诊断，返回稳定的 anomaly 详情与首个异常位置。"
        "GET /calendar/events 跨全部归档邮件只读汇总 text/calendar 叶子"
        "中的单次 VEVENT：展开 RFC 5545 折叠行，读取 METHOD（REQUEST/"
        "CANCEL）与 UID/SEQUENCE（缺省 0）/DTSTAMP/DTSTART/DTEND/"
        "SUMMARY，UTC 与 IANA TZID 时间折算 UTC，无效及夏令时不存在/"
        "歧义时间、同一节点重复 UID 逐项诊断（带 message_id/node_path），"
        "跨邮件同 UID 按 SEQUENCE/DTSTAMP/message_id 选当前版本，CANCEL "
        "标为已取消并保留来源，按起始时间与 UID 稳定排序。"
        "POST /messages/unarchive 按 {id, 当前指纹} 批量撤销归档：空列表、"
        "重复 id、邮件缺失或指纹不符拒绝整批且数据不变；preview=true 与"
        "执行共用同一影响计算（目标按 id 升序、将移除的附件引用数、仅本批"
        "删除后无人引用的附件 SHA-256 与可回收字节），执行在单个 "
        "BEGIN IMMEDIATE 事务内删除邮件/节点/引用并仅清理无人引用的附件，"
        "仍被其他邮件引用的字节保留可查可下载，任何失败整体回滚。"
    ),
    version="1.11.0",
    lifespan=lifespan,
)


def get_conn(request: Request) -> sqlite3.Connection:
    conn = database.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(MIMEParseError)
async def mime_parse_error_handler(request: Request, exc: MIMEParseError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=422, content=exc.to_dict())


@app.exception_handler(UnarchiveValidationError)
async def unarchive_validation_error_handler(
    request: Request, exc: UnarchiveValidationError
):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": {
                "code": exc.code,
                "message": exc.message,
                **exc.detail,
            }
        },
    )


# ---------------------------------------------------------------------------
# 节点序列化
# ---------------------------------------------------------------------------


def _node_summary(row: sqlite3.Row) -> NodeSummary:
    return NodeSummary(
        path=row["path"],
        depth=row["depth"],
        position=row["position"],
        content_type=row["content_type"],
        kind=row["kind"],
        cte=row["cte"],
        disposition=row["disposition"],
        children=json.loads(row["children_json"]),
    )


def _message_404(message_id: int) -> HTTPException:
    return HTTPException(status_code=404, detail=f"邮件 {message_id} 不存在")


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@app.get("/health")
def health(request: Request) -> dict:
    return {"status": "ok", "database": request.app.state.db_path}


@app.get("/calendar/events", response_model=CalendarEventsResponse)
def get_calendar_events(
    conn: sqlite3.Connection = Depends(get_conn),
) -> CalendarEventsResponse:
    """汇总全部归档邮件 ``text/calendar`` 节点中的单次 VEVENT（纯只读）。

    扫描所有归档邮件（含 ``message/rfc822`` / ``message/global`` 封装
    报文内部）的 ``text/calendar`` 文本叶子：先按 RFC 5545 §3.1 展开
    折叠行，读取日历 ``METHOD``（``REQUEST`` / ``CANCEL`` 等，大写）
    与每个事件的 ``UID``、``SEQUENCE``（缺省 0）、``DTSTAMP``、
    ``DTSTART``、``DTEND``、``SUMMARY``。

    每个事件必须有 UID / DTSTAMP / DTSTART / DTEND；UTC（``Z``）或
    IANA ``TZID`` 的起止时间折算 UTC；无效时间（含 DATE 值、浮动
    时间、无法解析的 TZID）以及夏令时转换中**不存在**（春季跳时）或
    **有歧义**（秋季回拨）的本地时间、非法 SEQUENCE、**同一节点内
    重复 UID** 逐项列入 ``diagnostics``（带 ``message_id`` /
    ``node_path``），对应事件不参与版本合并。

    跨邮件同一 UID 依次按（``SEQUENCE`` 降序、``DTSTAMP`` 降序、
    ``message_id`` 降序、节点文档序）取当前版本；``METHOD:CANCEL``
    的胜出版本标记 ``status=cancelled`` 且保留 ``source`` 来源。
    事件按 UTC 起始时间、UID 稳定排序。
    """

    rows = database.list_calendar_nodes(conn)
    events, diagnostics = collect_calendar_events(rows)
    return CalendarEventsResponse(
        events=[
            CalendarEvent(
                uid=event.uid,
                sequence=event.sequence,
                method=event.method,
                status=event.status,
                dtstamp=_format_utc(event.dtstamp),
                dtstart=_format_utc(event.dtstart),
                dtend=_format_utc(event.dtend),
                summary=event.summary,
                source=CalendarEventSource(
                    message_id=event.source.message_id,
                    node_path=event.source.node_path,
                ),
            )
            for event in events
        ],
        diagnostics=[
            CalendarDiagnostic(
                message_id=diagnostic.message_id,
                node_path=diagnostic.node_path,
                code=diagnostic.code,
                detail=diagnostic.detail,
            )
            for diagnostic in diagnostics
        ],
    )


@app.post("/messages", status_code=201, response_model=MessageCreated)
async def create_message(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> MessageCreated:
    """归档一封原始邮件。

    请求体即邮件原始字节（建议 ``Content-Type: message/rfc822``，
    但服务端只关心字节，不依赖该头）。解析或解码失败时返回 422，
    响应中的 ``node_path`` 标出出错节点。
    """

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="请求体为空：需要原始 RFC 5322 邮件")

    # 先完整解析+指纹；任何错误都在 save_message 之前抛出，
    # 数据库不会被触碰（“不写入半成品”）。
    tree = parse_message(data)
    fp, canonical = fingerprint(tree)

    message_id, created = database.save_message(
        conn,
        fingerprint=fp,
        raw_hash=raw_sha256(data),
        canonical=canonical,
        size=len(data),
        root=tree,
    )
    row = database.get_message(conn, message_id)
    node_count = len(database.list_nodes(conn, message_id))
    assert row is not None
    return MessageCreated(
        id=message_id,
        fingerprint=row["fingerprint"],
        raw_sha256=row["raw_sha256"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        created=created,
        node_count=node_count,
    )


@app.post("/messages/unarchive", response_model=UnarchiveResponse)
def unarchive_messages(
    payload: UnarchiveRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> UnarchiveResponse:
    """按 ``{id, fingerprint}`` 批量撤销归档（可预览）。

    请求包含一个或多个目标，每项目标给出归档 **id** 与该邮件的**当前
    指纹**（乐观并发凭据）。以下情况**拒绝整批、数据不变**：

    * 空目标列表 / id 重复：**422**（``UNARCHIVE_EMPTY_BATCH`` /
      ``UNARCHIVE_DUPLICATE_ID``）；
    * 任一 id 不存在：**404**（``UNARCHIVE_MESSAGE_NOT_FOUND``）；
    * 任一指纹与当前归档指纹不符：**409**
      （``UNARCHIVE_FINGERPRINT_MISMATCH``）。

    预览（``preview=true``）与执行使用**同一影响计算**：目标按 id
    升序返回，含每封邮件将移除的附件引用数；``orphaned_attachments``
    仅列出**本批删除后才无人引用**的附件 SHA-256 与大小，
    ``reclaimed_bytes`` 为其字节合计；仍被批外邮件引用的附件不出现、
    字节保持可查可下载。预览为只读，``executed=false``。

    执行（``preview=false`` 或缺省）在**单个 ``BEGIN IMMEDIATE``
    事务**内重新校验凭据并删除：目标邮件的附件引用 → 仅无人引用的
    附件实体（删除再以 ``NOT EXISTS`` 兜底）→ 节点 → 邮件；任何失败
    整体回滚，成功时 ``executed=true``。
    """

    items = [(m.id, m.fingerprint) for m in payload.messages]

    if payload.preview:
        # 纯只读：普通 SELECT 不开启写事务，绝不修改数据。
        impact = validate_and_compute_impact(conn, items)
        executed = False
    else:
        # BEGIN IMMEDIATE：进入即持写锁，凭据校验与删除原子串行。
        with database.immediate_transaction(conn):
            impact = validate_and_compute_impact(conn, items)
            database.delete_messages_cascade(
                conn,
                message_ids=[t.id for t in impact.targets],
                orphan_sha256s=[o.sha256 for o in impact.orphans],
            )
        executed = True

    return UnarchiveResponse(
        executed=executed,
        target_count=len(impact.targets),
        attachment_refs_removed=impact.refs_removed,
        orphaned_attachment_count=len(impact.orphans),
        reclaimed_bytes=impact.reclaimed_bytes,
        targets=[
            UnarchiveTarget(
                id=t.id,
                fingerprint=t.fingerprint,
                content_type=t.content_type,
                size_bytes=t.size_bytes,
                created_at=t.created_at,
                attachment_refs_removed=t.attachment_refs_removed,
            )
            for t in impact.targets
        ],
        orphaned_attachments=[
            UnarchiveOrphanedAttachment(sha256=o.sha256, size_bytes=o.size_bytes)
            for o in impact.orphans
        ],
    )


@app.get("/messages", response_model=MessageListResponse)
def list_messages(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> MessageListResponse:
    rows = database.list_messages(conn, limit=limit, offset=offset)
    return MessageListResponse(
        items=[
            MessageSummary(
                id=r["id"],
                fingerprint=r["fingerprint"],
                raw_sha256=r["raw_sha256"],
                content_type=r["content_type"],
                size_bytes=r["size_bytes"],
                created_at=r["created_at"],
            )
            for r in rows
        ],
        total=database.count_messages(conn),
        limit=limit,
        offset=offset,
    )


@app.get("/messages/{message_id}", response_model=MessageSummary)
def get_message(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> MessageSummary:
    row = database.get_message(conn, message_id)
    if row is None:
        raise _message_404(message_id)
    return MessageSummary(
        id=row["id"],
        fingerprint=row["fingerprint"],
        raw_sha256=row["raw_sha256"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
    )


@app.post("/messages/compare", response_model=CompareResponse)
def compare_two_messages(
    payload: CompareRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> CompareResponse:
    """只读比较两封已归档邮件的 MIME 树（不修改任何记录）。

    按 MIME 文档序递归对齐节点。**节点身份不是物化路径**：每层先按
    *同层内容指纹*配对（内容指纹与归档指纹同口径——规范化
    Content-Type、规范化头字段、解码叶子摘要，剔除 boundary /
    charset / CTE 等打包噪音）：

    * 某指纹在双方各出现一次 → 跨位置唯一配对（识别兄弟重排），随后
      递归比较该对子树；
    * 同层该指纹在任一方出现多次（如两个内容完全相同的附件互换）→
      无法唯一配对，整组记 ``ambiguous``，列出双方全部候选路径；
    * 唯一配对消费后的剩余节点按文档序 1..n 位置对齐：同位置递归
      配对（差异落到 ``changed`` 的各 facet 上），数量多出的一侧依次
      记 ``added`` / ``removed``（只报子树根，附子树节点数）。

    配对节点逐面比较：``content_type``（参数排序、剔除 boundary 与
    charset）、``disposition``（RFC 2231 参数解码排序）、``headers``
    （小写名 + 线性空白压缩，不含三个结构头与 CTE）与 ``leaf``
    （文本叶子规范化文本 SHA-256、非文本叶子解码字节 SHA-256 与长度；
    容器为 null）。``diffs`` 按双方文档序稳定排序，重复请求结果稳定。

    两个 id 相同返回 **400**；任一 id 不存在返回 **404**；请求体
    不合规（缺字段、id 非正整数）返回 **422**。
    """

    if payload.a == payload.b:
        raise HTTPException(
            status_code=400,
            detail=f"a 与 b 必须是不同的邮件 id（两者均为 {payload.a}）",
        )
    msg_a = database.get_message(conn, payload.a)
    if msg_a is None:
        raise _message_404(payload.a)
    msg_b = database.get_message(conn, payload.b)
    if msg_b is None:
        raise _message_404(payload.b)

    rows_a = database.list_nodes(conn, payload.a)
    rows_b = database.list_nodes(conn, payload.b)
    diffs = compare_messages(rows_a, rows_b)
    counts = {"added": 0, "removed": 0, "changed": 0, "ambiguous": 0}
    items: list[CompareDiff] = []
    for d in diffs:
        counts[d.kind] += 1
        items.append(
            CompareDiff(
                kind=d.kind,
                a_path=d.a_path,
                b_path=d.b_path,
                a_paths=d.a_paths,
                b_paths=d.b_paths,
                facets=[
                    CompareFacetChange(facet=f["facet"], a=f["a"], b=f["b"])
                    for f in d.facets
                ],
                node_count=d.node_count,
                fingerprint=d.fingerprint,
                content_type=d.content_type,
            )
        )
    return CompareResponse(
        a=CompareRef(id=msg_a["id"], fingerprint=msg_a["fingerprint"]),
        b=CompareRef(id=msg_b["id"], fingerprint=msg_b["fingerprint"]),
        identical=len(diffs) == 0,
        summary=CompareSummary(**counts),
        diffs=items,
    )


@app.get("/messages/{message_id}/tree", response_model=TreeResponse)
def get_tree(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> TreeResponse:
    """返回嵌套 MIME 树（children 内嵌完整子节点）。"""

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)
    root_row = next(r for r in rows if r["path"] == "")
    root = _build_nested(root_row, rows)
    return TreeResponse(id=message_id, fingerprint=msg["fingerprint"], tree=root)


def _build_nested(row: sqlite3.Row, rows: list[sqlite3.Row]) -> TreeNode:
    by_path = {r["path"]: r for r in rows}
    children_paths = json.loads(row["children_json"])
    return TreeNode(
        path=row["path"],
        depth=row["depth"],
        position=row["position"],
        content_type=row["content_type"],
        kind=row["kind"],
        cte=row["cte"],
        disposition=row["disposition"],
        children=[
            _build_nested(by_path[p], rows) for p in children_paths
        ],
    )


@app.get("/messages/{message_id}/nodes", response_model=NodeListResponse)
def list_nodes(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> NodeListResponse:
    """扁平返回全部节点（children 为子节点路径）。"""

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)
    return NodeListResponse(
        id=message_id,
        fingerprint=msg["fingerprint"],
        nodes=[_node_summary(r) for r in rows],
    )


@app.get(
    "/messages/{message_id}/nodes/{node_path:path}",
    response_model=NodeDetailResponse,
)
def get_node(
    message_id: int,
    node_path: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> NodeDetailResponse:
    """查询单个节点详情（含头字段与叶子内容）。

    根节点路径为空，使用 ``/messages/{id}/nodes/``（末尾斜杠）。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    row = database.get_node(conn, message_id, node_path)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"节点 {node_path!r} 不存在",
        )
    detail = NodeDetail(
        **_node_summary(row).model_dump(),
        headers=json.loads(row["headers_json"]),
        text=row["text_content"],
        blob_sha256=row["blob_sha256"],
        blob_length=row["blob_length"],
    )
    return NodeDetailResponse(
        id=message_id, fingerprint=msg["fingerprint"], node=detail
    )


# ---------------------------------------------------------------------------
# 展示正文选择
# ---------------------------------------------------------------------------


@app.get(
    "/messages/{message_id}/display-body",
    response_model=DisplayBodyResponse,
)
def get_display_body(
    message_id: int,
    prefer: Literal["html", "plain"] = Query(
        "html",
        description="客户端优先展示的正文格式（multipart/alternative 中优先）",
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> DisplayBodyResponse:
    """从已归档 MIME 树选出客户端应展示的正文（纯只读，不改写记录）。

    递归规则：

    * ``multipart/alternative``：在可展示分支中优先选择实际正文匹配
      ``prefer``（``text/html`` / ``text/plain``）的分支；否则取
      **最后一个可展示分支**（RFC 2046 末位最忠实）；
    * ``multipart/related``：按容器 ``start`` 参数（RFC 2387）匹配
      直接子件的 Content-ID；未配置时取**首个子件**；
    * ``multipart/mixed`` 等其他容器：取**首个可展示子件**。

    ``Content-Disposition: attachment`` 节点与
    ``message/rfc822`` / ``message/global`` 封装报文内部正文不作为
    候选；非文本叶子同样不可展示。无可展示正文返回 **404**；
    ``start`` 重复命中（或未命中）返回 **422**，错误体
    ``node_path`` 定位到对应的 multipart/related 节点。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)

    # start 配置错误（重复命中/未命中）抛 MIMEParseError，由全局
    # 异常处理器转成带 node_path 的 422。
    selection = select_display_body(rows, prefer)
    if selection.leaf is None:
        raise HTTPException(
            status_code=404,
            detail=f"邮件 {message_id} 没有可展示的正文（attachment /"
            " 封装报文 / 非文本叶子均不作为候选）",
        )
    leaf = selection.leaf
    return DisplayBodyResponse(
        id=message_id,
        fingerprint=msg["fingerprint"],
        prefer=prefer,
        node_path=leaf["path"],
        content_type=leaf["content_type"],
        text=leaf["text_content"] if leaf["text_content"] is not None else "",
        trail=[
            DisplayBodyStep(
                node_path=step.node_path,
                content_type=step.content_type,
                rule=step.rule,
                considered=step.considered,
                candidates=step.candidates,
                chosen=step.chosen,
                detail=step.detail,
            )
            for step in selection.steps
        ],
    )


# ---------------------------------------------------------------------------
# cid: 内嵌资源引用审计
# ---------------------------------------------------------------------------


@app.get(
    "/messages/{message_id}/related-resources",
    response_model=RelatedResourcesResponse,
)
def get_related_resources(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RelatedResourcesResponse:
    """审计归档邮件 HTML 对 ``cid:`` 内嵌资源的引用。

    从每个 **text/html 叶子** 的 ``src`` / ``href`` / ``poster`` /
    ``srcset`` 属性按文档序提取 ``cid:`` 引用，RFC 2392 百分号解码后，
    以该叶子 **最近的 ``multipart/related`` 祖先**为唯一作用域匹配
    ``Content-ID``：

    * 唯一命中 → ``resolved``（给 ``target_node_path``）；
    * 同作用域重复 Content-ID → ``ambiguous``（给全部 ``candidates``）；
    * 无命中或 HTML 不在任何 related 作用域 → ``missing``；
    * 不跨相邻兄弟作用域、也不向内层嵌套作用域串联。

    响应的 ``scopes`` 按文档序列出每个 multipart/related 作用域中
    未被引用的 Content-ID 节点（orphan）。邮件不存在返回 404。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)
    refs, scopes = audit_related_resources(rows)
    return RelatedResourcesResponse(
        id=message_id,
        fingerprint=msg["fingerprint"],
        references=[
            RelatedReference(
                source_node_path=r.source_node_path,
                attribute=r.attribute,
                cid=r.cid,
                scope_node_path=r.scope_node_path,
                status=r.status,
                target_node_path=r.target_node_path,
                candidates=r.candidates,
            )
            for r in refs
        ],
        scopes=[
            RelatedScope(
                node_path=s["node_path"],
                orphans=[RelatedOrphan(**o) for o in s["orphans"]],
            )
            for s in scopes
        ],
    )


# ---------------------------------------------------------------------------
# 附件安全策略评估
# ---------------------------------------------------------------------------


@app.get(
    "/messages/{message_id}/security-report",
    response_model=SecurityReportResponse,
)
def get_security_report(
    message_id: int,
    max_decoded_bytes: int = Query(
        DEFAULT_MAX_DECODED_BYTES,
        ge=0,
        description="全部附件解码字节合计上限（严格大于才命中）",
    ),
    max_attachment_count: int = Query(
        DEFAULT_MAX_ATTACHMENT_COUNT,
        ge=0,
        description="单封邮件附件数量上限（严格大于才命中）",
    ),
    max_mime_depth: int = Query(
        DEFAULT_MAX_MIME_DEPTH,
        ge=0,
        description="MIME 树深度上限，根为 0（严格大于才命中）",
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> SecurityReportResponse:
    """对已归档 MIME 树执行只读、确定性的附件安全策略评估。

    按节点物化路径做文档序遍历，递归累计全部非文本叶子的解码字节、
    附件数量与 MIME 深度，并检查附件文件名的**双扩展**与**控制
    字符**、附件的**禁止媒体类型**以及深度 ≥ 1 处的**嵌套封装
    报文**（``message/rfc822`` / ``message/global``）。三个规模
    阈值可用查询参数覆盖（非整数或负数 → 422）。

    任一规则命中都会在 ``violations`` 中给出
    ``code`` / ``node_path`` / ``detail``，且 ``allow=false``；
    无命中时 ``allow=true``。同一归档重复请求结果稳定，且不修改
    数据库。邮件不存在返回 **404**。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)
    report = evaluate_security_report(
        rows,
        max_decoded_bytes=max_decoded_bytes,
        max_attachment_count=max_attachment_count,
        max_mime_depth=max_mime_depth,
    )
    return SecurityReportResponse(
        id=message_id,
        fingerprint=msg["fingerprint"],
        allow=report.allow,
        totals=SecurityTotals(
            decoded_bytes=report.totals.decoded_bytes,
            attachment_count=report.totals.attachment_count,
            max_depth=report.totals.max_depth,
        ),
        thresholds=SecurityThresholds(
            max_decoded_bytes=max_decoded_bytes,
            max_attachment_count=max_attachment_count,
            max_mime_depth=max_mime_depth,
        ),
        violations=[
            SecurityViolation(
                code=v.code,
                node_path=v.node_path,
                detail=v.detail,
            )
            for v in report.violations
        ],
    )


# ---------------------------------------------------------------------------
# 投递状态报告（DSN）
# ---------------------------------------------------------------------------


@app.get(
    "/messages/{message_id}/delivery-report",
    response_model=DeliveryReportListResponse,
)
def get_delivery_report(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> DeliveryReportListResponse:
    """从已归档 MIME 树提取 RFC 3464 投递状态报告（纯只读，不改写记录）。

    遍历整棵已保存 MIME 树（含 ``message/rfc822`` 封装报文内部），
    识别每个 ``multipart/report; report-type="delivery-status"``
    （RFC 6522）作用域，解析其**直接** ``message/delivery-status``
    子件（RFC 3464）：

    * 按空行区分**每邮件块**（取 ``Original-Envelope-Id`` 作为信封
      标识）与**逐收件人块**，兼容 CRLF / LF / 裸 CR 与折叠行；
    * 逐收件人块必须含 ``Final-Recipient``（``addr-type; address``）、
      ``Action``（failed/delayed/delivered/relayed/expanded）与
      ``Status``（RFC 3463 三段数字增强状态码），并保留
      ``Diagnostic-Code``、``Remote-MTA``、``Last-Attempt-Date``；
    * 结果按 MIME 文档序给出作用域节点、报告节点、信封标识与标准化
      收件人字段。

    同一作用域出现多个/零个状态子件、字段块结构损坏或必填字段缺失/
    非法时返回带 ``node_path`` 的 **422**（作用域问题定位到
    multipart/report 节点，正文问题定位到 message/delivery-status
    节点）。邮件不存在返回 **404**；邮件中没有任何投递状态报告作用
    域时 ``reports`` 为空列表。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)

    def load_blob(sha256: str) -> Optional[bytes]:
        row = database.get_attachment(conn, sha256)
        return None if row is None else row["content"]

    reports = extract_delivery_reports(rows, load_blob=load_blob)
    return DeliveryReportListResponse(
        id=message_id,
        fingerprint=msg["fingerprint"],
        reports=[
            DeliveryReport(
                scope_node_path=r.scope_node_path,
                report_node_path=r.report_node_path,
                envelope_id=r.envelope_id,
                recipients=[
                    DeliveryRecipient(
                        final_recipient_type=rcp.final_recipient_type,
                        final_recipient_address=rcp.final_recipient_address,
                        action=rcp.action,
                        status=rcp.status,
                        diagnostic_code=rcp.diagnostic_code,
                        remote_mta=rcp.remote_mta,
                        last_attempt_date=rcp.last_attempt_date,
                    )
                    for rcp in r.recipients
                ],
            )
            for r in reports
        ],
    )


# ---------------------------------------------------------------------------
# 投递路径时钟审计（Received 头）
# ---------------------------------------------------------------------------


@app.get(
    "/messages/{message_id}/routing-audit",
    response_model=RoutingAuditResponse,
)
def get_routing_audit(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RoutingAuditResponse:
    """从根邮件 ``Received`` 头重建投递路径并审计时钟异常（纯只读）。

    邮件每经一个 MTA 就会在顶部追加一个 ``Received`` 头（RFC 5321
    §4.4），故物理顺序为最新在前。本接口从**根节点**（不含
    message/rfc822 封装报文内部）读取全部 ``Received`` 头，**自底向上**
    （物理顺序倒序）形成 hop 列表：每个 hop 给出 ``by`` 主机（规范化
    后与原始 token 并存）、``from`` 主机、``with`` 协议、带**头声明
    时区**的时间以及原始顺序（``index`` 为自底向上序号、
    ``header_index`` 为原始头序号）。

    检测三类异常并在 ``anomalies`` 中稳定输出：

    * ``time_reversal``：相邻两个时间可解析的 hop 出现时间倒退
      （后一跳时间更早）；
    * ``host_loop``：规范化 ``by`` 主机在更早 hop（不必相邻）已出现，
      ``detail`` 给出首次出现位置；
    * ``unparseable_header``：缺 ``by`` 主机或缺/损坏 date-time——
      **不拒收也不改写归档数据**，该 hop 仍列入响应（缺失字段为
      null），并列入诊断。

    ``first_anomaly`` 给出按 hop 顺序的首个异常位置；无异常时为 null。
    邮件不存在返回 **404**；没有任何 Received 头时 hop 列表为空。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    root_row = database.get_node(conn, message_id, "")
    if root_row is None:  # 正常归档必有根节点，保留防御性 404
        raise _message_404(message_id)

    result = audit_routing(root_row)
    first = result.anomalies[0] if result.anomalies else None
    return RoutingAuditResponse(
        id=message_id,
        fingerprint=msg["fingerprint"],
        hop_count=len(result.hops),
        hops=[
            RoutingHop(
                index=hop.index,
                header_index=hop.header_index,
                host=hop.host,
                host_raw=hop.host_raw,
                from_host=hop.from_host,
                protocol=hop.protocol,
                time=hop.time.isoformat() if hop.time is not None else None,
                time_raw=hop.time_raw,
                raw=hop.raw,
            )
            for hop in result.hops
        ],
        anomalies=[
            RoutingAnomaly(
                type=anomaly.type,
                hop_index=anomaly.hop_index,
                header_index=anomaly.header_index,
                detail=anomaly.detail,
            )
            for anomaly in result.anomalies
        ],
        first_anomaly=(
            RoutingFirstAnomaly(
                hop_index=first.hop_index,
                header_index=first.header_index,
                type=first.type,
            )
            if first is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# 会话关联查询
# ---------------------------------------------------------------------------


@app.get(
    "/messages/{message_id}/thread",
    response_model=ThreadResponse,
)
def get_thread(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> ThreadResponse:
    """返回该邮件所在的会话（回复图），纯只读、结果确定性。

    在**全部归档记录**上按 ``Message-ID`` / ``In-Reply-To`` /
    ``References`` 头构建回复图：

    * 父件取引用链中最后一个能在归档中解析的 msg-id；被引用邮件不在
      归档中时记 ``missing_parent`` 断链；循环引用（含自引用）断开
      环上时间最晚节点的父边并记 ``cycle`` 断链；
    * 父子边的连通分量即会话；没有任何引用关系的孤立邮件按规范化
      主题（去 ``Re:`` / ``Fwd:`` / ``回复：`` 等前缀）分组，空主题
      各自成组；
    * 会话内按引用关系拓扑排序（父在子前），并列按（Date 头时间，
      缺失回退归档时间，归档 id）稳定排序。

    响应给出会话成员、父子边、断链原因与排序结果；同一归档库重复
    请求结果逐字节稳定。邮件不存在返回 **404**。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_thread_sources(conn)
    result = build_thread(rows, message_id)

    # 回复深度：order 是拓扑序（父在子前），沿序累计即可。
    depth: dict[int, int] = {}
    for member_id in result.order:
        parent = result.parents.get(member_id)
        depth[member_id] = depth[parent] + 1 if parent is not None else 0

    position = {member_id: i for i, member_id in enumerate(result.order)}
    return ThreadResponse(
        id=message_id,
        fingerprint=msg["fingerprint"],
        thread_id=result.thread_id,
        members=[
            ThreadMember(
                id=m.id,
                message_id=m.message_id,
                subject=m.subject,
                normalized_subject=m.normalized_subject,
                date=m.date.isoformat() if m.date is not None else None,
                created_at=m.created_at,
                parent_id=result.parents.get(m.id),
                depth=depth[m.id],
            )
            for m in result.members
        ],
        edges=[
            ThreadEdge(parent_id=parent, child_id=child)
            for child, parent in sorted(
                result.parents.items(),
                key=lambda kv: (position[kv[1]], position[kv[0]]),
            )
        ],
        broken_links=[
            ThreadBrokenLink(
                message_id=b.message_id,
                referenced_message_id=b.referenced_message_id,
                reason=b.reason,
            )
            for b in result.broken_links
        ],
        order=result.order,
    )


# ---------------------------------------------------------------------------
# 整邮件附件 ZIP 归档
# ---------------------------------------------------------------------------


@app.get("/messages/{message_id}/attachments.zip")
def get_message_attachments_zip(
    message_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """下载整封邮件的附件 ZIP（纯只读、逐字节确定性）。

    全部非文本叶子按 **MIME 文档序**逐个写入 ZIP——同哈希的多次出现
    写成多个独立条目，不合并。归档名取解码文件名 basename（移除控制
    字符、替换 ``/`` 与 ``\\``），空名回退 ``attachment-{节点路径}``；
    名称冲突以 **NFC + casefold** 为键，在扩展名前追加 `` (2)``、
    `` (3)``。内置 ``manifest.json`` 逐项记录 node_path、
    original_filename、archive_name、media_type、sha256、size。

    条目时间/权限/压缩参数与 JSON 序列化全部固定，同一邮件重复下载
    字节一致；响应以归档自身 SHA-256 给出强 ``ETag``，命中
    ``If-None-Match`` 返回 304。无附件时返回仅含空 manifest 的有效
    ZIP；邮件不存在返回 **404**；附件字节因历史归档缺失时返回
    定位节点的 **422**。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)
    refs = database.list_message_attachment_refs(conn, message_id)
    refs_filename = {r["node_path"]: r["filename"] for r in refs}

    blobs: dict[str, bytes] = {}
    for row in rows:
        if row["kind"] != "blob":
            continue
        digest = row["blob_sha256"]
        if digest not in blobs:
            stored = database.get_attachment(conn, digest)
            if stored is None:
                # 升级附件库之前归档的邮件未留存解码字节。
                raise MIMEParseError(
                    code="ATTACHMENT_BYTES_UNAVAILABLE",
                    message=(
                        f"节点 {row['path']} 的附件字节不可用"
                        "（该邮件归档于附件字节留存升级之前，无法打包）"
                    ),
                    node_path=row["path"],
                    detail={"sha256": digest},
                )
            blobs[digest] = stored["content"]

    entries: list[AttachmentEntry] = entries_from_rows(rows, refs_filename, blobs)
    data = build_attachments_zip(entries)
    etag = archive_sha256(data)

    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None and _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="message-{message_id}-attachments.zip"'
            ),
            "ETag": f'"{etag}"',
            "Cache-Control": "no-cache",
        },
    )


def _etag_matches(header: str, etag: str) -> bool:
    """解析 ``If-None-Match``：精确匹配引号 ETag，``*`` 匹配任意存在资源。"""

    for token in (t.strip() for t in header.split(",")):
        if not token:
            continue
        if token == "*":
            return True
        if token == f'"{etag}"' or token == f'W/"{etag}"':
            return True
    return False


# ---------------------------------------------------------------------------
# 附件库
# ---------------------------------------------------------------------------


@app.get("/attachments", response_model=AttachmentListResponse)
def list_attachments(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sha256: Optional[str] = Query(
        None,
        pattern=r"^[0-9a-fA-F]{1,64}$",
        description="按哈希前缀过滤（传完整 64 位即精确查找）",
    ),
    filename: Optional[str] = Query(
        None, max_length=200, description="按解码文件名子串过滤"
    ),
    media_type: Optional[str] = Query(
        None, max_length=100, description="按出现位置的媒体类型精确过滤"
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> AttachmentListResponse:
    """分页列出附件实体，每项含引用次数与全部出现位置。"""

    rows = database.list_attachments(
        conn,
        limit=limit,
        offset=offset,
        sha256=sha256,
        filename=filename,
        media_type=media_type,
    )
    items = [
        AttachmentSummary(
            sha256=r["sha256"],
            size_bytes=r["size_bytes"],
            media_type=r["media_type"],
            reference_count=r["reference_count"],
            created_at=r["created_at"],
            occurrences=[
                AttachmentOccurrence(**dict(ref))
                for ref in database.list_attachment_refs(conn, r["sha256"])
            ],
        )
        for r in rows
    ]
    return AttachmentListResponse(
        items=items,
        total=database.count_attachments(
            conn, sha256=sha256, filename=filename, media_type=media_type
        ),
        limit=limit,
        offset=offset,
    )


def _parse_single_range(header: str, size: int) -> Optional[tuple[int, int]]:
    """解析单段 ``bytes`` Range，返回闭区间 ``(start, end)``。

    多段、语法非法、越界等一律返回 ``None``（调用方据此回 416）。
    合法的开口/后缀/超界末端按 RFC 7233 收敛到实际范围。
    """

    unit, sep, spec = header.partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None
    ranges = [r.strip() for r in spec.split(",")]
    if len(ranges) != 1 or not ranges[0]:
        return None
    start_s, dash, end_s = ranges[0].partition("-")
    if not dash:
        return None
    if not start_s:
        # 后缀范围：最后 N 字节
        if not end_s.isdigit():
            return None
        suffix = int(end_s)
        if suffix <= 0:
            return None
        start = max(size - suffix, 0)
        end = size - 1
    else:
        if not start_s.isdigit() or (end_s and not end_s.isdigit()):
            return None
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
        if start > end:
            return None
        end = min(end, size - 1)
    if start >= size:  # 越界（含空附件）
        return None
    return start, end


@app.get("/attachments/{sha256}/content")
def get_attachment_content(
    sha256: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """下载附件的原始解码字节。

    支持单段 ``bytes`` Range：合法范围返回 206 与 ``Content-Range``；
    越界、多段或非法范围返回 416（带 ``Content-Range: bytes */N``）。
    """

    row = database.get_attachment(conn, sha256.lower())
    if row is None:
        raise HTTPException(status_code=404, detail=f"附件 {sha256} 不存在")
    content: bytes = row["content"]
    size: int = row["size_bytes"]
    media_type: str = row["media_type"]

    range_header = request.headers.get("range")
    if range_header is None:
        return Response(
            content=content,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes"},
        )

    parsed = _parse_single_range(range_header, size)
    if parsed is None:
        raise HTTPException(
            status_code=416,
            detail=(
                f"Range 不可满足：{range_header!r}"
                f"（资源大小 {size} 字节，仅支持单段 bytes 范围）"
            ),
            headers={"Content-Range": f"bytes */{size}"},
        )
    start, end = parsed
    return Response(
        content=content[start:end + 1],
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
        },
    )
