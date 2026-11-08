> **最新 P4 出口审查（2026-09-15）：** [P4-EXIT-REVIEW-2026-09-15.md](P4-EXIT-REVIEW-2026-09-15.md) 为当前阶段门权威结论；旧测试数字和历史“未 shadow”表述仅供追溯。

# 🦊 酒狐档案馆｜Fufu Archive 综合项目介绍与进度报告

> **当前修复收口（2026-09-12）：** P4.3 的 capability 标签仍只是受信进程内误用防护，不是认证授权；CLI 已移除 `--role`，队列控制默认 fail-closed。P4.2 已补充 Hermes active/profile 语义、单次 SQLite 快照、durable publish journal 与真实 hard-kill 回归；P4.4 deterministic refinement 已接 collector 的 `content` schema，并对 tool-call-only 回合提供显式 opt-in（默认仍拒绝）。真实 source 数据仍有未声明缺口，P4 总出口仍不通过。

> **最新独立验证：P4 总出口不通过。** 当前全量数字以本文件末尾及最新 P4 验收记录为准；项目 venv 的 MCP SDK 测试仍因可选依赖缺失而 skipped，Hermes agent venv 已单独跑通 MCP 工具测试，并通过真实 stdio JSON-RPC smoke。合成反例已复现脱敏泄漏、归档正文完整性、CLI 冲突、health 误报及恢复一致性等问题。详见 [P4 验收报告](P4-VALIDATION-2026-09-10.md)；下文旧进度/测试数字不可作为最新验收结论。

> **2026-09-10 交接更新：** P4 工作树已推进至 P4.3 queue/Runner 管理核心；P4.1/P4.2 仍有路线出口与可靠性待验收项，P4.3 的 capability 标签已有合成测试但**仍无认证授权**；真实故障矩阵和常驻调度出口仍未完成，不能视为 P4 完成。当前全量复测 267 passed / 5 skipped（2026-09-11），2026-09-10 基线为 258 passed / 5 skipped。旧清单属于历史状态；分项证据以源码、测试和 [最新 P4 交接](P4-HANDOFF-2026-09-10.md) 为准。

> **文档地位：项目唯一权威总览（Source of Truth）**
> 项目目录：以下路径均相对于项目根目录。
> 默认数据库相对路径：`data/fufu_archive.db`
> 当前阶段：**P0–P2 完成；P3 核心闭环完成；P4.1/P4.2 有实现但仍待路线验收；P4.3 queue/Runner 核心已实现，P4 未完成；P5 未实施**
> 状态核对日期：2026-09-11
> 已发布代码基线：`<historical-commit> feat(p3): add reviewed archive candidates`
> 路线修订：P4/P5 聚焦 **Hermes + SQLite**；P4 已有分切实现但未完成总出口。具体切片与验收见 [P4/P5 路线](design/P4-P5-ROADMAP.md)。

本文统一说明 Fufu Archive 是什么、为什么这样设计、代码和数据如何流动、P0–P5 每阶段原计划是什么、已经完成什么、尚未完成什么，以及继续开发时必须遵守的边界。历史文档只用于追溯；若历史文档与本文或当前源码冲突，以**当前源码、测试结果和本文**为准。

---

## 1. 一句话介绍

**Fufu Archive（酒狐档案馆）面向 Hermes 长期工作，目标是本地优先、可独立运行的日志、知识与记忆服务。当前已有 SQLite、FTS5/本地向量、HTTP/stdio MCP 和候选审核；P4/P5 将补齐增量归档、分类提炼、独立调度、资料取回与受控维护。**

当前实现与目标必须分开：

- 当前 Hermes 原文仍在 `state.db`；现有 collector 只做保守 dry-run，不代表完整归档已实现。
- 目标是定期捕获新活动会话，将授权范围内可恢复的源档案、分类缩写与正式知识分层保存；不能将摘要宣称为无损备份。
- 归档与备份恢复验证通过后，允许在明确授权下清理非活跃宿主源会话；不是要求原文永远留在 Hermes，也不是现在就自动删除。
- skill/memory 正文与版本逐步入档，宿主保留精简引子、取回入口和必要启动/安全信息；执行型技能需要按需导出、路径验证与离线缓存。
- 当前只适配 Hermes + SQLite；具体其他项目接入仅属于未来展望，不预建通用适配平台。
- 项目文档仍统一在仓库 `docs/`；工作区只保留指向仓库的入口，不产生第二份权威方案。

---

## 2. 项目目标与明确不做的事

### 2.1 目标

