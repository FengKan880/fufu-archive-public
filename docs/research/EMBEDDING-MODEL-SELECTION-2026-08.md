> **文档性质：模型选型调研**
> 本文从技能参考资料迁入；榜单、价格与模型状态需按调研日期理解。

# Embedding（向量化）模型选型调研 — 2026-08 现状

> 调研时间 2026-08-06，场景：个人中文知识库、中等数据量（几千~几十万 chunk）、本地 CPU、隐私敏感。
> 结论：**主选 Qwen3-Embedding-0.6B（替代 bge-m3 方案），bge-m3 保留做混合检索/降级后备，bge-base-zh-v1.5 已过时（512 token 截断硬伤）**。
> ⚠️ 时效性：本领域每季度都在变，kb_monthly_review 应重验 MTEB/C-MTEB 榜单与价格。

## 2026 新霸主判断

- 中文 C-MTEB 最高记录仍是 **Qwen3-Embedding-8B = 73.84**（2025-06 提交），2026 年无新的中文专用霸主。
- 多语 MMTEB 榜首：腾讯 **KaLM-Embedding-Gemma3-12B-2511**（72.32，2025-11 起，2026-07 仍在位）——11.76B/3840 维/社区许可，个人场景不实用。
- 口径陷阱：微软 Harrier-OSS-v1 的 74.3 是 **MTEB v2 英文分片**，与 KaLM 的 72.32（MMTEB 多语）**不可直接比**；text-embedding-3-large 的 64.6（英）vs 58.96（多语）同理。
- 2026 新动向（对纯文本中文个人库均非替代品）：Gemini Embedding 2（2026-03，五模态）、Jina Embeddings v4（3.8B）、Qwen3-VL-Embedding 2B/8B（多模态）、字节 Seed1.6-embedding（API 70.26）。
- 生态：生产 RAG 使用率第一仍是 bge-m3（存量惯性），但新项目迁 Qwen3-Embedding 已是趋势。

## 开源本地模型速查

