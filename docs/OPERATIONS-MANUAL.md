# 🦊 酒狐档案馆操作手册
## Fufu Archive 面向人类操作员的使用说明

> **适用版本：** 当前 P4 受控验证工作树
>
> **重要状态：** 当前有命令行、HTTP API 和本地 MCP 接口，但没有 Web 后台控制台，也没有常驻后台调度器。生产自动采集、cron、自启和真实源会话清理均未启用。

---

## 1. 这个项目是做什么的？

Fufu Archive 是一个给 Hermes 和其他 AI 使用的**本地知识档案库**。

可以把它理解成一个更适合 AI 查找的个人资料柜：

- 把已经确认的资料、经验和操作方案保存下来；
- 让 AI 能按关键词或语义查找；
- 记录资料来自哪里、什么时候产生、是否经过审核；
- 新内容先放在“待审核区”，确认后才进入正式知识库；
- 数据库、备份和任务失败时可以检查、恢复和重试。

它不是：

- 自动把所有聊天都变成知识的黑盒；
- 自动替用户决定什么是真实知识的模型；
- ChatGPT 或 Hermes 的替代品；
- 目前已经上线的 Web 管理后台；
- 默认会长期运行、自动扫描或删除原始会话的服务。

核心原则是：**先保留来源，再审核提炼；先可恢复，再考虑自动化；没有足够证据时宁可不收录。**

---

## 2. 用普通话解释整个架构

### 2.1 一句话架构

```text
人类 / Hermes / 本地程序
          │
          ├── 命令行 CLI：运维、备份、检查、队列控制
          ├── HTTP API：入库、上传、检索
          └── stdio MCP：让 Hermes 调用知识库
                         │
                         ▼
              KnowledgeBase 业务层
                         │
                         ▼
                 SQLite 单文件数据库
                  ├─ 正式文档
                  ├─ 文档片段
                  ├─ 候选和审核状态
                  ├─ 审计记录
                  ├─ FTS5 关键词索引
                  └─ 可选 sqlite-vec 向量索引
                         │
                         ▼
          Ollama embedding（可选，当前 qwen3-embedding:0.6b）
```

### 2.2 每一层负责什么？

#### 入口层

入口层是“人或程序怎么和它说话”：

- **CLI**：给操作员使用，例如检查数据库、查看队列、备份和重试；
- **HTTP API**：给本机脚本、未来界面或其他程序使用；
- **MCP**：给 Hermes 通过本地 stdio 调用；不开放网络端口。

三个入口最终尽量共用同一个 `KnowledgeBase` 业务层，避免 HTTP 和 MCP 各自实现一套不同规则。

#### 业务层

业务层负责：

- 保存正式文档；
- 生成文本片段；
- 建立关键词和向量索引；
- 搜索并按 collection/domain/status 过滤；
- 管理候选状态；
- 检查版本冲突；
- 记录审计事件；
- 防止相同候选被并发提交两次。

#### SQLite 数据层

SQLite 是当前的核心数据库，不需要 Redis 或独立数据库服务。

大致分为：

- `documents`：正式知识；
- `chunks`：文档拆分后的检索片段；
- `candidates`：还没有进入正式知识的候选；
- `audit_events`：候选和文档的操作历史；
- FTS5：关键词检索；
- `chunk_vec`：可选的向量检索派生表。

当前 migration 版本为 v5。向量索引是可以重建的派生数据，正式文本和 FTS5 不应依赖向量服务才能工作。

#### embedding 层

embedding 的作用是把文字转换成数字向量，让系统能找“意思相近”的内容。

当前使用：

```text
Ollama
模型：qwen3-embedding:0.6b
默认地址：http://127.0.0.1:11434
默认维度：1024
```

如果 Ollama 或向量索引不可用：

- 正式文本仍然保存；
- FTS5 关键词检索仍然可用；
- dense/hybrid 会按代码策略降级到 FTS5。

当前 embedding 模型**不是聊天模型，也不是语义摘要模型**，不能直接用于“总结会话”。

---

## 3. 数据从哪里来、怎样变成知识？

### 3.1 普通资料入库

