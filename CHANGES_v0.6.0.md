# v0.6.0：活动经历写入与会话召回

本版把已提交活动反馈接进已有记忆系统。保留自然语言总结和结构化事实，不增加独立模型调用，不引入向量库。此前 v0.5.3 全量测试已由用户确认通过；本版仅静态检查，下面的本地测试由用户运行，暂不要求 QQ 联调。

## 写入与结算边界

- `ACTIVITY.stage_completed`：记录一次阶段经历，保留 checkpoint、task 与 activity ID；阶段成功不会把活动标成 completed，也不发固定完成收益。
- `ACTIVITY.completed` / `ACTIVITY.failed`：有 completion 反馈时保存原摘要；没有内容摘要时仅保存活动完成/失败这个最小事实。不会把名称、任务成功、错误栈或进度推断成实际观察、开心或有趣。
- 纯进度、开始、暂停、恢复和取消不产生记忆或记忆回执。停止恰好达到完成条件时，沿用执行器原有 completed 语义。
- 摘要最多保存原协议允许的 2000 字符；展示继续截短。模拟/推断来源使用 story，观察/确认使用 episodic，原 provenance 不提升。
- activity ID 对应一项活动，checkpoint 区分各阶段，phase 明确 stage/completed/failed，task 独立保存。每个源事件创建一条经历，不用自然语言近似归并不同 checkpoint；阶段与终态保留为同活动的不同记录。
- about_id 是 `agent:角色ID`，speaker_id 为空；活动由角色经历，不被记成当前 QQ 用户的计划。
- 事件、活动状态、完成收益、评价、反馈指针与记忆共用事务。写入失败全部回滚；重试使用原稳定事件 ID 和 activity:experience 回执，删除后旧事件也不能复活正文。删除只清理记忆记录，原活动事件及短期反馈仍按既有机制保留，反馈未过期时仍可能作为短期状态参考。记忆本身不增加角色版本或重新结算心理/物理需求。
- 原有评价器可独立关闭，仍能记忆活动；已有规则或语义结果仍按原流程只结算一次。原反馈自带且不 uncertain 的结构化 importance 可用于本次记忆，否则默认 0.5，无摘要失败默认 0.6。延后评价在原事件到达时就记忆，后来的语义结果不更新记忆重要度、时间或再造一条经历，也不重发效果。

## 作用域与升级

有 scope_id 的反馈仅当前角色、当前原会话可召回；群聊仍受 private_only 限制。无 scope_id 的反馈、普通自主活动终态进入内部 self：JSON scope_id 为 null，SQL 兼容现有 NOT NULL 列使用保留空串；公开 get/list/pending/history/delete/recall 需要非空会话范围并过滤 visibility=scope，不能把空串当成公开会话。只有应用专用活动入口可以写 self，通用草稿不能伪造活动经历或扩大可见范围。

内部 self 仅提供本地维护查询 `PersonaStore.list_internal_memories()`，没有 QQ 路由或全局召回。**默认自主模拟活动的结束事件没有 scope_id，因此它的最小事实不会出现在 QQ 聊天里。** 本版不把最近 QQ 会话猜作活动归属。未来活动执行器需在事实产生时提供可信的来源范围和内容摘要；本地测试显式提供 scope，验证从反馈到召回的闭环。

只处理本版接收的新事件，不扫描历史事件补记忆。提交前按事件 ID 判断是否已记录，重放旧提交或关闭开关期间的反馈不会在开启后补写。启动恢复旧耗尽 tick 的既有修复路径不回填经历；正常运行中确认 tick 耗尽产生的新失败可记录为内部最小事实。

数据库保持 **schema v6、十二张表、词项版本 2**；不执行新迁移或全量重建。ActivityMemoryContext 只增加 JSON 字段。普通旧记忆 activity=None 时不把新字段写入 JSON，也不改变旧草稿回执摘要。仍应保留更新前备份：SQL 版本相同不代表旧版严格 JSON 模型能理解新经历字段，回退代码需配套恢复旧数据。

## 召回与上下文

