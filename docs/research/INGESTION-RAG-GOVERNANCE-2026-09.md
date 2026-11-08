> **文档性质：公开项目调研**
> 本文是 2026-09 的摄取、RAG 与治理调研快照；许可证和活跃度结论按文中核验日期理解。

# Fufu Archive P3–P5：开源文档入库、RAG 数据治理与本地知识库复用调研

> 调研日期：2026-09-06。目标是找可组合模块、算法与测试思想，不建议用任何一套全家桶替换现有 SQLite + FTS5 + sqlite-vec + FastAPI 架构。

## 结论先行

1. **解析层首选 Docling 作为可插拔增强器**：原生 Windows、MIT、统一 `DoclingDocument`、无损 JSON、层级/混合 chunker、页级与结构级信息都适合转成 Fufu 的 canonical artifact。它是解析库，不碰索引、版本和权限，边界最干净。[1]
2. **Marker 只做困难 PDF 的二级回退/离线工具**：代码已是 Apache-2.0，但模型权重是修改版 OpenRAIL-M，免费商用有“融资/营收低于 500 万美元”等条件；若未来分发或商用，不能只看代码许可证。CPU fast mode 可在 Windows 用，balanced/GPU 路径明显更重。[4]
3. **Unstructured 最值得抄的是 ingest 状态机，不是整包依赖**：`index → filter → download → partition → chunk → embed → stage → upload`、中间结果缓存、`reprocess` 和 connector 端的稳定 ID/upsert 是很好的 P4 任务模型；但官方明确说开源 Ingest CLI/Python 不再积极追平 API 功能，因此不应把 Fufu 的长期主链绑在它上面。[5]
4. **RAGFlow 最值得复用的是治理 UX 与运维清单**：增量数据源同步、删除传播、metadata schema、chunk 启停/编辑/预览、检索测试、全卷备份恢复都成熟；但 Windows 只能现实地走 Docker Desktop/WSL2，官方最低 4 核/16 GB/50 GB，作为常驻依赖过重。[8]
5. **Open WebUI Knowledge 最值得借鉴的是工具/API 契约与 ACL**：`list/search/query/grep/view` 分工、文件级可读性检查、目录增量同步、知识库 zip 导出、外部向量库字段映射都适合 P3/P5；但当前许可证是带品牌保留和“滚动 30 天超过 50 个最终用户”限制的自定义 Open WebUI License，不宜复制 UI/后端代码。[22]
6. **AnythingLLM 适合借 watcher 和可搬迁存储布局，不适合当治理真相源**：Windows Desktop 可直接用，MIT；自动同步仍标为 beta，按文件观察、定时整文档重嵌入、无目录观察，官方还明确警告可能损坏本地 DB/向量库。[18]
7. **Khoj 与 Quivr 只作思想参考**：Khoj 的多客户端同步和隐私开关有参考价值，但 AGPL-3.0 不适合直接抽代码；Quivr 的 processor registry、YAML workflow 和内容哈希去重很小巧，但持久化/更新/删除仍不完整，不应进入生产主链。[25]

各条依据的官方来源如下。

- Docling 能力与 MIT、Windows 支持、分块器见其仓库与文档。[1][2][3]
- Marker 的代码/权重许可与模式见其仓库。[4]
- Unstructured 的许可与 ingest 语义见其仓库与官方文档。[5][6][7]
- RAGFlow 的架构门槛、同步、metadata、chunk 管理与备份见其文档。[8][10][11][12][13]
- Khoj 的自托管、隐私与数据源说明见其文档。[14][15][16][17]
- AnythingLLM 的许可、同步与存储见其仓库与文档。[18][19][20][21]
- Open WebUI 的工具契约与许可证见其文档与 LICENSE。[22][23][24]
- Quivr 的代码与序列化限制见其仓库。[25][26][27]

## 1. 功能与治理矩阵

