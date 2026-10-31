# v0.3.1：评价需求、幅度、查询与时效修订

此前 v0.3.0 全量测试已由用户确认通过。本次针对五处设计缺口修订，测试由用户运行；实现者只做静态检查，不生成安装包。AstrBot 目标仍为 v4.29.3。

## 1. 评价结果接入心理需求

RuleEvaluation 新增 needs_delta 和 needs_reason。文字语义模型仍只能提供体验维度，不能直接输出需求变化；代码根据 evaluation.needs 配置计算心理需求。默认开启，max_delta=2（每轴单次上限），cooldown_seconds=30。

- 兴趣可降低无聊，进展也可小幅降低无聊；六个体验维度均有可配置的心理需求权重，缺乏依据的维度默认零影响。
- 活动中的社交需求影响要求 AppraisalContext.social_interaction=true，由执行器确认；不会仅因独自游戏愉快就自动满足社交需求。
- 已明确指向角色且命中聊天规则时，正向互动默认社交需求 -0.5，负向互动 +0.5；含糊或非指向消息仍不改变需求。
- 精力、饱食不能由评价发放，只由执行事实、固定效果和时间演化管理。
- 活动定义中已有 completion_needs_delta 占用的心理需求轴，在该实例的阶段与整项评价中都不额外发放。要改为体验驱动，应在新活动定义中移除对应固定完成效果；已保存实例仍沿用原定义快照。
- 同活动或同会话发送者共享心理收益冷却，避免换 checkpoint 或换措辞立即重复获益。冷却跨重载保存。最终需求仍限制在 0～100。

评价的情绪和心理需求合成一条提案，只增加一次状态版本；整项固定完成收益仍是独立结算。需求、情绪、冷却、下一条情绪计划和最终评价回执在同一事务中提交。仅有需求变化时也可结算，不要求一定有 VAD 冲击。普通情绪接口仍拒绝随意混入需求，只有评价提交入口可附带经过约束的心理需求。

needs_reason 可显示 experience、completion_owned、need_cooldown、at_boundary、disabled 等原因；/persona_evaluation 会显示实际需求变化。

## 2. 保留先验的柔性修正

语义路径与规则路径统一使用 activity_effects（例如 stage_failure）的任务先验。AppraisalWeights 中不再维护独立 task_* 先验；旧 task_success/task_partial/task_failure 配置兼容读取但忽略，以 activity_effects 为准，旧存档不重写。

每个坐标先计算体验加权修正 c。令先验为 p、单轴上限为 L、方向上的剩余空间为 r，则结果为 p + r*c/(s+abs(c))，s 为 semantic.adjustment_scale（默认 0.1）。c=0 时保留 p；有限修正逐步使用剩余空间，不再将不同强度直接截成同一个 ±L。先验本身仍受全局限幅。

因此，无新增体验证据时开关语义不会使同样的任务失败自动变淡；有明确兴趣、进展或愉快证据时仍可抵消挫折甚至转为正愉悦度，任务事实不改变。新配置比旧配置更严格时，等待任务按新上限重新计算，而非先按旧上限计算后截平。

## 3. 待评价查询不扫描历史事件

pending_appraisals() 改从 scheduled_events 按 agent_id、status=pending、schedule_id 分页查当前未投递计划，再对候选评价任务按 ID 读取预约与最终回执。已完成任务跳过。读取量与当前未投递计划有关，不再随全部历史 agent_events 线性增长。

数据库打开时补建 scheduled_pending_idx(agent_id,status,schedule_id) 辅助索引。表结构和 JSON 版本仍为 schema v3 六张表；无新依赖、无历史数据改写。早已完成但截止时间尚未到的评价计划仍会暂时留在待投递计划中，最终回执使其不再作为待评价任务返回。

管理员“最近评价”等历史查询仍是按需分页日志查询，本次没有宣称消除所有历史扫描。

## 4. 重启与迟到结果共享效果时效

新增 evaluation.max_effect_age_seconds，默认 300 秒，按原反馈事件 timestamp 计算，不按模型返回或重启时刻重新计时。超过时效后，不论是模型成功、超时兜底、失败计划恢复还是即时规则，都只生成 layer=expired 的最终回执，不改变需求、情绪、游标或状态版本。分析可继续保存。

因此，两小时前未成功提交的超时反馈不会在重启后突然产生情绪冲击。仍在时效内的近期反馈可正常一次性兜底；模型截止时间与效果时效是两个独立约束。新时效配置更严格时采用更严格值，不追溯重算已结算结果。

v0.3.0 等待预约按旧字段兼容读取，版本标记默认为 1，新预约为 2；旧预约不追溯发放本版新增心理需求。旧原始事件、活动提交与回执保持不变。

## 按文件修改笔记

| 文件 | 内容 |
| --- | --- |
| cognition/need_appraisal.py | 新增心理需求策略、权重、保留轴屏蔽与限幅 |
| cognition/evaluation.py | needs_delta/needs_reason、效果时效与需求配置；拒绝物理需求评价 |
| cognition/semantic.py | 社交事实标记、保留需求轴、柔性修正参数；兼容旧 task_* 配置 |
| application/evaluation.py | 相同任务先验、柔性修正、聊天与活动心理需求映射 |
| application/evaluation_persistence.py | 一次提案原子提交两通道；心理收益冷却、边界处理和过期归档 |
| application/appraisal_persistence.py | 改查活跃调度计划；旧预约兼容、严格上限与新旧需求策略隔离 |
| application/persistence.py | 为可信评价提案校验心理需求，普通情绪接口继续受原约束 |
| activity/feedback.py | 新上下文标记的序列化兼容，旧反馈不补写默认字段 |
| application/host.py | 需求变化触发仲裁；管理员显示需求变化及原因 |
| infrastructure/storage.py | 为当前调度查询补建辅助索引 |
| _conf_schema.json、metadata.yaml | 配置默认值与 v0.3.1 |
| tests/test_appraisal.py、tests/test_local_evaluation.py | 新回归用例及需求变化的预期更新 |

## 验证与自行打包

```powershell
python -m unittest discover -s tests -p "test_appraisal.py" -v
python -m unittest discover -s tests -p "test_local_evaluation.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

新增用例覆盖：心理需求和情绪一条提案、纯需求路径、社交事实约束、固定完成收益不重复、跨 checkpoint 冷却、零证据先验一致、高强度区分、两小时旧反馈不补发、禁止历史扫描的待评价查询及 SQLite 查询计划、v0.3.0 预约兼容。

按 PACKAGING_FILES_v0.3.1.txt 保留相对目录自行打包，main.py 位于压缩包根目录。实现者未运行上述测试或 QQ 联调，也未生成 ZIP。
