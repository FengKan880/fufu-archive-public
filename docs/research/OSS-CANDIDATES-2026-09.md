> **文档性质：公开项目调研**
> 这是 2026-09 已完成的一轮候选项目调研，不代表持续更新后的最新生态；采纳结论以 `../PROJECT-OVERVIEW.md` 为准。

# Fufu Archive：2025–2026 活跃开源本地知识库 / RAG / AI Memory 候选

> 核验时间：2026-09-06。活跃度取 GitHub API 的 `pushed_at` 与官方 Releases；许可证以仓库根目录 `LICENSE` 为准，而不是 README 徽章。刻意排除已知的 `sqlite-rag`、`mneme`、`obsidian-hybrid-search`。

## 结论先行

- **最值得马上读源码：** `mcp-memory-service`（Python + Apache-2.0，sqlite-vec、MCP/REST、降级和维护链路）与 `Vault Agent Memory`（Python + Apache-2.0，候选→审阅→提升→过期/回滚的治理模型）。
- **最值得抄检索设计：** `QMD`（SQLite FTS5 + 向量 + typed query expansion + 加权 RRF + position-aware rerank）。它是 TypeScript，适合移植算法与评测思路，不适合把实现直接塞进 Python。
- **最接近“本地 SQLite 混合检索 + Hermes 生命周期钩子”的现成产品：** `MemOS` 的 `apps/memos-local-plugin`。可作为插件级对照实验，但不要替换 Fufu Archive 已完成的 Python 内核。
- **最值得补 P3/P4 的不是另一个向量库，而是三类机制：** Engram 的稳定 `topic_key`/会话交接，Vault 的审阅与回滚，remindb 的 token-budget / delta / 温度衰减。

## 8 个新候选

