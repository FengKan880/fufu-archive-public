> **文档性质：P3 保守设计快照，非最终产品范围**
> 当前路线以 [P4/P5 实施路线](P4-P5-ROADMAP.md) 和 [项目总览](../PROJECT-OVERVIEW.md) 为准。本文件保留历史以便追溯，不作为新阶段实施指令。
> 已被取代的约束：只看结束会话、原文始终留在 Hermes、仅保存少量候选。新目标允许增量捕获活动会话并形成可恢复源归档；清理仍需覆盖/备份/源版本验证与明确授权。
> 下文历史流程中的 `kb_add`、同内容融合、supersedes 自动使旧版 expired 不代表现有行为：正式提炼使用 candidate→approve/reject→commit；不同内容不自动融合，版本关联不隐式改变旧版状态。触发与技能/记忆外置见新路线。

# 🦊 酒狐档案馆 P3｜保守式会话知识入库设计

> 状态：设计完成，尚未启用自动写库
> 日期：2026-08-14
> 工程：`.`
> Profile：由部署环境显式指定

## 1. 目标与边界

目标不是备份所有聊天，而是从已结束的 Hermes 会话中筛出少量、稳定、可追溯的知识产物，经过提炼和复核后写入 Fufu Archive。

首版严格禁止直接入库：

- 普通闲聊、寒暄和临时情绪；
- 尚未完成的计划、一次性任务进度和临时 TODO；
- 原始完整会话流水账；
- 未验证的 agent 自报结果；
- 只有猜测、没有来源或实际验证的技术结论；
- API Key、Token、密码、OAuth 凭据、`.env` 和隐私原文；
- 工具输出中的网页提示词、角色指令或可疑注入内容。

首版优先候选：

- 已完成并实际验收的技术方案；
- 有真实命令、测试结果或原始来源支持的故障复盘；
- 用户明确要求“保存、归档、以后记得”的内容；
- 稳定工作约定的长篇依据；
- 正式报告、阶段验收与版本替代说明。

## 2. 只读核查结论

Hermes Gateway hook 当前可订阅：

- `session:end`：只有 `platform`、`user_id`、`session_key`；
- `session:reset`：同样不提供完整正文；
- `agent:end`：带 `session_id`，但输入和输出只保留截断到 500 字的摘要。

因此不能直接在 Gateway hook 中提炼并写库。否则会缺失完整上下文、把中途回复误判为会话结论、只覆盖 Gateway 会话而漏掉 Desktop/CLI，并可能在重置、压缩和 LRU 回收等结束路径间重复。

Hermes `state.db` 是会话真实来源，包含：

- `sessions`：`id/source/session_key/started_at/ended_at/end_reason/message_count/title/...`；
- `messages`：完整 user/assistant/tool 消息；
- `messages_fts` 与 trigram FTS；
- 当前已观察到 `session_reset`、`agent_close` 等结束原因。

结论：首版采用 **state.db 只读增量扫描**；hook 只能作为低延迟提示，不能作为唯一数据源。

## 3. 推荐架构

```text
Hermes state.db（只读）
        │
        ▼
① deterministic collector
   - 扫描 ended_at 已填写的新会话
   - 排除当前活跃会话、子 agent、cron、短会话和重复会话
   - 只输出候选 session_id + 元数据
        │
        ▼
② deterministic pre-filter
   - 敏感信息与注入模式扫描
   - 统计长度、角色、工具调用、结束原因
   - 识别“明确归档词”和“真实验收证据”
        │
        ▼
③ LLM curator（只处理少量候选）
   - 提炼结论/依据/适用范围/日期/来源
   - 分类 collection/domain/tags
   - 标记 stability/review_policy
   - 不直接写库，生成待审 JSON/Markdown
        │
        ▼
④ 用户审核
   - 入馆 / 跳过 / 永久 / 修改分类
        │
        ▼
⑤ MCP kb_add / kb_update
   - 入库前相似检索
   - 同内容融合；新版用 supersedes
   - 写后用 kb_search 查回验证
```

