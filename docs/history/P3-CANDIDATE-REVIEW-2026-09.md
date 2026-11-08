# 🦊 Fufu Archive P3｜审核、审计与主题版本闭环

> 状态：**已实现并验收**（2026-09-07）
> 工程：`.`
> 数据库：`data\fufu_archive.db`（不纳入 Git）

## 1. 这阶段解决什么

P3 不把 Hermes 原始聊天自动塞进知识库。它先把内容作为**候选**保存，经过审核后才成为可检索的正式知识；每一步都可追溯，并避免同一主题的同一内容重复入库。

```text
候选创建
  pending ──审核通过──> approved ──提交──> committed
      └──审核拒绝──> rejected
```

- `pending`、`approved`、`rejected` 都不会进入正式检索。
- 只有 `approved` 候选能提交为正式 document。
- 同一候选重复提交、或两个调用同时提交，只会得到同一份正式 document。
- 每个状态变化与正式提交都有审计事件；状态写入与审计写入位于同一 SQLite 事务。

## 2. 核心能力

| 能力 | 行为与边界 |
|---|---|
| 候选审核 | `pending → approved/rejected → committed`；不支持从已审核状态反向篡改。 |
| 乐观锁 | 写操作可传 `expected_revision`（兼容别名 `revision`）；陈旧客户端会收到 `RevisionConflict`，不会覆盖较新的状态。 |
| 审计 | `candidate.created/approved/rejected/committed` 与 `document.created` 记录实体、状态、revision、时间和关联详情。 |
| 并发保护 | 候选创建与提交使用 SQLite `BEGIN IMMEDIATE` 串行化关键读-检-写路径。 |
| 主题去重 | 可选 `topic_key` + SHA-256 `content_hash`：同主题且内容完全相同，返回已有候选/正式文档。未指定 `topic_key` 时保留旧版“允许内容重复”的行为。 |
| 版本关联 | 新候选提交时可显式传 `supersedes_document_id`；只建立可追溯链接，**不会自动替旧文档改状态**，生命周期状态仍由明确的 `kb_update` 或后续 P4 策略决定。 |
| MCP | 本地 stdio MCP 不复制 HTTP token，使用 Hermes 的 Python 环境；兼容 MCP SDK 2.x 的 `MCPServer`，并保留旧 `FastMCP` 回退。 |

## 3. 主要文件说明

| 文件 | 职责 |
|---|---|
| `src/fufu_archive/db.py` | SQLite migration（当前至 v5）、候选状态机、revision、审计、主题 hash 去重、版本关联、检索和事务边界。 |
| `src/fufu_archive/models.py` | `IngestRequest` / `SearchRequest` 的 `topic_key` 输入验证。 |
| `src/fufu_archive/main.py` | HTTP ingest、上传与检索把 `topic_key` 传入数据库层。 |
| `src/fufu_archive/mcp_server.py` | Hermes stdio MCP 适配器：检索、正式入库、候选审核、审计查询、文档更新/列表。 |
| `tests/test_candidates.py` | 审核状态机、revision 冲突、审计顺序、提交回滚与并发提交测试。 |
| `tests/test_topic_key.py` | topic 保存/查询、相同主题 hash 去重、版本关联、非法 topic、并发创建与回滚测试。 |
| `tests/test_mcp_adapter.py` | MCP 工具集合与端到端候选审核路径测试；项目 venv 未装可选 `mcp` 时跳过，另由 Hermes MCP smoke 覆盖协议启动。 |
| `data/fufu_archive.db` | 运行数据，不提交；升级时由 `initialize_database()` 自动应用 migration。 |

## 4. MCP 工具

| 工具 | 用途 |
|---|---|
| `kb_search` | FTS / dense / hybrid 检索；可按 `collection`、`domain`、`topic_key` 过滤。 |
| `kb_add` | 直接加入已确认的正式知识。 |
| `kb_candidate_add` | 创建待审候选。 |
| `kb_candidate_list` | 默认列出待审候选；支持状态、collection、topic 过滤。 |
| `kb_candidate_approve` / `kb_candidate_reject` | 审核候选。 |
| `kb_candidate_commit` | 将 approved 候选提交为正式知识；可指定 `supersedes_document_id`。 |
| `kb_audit_events` | 查询候选或正式文档的有序审计事件。 |
| `kb_update` / `kb_list` | 管理正式文档的生命周期属性与摘要列表。 |

## 5. 典型人工审核流程

```text
1. kb_candidate_add(..., topic_key="hermes/mcp/fufu-archive")
2. kb_candidate_list(status="pending")
3. 人工检查内容、来源、注入扫描 metadata 与分类
4. kb_candidate_approve(candidate_id, expected_revision=0)
5. kb_candidate_commit(candidate_id, expected_revision=1)
6. kb_search(...) 或 kb_list(...) 查回验证
7. 需要替代旧版本时，在第 5 步显式传 supersedes_document_id
```

会话采集器仍然只生成待审产物，不能绕开上述审核流程；设计见 [`../design/P3-CONSERVATIVE-SESSION-INGEST.md`](../design/P3-CONSERVATIVE-SESSION-INGEST.md)。

## 6. 验收证据

2026-09-07 在仓库根目录执行：

```text
.venv\Scripts\python.exe -m pytest --disable-warnings -rA
→ 149 passed, 5 skipped

.venv\Scripts\python.exe -m compileall -q src tests
→ 通过

git diff --check
→ 通过

hermes --profile example mcp test fufu-archive
→ Connected；Tools discovered: 10
```

5 个跳过项均是 `tests/test_mcp_adapter.py`：项目 `.venv` 未安装可选的 `mcp` extra；不是业务失败。实际协议链路由 Hermes agent venv（已安装 MCP 2.x）完成 smoke。

## 7. 尚未做的事（P4 / P5）

- 会话 collector 的 unittest discovery 故障定位与修复；当前真实 `state.db` dry-run 已跑通，但自动写库保持关闭。
- 首轮人工审核 5–10 条脱敏候选并通过 MCP 正式入库，覆盖接受、拒绝和显式版本替代。
- 数据库 `doctor`、备份/恢复演练、失效候选扫描与 cron 注册。
- 生命周期自动策略：若要在显式版本替代后自动将旧文档标为 `expired`，需先明确状态语义、审计事件与回滚策略，再作为独立垂直切片实现。
- Reranker、质量评测扩展和 Obsidian 浏览层属于 P5，不随 P3 引入。