符号：**强**=官方已有明确能力；**中**=有基础能力但边界明显；**弱**=只覆盖一部分；**无**=项目职责外或未发现官方实现。

| 项目 | 解析器 | 增量索引 | 去重 | 文档版本 | metadata | 安全 | 备份 | 可视化 |
|---|---|---|---|---|---|---|---|---|
| **Docling** | **强**：PDF/Office/图片/HTML/EPUB/邮件/音视频等统一模型；表格、公式、阅读顺序、OCR；无损 JSON 和 JSONL chunks。[2] | 无：转换库，由调用方管理 manifest/index | 无 | 无 | **强（结构/溯源）**：chunk 携带标题、caption 等；但 README 仍把题名/作者/参考文献/语言抽取列为 coming soon。[3] | 本地/air-gap；无多用户 ACL。[1] | 调用方负责；无状态转换结果易做内容寻址 | CLI/API；无治理 UI |
| **Marker** | **强于困难 PDF 场景**：PDF/图片/Office→MD/JSON/chunks/HTML；表格、公式、OCR；fast/balanced/LLM 模式。[4] | 无 | 无 | 无 | 中：结构化 JSON/chunk 输出可保留块信息；治理字段须自补 | 默认可本地；启用云 LLM 会出站 | 调用方负责 | 基础 Streamlit GUI，不是数据治理台 |
| **Unstructured** | **强且广**：元素化 partition、OCR/hi_res、按 title/page/basic chunk | **中-强（connector 层）**：源索引、过滤、中间缓存、恢复、`reprocess`；具体增量语义依 connector。[6][7] | 中：稳定 record/element ID、部分 destination 先删旧元素或 upsert，保证重跑幂等；不是统一近重复检测 | 弱：更新覆盖/幂等，不提供业务版本链 | **强**：元素类型、页码、坐标、语言、文件与 data source locator 等 | 凭证/ACL 多在 connector；本地 partition 可不调用 API | 中间工件可恢复；没有单一“全库备份” | 开源库/CLI 无完整治理 UI |
| **RAGFlow** | **强**：DeepDoc、多模板、复杂 PDF/表格/图片，解析结果可人工介入 | **强**：外部源首次全量，之后新增/修改刷新；可选同步删除。[10] | 弱-中：同步身份避免重复导入，但未见内容级/近重复治理契约 | **弱**：可重解析、覆盖/更新、禁用旧 chunk，但无 `supersedes` 历史链 | **强**：文档/块字段、自动 metadata、类型、批量维护、检索过滤；规则变化只影响新解析文档。[11] | 中：账户、API key、KB `Only me/Team`；粒度比 Fufu 的本机 token 模型复杂但不等于逐 chunk ACL | **强但重**：停服后打包 Docker volumes 再恢复，官方注明该页为社区贡献。[12] | **强**：文档/块预览、源位置联动、编辑/启停、日志、retrieval test。[8][13] |
| **Khoj** | 中：Word/PDF/Org/Markdown/TXT、GitHub、Notion | 中：Desktop/Obsidian/Emacs/Notion/GitHub 同步；官方文档的 “Incremental” 也可指 search-as-you-type，不应误当严格 CDC。[17] | 弱：未发现内容哈希/近重复治理的稳定公开契约 | 无：同步后的当前态，无历史链 | 弱-中：主要服务搜索过滤和来源 | 自托管可全本地；可匿名模式或认证；遥测可退出，若使用云模型，命中文本可能发送给模型商。[15][16] | PostgreSQL/嵌入式 DB 与文件需自行备份 | Web、Desktop、Obsidian、Emacs 多客户端 |
| **AnythingLLM** | 中：多文件/data connector，按 chunk 入向量库 | **中（beta）**：单文件 watcher；Desktop 本地文件每 10 分钟检查，后台源通常按 stale 周期；变更后替换该文档全部 embeddings。[19] | 弱：有 hashed vector-cache，但这不等同业务去重 | 无：同步是替换，不保留历史 | 中：workspace/document 基础 metadata；不够支撑 Fufu 生命周期 | Docker 版有单/多用户模式及 Admin/Manager/Default 角色；Desktop 不提供这套多用户控制。[21] | **易搬迁但无事务工具**：Windows 所有文档、LanceDB、SQLite、模型、插件在一个 storage 目录；宜停进程后整体快照。[20] | **强**：Windows 原生 Desktop，面向非技术用户 |
| **Open WebUI Knowledge** | **强（编排）**：Tika、Docling、Marker、MinerU、PaddleOCR 等可选 | **强**：目录镜像只上传新增/修改项并传播删除；API 上传异步可轮询状态。[22] | 中：目录同步使用文件 hash 做 diff；未见近重复语义合并 | **弱**：re-index 使用首次上传时已抽取的文本，不会重开原文；改 parser 后必须重新上传才真正重解析。[23] | **强**：文件/KB metadata、外部向量库字段映射（title/source/url/doc_id/page/extra metadata/score）[23] | **强**：用户/组/资源授权；默认查询前校验文件/KB read 权限；工具结果也按可读资源裁剪 | **强（应用层）**：KB zip 导出 + REST；部署仍需备份 DB、文件与向量存储 | **强**：目录树、文件管理、查询/grep/view、引用、管理台 |
| **Quivr Core** | 中：registry + simple/Tika/Megaparse(Unstructured)，易插自定义 processor | 无：`Brain.from_files` 偏一次性构建 | **中（文件级）**：`LocalStorage` 用 SHA-1 set 拒绝相同内容；不处理近重复。[27] | 无 | 中：`qfile_id/path/name/sha1/size` + 自定义字典 | 无认证/ACL；是 Python library | 弱：可保存 FAISS+config+本地文件；只支持部分序列化组合 | 无完整 UI（当前仓库定位为 core library） |

