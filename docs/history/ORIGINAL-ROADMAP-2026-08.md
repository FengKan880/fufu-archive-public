> **文档性质：历史方案快照**
> 本文保留项目早期总体构想，其中路径、许可证判断、阶段状态和自动生命周期设想可能已过时。当前事实以 `../PROJECT-OVERVIEW.md` 为准。

# 🦊 酒狐档案馆 —— 本地分类知识库方案

> 状态：**方案执行中**（2026-09-07 更新）
> 当前阶段：**P0–P2 ✅ 完成；P3 核心审核/审计/MCP 闭环 ✅ 完成；P4 待实施。** 检索基线：FTS5 recall@10=0.977；dense-prefix recall@10=1.000 / MRR@10=1.000（2026-08-06，Ollama qwen3-embedding:0.6b + sqlite-vec）。

---

## 一、需求总纲

本项目定位为**本地优先**的知识库，核心需求：

1. **少记忆提示词注入**——原生 MEMORY.md/USER.md 只留「热数据 + 摘要链接」，全文外置
2. **全文存档**——skill/memory 全文快照、会话提炼、日报/月报/缺陷复盘全文
3. **领域知识**——支持多种公开知识与经授权的领域文档
4. **会话内自动分块分类**——一个会话聊多个话题要自动拆开归位
5. **Advanced RAG**——向量 + 关键词混合检索（中文为主）
6. **完整出入库 API**——Agent 与用户上传的授权文档均可管理
7. **生命周期管理**——更新、遗忘、失效打标不删除、用户决策
8. **备份容灾**——可配置的独立备份与恢复验证

---

## 二、技术选型（2026-08 调研结论）

### 存储：SQLite 全家桶（最终选择）

```
kb.db 单文件
├── FTS5 + jieba 预分词    → 关键词路（BM25，中文词级）
├── sqlite-vec            → 向量路（dense embedding）
└── RRF 融合（k=60）       → 双路融合（两条 CTE 一条 SQL）
```

