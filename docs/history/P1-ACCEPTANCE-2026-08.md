> **文档性质：阶段验收记录**
> 本文记录 P1 当时的验收证据；当前项目状态以 `../PROJECT-OVERVIEW.md` 和现行测试为准。

# 酒狐档案馆｜P1 核心检索成果与验收归档

> 项目：Fufu Archive（酒狐档案馆）
>
> 工程目录：`.`
>
> 归档时间：2026-08-07
>
> 阶段状态：**P1 已完成并验收通过，可进入 P2**

---

## 1. P1 目标

在 P0 的 SQLite + FTS5 + FastAPI 基础上，完成核心检索能力：

- 关键词检索：SQLite FTS5 + 中文 jieba 预分词；
- 向量检索：可插拔 Embedder + Ollama `qwen3-embedding:0.6b`；
- 向量索引：sqlite-vec `vec0`；
- 混合检索：dense + FTS5 的 RRF 融合；
- `collection` / `domain` / `status` 过滤；
- Ollama 或 sqlite-vec 不可用时优雅降级到 FTS5；
- 建立固定评测集，防止后续优化改变口径或造成指标回退。

明确边界：P1 仍保持每个文档一个 chunk；分块、父子 chunk、复杂格式上传解析和会话自动入库留到 P2/P3。

---

## 2. 已落地实现

### 2.1 Embedding 后端

文件：`src/fufu_archive/embedding.py`

- `Embedder` Protocol：统一文档向量和查询向量接口；
- `OllamaEmbedder`：调用本机 Ollama `/api/embed`；
- 默认模型：`qwen3-embedding:0.6b`；
- 默认维度：1024；
- 查询侧使用中文检索 instruct 前缀：

```text
Instruct: 为这个句子生成表示以用于检索相关文章
Query: <query>
```

- 支持批量 embedding；
- 校验 HTTP 状态、JSON 结构、返回向量数量、向量维度和非有限数值；
- 通过环境变量配置：
  - `FUFU_ARCHIVE_EMBED_URL`
  - `FUFU_ARCHIVE_EMBED_MODEL`
  - `FUFU_ARCHIVE_EMBED_DIM`

### 2.2 数据库与向量索引

文件：`src/fufu_archive/db.py`

- `chunk_vec` sqlite-vec 虚拟表；
- 向量维度由配置传递，不再硬编码在应用入口；
- ingest 先提交文档、chunk、FTS 和 metadata，再单独生成/写入向量；
- embedding 服务失败时，FTS 文档仍然保留；
- dense/hybrid 失败时自动回退 FTS5；
- dense 检索会针对 collection/domain/status 过滤进行候选 over-fetch，避免先取 limit 个向量后过滤导致结果不足；
- hybrid 使用 RRF，并返回与结果顺序一致的融合 score；
- 数据库 migration 使用显式事务；
- 并发首次初始化时，WAL 设置增加锁冲突重试。

### 2.3 API

文件：`src/fufu_archive/main.py`

`/kb/search` 支持：

```json
{"mode": "fts5"}
{"mode": "dense"}
{"mode": "hybrid"}
```

鉴权保持 Bearer token：

- token 未配置：返回 `503`；
- token 缺失或错误：返回 `401`；
- 使用安全比较；
- `/health` 保持公开。

### 2.4 评测脚本

文件：`eval/run_eval.py`

- 32 篇合成语料；
- 44 条带标注查询；
- Recall@k / MRR@k / NDCG@k；
- FTS5、dense、dense-prefix、hybrid 统一走生产检索实现；
- dense 评测发现索引不完整时直接报错，避免把 FTS5 fallback 冒充 dense 指标；
- cutoff 参数校验和重复值去重。

---

## 3. 最终验收结果

### 3.1 自动化测试

```text
33 passed
```

覆盖内容包括：

- P0 数据库初始化、migration、中文 FTS5、metadata；
- 并发数据库初始化；
- API health、鉴权、ingest、三种搜索 mode；
- dense 精确检索、过滤、空库和降级；
- hybrid RRF score 和顺序；
- embedding 批量请求与异常响应；
- 评测参数及 dense 索引完整性。

仅有一个既有依赖警告：Starlette 提示当前 `httpx` 用法未来需要 `httpx2`，不影响本次测试结果。

