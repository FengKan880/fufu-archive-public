# 🦊 档案馆验收镜
## P4 跨模块验证与独立复现报告

结论：**P4 总出口不通过。现有自动测试通过不代表完整性、安全与运行出口已通过。**

本轮采用独立复核：只读审查发现由验收方读取脚本并重新执行。未改业务源码、未安装依赖、未访问真实 Hermes state.db/正式知识库、未启动生产服务或调度、未提交 Git。仅使用合成 SQLite 数据。本报告不声称穷尽全部缺陷。

## 1. 实际测试证据

- 项目 Python：`.venv/Scripts/python.exe`，实测 Python 3.11.9。
- `env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV .venv/Scripts/python.exe -m pytest -o addopts='' -q -ra --junitxml=<validation-workspace>/tmp/p4-validation-baseline.xml`：**249 passed, 5 skipped, 13 warnings in 7.46s**，exit 0。
- 从 XML 逐 test case 统计：collector 42；queue 22；runner 27；P4.2/P4.3 组合为 **91**，不是之前回复中的 80。backup 5，doctor 3。
- `compileall -q src tests`：exit 0。
- `git diff --check`：exit 0，有既存 LF/CRLF 提示。大量 P4 文件尚未被 Git 跟踪，因此此命令不能覆盖所有新文件的差异审查。
- 项目 venv 的 5 个 skip 全部因缺 `mcp`。在已有 Hermes venv 中显式设置 `PYTHONPATH=./src`，执行 `tests/test_mcp_adapter.py -o addopts='' -q -ra`：**7 passed in 0.74s**。该测试调用 adapter 函数，不是完整 stdio 协议 smoke，不能据此说项目独立 MCP 环境/CI 已解决。
- warnings 来自 FastAPI/Starlette 兼容与 HTTP 状态常量弃用。本轮未安装或升级包。

## 2. 主会话独立复现的阻塞/缺口

| ID | 优先级 | 位置 | 实测与影响 |
|---|---|---|---|
| V01 | 高 | task_runner.py:95–109 | 标准 `Authorization: Bearer <合成值>` 的 Bearer 被第一正则分支替换，实际值仍进入 last_error。此前“Bearer 已修复”的结论不成立，旧测试值本身含 bearer 字样，掩盖了漏洞。 |
| V02 | 高 | session_collector.py:804–842 | 修改合法 archive 的消息正文、保留声明 hash 后，load_source_archive 仍接受被修改的正文。结构校验不能替代正文与 fingerprint 对应校验。 |
| V03 | 高 | task_runner.py:205–245 | `--pause --inspect` 返回 0 并将持久化 paused 改为 true，没有拒绝冲突管理动作。 |
| V04 | 高 | task_queue.py:723–742 | 只保留三个表名、但缺 required 列的库被 health 报 ok:true。表名和 integrity_check 不能证明 queue 可用。 |
| V05 | 高 | backup.py:310–354 | verify_restore(source, unrelated) 对另一份内容不同的健康库返回 ok:true；当前验证证明可读性而非与指定源的恢复一致性。不得作为源清理前的恢复证明。 |
| V06 | 中 | session_collector.py:1762–1766 | ID 顺序时间为 200、100，now=250、quiet_seconds=150 时错误采用 100 并 captured；应按最新有效时间 200 判定延后。 |
| V07 | 中 | session_collector.py:1774–1775,1835–1867 | active snapshot 排除未知 role，无逐项 coverage 缺口计数，仍 captured。已有 content_recovery=user_assistant_messages_only，不能说代码承诺了全角色无损，但其覆盖说明不足；活跃会话本来就不得清理。 |
| V08 | 中 | session_collector.py:1755 | 合法列名 schema 中 message id='bad' 导致 ValueError 整批失败；安全失败不等于误归档，但单坏行阻塞整批且缺少明确隔离诊断。 |
| V09 | 中 | task_runner.py:138–154 | handler 超租约后 acknowledge 抛 TaskLeaseError；Runner 又调用同样无权的 retry，再次抛错而无运行报告。过期 running 由后续 claim/reclaim 恢复是合理队列行为，不应绕过 token 强制 ack；问题是 Runner 对租约丢失缺少独立控制流与诊断。 |
| V10 | 中 | doctor.py:280–335 | 删除 document 审计链后 doctor 仍 ok:true；只检测完全缺失的 candidate 链，document 完全缺链不报警。旧库无历史文档审计可能需要兼容策略，应标记未覆盖，不应默认为全链已验证。 |

