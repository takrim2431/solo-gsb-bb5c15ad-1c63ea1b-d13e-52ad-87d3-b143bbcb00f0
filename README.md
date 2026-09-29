# MIME 邮件归档指纹 API

接收**原始 RFC 5322 邮件**字节，递归解析 MIME 树（multipart 边界、
Content-Transfer-Encoding、字符集、RFC 2047 编码字、RFC 2231 参数），
按**规范化头字段**与**解码后的叶子内容**生成稳定 SHA-256 指纹，
并把可查询的 MIME 树存入 SQLite。

- 同一封邮件的不同换行风格（CRLF / LF / 裸 CR）、不同头字段大小写、
  不同 multipart `boundary`、不同 Content-Transfer-Encoding
  （8bit / base64 / quoted-printable）或等价字符集标注，
  **得到完全相同的指纹**；
- 损坏的边界、重复参数、无法解码的字符集 / base64 / quoted-printable
  等情况返回 **HTTP 422**，错误体带 **`node_path` 精确定位到 MIME 节点**，
  且**不会写入任何半成品数据**；
- 相同指纹重复提交是幂等的（返回同一 id，`created=false`）；
- 所有非文本叶子的解码字节进入**内容寻址附件库**（按 SHA-256 去重），
  记录每次出现的 `message_id` / `node_path` / 解码文件名 / disposition，
  支持分页检索与带 `Range` 的下载。

技术栈：FastAPI + SQLite（标准库 `sqlite3`，无 ORM），无第三方 MIME
解析库——解析器按 RFC 严格手工实现，刻意不使用容错的 Python `email` 包。

## 目录结构

```
app/
  main.py        FastAPI 路由与启动初始化
  mimeparser.py  严格 MIME/RFC 5322 解析、传输解码、规范化与指纹
  related.py     cid: 内嵌资源引用审计（RFC 2392，multipart/related 作用域）
  displaybody.py 展示正文选择（alternative/related/mixed 递归规则，纯只读）
  security.py    附件安全策略评估（双扩展/控制字符/禁止类型/嵌套/阈值，纯只读）
  delivery.py    投递状态报告解析（RFC 3464 DSN / RFC 6522 multipart/report，纯只读）
  routingaudit.py 投递路径时钟审计（Received 头自底向上重建 hop、时间倒退/主机回环/坏头诊断，纯只读）
  threading.py   会话关联构建（Message-ID/References 回复图、断链、拓扑排序，纯只读）
  calendar.py    日历事件汇总（text/calendar 单次 VEVENT 解析、TZID 折 UTC、夏令时诊断、跨邮件版本选择，纯只读）
  compare.py     两封邮件 MIME 树递归对齐比较（内容指纹配对、重排识别、歧义标记，纯只读）
  archive.py     整邮件附件 ZIP 打包（确定性条目/压缩、名称冲突改名、manifest.json，纯只读）
  unarchive.py   批量撤销归档：{id, 当前指纹} 凭据校验与预览/执行共用的影响计算（纯只读计算；预览在单一只读快照事务内执行，删除在 database 单事务内）
  database.py    SQLite 建表、事务化写入、消息/节点/附件查询与单事务级联删除
  errors.py      定位到节点的解析错误
  schemas.py     请求/响应模型
tests/           438 个 pytest 用例（指纹稳定性、损坏输入、附件库、
                 整邮件 ZIP、cid 引用审计、展示正文选择、安全策略评估、
                 DSN 投递状态、投递路径时钟审计、会话关联、日历事件汇总、
                 MIME 树比较、批量撤销归档、HTTP 端到端）
Dockerfile
docker-compose.yml
requirements.txt
```

## 快速开始（Docker）

```bash
docker compose up --build
```

一条命令完成构建与启动；服务监听 **http://localhost:8000**
（容器内监听 8000，compose 已映射到宿主机同名端口），SQLite 数据文件
位于容器内 `/data/mime.db`（命名卷 `mime-data` 持久化）。启动时自动
建库建表，无需任何额外初始化。

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok","database":"/data/mime.db"}
```

接口根地址：**http://localhost:8000**（交互文档
http://localhost:8000/docs ）。

## 本地运行（不用 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export MIME_DB_PATH=./data/mime.db   # 可选，默认 /data/mime.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 提交一封邮件

请求体就是邮件原始字节（建议 `Content-Type: message/rfc822`，
但服务端不依赖该头）：

```bash
curl -X POST http://localhost:8000/messages \
     -H 'Content-Type: message/rfc822' \
     --data-binary @message.eml
```

成功响应 `201`：

```json
{
  "id": 1,
  "fingerprint": "58d92498…431",
  "raw_sha256": "97d9604f…6fb",
  "content_type": "multipart/mixed",
  "size_bytes": 380,
  "created": true,
  "node_count": 3
}
```

再次提交语义相同但字节不同（换行、头大小写、boundary、CTE 不同）的邮件，
返回同一 `id` 与 `fingerprint`，`created` 为 `false`。

## 批量撤销归档（按 id + 当前指纹）

```bash
# 预览（不删除任何数据）
curl -X POST http://localhost:8000/messages/unarchive \
     -H 'Content-Type: application/json' \
     -d '{"preview": true, "messages": [
            {"id": 1, "fingerprint": "58d92498…431"},
            {"id": 2, "fingerprint": "78d5be53…d01b1"}]}'

# 执行（省略 preview 或 preview=false）
curl -X POST http://localhost:8000/messages/unarchive \
     -H 'Content-Type: application/json' \
     -d '{"messages": [
            {"id": 1, "fingerprint": "58d92498…431"},
            {"id": 2, "fingerprint": "78d5be53…d01b1"}]}'
```

预览与执行之间数据可能变化（见下文[乐观并发用法](#预览与执行之间数据可能变化乐观并发用法)）：
执行始终在事务内对当前数据重新校验，推荐“预览 → 确认 → 原样执行”，
收到 404/409 时先取回当前状态再重新预览，不要凭旧预览反复重试：

```bash
# 409 响应给出库中当前指纹，可据此重新预览后再执行
curl -s -X POST http://localhost:8000/messages/unarchive \
     -H 'Content-Type: application/json' \
     -d '{"messages": [{"id": 1, "fingerprint": "58d92498…old"}]}'