**⚠️ sqlite-vec 版本注意（2026-08 新情报）**：原版 2025 年中曾停更半年（[issue #226](https://github.com/asg017/sqlite-vec/issues/226)），2026-05 恢复；活跃延续是 **sqliteai/sqlite-vector**（1,071★，`vec0` 语法兼容）。**按 sqlite-vec 写、预留切换 sqlite-vector 的口子。**

### 现成项目复用（2026-08 调研新情报）

| 项目 | Star | 用途 | 匹配度 |
|---|---|---|---|
| **[sqliteai/sqlite-rag](https://github.com/sqliteai/sqlite-rag)** | 77★ | **主骨架**：FTS5+向量+RRF 混合检索、PDF/DOCX/MD 多格式、token-aware 分块、模型可换（本地 GGUF/HF）、PyPI 可装 | ⭐⭐⭐⭐⭐ 直接用检索核心+分块，补 FastAPI 与 jieba |
| **[PatentLLM 200 行实现](https://media.patentllm.org/blog/database/hybrid-rag-200-lines)** | 博客 | **RRF 查询标准写法**（CTE + ROW_NUMBER + 1/(k+rank)），直接抄 SQL | ⭐⭐⭐⭐⭐ |
| **[giuseppeferretti/sqlite-rag-mcp](https://github.com/giuseppeferretti/sqlite-rag-mcp)** | 0★ | 栈 100% 一致 + Ollama 本地 embedding + 增量索引（SHA-256）+ **Ollama 挂了自动降级** → 照抄降级逻辑 | ⭐⭐⭐⭐ |
| **[wdbhrg/local-rag-knowledge-base](https://github.com/wdbhrg/local-rag-knowledge-base)** | 0★ | **中文原生**、Windows 一键启动、FastAPI(SSE)+Streamlit+Cross-Encoder 重排 → 参考其 SSE 问答形态 | ⭐⭐⭐⭐ |
| **[flowing-abyss/obsidian-hybrid-search](https://github.com/flowing-abyss/obsidian-hybrid-search)** | 90★ | 最成熟 FTS5+sqlite-vec+RRF：标题/别名加权 BM25、fuzzy trigram、watcher 增量重索引 → **RRF 融合与加权方案值得抄**（也服务 Obsidian 前端） | ⭐⭐⭐⭐ 参考 |
| **[AgentGameLab/mneme](https://github.com/AgentGameLab/mneme)** | 6★ | **唯一带中文分词的参考**（README.zh-CN）→ 看它怎么做 FTS5 中文 tokenize | ⭐⭐⭐ 参考 |

**结论**：没有"四合一完整成品"可整包 fork；**最务实 = sqliteai/sqlite-rag 做骨架 + PatentLLM 抄 RRF SQL + sqlite-rag-mcp 抄降级 + mneme 抄中文分词 + 自写 FastAPI 层**，预计省 60-70% 检索/分块/融合工作量。

### 记忆/生命周期现成方案（新情报）

| 项目 | 亮点 | 用途 |
|---|---|---|
| **[memU](https://github.com/NevaMind-AI/memU)** | 「记忆即 Wiki 文件」核心仅 500 行、Apache 2.0、三层架构、**自进化（高频晋升/LRU 淘汰）**、双模检索、**官方支持 Hermes** | ⭐⭐⭐⭐⭐ 记忆层参考/可选接入 |
| **[staleness](https://github.com/asmitdash/staleness)** | **零 LLM、纯 Python 确定性新鲜度 linter**（ST001-ST007：过期 chunk/坏时间戳/age skew） | ⭐⭐⭐⭐⭐ 完美契合"少 LLM 注入"，挂 ingest 流水线 |
| [inite-brain-service](https://github.com/inite-ai/inite-brain-service) | bitemporal 双时态知识图谱：事实可过期、as-of 时间旅行、GDPR 遗忘 | 参考（AGPL/Node，重） |
| [YourMemory](https://github.com/sachitrafa/YourMemory) | Ebbinghaus 遗忘曲线衰减，自动衰减+修剪 | 参考（⚠️ CC-BY-NC 非商用） |
| [LocalRecall](https://github.com/mudler/LocalRecall) | 100% 本地 Go 单二进制 + WebUI + REST/MCP + 外部源自动刷新 | 参考（极轻量） |

**记忆层决定**：核心检索仍自建（SQLite 全家桶），**memU 的"文件即记忆+自进化淘汰"思想融入生命周期设计**（其 LRU 淘汰 ≈ 项目的 180 天失效设想），staleness 的 ST 规则挂进入库管线做新鲜度检查。

**为什么不用其他**：

| 方案 | 结论 | 原因 |
|---|---|---|
| MySQL | ❌ 不用 | 本项目采用嵌入式 SQLite，避免额外数据库服务 |
| LanceDB | ❌ | 原生 hybrid 香，但 FTS 底层 Tantivy 中文分词无解 |
| Chroma | ❌ | hybrid 是 Cloud 专属（open-core 陷阱） |
| Milvus Lite / Weaviate | ❌ | Windows 不支持 / Lite 无 FTS |
| RAGFlow | ⚠️ 只当工具 | 重（Docker 全家桶/4核16G）；中文全文有 bug；**按需起 Docker 做深度文档解析（扫描 PDF/表格）导出 chunk 回灌** |
| GraphRAG | ❌ 观察 | 索引烧 LLM token 4-6x；中文实体抽取差；等 LazyGraphRAG 并入主库 |
| AnythingLLM | ❌ | 无 hybrid 检索（issue #4338 挂一年）；最多当"给家人的问答 UI"前端 |
| Notion | ❌ | 限流 3rps；search 只匹配标题；AI 锁 Business $20/月；强制 OAuth |
| Obsidian | ✅ 加前端 | 完全本地、文件即 API、零限流；当**人类可读/可编辑的档案浏览层** |

### Embedding 模型

- **主选升级（2026-08-06）：Qwen3-Embedding-0.6B**（0.6B/1024d/32K ctx，C-MTEB 66.33 中文反超 bge-m3，Apache-2.0，Q4 仅 639MB，CPU ~5.8K tok/s，查询侧需 instruct 前缀）
- **bge-m3 保留**（568M/1024d/8K，无需前缀接入最简单、生态第一，作混合检索增强+降级后备）
- ~~bge-base-zh-v1.5~~ 弃用（512 token 硬伤，长文档截断丢语义）
- Reranker（P5）：bge-reranker-v2-m3 / gte-multilingual-reranker-base（0.3B）
- **部署（2026-08 定）**：Top1=**Ollama**（Windows exe，一条命令拉模型，`/api/embed` 批量，默认 Q8 安全档，切模型零成本）；Top2=**sentence-transformers+ONNX**（零常驻服务、进程内、与 sqlite-vec 集成最自然）；TEI/vLLM/SGLang 无 Windows 原生不用；FastEmbed 无 bge-m3 级中文模型不用
- **本地 vs 云端（2026-08 定）**：**主体本地**（隐私一票否决：私密来源默认不离开本地环境；成本非决策因素）；云端只做补充=非敏感内容走**硅基流动 bge-m3 免费 API**（同款模型向量可混用）；DeepSeek 官方无 embedding API；评测集证明本地 recall 不够再 A/B 阿里 text-embedding-v4/智谱 embedding-3
- 架构做成可插拔抽象（本地/云端一键切换），向量维度统一 1024
- 详见迁入后的 [`../research/EMBEDDING-OPTIONS-2026-08.md`](../research/EMBEDDING-OPTIONS-2026-08.md)

---

## 三、知识分类架构

```
kb.db
├── collection: hermes_ops        # 酒狐运行档案
│   ├── memory_snapshot           # MEMORY.md/USER.md 全文快照（带版本时间戳）
│   ├── daily_report              # 日报
│   ├── monthly_report            # 月报
│   ├── retrospective             # 缺陷复盘
│   ├── skill_snapshot            # SKILL.md 全文快照
│   └── session_log               # 会话提炼产物（非原始流水账）
│
├── collection: world_knowledge   # 现实世界知识
│   ├── domain: vrc               # VRChat: 发展史/社区风气/代理设置
│   ├── domain: gaming            # CSGO 等游戏对比/模式/社区
│   ├── domain: plant_work        # 领域文档（文件上传入库）
│   ├── domain: tools             # 工具/软件现状
│   └── domain: ...               # 按需扩展
│
└── 每条 chunk 元数据:
    ├─ collection / domain / topic
    ├─ source（session_id / 文件路径 / 上传者）
    ├─ date + expiries（时效标记: stable | volatile）
    ├─ confidence / verified_by
    ├─ last_accessed / access_count（调用频率追踪）
    └─ parent_id（父子分块）
```

### 两类知识行为差异

| 维度 | hermes_ops（运行档案） | world_knowledge（现实知识） |
|---|---|---|
| 更新 | 追加为主（每天新增日报）+ 技能月度复盘 | **更新+遗忘**（会过时） |
| 失效判定 | **月度复盘人工判定**（技能还用不用）+ 90天无检索辅助 | **自动**：volatile 条目 180 天无调用 → EXPIRED |
| 来源 | 酒狐运行自动产生 | 会话提炼 + 用户上传 + 酒狐调研 |
| 时效 | 永久存档 | stable（历史事实）vs volatile（现状，可过期） |

### 会话内知识自动分块

```
会话原始文本 → 按话题/时间切分 → 分类器
  ├─ "怎么操作"（流程/技能）→ hermes_ops
  ├─ "世界是什么样"（事实/历史/对比/风气）→ world_knowledge + domain 细分
  → 分块（300-500字/overlap 15%/标点边界）
  → 各自入库（带 domain/topic/时效 metadata）
```

---

## 四、知识生命周期状态机

```
                    ┌─────────────────────────────┐
                    │          PERMANENT          │  ♾ 永久(用户转)
                    └──────────────▲──────────────┘
                                   │ 用户选"转永久"
   ┌──────────┐   无调用超期/复盘判定   │ 用户选"恢复"
   │  ACTIVE  │──────────────────►┌───┴─────────┐
   │ (活跃)   │                   │  EXPIRED    │
   │ 每次检索  │◄──────────────────│ (失效标记)  │
   │ 更新热度  │   恢复计时(renew)  │ 检索降权不回 │
   └──────────┘                   └───┬─────────┘
        │ 新版本入库(supersede)        │ 用户选"删除"
        ▼                             ▼
  旧版 → EXPIRED                   DELETE(最终, 先入回收站30天)
```

| 状态 | 行为 | 触发 |
|---|---|---|
| **ACTIVE** | 正常返回检索；命中更新 last_accessed + access_count | 入库/恢复/更新 |
| **EXPIRED** | 检索降权不返回（数据永久保留）；列入月度报告 | ① world 180 天无调用 ② 技能月度复盘判定不用 ③ 被新版本取代 |
| **PERMANENT** | 永不因"无调用"失效，内容过时照样 supersede | 用户手动转（基石事实） |
| **DELETE** | 物理删除（先入回收站表留 30 天） | **仅用户决定** |

### 入库相似融合

```
POST /kb/ingest:
  1. 先跑相似检索（向量+关键词, 阈值 ~0.85）
  2. 命中相似条:
     ├─ 实质相同 → 融合（合并来源/引用数/更新 last_updated）
     ├─ 内容更新 → 新版 ACTIVE + 旧版 EXPIRED（supersede）
     └─ 只是相关 → 正常入库 + 关联引用
  3. 无相似 → 全新入库
```

---

## 五、数据库维护规划

- 确定性脚本承担同步、统计、候选失效扫描和维护预览；模型只参与可选提炼或报告。
- 执行频率、任务名称和通知渠道由部署配置决定，本文不记录个人任务安排。
- 删除与清理必须先生成预览并由操作员批准；数据库生命周期策略与实际调度启用状态分开验收。
- 报告可包含失效候选、已更新条目、重复候选和待审操作，不自动执行高风险变更。

---

## 六、数据库备份与恢复设计

- 使用 SQLite 一致性备份；数据库、向量配置和必要项目元数据形成可校验的恢复单元。
- 数据目录与备份目录由部署配置提供，不假设具体盘符、硬盘型号或容量。
- 备份策略可选择独立存储介质；保留期、加密方式和频率由部署方确定。
- 恢复演练在隔离位置执行，验证数据库完整性、文档可检索性与向量配置；未验证恢复前不删除来源。
- 宿主 Agent 的身份、凭据和私人文件不属于可公开项目资料，也不纳入代码仓库。

---

## 七、RAG 评估优化策略（按值得加分级）

**第一批（低成本高收益，进 P1/P2）**
1. 自建评测集（30~100 条真实查询 + 答案文档 ID）+ Recall@k/MRR/NDCG 脚本
2. 父子分块（chunks 表 parent_id：小块检索、父块生成）
3. Metadata 过滤（日期/标签/来源列过滤）
4. 引用溯源（chunk 带 doc_id + prompt 逐句标来源）
5. 多轮查询改写（对话指代消解成独立查询）
6. jieba 自定义词典（电厂术语/游戏名词/VRC 专有名词）

**第二批（可选按需）**：RAGAS 离线评估（TestsetGenerator 造测试集）、HyDE（仅低置信查询）、时间衰减加权

**明确不做（过度工程）**：query routing、问题分解 agent、LLMLingua 压缩、GraphRAG、TruLens 常驻监控

---

## 八、API 设计

```yaml
POST /kb/upload        # multipart 文件上传（.md .txt .pdf .docx .xlsx .csv，自动解析分类）
POST /kb/ingest        # 文本/结构化直接入库（酒狐/子agent用，自动分块/分类/融合）
POST /kb/search        # 混合检索（可限定 collection/domain/topic/metadata）
POST /kb/update        # 更新（新版 supersede 旧版）
DELETE /kb/delete      # 删除（先入回收站）
POST /kb/sync          # 从 Hermes 增量同步（memories/skills/会话提炼）
POST /kb/review        # 暂存审核（入库前过目）
POST /kb/expire-scan   # 手动触发遗忘扫描
```

- 只监听 127.0.0.1 + token 鉴权（从 .env 读）
- 入库做注入模式扫描（复用 Hermes memory 扫描逻辑）
- Hermes MCP 注册：`kb_search/kb_add/kb_update/kb_list`（tools.include 白名单 → 少注入）

---

## 九、实施阶段

| 阶段 | 内容 | 产出 |
|---|---|---|
| **P0 基建** | 建 kb.db（FTS5+jieba+sqlite-vec）、FastAPI 骨架、鉴权、目录 | 服务能启动、API 通 |
| **P1 核心检索** | 关键词路+向量路+RRF，按 collection/domain 过滤；**✅ 评测集已立（2026-08-06：eval/ 44查询+32语料，FTS5 基线 recall@10=0.977/MRR@10=0.928，驱动 3 轮改进：AND全词→停用词过滤→OR+BM25）**；剩余：embedding 部署（Ollama）+ 向量路 + RRF 融合 | kb_search 可用（中文验证） |
| **P2 入库管线** | 分块/metadata/parent_id、kb_ingest、kb_upload（领域文件解析）**+ 多轮查询改写（从P5提前）** | 全文入库+增量 |
| **P3 Hermes 接入** | ✅ MCP 注册；✅ 候选审核（pending→approved/rejected→committed）；✅ revision、审计、并发保护、可选 topic_key 去重与显式版本关联；会话 collector 保持候选优先、未开启自动写库 | Hermes 可通过 MCP 检索/审核/提交；P3 细节与验收：`.\docs\P3-CANDIDATE-REVIEW.md` |
| **P4 自动化+备份（历史设想）** | 可配置维护、备份恢复与调度 | 仅历史规划，不代表已实现或启用；以最新路线与出口审查为准 |
| **P5 增强** | reranker、RAGAS、HyDE、Obsidian 前端 | 检索质量提升 |

---

## 十、待拍板决策点

1. ~~存储路线~~ → ✅ SQLite 全家桶（MySQL 确认不用）
2. **数据目录**：由部署配置指定。
3. **备份目录**：由部署配置指定，建议与主数据故障域隔离。
4. **Obsidian 前端**：规划中的可选功能（人类可读/可编辑浏览层）
5. 其他细节：报告通知渠道→ 并入 P4 定期任务一起定

---

## 附：关键参考来源

- Hermes 记忆系统：https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
- Hermes MCP：https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- Hermes Memory Providers：https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers
- sqlite-vec hybrid（RRF 模板）：https://alexgarcia.xyz/blog/2024/sqlite-vec-hybrid-search/index.html
- bge-m3：https://huggingface.co/BAAI/bge-m3
- RRF 论文：https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf
- C-MTEB：https://github.com/FlagOpen/C-MTEB