1. **本地优先与隐私优先**：私人会话、授权领域资料和个人知识默认不上传第三方服务。
2. **中文可用**：关键词检索不能把整句中文当成一个 token；向量检索应适合中文语义。
3. **可降级**：Ollama 或 sqlite-vec 不可用时，关键词检索仍能工作。
4. **可验证**：检索优化必须跑固定评测集；代码阶段必须有测试、编译和真实协议检查。
5. **候选优先**：来源复杂或由会话自动提炼的内容，先进入候选区，审核后才能成为正式知识。
6. **可追溯和防覆盖**：状态变化有审计，revision 阻止旧操作覆盖新状态，并发提交不能生成重复正式文档。
7. **轻量可维护**：优先 Python + SQLite 的小型自实现，不整套引入大型 RAG、图数据库或工作流平台。
8. **最终可备份恢复**：代码走 Git，数据库与授权资料独立备份，通过恢复演练后才评估源清理。
9. **独立与可降级**：自有调度不依赖 Hermes 会话存活；无模型仍能归档、规则整理与 FTS 检索，明确报告未完成的语义处理。

### 2.2 当前明确不做

- 不未经授权全量采集聊天；规划中的来源档案与正式知识分区，不能把原始聊天直接当可信知识；
- 不让 LLM 未经审核直接写正式知识；
- 不自动下载模型；
- 当前未自动分类 `topic_key`；P4 只增加待审分类建议，不建复杂主题树；
- 不自动合并同主题但不同内容；
- 不因“长期没被搜索”就自动判定知识为错误或删除；
- 不上 GraphRAG、图数据库、复杂 query routing 或常驻 RAG 监控；
- 不整套引入 Docling、Marker、Unstructured、RAGFlow 等大型解析工程；
- 不复制无许可证、许可证冲突或限制不清的外部代码；
- 不把 `.env`、API token、数据库文件或私人原文提交 Git。

---

## 3. 总体架构

```text
                         ┌──────────────────────────┐
                         │      Hermes Agent        │
                         │ memory / session_search  │
                         └────────────┬─────────────┘
                                      │ 本地 stdio MCP
                                      ▼
┌──────────────────┐       ┌──────────────────────────┐
│  HTTP 调用方      │──────▶│ fufu_archive 接口层       │
│ Bearer Token     │       │ FastAPI / MCP adapter    │
└──────────────────┘       └────────────┬─────────────┘
                                      │ 共用 KnowledgeBase
                                      ▼
                         ┌──────────────────────────┐
                         │      SQLite 单文件        │
                         │ documents / chunks       │
                         │ metadata / tags          │
                         │ candidates / audit       │
                         │ FTS5 / sqlite-vec        │
                         └────────────┬─────────────┘
                                      │ 可选向量化
                                      ▼
                         ┌──────────────────────────┐
                         │ Ollama                   │
                         │ qwen3-embedding:0.6b     │
                         └──────────────────────────┘

Hermes state.db（只读）
        │
        ▼
外置 session_candidate_collector
        │ 只产生 dry-run 待审报告
        ▼
候选审核流程 ──批准后──▶ Fufu Archive 正式知识
```

### 3.1 两个入口，共用一套数据库逻辑

本节架构图和入口表描述现有实现。规划目标是独立服务拥有配置与调度，Hermes 为接入端；源档案、任务与 UI 尚未实现，见 [P4/P5 路线](design/P4-P5-ROADMAP.md)。

| 入口 | 用途 | 安全边界 |
|---|---|---|
| FastAPI HTTP | 供本机程序、脚本和将来的 UI 调用 | `/kb/*` 必须使用环境变量中的 Bearer token；`/health` 公开 |
| stdio MCP | Hermes 本地调用 | 直接复用 `KnowledgeBase`，不开放端口，也不把 HTTP token 复制进 MCP 配置 |

两个入口最终都调用 `src/fufu_archive/db.py`，避免 HTTP 和 MCP 各写一套业务规则。

### 3.2 正式知识与候选知识分离

```text
外部内容 / 会话提炼
        │
        ▼
pending candidate
   ├── approve ──▶ approved ── commit ──▶ committed + formal document
   └── reject  ──▶ rejected
```

- `pending`、`approved`、`rejected` 都不进入正式检索；
- 只有 `approved` 能提交；
- 重复或并发提交同一候选，只生成一份正式文档；
- 候选状态变化和审计事件位于同一事务；
- `expected_revision` 用于检测陈旧操作；
- 可选 `topic_key` + SHA-256 `content_hash` 只防止“同主题、完全相同内容”重复；
- 同主题但内容不同不会自动覆盖；
- `supersedes_document_id` 只建立显式版本关系，目前**不会自动把旧文档改成 expired**。

---

## 4. 数据模型与数据库迁移

当前数据库 migration 到 **v5**。

| 版本 | 主要内容 | 所属阶段 |
|---|---|---|
| v1 | `documents`、`chunks`、`metadata`、FTS5；collection/domain/status/parent/supersedes 基础字段 | P0 |
| v2 | title、content type、language、`parent_contexts`、`document_tags` | P2 |
| v3 | `candidates`，含 pending/approved/rejected/committed 状态约束 | P3 |
| v4 | documents/candidates revision；`audit_events` | P3 |
| v5 | documents/candidates 的 `topic_key`、`content_hash` 与唯一索引 | P3 |

`chunk_vec` 是 sqlite-vec 虚拟表，由运行环境按配置的向量维度建立；sqlite-vec 无法加载时不会阻断 FTS5。

