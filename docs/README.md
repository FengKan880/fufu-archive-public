> **当前 P4 出口审查：** [P4-EXIT-REVIEW-2026-09-15.md](P4-EXIT-REVIEW-2026-09-15.md)（2026-09-15，权威阶段门结论）

# 🗂️ 酒狐档案馆 · Fufu Archive 文档中心

> **2026-09-10 交接更新：** P4 工作树已推进至 P4.3-1 queue 核心；P4.1/P4.2 有实现但仍有路线出口与可靠性待验收项，不能视为全部完成。当前复测 195 passed / 5 skipped。旧文中的“P4 未实施”及旧清单属于历史状态。请先读 [最新 P4 交接](P4-HANDOFF-2026-09-10.md)；本轮暂停开发，换会话续作。

> 首次阅读请先看 **[PROJECT-OVERVIEW.md](PROJECT-OVERVIEW.md)**。它是项目架构、阶段进度、边界和待办的唯一权威总览。

## 文档地图

### 当前权威文档

| 文档 | 用途 |
|---|---|
| [PROJECT-OVERVIEW.md](PROJECT-OVERVIEW.md) | 项目综合介绍、架构、P0–P5 计划、真实进度、风险和下一步 |
| [../README.md](../README.md) | 安装、API 和基本使用入口 |
| [../eval/README.md](../eval/README.md) | 固定检索评测集与基线 |

### `design/`｜仍有效的设计边界

| 文档 | 用途 |
|---|---|
| [P4-P5-ROADMAP.md](design/P4-P5-ROADMAP.md) | 当前 Hermes + SQLite 路线；切片、验收、资料取回、源清理与未来展望（功能未实施） |
| [P3-CONSERVATIVE-SESSION-INGEST.md](design/P3-CONSERVATIVE-SESSION-INGEST.md) | P3 保守采集历史设计；安全原则保留，范围/下一步以新路线为准 |

### `operations/`｜部署与运行

| 文档 | 用途 |
|---|---|
| [EMBEDDING-DEPLOYMENT-WINDOWS.md](operations/EMBEDDING-DEPLOYMENT-WINDOWS.md) | Windows/Ollama embedding 部署与实测坑 |

### `research/`｜调研快照

| 文档 | 用途 |
|---|---|
| [TECH-STACK-RESEARCH-2026-08.md](research/TECH-STACK-RESEARCH-2026-08.md) | 早期数据库、RAG 与技术栈比较；含历史判断，阅读前看顶部说明 |
| [EMBEDDING-MODEL-SELECTION-2026-08.md](research/EMBEDDING-MODEL-SELECTION-2026-08.md) | Embedding 模型选型 |
| [EMBEDDING-OPTIONS-2026-08.md](research/EMBEDDING-OPTIONS-2026-08.md) | 本地/云端部署方案历史比较 |
| [OSS-CANDIDATES-2026-09.md](research/OSS-CANDIDATES-2026-09.md) | 相似本地知识库与 MCP 项目候选 |
| [AGENT-MEMORY-LIFECYCLE-2026-09.md](research/AGENT-MEMORY-LIFECYCLE-2026-09.md) | Agent memory 生命周期和治理调研 |
| [INGESTION-RAG-GOVERNANCE-2026-09.md](research/INGESTION-RAG-GOVERNANCE-2026-09.md) | P3–P5 摄取、RAG 与治理调研 |
| [citations/](research/citations/) | 公开调研引用和核验台账 |

调研文档是带日期的快照，不自动代表当前最新生态。引入依赖前必须重新核验版本、提交活跃度、测试、许可证和 Windows 支持。

### `history/`｜阶段历史与验收记录

| 阶段 | 文档 | 性质 |
|---|---|---|
| 总体早期规划 | [ORIGINAL-ROADMAP-2026-08.md](history/ORIGINAL-ROADMAP-2026-08.md) | 历史方案快照，包含已过时路径和设想 |
| P1 | [P1-ACCEPTANCE-2026-08.md](history/P1-ACCEPTANCE-2026-08.md) | P1 正式验收 |
| P1 | [P1-FTS5-EVALUATION-2026-08.md](history/P1-FTS5-EVALUATION-2026-08.md) | 中文 FTS5 优化过程 |
| P1 | [P1-VECTOR-EVALUATION-2026-08.md](history/P1-VECTOR-EVALUATION-2026-08.md) | 四路检索基线 |
| P1 | [P1-VECTOR-PRODUCTION-2026-08.md](history/P1-VECTOR-PRODUCTION-2026-08.md) | 向量路生产接入实录 |
| P3 | [P3-HERMES-INTEGRATION-READINESS.md](history/P3-HERMES-INTEGRATION-READINESS.md) | 开工前边界，部分工具数已过时 |
| P3 | [P3-MCP-STDIO-IMPLEMENTATION-2026-08.md](history/P3-MCP-STDIO-IMPLEMENTATION-2026-08.md) | MCP 第一切片实录 |
| P3 | [P3-SESSION-INGEST-AND-SUBAGENT-LOGS-2026-08.md](history/P3-SESSION-INGEST-AND-SUBAGENT-LOGS-2026-08.md) | collector 与子 Agent 日志实施记录 |
| P3 | [P3-CANDIDATE-REVIEW-2026-09.md](history/P3-CANDIDATE-REVIEW-2026-09.md) | P3 最终候选审核、审计与主题版本验收 |

## 阅读规则

1. **想知道项目现在到哪了**：只读 `PROJECT-OVERVIEW.md`。
2. **想使用或开发项目**：读仓库根 `README.md`、相关源码和测试。
3. **想追溯为什么这样选**：读 `research/`。
4. **想复盘某阶段发生了什么**：读 `history/`。
5. 历史文档与当前源码冲突时，以当前源码、测试和 `PROJECT-OVERVIEW.md` 为准。

## 维护规则

- 项目专属长文只放本目录；验证工作区 可有精简入口，但不复制路线或进度，不修改历史备份内容；
- 新阶段完成时同步更新 `PROJECT-OVERVIEW.md`，不要另建第二篇“当前总进度”；
- 历史验收报告不覆写，只补过时提示；
- 公开调研必须带日期和引用台账；
- `.env`、token、数据库和私人原始资料不得进入 Git；
- 看板只管理未完成主线，不用大量已完成细卡重复保存项目历史。
- [OPERATIONS-MANUAL.md](OPERATIONS-MANUAL.md)｜面向人类操作员的架构、命令和安全操作手册
