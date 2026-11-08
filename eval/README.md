# Fufu Archive 检索评测集（eval）

面向通用使用场景的合成检索质量基线。**所有优化（向量路、混合检索、reranker）上线前必须先跑这里对比，禁止盲调。**

## 结构

```
eval/
├── run_eval.py        # 评测脚本：建临时库 → 逐条查询 → Recall@k/MRR/NDCG
├── queries.json       # 44 条合成标注查询（含口语化/语义型）
└── corpus/            # 32 篇语料（公开知识，无隐私），6 领域
    ├── vrc/           # VRChat 使用/网络/制作（6 篇）
    ├── plant_work/    # 电厂运维（6 篇）
    ├── music/         # 音乐编曲（6 篇）
    ├── dev/           # 游戏开发/AI 编程（5 篇）
    ├── ops/           # Hermes 运维（5 篇）
    └── tools/         # 工具/网络（4 篇）
```

- 语料 frontmatter：`id`（必须，评测标注引用它）、`title`、`collection`、`domain`
- 查询标注：`query` + `expected`（文档 id 数组，1~3 个）+ `domain` + `note`
- 查询故意混入口语化/语义型（关键词不完全重叠），让基线有区分度

## 运行

```bash
python eval/run_eval.py              # FTS5 基线
python eval/run_eval.py --verbose    # 带逐条明细
python eval/run_eval.py --k 1 --k 10 # 自定义截断点
```

## 基线（历史结果）

以下数字来自历史语料与对应环境，不是脱敏后版本的新验收结果。语料通用化后必须重跑评测，且有无 jieba 的结果不可混为一谈。

### 2026-08-06：FTS5 关键词路，jieba 分词

| metric | @1 | @5 | @10 |
|---|---|---|---|
| Recall | 0.852 | 0.977 | 0.977 |
| MRR | 0.886 | 0.928 | 0.928 |
| NDCG | 0.886 | 0.941 | 0.941 |

## 向量路基线（2026-08-06，Ollama qwen3-embedding:0.6b + sqlite-vec）

运行：`python eval/run_eval.py --retriever dense` / `dense-prefix` / `hybrid`（首次会经 Ollama 建向量索引）

| retriever | recall@1 | recall@5 | recall@10 | MRR@10 | NDCG@10 |
|---|---|---|---|---|---|
| fts5（对照） | 0.852 | 0.977 | 0.977 | 0.928 | 0.941 |
| **dense**（无前缀） | 0.943 | 0.989 | 1.000 | 0.989 | 0.988 |
| **dense-prefix**（中文 instruct 前缀） | **0.966** | **1.000** | **1.000** | **1.000** | **0.996** |
| hybrid（FTS5+dense，RRF k=60 无权重） | 0.898 | 1.000 | 1.000 | 0.960 | 0.970 |

**结论（2026-08-06）**：

1. **dense-prefix 是最佳单路**：44 条查询 Recall@10=1.000、MRR@10=1.000（Recall@1=0.966，非 100% top1）。官方中文 instruct 前缀有效（对比无前缀 @1 +2.3%）。
2. **naive RRF 反而被 FTS5 拖累**（hybrid MRR 0.960 < dense-prefix 1.000）：3 条查询 top1 被 FTS5 的错误首名挤掉。根因：RRF 平手时 FTS5 先插入排前（`_rrf_merge` 先遍历 fts_ids）；且语义鸿沟类查询 FTS5 完全无召回，只贡献噪声。**后续 hybrid 需加权 RRF（dense 权重高）或改为「dense 为主 + FTS5 只补召回」**。
3. 评测集对 qwen3-embedding 已接近饱和（Recall@10/MRR@10=1.000，仅 Recall@1 尚有 0.966→1.000 空间），作为**防回归基线**仍有效；后续加难查询（更长同义改写/跨域）以拉开 dense 与 reranker 的差距。

**演进记录**（同一评测集，避免"优化换评测"作弊）：

1. P0 原始（AND 全词匹配）：recall@10 = **0.023** —— 真实口语查询（带"为什么/怎么/什么/的"）全部 miss，AND 要求查询每个词都出现在文档里，虚词直接整条失败。
2. 查询侧停用词过滤（AND 保留）：recall@10 = **0.250** —— 疑问词/语气词滤掉后核心词能 AND 命中，但"切/大/哪些"这类口语词仍导致失败。
3. **+ OR 连接 + BM25 排序**（当前）：recall@10 = **0.977** —— 召回优先，BM25 决定排序；44 条仅 1 条完全 miss。

**剩余问题（P1 向量路的机会）**：
- 「厂里突然断电了对机组有什么影响」→ 期望 plant-electrical，但文档写的是"失去厂用电"，关键词零重叠 —— **同义改写/语义鸿沟，FTS5 无解，向量检索的典型加分项**。
- 「怎么给 VRChat 做一个自己的角色模型」「混音时 EQ 怎么调」「记忆和技能有什么区别」等 4~5 条 @1 未命中但 @10 命中 —— 排序精度问题，**reranker 的加分项**。

## 注意

- 语料用于模拟一般技术问题，不用于描述仓库维护者的经历或设备。公开版本已将环境相关示例通用化；测试文档与查询仅作为评测输入，不应视作个人事实或现行操作指南。
- 评测集是活的：新增领域语料/查询时保持既有 44 条不变（或记录新基线），否则无法对比。
- 新增 retriever（dense/hybrid）在 `run_eval.py` 的 `RETRIEVERS` 注册即可，接口统一。
