# 酒狐档案馆 · Fufu Archive

> **2026-09-10 交接更新：** P4 工作树已推进至 P4.3-1 queue 核心；P4.1/P4.2 有实现但仍有路线出口与可靠性待验收项，不能视为全部完成。当前复测 195 passed / 5 skipped。旧文中的“P4 未实施”及旧清单属于历史状态。请先读 [最新 P4 交接](docs/P4-HANDOFF-2026-09-10.md)；本轮暂停开发，换会话续作。

> 面向 Hermes 长期工作的本地日志、知识与记忆服务（SQLite 优先）

Fufu Archive 当前提供文本入库、中文关键词检索、可选向量/混合检索，以及受 token 保护的 HTTP API。P2 的首个窄切片加入了显式启用的中英文文本切块；应用不会自动下载模型。P3 已加入本地 stdio MCP、候选审核、审计追溯、revision 防覆盖，以及可选 `topic_key` 的去重和显式版本关联。

项目架构、P0–P5 计划、真实完成度、未完成事项和维护边界统一记录在 [`docs/PROJECT-OVERVIEW.md`](docs/PROJECT-OVERVIEW.md)；全部历史、设计、运维和调研文档见 [`docs/README.md`](docs/README.md)。

**当前与规划：** P0–P3 核心已实现；[P4/P5 路线](docs/design/P4-P5-ROADMAP.md)尚未实施。P4 补齐可靠性、Hermes 新活动会话的可恢复归档、自有调度、可选提炼与无模型降级；P5 补齐管理界面、skill/memory 正文取回和受控源清理。源归档与正式知识分层，摘要不承诺无损；真实源删除须有覆盖、备份恢复证明和明确授权。其他数据库与 Agent 仅列未来展望，不属于当前适配范围。

## 特性与边界

- SQLite migration 管理，包含 `documents`、`chunks`、`metadata` 和 FTS5 索引。
- 中文安全预分词：安装 `jieba` 时使用它；未安装时自动降级为字符/二元词切分，仍可检索中文。
- `/kb/*` 必须携带环境变量配置的 Bearer token；`/health` 无需鉴权。
- 所有 SQL 值均使用参数绑定。FTS 查询也只由受控分词结果构造。
- `collection`、`domain`、`status`、`parent_id`、`supersedes` 已在基础模型中预留。
- 入库可保存 `title`、`content_type`、`language`、`tags`，同时保留任意 `metadata`。
- 候选先处于不可检索的 `pending`，只有人工/受控流程审核为 `approved` 后才能提交；`rejected` 永不进入正式检索。
- `topic_key` 是可选稳定主题标识。同一 `topic_key` 的完全相同内容会由 SHA-256 去重；不同内容不会被自动合并或覆盖。
- 完整 P3 流程、MCP 工具、文件职责和验收记录见 [`docs/history/P3-CANDIDATE-REVIEW-2026-09.md`](docs/history/P3-CANDIDATE-REVIEW-2026-09.md)。

## 本地运行（Python 3.11）

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1  # Windows PowerShell
pip install -e ".[dev,chinese]"
Copy-Item .env.example .env
# 编辑 .env，将 FUFU_ARCHIVE_API_TOKEN 改为随机长 token；再由 shell 或进程管理器加载它
$env:FUFU_ARCHIVE_API_TOKEN = "your-long-random-token"
fufu-archive-init-db
fufu-archive
```

服务默认在 `http://127.0.0.1:8000` 运行。也可以不安装 `jieba`：使用 `pip install -e ".[dev]"`，中文搜索仍能工作。
`.env` 是部署工具可读取的示例文件；应用本身只读取进程环境变量，不会隐式加载本地 `.env`。

### jieba 领域词典

版本库内置的通用领域词位于 `resources/dict/*.txt`。安装 `jieba` 时，分词器会按固定文件顺序加载存在的词典，并为每组词典路径缓存独立的 tokenizer；缺少文件或未安装 `jieba` 时会继续使用原有的安全降级分词。词典修改后需重启进程（测试可调用 `clear_jieba_tokenizer_cache()`）。

添加词条时，将不含隐私信息的稳定通用术语写入对应文件，每行使用 jieba userdict 格式 `词语 频率 词性`，例如 `红石电路 100000 n`。临时或测试词典可通过 `tokenize(text, dictionary_paths=(path,))` 显式指定，不会污染 jieba 的全局 tokenizer。

### Read-only database doctor

```bash
fufu-archive-doctor path/to/archive.sqlite3 --json
```

The doctor requires an explicit SQLite path, opens it with URI `mode=ro` and
`PRAGMA query_only=ON`, and never creates, migrates, repairs, backs up, or
modifies the target database. It exits nonzero for integrity, schema,
foreign-key, or structural failures; unavailable optional vector checks are
reported separately in the JSON result.

### SQLite backup and isolated restore check (P4.1)

```bash
fufu-archive-backup backup path/to/archive.sqlite3 path/to/archive-backup.sqlite3
fufu-archive-backup restore-check path/to/archive.sqlite3 path/to/isolated-restore.sqlite3
fufu-archive-backup verify path/to/archive.sqlite3 path/to/isolated-restore.sqlite3
```

All paths are explicit. The source is opened read-only and copied with the
SQLite backup API. `restore-check` verifies the isolated destination with
integrity, foreign-key, schema, FTS, candidate, and audit reads, emitting
stable JSON. It never rebuilds optional vector indexes; use a separate,
explicitly authorized destination for restore drills.

## API

健康检查（公开）：

