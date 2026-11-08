---
id: ops-kb-usage
title: 知识库使用操作
collection: hermes_ops
domain: ops
---
Fufu Archive（酒狐档案馆）是面向 AI Agent 的外置长期记忆与知识库，基于 SQLite 全家桶：FTS5 做关键词检索（BM25），sqlite-vec 做向量检索，RRF 融合两路结果。

知识分两大类：hermes_ops（运行档案：记忆快照、日报、技能快照、会话提炼）和 world_knowledge（现实知识：vrc、gaming、plant_work、tools）。

使用方式：入库（Ingest）把文档写入，检索（Search）查相关内容。文档按 collection 和 domain 分类，带 metadata。知识有生命周期：长期不用的知识会标记过期（EXPIRED），过期由月度复盘决定删除或保留。备份靠定期任务，数据库文件不走 git。