### “版本”必须单独补，不要用 re-index 冒充

这八个项目里没有一个直接等价于 Fufu 的 `ACTIVE → superseded/EXPIRED → 回收站 → 永久` 版本链。RAGFlow 的 enable/disable 很接近“软下线”，但没有不可变 revision 与 `supersedes_id`；AnythingLLM/Open WebUI 的重新嵌入主要是覆盖派生索引；Unstructured 的幂等 upsert 是运行语义，不是知识版本语义。因此 **Fufu 现有版本模型应保留为系统真相**，解析器和外部 UI 只能产生新的 immutable artifact/revision。

## 2. 许可证、Windows 与依赖重量

| 项目 | 许可证核验 | Windows 本地可用性 | 依赖重量 | 合规/部署结论 |
|---|---|---|---|---|
| **Docling** | MIT；README 提醒各模型另看其原始许可证。[1] | **原生可用**，官方列出 Windows x86_64/arm64、Python ≥3.10。[1] | 中-重；基础解析可 CPU，OCR/VLM/ASR extras 增重 | **可直接依赖/调用**；锁 parser、model、config 版本 |
| **Marker** | 代码 Apache-2.0；模型权重 modified AI Pubs OpenRAIL-M，研究/个人/融资或营收低于 500 万美元 startup 免费，超出需商业许可。[4] | **可用但分模式**：CPU fast + llama.cpp 可本地；NVIDIA balanced 推荐 Docker/vLLM | 重；PyTorch + Surya VLM，GPU/模型常驻成本高 | **仅独立 worker/CLI 回退**；记录权重许可证与模型 hash，不把权重打包进 Fufu |
| **Unstructured** | Apache-2.0。[5] | Python 可跑；完整格式常需 Poppler、Tesseract、LibreOffice、Pandoc/libmagic 等系统依赖 | 中到很重，取决于 extras/hi_res | **优先借状态机/element schema**；若接入，仅装所需格式 extras，并隔离进 worker |
| **RAGFlow** | Apache-2.0。[9] | **非原生**；Windows 走 Docker Desktop + WSL2。官方最低 4C/16GB/50GB，镜像约 2GB、解压约 7GB。[8] | 很重：应用 + DB + 对象存储 + 搜索引擎 + Redis | **不嵌入、不常驻**；最多按需运行或通过 API 输出 artifact |
| **Khoj** | AGPL-3.0。[14] | 官方提供 Windows pip 安装，也提供 WSL2 + Docker；本地模型会继续增加重量。[16] | 中-重（Django/DB/embedding/LLM 客户端） | **只借思想/协议，不复制代码**，避免 AGPL 传播义务进入 Fufu |
| **AnythingLLM** | MIT。[18] | **原生 Windows Desktop 最友好**；Docker 版才有完整多用户权限。[20][21] | 中；Desktop 自带 SQLite/LanceDB，模型另计 | 可参考或做旁路演示；不要让其 DB 成为主数据 |
| **Open WebUI** | **自定义 Open WebUI License，GitHub API 不能给标准 SPDX**；保留 BSD 风格条款并增加品牌限制，超过 50 最终用户时不得自行去标，除非获许可/企业许可。[24] | 通常 Docker Desktop/WSL2；可连接 Windows 上的 Ollama | 重（Web app + DB + 向量库/解析器可选） | **只复用 API/UX 思想；避免复制当前受限代码**。旧提交可能有旧许可证，逐文件复用必须再查 `LICENSE_HISTORY` |
| **Quivr Core** | Apache-2.0。[25] | Python ≥3.11；FAISS CPU 可用，但依赖 LangChain、Transformers、Megaparse 等较多 | 中-重 | 可抽 processor registry/YAML 思想；不建议直接拿其 persistence |

