# 🦊 P4 出口审查镜
## 2026-09-15 最终阶段门复核

> 本文是当前 P4 出口判断的最新权威增补。早期报告中的历史数字和“尚未进行真实 shadow”表述仍保留在原文中，但不得覆盖本文。

## 总体结论

**P4 总出口仍不通过，P5 不启动，生产常驻采集、worker、cron、自启和真实源清理继续关闭。** 当前代码具备较完整的安全、可恢复和降级底座，但 P4 完成定义要求 P4.1–P4.4 都有可复现证据，当前仍缺少真实语义 provider、人工候选样本、部署权限与跨文件系统故障证据。

## blocking issues

1. P4.4 没有实际语义提炼 provider。当前 Ollama 可访问且有 `qwen3-embedding:0.6b`，但 embedding 模型不能证明语义摘要、分类或提炼能力。`deterministic_refinement` 保持 `provider=None`、`semantic_extraction=false`，属于正确的 fail-closed 状态。
2. 没有 20 条真实人工候选评审证据。真实 Hermes shadow 的 3 条候选全部 partial/blocked；默认 refinement 创建 0 条，显式 declared-exclusion opt-in 后仍为 0 条，不能计入人工样本。
3. 生产认证主体和端到端授权尚未实现。trusted local operator/capability 只是本地互锁，不是认证。
4. 跨卷、掉电、文件系统错误和真实生产目录权限尚未演练；同卷 staged publish、异常回滚和进程 hard-kill 不能替代这些证据。

## high-risk issues

- 项目 venv 的 MCP 测试仍有 5 项 skipped；Hermes agent venv 专项及真实 stdio smoke 已通过，但项目自身 CI/安装矩阵尚未固定。
- 离线/SQLite 故障矩阵已覆盖临时 fixture 的公开入口，不等同生产权限、资源压力或长期运行验证。
- 真实 coverage 含 `cleanup_qualification_blocked=true`；附件、截断、未知格式和未声明缺口没有可恢复证据前，不得删除源会话。
- 工作树仍有未提交修改和未跟踪 P4 文件；本次没有提交、推送或重置。

## documentation inconsistencies

- 旧报告的 `249/267/308 collected` 等数字是历史基线；当前项目 venv 最新全量为 `325 collected / 320 passed / 0 failed / 0 errors / 5 skipped`。
- “未访问真实 state.db”“真实 shadow 尚未完成”已被后续真实只读 shadow 证据 supersede。
- “P4.3 离线故障矩阵未完成”已被最新临时 fixture 矩阵覆盖；真实部署级故障矩阵仍未完成。
- P4.4 仍未完成；当前只有无模型 deterministic pending-only 路径和 provider 阻断结论。

## missing acceptance evidence

1. 实际可用、明确授权的语义提炼 provider；
2. provider 超时、限额、非法结构和恢复补偿测试；
3. 至少 20 条真实、脱敏、可追溯候选；
4. 人工 approve/reject、重复、错误归因、supersedes、commit、检索和 audit 样本报告；
5. 项目 venv/CI 可重复安装 MCP SDK 的测试矩阵；
6. 真实跨卷 publish、权限拒绝、SQLite 锁/磁盘故障和 hard-kill 重启恢复；
7. 生产认证主体、授权策略和多实例管理动作的端到端证据；
8. 常驻调度的资源预算、停止/恢复和监控方案。保持未启用是安全选择，不是出口完成。

## 已确认的证据

### 测试环境

- 项目解释器：项目虚拟环境中的 `.venv/Scripts/python.exe`。
- 全量：`325 collected / 320 passed / 5 skipped / 0 failures / 0 errors`
- 5 个 skip 均为项目 venv 缺少可选 `mcp` SDK。
- `compileall -q src tests scripts`：通过；`git diff --check`：通过，仅有 LF/CRLF 提示。

### P4.2 真实 shadow

- 正式 `state.db` 只读复制到临时 shadow，未修改正式库。
- ended 首轮捕获 11、跳过 8、生成 11 条 source archive 和 3 个 candidate payload。
- active snapshot 捕获 2 条；不推进 ended checkpoint，不生成正式知识候选。
- 追加合成消息形成新版本；发布失败时 archive、candidate payload、checkpoint 保持一致；恢复重放成功；无变更重跑为 `captured_count=0`。

### P4.3 故障与降级

