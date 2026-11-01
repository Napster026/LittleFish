# v0.6.1：允许角色表达自身模拟活动经历

角色自主休息、进食、玩产生的完成/失败事实，可以进入已有 QQ 聊天请求。没有 scope_id 不影响这类角色自身事实的表达，模型能区分角色自身和用户的经历。仍不增加模型请求、自动发送消息或重新执行活动。

## 召回与表达

- 保留原 visibility=self、scope_id=None、关于当前角色的身份和原始 simulated 来源。不伪造 QQ 会话、不复制数据，也不改成 global。
- “你刚才做了什么”使用当前会话经历与角色自身事实；普通活动关键词用词项查询两路有界候选；没有查询词时也可挑选近期自身事实。合并后使用原相关度、重要度、时间衰减及 recall_limit。
- 提示数据包含 `owner=agent`、`about=agent:角色ID`、`provenance=simulated`、活动名称/ID 与阶段。固定约束说明它属于角色自身模拟经历，不是用户行为或现实观察。角色可据此说“我刚才休息了一会儿”，不能推测并无反馈支持的梦、游戏细节或食物。
- 当前默认模拟执行器只产生最小事实，因此本版共享结构匹配的“活动完成/失败”记录：当前角色、self、无范围、无说话人、event/story、simulated、终态、无 checkpoint、task 与终态匹配、正文等于宿主最小事实模板。无归属的阶段/终态自由文本反馈继续内部保留，具体会话的反馈继续只在原会话使用。
- memory 与最终提示组装分别检查边界；不能仅凭 scope_id=None 或 visibility=self 就公开未知内容。其他角色、其他会话、observed/inferred 内容不会从这一路混入。
- 记忆及聊天总开关、绑定和私聊/群聊限制沿用宿主现有门禁；activity_memories_enabled=false 同时停止新增活动记忆和这一路自身事实召回。auto_capture 仍只控制 QQ 文本提取。
- 上下文默认总计 1000 字符、记忆部分 600 字符，不扩大预算。最多附加能容纳的若干条，并非完整活动历史。

## 存储与管理

已有 v0.6.0 自身最小事实无需迁移即可召回。没有增加或修改原始事件、记忆 JSON、来源、回执、角色状态；重启后读相同记录，不额外结算需求和情绪。数据库仍为 schema v6、十二张表、词项版本 2，无新依赖。

公开管理的 list/show/history/pending/delete 仍管理当前会话记录，不能用空 scope 操作内部存储。search 与聊天共用召回，因此可以预览允许共享的自身最小事实，并显示“角色自身模拟经历”；其 ID 的内部维护仍使用本地入口。没有把内部摘要的管理权限扩展到任意 QQ 会话。

## 按文件修改

| 文件 | 内容 |
| --- | --- |
| `memory/retrieval.py` | shareable_self_activity：纯规则辨认角色自身的最小模拟活动事实 |
| `memory/persistence.py` | 当前会话与允许共享的自身事实合并召回；两路关键词索引有界、批量加载、统一排序；保持原公开管理边界 |
| `cognition/prompt_builder.py` | ContextMemory 保留 nullable 原范围与 visibility；独立复核 self；owner=agent 和表达约束；沿用字符预算 |
| `application/chat_context.py` | 原样传递 self 的来源范围和可见性，不绑定到当前用户 |
| `application/memory.py` | search 结果标注角色自身模拟经历 |
| `_conf_schema.json` | 更新活动记忆开关和自身事实召回说明，默认值不变 |
| `tests/test_activity_memory.py` | 补跨会话、休息/进食/玩、关键词/近期、未知摘要隔离、其他角色/开关、重启无副作用、有界检索及提示边界用例；未运行 |
| `metadata.yaml`、`README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md` | 更新当前版本与使用边界 |
| `CHANGES_v0.6.1.md`、`PACKAGING_FILES_v0.6.1.txt` | 本版笔记和全量打包清单 |

v0.6.0 新增 tick 耗尽用例中的非规范 max_attempts=1 已修正：沿用完整原计划和返回的 next_attempt_at 逐次重试，耗尽前断言 running/无失败记忆，耗尽后断言 failed/恰好一条，再检查摘要与重启幂等。本次保持该修复。

## 用户本地验证

```powershell
python -m unittest discover -s tests -p "test_activity_memory.py" -v
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_chat_context.py" -v
python -m unittest discover -s tests -v
```

实现者只做静态检查，不运行测试、实际数据库操作或 QQ 联调，不生成 ZIP。当前版本 **v0.6.1**，自行全量打包使用 [PACKAGING_FILES_v0.6.1.txt](PACKAGING_FILES_v0.6.1.txt)，保留相对目录；tests、缓存、数据库、旧打包清单不放入安装包。