## 3. 最值得 Fufu P3–P5 复用的模块

### P3：Hermes 接入与知识工具

**优先级 A — 立即做**

1. **采用 Open WebUI 风格的窄工具分层，但由 Fufu 自己实现**：
   - `kb_list`：只列 collection/document 与计数；
   - `kb_search`：语义/hybrid 查 chunk；
   - `kb_grep`：精确字符串/正则，返回行号；
   - `kb_view`：按字符 offset 或 line range 分页；
   - 所有结果返回 `doc_id/revision_id/chunk_id/source/page/line_range/content_hash`。
   这比一个“万能 search”更利于 agent 正确编排，也与 Open WebUI 的 `query → grep → view` 路径一致。[22]
2. **权限检查放在检索之前，不靠 prompt**：保持 Fufu 当前 `127.0.0.1 + token`，同时在 repository 层强制 `collection/domain/status` 范围；未来多用户再引入 principal/ACL。Open WebUI 默认在向量查询前做资源 read 检查，这个顺序值得照搬，代码不复制。
3. **parser provenance 进入 revision**：新增 `parser_name/parser_version/parser_config_hash/model_id/model_hash/artifact_sha256`。Docling/Marker/Unstructured 的输出必须先变成 canonical artifact，再进现有 chunk/embedding，禁止 parser 直接写生产向量表。

**优先级 B — P3 后半**

4. **Docling adapter**：只做 `source → DoclingDocument JSON + Markdown + page/heading/table provenance`，复用现有 Fufu 分块为默认；另提供实验 flag 使用 Docling `HybridChunker`。Docling 的 chunker 会按结构起块，再按 embedding tokenizer 拆超长、合并同标题的小块，并可重复表头，适合复杂电厂文档。[3]
5. **Parser capability registry**：借 Quivr registry 思想，按 MIME/扩展名和能力选择 parser；但加上 Fufu 必需的 `supports_ocr/supports_tables/cost_tier/isolation_required/license_policy`，不要引入整个 Quivr/LangChain 依赖树。

### P4：增量、治理与备份

**优先级 A — 立即做**