- 多进程 claim 互斥；worker 崩溃后 lease 回收，backoff 后可重新领取。
- handler 失败会重试，耗尽后 failed；敏感错误不落原始凭据。
- paused queue 不 claim；未知 handler 有限重试；手工重排队保留计数。
- 无 embedding 或 embedding 异常时 FTS5 保留并可检索，dense/hybrid 降级。
- 缺失 queue 的只读 health fail-closed，且不创建数据库。
- 以上为隔离临时数据库证据，不代表生产调度已启用。

### P4.4 refinement boundary

- 默认 fail-closed：真实 3 条候选均以 `coverage_not_complete` 拦截。
- 显式 declared-exclusion opt-in 仍创建 0 条；`cleanup_qualification_blocked` 和消息证据缺口继续阻断。
- 本次没有任何 approve/commit。

## recommended next order

1. 保持生产采集、常驻 worker、cron、自启和源清理关闭。
2. 若确实需要语义提炼，先设计独立 provider interface；不要读取或复制 Hermes 私密凭据。
3. provider 接入遵循 TDD，先测超时、非法结构、敏感信息、批量/费用限制和 pending-only 权限。
4. 只用 coverage 完整且可追溯的真实候选做人工评审，不用合成数据凑 20 条。
5. 补齐跨卷、权限和故障恢复证据后，再复审 P4 总出口。

## 阶段门决定

| 阶段 | 当前决定 |
|---|---|
| P4.1 | 部分可用，出口未完全放行 |
| P4.2 | 真实 shadow 与回滚已验证，源清理资格未取得 |
| P4.3 | queue/Runner/降级核心已验证，生产调度未启用 |
| P4.4 | pending-only 安全边界通过，provider 与人工样本未完成 |
| P5 | 不启动，依赖 P4 总出口通过 |

**最终判定：当前是可继续开发和受控验证的 P4 实现，不是可生产放行的 P4 完成版。**

## 2026-09-15 P4.4-1/2 实现后增补

本次已完成 provider 接口与错误边界的实现及独立复核：新增 `semantic_provider.py` 的显式注入契约，provider 输出要求结构化 `items`、合法来源证据和有界输出预算；超时、非法 JSON/结构、敏感信息、批量限制和 `max_attempts=1..5` 均有稳定错误码与回归测试。Provider 成功持久化 smoke 已确认只生成 `pending` candidate，FTS 查询不可见。

这只是**实现层 + 合成验证层**的推进，不改变本文原有出口阻塞项：尚无实际语义 provider、真实人工候选样本、生产认证主体、跨卷/权限/文件系统故障证据，P4 总出口仍不通过，P5 仍不启动。

## 2026-09-15 路线边界修订：P4/P5 优先单用户使用，平台生态后移 P6

项目范围约定：DSH、AstrBot、通用 Agent Adapter、插件/Provider/Backend SDK、插件分发和多租户等平台化能力不提前纳入 P4/P5，统一作为未来 P6 方向。

- **P4**：继续完成可靠核心、Hermes 增量归档、queue/Runner、语义 Provider 和候选审核出口。
- **P5**：目标调整为 单用户可直接使用的个人版：本地 CLI/HTTP/MCP、Hermes 单一接入、记忆检索/审核/备份恢复，以及少量 skill/memory 按需取回；UI 仅在实际使用需要时做轻量版本。
- **P6**：再规划 Hermes 完整插件、DeepSeek Harness MCP/Adapter、AstrBot Star、通用 Agent Adapter、插件清单/能力声明、Provider/Backend Registry、插件分发和多用户/多租户。

不得为了 P6 的通用兼容性提前引入第三方 Agent 依赖、复制其内部插件内核或扩展当前 P4/P5 的验收范围。P4 未通过前不启动 P5；生产常驻采集、cron、自启和源清理边界保持不变。

## 2026-09-17 P4.4-3A 进度补充

已新增 `src/fufu_archive/archive_codex_provider.py` 与 `tests/test_archive_codex_provider.py`，实现档案馆独立 Codex Responses Provider 的安全骨架：显式 archive-owned credential store、原子保存、独立 refresh lock、可注入 transport、Codex `/responses` 请求和错误脱敏。当前不读取 Hermes `auth.json`，不实现浏览器 OAuth 登录，不发送真实会话内容。

本轮项目 venv 定向测试为 22 passed（含既有 refinement），全量回归为 325 collected / 320 passed / 5 skipped；compileall 通过，`git diff --check` 通过。真实 OAuth grant、实际订阅请求、独立额度归属和生产启用仍未验证，不能宣称 Provider 已接通生产。
