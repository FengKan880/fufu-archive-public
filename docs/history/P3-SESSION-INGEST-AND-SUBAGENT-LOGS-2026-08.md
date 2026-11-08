> **文档性质：P3 实施记录**
> 本文记录会话采集器与子 Agent 日志边界；其中当时“尚未 dry-run”的状态已过时，当前状态见 `../PROJECT-OVERVIEW.md`。

# P3 保守式会话提炼与子 Agent 优化日志（2026-08）

## 已验证的设计结论

Hermes 会话知识不能直接从 Gateway hook 自动入库：

- `session:end` / `session:reset` 不提供完整正文；
- `agent:end` 的输入与输出只保留截断摘要；
- 完整来源是 profile `state.db` 的 `sessions` / `messages`；应以 SQLite `mode=ro` + `PRAGMA query_only=ON` 打开。

因此采用分层管线：

1. 确定性 collector 只扫描 `ended_at IS NOT NULL`；
2. 根会话进入知识候选筛选；
3. `parent_session_id IS NOT NULL` 的子 Agent 会话进入独立优化日志，不直接作为知识候选；
4. 敏感信息、注入、重复与短会话先确定性过滤；
5. LLM 只提炼少量候选，默认生成待审 JSON/Markdown；
6. 用户审核后才调用 `kb_add` / `kb_update`；新版使用 `supersedes`，旧版标 `expired`；
7. 写后必须自然语言查回并检查数据库完整性。

## 子 Agent 日志契约

项目支持保留子 Agent 会话供优化，但与正式知识库隔离。日志可保存：

- session ID、parent session ID、source、title；
- 起止时间、结束原因；
- user/assistant 消息数、工具调用数、去重后的工具名；
- user+assistant 内容 SHA-256；
- 通过敏感扫描后的最终回复短预览。

禁止保存：

- tool 输出正文；
- system prompt、`.env`、`auth.json`、密钥和认证材料；
- 命中敏感规则的正文或预览；
- 未经审核而将子 Agent 自报结论写成正式知识。

优化统计方向：无最终回复、异常结束、高频失败工具、重复返工、任务类型与工具链/耗时/成功率关系。

## Collector 安全与幂等

- 默认 dry-run，报告显式写 `archive_writes: 0`；
- 状态提交必须单独开关；状态文件原子替换；
- 按 session ID + 内容摘要去重；
- 不扫描当前活跃会话；cron/内部辅助任务单独排除；
- 档案馆或 Ollama 不可用时保留待审报告，不阻塞 Hermes，并降级到原生 memory/session_search；
- 至少积累 20 个人工复核样本后，才讨论极窄白名单自动入库。

## Windows SQLite 测试注意事项

Python `sqlite3.Connection` 的 context manager 管事务，不保证离开 `with` 后关闭句柄。Windows 测试若紧接着删除临时数据库，必须显式 `close()`（推荐 `try/finally`），否则可能出现 WinError 32。修复后必须重新运行单测；未复跑通过前只能报告“已修补，待验收”，不能宣称完成。

## 当时的验收边界

本文记录早期采集方案，不是当前完成状态。FTS5/dense/hybrid 检索、生命周期与会话采集应分别保留可复现验证；初版 Windows SQLite 句柄问题需要回归测试。未完成真实来源库 dry-run 或完整复验时，不能宣称 collector 已通过验收。后续结果以项目总览及最新验收报告为准。