```bash
curl http://127.0.0.1:8000/health
```

写入文档：

```bash
curl -X POST http://127.0.0.1:8000/kb/ingest \
  -H "Authorization: Bearer your-long-random-token" \
  -H "Content-Type: application/json" \
  -d '{"content":"Fufu Archive 支持中文知识检索。", "collection":"notes", "metadata":{"author":"demo"}}'
```

`/kb/ingest` 与 `/kb/upload` 默认对原始入库文本执行无依赖、确定性的提示注入启发式扫描，并将结果保存为 metadata 的保留键 `security.injection_scan`。扫描只报告命中的模式 ID/类别，绝不执行、删除、改写或拒绝内容；FTS 与向量仍只索引原始内容（或其原样切块），不会索引扫描 metadata。该扫描不是安全边界，可能误报或漏报，也不能替代内容隔离与调用侧权限控制；它不调用 LLM 或外部服务。

需要跳过时，JSON ingest 传入 `"scan_injection": false`，multipart upload 传入表单字段 `scan_injection=false`；跳过后不写入该 scanner metadata。除保留的 scanner 键外，调用方提供的任意 metadata 保持不变。

省略 `chunking` 时保持原有单 chunk 行为。显式传入后按 Unicode 字符数切分，例如：

```json
{
  "content": "需要切分的长篇中英文内容……",
  "chunking": {"enabled": true, "target_size": 1000, "overlap": 100}
}
```

多 chunk 文档会建立一条不可直接检索的 parent context 关系记录；FTS 与向量索引只包含其 child chunks。入库响应的 `chunk_count` 与 `parent_chunk_count` 会反映两类记录数量。

上传 UTF-8 文本文档：

```bash
curl -X POST http://127.0.0.1:8000/kb/upload \
  -H "Authorization: Bearer your-long-random-token" \
  -F "file=@notes.md" \
  -F "collection=notes" \
  -F 'tags=["upload","demo"]' \
  -F 'metadata={"author":"demo"}' \
  -F 'chunking={"enabled":true,"target_size":1000,"overlap":100}'
```

仅支持 `.txt`、`.md`、`.json`（扩展名不区分大小写）。可选文本字段为 `collection`、`domain`、`status`、`source`、`title`、`content_type`、`language`；`scan_injection` 是布尔表单字段；`tags` 必须是 JSON 数组，`metadata` 和 `chunking` 必须是 JSON 对象。JSON 文件会先安全解析，再按键排序为紧凑 JSON 文本后入库，不执行文件内容。上传默认上限为 1,000,000 bytes，可用正整数环境变量 `FUFU_ARCHIVE_UPLOAD_MAX_BYTES` 调整；超限返回 `413`，不支持的扩展名返回 `415`，空文件、非法 UTF-8 或非法 JSON 返回 `422`。

检索：

```bash
curl -X POST http://127.0.0.1:8000/kb/search \
  -H "Authorization: Bearer your-long-random-token" \
  -H "Content-Type: application/json" \
  -d '{"query":"中文检索", "collection":"notes", "limit":5}'
```

确定性查询改写默认关闭，以保持原有检索与评测行为不变。显式传入 `"rewrite": true` 后，服务最多使用 3 个有序查询（原始查询、标点/空白规范化查询、去除已配置中英文会话填充词的查询）执行所选 `mode`，再按查询顺序合并并按 chunk 去重；响应会额外包含 `rewritten_queries`。改写不生成同义词、不调用 LLM，也不理解查询语义；所有变体仍经过现有的安全 FTS 分词/转义以及 collection/domain/status 过滤和向量降级路径。

如果没有设置 `FUFU_ARCHIVE_API_TOKEN`，`/kb/*` 会返回 `503`，不会回退到硬编码或默认 token。

## 测试

```bash
pip install -e ".[dev]"
pytest
```

测试覆盖数据库 migration、单/多 chunk ingest、FTS/向量索引、中文检索、token 鉴权、公开 health endpoint，以及 P3 候选审核、revision、审计、并发提交和 topic 去重。MCP adapter 的单测依赖可选 `mcp` extra；实际 Hermes stdio MCP 冒烟命令见 P3 文档。

## 向量检索（P1 已接入）

- 检索模式：`/kb/search` 支持 `mode` 字段：`fts5`（关键词）、`dense`（向量，默认）、`hybrid`（RRF 融合）
- 向量后端可插拔：`src/fufu_archive/embedding.py`（默认 Ollama `qwen3-embedding:0.6b`，配置 `FUFU_ARCHIVE_EMBED_URL/MODEL`）
- 降级策略：Ollama 不可用或 sqlite-vec 未装时自动退回 FTS5，检索永不硬失败
- 评测基线见 `eval/README.md`（dense-prefix recall@10=1.000 / MRR@10=1.000）

## 本地 stdio MCP（P3）

MCP adapter 直接访问本地 SQLite facade，不经过 HTTP API，因此无需把 HTTP Bearer token 复制到 Hermes MCP 配置。它提供 `kb_search`、`kb_add`、候选审核工具、审计查询、`kb_update` 与 `kb_list`；可用工具与审核顺序见 [`docs/history/P3-CANDIDATE-REVIEW-2026-09.md`](docs/history/P3-CANDIDATE-REVIEW-2026-09.md)。

## 操作员手册

面向人类操作员的架构说明、命令行、HTTP、备份、队列、MCP 和安全边界见 [`docs/OPERATIONS-MANUAL.md`](docs/OPERATIONS-MANUAL.md)。
