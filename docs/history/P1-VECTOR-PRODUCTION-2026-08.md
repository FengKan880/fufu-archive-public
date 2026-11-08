> **文档性质：P1 实施记录**
> 本文保留向量路接入生产代码的设计与验收背景。

# 向量路生产接入（2026-08-06，P1 落地）

Ollama 部署 + 评测基线完成后，把 dense 向量检索正式接入 `src/fufu_archive` 生产代码。设计目标：**可插拔 + 优雅降级 + 评测驱动**。

## 代码结构

```
src/fufu_archive/
├── embedding.py   # 新增：可插拔 Embedder 抽象（Protocol）+ OllamaEmbedder + build_embedder 工厂
├── db.py          # 改造：chunk_vec vec0 表 + ingest 自动写向量 + search 三模式
├── config.py      # 新增 FUFU_ARCHIVE_EMBED_URL/MODEL/DIM 环境变量
├── models.py      # SearchRequest 加 mode 字段（fts5|dense|hybrid，默认 dense）
└── main.py        # lifespan 里构造 OllamaEmbedder 注入 KnowledgeBase
```

## embedding.py 设计（可插拔核心）

- `Embedder` Protocol：`dim: int` + `embed_documents(texts)` + `embed_query(query)`
- `OllamaEmbedder`：文档侧**不加前缀**，查询侧自动加中文 instruct 前缀（`QUERY_PREFIX_CN = "Instruct: 为这个句子生成表示以用于检索相关文章\nQuery: "`）——评测证明 +2.3% recall@1
- `build_embedder()` 工厂：未来换云端 API（硅基流动等）只改这一处，db 侧零改动
- 批量：`/api/embed` 的 `input` 支持字符串数组（原生批量）

## db.py 检索三模式（search 加 mode 参数）

- `mode="fts5"`：原 BM25 路（P0 兼容）
- `mode="dense"`（**默认**）：embed_query → vec0 KNN（`WHERE embedding MATCH ? ORDER BY distance LIMIT ?`）→ join chunks 拿内容；score = -distance（越小越近）
- `mode="hybrid"`：fts5 取 limit*3 + dense 取 limit → `_rrf_fuse`（k=60，按 chunk_id 融合）→ 前 limit
- `mode="dense"/"hybrid"` 在 embedder 为 None 或 Ollama 不可达时 **自动降级 fts5**（log warning，不硬失败）——符合「检索降级不阻塞」原则

## 关键实现细节

- vec0 表：`CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(embedding float[1024])`（`_ensure_vec_table` 在 initialize_database 里 best-effort，sqlite-vec 没装就跳过，FTS5 仍可用）
- `_connect` 统一 `enable_load_extension(True) + sqlite_vec.load(connection)`（import 用 try/except 包住，可选扩展）
- ingest 写向量：chunk 插入后调 `_index_vector`（best-effort，embed 失败只 warning 不 rollback——文档仍可 FTS 检索）
- `_rows_for_chunks`：按 fused chunk_id 列表保序取回 chunks（IN 子句会丢顺序，必须 dict 后按列表序重排）
- sqlite3 未设 row_factory 时 fetchall 返回 tuple：用 `row[0]`/`row[1]`
- pyproject.toml 正式声明 `sqlite-vec>=0.1.9` 依赖

## 测试模式（不依赖 Ollama）

`tests/test_vectors.py` 用 **FakeEmbedder**（确定性伪向量）：`hashlib.sha256(text)` 取前 8 字节归一化成 8 维向量——相同文本→相同向量，不同文本可区分。这样 dense KNN 行为可预测、CI 无 Ollama 也能跑。覆盖：dense 精确命中、dense 排序、collection 过滤、hybrid 双路、无 embedder 降级 fts5、fts5 兼容、非法 mode 报错、chunk_vec 表存在。

## 真模型冒烟（验证整条链）

`scripts/smoke_vector.py`：临时库 + 真实 Ollama → ingest 2 篇 → 三 mode 各查 2 条语义改写查询。验证点：语义鸿沟（「断电」vs「失去厂用电」）dense 能命中而 fts5 靠侥幸、VRChat 视频 yt-dlp 命中。`scripts/smoke_api_vector.py`：TestClient + create_app 真 embedder → ingest 200 + 三 mode search 200。

## 结论回顾（已评测验证）

- **默认 dense 是数据驱动的选择**：评测 dense-prefix MRR@10=1.000 最优；naive RRF hybrid 反被 FTS5 拖累（0.960），所以生产默认 dense，hybrid 保留供调用方显式选
- 降级策略保证：Ollama 挂了知识库仍可 FTS 检索，不会整站不可用