### 4.1 重要数据边界

- 运行数据库：`data/fufu_archive.db`，已被 `.gitignore` 排除；
- Git 只保存源码、测试、评测语料和文档；
- 文档正文、metadata 与 tag 进入正式库前仍需检查隐私和来源；
- 向量索引是可重建派生数据，但当前尚无正式 `doctor` 或重建命令；
- 未来 migration 前必须先做 SQLite 一致性检查和备份恢复演练。

---

## 5. 主要代码模块

| 文件 | 责任 |
|---|---|
| `src/fufu_archive/config.py` | 从环境变量读取数据库、监听地址、token、embedding 与上传大小配置；缺 token 时 fail-closed |
| `src/fufu_archive/models.py` | HTTP 输入输出模型、长度限制、安全 JSON、tag 和 `topic_key` 校验 |
| `src/fufu_archive/db.py` | migration、事务、正式入库、候选审核、审计、FTS/向量/混合检索、版本与并发保护 |
| `src/fufu_archive/tokenization.py` | jieba 中文预分词、停用词处理与无 jieba 降级分词 |
| `src/fufu_archive/embedding.py` | 可插拔 Embedder 与 Ollama 实现；查询侧加入中文检索 instruct |
| `src/fufu_archive/chunking.py` | 显式启用的中英文文本切块与 overlap |
| `src/fufu_archive/injection_scan.py` | 无 LLM 的提示注入启发式扫描，只记录模式，不执行或自动删除内容 |
| `src/fufu_archive/query_rewrite.py` | 可选、确定性的空白/标点/会话填充词改写，不生成同义词 |
| `src/fufu_archive/main.py` | FastAPI 应用、鉴权、health、ingest、upload、search |
| `src/fufu_archive/mcp_server.py` | 10 个 stdio MCP 工具；优先兼容 MCP SDK 2.x `MCPServer`，保留旧版 fallback |
| `eval/` | 32 篇无隐私语料、44 条固定查询与 Recall/MRR/NDCG 评测脚本 |
| `tests/` | 14 个测试模块，覆盖数据库、API、向量、上传、分块、注入、改写、候选、并发和 MCP adapter |

---

## 6. 四条关键数据流

### 6.1 正式文本入库

```text
HTTP kb/ingest、kb/upload 或 MCP kb_add
  → Pydantic/参数校验
  → 可选注入模式扫描
  → 可选 chunking
  → documents/chunks/metadata/tags/FTS5 事务写入
  → best-effort 生成向量并写 chunk_vec
```

向量服务失败时，正式文本和 FTS5 副本仍保留。当前上传只支持 `.txt`、`.md`、`.json`；PDF、DOCX、XLSX 等并未实现。

### 6.2 检索

```text
查询
  → 可选确定性 rewrite（最多 3 个变体）
  → fts5 / dense / hybrid
  → collection/domain/status/topic_key 过滤
  → 结果合并与去重
```

- 默认 `dense`；
- `fts5` 使用中文预分词 + 停用词过滤 + OR + BM25；
- `hybrid` 使用 RRF，但既有评测显示无权重融合的排序不如 dense-prefix；
- Ollama、sqlite-vec 或向量路径失败时自动降级 FTS5。

### 6.3 候选审核

```text
kb_candidate_add
  → pending
  → kb_candidate_approve / kb_candidate_reject
  → approved 才能 kb_candidate_commit
  → 正式 document + audit events
  → kb_search / kb_list 查回验证
```

### 6.4 Hermes 会话提炼（尚未自动闭环）

```text
<profile-root>/state.db（只读）
  → deterministic collector
  → 敏感/短会话/活跃会话/子 agent 分流
  → dry-run JSON/Markdown 候选
  → 人工复核
  → P3 candidate API
```

会话 collector 位于 profile 长期脚本目录，不在本仓库。真实 `state.db` dry-run 已成功，但 unittest discovery 仍有历史失败，且没有注册自动写库 cron。子 Agent 运行日志应独立保存为优化资料，不直接混入正式知识。

---

## 7. P0–P5 阶段计划与真实进度

### 7.1 总览

| 阶段 | 原计划目标 | 当前结论 |
|---|---|---|
| P0 | SQLite/FTS5、FastAPI、鉴权、基础入库与检索 | **完成** |
| P1 | 中文关键词、向量、混合检索、过滤、固定评测 | **完成** |
| P2 | 分块、metadata/tags、上传、父子上下文、查询改写与扫描 | **完成已批准的轻量范围**；大型格式解析未做 |
| P3 | Hermes MCP、候选审核、会话提炼、审计与版本控制 | **核心工程闭环完成**；会话自动整理和首批人工运营验收未做 |
| P4 | 可靠性底座、Hermes 增量源归档、独立调度、可选提炼与无模型降级 | **P4.1/P4.2 有实现待出口验收；P4.3 queue/Runner 管理核心已实现；总出口未完成** |
| P5 | 管理界面、skill/memory 取回、受控源清理；检索增强按需 | **未实施，等待 P4** |

