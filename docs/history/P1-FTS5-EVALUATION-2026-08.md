> **文档性质：P1 实施记录**
> 本文保留 FTS5 中文检索基线与优化过程；现行评测入口仍为仓库 `eval/README.md`。

# 检索评测集与 FTS5 中文查询优化（2026-08-06 实战记录）

## 评测集设计（eval/，位于 .\eval）

- `corpus/`：32 篇合成公开知识语料（**无隐私可进 git**），frontmatter(`id`/`title`/`collection`/`domain`) + markdown 正文；6 领域：vrc/plant_work/music/dev/ops/tools
- `queries.json`：44 条标注查询 `{query, expected(文档id数组 1~3), domain, note}`，**故意混入口语化/语义型**（关键词不完全重叠）让基线有区分度——纯名词查询测不出 AND 缺陷
- `run_eval.py`：建临时库 → 逐条检索 → Recall@k/MRR@k/NDCG@k；retriever 注册式（`RETRIEVERS` dict），新路（dense/hybrid）实现同接口即对比
- **关键设计：保留既有 44 条不变（或记录新基线），禁止"优化换评测"作弊**——新增语料/查询只追加

## FTS5 中文查询基线演进（同一评测集）

| 版本 | 查询策略 | recall@10 | 结论 |
|---|---|---|---|
| P0 原始 | AND 全词匹配 | **0.023** | 真实口语查询（带"为什么/怎么/什么/的"）几乎全灭：查询每个词都须出现在文档，虚词直接整条失败 |
| +停用词过滤 | AND + 滤虚词 | 0.250 | 疑问词滤掉后核心词能 AND 命中，但"切/大/哪些/开始"等口语词仍让 AND 失败 |
| **当前** | **停用词过滤 + OR + BM25** | **0.977** | 召回优先，bm25() 决定排序；44 条仅 1 条完全 miss |

## 可复用教训

1. **FTS5 中文查询绝不能用 AND 全词匹配**（口语查询带虚词/泛化词，任何一词不在索引就整条失败）。正确姿势 = 查询侧滤中文停用词 + **OR 连接** + 靠 `bm25()` 排序；索引侧保留完整分词。
2. 停用词表在 `src/fufu_archive/tokenization.py` 的 `_QUERY_STOPWORDS`（疑问词/语气词/虚化词：的/了/是/怎么/为什么/什么/吗/呢/吧/出来/自己/一个…约 70 词）。
3. 单字/不在索引的 token 在 OR 下只是不贡献命中，不影响其他词召回——**OR 对小语料（几十篇）安全**，BM25 负责排序。
4. 剩余 miss 分两类：**语义鸿沟**（"断电" vs 文档"失去厂用电"关键词零重叠）——FTS5 无解，向量路/同义改写的加分项；**@1 未中但 @10 中**（排序精度）——reranker 的加分项。评测明细正好指明 P1 每项增强的价值点。
5. **P0 测试通过 ≠ 真实查询可用**：P0 测试用例都是纯名词查询（"中文检索"），没暴露 AND 缺陷。评测集必须用真实风格查询才有意义。

## 基线数字（2026-08-06，FTS5 + jieba 分词）

| metric | @1 | @5 | @10 |
|---|---|---|---|
| Recall | 0.852 | 0.977 | 0.977 |
| MRR | 0.886 | 0.928 | 0.928 |
| NDCG | 0.886 | 0.941 | 0.941 |

## 工程注意

- 跑评测/独立 venv 脚本须 `env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV`（防 Hermes 全局 PYTHONPATH 串 venv）
- `run_eval.py` 的 `_resolve_doc_ids` 用 sqlite3 直连时记得 `row_factory = sqlite3.Row` + try/finally close，否则 tuple 索引报错 + db 文件被占用删不掉（临时目录清理 PermissionError）
