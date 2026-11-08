> **文档性质：历史选型调研**
> 本文记录早期 embedding 部署与 API 方案比较；模型与价格具有时效性。当前已落地方案见 `../PROJECT-OVERVIEW.md`。

# 2026 年 Embedding（向量化）模型选型对比报告（中文）

> 调研时间：2026-08-06 ｜ 场景：个人中文知识库（SQLite + sqlite-vec + FTS5 + RRF 混合检索），数据量中等（几千~几十万 chunk），Windows 环境，本地 CPU 推理为主，隐私敏感。
> 结论速览：**主选 Qwen3-Embedding-0.6B（升级原 bge-m3 方案），bge-m3 保留做混合检索/降级后备，bge-base-zh-v1.5 已明显过时（512 token 截断是硬伤），建议降为历史参照。**

---

## 一、2026 年现状与「新霸主」判断（时效性标注）

- **中文本地最强**：截至 2026-08 可查信息，C-MTEB 中文榜最高分记录仍是 **Qwen3-Embedding-8B = 73.84**（2025-06 提交）；4B = 72.27，0.6B = 66.33。2026 年**没有出现新的中文专用霸主**。
- **多语 MMTEB 新霸主**：腾讯 **KaLM-Embedding-Gemma3-12B-2511**（2025-11 发布，MMTEB 72.32，2026-07 仍在位）。但 11.76B 参数 / 3840 维 / 自定义社区许可（非 Apache/MIT），对个人 CPU 场景不实用，仅作"质量上限"参考。
- **微软 Harrier-OSS-v1** 的 74.3 是 **MTEB v2 分片口径**（英文为主，27B），与 KaLM 的 72.32（MMTEB 多语）**不可直接比较**（多来源明确提示，属常见口径陷阱）。
- 2026 年新动向：Google Gemini Embedding 2（2026-03，五模态，跨语言实测最强）、Jina Embeddings v4（3.8B，Qwen2.5-VL 底座）、Qwen3-VL-Embedding-2B/8B（多模态，2025-10）、字节 Seed1.6-embedding（API 70.26）。**对纯文本中文个人库，均不构成对 Qwen3-Embedding 的替代。**
- 生态地位：LangChain/LlamaIndex 遥测显示生产 RAG 使用率第一仍是 **BGE-M3**（存量惯性），但新项目迁移到 Qwen3-Embedding 已是明显趋势（中文社区多篇实测：0.6B 在中文检索/相似度上反超 bge-m3）。

## 二、开源本地模型对比

| 模型 | 参数 | 输出维度 | 上下文 | 需 instruct 前缀？ | C-MTEB(中文) | MMTEB(多语) | 许可 |
|---|---|---|---|---|---|---|---|
| **Qwen3-Embedding-0.6B** | 0.6B | 1024（MRL 32~1024） | 32K | 推荐（查询侧 +1~5%） | **66.33** | 64.33 | Apache-2.0 |
| **Qwen3-Embedding-4B** | 4B | 2560（MRL 32~2560） | 32K | 推荐 | 72.27 | 69.45 | Apache-2.0 |
| **Qwen3-Embedding-8B** | 8B | 4096（MRL 32~4096） | 32K | 推荐 | **73.84** | **70.58**（2025-06 多语榜第一） | Apache-2.0 |
| **bge-m3** | 568M | 1024 | 8192 | **不需要** | ≈63（历史值） | 59.56 | MIT |
| bge-large-zh-v1.5 | 326M | 1024 | 512 | 查询需加"为这个句子生成表示以用于检索相关文章：" | 64.53（2023-24 时代榜首） | - | MIT |
| **bge-base-zh-v1.5** | 109M | 768 | **512（硬伤）** | 同上 | ≈63.3（历史值） | - | MIT |
| gte-Qwen2-7B-instruct | 7.6B | 3584 | 32K | 需要 | 71.62 | 62.51 | Apache-2.0 |
| gte-Qwen2-1.5B-instruct | 1.5B | 1536 | 32K | 需要 | 67.12 | 59.45 | Apache-2.0 |
| multilingual-e5-large-instruct | 560M | 1024 | 4096 | 需要（query:/passage:） | 58.08 | 63.22 | MIT |
| E5-Mistral-7B-instruct | 7.1B | 4096 | 32K | 需要 | 中文一般 | ~62 | MIT |
| ritrieve_zh_v1（richinfoai） | 0.3B | 1024 | 512 | 否 | **72.71**（黑马，但生态/文档少） | - | - |
| acge_text_embedding（合合） | 326M | 1024 | 512 | 否 | 69.07（2024-03 时代榜首） | - | 商用需授权 |
| stella-large-zh-v3-1792d | 326M | 1792 | 1024 | 否 | ~72（2024 时代强） | - | MIT |
| jina-embeddings-v3 | 570M | 1024 | 8192 | 按 task 前缀 | 一般 | 58.37 | CC BY-NC（禁商用） |
| KaLM-Embedding-Gemma3-12B | 11.76B | 3840 | 8K | 需要 | 中文强 | **72.32**（MMTEB #1） | 社区许可（非商用友好） |
| NV-Embed-v2 | 7.8B | 4096 | 32K | 需要 | 中文一般 | 56.29（legacy） | CC BY-NC |
| llama-embed-nemotron-8b | 8B | 4096 | 32K | 需要 | - | 69.46 | NVIDIA 许可 |