### 7.2 P0｜基础可运行

**计划内容**

- 建立 Python 项目；
- SQLite migration、documents/chunks/metadata、FTS5；
- FastAPI `/health`、`/kb/ingest`、`/kb/search`；
- 本地监听与 Bearer token 鉴权；
- 中文基础检索、测试和 Git 忽略规则。

**已完成**

- 提交 `<historical-commit>` 建立基础工程；
- FTS5、参数绑定、基础 metadata 与状态过滤落地；
- token 未配置时 `/kb/*` 返回 503，错误或缺失 token 返回 401；
- `/health` 保持公开；
- 后续 `<historical-commit>` 补齐并发初始化和 P0/P1 验收问题。

**未做/不属于 P0**

- 向量检索、复杂分块、MCP、自动会话入库和备份。

### 7.3 P1｜检索质量可测量

**计划内容**

- 中文 FTS5；
- Qwen3 embedding + sqlite-vec dense；
- FTS+dense 的 RRF；
- collection/domain/status 过滤；
- 固定评测集和真实 HTTP 冒烟。

**已完成**

- jieba 预分词；无 jieba 时字符/二元词降级；
- 修复 AND 全词导致中文口语查询几乎全灭的问题，改成停用词过滤 + OR + BM25；
- `Embedder` 抽象、Ollama `qwen3-embedding:0.6b`、查询侧中文 instruct；
- dense / hybrid / fts5 三模式和 FTS5 降级；
- 32 篇语料、44 条查询、Recall/MRR/NDCG 固定评测；
- 既有基线：FTS5 Recall@10 0.977、MRR@10 0.928；dense-prefix Recall@10 1.000、MRR@10 1.000；
- 发现无权重 hybrid 的 MRR@10 0.960，低于 dense-prefix，因此没有把“混合”误当成天然更好；
- 修复 dense 过滤不足、embedding 失败影响 FTS、hybrid score 不一致、评测误把 fallback 当 dense、Ollama 响应校验和首次 WAL 并发锁等问题。

**仍可改进，但不阻塞 P1 完成**

- 评测集对当前模型已接近饱和，需要更难的同义改写、跨域和近似文档；
- hybrid 若继续优化，应做加权 RRF 或 dense 主导，而不是直接加复杂框架；
- reranker 放在 P5，必须以新难例证明收益。

### 7.4 P2｜受控入库管线

**计划内容**

- 文本分块和父子上下文；
- title/content type/language/tags/metadata；
- 文件上传与大小/编码/格式校验；
- 查询改写；
- 注入扫描和输入限制；
- 为未来多格式文档预留扩展点。

**已完成**

- 提交 `<historical-commit>`；
- opt-in chunking、overlap、parent context；
- metadata、tags、标题、语言和内容类型；
- `.txt`、`.md`、`.json` 上传；
- 单文件与整体请求限制、UTF-8/JSON/扩展名校验；
- 无依赖、确定性的提示注入模式扫描；
- 最多 3 个变体的确定性查询改写；
- 直接 `KnowledgeBase.ingest` 与 API 共用关键校验；
- 项目当时 P2 回归为 130 passed，后续 P3 全量回归继续覆盖这些路径。

**没有完成**

- PDF、DOCX、XLSX、扫描件 OCR；
- LLM 语义查询改写；
- 自动文件 watcher 或目录批量同步；
- 自动相似内容融合。

这些不是遗漏式 bug，而是为控制依赖和维护成本主动保留的边界。真实文件需求出现时，优先评估 `pypdf`、`python-docx` 等小依赖，不默认加入 Docling/RAGFlow 全家桶。

### 7.5 P3｜Hermes 接入与审核治理

**计划内容**

- stdio MCP；
- 会话只读增量扫描；
- 候选审核而非自动入库；
- 审计、revision、并发与版本关联；
- 原生 memory 与外置知识的职责分离。

**已完成**

- 最终提交 `<historical-commit>` 已推送 `origin/main`；
- MCP 从最初 4 个工具扩展为 10 个；
- MCP SDK 2.x `MCPServer` 兼容，同时保留旧版 fallback；
- `pending → approved/rejected → committed`；
- revision 乐观锁、审计事件与同事务写入；
- `BEGIN IMMEDIATE` 防止同一候选并发发布两份正式文档；
- 可选 `topic_key`、SHA-256 内容去重、显式 supersedes；
- 真实 Hermes MCP stdio 连接并发现 10 个工具；
- 2026-09-07 全量回归：149 passed、5 skipped；Hermes agent venv 的 MCP adapter：7 passed；compileall 与 `git diff --check` 通过；
- 独立审查发现 `list_candidates(collection=...)` 使用不存在的 SQL 别名，提交前已修复并在真实 MCP 环境复测。

**尚未完成**