1. **manifest 驱动的确定性增量同步**：每个 source item 存 `source_locator, size, mtime_ns, sha256, observed_at, parser_fingerprint, status`。顺序为：廉价 stat 筛选 → SHA-256 确认 → 未变跳过 → 内容变更创建新 revision → source 删除写 tombstone/待审，不直接物理删。RAGFlow 的新增/修改/删除同步语义和 Open WebUI 的目录镜像可作为验收标杆。[10][22]
2. **幂等 run ledger**：借 Unstructured 的缓存/恢复模型，建立 `ingest_runs` 与逐阶段状态：`DISCOVERED → HASHED → PARSED → NORMALIZED → CHUNKED → EMBEDDED → COMMITTED`；每阶段输出内容寻址 artifact，可断点续跑，`--reprocess` 显式越过缓存。[6][7]
3. **把“删除”作为一等事件**：源删除只将当前 revision 标记 `source_missing` 并进入待审；确认后才走 Fufu 现有回收站。不要直接照搬 RAGFlow/Open WebUI 的同步删除，因为档案馆目标是可恢复与审计。
4. **备份采用 SQLite 原生一致性快照 + manifest + restore drill**：
   - 在线 DB 用 SQLite Backup API 或 `VACUUM INTO`，不要复制活跃 WAL 三件套中的单个 `.db`；
   - 同批备份 uploads/artifacts、Hermes memory/skills/config（密钥单独加密）、parser manifest；
   - 生成 `backup.json`（schema_version、每文件 SHA-256、大小、创建时间、应用 commit）；
   - 每月自动恢复到临时目录并跑 `PRAGMA integrity_check` + 代表性检索。
   RAGFlow 的“停服务→打包所有持久卷→目标机恢复→再启动”说明了必须覆盖所有状态存储，而不是只备份向量索引。[12]

**优先级 B — P4 后半**

5. **metadata schema registry**：字段声明包含类型、作用域（doc/chunk/revision）、可过滤、必填、PII 分类、默认失效策略；变更规则只作用于新解析文档时必须触发“待重解析”计数。RAGFlow 已明确提示自动 metadata 规则变更不会追溯旧文档，这正是 Fufu 需要显式治理的漂移风险。[11]
6. **去重分三层，不混为一谈**：
   - exact binary：`source_sha256`；
   - canonical text：标准化后 `text_sha256`；
   - near duplicate：SimHash/MinHash 候选召回 + embedding 相似度复核，只生成 merge proposal，不自动融合。
   Quivr 的 SHA-1 set 适合作最小 exact-dedupe 参考，但 Fufu 应用 SHA-256、数据库唯一约束与 revision 语义替代进程内 set。[27]

### P5：质量与可视化

**优先级 A**

1. **做 RAGFlow 式 chunk inspector，而不是搬 RAGFlow**：文档页/原文与 chunk 联动；显示 parser/version/hash、标题层级、页码、metadata、embedding 状态；支持 disable/restore、人工改 metadata、标记错误，但正文修订必须创建新 revision，不能静默覆写。[13]
2. **做 parser bake-off 评测集**：Docling、Marker、Unstructured 与当前 parser 同跑真实样本，结果由指标决定，不根据项目 README 自报排名决定。
3. **引入 retrieval test 台**：固定查询集显示 FTS/dense/hybrid/rerank 各路排名、RRF 分数、过滤原因和最终引用；沿用现有 Recall@k/MRR/NDCG 基线，不换题作弊。

**优先级 B**

4. **Obsidian 仍是首选人类浏览层**；如果需要面向家人/非技术用户的聊天 UI，可旁路试 AnythingLLM Desktop。Open WebUI 可作为 API/工具契约参考，但因许可证和依赖重量不作为默认前端。
5. **Marker 仅在失败升级路径启用**：例如 Docling 的表格/公式/扫描页质量门禁失败时，把该文档送 Marker worker；不要所有 PDF 双跑。

## 4. 建议的 parser cascade

