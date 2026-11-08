> **文档性质：P3 实施记录**
> 本文记录 MCP 第一切片；后续已扩展到 10 个工具并兼容 MCP SDK 2.x，最终状态见 `P3-CANDIDATE-REVIEW-2026-09.md`。

# P3 本地 stdio MCP 接入实录（2026-08）

适用：Fufu Archive / 本地 SQLite 知识库接入 Hermes Agent，且不希望额外开放 HTTP MCP 端口或复制 API Token。

## 推荐架构

```text
Hermes
  └─ stdio MCP
       └─ fufu_archive.mcp_server
            └─ KnowledgeBase → SQLite
```

MCP 适配器直接复用 `KnowledgeBase`，提供四个窄工具：

- `kb_search(query, collection?, domain?, limit?, mode?)`
- `kb_add(content, collection?, domain?, source?, title?, tags?, metadata?, chunking?)`
- `kb_update(document_id, status?, source?, title?, supersedes?, metadata?, tags?)`
- `kb_list(collection?, domain?, status="active", limit?)`

`kb_update` 首版只更新生命周期和描述字段，不重写正文、不重新切块；正文重写应另建版本并用 `supersedes` 连接。

## 依赖隔离

项目的 MCP SDK 可以声明为 optional extra，例如 `mcp = ["mcp>=1.0"]`，不要让项目基础 venv 因缺少 SDK 而无法运行 P0-P2 测试。MCP 外壳对 SDK 使用延迟导入；核心数据库测试不依赖 SDK。若测试环境没有 SDK，协议测试可 skip，但必须使用实际拥有 SDK 的 Hermes venv 做协议级 smoke。

Windows Hermes 注册时使用 Hermes 自带 Python，并通过环境变量把源码目录传给子进程：

```bash
hermes mcp add fufu-archive \
  --command <hermes-python> \
  --env PYTHONPATH=./src \
        FUFU_ARCHIVE_DB_PATH=./data/fufu_archive.db \
  --args -m fufu_archive.mcp_server
```

注册命令会连接服务并询问 `Enable all N tools? [Y/n/select]`。非 PTY 调用可能自动取消；需要用 PTY，明确回答 `y` 后若尚未提交再发送回车。不要盲目确认其他提示。

## 必须验证的三层

1. `hermes mcp list`：服务为 enabled，工具数正确。
2. `hermes mcp test fufu-archive`：stdio 连接、工具发现成功。
3. MCP ClientSession 真实协议 smoke：`initialize → list_tools → kb_add → kb_search`，使用临时数据库，确认 structuredContent 和中文检索结果。

项目侧同时运行：

```bash
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
  .venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m compileall -q src tests
git diff --check
```

验收时 `git diff` 不包含 `??` 未跟踪文件；必须检查新增 MCP 源码和测试。

## 安全与边界

- stdio 本地服务可以不配置 API Token；不要把 Token 写入 Hermes MCP env。
- `kb_add` 继续走现有 `IngestRequest` 校验、chunking 和注入扫描。
- `kb_update` 也必须拒绝非 JSON metadata、限制 tags 长度，并过滤保留的 `security.injection_scan`，避免调用方伪造扫描结果。
- 默认 `kb_list` 只列 `active`；查询 expired 时显式传 `status="expired"`。
- 会话自动入库首版只处理明确标记/总结/用户要求保存的知识，不要全量写原始流水账；记忆瘦身和自动入库应与 MCP 工具稳定性分开验收。

## 这次遇到的可迁移教训

- MCP SDK 缺失是依赖层问题，不要把它记录成“项目不支持 MCP”；采用 optional extra + 延迟导入 + Hermes venv 协议 smoke。
- Hermes `mcp add` 的工具发现成功不等于配置启用成功；必须看到 `Saved ... (N/N tools enabled)`，随后再独立运行 `mcp test`。
- list/update 的生命周期测试要同时验证默认 active 过滤、显式 expired 查询和 `supersedes` 返回字段，避免只测 UPDATE 成功而漏掉管理端读取契约。