# {"detail":{"code":"UNARCHIVE_FINGERPRINT_MISMATCH","id":1,
#   "provided_fingerprint":"58d92498…old",
#   "current_fingerprint":"9f31aa02…new"}}
```


归档管理员按**邮件 id 与该邮件的当前指纹**批量撤销归档。指纹是乐观并发
凭据：请求时携带的指纹必须与库中**当前**指纹逐字符一致，防止撤错版本。
以下情况**拒绝整批、数据不变**：

| 状态码 | 错误码 `detail.code` | 触发条件 |
| ---- | ---- | ---- |
| **422** | `UNARCHIVE_EMPTY_BATCH` | `messages` 为空列表（至少需要一个目标） |
| **422** | `UNARCHIVE_DUPLICATE_ID` | 请求中同一 id 出现多次（`detail.id` / `detail.index` 定位首个重复项） |
| **404** | `UNARCHIVE_MESSAGE_NOT_FOUND` | 任一 id 不存在或已被撤销（`detail.id`，按 id 升序报告首个缺失项） |
| **409** | `UNARCHIVE_FINGERPRINT_MISMATCH` | 任一指纹与当前归档指纹不符（`detail.id` / `detail.provided_fingerprint` / `detail.current_fingerprint`） |

请求体不合规（缺 `messages`、`id` 非正整数、`fingerprint` 为空）返回
FastAPI 参数校验 **422**。

**预览与执行使用同一影响计算**（`preview=true` 为纯只读，不落库）。
预览的指纹校验、目标信息、各目标附件引用数、孤儿附件集合与可回收字节
在**同一只读数据库快照**（单一 `BEGIN` 读事务）内取得：即使预览进行中
另一请求撤销了目标，本次成功结果仍整体按快照建立时的版本解释，不会
出现“目标信息来自删除前、附件统计来自删除后”的失真回收估算；目标在
**快照建立前**已撤销仍按 404 拒绝整批，指纹不符仍按 409 处理。快照为
只读延迟事务（共享读锁、无任何写入），不阻塞其他请求并发归档 / 撤销。
响应：

- `executed`：是否实际执行（预览 `false`，执行 `true`）；
- `targets`：目标邮件**按 id 升序**，每项含 `id` / `fingerprint` /
  `content_type` / `size_bytes` / `created_at` 与
  **`attachment_refs_removed`**（该邮件将移除的附件引用行数，即 blob
  出现次数，同一内容出现多次按多行计）；
- `orphaned_attachments`：**仅在本批删除后才无人引用**的附件，按
  SHA-256 升序，给出 `sha256` 与 `size_bytes`；仍被批外其他邮件引用
  的附件**不出现**；
- `attachment_refs_removed`：本批移除的引用行数合计；
  `orphaned_attachment_count`：将清理的去重附件实体数；
  `reclaimed_bytes`：无人引用附件的字节合计（同一内容无论被引用多少次
  只计一份字节）。

```json
{
  "executed": false,
  "target_count": 2,
  "attachment_refs_removed": 3,
  "orphaned_attachment_count": 1,
  "reclaimed_bytes": 40,
  "targets": [
    {"id": 1, "fingerprint": "58d92498…431", "content_type": "multipart/mixed",
     "size_bytes": 380, "created_at": "2026-09-21T03:06:15.676328+00:00",
     "attachment_refs_removed": 2},
    {"id": 2, "fingerprint": "78d5be53…d01b1", "content_type": "multipart/mixed",
     "size_bytes": 291, "created_at": "2026-09-21T03:07:01.112233+00:00",
     "attachment_refs_removed": 1}
  ],
  "orphaned_attachments": [
    {"sha256": "c1e372ae…8001", "size_bytes": 40}
  ]
}
```

**执行语义**：在单个 `BEGIN IMMEDIATE` 事务内重新校验全部凭据后，
依次删除目标邮件的附件引用 → **仅**无人引用的附件实体（删除语句再以
`NOT EXISTS` 兜底，绝不删除仍被引用的字节）→ 邮件的全部 MIME 节点 →
邮件本身；影响行数与预期不符或任何步骤失败均**整体回滚**，数据库回到
撤销前状态。撤销后：

- 目标邮件及其节点、附件引用不可再查（`/messages/{id}` 返回 404）；
- 仅被目标邮件引用的附件实体与字节一并清理，`/attachments` 不再列出；
- 仍被**其他邮件**引用的附件字节完整保留，`/attachments` 可查、
  `/attachments/{sha256}/content` 可下载（含 `Range`），其
  `occurrences` / `reference_count` 只反映剩余引用；
- 撤销后重新归档同一封邮件会作为新行入库（指纹相同但 id 不同，
  `created=true`）。

### 预览与执行之间数据可能变化（乐观并发用法）

预览快照**只保证这一次预览内部自洽**，并不锁定数据：预览返回后、执行
提交前，其他管理员（或自动化任务）仍可能归档新邮件、撤销同一批目标或
撤销共享附件的其他引用邮件。因此：

- **不要**把预览的 `targets` / `reclaimed_bytes` 当作执行时的承诺，
  也**不要**缓存预览结果后隔很久再执行；典型用法是“先预览给操作员
  确认，随即用相同的 `{id, fingerprint}` 凭据执行”；
- 执行请求在单个 `BEGIN IMMEDIATE` 事务内**对当前数据重新校验指纹并
  重新计算影响**，成功响应按执行时的最新数据给出：预览后目标已被其他
  请求撤销 → 执行返回 **404**；目标被重新归档为其他版本 → 执行返回
  **409**（响应 `current_fingerprint` 为最新指纹，可取回后重新预览）；
  共享附件的其他引用在期间消失 → 执行的 `orphaned_attachments` /
  `reclaimed_bytes` 会比预览时更多，反之亦然，均属正常；
- 典型调用序列：`preview=true` 展示 → 操作员确认 → 原样提交执行 →
  收到 404/409 时刷新当前状态并重新预览，不要凭旧预览重试。

## 查询

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/messages` | 列表（`limit`、`offset` 分页） |
| POST | `/messages/unarchive` | **按 id + 当前指纹批量撤销归档**（可预览，见下） |
| GET | `/messages/{id}` | 邮件元信息 |
| GET | `/messages/{id}/tree` | **嵌套 MIME 树** |
| GET | `/messages/{id}/nodes` | 全部节点（扁平，children 为路径） |
| GET | `/messages/{id}/nodes/{path}` | 单个节点详情（头字段、文本/二进制哈希） |
| GET | `/messages/{id}/related-resources` | 审计 HTML 对 `cid:` 内嵌资源的引用（见下） |
| GET | `/messages/{id}/display-body?prefer=html\|plain` | 选择客户端应展示的正文（见下） |
| GET | `/messages/{id}/security-report` | 只读附件安全策略评估（见下，阈值可查询参数覆盖） |
| GET | `/messages/{id}/delivery-report` | 解析 RFC 3464 投递状态报告（DSN，见下） |
| GET | `/messages/{id}/routing-audit` | 从 `Received` 头重建投递路径并审计时钟倒退/回环/坏头（见下） |
| GET | `/messages/{id}/thread` | 会话关联查询：回复图、断链与拓扑排序（见下） |
| GET | `/messages/{id}/attachments.zip` | **整邮件附件 ZIP**：文档序逐 blob 出现打包 + 内置 manifest（见下） |
| POST | `/messages/compare` | 只读比较两封已归档邮件的 MIME 树（见下） |
| GET | `/calendar/events` | **跨全部归档邮件**汇总 `text/calendar` 单次 VEVENT（见下） |
| GET | `/attachments` | 附件库列表（分页 + `sha256`/`filename`/`media_type` 过滤） |
| GET | `/attachments/{sha256}/content` | 下载附件原始解码字节（支持单段 `bytes` Range） |

节点路径规则：根节点为 `""`，子节点为 1 基序号 `1`、`2`，
深层为 `1/2` 这样的物化路径。根节点详情用末尾斜杠：

```bash
curl http://localhost:8000/messages/1/tree
curl http://localhost:8000/messages/1/nodes/        # 根节点
curl http://localhost:8000/messages/1/nodes/2       # 第 2 部分
curl http://localhost:8000/messages/1/nodes/2/1     # 第 2 部分里的封装邮件
```

文本叶子的 `text` 是按声明字符集解码、NFC 规范化、换行统一为 `\n`
后的内容；非文本叶子不回传字节，只给 `blob_sha256` 与 `blob_length`。

## `cid:` 内嵌资源引用审计

```bash
curl http://localhost:8000/messages/1/related-resources
```

审计归档邮件 HTML 对 `multipart/related`（RFC 2387）内
`Content-ID` 资源的 `cid:` 引用：

- 从每个 **text/html 叶子** 的 `src`、`href`、`poster`、`srcset`
  属性（按 HTML 文档序，`srcset` 支持多个候选）提取 `cid:` 引用，
  按 **RFC 2392 §2 百分号解码**（如 `cid:logo%40x.com` →
  `logo@x.com`），`cid:` scheme 大小写不敏感；
- 作用域严格限定为该 HTML 叶子 **最近的 `multipart/related`
  祖先**：只在该祖先的后代中匹配 `Content-ID`，**不向相邻兄弟
  作用域、也不向内层嵌套作用域串联**（`multipart/mixed` 等其他
  容器对作用域判定透明）；
- 每条引用给出源 `source_node_path`、状态与目标：
  - `resolved` —— 作用域内唯一命中，附 `target_node_path`；
  - `missing` —— 作用域内无对应 `Content-ID`（HTML 不在任何
    related 作用域时同样判 missing，`scope_node_path` 为 `null`）；
  - `ambiguous` —— **同作用域内重复 Content-ID**，附全部
    `candidates` 节点路径（跨不同作用域的同名 ID 合法，不算歧义）；
- `scopes` 按文档序列出每个 `multipart/related` 作用域中未被本作用域
  HTML 引用的 `Content-ID` 节点（`orphans`）；