## 3. 可复现产物

位于 `<profile-root>/workspace/tmp/`：

- `p4-validation-baseline.xml`：全量测试机器可读记录。
- `p4-validation-extra.py`：正常 restore-check、无关恢复库误接受、缺失 document audit 检查。
- `p42_collector_repro.py`：未知 role、非法 ID、时间倒序、正文被修改。
- `p43_repro.py`：CLI 冲突、health 缺列、租约超时、合成凭据泄漏。

主会话分别执行后三个脚本，均最终 exit 0，观察到了表内问题。运行方式：

```bash
env -u PYTHONHOME -u VIRTUAL_ENV PYTHONPATH='./src' \
  .venv/Scripts/python.exe '<validation-workspace>/tmp/<script>.py'
```

extra 脚本首次打印结果后因 Windows SQLite 连接垃圾回收前临时目录删除失败而 exit 1；加入 gc.collect 后重跑 exit 0。未把第一次清理失败宣称为成功。临时复现数据不属于正式归档。

## 4. 路线出口判断

| 模块 | 已有证据 | 不通过/未覆盖 |
|---|---|---|
| P4.1 | backup/doctor/候选测试、已有环境 MCP adapter | 恢复一致性与审计缺口；项目自身 MCP SDK/CI；真实人工样本；向量模型/维度持久化与补建出口未完成 |
| P4.2 | ended/active 独立调用、schema、幂等、profile、staging/异常回滚测试 | 正文 hash 对应校验；active 覆盖和静默窗口；ended 一致快照；附件/截断/消息恢复；真实 schema 与跨物理盘、kill/掉电演练未验收 |
| P4.3 | SQLite queue、lease token、重启/多进程测试、bounded Runner、health | 脱敏、CLI 冲突、health 缺列、租约丢失；管理权限/API、独立配置调度和完整离线降级未完成 |
| P4.4 | 无完整实现证据 | provider、提炼证据映射、至少20个人工样本、断模补偿闭环未完成 |

生产调度未启用不是本轮应擅自消除的阻塞；但缺少独立调度实现/验收必须与“主动不部署”区分。无模型测试不等于断开真实模型后的完整运行演练。P4 完成定义要求 P4.1–4.4，不可凭 249 passed 放行 P5 或源清理。

## 5. 上文纠偏与后续顺序

- 先前“10项/50步大推进完成”没有逐项可验证的大任务验收台账，不能视为50项工程能力已交付；实际主要增加 health 与零散测试。
- “80 passed”与实际组合数量不符；以本轮 XML 91 个组合测试为准。
- Bearer 修复和 health 完整结构校验此前被夸大，已被反例推翻。
- 只做验证，不悄悄修源码改变验收基线。建议下轮先修 V01/V02/V03/V04/V05，补独立失败用例；再处理 active 与租约等边界，最后重跑模块与路线出口。

**总评：当前是可继续开发的部分实现，不是可放行的 P4 完成版。**

## 6. 验收后修复与复测（同日）

以下 V01–V09 已在隔离测试下修复；本节不改变第 1 节的原始基线证据。

| ID | 修复 | 新增回归 |
|---|---|---|
| V01 | `Authorization: Bearer <value>` 作为同一敏感字段脱敏，避免先替换 `Bearer` 而泄漏真实值。 | 标准 Authorization/Bearer 值不进入 `last_error`。 |
| V02 | source archive 读取时验证 fingerprint 集合摘要，且将可恢复 user/assistant 正文与其 fingerprint hash 逐项绑定。 | 篡改正文 fail closed；`content_present=false` 的 partial coverage 保持兼容。 |
| V03 | CLI 只允许一个管理动作，inspect/summary/health 与 pause/resume 等不能混用。 | `--inspect` 与控制动作组合被 parser 拒绝。 |
| V04 | readonly health 检查必需表的必需列，不再只看表名。 | 表名齐全但列缺失返回 `ok:false` 和 `missing_columns`。 |
| V05 | restore verify 比较 P4 核心表的 canonical rows，不把虚拟 FTS 表的物理布局当作源一致性。 | 内容不同的健康库不能通过 source/destination match。 |
| V06 | active quiet-window 取最大有效消息时间，而非最后一个 ID 行的时间。 | 倒序时间戳不会提前捕获。 |
| V07 | active snapshot 生成 coverage manifest；unknown role、非法 ID、坏字段/内容会标记 partial 并阻断 cleanup qualification。 | unknown/invalid synthetic rows 不再静默表现为 complete。 |
| V08 | active snapshot 对非法 message ID 计数并跳过该行，不再因 `int()` 使整批崩溃。 | TEXT message ID 产生 partial archive。 |
| V09 | Runner 将 handler/retry 或 acknowledge 的 lease loss 作为单独控制流：显式 reclaim expired lease，不再以第二个 `TaskLeaseError` 终止 bounded run。 | 人为推进 clock 越过 lease 后任务回到 queued，租约字段清空。 |