| 项目 | 当前技术贴合度 | 许可证（已核 LICENSE） | 2025–2026 活跃度证据 | 最值得 Fufu Archive 借鉴 | 能否直接复用代码 |
|---|---|---|---|---|---|
| **Basic Memory** — [GitHub][1] | Python；Markdown 为真源；本地默认 SQLite；全文+向量 hybrid；FastEmbed；可选本地 cross-encoder rerank；MCP stdio/HTTP，并已有 Hermes 集成目录。 | **AGPL-3.0**。[LICENSE][2] | 2026-08-25 发布 v0.23.2，2026-09-06 仍有 push；非归档。[Releases][3] | 文件↔索引双向一致性、`doctor`/reindex、MCP tool annotations、progressive tool discovery、按项目隔离、搜索结果直接携带 matched chunk。 | **技术上可集成，法律上不建议拷贝进当前项目**：除非 Fufu 接受整体/网络服务层 AGPL 义务。建议只做 clean-room 行为对照和接口设计参考。 |
| **mcp-memory-service** — [GitHub][4] | Python；本地 SQLite 后端 + `sqlite-vec`；本地 ONNX embedding；stdio MCP、Remote MCP、REST、SSE；知识图谱、合并、衰减、consolidation。 | **Apache-2.0**。[LICENSE][5] | 2026-09-05 发布 v11.11.0 且同日 push；发布频密。[Releases][6] | sqlite-vec 迁移/维度防护、删除与 rowid 坑、embedding backend 自检、ONNX→fallback、两阶段 `explore → detail`、响应字符预算、MCP/HTTP 共用服务层、维护脚本。 | **可以，优先级最高。** 与 Fufu 同为 Python，Apache-2.0 允许抽取模块/测试；保留版权与许可证，并检查是否有 NOTICE。先挑小模块和测试，不整仓搬入。 |
| **QMD** — [GitHub][7] | TypeScript/Bun/Node；SQLite + FTS5 + 向量；本地 GGUF；stdio/HTTP MCP。检索链为 typed expansion（`lex/vec/hyde`）→双路→RRF→Qwen3 reranker。 | **MIT**。[LICENSE][8] | 2026-08-16 发布 v2.8.3，2026-08-18 push；项目始于 2025-12，迭代快。[Releases][9] | 原查询双路且权重×2、RRF k=60、top-rank bonus、候选 30、位置感知融合（头部更信 RRF、尾部更信 reranker）；900-token 智能断点+15% overlap；collection/path context；HTTP Origin/Host 防 DNS rebinding。 | **法律可复用、工程上不宜直接复制。** TS 与 Python 栈不匹配；应按 MIT 归属要求移植算法/测试向量，或把 QMD 当外部基准，不引入第二套运行时。 |
| **MemOS / memos-local-plugin** — [GitHub][10] | 主体是 memory OS；其本地插件明确为 SQLite + FTS5 + vector hybrid，100% on-device，并有 Hermes/OpenClaw/DeepSeek Harness 适配、viewer、去重、分层技能演化。 | **Apache-2.0**。[LICENSE][11] | 2026-09-03 发布 v2.0.33，2026-09-01 发布 local-plugin v2.0.18，2026-09-03 push。[Releases][12] | L1 traces / L2 policies / L3 world model / crystallized skills 分层；hook 前召回、成功后捕获、fail-open；viewer；多 agent 共享/隔离；反馈纠错。 | **可复用但建议仅插件级/适配器级。** local plugin 主要是 Node/TS，与 Fufu 内核不同；Apache 允许复制，但更适合作为 Hermes P3 对照与 hook 行为来源。 |
| **Engram** — [GitHub][13] | 单 Go binary + 单 SQLite + FTS5；MCP stdio、HTTP、CLI、TUI；无向量路，强调“精选记忆而非 transcript sink”。 | **MIT**。[LICENSE][14] | 2026-09-06 发布 v2.0.0-rc.6 并 push；v2 仍是 RC，但维护强。[Releases][15] | `current_project → context/search → detail/timeline → save → session_summary` 操作契约；稳定 `topic_key` 更新同一主题，避免竞争记忆；Git Sync；doctor；跨 compaction 交接。 | **许可允许，技术上不直接。** Go 实现不适合拷进 Python；可 clean-room 复刻 MCP 工具语义、topic key 与 session handoff 流程。 |
| **Awareness Local** — [GitHub][16] | Node；Markdown 真源 + SQLite FTS5 + optional local embedding；RRF hybrid；MCP daemon + dashboard；两阶段 summary→full progressive disclosure。 | **实际 LICENSE 是 MIT**。[LICENSE][17] **但 README/徽章声称 Apache-2.0，仓库元数据也曾显示 MIT，存在文档冲突。** | 2026-04-28 发布 v0.11.6，最后 push 2026-05-04；仍属 2026 活跃，但相较其余候选较安静。[Releases][18] | Markdown 可重建索引、知识卡（decision/solution/risk）、两阶段召回、纯检索 LongMemEval harness 与 vector/BM25/RRF 消融。 | **暂缓直接复制。** 根 LICENSE 通常是支配文本，但许可证标注冲突应先向上游确认；可先按 clean-room 方式复刻测试与两阶段召回。 |
| **Vault Agent Memory** — [GitHub][19] | Python；SQLite/Markdown 真源；CLI/MCP/Gateway/OpenAPI；后端可插拔。检索并非强项，核心是 governed memory lifecycle。 | **Apache-2.0**。[LICENSE][20] | 2026-07-12 发布 v0.10.2，2026-08-27 push；Public Beta / Developer Preview。[Releases][21] | `propose → review → promote → bounded read → rollback → audit`；来源范围、敏感度、信任、可见性、时效；同机共享而跨机只提交 candidate；日报审阅；绝不自动硬删；search QA 明确区分 retrieval source-hit 与最终 QA。 | **可以，优先级很高。** Python + Apache 与 Fufu 匹配；适合直接复用数据模型、迁移、MCP schema 和治理测试，但语义检索部分仍在演进，不应替换现有检索器。 |
| **remindb** — [GitHub][22] | 单 Go binary + 单 SQLite；FTS5；结构化 `MemoryTree`、wiki-link graph、MCP tools/resources；没有向量路。 | **MIT**。[LICENSE][23] | 2026-06-06 发布 v0.3.12，2026-08-03 push。[Releases][24] | 每节点 temperature（读升温/随时间衰减）、冷节点提示压缩而不删除；快照+per-node diff；`MemoryDelta` 增量恢复；所有 fetch/search 接受 token budget；关系遍历与 source anchor；Hermes MemoryProvider 插件。 | **许可允许，技术上不直接。** Go→Python 需移植；推荐借鉴 schema/行为测试，尤其 token-budget、delta cursor、pin/unpin、rollback，不搬存储内核。 |

## 对 Fufu Archive 的落地优先级