- `Content-ID` 头规范化时剥掉 RFC 2045 的一层尖括号（`"<foo>"` →
  `foo`，裸 ID 按字面接受），`cid:<foo>` 与
  `cid:%3Cfoo%3E` 也做对称容忍；
- 邮件不存在返回 **404**。

响应示例：

```json
{
  "id": 1,
  "fingerprint": "defeef89…299c40",
  "references": [
    {
      "source_node_path": "1",
      "attribute": "src",
      "cid": "logo@x.com",
      "scope_node_path": "",
      "status": "resolved",
      "target_node_path": "2",
      "candidates": []
    },
    {
      "source_node_path": "1",
      "attribute": "src",
      "cid": "missing@x.com",
      "scope_node_path": "",
      "status": "missing",
      "target_node_path": null,
      "candidates": []
    }
  ],
  "scopes": [
    {
      "node_path": "",
      "orphans": [
        {
          "node_path": "3",
          "content_type": "image/gif",
          "content_ids": ["orphan@x.com"]
        }
      ]
    }
  ]
}
```

`ambiguous` 时 `target_node_path` 为 `null`，`candidates` 列出同作用域
所有同 ID 节点；歧义候选视为“已被引用”，不会再出现在 `orphans` 中。

## 展示正文选择

```bash
curl 'http://localhost:8000/messages/1/display-body?prefer=html'
curl 'http://localhost:8000/messages/1/display-body?prefer=plain'
```

在**已归档 MIME 树**上递归选出客户端应展示的正文。该接口是**纯只读
查询**：只读取归档时保存的节点与头字段，不改写任何记录，因此对历史
归档数据同样有效（无需重新归档）。

递归选择规则（沿实际选中路径逐层进行）：

- **`multipart/alternative`**：在可展示分支中，优先选择其*实际正文*
  匹配 `prefer` 的分支——`prefer=html` 偏好 `text/html`、
  `prefer=plain` 偏好 `text/plain`，多个命中取文档序第一个；没有任何
  分支能给出偏好类型时，选择**最后一个可展示分支**（RFC 2046：备选按
  保真度从低到高排列，末位最忠实）；
- **`multipart/related`**：按容器 Content-Type 上的 **RFC 2387
  `start` 参数**匹配**直接子件**的 `Content-ID`（`start` 支持
  RFC 2231 百分号编码，尖括号做与 `cid:` 审计一致的一层容忍）；
  **未配置 `start` 时取首个子件**（RFC 2387 的缺省根部分）；
- **`multipart/mixed` 及其他 multipart 子类型**（如
  `multipart/signed` / `multipart/parallel`）：按文档序取
  **首个可展示子件**。

