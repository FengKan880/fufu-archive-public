> **文档性质：历史实施前置**
> 本文是 P3 开工前边界，其中“四工具”等描述已被后续实现扩展；P3 最终状态见 `P3-CANDIDATE-REVIEW-2026-09.md`。

# P3 Hermes 接入前置与实施边界

## 触发场景

Fufu Archive 的 P2 检索/入库能力已经稳定，准备接入当前 Hermes profile；目标包含 MCP 工具、记忆摘要化和会话知识入库。

## 开工前只读基线

1. 工程根目录执行：
   - `git status --short --branch`
   - 工程 venv 的 `compileall`
   - 工程 venv 的全量 `pytest`
   - `git diff --check`
2. Hermes profile 使用明确的 Windows `HERMES_HOME`，只读检查：
   - `hermes config path`
   - `hermes config check`
   - `hermes mcp list`
   - `hermes tools list`
   - `hermes config get tools.include`
   - `hermes config get mcp_servers`
3. 记录现有 MCP、工具白名单和记忆规模；不要回显密钥、Token、`.env` 或记忆正文。

## 推荐接入路线

首版优先使用**本地 stdio MCP adapter**，由 Hermes 启动 Fufu Archive MCP 子进程，适配现有 `KnowledgeBase`。除非确有跨进程/跨主机需要，不先开放 HTTP MCP 端口。

工具首版保持窄接口：

- `kb_search`
- `kb_add`
- `kb_update`
- `kb_list`

注册与验证使用 Hermes 原生命令；不要手改 `config.yaml`：

```text
hermes mcp add ...
hermes mcp test ...
hermes mcp list
```

确认实际发现的工具名为 `mcp_<server>_<tool>` 形式，并确认失败时原生 memory/session_search 仍可用。

## 会话知识入库边界

首版只自动处理明确的知识候选：归档总结、完成成果、稳定操作方案、用户明确要求保存的内容。不要把普通闲聊、临时故障过程、未验证 agent 自报结果、原始完整会话流水账直接入库。

分类建议：

- `hermes_ops`：运行档案、记忆快照、日报/月报、会话提炼、技能快照
- `world_knowledge`：VRC、游戏、电厂工作、工具等现实知识

复用现有 chunking、metadata、tags、注入扫描和 supersedes 语义；入库失败不得阻塞主对话。

## 集成安全边界

宿主配置、记忆和会话来源的修改必须由部署维护者显式授权。外部开发工具只应访问隔离的项目副本，不应自动修改宿主身份、配置或私人资料。

## 验收

- MCP 服务能被发现，四个工具逐项可用；
- 中文检索、过滤、metadata/tags、更新/版本关系通过；
- MCP 不可用时原生 memory/session_search 降级可用；
- 记忆瘦身前后内容和字符统计可复核；
- 明确归档样例能入库并通过 MCP 查回；
- 未发生 Hermes 外部文件、密钥或数据目录的未授权写入。