```text
文件发现
  → MIME + magic bytes + zip bomb/页数/大小限额
  → SHA-256 与 manifest 去重
  → born-digital 简单格式：现有轻量 parser
  → 复杂 PDF/Office：Docling adapter
  → 扫描/公式/表格质量门禁失败：隔离进程 Marker（按需）
  → 仍失败：进入人工队列，可选临时 RAGFlow 检查
  → canonical artifact
  → Fufu 现有 chunk → embed → revision commit
```

**质量门禁**应是确定性的：空文本比例、页覆盖率、异常字符率、标题树断裂、表格单元格数异常、OCR 置信度、重复页眉占比、解析超时/内存峰值。LLM 只做可选的困难文档判读，不进入默认提交路径。

## 5. 必须补的测试思想

### 解析 golden corpus

至少覆盖：中文 born-digital PDF、中文扫描 PDF、横跨多页表格、双栏+脚注、公式、DOCX 标题树、XLSX 多 sheet/合并单元格、PPTX、坏文件/加密 PDF/超大图片。每份保留人工确认的关键 assertions，而不是维护脆弱的全文快照。

指标：

- 文本：归一化 CER/WER、异常字符率、页覆盖率；
- 阅读顺序：块对 pairwise order accuracy；
- 表格：TEDS 或结构化 cell precision/recall；
- 结构：标题层级、列表、caption、公式、代码块 F1；
- 溯源：每 chunk 的 page/bbox/source locator 完整率；
- 工程：耗时、峰值 RAM/VRAM、冷启动、失败率、输出确定性。

### 数据治理不变量

1. 同文件同配置重复入库：document/revision/chunk 数不增长。
2. 文件改名但内容相同：只新增 source alias，不重复 embedding。
3. 内容改变：创建新 revision，旧 revision 状态可追溯；历史引用仍可解析。
4. parser/config/model fingerprint 改变：只使对应派生 artifact 失效，不改原始 source hash。
5. 源删除：产生 tombstone/待审；不直接物理删除。
6. 中途杀进程：重跑从最后完成阶段恢复，不产生半提交 chunk/vector。
7. embedding 服务不可用：事务不提交“有 chunk 无向量”的 ACTIVE revision，或明确进入 `INDEX_PENDING`。
8. ACL/collection 过滤必须在召回前后各验证一次，防止向量库 filter 漏洞泄露。
9. 备份恢复后：schema、文档数、revision 数、向量数、每文件 SHA-256 与固定检索结果一致。
10. 所有人工 chunk 编辑：必须产生日志、作者、时间和新 revision；不能原地无痕改写。

## 6. 不建议复用的部分

- **不把 RAGFlow/Open WebUI/AnythingLLM 的数据库当主数据**：会制造第二套生命周期、权限和备份真相。
- **不直接采用 Open WebUI 当前代码**：许可证不是标准 BSD/MIT/Apache；只实现公开 API/UX 思想。[24]
- **不复制 Khoj 代码**：AGPL 适合完整自托管产品，不适合悄悄嵌入 Fufu；如真要使用，保持独立进程并做正式合规评估。[14]
- **不采用 Quivr 的现有 load/save 作为可信备份**：当前 `Brain.load` 对 FAISS 使用 `allow_dangerous_deserialization=True`，且保存/加载仅支持有限的 vector store/embedder 组合；其 `LocalStorage.remove_file` 仍未实现。[26][27]
- **不把 Unstructured OSS ingest 当长期 connector 平台**：官方已提示开源 Ingest CLI/Python 不再积极获得 API 的最新能力。[6]
- **不使用 SHA-1 作为新系统内容地址**：Quivr 的实现足以阻止同进程重复上传，但 Fufu 应统一 SHA-256，并用数据库约束实现并发幂等。[27]

## 7. 最终集成排序

