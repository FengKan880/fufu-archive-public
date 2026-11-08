> **文档性质：公开项目调研**
> 本文是 2026-09 的记忆生命周期调研快照；不把外部项目整套引入 Fufu Archive。

# 开源 AI Agent 长期记忆 / 会话提炼 / 生命周期项目调研

> 核对日期：2026-09-06（UTC+08:00）
> 目标：为 Fufu Archive 的 `state.db 只读增量扫描 → 确定性过滤 → LLM 待审提炼 → ACTIVE/EXPIRED/PERMANENT/回收站` 管线寻找可移植机制，并明确 OSS / 云服务边界。

## 结论先行

1. **最适合直接借鉴的是 LangMem 的“函数式候选变更集”**：LLM 输出 create/update/remove 建议，但不必自动写库；`RemoveDoc` 可由调用方解释为软删除、硬删除或降权。这与 Fufu 的 dry-run、待审、回收站天然对齐。[19][20]
2. **最值得移植的数据模型来自 Graphiti，而不是整套图数据库**：给事实增加 `valid_at / invalid_at / learned_at`，更新时关闭旧事实的有效区间，保留来源链。Fufu 首版可在 SQLite 中实现，不必引入 Neo4j/FalkorDB，也不必保存完整原始会话 episode。[14][16]
3. **最值得移植的审计机制来自 Letta MemFS**：每次记忆变更带原因并形成 Git commit，支持 diff、历史和冲突处理；但 Letta 的记忆写入不是默认的人类审批队列，后台 “Agent reviews before applying” 也是第二个 agent 复核，并非用户确认。[6][8][9]
4. **memU 与 Fufu 的采集边界最接近**：只接收已完成会话，`prepare → 外部 agent 提炼 → commit`，以内容哈希提交变化；但当前 v1 没有文件删除提交、ETag/三方合并或后端冲突解决，不能照搬其提交语义。[10][12]
5. **Mem0 OSS 当前 v3 不再自动做冲突更新/删除**：`add()` 是单次 ADD-only 抽取；可手工 update/delete，有单条记忆 history 和 expiration，但 Graph Memory、Decay、Temporal Reasoning、Dream 自动合并/淘汰属于 Platform，不是本地 OSS。[3][4][5]
6. **“OpenMemory” 已换项目含义**：当前 `mem0ai/openmemory` 是在 Claude Code/Codex/OpenCode 间搬运编码会话的 CLI/TUI，不是长期记忆引擎；旧自托管 MCP+Dashboard 已归档，只能作代码参考。[21][23]
7. **应更正 Fufu 既有方案中的旧结论**：当前 memU 官方仓库没有可核实的“高频晋升/LRU 淘汰”机制；其现行重点是 completed-session、自进化 Markdown、混合检索与 Local/Cloud backend。不要把旧版本/宣传口径继续写成已具备的生命周期能力。[10][12][13]

## 项目与许可证