```text
文本 / Markdown / JSON
        │
        ▼
参数、大小、格式和安全检查
        │
        ▼
可选注入模式扫描、分块、metadata/tag
        │
        ▼
写入 documents/chunks/FTS5
        │
        ├─ 向量服务正常：尝试建立向量
        └─ 向量服务异常：保留文本和 FTS5
```

### 3.2 候选审核

自动或人工整理出来的内容不能直接成为正式知识：

```text
candidate_add
      │
      ▼
   pending
   ├── approve → approved → commit → 正式 document
   └── reject  → rejected
```

只有 `approved` 候选才能提交。候选生成过程不应调用 `kb_add` 绕过审核。

### 3.3 Hermes 会话归档

Hermes 会话归档是另一条更谨慎的流程：

```text
Hermes state.db（只读）
          │
          ▼
增量采集 / active snapshot
          │
          ▼
source archive + coverage manifest
          │
          ▼
证据完整性检查
          │
          ▼
deterministic pending candidate
          │
          ▼
人工审核后才可能进入正式库
```

当前真实 shadow 验收表明，真实会话存在工具正文排除、缺失内容和 cleanup blocked 情况，所以系统会选择不生成候选。这个“0 条候选”是安全拦截，不是程序没有运行。

---

## 4. 安装和目录约定

项目目录：

```text
.
```

项目虚拟环境：

```text
.\.venv\Scripts\python.exe
```

推荐先安装开发依赖：

```bash
cd <project-root>
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

MCP SDK 是可选依赖。如果要在项目虚拟环境直接运行 MCP 测试：

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev,mcp]"
```

当前验收没有擅自安装这个依赖；项目基础测试缺少它时，5 项 MCP 测试会 skip。真实 MCP 协议 smoke 应使用已经安装 MCP SDK 的 Hermes agent venv，或固定项目 CI 环境。

数据库默认位置由环境变量决定，建议始终使用明确路径：

```text
FUFU_ARCHIVE_DB_PATH
FUFU_ARCHIVE_API_TOKEN
FUFU_ARCHIVE_HOST
FUFU_ARCHIVE_PORT
FUFU_ARCHIVE_EMBED_URL
FUFU_ARCHIVE_EMBED_MODEL
FUFU_ARCHIVE_EMBED_DIM
FUFU_ARCHIVE_UPLOAD_MAX_BYTES
```

不要把真实数据库、token、`.env`、Hermes `state.db` 或完整聊天正文提交到 Git。

---

## 5. 数据库初始化和健康检查

### 5.1 初始化数据库

环境变量方式：

```bash
FUFU_ARCHIVE_DB_PATH=data/fufu_archive.db \
.venv/Scripts/python.exe -m fufu_archive.db
```

更推荐使用项目入口：

```bash
FUFU_ARCHIVE_DB_PATH=data/fufu_archive.db \
fufu-archive-init-db
```

初始化/迁移属于写操作。正式库操作前先确认路径和备份，不要把拼错的路径当成正式数据库。

### 5.2 只读 doctor

```bash
.venv/Scripts/python.exe -m fufu_archive.doctor path/to/fufu_archive.db
```

JSON 输出是默认格式，也可以显式写：

```bash
.venv/Scripts/python.exe -m fufu_archive.doctor \
  --json path/to/fufu_archive.db
```

doctor 用于发现：

- SQLite 完整性问题；
- migration 或表结构问题；
- 正式文档、片段、候选和审计之间的不一致；
- FTS/向量等派生数据状态。

它是诊断工具，不等于自动修复工具。看见异常后先备份、定位，再决定是否修复。

---

## 6. 备份和恢复验证

所有路径都要显式指定：

```bash
# 创建一致性备份
.venv/Scripts/python.exe -m fufu_archive.backup \
  backup source.db destination.db

# 在隔离目标中做恢复检查
.venv/Scripts/python.exe -m fufu_archive.backup \
  restore-check source.db restored.db

# 验证指定源和目标是否一致
.venv/Scripts/python.exe -m fufu_archive.backup \
  verify source.db restored.db
```

注意：

- `restore-check` 适合检查目标能否恢复、读取和查询；
- `verify` 才用于比较指定源和目标的核心内容；
- “目标数据库能打开”不等于“它就是这份源数据库的正确恢复”；
- 不要用无关的健康数据库冒充恢复结果；
- 生产恢复应先在隔离目录验证，再考虑替换正式库。

