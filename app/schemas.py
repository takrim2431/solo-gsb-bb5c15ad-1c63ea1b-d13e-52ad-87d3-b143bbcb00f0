"""API 响应模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class MessageSummary(BaseModel):
    id: int
    fingerprint: str
    raw_sha256: str
    content_type: str
    size_bytes: int
    created_at: str


class MessageListResponse(BaseModel):
    items: list[MessageSummary]
    total: int
    limit: int
    offset: int


class MessageCreated(BaseModel):
    id: int
    fingerprint: str
    raw_sha256: str
    content_type: str
    size_bytes: int
    created: bool  # True=新入库；False=同指纹邮件已存在（幂等）
    node_count: int


class NodeSummary(BaseModel):
    path: str
    depth: int
    position: int
    content_type: str
    kind: str
    cte: str
    disposition: Optional[str] = None
    children: list[str]


class TreeNode(BaseModel):
    """嵌套 MIME 树节点，children 为完整子树。"""

    path: str
    depth: int
    position: int
    content_type: str
    kind: str
    cte: str
    disposition: Optional[str] = None
    children: list["TreeNode"] = []


TreeNode.model_rebuild()


class TreeResponse(BaseModel):
    id: int
    fingerprint: str
    tree: TreeNode


class NodeListResponse(BaseModel):
    id: int
    fingerprint: str
    nodes: list[NodeSummary]


class NodeDetail(NodeSummary):
    headers: list[list[str]]
    text: Optional[str] = None
    blob_sha256: Optional[str] = None
    blob_length: Optional[int] = None


class NodeDetailResponse(BaseModel):
    id: int
    fingerprint: str
    node: NodeDetail


class AttachmentOccurrence(BaseModel):
    """附件的一次出现：哪封邮件的哪个节点、以什么文件名/处置方式。"""

    message_id: int
    node_path: str
    filename: Optional[str] = None
    disposition: Optional[str] = None
    media_type: str


class AttachmentSummary(BaseModel):
    sha256: str
    size_bytes: int
    media_type: str  # 首次归档时的媒体类型
    reference_count: int
    created_at: str
    occurrences: list[AttachmentOccurrence]


class AttachmentListResponse(BaseModel):
    items: list[AttachmentSummary]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# cid: 内嵌资源引用审计（GET /messages/{id}/related-resources）
# ---------------------------------------------------------------------------


class RelatedReference(BaseModel):
    """一条来自 text/html 叶子的 ``cid:`` 引用及其解析结果。"""

    source_node_path: str
    attribute: str  # src / href / poster / srcset
    cid: str  # RFC 2392 百分号解码后的目标 Content-ID
    # 最近的 multipart/related 祖先；HTML 不在任何 related 作用域时为 None
    scope_node_path: Optional[str] = None
    status: str  # resolved | missing | ambiguous
    target_node_path: Optional[str] = None  # status == resolved
    candidates: list[str] = []  # status == ambiguous 时的全部候选节点


class RelatedOrphan(BaseModel):
    """作用域内声明了 Content-ID 却未被该作用域 HTML 引用的节点。"""

    node_path: str
    content_type: str
    content_ids: list[str]


class RelatedScope(BaseModel):
    """一个 multipart/related 容器的审计侧影。"""

    node_path: str
    orphans: list[RelatedOrphan]


class RelatedResourcesResponse(BaseModel):
    id: int
    fingerprint: str
    references: list[RelatedReference]
    scopes: list[RelatedScope]


# ---------------------------------------------------------------------------
# 展示正文选择（GET /messages/{id}/display-body）
# ---------------------------------------------------------------------------


class DisplayBodyStep(BaseModel):
    """容器节点上的一次逐层选择（选择轨迹中的一项）。"""

    node_path: str
    content_type: str
    rule: str  # alternative-prefer | alternative-fallback-last
    # related-start | related-first | first-displayable
    considered: list[str]  # 该层实际评估过的直接子件路径（文档序）
    candidates: list[str]  # 规则下合格的候选子件路径（文档序）
    chosen: Optional[str]  # 实际进入的子件路径
    detail: dict = {}  # {"prefer": "html"} 或 {"start": "<cid>"}


class DisplayBodyResponse(BaseModel):
    id: int
    fingerprint: str
    prefer: str  # html | plain
    node_path: str
    content_type: str
    text: str
    trail: list[DisplayBodyStep]


# ---------------------------------------------------------------------------
# 附件安全策略评估（GET /messages/{id}/security-report）
# ---------------------------------------------------------------------------


class SecurityViolation(BaseModel):
    """一条安全规则命中。"""

    code: str
    node_path: str
    detail: dict


class SecurityTotals(BaseModel):
    """按节点路径递归累计的规模量。"""

    decoded_bytes: int  # 全部非文本叶子的解码字节合计
    attachment_count: int  # 非文本叶子（附件）个数
    max_depth: int  # MIME 树最大深度（根为 0）


class SecurityThresholds(BaseModel):
    """本次评估实际生效的阈值（默认值或查询参数覆盖后的值）。"""

    max_decoded_bytes: int
    max_attachment_count: int
    max_mime_depth: int


class SecurityReportResponse(BaseModel):
    id: int
    fingerprint: str
    allow: bool  # 无任何 violation 时为 true
    totals: SecurityTotals
    thresholds: SecurityThresholds
    violations: list[SecurityViolation]


# ---------------------------------------------------------------------------
# 会话关联查询（GET /messages/{id}/thread）
# ---------------------------------------------------------------------------


class ThreadMember(BaseModel):
    """会话中的一封邮件。"""

    id: int
    message_id: Optional[str] = None  # 规范化 Message-ID（无该头时 None）
    subject: Optional[str] = None
    normalized_subject: str  # 去 Re:/Fwd: 等前缀、压缩空白、casefold
    date: Optional[str] = None  # Date 头解析成功时的 ISO 8601（UTC）
    created_at: str
    parent_id: Optional[int] = None  # 父件归档 id（根为 None）
    depth: int  # 回复深度，根为 0


class ThreadEdge(BaseModel):
    """一条父子回复边（parent_id 是 child_id 的直接父件）。"""

    parent_id: int
    child_id: int


class ThreadBrokenLink(BaseModel):
    """一条可定位的断链：哪封邮件引用了什么、为何无法落成父边。"""

    message_id: int  # 发起引用的归档邮件 id
    referenced_message_id: str  # 规范化后的被引用 msg-id
    reason: str  # missing_parent（被引用邮件不在归档中）| cycle（循环引用）


class ThreadResponse(BaseModel):
    id: int
    fingerprint: str
    thread_id: int  # 会话的确定性标识：排序结果首成员的归档 id
    members: list[ThreadMember]  # 按 order 顺序
    edges: list[ThreadEdge]
    broken_links: list[ThreadBrokenLink]
    order: list[int]  # 拓扑/稳定排序后的归档 id 序列（父在子前）


# ---------------------------------------------------------------------------
# 投递状态报告（GET /messages/{id}/delivery-report，RFC 3464 DSN）
# ---------------------------------------------------------------------------


class DeliveryRecipient(BaseModel):
    """一个逐收件人块的标准化字段。"""

    final_recipient_type: str  # 小写地址类型，如 "rfc822"
    final_recipient_address: str  # 去首尾空白后的地址（保留原始大小写）
    action: str  # failed | delayed | delivered | relayed | expanded（小写）
    status: str  # RFC 3463 三段数字增强状态码
    diagnostic_code: Optional[str] = None
    remote_mta: Optional[str] = None
    last_attempt_date: Optional[str] = None


class DeliveryReport(BaseModel):
    """一个 multipart/report; report-type=delivery-status 作用域的解析结果。"""

    scope_node_path: str  # multipart/report 容器（作用域）节点路径
    report_node_path: str  # 直接 message/delivery-status 子件节点路径
    envelope_id: Optional[str] = None  # 每邮件块 Original-Envelope-Id
    recipients: list[DeliveryRecipient]


class DeliveryReportListResponse(BaseModel):
    id: int
    fingerprint: str
    reports: list[DeliveryReport]


# ---------------------------------------------------------------------------
# 两封邮件 MIME 树比较（POST /messages/compare）
# ---------------------------------------------------------------------------


class CompareRequest(BaseModel):
    """比较请求：两个已归档邮件 id（必须存在且互不相同）。"""

    a: int = Field(..., ge=1, description="第一封邮件的归档 id")
    b: int = Field(..., ge=1, description="第二封邮件的归档 id")


class CompareRef(BaseModel):
    """比较侧的邮件标识。"""

    id: int
    fingerprint: str


class CompareFacetChange(BaseModel):
    """一对配对节点上单个规范化侧面的差异。"""

    facet: str  # content_type | disposition | headers | leaf
    a: object = None
    b: object = None


class CompareDiff(BaseModel):
    """一条节点差异。

    * ``changed``：双方各一个节点（``a_path`` / ``b_path``），
      ``facets`` 给出有差异的侧面；
    * ``added`` / ``removed``：只在一侧出现的子树根，``node_count``
      为该子树节点总数；
    * ``ambiguous``：同层同内容指纹但任一侧出现多次、无法唯一配对，
      ``a_paths`` / ``b_paths`` 列出双方全部候选路径。
    """

    kind: str  # added | removed | changed | ambiguous
    a_path: Optional[str] = None
    b_path: Optional[str] = None
    a_paths: list[str] = []
    b_paths: list[str] = []
    facets: list[CompareFacetChange] = []
    node_count: Optional[int] = None  # added / removed 子树的节点总数
    fingerprint: Optional[str] = None  # ambiguous 组的重复内容指纹
    content_type: Optional[str] = None


class CompareSummary(BaseModel):
    """各类差异条数统计（ambiguous 按条目数计，不按候选数展开）。"""

    added: int
    removed: int
    changed: int
    ambiguous: int


class CompareResponse(BaseModel):
    a: CompareRef
    b: CompareRef
    identical: bool  # 规范化后整封邮件等价时为 true（diffs 为空）
    summary: CompareSummary
    diffs: list[CompareDiff]


# ---------------------------------------------------------------------------
# 投递路径时钟审计（GET /messages/{id}/routing-audit，Received 头）
# ---------------------------------------------------------------------------


class RoutingHop(BaseModel):
    """一个 Received 头重建出的投递跳（hop 顺序为自底向上/传输方向）。"""

    index: int  # 自底向上 hop 序号，0 基（最早一跳为 0）
    header_index: int  # 原始 Received 头物理顺序序号，0 基（最新一跳为 0）
    host: Optional[str] = None  # by 主机规范化值（小写、剥地址括号/根点）
    host_raw: Optional[str] = None  # by 后的原始 token
    from_host: Optional[str] = None  # from 后的原始 token（缺失为 null）
    protocol: Optional[str] = None  # with 协议（规范化小写，如 esmtps）
    time: Optional[str] = None  # 头中时区的 ISO 8601 时间；不可解析为 null
    time_raw: Optional[str] = None  # 最后一个 ';' 之后的原始 date-time 文本
    raw: str  # 去折叠后的完整 Received 头值（诊断用）


class RoutingAnomaly(BaseModel):
    """一条投递路径异常（按 hop 顺序与类型固定优先级稳定排序）。

    * ``unparseable_header``：缺 by 主机或缺/损坏 date-time，
      ``detail.reasons`` 为 missing_by / missing_date / invalid_date；
    * ``time_reversal``：相邻 hop 时间倒退，detail 给双方时间与负秒差；
    * ``host_loop``：by 主机在更早 hop 已出现，detail 给首次位置。
    """

    type: str  # unparseable_header | time_reversal | host_loop
    hop_index: int
    header_index: int
    detail: dict = {}


class RoutingFirstAnomaly(BaseModel):
    """首个异常的位置（hop 与原始头双重序号 + 类型）。"""

    hop_index: int
    header_index: int
    type: str


class RoutingAuditResponse(BaseModel):
    id: int
    fingerprint: str
    hop_count: int
    hops: list[RoutingHop]
    anomalies: list[RoutingAnomaly]
    first_anomaly: Optional[RoutingFirstAnomaly] = None


# ---------------------------------------------------------------------------
# 日历事件汇总（GET /calendar/events，RFC 5545 iCalendar）
# ---------------------------------------------------------------------------


class CalendarEventSource(BaseModel):
    """事件当前版本的归档来源（CANCEL 取消也保留来源）。"""

    message_id: int
    node_path: str


class CalendarEvent(BaseModel):
    """跨邮件按 SEQUENCE / DTSTAMP 选出的单个事件当前版本。"""

    uid: str
    sequence: int  # 缺省 0
    method: Optional[str] = None  # 大写日历 METHOD：REQUEST / CANCEL 等；无则 null
    status: str  # confirmed | cancelled（METHOD:CANCEL）
    dtstamp: str  # UTC ISO 8601（Z）
    dtstart: str  # UTC ISO 8601（Z）
    dtend: str  # UTC ISO 8601（Z）
    summary: Optional[str] = None
    source: CalendarEventSource


class CalendarDiagnostic(BaseModel):
    """逐项诊断：无效时间、夏令时歧义、缺字段或节点内重复 UID。"""

    message_id: int
    node_path: str
    code: str
    detail: dict = {}


class CalendarEventsResponse(BaseModel):
    events: list[CalendarEvent]  # 按 (UTC 起始时间, UID) 稳定排序
    diagnostics: list[CalendarDiagnostic]


# ---------------------------------------------------------------------------
# 批量撤销归档（POST /messages/unarchive）
# ---------------------------------------------------------------------------


class UnarchiveItemRequest(BaseModel):
    """单个撤销目标：归档 id 与该邮件的**当前**指纹（CAS 凭据）。"""

    id: int = Field(..., ge=1, description="目标邮件的归档 id")
    fingerprint: str = Field(
        ..., min_length=1, description="目标邮件当前的 SHA-256 指纹"
    )


class UnarchiveRequest(BaseModel):
    """批量撤销归档请求；``preview=true`` 只算影响不落库。"""

    messages: list[UnarchiveItemRequest] = Field(
        ..., description="一个或多个 {id, fingerprint} 目标"
    )
    preview: bool = Field(
        False, description="true 时仅预览影响（不删除任何数据）"
    )


class UnarchiveTarget(BaseModel):
    """撤销目标邮件及其将被移除的附件引用数（按 id 升序）。"""

    id: int
    fingerprint: str
    content_type: str
    size_bytes: int
    created_at: str
    attachment_refs_removed: int  # 该邮件在 attachment_refs 中的出现行数


class UnarchiveOrphanedAttachment(BaseModel):
    """本批删除后不再被任何邮件引用、将被（或预览中将被）清理的附件。"""

    sha256: str
    size_bytes: int


class UnarchiveResponse(BaseModel):
    executed: bool  # false=仅预览；true=已在单事务内执行
    target_count: int
    attachment_refs_removed: int  # 本批移除的附件引用（出现次数）合计
    orphaned_attachment_count: int  # 将被清理的去重附件实体数
    reclaimed_bytes: int  # 无人引用附件的字节合计
    targets: list[UnarchiveTarget]  # 按 id 升序
    orphaned_attachments: list[UnarchiveOrphanedAttachment]  # 按 sha256 升序


class ErrorBody(BaseModel):
    code: str
    message: str
    node_path: str
    detail: dict


class ErrorResponse(BaseModel):
    error: ErrorBody