- collector 的 unittest discovery 失败定位；
- 5–10 条脱敏真实候选的人工审核、拒绝、重复和 supersedes 运营验收；
- 自动定期扫描；
- LLM curator 的稳定输出和至少 20 个候选人工样本；
- 自动写正式库——目前明确保持关闭；
- `supersedes_document_id` 目前只验证目标存在，不强制同一 `topic_key`，也不自动改变旧版状态；
- 项目独立 venv 未安装可选 MCP extra，相关测试会 skip；真实 MCP 测试需继续使用带 SDK 的环境或加入 CI 矩阵。

因此最准确的说法是：**P3 核心数据库/MCP/治理开发完成；自动会话整理没有启用，首批真实候选运营验收是进入 P4 前置。**

### 7.6 P4｜可靠底座与独立归档整理

**分切实现中，整体未完成。** 只围绕 Hermes + SQLite，按以下切片顺序验收；任务与出口详见 [P4/P5 路线第 3 节](design/P4-P5-ROADMAP.md#3-p4可靠底座与独立归档整理)。

1. **P4.1 基线与可恢复性**：collector 真实失败修复、项目自身 MCP SDK 验收、5–10 条脱敏候选、只读 doctor、SQLite 一致性备份与隔离恢复、向量模型/维度和补建边界。
2. **P4.2 增量捕获与源归档**：collector 纳入项目管理；新活动会话已落盘消息的只读快照、水位、版本/hash、幂等、覆盖与脱敏清单；来源档案和知识分离。
3. **P4.3 独立运行与降级**：已实现 SQLite queue、lease token、重试/backoff、持久化 pause/resume、bounded Runner、collector handler、一次性 CLI、inspect/summary/reclaim/retry-failed 及多进程/重启测试；进程内 capability 标签已具备合成测试，但**认证主体与端到端授权未实现**（CLI 已移除可伪造的 `--role`，改为默认 fail-closed 的本地操作互锁）；真实部署权限、离线故障矩阵和常驻调度出口仍未完成。
4. **P4.4 可选提炼**：一个可用 provider、分类缩写与证据映射、至少 20 个候选人工样本、候选审核查回；失败待补偿，不自动批准。

**出口**：前述切片均有实际验收，重启/故障/无模型路径可用，关闭 Hermes 不影响档案馆已有任务。生产常驻启动、模型凭据、跨盘备份位置及策略另行确认。P4 不执行真实源会话删除或宿主全文外置。

### 7.7 P5｜管理界面、资料取回与受控清理

**未实施，依赖 P4 完成。** 具体任务见 [P4/P5 路线第 4 节](design/P4-P5-ROADMAP.md)。

1. **P5.1 管理界面**：轻量本地 Web UI，检索/来源/审核/任务/设置/备份状态；UI、CLI 与后台共用校验；主题、提示音、资源和自启仅对已实现且授权的功能开放。
2. **P5.2 skill/memory 取回**：小批量正文/版本快照、精简宿主入口、按需导出与脚本路径验证、离线缓存、冲突与回滚；不直接搬空宿主文件。
3. **P5.3 生命周期与受控源清理**：先清理预览，再隔离演练，最后才在明确授权与源版本/覆盖/备份验证通过后操作真实非活跃会话；默认不删正式知识。
4. **可选增强**：难例评测、权重/reranker 按实测收益选择；RAGAS、HyDE、Obsidian 是后续实验，不作为基本出口。

**出口**：界面真实可操作，Hermes 新会话可取回并使用外置资料，断线可回退，隔离源清理后仍可检索恢复。生产清理未授权时明确报告未启用，不冒称已上线。

### 7.8 未来展望（不进入 P4/P5 当前排期）

其他数据库（例如 MySQL）、DeepSeek Harness（DSH）及其他 Agent、通用 adapter/SDK、用户 AI 生成适配器与跨 Agent 辅助工作者，仅保留为后续方向。当前不做任何具体其他项目适配，不为它们预建框架或增加依赖；新需求出现后重新核查公开接口、许可、版本与实际兼容性。

---

## 8. 当前 MCP 工具

| 工具 | 用途 |
|---|---|
| `kb_search` | FTS/dense/hybrid 检索，可按 collection/domain/topic 过滤 |
| `kb_add` | 对已确认的正式资料直接入库 |
| `kb_candidate_add` | 创建待审候选 |
| `kb_candidate_list` | 列出候选，默认 pending |
| `kb_candidate_approve` | 批准候选 |
| `kb_candidate_reject` | 拒绝候选 |
| `kb_candidate_commit` | 把 approved 候选提交为正式知识 |
| `kb_audit_events` | 查询候选或正式文档审计事件 |
| `kb_update` | 修改正式文档状态或描述字段，不重写正文 |
| `kb_list` | 列出正式文档摘要 |

`kb_add` 适合用户明确确认的整理后资料；自动会话提炼不得用它绕过 candidate 流程。

---

## 9. 开源项目借鉴边界

已完成的公开项目调研结论是：**只借成熟、独立、许可证清楚的小设计，不引入完整外部记忆/RAG 系统。**

| 来源 | 可借鉴 | 不引入/不复制 |
|---|---|---|
| mcp-memory-service | `doctor` 故障清单、SQLite/sqlite-vec 运维思路 | 不替换现有数据库 |
| Vault Agent Memory | candidate-first、审核、回滚、审计模型 | 不整套移植服务 |
| QMD | 加权 RRF、rerank 评测思想 | 不引入完整 CLI/索引体系 |
| Engram | 稳定 `topic_key`、渐进披露 | 不引入另一套记忆库 |
| remindb | 内容 hash、快照、单点设计 | 不复制未核清许可或低成熟代码 |
| Graphiti | `valid_at / invalid_at / learned_at` 时间语义 | 不上图数据库 |
| Docling/Marker/Unstructured | 只在真实复杂文档需求出现时参考 adapter | 当前不增加大型解析依赖 |
| sqliteai/sqlite-rag | clean-room 借鉴架构概念 | 仓库无 LICENSE，不复制、改编或分发源码 |

旧文档中“直接抄”“整包 fork”“memU 有 LRU 淘汰”等表述已经被后续许可证和源码复核推翻，只能视为历史记录，不能再作为实施指令。

> **调研时效说明**：仓库内已有 2026-08/09 的公开项目调研与引用台账；本轮文档整理没有假装完成一次新的全网搜索。未来若要引入新依赖，应重新核验当前版本、最近提交、测试、许可证和 Windows 支持。

---

## 10. 可靠性、安全与维护注意点

1. **测量真实路径**：项目 venv 测试会因未装可选 MCP 而 skip；必须补跑 Hermes agent venv 的 adapter 测试和真实 `hermes mcp test`。
2. **启动成功不等于工具可用**：MCP 连接和工具发现不能替代逐工具调用；P3 的 SQL 别名问题就是在真实调用中发现的。
3. **向量降级会掩盖问题**：生产检索允许 fallback，但评测必须确认向量索引完整，不能把 FTS5 fallback 算成 dense 成绩。
4. **事务优先**：涉及状态、审计、去重和版本关系时，读—检—写必须在明确事务内，并有并发和回滚测试。
5. **中文 FTS 不用 AND 全词**：真实口语有大量虚词；保持查询侧停用词过滤 + OR + BM25。
6. **模型切换要重建向量**：不同 embedding 模型或维度的向量不能混用。
7. **注入扫描不是安全边界**：它只记录启发式命中；不能执行归档内容里的命令，也不能因此省略权限与来源检查。
8. **热度不等于真伪**：`last_accessed` 只能用于复核排序，不能单独把知识判为 expired 或删除。
9. **原始会话不等于知识**：当前原文留在 `state.db`；目标将可恢复来源档案与提炼知识分层。摘要不等于无损备份，覆盖缺口或未知源版本时不得清理源会话。
10. **数据与代码分开备份**：Git 保护源码和文档；SQLite 与私密配置需要单独、加密且可恢复的备份。
11. **不存凭据**：`.env`、token、私钥、认证文件、完整 system prompt 和敏感工具输出不进入 Git 或候选正文。
12. **文档不能替代测试**：阶段“完成”必须对应源码、测试、真实 smoke 和可复现提交，而不是计划表被勾选。
13. **`/health` 只表示应用已启动**：不验证 migration、FTS、向量索引完整性或 Ollama 可用性；真实健康检查属于 P4 `doctor` 的职责。
14. **SQLite 未持久化“当前向量模型/维度”元数据**：切换 embedding 模型或维度前必须显式重建 `chunk_vec`，不能只改 `FUFU_ARCHIVE_EMBED_*` 后假定旧向量仍兼容。
15. **“请求成功”不等于“向量已建立”**：embedding 失败时正式文档与 FTS 保留、向量缺失；P4 需要缺失向量补建能力，避免静默落后。
16. **上传与 embedding 的资源注意点**：multipart 会先把整个请求读入内存；同步 Ollama 调用在请求线程内执行，模型变慢会占住工作线程。并发大上传 + 慢模型时需关注内存与线程占用。
17. **`.env.example` 未列出 `FUFU_ARCHIVE_UPLOAD_MAX_BYTES`**：`config.py` 已支持，部署样例待补齐（P4 顺手修正）。

---

## 11. 配置与运行边界

主要环境变量：

| 变量 | 默认/行为 |
|---|---|
| `FUFU_ARCHIVE_DB_PATH` | 默认 `data/fufu_archive.db` |
| `FUFU_ARCHIVE_HOST` | 默认 `127.0.0.1` |
| `FUFU_ARCHIVE_PORT` | 默认 `8000` |
| `FUFU_ARCHIVE_API_TOKEN` | 无默认值；缺失时受保护 API fail-closed |
| `FUFU_ARCHIVE_EMBED_URL` | 默认 `http://127.0.0.1:11434` |
| `FUFU_ARCHIVE_EMBED_MODEL` | 默认 `qwen3-embedding:0.6b` |
| `FUFU_ARCHIVE_EMBED_DIM` | 默认 `1024` |
| `FUFU_ARCHIVE_UPLOAD_MAX_BYTES` | 默认 `1,000,000` |

MCP 由 Hermes 使用本地 stdio 启动；不应在文档或命令输出中记录实际 token。

---

## 12. 文档管理规则

### 12.1 权威顺序

1. 当前源码、数据库 migration 与测试；
2. 本文 `docs/PROJECT-OVERVIEW.md`；
3. `README.md` 的用户入口说明；
4. `docs/design/` 的现行边界；
5. `docs/operations/` 的运行方法；
6. `docs/research/` 的定期调研快照；
7. `docs/history/` 的历史计划和验收记录。

### 12.2 更新规则

- 每完成一个阶段，先更新测试和代码，再更新本文的“已完成/未完成”；
- 历史报告不重写结论，只加醒目的过时说明；
- 公开调研必须带核验日期和引用台账；
- 价格、star、模型榜单和许可证状态属于可变事实；
- 项目专属长文统一进仓库 `docs/`；
- `local-knowledge-base` 技能只保留触发规则、稳定边界和本文路径，不再复制整套阶段日志；
- 临时验证产物不作为项目文档的权威来源。

---

## 13. 当前待办与阶段闸门

> 当前独立验证：项目全量 `267 passed / 5 skipped`；Hermes agent venv MCP adapter `7 passed`；`compileall` 与 `git diff --check` 通过。
> 测试通过不等于 P4 总出口通过；以下按“已实现 / 仍需真实验证 / 生产启用”分层。

### 高优先级：P4 真实运行出口（不启用生产常驻）

- [x] P4.1/P4.2/P4.3 可离线实现：doctor、backup/restore、collector、checkpoint、source archive、queue/Runner、lease、retry、coverage、fail-closed、确定性 pending candidate。
- [x] 真实 Hermes `state.db` 只读 schema 探针：required schema valid，`messages.timestamp` 为 REAL，WAL 多读 smoke 通过，源库元数据未被探针改变。
- [ ] 真实 shadow run：在临时 archive/checkpoint/report 目录运行真实只读采集，验证 active/ended 分流、变更补采、发布失败回滚；不写正式库。
- [ ] 真实锁争用、跨卷文件系统故障、kill/掉电恢复演练；无法证明时不得取得源清理资格。
- [ ] 将 MCP SDK 纳入项目 venv/CI 依赖矩阵；当前项目 venv 的 5 项 MCP 测试仍 skip，Hermes agent venv 的 MCP 测试与 stdio smoke 已通过。
- [ ] P4.3 认证主体与端到端授权；当前本地 operator 互锁不是认证。
- [ ] 离线 / SQLite 故障 / 多实例的真实 bounded-run 故障矩阵。

### 中优先级：P4.4 受控提炼

- [x] 无模型确定性 pending-only 入口、collector→refinement 接线、`content`/`preview` 兼容、证据映射、身份校验和 partial fail-closed；tool-call-only partial 仅支持显式 opt-in。
- [ ] 一个实际可用的本地语义提炼 provider；没有现成模型时保持无模型降级，不下载模型、不伪造 provider 成功。
- [ ] 至少 20 条人工候选评审，验证遗漏、误收、重复、归因和失败补偿。

### P5：依赖 P4 真实运行出口

- [ ] P5.1：轻量管理界面；前置是 P4 shadow run、权限边界和任务状态稳定。
- [ ] P5.2：skill/memory 正文版本与按需取回；需先做小批量隔离演练和离线回滚。
- [ ] P5.3：生命周期预览、隔离清理演练；生产源清理必须另行明确授权。

### 明确禁止自动执行

常驻 worker、cron、自启、生产自动采集、修改 Hermes memory/skills、正式候选 approve/commit、真实源会话删除均不因上述测试通过而自动启用。

保留项目总卡 `<historical-task>`、P4 `<historical-task>`、P5 `<historical-task>`；P4 保持 ready，P5 保持 todo（依赖 P4），不新增碎卡。

## 14. 关键证据入口

- Git 历史：`<historical-commit>` → `<historical-commit>` → `<historical-commit>` → `<historical-commit>` → `<historical-commit>` → `<historical-commit>` → `<historical-commit>`
- 当前用户入口：[`../README.md`](../README.md)
- 文档索引：[`README.md`](README.md)
- P1 评测：[`../eval/README.md`](../eval/README.md)
- P3 最终验收：[`history/P3-CANDIDATE-REVIEW-2026-09.md`](history/P3-CANDIDATE-REVIEW-2026-09.md)
- P4/P5 当前路线：[`design/P4-P5-ROADMAP.md`](design/P4-P5-ROADMAP.md)
- P3 保守采集历史边界：[`design/P3-CONSERVATIVE-SESSION-INGEST.md`](design/P3-CONSERVATIVE-SESSION-INGEST.md)（不代表最终归档范围）
- 开源调研：[`research/`](research/)

---

## 总结

P0–P3 已提供 SQLite 检索和审核治理底座；本轮只修订定位与路线。P4 先可靠性，再 Hermes 增量源归档、独立调度与受控提炼；P5 再管理界面、资料取回和验证后的源清理。坚持 Hermes + SQLite 优先、无模型可用、小步验收，不将未来的其他数据库/Agent 接入扩大成当前工程。


## 2026-09-11 宽任务收口补充：四级证据边界

本次仅补充可安全实现的本地闭环，**不代表 P4 总出口完成**：

- **实现层**：`deterministic_refinement.py` 已接收 collector 的 `messages[].content`（兼容旧 `preview`），按消息顺序保留原文并建立 `source_message_id` 证据映射；默认只接受 complete，显式 `allow_declared_exclusions` 时仅接受 tool-call-only/tool-body 这类已声明缺口，仍只能创建 `pending` candidate。
- **合成验证层**：临时 SQLite/WAL fixture 覆盖 collector 的 schema、active/ended snapshot、变更 fingerprint、tool-call-only/旧 schema/partial/blocked coverage、同卷 staged publish/retry、真实子进程 hard-kill，以及 queue 的 lease/retry/SQLite health/脱敏路径；MCP 另有真实 stdio smoke。
- **真实环境验证层**：已对真实 Hermes `state.db` 做只读 schema/count shadow 复核；当前 3 个候选均为 partial，未声明缺口为 malformed/missing（88/9/12），因此默认与 opt-in 都是 0 条可精炼。真实库未写入；附件接口、跨盘掉电、生产锁争用和消息变更采集仍未验收。
- **生产启用层**：未启用采集、常驻 worker、cron、自启或认证部署；`FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR=1` 仍只是本地互锁，不是认证。附件、未知格式、截断和缺失消息保持 partial/blocked，禁止据此清理源。

本轮组合证据与解释器、skip 原因见 `docs/P4-VALIDATION-2026-09-10.md` 的最新更新；项目 venv 全量测试仍不等于 P4 总出口，MCP 必须在 Hermes agent venv 或 CI SDK 矩阵中验收。

## 2026-09-15 P4.4-1/2 实现增补

- **实现层**：新增显式注入的 `SemanticExtractionProvider` 契约与 `SemanticExtractionRequest`；`deterministic_refinement.refine_with_provider` 校验结构化 `items`、来源消息证据、身份和输出预算，成功结果仍只能是 `pending`。
- **合成验证层**：provider 成功、非法 JSON/结构、超时、有界重试、敏感信息和批量预算边界已通过测试；Provider 持久化 smoke 已确认候选为 `pending` 且不进入 FTS 检索。
- **未完成**：尚无实际语义模型/provider 接入；没有真实网络调用、生产 queue 注册、常驻调度、approve/commit 或正式源数据写入。
- **阶段结论**：P4.4 接口与安全边界已实现并完成合成验证，但 provider 实际能力、20 条真实人工样本、生产认证/跨卷故障证据仍缺失；P4 总出口与 P5 状态不变。

## 2026-09-15 路线边界修订：P4/P5 优先单用户使用，平台生态后移 P6

项目范围约定：DSH、AstrBot、通用 Agent Adapter、插件/Provider/Backend SDK、插件分发和多租户等平台化能力不提前纳入 P4/P5，统一作为未来 P6 方向。

- **P4**：继续完成可靠核心、Hermes 增量归档、queue/Runner、语义 Provider 和候选审核出口。
- **P5**：目标调整为 单用户可直接使用的个人版：本地 CLI/HTTP/MCP、Hermes 单一接入、记忆检索/审核/备份恢复，以及少量 skill/memory 按需取回；UI 仅在实际使用需要时做轻量版本。
- **P6**：再规划 Hermes 完整插件、DeepSeek Harness MCP/Adapter、AstrBot Star、通用 Agent Adapter、插件清单/能力声明、Provider/Backend Registry、插件分发和多用户/多租户。

不得为了 P6 的通用兼容性提前引入第三方 Agent 依赖、复制其内部插件内核或扩展当前 P4/P5 的验收范围。P4 未通过前不启动 P5；生产常驻采集、cron、自启和源清理边界保持不变。

## 2026-09-17 P4.4-3A 进度补充

已新增 `src/fufu_archive/archive_codex_provider.py` 与 `tests/test_archive_codex_provider.py`，实现档案馆独立 Codex Responses Provider 的安全骨架：显式 archive-owned credential store、原子保存、独立 refresh lock、可注入 transport、Codex `/responses` 请求和错误脱敏。当前不读取 Hermes `auth.json`，不实现浏览器 OAuth 登录，不发送真实会话内容。

本轮项目 venv 定向测试为 22 passed（含既有 refinement），全量回归为 325 collected / 320 passed / 5 skipped；compileall 通过，`git diff --check` 通过。真实 OAuth grant、实际订阅请求、独立额度归属和生产启用仍未验证，不能宣称 Provider 已接通生产。