---

## 7. 启动 HTTP 服务

当前 HTTP 服务是 API 服务，不是后台控制台：

```bash
FUFU_ARCHIVE_DB_PATH=data/fufu_archive.db \
FUFU_ARCHIVE_API_TOKEN='只在本机环境变量中设置' \
.venv/Scripts/python.exe -m fufu_archive.main
```

主要接口：

```text
GET  /health
POST /kb/ingest
POST /kb/upload
POST /kb/search
```

规则：

- `/health` 公开；
- `/kb/*` 需要 Bearer token；
- 没有 token 时受保护接口 fail-closed；
- 不要把 token 写进文档、命令历史、Git 或 MCP 配置；
- `/health` 只代表应用层能响应，不代表数据库、向量和 Ollama 全部健康。

示例：

```bash
curl http://127.0.0.1:8000/health
```

```bash
curl -X POST http://127.0.0.1:8000/kb/search \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"中文检索","collection":"notes","limit":5}'
```

---

## 8. 任务队列和人类操作员命令

### 8.1 重要边界

任务队列是**一次性执行核心**，当前没有：

- 常驻 worker；
- scheduler；
- cron 自动接管；
- 自动发现生产数据库路径；
- 自动启动采集。

所有 queue 命令都必须显式给出 queue 数据库路径。

查看完整参数：

```bash
.venv/Scripts/python.exe -m fufu_archive.task_runner --help
```

### 8.2 查看队列

```bash
# 查看任务，不领取、不执行
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db \
  --inspect

# 只看某种状态
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db \
  --inspect --status failed

# 查看 queued/running/succeeded/failed 统计和暂停状态
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db \
  --summary

# 只读完整性检查
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db \
  --health
```

### 8.3 执行一次任务

```bash
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db \
  --worker-id operator-worker \
  --max-tasks 10
```

它执行一批后返回，不会变成后台服务。

### 8.4 lease 和失败任务

```bash
# 回收已经过期的 lease
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db \
  --reclaim-expired
```

手动重排队需要本地操作互锁，并且必须写明原因：

```bash
FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR=1 \
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db \
  --retry-failed TASK_ID \
  --reason "已确认上游故障已恢复"
```

### 8.5 暂停和恢复

```bash
# 暂停领取新任务
FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR=1 \
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db --pause

# 查看持久化暂停状态
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db --control-status

# 恢复领取
FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR=1 \
.venv/Scripts/python.exe -m fufu_archive.task_runner \
  --queue-db path/to/queue.db --resume
```

暂停只阻止新任务领取，不会偷偷取消已经 running 的 lease。

### 8.6 操作员安全说明

`FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR=1` 只是告诉程序“这是明确的本地操作”，不是：

- 用户登录；
- 管理员身份认证；
- 远程权限系统；
- 不可伪造的授权凭据。

因此当前 queue CLI 适合用户在本机手动使用，不适合直接暴露到远程网络。

---

## 9. 会话归档和 deterministic refinement

### 9.1 deterministic refinement

查看参数：

```bash
.venv/Scripts/python.exe -m fufu_archive.deterministic_refinement --help
```

基本形式：

```bash
.venv/Scripts/python.exe -m fufu_archive.deterministic_refinement \
  --source-archive path/to/source-archive.json \
  --db path/to/archive.db \
  --profile example
```

它的特点：

- 不调用 LLM；
- 不做语义总结；
- 按原消息顺序保留可验证文本；
- 保存 `profile/session/hash/source_message_id` 证据；
- 只能创建 `pending` candidate；
- 不会自动 approve 或 commit。

只有在确认缺口属于明确、可计数的声明排除时，才可以显式使用：

```bash
--allow-declared-exclusions
```

真正的 malformed、unknown role、缺失正文、cleanup blocked 仍应被拒绝。

### 9.2 真实 Hermes state.db

正式 Hermes 数据库只能：

- 只读检查 schema、数量、状态和 hash；或
- 复制到临时 shadow 后进行行为测试。

不要把正式 `state.db` 直接作为写入目标，不要把真实消息正文打印到终端或报告，不要在没有源版本、覆盖清单和可恢复备份时清理原始会话。

---