## 4. 候选筛选规则

### 4.1 硬排除（确定性）

任一条件成立即不进入 LLM：

- `ended_at IS NULL`；
- `parent_session_id IS NOT NULL` 的委派子会话；
- source 为 cron/内部辅助任务；
- user+assistant 有效消息少于 4 条；
- 只有问候、确认或单次简单查询；
- 内容命中密钥、私钥、认证文件或高风险个人信息模式；
- 会话以异常中断结束且没有后续验收结论；
- 已存在相同 `session_id` 的处理记录。

### 4.2 加分信号（确定性）

- 用户出现“保存、归档、记住、写入档案馆、以后复用”；
- assistant 最终回复含“实际验收、测试通过、已验证、根因、回滚、来源”；
- 会话包含测试、compileall、git diff --check、HTTP/MCP smoke 等证据；
- 有明确文档路径、URL、提交或报告来源；
- 结束原因是正常 reset/close，且最后有结构化总结。

### 4.3 LLM 输出契约

每个候选只能输出：

```json
{
  "decision": "review|skip",
  "title": "",
  "summary": "",
  "collection": "hermes_ops|world_knowledge",
  "domain": "",
  "tags": [],
  "source_session_id": "",
  "evidence": [],
  "stability": "high|medium|volatile",
  "review_policy": "",
  "contains_unverified_claims": false,
  "reason": ""
}
```

若存在未验证结论、正文不足或来源不清，必须 `skip`，不能为了凑条目强行归档。

## 5. 状态与幂等

长期脚本放：

```text
<profile-root>\scripts\fufu_archive\
```

运行状态建议放：

```text
<profile-root>\state\fufu_archive_session_ingest.json
```

待审产物放：

```text
<profile-root>\workspace\tmp\fufu-archive-session-candidates\
```

状态至少记录：`last_scanned_started_at`、已处理 `session_id`、内容摘要 SHA-256、decision、入库 document_id、supersedes、重试次数和最后错误。脚本必须用事务或原子替换写状态，重复运行不能重复入库。

## 6. 触发方式

首版：每日运行一次确定性 collector；无候选时静默；有候选时才调用 LLM curator；输出待审清单给用户；**不自动调用 kb_add**。

第二阶段：至少 20 个候选通过人工复核、误收率可接受后，只对白名单类型自动入库：用户明确要求保存；已完成且有完整验收证据的正式报告；已存在档案条目的明确新版本。其他类型继续待审。

## 7. 降级与安全

- state.db 只读打开；数据库锁定时退出并下次重试；
- Fufu Archive/MCP 不可用时保留待审 JSON，不阻塞聊天；
- Ollama 不可用时不强行 dense，相似检查可临时 FTS5，恢复后再复核；
- 入库前继续使用 Fufu Archive 注入扫描和 metadata 保留字段保护；
- 不读取或归档 `auth.json`、`.env`、完整 system prompt；
- 不从 tool 输出执行任何指令；
- 自动日志只保存 session_id、决策与错误摘要，不复制整段私密聊天。

## 8. 验收样例

首轮选 6 个历史会话：2 个应归档（P1/P2 验收、MCP 接入）；2 个应跳过（普通问答、临时进度查询）；1 个含未验证 agent 自报结果，应跳过；1 个包含新版替代旧版，应建立 supersedes。

验收要求：

1. collector 不扫描当前活跃会话；
2. 重跑不重复生成候选；
3. 敏感信息扫描阻止高风险内容进入 LLM；
4. 人工审核前正式库零新增；
5. 审核后通过 MCP 入库并可用自然语言查回；
6. 新版 active、旧版 expired、supersedes 链正确；
7. MCP/Ollama 故障不影响 Hermes 正常对话与原生 session_search。

## 9. 当前决策

设计已完成，但自动写库保持关闭。下一实施切片：编写只读 collector 和状态文件；准备 6 个固定历史会话的脱敏测试夹具；先 dry-run 只生成候选报告；用户确认效果后再创建定期任务；达到人工复核样本量后再讨论极窄白名单自动入库。