要点：
- **Qwen3-Embedding 系是 2025-06 发布**（部分英文文章误标 2026 发布，以 HF 官方为准）；instruct 前缀格式 `Instruct: <任务描述>\nQuery: <query>`，仅查询侧加，文档侧不加；多语言任务建议英文指令。sentence-transformers 直接 `model.encode(queries, prompt_name="query")` 即可。
- bge-m3 无需前缀、自带 dense+sparse+colbert 三模式，与 FTS5+RRF 方案天然互补（sparse 权重可当 BM25 平替）。
- bge-zh v1.5 系列 512 token 上限，中文长文档直接截断丢语义，**不建议再作新项目主力**。

## 三、云端 Embedding API 对比

| API | 中文效果口碑 | 价格（每百万 token） | 国内直连 | 备注 |
|---|---|---|---|---|
| OpenAI text-embedding-3-small | 中（中文弱于国产） | $0.02（≈0.14 元） | ✗ 需代理 | 1536 维，8191 token |
| OpenAI text-embedding-3-large | 中上（MTEB 多语 58.96，被开源反超） | $0.13（≈0.93 元） | ✗ 需代理 | 3072 维可裁剪；2024-01 后未更新 |
| 阿里 text-embedding-v4 | **优**（Qwen3 底座，比 v3 提升 15~40%） | **0.5 元**（Batch File 0.25 元） | ✓ | 64~2048 维可自定义，8192 token/条，批次 10 |
| 阿里 text-embedding-v3 | 良~优 | 0.5 元 | ✓ | 1024 维可裁剪 |
| 智谱 embedding-3 | 优（老牌稳定） | **0.5 元** | ✓ | 256~2048 维，8K 上下文 |
| Cohere embed-v4 | 中（英文主打，多语用 v3.0） | $0.12（Bedrock 口径） | ✗ 需代理 | 英文检索强，中文非强项 |
| Voyage voyage-4 / 3.5 | 中上（RAG 实测口碑好，英文/混合强） | $0.06 / $0.06（4-large $0.12） | ✗ 需代理 | 32K 上下文，1024 维 |
| Gemini embedding-001 / Embedding 2 | 优（Embedding 2 跨语言实测第一） | 有免费层，付费按量 | ✗ 需代理 | 2026-03 发布，五模态；国内不可直连 |
| 硅基流动 SiliconFlow | 优（开源模型转售） | bge-m3 长期**免费**；Qwen3-Embedding 0.6B/4B/8B 国际站 $0.01/$0.02/$0.04 | ✓ | OpenAI 兼容 API，国内站人民币计价更低 |

要点：OpenAI 在国内需代理且中文已落后于国产开源；**国内直连 + 中文强 + 便宜**的 API 首选阿里百炼 text-embedding-v4 或智谱 embedding-3（均 0.5 元/百万）。

## 四、场景推荐 Top3（个人中文知识库 / 中等数据量 / 本地 CPU / 隐私敏感）

1. **Qwen3-Embedding-0.6B（主选，替代原 bge-m3 方案）**
   - 中文 C-MTEB 66.33 vs bge-m3 ≈63，0.6B 里中文最强；32K 上下文覆盖长文档；1024 维可 MRL 降到 512/768 给 sqlite-vec 省空间；Q4 量化仅 639MB，CPU 实测约 5.8K token/s（Xeon），内存占用需按部署环境评估；Apache-2.0 无商用顾虑。
   - 代价：查询侧需加 instruct 前缀（约 +1~5% 收益），文档侧不用；接入比 bge-m3 多一步。