### 3.2 检索评测

固定评测集：32 documents / 44 queries。

| Retriever | Recall@1 | Recall@5 | Recall@10 | MRR@10 | NDCG@10 |
|---|---:|---:|---:|---:|---:|
| FTS5 | 0.852 | 0.977 | 0.977 | 0.928 | 0.941 |
| dense-prefix | 0.966 | 1.000 | 1.000 | 1.000 | 0.996 |

说明：dense-prefix 的 Recall@1 本次实际复测为 0.966；Recall@5、Recall@10 和 MRR@10 均达到 1.000。此前会话中记录的 Recall@1=1.000 不作为本次最终固定指标，避免把评测波动写成确定事实。

### 3.3 HTTP 冒烟

`scripts/smoke_api_vector.py` 实测通过：

```text
ingest: 200
search[dense]: 200
search[hybrid]: 200
search[fts5]: 200
API-SMOKE-PASS
```

此外，P0 的 401/503 鉴权测试和 API 回归测试均通过。

### 3.4 静态检查

- `python -m compileall`：通过；
- `git diff --check`：通过；
- Ollama `qwen3-embedding:0.6b`：本机 `127.0.0.1:11434` 正常。

---

## 4. 本阶段发现并修复的关键问题

### Bug 1：dense 过滤后结果不足

**现象**：先取 `limit` 个最近邻，再按 collection/domain 过滤，可能返回少于 limit 的结果。

**修复**：先查询符合条件的 chunk 集合，向量 KNN 候选逐步扩大，直到填满 limit 或耗尽候选。

### Bug 2：embedding 失败可能影响 FTS 入库

**现象**：原先在文档事务中调用 embedding 服务；远程服务失败可能使整个 ingest 事务受影响。

**修复**：先提交 FTS 文档，再独立执行向量生成和写入；向量失败只记录 warning，关键词副本仍可搜索。

### Bug 3：hybrid score 与结果顺序不一致

**现象**：RRF 只返回 chunk 顺序，后续却使用 dense distance 填充 score，score 不能代表 hybrid 排名。

**修复**：RRF 返回实际融合分数，结果按融合分数排序并填充 score。

### Bug 4：评测脚本可能把 FTS fallback 当成 dense 结果

**现象**：生产 `search(mode="dense")` 的设计是服务失败时降级 FTS5；评测如果直接调用公开 search，可能在 dense 后端失败时仍产生看似正常的 dense 指标。

**修复**：评测 dense/hybrid 直接调用生产内部 dense/hybrid 路径，并在建立评测索引后检查向量数量完整性。

### Bug 5：Ollama 响应校验不足

**现象**：原实现未充分检查非 200 响应、非法 JSON、返回数量、维度和 NaN/Infinity。

**修复**：增加 HTTP、JSON、数量、维度和数值合法性校验，并支持批量请求。

### Bug 6：并发首次初始化出现 database locked

**现象**：多个线程同时首次打开数据库并执行 `PRAGMA journal_mode = WAL` 时可能锁冲突。

**修复**：连接 timeout + WAL 初始化渐进重试；migration 使用显式 `BEGIN IMMEDIATE` 事务。

---

## 5. 发布说明

历史开发提交不作为当前公开仓库的提交标识；此文仅保留项目验收结果与技术结论。

## 6. P2 开始点

P2 的项目实施建议顺序：

1. **分块管线**：按 300–500 字或 token 长度切块，保留 chunk_index；
2. **父子 chunk**：父文档摘要/上下文 + 子 chunk 检索；
3. **多格式入库**：Markdown、TXT、JSON、PDF/Office 解析边界；
4. **metadata 与 source 管线**：source、标题、路径、时间、标签、版本；
5. **更新/去重/supersedes**：相似知识融合、版本替换和状态继承；
6. **上传 API**：文件上传、大小限制、编码检测、解析失败反馈；
7. **查询改写**：多轮会话查询改写可在 P2 提前接入，但需增加独立评测集；
8. **自定义 jieba 词典**：电厂术语、VRChat、Minecraft、音频制作等专名。

P2 必须继续使用当前评测集作为回归基线，并在新增 chunk 后补充 chunk-level / document-level 两套指标，避免只优化单文档单 chunk 场景。