| 项目 | 当前官方源码 | 许可证 | 本地开源形态 | 关键边界 |
|---|---|---|---|---|
| Mem0 | [mem0ai/mem0](https://github.com/mem0ai/mem0) | Apache-2.0[1][2] | Python/TS 库；可自托管 server | OSS 有核心增删改查、history、expiration；没有可查询图、decay、temporal reasoning、Dream、项目级事件流。[4] |
| Letta / MemGPT | [letta-ai/letta-code](https://github.com/letta-ai/letta-code) | Apache-2.0[6][7] | 本地 CLI/App Server + 本地 agent memory repo | 旧 `letta-ai/letta` 主分支已是 landing page；当前实现看 `letta-code`。Cloud 才提供跨设备托管 agent 状态；本地 repo 要自行备份。[6][8] |
| memU | [NevaMind-AI/memU](https://github.com/NevaMind-AI/memU) | Apache-2.0（文件名为 `LICENSE.txt`）[10][11] | 本地 SQLite（单写者、暴力余弦）或本地 Postgres/pgvector；也可接 Cloud | `MemoryService` 不负责 LLM 提炼，提炼由外部 agent 做；现行设计明确不做图。[10][12][13] |
| Zep / Graphiti | [getzep/graphiti](https://github.com/getzep/graphiti) | Apache-2.0[14][15] | Graphiti 自托管框架，需自带 Neo4j/FalkorDB/Neptune 等图后端和 LLM/embedding | Zep 是托管产品，专有 Context Graph Engine、用户/线程管理、dashboard/debug/API logs 不在 Graphiti OSS。[14] |
| LangMem | [langchain-ai/langmem](https://github.com/langchain-ai/langmem) | MIT[17][18] | Python primitives；可接任意存储，原生接 LangGraph Store | `InMemoryStore` 不持久；生产持久化、审计、TTL、审批、图谱均由调用方/存储层补齐。[17][20] |
| OpenMemory（当前） | [mem0ai/openmemory](https://github.com/mem0ai/openmemory) | MIT[21][22] | 编码会话迁移 CLI/TUI | 负责 preview/select/port session，不是记忆提炼与生命周期系统。[21] |
| OpenMemory（旧归档） | [openmemory-archive](https://github.com/mem0ai/openmemory/tree/main/openmemory-archive) | Apache-2.0[23][24] | 旧 MCP server + dashboard + Mem0 backend | 已停止维护；可研究其状态机/管理 UI，不应作为新生产依赖。[23] |

## 机制对比

| 维度 | Mem0 OSS v3 | Letta Code / MemGPT | memU | Graphiti OSS | LangMem | OpenMemory |
|---|---|---|---|---|---|---|
| **会话增量抽取** | 应用每轮/每批调用 `add(messages)`；单次 LLM、ADD-only，持续累积。[1][3] | agent 在热路径主动改 MemFS；dreaming 可按步数或 compaction 后后台复盘近期会话。[6][9] | 只接收 completed session；`prepare` 生成任务，外部 agent 对既有 memory/skill 做 create/patch/no-op，`commit` 只交内容哈希变化。[12] | 每个 episode 增量抽实体/边，不需全图重算；原始 episode 是溯源根。[14] | hot path 或会后/background reflection；manager 接收 conversation + current memories，返回更新状态。[19][20] | 当前仅迁移会话；旧归档通过 Mem0 add 做抽取，但已停止维护。[21][23] |
| **事实更新 / 冲突** | 当前自动抽取不 UPDATE/DELETE；冲突事实可能并存。应用可显式 update/delete。Dream 的合并、supersede 属 Platform。[3][4] | agent 直接重写文件；Git 可重放/合并并保存历史。Memory block API 的并发写本质上仍可能 last-write-wins，需靠 repo workflow/串行化控制。[8] | 外部 agent 比较既有文件后 patch/create；一个 developer workspace 同时只允许一 run，但 backend 无 ETag/base revision/三方合并，重叠写可能覆盖。[12] | 新事实与已有边冲突时令旧边失效，保留时间历史；关系边时间化较强，但 node 属性/summary 并非完整版本化，官方 issue 明确此缺口。[14][25] | collection manager 可 create/update/remove；函数式 API 先返回变更，调用方决定 RemoveDoc 的语义。profile 是“只保留当前状态”的单文档覆盖模型。[19][20] | 旧归档有 active/paused/archived/deleted 状态和状态历史，但依赖旧 Mem0 事件模型；不宜按当前 v3 假设继续工作。[26][27] |
| **时效性** | `expiration_date` 在 OSS/Platform 都可用；到期后 search/get_all 隐藏，get(id) 仍可查，清空日期可恢复。[5] | 没有通用 valid-time/TTL；依赖 agent 重写、dreaming/doctor 清理和 Git 历史。[8][9] | 现行 completed-session 管线无 TTL/有效区间；由宿主或后端自建。[12] | 边带 `valid_at/invalid_at`，episode 另有 ingest/created time，适合 as-of 查询。[14][16] | 文档建议检索综合 similarity、importance、recency/frequency/strength，但库本身不提供统一 TTL 状态机。[19] | 旧归档有 archive schema/状态时间；当前项目无长期记忆时效模型。[21][26] |
| **遗忘 / 衰减** | OSS 有显式 delete 和 expiration；**没有 decay**。Platform decay 只是检索时软排序偏置，不删除，Dream 才做后台合并/淘汰。[4][5] | 显式文件 delete/重写；无内建遗忘曲线或 EXPIRED/PERMANENT 状态。[6][8] | v1 commit 明确不提交文件删除；无可核实的 LRU/遗忘曲线。[12] | 旧事实 invalidation ≠ 遗忘；没有基于访问频率的 decay/自动回收。[14] | RemoveDoc 可映射为删除、失效或降权；具体生命周期完全交给调用方。[20] | 旧归档状态机可软隐藏/归档；当前项目不适用。[21][26] |
| **图谱** | OSS v3 仍做 entity extraction 并用于排序，但可查询 Graph Memory 已移除；图是 Platform 功能。[3][4] | 核心是 Git-backed 文件层级与按需读取，不是事实图谱。[8] | 当前 ADR 明确“hybrid retrieval, no graph”，无实体层/多跳。[13] | 核心能力：entity/fact/episode/context graph，混合语义+BM25+图遍历。[14] | 可用 schema 抽 triple，但只是记录集合，不自动形成图数据库/遍历层。[20] | 旧归档是 Mem0+向量库管理 UI，不应视作图谱实现。[23] |
| **审计** | OSS 只有 per-memory history；Platform 才有 project-wide event feed、dashboard/webhooks 等管理面。[4] | MemFS 每次编辑形成 Git commit，天然 diff/history/rollback；`/doctor` 审核结构、重复和 prompt token 使用。[8][9] | prepare 时做内容哈希快照，失败保留 workspace，commit 只提交 changed files；没有完整用户级审批日志。[12] | 每个事实追溯到 episode，溯源强；但 Graphiti 本身不含 Zep 的 dashboard/debug/API logs。[14] | 函数式结果可在写库前记录完整 proposal；若用 store manager 自动写，则审计要由应用补。[17][20] | 旧归档有状态变化表和 changed_by；当前迁移工具只有用户可见 preview/selection。[21][26] |
| **人工审批** | 无内建 review queue；调用方可在 add/update/delete 前后自建审批。[1][4] | 有通用 tool permission/HITL，但记忆工作流可自动提交；“Agent reviews before applying”不是人审。Git diff 适合作为人工复核界面底座。[8][9] | 无人审 UI；但 prepare 与 commit 是天然审批闸门，Fufu 可在中间插入用户确认。[12] | 无；episode 入图后自动抽取/去重/失效。需要外围 staging 表。[14] | 最容易插人审：使用 functional manager 获取 create/update/RemoveDoc，不调用 store manager 自动落库。[20] | 旧 dashboard 可手动 pause/archive/delete；已归档，只适合借鉴交互。[23][27] |

## 云能力误认风险清单

### Mem0

- 当前 OSS 与 Platform 共享核心 CRUD、抽取/检索、entity-aware ranking、expiration 和 per-memory history。[4][5]
- **仅 Platform**：可查询 Graph Memory、Memory Decay、Temporal Reasoning、Dream 后台 consolidation/supersede、项目级事件流、webhooks、批量操作、feedback、summary、托管 dashboard。[4]
- README 的 2026 benchmark 明示使用 managed platform 的专有优化；不能把分数直接当 OSS 本地预期。[1]
- 旧 v0.x 文档中的“自动 ADD/UPDATE/DELETE 冲突解决”和外部 Neo4j graph 配置已被 v3 迁移指南否定。[3]

### Letta

- 当前开源实现入口是 `letta-code`，不是已退休的 Letta V1 server 主分支。[6]
- Local agent 的 MemFS commit/history 可本地使用，但**跨设备托管、cloud agent 状态、部分 shared repository SDK**不能当作本地能力；本地 repo 要自行备份。[6][8]
- 通用 approval UI 不等于“记忆候选必须由人批准”；memory/dreaming 可自动执行，需另加政策闸门。[9]

### memU

- Local backend 可用 SQLite/Postgres，但默认 embedding provider 仍可能是外部 API；“本地存储”不自动等于“全链路离线”。[10]
- Cloud 的跨设备/在线浏览不是本地能力。当前仓库未证明“LRU 淘汰”，不要沿用旧二手总结。[10][12][13]

### Zep / Graphiti

- Graphiti 是 OSS 框架；Zep 的专有 Context Graph Engine、大规模低延迟、多租户 user/thread 管理、治理、dashboard/debug/API logs 是托管产品能力。[14]
- Graphiti 本地运行仍需图数据库、LLM、embedding、运维与自己的会话/用户层；并非 SQLite 嵌入式 drop-in。[14]

### LangMem

- LangMem 是 memory primitives，不是完整数据库或治理系统。LangGraph Platform 默认提供 store 集成，不代表纯 `langmem` 包自带持久化、审计和运维。[17][20]

### OpenMemory

- 搜索结果常仍指向旧 `mem0/openmemory`，但该自托管 MCP/dashboard 已归档；当前同名仓库已变成 session-portability 工具。[21][23]

## 对 Fufu Archive 的可落地复用

### P0：首版就做（不增加重依赖）

1. **采用 LangMem 式 proposal，而不是自动写入**
   - LLM 输出固定结构：`CREATE | SUPERSEDE | MERGE | NOOP | EXPIRE_CANDIDATE`。
   - 每条带 `candidate_id`、`source_session_id`、`source_message_ids`、`reason`、`confidence`、`proposed_fact`、`matched_memory_ids`。
   - 映射：LangMem `RemoveDoc` 不物理删除，统一转 Fufu `EXPIRE_CANDIDATE` 或回收站提案。[20]

2. **采用 memU 式 prepare/commit 边界**
   - `prepare`：只读扫描已结束根会话，确定性过滤、内容哈希去重，生成独立 review bundle。
   - `commit`：只接受用户批准的 candidate IDs，写库后自然语言查回验证。
   - 不继承 memU 的限制：Fufu 要有 discard、可恢复重试、base revision/乐观锁和删除提案。[12]

3. **采用 Letta 式变更审计，但不必把整个 kb.db 放 Git**
   - 每次批准生成 append-only `memory_change_log`：before/after、reason、actor、source、proposal hash、模型/提示词版本、时间。
   - 同时导出人类可读 Markdown diff；必要时将审核 bundle/Obsidian 投影放 Git，数据库本体仍走 SQLite backup。[8]

4. **采用 Mem0 expiration 的“隐藏不删、可恢复”语义**
   - `EXPIRED` 默认不参与普通检索，ID 精确查询/审计仍可见；renew 清掉过期原因即可恢复。
   - 坏时间戳应 fail-open 并进入异常报告，避免静默丢记忆。[5]

### P1：补强当前生命周期模型

5. **从 Graphiti 借三类时间，不上图**
   - `valid_at / invalid_at`：现实事实何时成立/失效。
   - `learned_at`：Fufu 何时得知。
   - `created_at / updated_at`：记录写入时间。
   - `supersedes_id` 只表达版本链；检索当前事实以有效区间 + ACTIVE 状态共同过滤。[14][16]

6. **把“内容过时”和“长期未调用”分开**
   - `invalid_at/superseded` 是事实真实性变化；
   - `last_accessed/access_count` 只用于排序或复核优先级，不能单独证明事实为假；
   - 180 天无调用只能进入 `EXPIRE_CANDIDATE`，不应直接物理删除。

7. **软衰减只影响排名，不驱动删除**
   - 可借 Mem0 Platform decay 的原则：设上下界，作为相近相关度结果间的轻量 tie-breaker；不得把访问频率变成“真伪判定”。[4]
   - 这套算法需在 Fufu 自行实现并评测，不能声称从 Mem0 OSS 获得。

### P2：数据量和关系查询证明需要后再做

8. **轻量关系表优先于 Graphiti 全家桶**
   - SQLite 表 `entities`、`facts(subject,predicate,object,valid_at,invalid_at,learned_at)`、`fact_sources` 足以支持来源追踪和简单关系查询。
   - 只有真实评测出现多跳召回缺口，再评估 Graphiti；否则 Neo4j/FalkorDB + LLM 抽图的复杂度、成本和中文时态误判不划算。[14][16]

9. **永远不要把 Graphiti raw episode 模型原样移植到会话库**
   - Graphiti 以原始 episode 为溯源根；Fufu 已明确“不全量入库原始会话”。
   - Fufu 应保存 `session_id + message_id ranges + content hash + 脱敏摘录`，需要原文时回查只读 `state.db`；子 agent 只进独立优化日志。

## 建议的 Fufu 审批状态机

```text
SCANNED
  → FILTERED_OUT                         # 确定性排除，保留原因/哈希
  → PROPOSED                             # LLM 结构化候选，archive_writes=0
      → REJECTED                         # 用户拒绝，保留审计
      → APPROVED
          → CREATE ACTIVE
          → MERGE ACTIVE
          → SUPERSEDE (new ACTIVE, old EXPIRED)
          → EXPIRE_CANDIDATE → EXPIRED
EXPIRED
  → RENEW ACTIVE
  → PERMANENT
  → TRASHED (30 天可恢复)
  → PURGED                               # 仅用户明确批准
```

并发安全建议：proposal 记录 `matched_memory_id + matched_memory_revision`；commit 时 revision 不一致则退回 `NEEDS_REBASE`，重新生成 diff，避免 memU 式 last-writer-wins。[12]

## 主要风险

- **LLM 冲突判断不稳定**：同一事实可能被误判为 duplicate/update/related；首版应输出候选和证据，不自动 supersede。
- **时间抽取幻觉**：Graphiti 的 prompt 虽要求不要臆造日期，但 `valid_at/invalid_at` 仍来自 LLM；Fufu 应保留 `time_confidence`，明确日期优先，模糊相对时间进入待审。[16]
- **ADD-only 污染**：照搬 Mem0 OSS v3 会让“现职/当前配置”等冲突事实并存；Fufu 必须在审核层显式做版本链。[3]
- **审计不等于审批**：Git history、per-memory history、episode provenance 都是事后可追踪；只有 commit 前的人类 gate 才是审批。
- **本地不等于离线**：Mem0、memU、Graphiti 的默认模型/embedding 常指云 API；部署时必须显式配置 Ollama/本地 embedding，并做断网验收。[1][10][14]
- **图谱过度工程**：Graphiti 会引入图数据库、抽取模型、实体去重、时态修正与运维；Fufu 当前 SQLite/FTS5/dense 路线不应为“可能有用”而更换主存储。
- **旧资料漂移**：Mem0 v2 自动 UPDATE/DELETE、Mem0 OSS Graph、旧 OpenMemory、memU LRU 等说法均可能来自旧版或二手资料；实现前应按当前 tag/commit 重新核对。

## 推荐优先级

| 优先级 | 机制 | 来源 | 对 Fufu 的价值 |
|---|---|---|---|
| P0 | 函数式候选变更集 + RemoveDoc 自定义语义 | LangMem | 直接支撑 dry-run / 待审 / 软删除 |
| P0 | prepare → review → commit、内容哈希幂等 | memU | 贴合只读增量扫描，隔离 LLM 与写库 |
| P0 | append-only 变更日志 + Markdown diff/Git 投影 | Letta | 审计、回滚、人工可读 |
| P0 | expiration 隐藏但不删、可恢复 | Mem0 | 与 EXPIRED 状态一致 |
| P1 | valid-time + learned-time + source lineage | Graphiti | 正确表达“何时为真”和“何时得知” |
| P1 | 有界访问热度软衰减 | Mem0 Platform 思路，自行实现 | 改善排序，不污染生命周期真值 |
| P2 | 轻量事实/实体关系表 | Graphiti 思路，SQLite 实现 | 在不上图数据库前获得 80% 时态收益 |
| 不采用 | Mem0 OSS ADD-only 作为唯一冲突策略 | Mem0 | 对可变事实会积累矛盾 |
| 不采用 | Graphiti 全套图后端 | Graphiti | 当前复杂度/运维/中文时态风险高 |
| 不采用 | 旧 OpenMemory 作为生产依赖 | OpenMemory archive | 已停止维护 |

## Sources

[1] https://github.com/mem0ai/mem0
[2] https://github.com/mem0ai/mem0/blob/main/LICENSE
[3] https://github.com/mem0ai/mem0/blob/main/docs/migration/oss-v2-to-v3.mdx
[4] https://github.com/mem0ai/mem0/blob/main/docs/platform/platform-vs-oss.mdx
[5] https://github.com/mem0ai/mem0/blob/main/docs/platform/features/memory-expiration.mdx
[6] https://github.com/letta-ai/letta-code
[7] https://github.com/letta-ai/letta-code/blob/main/LICENSE
[8] https://docs.letta.com/letta-code/memfs
[9] https://docs.letta.com/letta-agent/memory
[10] https://github.com/NevaMind-AI/memU
[11] https://github.com/NevaMind-AI/memU/blob/main/LICENSE.txt
[12] https://github.com/NevaMind-AI/memU/blob/main/docs/developer.md
[13] https://github.com/NevaMind-AI/memU/blob/main/docs/adr/0007-three-independent-memory-lines-wiki-graph.md
[14] https://github.com/getzep/graphiti
[15] https://github.com/getzep/graphiti/blob/main/LICENSE
[16] https://github.com/getzep/graphiti/blob/main/graphiti_core/prompts/extract_edges.py
[17] https://github.com/langchain-ai/langmem
[18] https://github.com/langchain-ai/langmem/blob/main/LICENSE
[19] https://github.com/langchain-ai/langmem/blob/main/docs/docs/concepts/conceptual_guide.md
[20] https://github.com/langchain-ai/langmem/blob/main/docs/docs/guides/extract_semantic_memories.md
[21] https://github.com/mem0ai/openmemory
[22] https://github.com/mem0ai/openmemory/blob/main/LICENSE
[23] https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/README.md
[24] https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/LICENSE
[25] https://github.com/getzep/graphiti/issues/1166
[26] https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/api/app/models.py
[27] https://github.com/mem0ai/openmemory/blob/main/openmemory-archive/api/app/routers/memories.py
