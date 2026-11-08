> **文档性质：历史技术选型记录**
> 本文从技能参考资料迁入。部分星数、模型排行和早期“直接复用”措辞具有时效性；现行边界以 `../PROJECT-OVERVIEW.md` 为准。

# 知识库技术调研明细（2026-08 委派子agent调研结果）

汇总自 3 轮并行子agent调研（deleg_cf022f42 / deleg_dcf3983d / deleg_1a77cf6f），结论均已交叉验证。用于「酒狐档案馆」项目的选型依据。

## 1. 向量数据库选型对比（2026-08）

| 产品 | 纯本地 | 内置Hybrid | Windows | 形态 | 结论 |
|---|---|---|---|---|---|
| LanceDB | ✅ | ✅ 原生(BM25+向量+RRF) | ✅ | 嵌入式 | ❌ FTS 底层 Tantivy，中文分词无解(tantivy-py issue #25) |
| Qdrant | ✅ | ✅ 最强(sparse+dense+RRF) | ⚠️ 二进制/Docker | 独立服务 | 次选，完整 hybrid 需跑进程 |
| sqlite-vec | ✅ | 手写 SQL RRF | ✅ wheel | 嵌入式 | ✅ 最终选择，注意版本坑(见下) |
| Chroma | ✅ | ❌ Cloud 专属(open-core陷阱) | ✅ | 嵌入式 | ❌ 本地无 BM25 |
| Milvus Lite | ✅ | ❌ Lite 无 FTS | ❌ 官方不含 | 嵌入式 | ❌ 排除 |
| Weaviate | ✅ | ✅ | ❌ 无 Win 二进制 | 嵌入式(Linux/Mac) | ❌ 排除 |
| FAISS/Hnswlib | ✅ | ❌ 纯 ANN | ✅ | 库 | ❌ 组件非数据库 |
| MySQL | ✅ | ❌ 无原生向量(要9.0+) | ✅ | 服务 | ❌ 项目采用 SQLite，无需独立服务 |
| RAGFlow | ✅ Docker | ✅ | ⚠️ Docker+WSL2 | 全家桶(9GB/4核16G) | ⚠️ 只当深度文档解析工具(扫描PDF/表格)，中文全文有bug(#9678) |
| GraphRAG | ✅ | ❌ 图检索 | ✅ | pip+CLI | ❌ 索引烧LLM token 4-6x，中文实体差(#596)，等LazyGraphRAG并入主库 |
| AnythingLLM | ✅ | ❌ 无(issue #4338) | ✅ | 桌面/Docker | ❌ 最多当问答UI前端 |
| Notion | ❌云 | ❌ | - | SaaS | ❌ 限流3rps+search只匹配标题+AI锁Business$20 |
| Obsidian | ✅ | ✅ 插件 | ✅ | 本地md | ✅ 加为人类可读浏览层 |

**sqlite-vec 版本坑**：原版 2025年中停更半年(issue #226)，2026-05 恢复；活跃延续 `sqliteai/sqlite-vector`(1,071★, vec0 兼容)。按 sqlite-vec 写、预留切换口子。

## 2. 中文 RAG 技术栈

- **Embedding**：**Qwen3-Embedding-0.6B(0.6B, 1024d, 32K ctx, C-MTEB 66.33, 中文更强, 需instruct前缀, Apache-2.0, Q4 仅639MB, CPU ~5.8K tok/s) 已反超 bge-m3，升级为主选**；bge-m3(568M, 1024d, 8192长文, dense+sparse+colbert 三路, C-MTEB ~63, **无需前缀接入最简单, 生态第一**) 保留为混合检索增强+降级后备；bge-base-zh-v1.5(109M/768d/**512 token 硬伤**) 已过时弃用；Qwen3-Embedding-4B(72.27)/8B(73.84, C-MTEB 榜首) 质量进阶备选；2026 无新中文霸主；CPU 推理全部可行（需按模型评估内存占用）
- **Embedding 部署(2026-08 定)**：**Top1=Ollama**(Windows exe, `ollama pull bge-m3`/`qwen3-embedding` 官方库直有, `/api/embed` 支持数组批量, Python `ollama` 包 3 行接入, 默认 Q8_0 安全档)；**Top2=sentence-transformers+ONNX**(零常驻服务、进程内、与 sqlite-vec 集成最自然, `SentenceTransformer("BAAI/bge-m3", backend="onnx")`)；TEI/vLLM/SGLang 无 Windows 原生排除；FastEmbed 无 bge-m3 级中文模型排除；llama.cpp 可跑但 qwen3 GGUF 有质量一致性风险；量化 Q8/int8 安全(保留95-99%)、Q4 以下慎用；模型切换只需改 Ollama model 名，sqlite-vec 侧零改动
- **本地 vs 云端(2026-08 定)**：**主体本地**（隐私一票否决：聊天记忆+电厂文档不出境；成本两者都 <¥15/1500万token 非决策因素）；DeepSeek 官方无 embedding API(只有 v4-flash/v4-pro chat)；云端只做补充=非敏感内容(公开教程/攻略/网页摘要)批量入库走**硅基流动 bge-m3 免费 API**(同款模型向量可混用同 collection)；评测集证明本地 recall 不够再 A/B 阿里 text-embedding-v4(¥0.5/M)/智谱 embedding-3(¥0.5/M, 新人送2000万token)；架构做成可插拔抽象(统一接口本地/云端切换, 维度统一1024)；详见 [`EMBEDDING-OPTIONS-2026-08.md`](EMBEDDING-OPTIONS-2026-08.md)
- **分块**：中文标点递归分块(`\n\n`→`\n`→`。！？；`→`，`)、300-500字、overlap 10-20%、markdown按标题/日期切、日期/标题入 metadata；中文1字≈1token
- **融合**：RRF 事实标准 k=60，score=Σ1/(k+rank)；hybrid vs dense-only recall@10 78%→91%
- **Reranker**：gte-multilingual-reranker-base(0.3B, C-MTEB-R 74.08 小模型最强, CPU首选)；bge-reranker-v2-m3(0.6B, 生态最成熟)；rerank 是单步最大质量提升(+5~15 NDCG)
- **中文关键词坑**：Qdrant 内置 qdrant/bm25 对中文差(issue #8014)，必须 jieba 预分词；FTS5 默认 unicode61 把整句当一个 token——必须 trigram(零依赖)或 jieba 预分词(词级)或 wangfenjin/simple(词级+拼音, Windows DLL)

## 3. 现成开源项目复用（别从零写）

| 项目 | Star | 用途 |
|---|---|---|
| sqliteai/sqlite-rag | 77★；**仓库无 LICENSE（GitHub license:null，2026-08 已核）** | 仅可 clean-room 借鉴 FTS5+向量+RRF、多格式解析、token-aware 分块等架构思路；不可复制、改编或分发其源码 |
| PatentLLM 200行 | 博客 | RRF SQL 标准写法(CTE+ROW_NUMBER+1/(k+rank))，直接抄 |
| giuseppeferretti/sqlite-rag-mcp | 0★ | 栈100%一致+Ollama本地embedding+增量索引(SHA-256)+挂掉自动降级 |
| wdbhrg/local-rag-knowledge-base | 0★ | 中文原生+Windows一键启动+FastAPI(SSE)+Streamlit+Cross-Encoder重排 |
| flowing-abyss/obsidian-hybrid-search | 90★ | 最成熟FTS5+sqlite-vec+RRF：标题/别名加权BM25、fuzzy trigram、watcher增量重索引 |
| AgentGameLab/mneme | 6★ | 唯一带中文分词的参考(README.zh-CN) |
| staleness | - | **零LLM纯Python新鲜度linter**(ST001-ST007: 过期chunk/坏时间戳/age skew)，挂ingest |
| memU (NevaMind-AI) | - | 文件即记忆+自进化(高频晋升/LRU淘汰)+双模检索+**官方支持Hermes**，Apache 2.0 |
| inite-brain-service | - | bitemporal双时态知识图谱(事实可过期/时间旅行/GDPR遗忘)，AGPL/Node偏重 |
| YourMemory | - | Ebbinghaus遗忘曲线衰减，⚠️CC-BY-NC非商用 |

## 4. RAG 评估与优化分级（2026）

**第一批值得加（低成本高收益）**：
1. 自建评测集(30~100条真实查询+答案文档ID)+Recall@k/MRR/NDCG脚本——一切优化前提，零LLM成本
2. 父子分块(parent_id：小块检索、父块生成)
3. Metadata过滤(日期/标签/来源列过滤)
4. 引用溯源/grounding(chunk带doc_id+prompt逐句标来源，Perplexity实证-61%幻觉引用)
5. 多轮查询改写(对话指代消解成独立查询，几十行)
6. jieba自定义词典(电厂术语/游戏名词/VRC专有名词)
7. 中文embedding/reranker确认(bge-m3或Qwen3 + bge-reranker-v2-m3)

**第二批可选**：RAGAS离线评估(TestsetGenerator造测试集，judge用Qwen系且与生成模型不同族，只做相对A/B不信绝对分)、HyDE(仅低置信查询)、时间衰减加权

**明确不做(过度工程)**：query routing、RQ-RAG问题分解(延迟16s+)、LLMLingua压缩、GraphRAG、TruLens常驻监控

## 5. Hermes 接入要点

- MCP 配置在 config.yaml `mcp_servers` 键，stdio(command+args) 或 HTTP(url)；`hermes mcp add/test/catalog/configure`；`/reload-mcp` 热重载；tools.include/exclude 白名单=少注入关键；工具命名 `mcp__<server>__<tool>`
- External memory providers 8个：Holographic(纯本地SQLite)/OpenViking(分层L0/L1/L2)/Hindsight(本地PG)/Mem0(OSS本地)/ByteRover(本地CLI)/Supermemory(自托管)/Honcho(自托管)/RetainDB+Memori(纯云$20不选)；**同时只能激活一个**；要少注入需显式配成 tools-only 模式
- 数据落盘：memories/MEMORY.md+USER.md(§分隔)、skills/**/SKILL.md、state.db(SQLite WAL, sessions/messages表+FTS5三套索引含trigram CJK)
- 子agent继承父工具集(terminal/curl/read_file等)，被禁delegation/clarify/memory写/code_execution——可 curl 直调本地 FastAPI
- 自研 Memory Provider：继承 MemoryProvider ABC，钩子 system_prompt_block/prefetch/sync_turn/on_session_end/on_pre_compress/on_memory_write，存 plugins/memory/<name>/

## 6. 中文社区参考教程

- 掘金《2026开源AI记忆方案全景图》(juejin.cn/post/7624349876398538761)
- 天盘《知识半衰期问题》(tianpan.co/zh/blog/2026-05-06-rag-knowledge-freshness-tiered-reindex-staleness)——新鲜度三层架构+删除作为一等事件
- 小企鹅《AI Agent 記憶系統實作教學》(penchan.co/ai/agent/ai-agent-memory-guide)——文件系统记忆5原则：位置即索引、单文件≤200行、主题拆分

## 7. 调研注意（给未来的酒狐）

- 中文检索词被 Bing 本地化语义干扰时，改用英文/中英混合查询绕过
- 子agent总结被截断时：`tr -d '\r' < subagent-summary-*.txt | grep -v '^$'` 读取（UTF-8+CRLF，read_file 会误判 binary）