### V10 更正

V10 不作为 bug 修复：源码核对表明直接 `KnowledgeBase.ingest()` 合法地不创建 `document.created` 审计事件；强制每个 document 都有 audit chain 会让正常库被 doctor 误报。因此 doctor 保持“对存在的 audit chain 做连续性/实体一致性校验、候选必须有 chain”的既有契约。该项应从阻塞清单移除，而不是通过破坏兼容性来“修复”。

仍不满足 P4 总出口的路线级缺口包括附件/截断恢复、ended collector 的跨会话一致读快照、真实 Hermes schema 与跨卷/掉电演练、调度/权限/离线降级、P4.4 提炼和人工样本；这些不是本轮已确认代码缺陷可以凭局部 patch 消除。


## 2026-09-12 独立复验增补（优先于本文早期数字）

本文早期的 `267 passed / 5 skipped`、Hermes venv `7 passed` 以及“仅合成
SQLite、未访问真实 state.db”的表述属于历史记录，不能作为当前出口证据。

本次独立复验使用两个明确解释器：

- 项目 venv：全量 `300 collected / 0 failures / 0 errors / 5 skipped`；5 个
  skipped 全部是项目 venv 未安装可选 `mcp` SDK。
- Hermes agent venv（实际 MCP server 的解释器）：`tests/test_mcp_adapter.py`
  为 `10 passed / 0 skipped`；真实 JSON-RPC stdio smoke 握手成功，协议
  `2024-11-05`，列出 10 个工具，stderr 为空。

已补充的真实/合成验收包括：

- `os._exit(137)` 进程级 hard-kill：source archive 已发布而 checkpoint 未发布
  时，下一次 incremental run 先恢复 durable publish manifest，再重新规划；
  最终 archive 无重复、checkpoint 存在、manifest/backup 清除。
- 已执行受控来源库的只读 schema/count shadow；不公开具体来源库的会话、消息与角色分布计数，未写回原库、未输出正文。
- 受控样本存在 partial coverage；默认及 declared-exclusion opt-in 均未绕过未声明缺口。角色与 tool-call 结构交叉核对说明，空 content 但存在 tool_calls 的 assistant 消息需单独分类。
  因此新增 `tool_call_only_turns` 分类；它仍使 coverage 为 `partial`，默认
  不放行，仅显式 opt-in 时作为 declared exclusion。tool 行只计
  `tool_bodies_excluded`，不再双重计入 malformed。
- `session_meta` 等 unknown role、结构化 `\x00json:` content、无正向
  `tool_calls` 的空正文仍是未声明缺口，即使 opt-in 也 fail-closed。
- coverage manifest 仍为 v1；为不让新增 `tool_call_only_turns` 破坏已落盘
  v1 source archive，加载器仅对**缺失该新增计数**的历史 manifest 注入 `0`。
  其余既有计数与状态字段继续必填且 fail-closed；该行为有独立 RED→GREEN
  回归。
- CLI 的 `--inspect`、`--summary` 与 `--control-status` 现在要求既有
  queue DB；此前三种名义只读动作会因构造 `TaskQueue` 而创建拼错路径下的
  SQLite/WAL 文件。新增 3 条 RED→GREEN 回归；已有 DB 的深度零写迁移语义
  仍不作为本条结论。
- Windows `spawn` 双进程 `run_once()` 验收：两个独立 worker 对一条临时
  queue task 并发运行，合计仅 `claimed=1`、`succeeded=1`，另一 worker idle，
  最终任务为 succeeded；子进程先 join/核验 exit code，再有界读取报告，避免
  child 崩溃时测试自身无限等待。

当前全量更新为 `308 collected / 303 passed / 0 failures / 0 errors / 5 skipped`
（项目 venv）；5 个 skip 均为该 venv 缺少可选 `mcp` SDK。编译
`compileall src/fufu_archive tests scripts` 与 `git diff --check` 均 exit 0。
这些证据仍不能替代真实跨卷故障、生产权限、认证主体、真实 provider 与
20 条人工样本，因此 **P4 总出口仍不通过，P5 不得启动**。