| 模型 | 参数 | 维度 | 上下文 | instruct 前缀 | C-MTEB | MMTEB | 许可 |
|---|---|---|---|---|---|---|---|
| Qwen3-Embedding-0.6B | 0.6B | 1024 (MRL 32~1024) | 32K | 推荐(查询侧 +1~5%) | 66.33 | 64.33 | Apache-2.0 |
| Qwen3-Embedding-4B | 4B | 2560 (MRL) | 32K | 推荐 | 72.27 | 69.45 | Apache-2.0 |
| Qwen3-Embedding-8B | 8B | 4096 (MRL) | 32K | 推荐 | **73.84** | **70.58**(2025-06 多语#1) | Apache-2.0 |
| bge-m3 | 568M | 1024 | 8192 | **不需要** | ≈63 | 59.56 | MIT |
| bge-large-zh-v1.5 | 326M | 1024 | 512 | 查询需前缀 | 64.53(2023-24 榜首) | - | MIT |
| bge-base-zh-v1.5 | 109M | 768 | **512(硬伤)** | 查询需前缀 | ≈63.3 | - | MIT |
| gte-Qwen2-7B-instruct | 7.6B | 3584 | 32K | 需要 | 71.62 | 62.51 | Apache-2.0 |
| multilingual-e5-large-instruct | 560M | 1024 | 4096 | 需要(query:/passage:) | 58.08 | 63.22 | MIT |
| ritrieve_zh_v1 (richinfoai) | 0.3B | 1024 | 512 | 否 | **72.71**(黑马,生态少) | - | - |
| acge_text_embedding (合合) | 326M | 1024 | 512 | 否 | 69.07(2024-03 榜首) | - | 商用需授权 |
| stella-large-zh-v3-1792d | 326M | 1792 | 1024 | 否 | ~72(2024 时代) | - | MIT |
| jina-embeddings-v3 | 570M | 1024 | 8192 | 按 task | 一般 | 58.37 | CC BY-NC |
| KaLM-Embedding-Gemma3-12B | 11.76B | 3840 | 8K | 需要 | 中文强 | **72.32**(MMTEB#1) | 社区许可 |
| NV-Embed-v2 | 7.8B | 4096 | 32K | 需要 | 中文一般 | 56.29 | CC BY-NC |

关键细节：
- Qwen3-Embedding 系 **2025-06 发布**（个别英文文误标 2026，以 HF 为准）。instruct 格式 `Instruct: <任务>\nQuery: <query>`，仅查询侧、文档侧不加，多语任务用英文指令；sentence-transformers 用 `model.encode(q, prompt_name="query")`。
- bge-m3 无需前缀 + dense/sparse/colbert 三模式，sparse 权重可当 BM25 平替，与 FTS5+RRF 天然互补。
- bge-zh v1.5 系列 512 token 上限，长文档截断丢语义，**别作新项目主力**。

## 云端 API 速查（价格=每百万 token）

| API | 中文口碑 | 价格 | 国内直连 |
|---|---|---|---|
| OpenAI 3-small / 3-large | 中 / 中上(多语 58.96,被开源反超) | $0.02 / $0.13 (≈0.14/0.93 元) | ✗ 需代理 |
| 阿里 text-embedding-v4 (Qwen3 底座,比 v3 提升 15~40%) | 优 | **0.5 元**(Batch File 0.25 元) | ✓ |
| 智谱 embedding-3 | 优(老牌) | **0.5 元** | ✓ |
| Cohere embed-v4 | 中(英文主打) | $0.12 | ✗ |
| Voyage voyage-4 / 3.5 | 中上(RAG 口碑好) | $0.06 (4-large $0.12) | ✗ |
| Gemini embedding-001 / Embedding 2 | 优(跨语言实测第一) | 有免费层 | ✗ |
| SiliconFlow (开源转售) | 优 | bge-m3 长期**免费**; Qwen3-Embedding 0.6B/4B/8B 国际站 $0.01/$0.02/$0.04 | ✓ |

国内直连+中文强+便宜 → 阿里百炼 text-embedding-v4 或智谱 embedding-3（均 0.5 元/百万）。

## 场景 Top3（个人中文库/中等数据/本地 CPU/隐私敏感）

1. **Qwen3-Embedding-0.6B（主选）**：中文最强 0.6B、32K ctx、Q4 仅 639MB、CPU ~5.8K tok/s（Xeon 实测）；MRL 裁到 768 维喂 sqlite-vec 省空间；代价=查询侧要 instruct 前缀。
2. **bge-m3（混合检索增强 + 降级后备）**：无需前缀、生态最熟；sparse 权重补 BM25；中文弱约 3 分但更快。
3. **Qwen3-Embedding-4B（进阶备选，仅当 0.6B 不达标）**：中文 72.27，Q4 约 2-3GB 内存，2560 维存储成本高。

落地注意：换模型必须全量重嵌+重建索引（不同模型向量不可混用）；可选 bge-reranker-v2-m3 / Qwen3-Reranker-0.6B 二阶段精排。

## 关键 URL

- Qwen3-Embedding: huggingface.co/Qwen/Qwen3-Embedding-0.6B | github.com/QwenLM/Qwen3-Embedding | arxiv.org/abs/2506.05176
- bge-m3: huggingface.co/BAAI/bge-m3 | github.com/FlagOpen/FlagEmbedding
- KaLM: huggingface.co/tencent/KaLM-Embedding-Gemma3-12B-2511 | kalm-embedding.github.io
- 榜单: huggingface.co/spaces/mteb/leaderboard | github.com/FlagOpen/FlagEmbedding/tree/master/C_MTEB（C-MTEB HF space 已迁移/需登录，用 GitHub 侧）
- 2026 综述: codesota.com/benchmarks/mteb（15 模型表,2026-05 更新）| presenc.ai/research/best-open-weight-embedding-models-2026 | app.ailog.fr/en/blog/news/embedding-models-2026
- 2026 实测(跨语言/长文/MRL): zc277584121.github.io/rag/2026/03/20/embedding-models-benchmark-2026.html
- 中文实测(Qwen3 vs bge-m3, Ollama/速度/显存): blog.csdn.net/vulkan6gpu/article/details/152545992
- 阿里 v4: help.aliyun.com/zh/model-studio/text-embedding-v4 | 智谱: docs.bigmodel.cn/cn/guide/models/embedding/embedding-3
- OpenAI 定价: costgoat.com/pricing/openai-embeddings | Voyage: docs.voyageai.com/docs/pricing | SiliconFlow: siliconflow.cn/pricing

## 核实记录（矛盾点）

- 「Qwen3-Embedding 2026 年发布」仅 presenc.ai 一家，官方明确 2025-06 → 以官方为准。
- Harrier 74.3 vs KaLM 72.32：口径不同不可直接比（ailog 明确提示）。
- SiliconFlow bge-m3 免费：linux.do 2026 社区实测；国际站 .com 无此模型页，以国内站控制台为准。