“可展示”指递归到一个 **text 叶子**（`kind == "text"`，含
`text/enriched` 等其他 text/*）。以下内容**不作为候选**：

- 任何 `Content-Disposition: attachment` 节点（及其整棵子树；
  `inline` 不受影响）；
- **`message/rfc822` / `message/global` 封装报文的内部正文**——封装
  容器是不向内穿透的屏障；
- 非文本叶子（内嵌图片等 `blob` 节点）。

空的 text 叶子仍是可展示正文（空正文也是正文）。`start` / 首个子件是
related 的**权威**选择：目标不可展示时不静默改选其他兄弟。未被选中的
alternative 分支即使内部含 `start` 配置错误也不阻断其他分支；当全部
分支都不可展示时，唯一路径上的配置错误会照常返回。

响应（命中时 `200`）给出最终叶子的 `node_path`、`content_type`、
`text`，以及逐层选择轨迹 `trail`：

```json
{
  "id": 1,
  "fingerprint": "defeef89…299c40",
  "prefer": "html",
  "node_path": "2/2/1",
  "content_type": "text/html",
  "text": "<p>正文</p>",
  "trail": [
    {
      "node_path": "",
      "content_type": "multipart/mixed",
      "rule": "first-displayable",
      "considered": ["1", "2"],
      "candidates": ["2"],
      "chosen": "2",
      "detail": {}
    },
    {
      "node_path": "2",
      "content_type": "multipart/alternative",
      "rule": "alternative-prefer",
      "considered": ["2/1", "2/2"],
      "candidates": ["2/1", "2/2"],
      "chosen": "2/2",
      "detail": {"prefer": "html"}
    },
    {
      "node_path": "2/2",
      "content_type": "multipart/related",
      "rule": "related-start",
      "considered": ["2/2/1", "2/2/2"],
      "candidates": ["2/2/1"],
      "chosen": "2/2/1",
      "detail": {"start": "body@x.com"}
    }
  ]
}
```

轨迹中的 `rule` 取值：

| `rule` | 含义 |
| ---- | ---- |
| `alternative-prefer` | alternative 中按 `prefer` 命中实际正文匹配的分支 |
| `alternative-fallback-last` | alternative 无偏好命中，回退最后一个可展示分支 |
| `related-start` | related 按 `start` 参数匹配 Content-ID（`detail.start`） |
| `related-first` | related 未配置 `start`，取首个子件 |
| `first-displayable` | mixed 等容器取首个可展示子件 |

每项的 `considered` 是该层实际评估过的直接子件、`candidates` 是规则下
合格的候选、`chosen` 是实际进入的子件。

异常情况：

- 邮件不存在：**404**；邮件存在但整棵树没有任何可展示正文（全是
  attachment / 封装报文 / 二进制叶子）：**404**；
- `prefer` 取值不是 `html` / `plain`：FastAPI 参数校验 **422**；
- `start` 配置错误统一为带节点定位的 **422**，`node_path` 指向出问题
  的 **multipart/related 容器**：

  | 错误码 `code` | 触发条件 |
  | ---- | ---- |
  | `AMBIGUOUS_RELATED_START` | `start` 在直接子件中重复命中 Content-ID，`detail.candidates` 给出全部命中子件路径，`detail.start` 为规范化后的 ID |
  | `RELATED_START_NOT_FOUND` | 配置了 `start` 但没有任何直接子件声明该 Content-ID（只匹配直接子件，不向内层后代串联），`detail.start` 为该 ID |

## 附件安全策略评估

```bash
curl http://localhost:8000/messages/1/security-report
curl 'http://localhost:8000/messages/1/security-report?max_decoded_bytes=1048576&max_attachment_count=10&max_mime_depth=5'
```

在**已归档 MIME 树**上执行确定性的附件安全策略评估。该接口是**纯只读
查询**：只读取归档时保存的节点行（与内容寻址附件库口径一致），不改写
任何记录，对历史归档数据同样有效；评估结果只由节点行与显式阈值决定，
**同一归档重复请求结果稳定**。

按节点物化路径做文档序（先根后子、兄弟按 1..n）遍历，递归累计
`totals`：

- `decoded_bytes` —— 全部**非文本叶子**（`kind == "blob"`，即附件
  节点；`text/*` 叶子不计）的**解码后字节**合计（`blob_length`
  之和，同一内容出现多次按多次累计）；
- `attachment_count` —— 非文本叶子个数；
- `max_depth` —— MIME 树最大深度（根为 0，封装报文内部节点照常计入）。

命中的每条规则在 `violations` 中给出 `code` / `node_path` /
`detail`，并使 `allow=false`；无任何命中时 `allow=true`、
`violations=[]`。规则：

| `code` | 命中条件 | `detail` |
| ---- | ---- | ---- |
| `DOUBLE_EXTENSION` | 附件文件名 basename 含 **两个及以上非空扩展名段**（如 `invoice.pdf.exe`；`archive.tar.gz` 同样命中，`.bashrc` / 无扩展名不命中） | `filename`、`extensions` |
| `FILENAME_CONTROL_CHARS` | 文件名含 **Unicode 控制字符**（`Cc` 类：C0 含 NUL/TAB/换行、DEL、C1） | `filename`、去重后按序的 `codepoints`（如 `["U+0009"]`） |
| `FORBIDDEN_MEDIA_TYPE` | 附件 Content-Type 命中内置可执行类型黑名单（`application/x-msdownload`、`application/x-msi`、`application/java-archive` 等，大小写无关精确匹配） | `content_type`（小写） |
| `NESTED_MESSAGE` | 深度 ≥ 1 处出现 `message/rfc822` / `message/global` 封装容器（**根自身是封装报文不算嵌套**） | `content_type` |
| `DECODED_BYTES_EXCEEDED` | 累计解码字节**严格大于** `max_decoded_bytes`；只报文档序第一个把累计推过阈值的附件节点 | `limit`、`decoded_bytes`、`node_bytes` |
| `ATTACHMENT_COUNT_EXCEEDED` | 附件数**严格大于** `max_attachment_count`；只报第一个越界附件节点 | `limit`、`count` |
| `MIME_DEPTH_EXCEEDED` | 节点深度**严格大于** `max_mime_depth`；只报第一个越界节点 | `limit`、`depth` |

文件名取节点 `Content-Disposition` 的 `filename`（含 RFC 2231
`filename*=`），缺省时回退 `Content-Type` 的 `name`；两者都没有的
附件节点不做文件名类检查（仍计入 `totals` 与媒体类型检查）。阈值为
“严格大于”语义——恰好等于阈值不命中。

阈值可通过查询参数覆盖默认值；非整数或负数返回 **422**：

| 查询参数 | 默认值 |
| ---- | ---- |
| `max_decoded_bytes` | `10485760`（10 MiB） |
| `max_attachment_count` | `100` |
| `max_mime_depth` | `20` |

响应示例（一封带双扩展可执行附件的邮件）：

```json
{
  "id": 1,
  "fingerprint": "78d5be53…d01b1",
  "allow": false,
  "totals": {
    "decoded_bytes": 10,
    "attachment_count": 1,
    "max_depth": 1
  },
  "thresholds": {
    "max_decoded_bytes": 10485760,
    "max_attachment_count": 100,
    "max_mime_depth": 20
  },
  "violations": [
    {
      "code": "FORBIDDEN_MEDIA_TYPE",
      "node_path": "2",
      "detail": {"content_type": "application/x-msdownload"}
    },
    {
      "code": "DOUBLE_EXTENSION",
      "node_path": "2",
      "detail": {"filename": "invoice.pdf.exe", "extensions": ["pdf", "exe"]}
    }
  ]
}
```

无风险邮件返回 `"allow": true` 与空 `violations`。邮件不存在返回
**404**；阈值参数非法（负数、非整数）返回 **422**。

## 投递状态报告（DSN）

```bash
curl http://localhost:8000/messages/1/delivery-report
```

在**已归档 MIME 树**上只读解析投递状态通知（Delivery Status
Notification，RFC 3464 / RFC 6522）。该接口是**纯只读查询**：只读取
归档时保存的节点行与附件库中的状态部分解码字节，不改写任何记录，对
历史归档数据同样有效。

**作用域识别**：遍历整棵 MIME 树（含 `message/rfc822` /
`message/global` 封装报文内部），找出所有
`Content-Type: multipart/report; report-type="delivery-status"`
容器作为报告作用域（`report-type` 大小写不敏感）；其他
`report-type`（如 `disposition-notification` 的 MDN）或缺该参数的
`multipart/report` 一律忽略。每个作用域必须有**恰好一个直接**
`message/delivery-status` 子件——子件状态部分位于孙代、零个或多个
均为结构错误。

**正文解析**：`message/delivery-status` 正文按空行划分字段块——
第 1 块为**每邮件块**（从中取 `Original-Envelope-Id` 作为
`envelope_id`，无则为 `null`），其余每块为一个**逐收件人块**：

- 换行兼容 **CRLF / LF / 裸 CR**；以空白开头的物理行按 RFC 5322
  **折叠规则**并入上一逻辑行（连续空行与首尾空行容忍）；
- 每个逐收件人块必须含三个必填字段，否则 **422**：
  - `Final-Recipient`：形如 `addr-type; address`，地址类型规范化为
    小写（如 `RFC822` → `rfc822`），地址去首尾空白后保留原始大小写；
  - `Action`：`failed` / `delayed` / `delivered` / `relayed` /
    `expanded` 之一（大小写不敏感，输出规范化为小写）；
  - `Status`：RFC 3463 增强状态码，**三段数字**、每段 1..3 位
    （如 `5.1.1`、`4.4.1`）；
- 保留可选字段 `Diagnostic-Code`、`Remote-MTA`、
  `Last-Attempt-Date` 的去折叠原始值（缺失为 `null`）；每块内字段
  重复、字段行无冒号、块首出现折叠续行同样是 **422**。

响应按 **MIME 文档序**给出作用域节点、报告节点、信封标识与标准化
收件人字段：

```json
{
  "id": 1,
  "fingerprint": "78d5be53…d01b1",
  "reports": [
    {
      "scope_node_path": "",
      "report_node_path": "2",
      "envelope_id": "envelope-12345",
      "recipients": [
        {
          "final_recipient_type": "rfc822",
          "final_recipient_address": "bob@example.com",
          "action": "failed",
          "status": "5.1.1",
          "diagnostic_code": "smtp; 550 5.1.1 No such user",
          "remote_mta": "dns; mx.example.com",
          "last_attempt_date": "Mon, 22 Sep 2026 11:00:00 +0000"
        },
        {
          "final_recipient_type": "rfc822",
          "final_recipient_address": "Carol@Example.COM",
          "action": "delayed",
          "status": "4.4.1",
          "diagnostic_code": "smtp; 421 try later",
          "remote_mta": null,
          "last_attempt_date": null
        }
      ]
    }
  ]
}
```

异常情况：

- 邮件不存在：**404**；邮件中没有任何 DSN 作用域（普通邮件、MDN
  等）：`200` 且 `"reports": []`；
- 结构 / 必填字段非法：**422**，错误体带 `node_path`——作用域级
  问题（多个/零个直接状态子件）定位到 `multipart/report` 节点，
  正文级问题（字段块/字段损坏、必填字段缺失或值非法）定位到
  `message/delivery-status` 节点；多个作用域时按文档序报告第一个
  出错的作用域：

| 错误码 `code` | 触发条件 |
| ---- | ---- |
| `MULTIPLE_DELIVERY_STATUS` | 同一作用域的直接子件中出现多个 `message/delivery-status`（`detail.candidates` 给出全部子件路径） |
| `DELIVERY_STATUS_NOT_FOUND` | DSN 作用域没有直接 `message/delivery-status` 子件 |
| `DELIVERY_STATUS_UNREADABLE` | 状态部分解码字节缺失或无法按声明字符集解码（如归档于附件库升级之前） |
| `INVALID_DELIVERY_STATUS` | 正文为空、缺少逐收件人块、字段行无冒号、块内字段重复、块首折叠续行 |
| `MISSING_DELIVERY_FIELD` | 逐收件人块缺少非空 `Final-Recipient` / `Action` / `Status`（`detail.field`） |
| `INVALID_DELIVERY_FIELD` | `Final-Recipient` 不是 `type; address`、`Action` 枚举非法或 `Status` 非三段数字（`detail.field` / `detail.value`） |

## 投递路径时钟审计

```bash
curl http://localhost:8000/messages/1/routing-audit
```

在**根邮件**（path 为 `""` 的节点；`message/rfc822` 封装报文内部的
`Received` 头不计）上，从全部 `Received` 头（RFC 5321 §4.4）重建投递
路径并做确定性的时钟/路径审计。该接口是**纯只读查询**：只读取归档时
保存的根节点头字段，不改写任何记录，对历史归档数据同样有效；重复请求
结果稳定。

**hop 重建**：邮件每经过一个 MTA，接收方就在报文**顶部**追加
`Received` 头，因此物理顺序为最新在前、最旧在后。接口按邮件传输方向
**自底向上**（物理顺序倒序）形成 hop 列表，每个 hop 给出：

| 字段 | 含义 |
| ---- | ---- |
| `index` | 自底向上 hop 序号，0 基（最早一跳为 0，与传输方向一致） |
| `header_index` | 原始 `Received` 头物理顺序序号，0 基（最新一跳为 0），与 `index` 互为倒序 |
| `host` | `by` 接收主机的**规范化**值：小写、剥地址字面量方括号（`[10.0.0.1]` / `[IPv6:…]`）与 FQDN 末尾根点 |
| `host_raw` | `by` 之后的原始 token |
| `from_host` | `from` 之后的原始 token（该子句可选，缺失为 `null`） |
| `protocol` | `with` 协议（规范化为小写，如 `esmtps`；缺失为 `null`） |
| `time` | date-time 解析结果（ISO 8601，**保留头中声明的时区偏移**；无法解析为 `null`） |
| `time_raw` | 最后一个 `;` 之后的原始 date-time 文本（缺失为 `null`） |
| `raw` | 去折叠后的完整 `Received` 头值（归档口径，诊断用） |

解析时先剥掉 RFC 5321 圆括号注释（`(Postfix)`、`(PDT)`、`([10.0.0.1])`
等，支持嵌套与反斜杠转义），再以最后一个 `;` 切出 date-time；之前的
部分按 `from` / `by` / `via` / `with` / `id` / `for` 关键字切子句。
date-time 兼容 RFC 5322 数字时区（`+0530`）、命名时区
（`GMT` / `EST` / `UTC` 等）与两位年份（obs-year 规则）；时间比较
统一折算 UTC，响应展示仍用头中原始偏移。

**异常检测**（`anomalies`，按 hop 顺序、同 hop 按类型固定优先级稳定
排序；`first_anomaly` 给出首个异常的 `hop_index` / `header_index` /
`type`，无异常为 `null`）：

| `type` | 命中条件 | `detail` |
| ---- | ---- | ---- |
| `time_reversal` | **相邻**两个时间均可解析的 hop，后一 hop（更靠近接收端）时间早于前一 hop（相等不算）；时间不可解析的坏头不参与配对 | `previous`、`current`（各自原始偏移 ISO 时间）、`previous_index`、`delta_seconds`（负整数秒差） |
| `host_loop` | 规范化 `by` 主机在**更早的 hop**（不必相邻）已出现 | `host`、`first_index`、`first_header_index`（首次出现位置） |
| `unparseable_header` | 缺 `by` 主机（`missing_by`）、缺 date-time（`missing_date`）或 date-time 无法解析/日期不存在（`invalid_date`） | `reasons`（可多个）、`date_raw` |

**坏头不拒收、不改写归档数据**：无法解析的 `Received` 头仍作为 hop
保留在列表中（缺失字段为 `null`），同时列入 `anomalies` 诊断——接口
始终返回 **200**，不会因坏头返回 422，归档的头值也保持原样。

响应示例（三跳，最新一跳时间比上一跳早了 4 小时）：

```json
{
  "id": 1,
  "fingerprint": "4d460f48…ddf8",
  "hop_count": 3,
  "hops": [
    {
      "index": 0,
      "header_index": 2,
      "host": "msa.example.net",
      "host_raw": "msa.example.net",
      "from_host": "mail.example.com",
      "protocol": "smtp",
      "time": "2026-09-22T10:00:00-05:00",
      "time_raw": "Mon, 22 Sep 2026 10:00:00 -0500",
      "raw": "from mail.example.com (helo=foo) by msa.example.net with SMTP; Mon, 22 Sep 2026 10:00:00 -0500"
    },
    {
      "index": 1,
      "header_index": 1,
      "host": "mx1.example.com",
      "host_raw": "mx1.example.com",
      "from_host": "msa.example.net",
      "protocol": "esmtp",
      "time": "2026-09-22T11:00:00-05:00",
      "time_raw": "Mon, 22 Sep 2026 11:00:00 -0500",
      "raw": "from msa.example.net by mx1.example.com with ESMTP id XYZ789; Mon, 22 Sep 2026 11:00:00 -0500"
    },
    {
      "index": 2,
      "header_index": 0,
      "host": "mx2.example.com",
      "host_raw": "mx2.example.com",
      "from_host": null,
      "protocol": "esmtps",
      "time": "2026-09-22T12:00:00+00:00",
      "time_raw": "Mon, 22 Sep 2026 12:00:00 +0000",
      "raw": "by mx2.example.com with ESMTPS id ABC123; Mon, 22 Sep 2026 12:00:00 +0000"
    }
  ],
  "anomalies": [
    {
      "type": "time_reversal",
      "hop_index": 2,
      "header_index": 0,
      "detail": {
        "previous": "2026-09-22T11:00:00-05:00",
        "current": "2026-09-22T12:00:00+00:00",
        "previous_index": 1,
        "delta_seconds": -14400
      }
    }
  ],
  "first_anomaly": {
    "hop_index": 2,
    "header_index": 0,
    "type": "time_reversal"
  }
}
```

异常情况：

- 邮件不存在：**404**；
- 邮件没有任何 `Received` 头（纯本地起草等）：**200**，`hop_count=0`、
  `hops=[]`、`anomalies=[]`、`first_anomaly=null`；
- 重复的 `Received` 头逐个保留（不去重），折叠头按归档时的去折叠结果
  解析；封装在 `message/rfc822` / `message/global` 内部的 `Received`
  头不属于根邮件，不计入审计。

## 会话关联查询

```bash
curl http://localhost:8000/messages/2/thread
```

在**全部归档记录**上按 `Message-ID` / `In-Reply-To` / `References`
头构建确定性的回复图，返回该邮件所在的会话。该接口是**纯只读查询**：
只读取归档时保存的根节点头字段，不改写任何记录，对历史归档数据同样
有效；同一归档库重复请求结果逐字节稳定。

**回复图构建**：

- 每封邮件的引用链 = `References` 中的 msg-id（按序）+ `In-Reply-To`
  中未出现过的 msg-id（追加在尾）；msg-id 规范化时剥一层尖括号、
  NFC、小写（大小写不敏感匹配），裸 token（无尖括号）按空白切分容错；
- **父件**取引用链中**最后一个能在归档中解析**的 msg-id（跳过解析到
  自己的自引用，继续向前找）；重复 `Message-ID`（不同指纹的邮件声明
  同一 ID）解析到**最早归档**的那封；
- **断链**（`broken_links`）保留两种可定位的失败：
  - `missing_parent` —— 引用链尾（最直接的父件）在归档中不存在；
    即使该邮件已借由链上更早的祖先入链，断链同样保留；
  - `cycle` —— 循环引用（含自引用）。候选父边成环时，断开环上
    **时间最早**（并列取归档 id 最小）节点的父边——最早的邮件引用
    环内更晚的邮件作为父件是最可疑的引用；重复检测直到无环，回复图
    最终是森林。

**会话分组与排序**：

- 父子边（视为无向）的连通分量各为一个会话；
- 没有任何引用关系的孤立邮件按**规范化主题**分组：循环去除
  `Re:` / `Fwd:` / `Fw:` / `回复：` / `答复：` / `转发：` 等前缀
  （大小写不敏感、半角/全角冒号均可）、压缩空白、casefold；空主题的
  孤立邮件各自成组（不把所有无主题邮件混为一组）；
- 会话内按引用关系**拓扑排序**（父在子前），并列时按（`Date` 头时间
  ——缺失或非法时回退归档时间，归档 id）稳定排序；`Date` 解析支持
  RFC 5322 数字/命名时区并归一为 UTC，两位年份按 obs-year 规则折算；
- `thread_id` 是排序结果首成员的归档 id（确定性标识）。

响应示例（`a@x` → `b@x` 回复链，`b` 还引用了一封未归档的邮件）：

```json
{
  "id": 2,
  "fingerprint": "58d92498…431",
  "thread_id": 1,
  "members": [
    {"id": 1, "message_id": "a@x", "subject": "Hello",
     "normalized_subject": "hello",
     "date": "2024-01-01T10:00:00+00:00",
     "created_at": "2026-09-21T03:06:15.676328+00:00",
     "parent_id": null, "depth": 0},
    {"id": 2, "message_id": "b@x", "subject": "Re: Hello",
     "normalized_subject": "hello",
     "date": "2024-01-02T10:00:00+00:00",
     "created_at": "2026-09-21T03:07:01.112233+00:00",
     "parent_id": 1, "depth": 1}
  ],
  "edges": [{"parent_id": 1, "child_id": 2}],
  "broken_links": [
    {"message_id": 2, "referenced_message_id": "ghost@x",
     "reason": "missing_parent"}
  ],
  "order": [1, 2]
}
```

`members` 按 `order` 顺序给出，每项含规范化 `message_id`（无该头时
`null`）、原始与规范化主题、解析后的 `date`（非法/缺失时 `null`）、
`parent_id` 与回复深度 `depth`（根为 0）；`edges` 是父子边
（`parent_id` 是 `child_id` 的直接父件）。邮件不存在返回 **404**。

## 整邮件附件 ZIP

```bash
curl -OJ http://localhost:8000/messages/1/attachments.zip
# 或指定输出名
curl -o attachments.zip http://localhost:8000/messages/1/attachments.zip
```

把一封已归档邮件的全部**非文本叶子**（`kind == "blob"`，含
`message/rfc822` 封装报文内部附件）按 **MIME 文档序**打包为 ZIP。
该接口是**纯只读查询**：只读取节点行与附件库字节，不改写任何记录，
对历史归档数据同样有效。

**逐出现打包**：每个 blob 的**每次出现**写成独立 ZIP 条目——同一
内容（同 SHA-256）在邮件中出现两次就是两个条目，**不合并**；文本
叶子不进入归档。

**条目名（归档名）派生**：

- 取节点**解码文件名**（Content-Disposition 的 `filename` / RFC 2231
  `filename*=`，缺省回退 Content-Type 的 `name`）的 **basename**
  （`/` 与 `\` 均视为目录分隔符，取最后一段）；
- 移除 basename 中的全部 **Unicode 控制字符**（`Cc` 类：C0、DEL、
  C1），并把残留的 `/`、`\` 替换为 `_`；
- 无文件名或清洗后为空时回退 **`attachment-{节点路径}`**（根节点
  路径为空，即 `attachment-`；深层如 `attachment-1_2`，路径分隔符
  同样替换）；
- 名称冲突以 **NFC + casefold** 为键（大小写不敏感），按文档序在
  **扩展名前**追加 ` (2)`、` (3)`……（`a.png` 与 `A.PNG` →
  `a.png`、`A (2).PNG`；点开头隐藏文件如 `.bashrc` 无扩展名，
  序号追加在末尾）；内置 `manifest.json` 预占同名槽位，附件不会
  覆盖清单。

**内置 `manifest.json`** 固定为 ZIP 首条目，逐项（文档序）记录：

| 字段 | 含义 |
| ---- | ---- |
| `node_path` | blob 叶子的物化路径（根为 `""`） |
| `original_filename` | 归档时保存的解码文件名（无则为 `null`，原样保留控制字符） |
| `archive_name` | ZIP 内实际条目名（清洗 / 回退 / 改名后） |
| `media_type` | 出现位置的规范化媒体类型（小写） |
| `sha256` | 解码字节的 SHA-256 |
| `size` | 解码字节长度 |

**字节确定性**：条目顺序、ZIP 条目时间（固定 `1980-01-01 00:00:00`）、
Unix 权限（固定 `0644`）、压缩方法与级别（固定 DEFLATE level 9）、
`manifest.json` 序列化（键排序、无多余空白、UTF-8、无尾换行）全部与
运行环境/时间无关——**同一邮件重复下载字节完全一致**。响应以归档
字节自身的 **SHA-256 给出强 ETag**（`ETag: "<hex>"`），可携带
`If-None-Match` 做条件请求，命中返回 **304**（无响应体）：

```bash
curl -o a.zip -D - http://localhost:8000/messages/1/attachments.zip
# ETag: "9f2c…7a"
curl -H 'If-None-Match: "9f2c…7a"' \
     http://localhost:8000/messages/1/attachments.zip
# HTTP/1.1 304 Not Modified
curl -s http://localhost:8000/messages/1/attachments.zip -o b.zip
cmp a.zip b.zip   # 两次下载字节一致
```

响应头：`Content-Type: application/zip`、
`Content-Disposition: attachment; filename="message-1-attachments.zip"`。

```bash
unzip -l attachments.zip
# Archive:  attachments.zip
#   Length  Name
#       …   manifest.json
#       40  a.png
#       40  A (2).PNG
unzip -p attachments.zip manifest.json | python3 -m json.tool
```

异常情况：

- **无附件**（纯文本邮件、multipart 但全是文本叶子）：仍返回
  **200** 的有效 ZIP，仅含一个内容为 `[]` 的 `manifest.json`；
- 邮件不存在：**404**；
- 附件解码字节不可用（邮件归档于附件字节留存升级之前）：**422**，
  错误码 `ATTACHMENT_BYTES_UNAVAILABLE`，`node_path` 定位到缺字节
  的 blob 节点。

## 两封邮件 MIME 树比较

```bash
curl -X POST http://localhost:8000/messages/compare \
     -H 'Content-Type: application/json' \
     -d '{"a": 1, "b": 2}'
```

在**两封已归档邮件**的 MIME 树上做只读、确定性的递归比较。该接口是
**纯只读查询**：只读取归档时保存的节点行，不写入、不修改任何记录，
对历史归档数据同样有效；同一对邮件重复请求结果逐字节稳定。

### 对齐与配对

按 **MIME 文档序**（先根后子、兄弟按 1..n）递归对齐节点。**物化路径
不是节点身份**——兄弟节点重排时路径会整体改变。每一层先按
**同层内容指纹**配对，内容指纹与归档指纹同口径（规范化 Content-Type、
规范化头字段、解码叶子摘要，剔除 multipart `boundary`、文本
`charset`、CTE 等打包噪音，取规范 JSON 的 SHA-256）：

- 某指纹在双方各出现一次 → **跨位置唯一配对**（因此纯重排被判等价，
  重排后的变更也能落到正确的对应节点上），随后递归比较该对子树；
- 同层该指纹在任一方出现多次（如两个内容完全相同的附件互换、一侧
  多了一份副本）→ 无法唯一配对，整组记 **`ambiguous`**，列出双方
  全部候选路径（`a_paths` / `b_paths`），不强行按位置落对、也不把
  多出的副本简单记成新增；
- 唯一配对消费后的剩余节点按文档序 1..n **位置对齐**：同位置递归
  配对（内容差异落到 `changed` 的各 facet 上），数量多出的一侧依次
  记 **`added`** / **`removed`**——只报子树根并附 `node_count`
  （该子树的节点总数），不展开每个节点；
- 容器 ↔ 叶子这类跨结构配对：节点本身记 `changed`（`content_type`
  等 facet），容器一侧的子节点在该层对齐中全部成为 `removed` /
  `added`。

### 比较侧面（facet）

每对配对节点逐面比较四个规范化侧面，`changed` 项的 `facets` 只列出
**有差异**的侧面（顺序固定：`content_type` → `disposition` →
`headers` → `leaf`）：

| facet | 规范化口径 |
| ---- | ---- |
| `content_type` | `{media_type, params}`：媒体类型小写，参数 RFC 2231 解码后排序；剔除 `boundary`（打包噪音）与 `charset`（内容已解码） |
| `disposition` | `{type, params}` 或 `null`：disposition 小写、参数解码排序 |
| `headers` | `[[小写名, 线性空白压缩后的解码值], ...]`，保留重复头与顺序；不含三个结构头（Content-Type / Content-Disposition / Content-Transfer-Encoding），故 CTE 不参与比较 |
| `leaf` | 文本叶子 `{kind:"text", text_sha256, length}`（规范化文本的 SHA-256 与字符数）；非文本叶子 `{kind:"blob", blob_sha256, length}`（解码字节的 SHA-256）；容器为 `null` |

### 响应

`diffs` 按双方文档序稳定排序（同层 `changed` → `ambiguous` →
`removed` → `added`）；`summary` 给出各类条数统计。示例（两封
mixed 邮件，第二叶子文本由 `two` 变为 `TWO!`，且一侧多了附件）：

```json
{
  "a": {"id": 1, "fingerprint": "8fbf98a6…2bd"},
  "b": {"id": 2, "fingerprint": "0cda5052…efe"},
  "identical": false,
  "summary": {"added": 1, "removed": 0, "changed": 1, "ambiguous": 0},
  "diffs": [
    {
      "kind": "changed",
      "a_path": "2",
      "b_path": "2",
      "a_paths": ["2"],
      "b_paths": ["2"],
      "facets": [
        {
          "facet": "leaf",
          "a": {"kind": "text", "text_sha256": "7692c3ad…", "length": 3},
          "b": {"kind": "text", "text_sha256": "24f1afcf…", "length": 4}
        }
      ],
      "node_count": null,
      "fingerprint": null,
      "content_type": "text/plain"
    },
    {
      "kind": "added",
      "a_path": null,
      "b_path": "3",
      "a_paths": [],
      "b_paths": ["3"],
      "facets": [],
      "node_count": 1,
      "fingerprint": null,
      "content_type": "application/pdf"
    }
  ]
}
```

`ambiguous` 项示例（双方各有两份相同叶子，无法区分谁对应谁）：

```json
{
  "kind": "ambiguous",
  "a_path": null,
  "b_path": null,
  "a_paths": ["1", "2"],
  "b_paths": ["2", "3"],
  "facets": [],
  "node_count": null,
  "fingerprint": "c1e372ae…8001",
  "content_type": "image/png"
}
```

说明：

- `identical: true` 当且仅当递归比较无任何差异。注意归档指纹把
  **节点顺序**纳入指纹，而比较不以路径为身份——因此仅兄弟顺序不同的
  两封邮件是不同归档 id，但比较结果 `identical`；规范化后完全等价
  （boundary / CTE / 头大小写不同）的邮件在归档时已幂等为同一 id；
- `changed` 项同时给出 `a_path` / `b_path`；`added` 只给
  `b_path`、`removed` 只给 `a_path`；`ambiguous` 两者均为 `null`，
  候选在 `a_paths` / `b_paths` 中；
- 根节点路径为 `""`，单节点邮件的差异 `a_path` / `b_path` 都是
  `""`。

### 错误

| 状态码 | 条件 |
| ---- | ---- |
| **400** | `a` 与 `b` 是同一个 id（自比较无意义） |
| **404** | 任一 id 在归档中不存在（`detail` 指明具体 id） |
| **422** | 请求体不合规：缺字段、字段非整数、id 小于 1 |

## 日历事件汇总（iCalendar / RFC 5545）

```bash
curl http://localhost:8000/calendar/events
```

在**全部归档邮件**的 `text/calendar` 文本叶子（含
`message/rfc822` / `message/global` 封装报文内部）上做**只读、确定性**
的日历事件汇总：只读取归档时保存的节点文本，不写入、不修改任何记录，
对历史归档数据同样有效；同一归档库重复请求结果逐字节稳定。

**解析规则**：

- 先按 RFC 5545 §3.1 **展开折叠行**（归档文本换行已统一为 `\n`；
  续行以 SP/HT 开头，展开时删除换行及续行的首个空白字符，其余空白
  保留），再遍历组件：`BEGIN:VCALENDAR` 内的 `METHOD`（值大写化，
  典型为 `REQUEST` / `CANCEL`；在 VCALENDAR 中的位置宽容）作为其
  范围内 VEVENT 的方法，`VTIMEZONE` 等非 VEVENT 组件整体跳过；
- 每个 **VEVENT** 读取 `UID`、`SEQUENCE`（缺省 `0`，须为非负整数）、
  `DTSTAMP`、`DTSTART`、`DTEND`、`SUMMARY`（TEXT 值按 RFC 5545
  §3.3.11 反转义）；重复属性宽容取首次；本接口只抽取单次事件，
  `RRULE` 等重复规则不影响字段读取；
- 每个事件**必须有** `UID`、`DTSTAMP`、`DTSTART`、`DTEND`；
- 时间只接受两种形式：**UTC**（`Z` 后缀）或带 **IANA `TZID`** 的
  DATE-TIME（`Asia/Shanghai`、`America/New_York` 等，经 `zoneinfo` +
  随镜像打包的 `tzdata` 折算 UTC）。DATE 值（`VALUE=DATE`）、浮动
  时间（无 Z 无 TZID）、`Z` 与 `TZID` 同写、无法解析的 TZID、
  不存在的日历日期均为无效时间；
- **夏令时诊断**：带 TZID 的本地时间若落在春季跳时空隙
  （如 `America/New_York` 2026-03-08 02:30）→ 不存在；落在秋季
  回拨重复区间（如 2026-11-01 01:30）→ 有歧义——两种情况逐项诊断，
  不猜测偏移；
- **同一节点内重复 UID** 的每个事件逐项诊断（含 `vevent_index`、
  `uid`、`count`），全部不入选；不同节点 / 不同邮件的同 UID 合法
  事件进入版本选择，不算重复。

**跨邮件版本选择**：同一 UID 的合法事件依次按
（`SEQUENCE` 降序 → `DTSTAMP` 降序 → `message_id` 降序 →
`node_path` 文档序降序）选出**当前版本**；胜出版本的 `METHOD` 为
`CANCEL` 时 `status="cancelled"`（其余 `confirmed`），并在
`source` 中保留其归档来源（`message_id` / `node_path`）。结果按
**UTC 起始时间、UID** 稳定排序。

响应中的每条 `diagnostics` 都带 `message_id` 与 `node_path`
（根节点为 `""`），被诊断的事件不会出现在 `events` 中：

```json
{
  "events": [
    {
      "uid": "team-sync@example.com",
      "sequence": 2,
      "method": "REQUEST",
      "status": "confirmed",
      "dtstamp": "2026-09-20T12:00:00Z",
      "dtstart": "2026-09-28T14:00:00Z",
      "dtend": "2026-09-28T15:00:00Z",
      "summary": "Team sync",
      "source": {"message_id": 3, "node_path": "2"}
    }
  ],
  "diagnostics": [
    {
      "message_id": 2,
      "node_path": "1",
      "code": "CALENDAR_TIME_NONEXISTENT",
      "detail": {
        "vevent_index": 0,
        "field": "DTSTART",
        "value": "20260308T023000"
      }
    }
  ]
}
```

诊断码：

| `code` | 触发条件 |
| ---- | ---- |
| `MISSING_CALENDAR_FIELD` | 缺 `UID` / `DTSTAMP` / `DTSTART` / `DTEND`（`detail.field`） |
| `INVALID_CALENDAR_TIME` | 时间值无法解析：DATE 值、浮动时间、Z+TZID、未知 TZID、非法日期（`detail.field` / `detail.value`） |
| `CALENDAR_TIME_NONEXISTENT` | TZID 本地时间落入春季夏令时跳时空隙（墙钟不存在） |
| `CALENDAR_TIME_AMBIGUOUS` | TZID 本地时间落入秋季夏令时回拨重复区间（两种 UTC 偏移） |
| `INVALID_CALENDAR_SEQUENCE` | `SEQUENCE` 不是非负整数（`detail.value`） |
| `DUPLICATE_CALENDAR_UID` | 同一 `text/calendar` 节点内多个 VEVENT 使用同一 UID（逐项给出，`detail.vevent_index` / `uid` / `count`） |

空归档（或没有任何日历节点）返回
`{"events": [], "diagnostics": []}`，状态码始终为 **200**——解析问题
全部体现在诊断列表中，不影响接口可用性。

## 内容寻址附件库

归档时，所有**非文本叶子**（`kind == "blob"`，含整封邮件本身就是二进制、
以及 `message/rfc822` 封装报文内部的附件）的**解码后字节**按 SHA-256
去重存入 `attachments` 表；每次出现（哪封邮件的哪个节点、解码后的文件名、
disposition、媒体类型）记入 `attachment_refs` 表。消息、节点、附件实体
与引用在**同一事务**提交；重复归档同一指纹的邮件不会新增任何实体或引用。

- 同一内容在同一封邮件出现多次、或出现在多封邮件中，只存一份字节，
  `reference_count` 与 `occurrences` 反映全部出现位置；
- 解码文件名取 `Content-Disposition` 的 `filename`（含 RFC 2231
  `filename*=` 形式），缺省时回退 `Content-Type` 的 `name`；
- 新表用 `CREATE TABLE IF NOT EXISTS` 建立，启动时对已有 SQLite 库
  幂等升级；升级前归档的邮件未留存附件字节，不会回溯补录。

### 检索

```bash
curl 'http://localhost:8000/attachments?limit=20&offset=0'
curl 'http://localhost:8000/attachments?sha256=c1e372ae'      # 哈希前缀（完整 64 位即精确）
curl 'http://localhost:8000/attachments?filename=发票'         # 文件名子串（ASCII 大小写不敏感）
curl 'http://localhost:8000/attachments?media_type=image/png' # 出现位置的媒体类型
```

每个列表项：

```json
{
  "sha256": "c1e372ae…8001",
  "size_bytes": 45,
  "media_type": "image/png",
  "reference_count": 2,
  "created_at": "2026-09-21T03:06:15.676328+00:00",
  "occurrences": [
    {"message_id": 2, "node_path": "2", "filename": "s.png",
     "disposition": "attachment", "media_type": "image/png"},
    {"message_id": 5, "node_path": "1/2", "filename": "copy.png",
     "disposition": "attachment", "media_type": "image/png"}
  ]
}
```

`media_type`（实体级）为首次归档时的媒体类型；`filename` / `media_type`
过滤按**任一出现位置**匹配。

### 下载

```bash
curl -O http://localhost:8000/attachments/c1e372ae…/content
curl -H 'Range: bytes=0-99'  http://localhost:8000/attachments/c1e372ae…/content   # 206
curl -H 'Range: bytes=100-'  http://localhost:8000/attachments/c1e372ae…/content   # 206
curl -H 'Range: bytes=-50'   http://localhost:8000/attachments/c1e372ae…/content   # 206（最后 50 字节）
```

- 不带 `Range`：`200` 返回完整解码字节，`Content-Type` 为实体媒体类型，
  响应带 `Accept-Ranges: bytes`；
- 合法单段范围：`206` + `Content-Range: bytes start-end/size`，
  末端超出大小按 RFC 7233 收敛；
- 越界、多段（`bytes=0-1,3-4`）或非法范围：`416` +
  `Content-Range: bytes */size`；
- 未知哈希：`404`。

## 错误响应

所有解析 / 解码失败统一为 `422`：

```json
{
  "error": {
    "code": "UNDECODABLE_CONTENT",
    "message": "base64 内容含非法字符 0x40",
    "node_path": "2",
    "detail": {"offset": 0, "byte": 64}
  }
}
```

`node_path` 指向出问题的 MIME 节点（根节点为 `""`）。解析在写库之前
**完整完成**，因此失败请求不会在 `messages` / `nodes` / `attachments` /
`attachment_refs` 表中留下任何行；写库本身也在单事务内，中途出错整体回滚。

错误码：

| `code` | 触发条件 |
| ---- | ---- |
| `INVALID_MESSAGE` / `INVALID_HEADER` / `INVALID_HEADER_NAME` | 报文/头块结构不合法 |
| `DUPLICATE_HEADER` | Content-Type / CTE / Content-Disposition 重复出现 |
| `INVALID_CONTENT_TYPE` / `INVALID_DISPOSITION` | 媒体类型或 disposition 非法 |
| `INVALID_BOUNDARY` | 缺 boundary、边界非法、正文中找不到边界、缺闭合边界 |
| `DUPLICATE_PARAMETER` | 同名参数重复（含 RFC 2231 续片序号重复、普通/扩展混用） |
| `INVALID_PARAMETER` | RFC 2231 续片序号不连续、扩展值损坏 |
| `INVALID_HEADER_VALUE` | 参数引号未闭合、结构头中出现 encoded-word 等 |
| `UNKNOWN_CHARSET` | 声明的字符集无法识别 |
| `INVALID_CTE` / `UNSUPPORTED_CTE` | 传输编码非法或不支持 |
| `UNDECODABLE_CONTENT` | base64 / quoted-printable / 字符集解码失败，或 7bit 含非 ASCII 字节 |
| `UNDECODABLE_HEADER` | RFC 2047 编码字（B/Q）本身损坏 |
| `AMBIGUOUS_RELATED_START` / `RELATED_START_NOT_FOUND` | `display-body` 查询时 multipart/related 的 `start` 重复命中或未命中（查询期错误，不改写记录） |
| `MULTIPLE_DELIVERY_STATUS` / `DELIVERY_STATUS_NOT_FOUND` / `DELIVERY_STATUS_UNREADABLE` / `INVALID_DELIVERY_STATUS` / `MISSING_DELIVERY_FIELD` / `INVALID_DELIVERY_FIELD` | `delivery-report` 查询时 DSN 作用域结构或 `message/delivery-status` 字段非法（查询期错误，不改写记录） |
| `ATTACHMENT_BYTES_UNAVAILABLE` | `attachments.zip` 打包时 blob 节点的解码字节在附件库中缺失（邮件归档于附件字节留存升级之前），`node_path` 定位该节点 |
| `UNARCHIVE_EMPTY_BATCH` / `UNARCHIVE_DUPLICATE_ID` | 批量撤销请求为空列表或含重复 id（422，整批拒绝、数据不变） |
| `UNARCHIVE_MESSAGE_NOT_FOUND` | 批量撤销目标 id 不存在或已被撤销（404，整批拒绝、数据不变） |
| `UNARCHIVE_FINGERPRINT_MISMATCH` | 批量撤销目标的指纹与当前归档指纹不符（409，整批拒绝、数据不变；`detail` 给出提供值与当前值） |

`/calendar/events` 的日历问题**不是错误响应**：接口始终返回 200，
`MISSING_CALENDAR_FIELD` / `INVALID_CALENDAR_TIME` /
`CALENDAR_TIME_NONEXISTENT` / `CALENDAR_TIME_AMBIGUOUS` /
`INVALID_CALENDAR_SEQUENCE` / `DUPLICATE_CALENDAR_UID` 逐项放在响应体
的 `diagnostics` 中（见上文“日历事件汇总”）。

## 指纹规范化规则

指纹输入是一棵 JSON 规范树（键排序、无多余空白、UTF-8、LF），
对其取 SHA-256。规范化时**剔除打包/传输噪音**：

- 头名统一小写；头值完成 RFC 2047 解码、Unicode NFC、线性空白压缩，
  相邻 encoded-word 之间的空白按 RFC 删除；
- 叶子文本按声明字符集解码为 Unicode，再 NFC、换行（CRLF/CR/LF）归一为 `\n`；
- 不纳入指纹：multipart `boundary`、文本节点的 `charset`（已解码）、
  Content-Transfer-Encoding（已解码）、头折叠方式、邮件换行风格；
- 非文本叶子取**解码后字节**的 SHA-256 与长度；
- 节点顺序、媒体类型、`Content-Disposition` 及其文件名等参数仍参与指纹
  （调换 part 顺序会得到不同指纹）。

可以用 `app.mimeparser` 直接查看规范化结果：

```python
from app.mimeparser import parse_message, fingerprint, canonical_tree, canonical_json
tree = parse_message(open("message.eml", "rb").read())
fp, canon = fingerprint(tree)
print(fp)
print(canon.decode())
```

## 运行测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## 环境变量

| 变量 | 默认 | 说明 |
| ---- | ---- | ---- |
| `MIME_DB_PATH` | `/data/mime.db` | SQLite 文件路径，启动时自动建库建表 |

## 设计说明与边界

- **严格而非容错**：不使用 Python 标准库 `email` 包。标准库会修复坏边界、
  吞掉非法字节、对重复参数“后者覆盖”，不适合需要可复现指纹的归档场景。
- **字面头字节宽容**：非 encoded-word 的“脏”头（如老式 latin-1 Subject）
  按 UTF-8 → latin-1 回退解码，不会因此拒收邮件；编码字本身仍严格校验。
- **7bit 语义**：保留“内容仅 US-ASCII”这一不变量，但不强制 CRLF 配对，
  以支持换行风格无关的指纹。
- **epilogue / preamble**：multipart 前导区与闭合边界后的尾注按 RFC 忽略，
  不参与指纹。
- `message/rfc822` 与 `message/global` 容器递归解析；容器自身的 CTE
  先还原再递归。