“你刚才做了什么 / 刚刚在做什么 / 你的最近经历”走当前会话的角色经历候选；用户自己的计划继续按人过滤。普通内容查询沿用词项检索，有界候选、重要度、归一化相关度和时间衰减保持原规则。没有声称任意问法都能语义匹配。

模型看到 text、provenance、来源时间，以及 activity.id/name/phase/task/checkpoint。同活动 ID 的 stage 不表示整项完成。短期反馈兜底也保留活动 ID 与 phase。同一原事件已作为记忆注入时省略重复的 feedback；预算裁掉记忆后再尝试短期反馈兜底。引用列表只反映实际保留下来的内容。总上下文预算默认 1000，记忆预算默认 600；其他会话和 self 不参与组装。

## 按文件笔记

| 文件 | 新增或修改 |
| --- | --- |
| `application/activity_memory.py`（新增） | 已提交活动记录/事件适配到经历草稿；保留阶段、任务、来源与原摘要；无摘要终态生成最小事实 |
| `application/persistence.py` | 原子接入活动记忆；提交前识别新事件，阶段上报和运行中耗尽失败接线；本地内部记忆查询 |
| `memory/models.py` | ActivityMemoryContext；activity_memories_enabled；可选活动元数据及 self/阶段校验；文本上限与反馈协议一致为 2000 |
| `memory/persistence.py` | 专用经历入口、稳定回执、独立 checkpoint；内部 self 存取；公开范围校验/visibility 过滤；兼容旧 JSON 和旧草稿摘要 |
| `memory/retrieval.py` | 有限的助手近期经历查询意图，避免替代用户计划和用户自己往事 |
| `application/chat_context.py` | 把活动经历映射到独立提示 DTO，短期反馈保留来源活动及阶段 |
| `cognition/prompt_builder.py` | 经历 DTO 与 phase 约束；活动元数据注入；同源反馈去重、预算裁剪及反馈兜底；保持预算 |
| `application/memory.py` | list/show/history 展示阶段、整项完成/失败、来源和 checkpoint |
| `_conf_schema.json` | 增加活动记忆开关和作用域提示，老配置缺字段时使用模型默认值 |
| `tests/test_activity_memory.py`（新增） | 本地活动→记忆→召回、来源/权限、幂等、回滚、评价互斥、旧 JSON/回执和预算回归；未运行 |
| `tests/test_chat_context.py` | 同源内容可以通过记忆引用保留，仍要求只引用一次；未运行 |
| `metadata.yaml`、`README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md`、`PACKAGING_FILES_v0.6.0.txt` | v0.6.0 版本、结构、用户验证与全量文件清单 |

静态检查已通过 Python 3.10 语法/编译、相对导入符号、依赖方向、配置默认值、文件清单和 diff 空白检查；没有运行业务模块或测试。最小 500 字符预算另检查了较长角色 ID、情绪及运行活动的组合。

跨业务适配放在 application，memory 不导入 activity，cognition 不导入 memory。没有新增空占位目录、第三方依赖或后台模型任务。

## 本地验收

```powershell
python -m unittest discover -s tests -p "test_activity_memory.py" -v
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_chat_context.py" -v
python -m unittest discover -s tests -v
```

新增用例使用固定时间和临时数据库，不使用实际插件数据库、QQ 或模型。 tick 耗尽用例沿用 build_activity_tick 返回的完整规范计划，按 max_attempts 与每次返回的 next_attempt_at 逐次确认失败；重试未耗尽时保持 running 且无失败记忆，耗尽后先验证活动 failed 和恰好一条记忆，再检查内容与重启幂等。重点看运行中的阶段记忆仍为 stage、完整结束收益只一次、重启不重复、两小时后语义结果不影响原状态、非原会话和 self 不泄漏、写入失败可完整重试。

版本 **v0.6.0**。全量打包以 [PACKAGING_FILES_v0.6.0.txt](PACKAGING_FILES_v0.6.0.txt) 为准，保留相对路径，入口位于包根目录；tests、缓存、真实数据库和旧打包清单不进入本版安装包。实现者不运行测试、不操作真实数据库、不生成 ZIP。