| 顺序 | 模块/思想 | 来源 | 复用粒度 | 阶段 | 预计改动 |
|---:|---|---|---|---|---|
| 1 | `query → grep → view` 窄工具与统一 provenance | Open WebUI | **接口/测试思想，自写** | P3 | 小-中 |
| 2 | manifest + tombstone + parser fingerprint | RAGFlow / Open WebUI / Unstructured | **算法与 schema，自写** | P4 | 中 |
| 3 | resumable staged ingest + content-addressed cache | Unstructured | **状态机，自写** | P4 | 中 |
| 4 | 一致性备份 manifest + 定期 restore drill | RAGFlow 运维清单 + SQLite 原生能力 | **脚本/验收，自写** | P4 | 中 |
| 5 | Docling canonical parser adapter | Docling | **Python 依赖 + 薄 adapter** | P3/P5 | 中 |
| 6 | chunk inspector + retrieval test | RAGFlow | **UX/验收思想，自写** | P5 | 中-大 |
| 7 | exact/canonical/near-duplicate 三层治理 | Quivr hash + 通用算法 | **小模块，自写** | P4/P5 | 中 |
| 8 | Marker 困难 PDF worker | Marker | **独立进程/CLI，不嵌权重** | P5 | 中 |
| 9 | 家人可用旁路 UI | AnythingLLM Desktop | **独立应用/API** | P5 可选 | 小 |
| 10 | processor registry/YAML workflow | Quivr | **设计思想** | P5 实验 | 小 |

## Sources

[1] https://github.com/docling-project/docling
[2] https://docling-project.github.io/docling/usage/supported_formats
[3] https://docling-project.github.io/docling/concepts/chunking
[4] https://github.com/datalab-to/marker
[5] https://github.com/Unstructured-IO/unstructured
[6] https://docs.unstructured.io/open-source/ingestion/overview
[7] https://docs.unstructured.io/open-source/ingest/ingest-configuration/processor-configuration
[8] https://ragflow.io/docs
[9] https://github.com/infiniflow/ragflow
[10] https://raw.githubusercontent.com/infiniflow/ragflow-docs/main/website/versioned_docs/version-v0.27.1/guides/data_source/add_to_knowledge_base_and_sync.md
[11] https://raw.githubusercontent.com/infiniflow/ragflow-docs/main/website/versioned_docs/version-v0.27.1/guides/dataset/metadata_management.md
[12] https://raw.githubusercontent.com/infiniflow/ragflow-docs/main/website/versioned_docs/version-v0.27.1/administrator/migration/backup_and_migration.md
[13] https://raw.githubusercontent.com/infiniflow/ragflow-docs/main/website/versioned_docs/version-v0.27.1/guides/dataset/chunk_parsing_results_and_knowledge_fragment_management.md
[14] https://github.com/khoj-ai/khoj
[15] https://raw.githubusercontent.com/khoj-ai/khoj/master/documentation/docs/get-started/privacy-security.md
[16] https://raw.githubusercontent.com/khoj-ai/khoj/master/documentation/docs/get-started/setup.mdx
[17] https://raw.githubusercontent.com/khoj-ai/khoj/master/documentation/docs/data-sources/share_your_data.md
[18] https://github.com/Mintplex-Labs/anything-llm
[19] https://docs.anythingllm.com/beta-preview/active-features/live-document-sync
[20] https://docs.anythingllm.com/installation-desktop/storage
[21] https://docs.anythingllm.com/features/security-and-access
[22] https://docs.openwebui.com/features/workspace/knowledge
[23] https://docs.openwebui.com/features/chat-conversations/rag
[24] https://raw.githubusercontent.com/open-webui/open-webui/main/LICENSE
[25] https://github.com/The-Vibe-Company/quivr
[26] https://raw.githubusercontent.com/The-Vibe-Company/quivr/main/core/quivr_core/brain/brain.py
[27] https://raw.githubusercontent.com/The-Vibe-Company/quivr/main/core/quivr_core/storage/local_storage.py