2. **bge-m3（保留为混合检索增强 + 降级后备）**
   - 无需前缀、生态最成熟（LangChain/LlamaIndex 默认，使用率第一）；dense+sparse 双模式可与 FTS5+RRF 深度配合；中文弱 3 分但速度更快（CPU 友好）。
   - 适合：想省事 / 用其 sparse 权重替代或补充 BM25 的场景。
3. **Qwen3-Embedding-4B（质量进阶备选，非必须）**
   - 中文 72.27（接近 8B 的 73.84）；Q4 约 2~3GB 内存，CPU 可跑但单条编码明显变慢；2560 维在 sqlite-vec 里存储/检索成本高（中等数据量可承受，但不划算）。**仅当 0.6B 检索效果不达标时再上。**

落地建议（与现架构衔接）：
- 直接 sentence-transformers 加载 `Qwen/Qwen3-Embedding-0.6B`，查询用 `prompt_name="query"`；文档侧不用前缀。
- 维度：建议 `dimensions=768`（MRL 裁剪）平衡效果与 sqlite-vec 存储；若换模型，**必须全量重嵌 + 重建索引**（不同模型向量不可混用）。
- 可选加 bge-reranker-v2-m3 或 Qwen3-Reranker-0.6B 做二阶段精排，中文 RAG 收益明显。
- 备选黑马：ritrieve_zh_v1（0.3B，C-MTEB 72.71）中文专用，值得在自有数据集上 A/B 一下，但社区资料少。

## 五、关键 URL 清单

- Qwen3-Embedding 官方：https://huggingface.co/Qwen/Qwen3-Embedding-0.6B ｜ https://huggingface.co/Qwen/Qwen3-Embedding-8B ｜ https://github.com/QwenLM/Qwen3-Embedding ｜ 论文 https://arxiv.org/abs/2506.05176
- bge-m3：https://huggingface.co/BAAI/bge-m3 ｜ https://github.com/FlagOpen/FlagEmbedding
- KaLM（MMTEB #1）：https://huggingface.co/tencent/KaLM-Embedding-Gemma3-12B-2511 ｜ https://kalm-embedding.github.io/
- MTEB 总榜：https://huggingface.co/spaces/mteb/leaderboard ｜ C-MTEB：https://github.com/FlagOpen/FlagEmbedding/tree/master/C_MTEB（HF space 已迁移/需登录，用 GitHub 侧）
- 2026 综述类：https://www.codesota.com/benchmarks/mteb （15 模型榜单表，2026-05 更新）｜ https://presenc.ai/research/best-open-weight-embedding-models-2026 ｜ https://app.ailog.fr/en/blog/news/embedding-models-2026
- 2026 实测（跨语言/长文/MRL）：https://zc277584121.github.io/rag/2026/03/20/embedding-models-benchmark-2026.html
- 中文实测对比（Qwen3 vs bge-m3，Ollama 部署/速度/显存）：https://blog.csdn.net/vulkan6gpu/article/details/152545992
- 阿里百炼 API 文档与价格：https://help.aliyun.com/zh/model-studio/text-embedding-v4 ｜ https://help.aliyun.com/zh/model-studio/embedding
- 智谱 embedding-3：https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3
- OpenAI 定价：https://openai.com/index/new-embedding-models-and-api-updates/ ｜ https://costgoat.com/pricing/openai-embeddings
- Voyage：https://docs.voyageai.com/docs/pricing ｜ Cohere：https://cohere.com/pricing
- SiliconFlow：https://siliconflow.cn/pricing ｜ https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings

## 六、核实说明（矛盾点处理）

- 「Qwen3-Embedding 2026 年发布」：仅见 presenc.ai 一家如此写，HF 官方/GitHub 明确 2025-06 发布，以官方为准。
- 「Harrier 74.3 vs KaLM 72.32」：口径不同（MTEB v2 英文分片 vs MMTEB 多语），ailog 明确提示不可直接比。
- 「text-embedding-3-large MTEB 64.6 vs 58.96」：英文榜/多语榜口径差异，均已标注。
- SiliconFlow bge-m3 免费：linux.do 2026 社区实测确认；国际站 .com 无此模型页，以国内站控制台为准。
