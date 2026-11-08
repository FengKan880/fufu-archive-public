> **文档性质：P1 实施记录**
> 本文保留向量检索四路基线；现行评测入口仍为仓库 `eval/README.md`。

# 检索评测基线（2026-08-06，P1 向量路）

## 评测集（eval/）
- `eval/queries.json`：44 条真实风格查询（口语化/语义型/跨域），每条 `expected` 1~3 个 doc id + `domain` + `note`
- `eval/corpus/`：32 篇合成公开语料（6 领域：vrc/plant_work/music/dev/ops/tools），frontmatter 含 `id`/`title`/`collection`/`domain`（无隐私，可进 git）
- `eval/run_eval.py`：建临时库 → 逐条查询 → Recall@k / MRR / NDCG；新检索路在 `RETRIEVERS` 字典注册

## 四路基线对比（同评测集，防「优化换评测」作弊）

| retriever | recall@1 | recall@5 | recall@10 | MRR@10 | NDCG@10 |
|---|---|---|---|---|---|
| fts5（jieba+BM25） | 0.852 | 0.977 | 0.977 | 0.928 | 0.941 |
| dense（无前缀） | 0.943 | 0.989 | 1.000 | 0.989 | 0.988 |
| **dense-prefix（中文 instruct 前缀）** | **0.966** | **1.000** | **1.000** | **1.000** | **0.996** |
| hybrid（FTS5+dense，RRF k=60 无权重） | 0.898 | 1.000 | 1.000 | 0.960 | 0.970 |

## 结论（已写入 eval/README.md）
1. **dense-prefix 是最佳单路**：Recall@10=1.000 / MRR@10=1.000（Recall@1=0.966，非全部 top1）。Qwen3-Embedding 官方中文 instruct 前缀有效（对比无前缀 @1 +2.3%）；文档侧不加前缀，仅查询侧。
2. **naive RRF 被 FTS5 拖累**（hybrid MRR 0.960 < dense-prefix 1.000）：3 条查询 top1 被 FTS5 错误首名挤掉。根因有二：① `_rrf_merge` 先遍历 fts_ids，RRF 平手时 FTS5 先插入排前；② 语义鸿沟类查询 FTS5 完全无召回，只贡献噪声。**后续 hybrid 必须加权 RRF（dense 权重高）或「dense 为主 + FTS5 只补召回」**，不能无脑融合。
3. 评测集对 qwen3-embedding 已接近饱和，作**防回归基线**仍有效；后续加难查询（更长同义改写/跨域）才能拉开 dense 与 reranker 的差距。

## 运行
```bash
python eval/run_eval.py [--retriever fts5|dense|dense-prefix|hybrid] [--verbose] [--k 1 --k 10]
```
首次 dense/hybrid 会经 Ollama 建临时向量索引（32 文档 embed + 44 查询 embed，CPU 约 1~2 分钟）。

## 接入新 retriever 的坑（2026-08-06 实测）
- `main()` 里 `dense`/`dense-prefix`/`hybrid` 都必须在 build_kb 后调 `build_dense_index`（建 vec0 表 + Ollama embed 每文档），只判断了 fts5 会漏
- sqlite3 连接未设 `row_factory` 时 `fetchall()` 返回 tuple：用 `row[0]`/`row[1]`，不是 `row["id"]`
- `_resolve_doc_ids`：hybrid 返回的行自带 `eval_doc_id`，需优先直接取（否则按 chunk_id 查 metadata 会错位）
- vec0 表 `rowid == chunks.id`（P0 每文档 1 chunk）；eval_doc_id 存 metadata 表 `value_json`（json.dumps ensure_ascii=False）
- embedding 批量：Ollama `/api/embed` 的 `input` 支持字符串数组=原生批量（索引管线直接 POST 一批 chunk）