1. **P3 立即做：**
   - 从 `mcp-memory-service` 对照 stdio/HTTP 共用 service 层、sqlite-vec 维度/迁移/删除回归测试、`explore → detail` 响应预算。
   - 从 Engram 引入稳定 `topic_key` 和 session summary/handoff contract，避免自动会话提炼不断生成重复事实。
   - 从 Vault 引入 candidate-first：自动提炼先进入待审层；只有低风险、来源明确、非冲突项才能提升 ACTIVE。
2. **P4 生命周期与运维：**
   - Vault 的审计/回滚/敏感度/来源范围 + remindb 的 temperature/pin/delta；与 Fufu 现有 `ACTIVE → EXPIRED → DELETE/renew/PERMANENT` 合并，而不是另起状态机。
   - 加 `doctor`：文件/DB、FTS 行数、向量维数、孤儿 rowid、embedding 模型指纹、migration 版本一致性。
3. **P5 检索增强：**
   - 把 QMD 当实验规格：在现有 44-query eval 上依次验证“原查询×2”“top-rank bonus”“位置感知 rerank blend”，**一次只改一个变量**；不要照搬整条链后再归因。
   - Awareness 的 benchmark 只作为 harness 参考；其 README 数字是项目方自报，不应当成 Fufu 的外部质量证明。
4. **不建议：** 为了借鉴 MemOS/QMD 引入 Node 作为核心依赖；为了 Basic Memory 的现成功能把 Fufu 改成 AGPL；用 Engram/remindb 的 FTS-only 内核替换已验证的 Qwen3 dense 路。

## 复用边界

- **可直接复制/改造：** Apache-2.0/MIT 项目在满足版权、许可证文本、NOTICE（若有）与修改说明后可复用；但语言栈不匹配时，“许可可复用”不等于“工程上值得复制”。
- **仅 clean-room：** Basic Memory（当前 Fufu 若不采用 AGPL）；Awareness Local 在许可证声明冲突解决前也按此处理。
- **外部进程复用：** QMD、Engram、remindb、MemOS local plugin 都可独立运行或作为对照基准，但 Fufu 已有稳定 Python+SQLite 内核，默认不应形成双写双真源。

## Sources

[1] https://github.com/basicmachines-co/basic-memory — Basic Memory
[2] https://github.com/basicmachines-co/basic-memory/blob/main/LICENSE — Basic Memory LICENSE
[3] https://github.com/basicmachines-co/basic-memory/releases — Basic Memory releases
[4] https://github.com/doobidoo/mcp-memory-service — mcp-memory-service
[5] https://github.com/doobidoo/mcp-memory-service/blob/main/LICENSE — mcp-memory-service LICENSE
[6] https://github.com/doobidoo/mcp-memory-service/releases — mcp-memory-service releases
[7] https://github.com/tobi/qmd — QMD
[8] https://github.com/tobi/qmd/blob/main/LICENSE — QMD LICENSE
[9] https://github.com/tobi/qmd/releases — QMD releases
[10] https://github.com/MemTensor/MemOS — MemOS
[11] https://github.com/MemTensor/MemOS/blob/main/LICENSE — MemOS LICENSE
[12] https://github.com/MemTensor/MemOS/releases — MemOS releases
[13] https://github.com/Gentleman-Programming/engram — Engram
[14] https://github.com/Gentleman-Programming/engram/blob/main/LICENSE — Engram LICENSE
[15] https://github.com/Gentleman-Programming/engram/releases — Engram releases
[16] https://github.com/edwin-hao-ai/Awareness-Local — Awareness Local
[17] https://github.com/edwin-hao-ai/Awareness-Local/blob/main/LICENSE — Awareness Local LICENSE
[18] https://github.com/edwin-hao-ai/Awareness-Local/releases — Awareness Local releases
[19] https://github.com/zycaskevin/Vault-Agent-Memory — Vault Agent Memory
[20] https://github.com/zycaskevin/Vault-Agent-Memory/blob/main/LICENSE — Vault Agent Memory LICENSE
[21] https://github.com/zycaskevin/Vault-Agent-Memory/releases — Vault Agent Memory releases
[22] https://github.com/radimsem/remindb — remindb
[23] https://github.com/radimsem/remindb/blob/main/LICENSE — remindb LICENSE
[24] https://github.com/radimsem/remindb/releases — remindb releases