## 10. MCP 给 Hermes 的接口

当前 MCP 是本地 stdio 服务，不是 HTTP 控制台。

包含：

```text
kb_search
kb_add
kb_candidate_add
kb_candidate_list
kb_candidate_approve
kb_candidate_reject
kb_candidate_commit
kb_audit_events
kb_update
kb_list
```

Hermes 注册/测试由 Hermes 命令完成，典型检查：

```bash
hermes mcp list
hermes mcp test fufu-archive
```

实际 MCP server 使用的数据库路径必须是明确的测试库或正式档案库路径，不能依赖模糊的当前工作目录。

MCP 发现工具成功，不等于每个工具都通过；验收时要实际调用候选创建、审核、提交、检索和审计链。

---

## 11. 常见问题排查

### 搜索不到内容

1. 先用 `mode=fts5` 测试关键词检索；
2. 检查 collection/domain/status 过滤条件；
3. 用 doctor 检查数据库和 FTS；
4. 再检查 Ollama/向量索引；
5. 不要把 dense 失败误判成正式文本丢失。

### Ollama 不可用

这是允许的降级场景：

- FTS5 仍应可用；
- dense/hybrid 应降级；
- 不要因为一次 embedding 失败就删除或重建正式文档；
- 恢复 Ollama 后，向量补建需要明确的后续操作，当前没有自动补建闭环。

### queue 不动

1. 执行 `--summary`；
2. 检查是否 paused；
3. 执行 `--inspect --status running` 看 lease；
4. 必要时显式执行 `--reclaim-expired`；
5. 确认 backoff 尚未到期；
6. 不要连续强制重试永久失败任务。

### doctor 报数据库异常

1. 停止写入操作；
2. 复制数据库做隔离检查；
3. 先备份；
4. 用 `restore-check` 验证恢复副本；
5. 不要直接删除 WAL/SHM 或手工改 migration 表。

### 想让项目自动总结会话

当前不能只靠配置打开：

- deterministic refinement 不是语义摘要；
- 当前 Ollama 模型是 embedding 模型；
- OpenAI ChatGPT/Codex 订阅不能直接当普通 OpenAI API Key；
- DeepSeek 网页订阅不能代替 DeepSeek API Key；
- 需要先实现独立、可替换的 LLM provider，并保持“生成只能产生 pending candidate”的权限边界。

---

## 12. 当前没有实现的功能

以下属于规划或后续阶段，不要在操作手册外自行假定已经存在：

- Web 后台控制台；
- 候选审核网页；
- 图形化任务队列面板；
- 常驻 worker 和 scheduler；
- 自动 cron 采集；
- 真实生产权限认证；
- 自动会话清理；
- 自动语义提炼 provider；
- 自动向量缺失补建；
- PDF、DOCX、XLSX 等复杂文档解析；
- 远程多用户管理。

---

## 13. 操作员最小安全清单

执行写操作前确认：

- [ ] 数据库路径是目标数据库，不是拼错的新路径；
- [ ] 已有可恢复备份；
- [ ] 正式库、测试库和 shadow 库没有混淆；
- [ ] 没有把 Hermes `state.db` 当成档案馆数据库；
- [ ] 不会把真实正文、token 或密钥写入日志；
- [ ] 任务操作带明确 worker、reason 和 queue 路径；
- [ ] 只读检查没有产生 WAL、迁移或新数据库；
- [ ] 候选仍经过 pending → approve → commit；
- [ ] 没有因为测试通过就开启常驻调度或删除源会话。

---

## 14. 当前阶段结论

Fufu Archive 当前是一个**本地优先、可被 Hermes 调用、带审核和恢复边界的知识库底座**。

它已经有：

- SQLite/FTS5/可选向量检索；
- HTTP API；
- 本地 stdio MCP；
- 候选审核和审计；
- 一次性 queue/Runner；
- doctor、backup、restore-check；
- Hermes 会话 shadow 归档和 deterministic pending-only 边界；
- embedding 故障降级。

它还不是：

- 完整后台管理产品；
- 自动知识整理机器人；
- 已授权的生产自动化系统。

当前正确的使用方式是：**操作员显式执行、每一步指定路径、先检查和备份、候选先审核、失败可恢复、证据不足就停下。**
