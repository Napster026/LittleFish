# 手动验证

测试由开发者在项目根目录运行。自主活动选择、最小行为仲裁与情绪引擎已由开发者测试通过；情绪偏好评分也已通过；用户已验证 QQ 私聊基础链路；v0.2.1 全量测试已由用户确认通过；v0.3.0 全量测试已由用户确认通过；v0.5.3 全量测试已由用户确认通过；v0.6.2 测试已由用户确认通过；本轮 v0.7.1 尚待用户验证。以功能行为和真实缺陷作为验收依据，不以测试数量衡量进度。

本轮 v0.7.1 仅做静态检查，未运行测试、实际数据库或 QQ：

```powershell
python -m unittest discover -s tests -p "test_relationship*.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

新增每日倾向回归：负值公式与零钳制、0..0.5 保留、正向超额减半、多日折叠、日期游标、04/06 点边界、03:30 新消息/新档案不提前结算、旧午夜事件迁移到新时段、重启和重复投递、离线只记一条数值维护、先回归后互动、独立额度/熟悉度/最近互动、下一计划失败原子回滚、异常行隔离及跨平台分页。历史读取允许当前用户的全局数字日回归，但其他会话的聊天回执继续隔离。详见 [CHANGES_v0.7.1.md](../CHANGES_v0.7.1.md)。

本轮 v0.7.0 仅做静态检查，未运行测试、实际数据库或 QQ：

```powershell
python -m unittest discover -s tests -p "test_relationships.py" -v
python -m unittest discover -s tests -p "test_relationship_context.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

新增验证：既有聊天规则映射、身份/平台/角色隔离、独立冷却、正负合计日上限、本地预算日、重复/重启/历史不补算、默认私聊与群聊明确指向、无效/过期/旧消息、关系不增加需求或情绪结算、写入失败全回滚、索引历史无正文；当前身份只读注入、切换用户、开关/范围门禁、提示裁剪、管理员命令；v6→v7 无损迁移及失败回滚/重试。旧结构夹具先移除 v7 关系表，所有成功升级断言更新到 v7，未知版本测试改为 v8。见 [CHANGES_v0.7.0.md](../CHANGES_v0.7.0.md)。

本轮 v0.6.2 仅做静态检查，未运行测试、实际数据库或 QQ：

```powershell
python -m unittest discover -s tests -p "test_activity_recall.py" -v
python -m unittest discover -s tests -p "test_activity_memory.py" -v
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -v
```

新增验证：同活动阶段在截断前分组，不同活动及角色/范围/来源/定义边界保留；代表选择优先回答查询，有内容总结与最小完成事实区别处理；详细过程最多两条，代表保留原评分位置；15 个 checkpoint 不阻断其他活动的有界补取，近期/词项/空查询都覆盖；会话和 self 共用八页额外预算，游标推进、达到目标立即停止、不跨范围、不写入或重发效果。现有计划展示回归仍需通过。详见 [CHANGES_v0.6.2.md](../CHANGES_v0.6.2.md)。

本轮 v0.6.1 仅做静态检查，未运行测试、实际数据库或 QQ：

```powershell
python -m unittest discover -s tests -p "test_activity_memory.py" -v
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_chat_context.py" -v
python -m unittest discover -s tests -v
```

新增验证：自主休息/进食/玩最小事实可召回并标注角色自身与模拟来源，跨会话及重启不复制数据、不重发效果；未知摘要、会话私有内容、其他角色与不符合结构的 self 不共享；关键词和近期上下文均可命中，开关、字符预算、两路有界检索和无历史事件扫描保持有效。v0.6.0 的 tick 耗尽测试已修正为沿用规范计划的真实重试次数。详见 [CHANGES_v0.6.1.md](../CHANGES_v0.6.1.md)。

本轮 v0.6.0 仅做静态检查，未运行测试、实际数据库或 QQ：

```powershell
python -m unittest discover -s tests -p "test_activity_memory.py" -v
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_chat_context.py" -v
python -m unittest discover -s tests -v
```

新增活动经历闭环：阶段与整项的身份、同 checkpoint 重放/冲突、不同 checkpoint 不误合并、原会话召回及内部 self 隔离、群聊开关、无内容进度不入库、完成/失败的最小事实、重启幂等、原子回滚、规则和延后评价只结算一次、迟到结果不改记忆、历史事件不回填、旧草稿回执与 JSON 兼容、长摘要存储/提示预算、同源反馈去重及裁剪兜底。测试不访问模型。原聊天反馈用例允许从短期反馈或同源记忆引用来源，但不允许重复注入。见 [CHANGES_v0.6.0.md](../CHANGES_v0.6.0.md)。

以下为历史版本验证说明。

本轮 v0.5.3 仅做静态检查，测试由用户执行：

```powershell
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_persistence_layers.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

新增验证：单条坏 JSON 不阻断正常行或 host.ready，修复失败/修订冲突按单行回滚，501 条全坏批次仍推进游标，待修复行不参与消息归并与上下文，状态/按 scope 的 pending 和坏行删除可用，日志不含正文，全局连接/保存点错误不被降级误吞。SQLite 与内存适配器保持局部回滚契约。详见 [CHANGES_v0.5.3.md](../CHANGES_v0.5.3.md)。没有执行测试或生成 ZIP，数据库仍为 v6。

本轮 v0.5.2 仅做静态检查，测试由用户执行：

```powershell
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -v
```

新增验证：排序后展示选择、粗细组最高排名、非相邻重复不占限额、不同具体安排及主体/来源/状态边界、未知元数据和非计划不去重；真实临时库中保留两条，召回一条且正文/修订/来源/角色不变。见 [CHANGES_v0.5.2.md](../CHANGES_v0.5.2.md)。没有执行测试或生成 ZIP，数据库仍为 v6。

以下为历史版本验证说明。

本轮 v0.5.1 仅做静态检查，测试由用户执行：

```powershell
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_persistence_layers.py" -v
python -m unittest discover -s tests -v
```

新增验证：长文填充不会天然加分，importance 进入主排序，30 天半衰及重新确认，词形/否定保留，有界候选批量评分，终态拒绝普通反向更新；v5→v6 迁移和旧索引重建不改原 JSON/修订/来源/角色，失败回滚及重试，独立时间列的包含边界条件。说明见 [CHANGES_v0.5.1.md](../CHANGES_v0.5.1.md)。没有执行单元测试、真实库升级或生成 ZIP。

以下为历史版本反馈和验证说明。

用户全量回归报告 351 项、1 项旧夹具错误；已修正 `test_activity_rewards.py` 在直接构造 completed 快照时漏写 status 索引的问题，并独立保留索引损坏的拒绝用例。业务代码未改，v0.5.0 与打包清单不变。实现者未运行测试，请先复测：

```powershell
python -m unittest discover -s tests -p "test_activity_rewards.py" -v
python -m unittest discover -s tests -v
```

本轮 v0.5.0 仅做静态检查，测试由用户执行：

```powershell
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

重点：用户给出的“明天出门→明天逛街看电影”、近期候选、不同安排/人物/日期隔离、跨午夜、明确改期/取消、偏好维度、来源修订、删除后重投不复活、事务回滚、局部索引查询和独立/总字符预算。QQ 替身覆盖管理员与绑定门禁、含空格正文、实际请求召回及删除预览清空。schema v5 迁移覆盖旧状态保留、失败回滚及重试，旧迁移夹具同步更新。详见 [CHANGES_v0.5.0.md](../CHANGES_v0.5.0.md)。没有执行测试或生成 ZIP。

以下为历史版本验证说明。

本轮 v0.4.0 仅做静态检查，测试由用户执行：

```powershell
python -m unittest discover -s tests -p "test_chat_context.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

重点验证：上下文总字符预算、只读状态投影、会话反馈隔离与原子回滚、重载连续、当前查询不扫历史事件/终态活动；保留原人设/提问/历史/工具，重复装配、绑定/命令/管理员门禁、群聊显式开启、配置开关与异常回退。迁移测试覆盖 v1/v2/v3 到 v4、活动索引回填不改业务 JSON、失败回滚和重试。`schema_fixtures.py` 只用于临时测试库重建旧结构，不能用于生产降级。

QQ 真实验证和安装迁移说明见 [CHANGES_v0.4.0.md](../CHANGES_v0.4.0.md)。替身用例不能替代真实 AstrBot/QQ 联调；本轮未生成安装包。以下保留历史版本验证说明。

本次 v0.3.2 先运行：

```powershell
python -m unittest discover -s tests -p "test_local_evaluation.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

重点检查：总开关关闭后普通及情感消息均不修改状态，不占用后续冷却；时间钳制保留平台原时间，重投/时钟追上/重载不重复结算，旧消息兼容，以及审计与消息整体回滚。实现者未运行测试，以下保留历史版本说明。

本次 v0.3.1 先运行：

```powershell
python -m unittest discover -s tests -p "test_appraisal.py" -v
python -m unittest discover -s tests -p "test_local_evaluation.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

重点验证：心理需求与情绪共用提案、纯需求路径、社交事实约束、完成收益保留轴、跨 checkpoint 冷却、零证据先验一致、高强度区分、两小时前的失败评价仅归档、活跃调度查询不读历史事件、辅助索引查询计划，以及旧 v0.3.0 预约兼容。本次实现者只做静态检查，没有执行这些测试。下方保留历史版本说明。

本次 v0.3.0 先运行：

```powershell
python -m unittest discover -s tests -p "test_appraisal.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -p "test_local_evaluation.py" -v
python -m unittest discover -s tests -v
```

新用例覆盖：事实与体验分离、严格 JSON、同次总结携带体验、唯一最终结算、等待跨重载、最新状态提交、结果与截止时间竞争、非法/uncertain 兜底、迟到仅存分析、整项收益不等待、预约及结果事务回滚、关闭评价与收紧幅度、耗尽计划恢复、旧反馈字段格式兼容，以及宿主真实调度链的替身验证。测试不访问任何模型。实现者只做静态检查，未运行这些测试。

以下保留此前版本的验证说明。

本次 v0.2.1 修复先运行：

```powershell
python -m unittest discover -s tests -p "test_autonomy.py" -v
python -m unittest discover -s tests -p "test_emotion.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

重点检查同版本启动回执与活动 ID 复用、pending 启动失败重试、空闲回执去重、新版本生成新键；以及默认 300 秒、已有间隔调整、同游标旧 tick 失效、旧格式计划恢复、参数越权拒绝与事务回滚。实现者未运行这些测试。

尚未安装依赖时：

```powershell
python -m pip install -r requirements.txt
```

本步先验证本地规则与阶段反馈，再运行完整回归：

```powershell
python -m unittest discover -s tests -p "test_local_evaluation.py" -v
python -m unittest discover -s tests -v
```

本步重点验证：

- 直接指向角色的短语规则、混合信号与歧义回退、配置类型约束及限幅。
- 原消息重复投递不重复冲击，同内容跨时间桶和重载保留冷却。
- 阶段反馈更新摘要/引用，保留活动状态、进度与原 tick，不发放完成需求收益。
- 执行器观察结果可携带阶段反馈；阶段失败与整项完成分离。
- 同一 checkpoint 内容冲突拒绝，原反馈重放一次生效；终态后拒绝新 checkpoint。
- 注入提交失败时，原反馈、情绪、回执和活动状态整体回滚。
- 数据库保持 v3；不调用外部模型，没有新增依赖。

实现者仅做静态检查与打包，测试仍由开发者运行。

已有宿主重点验证：

- 在嵌套插件包中加载业务代码，不污染 core/activity 等全局模块名。
- 配置界面默认值与严格模型一致，角色启动、关闭、到期恢复不重复创建活动。
- QQ 消息的稳定 ID、群/私聊与平台实例区分、原时间保留、重复投递与内容冲突。
- 控制项保存失败时保留旧策略，重载后策略与状态连续。
- 管理员校验、绑定范围过滤以及官方 API 替身下的插件入口行为。

测试不需要安装 AstrBot，入口测试只替身公开 API，不替代真实框架与 QQ 联调。业务和测试导入已统一到 persona_agent 命名空间，完整回归尤其重要；测试命令不变。本版配置与回归说明见 CHANGES_v0.3.1.md。

已有情绪偏好回归重点：

- 相同需求下情绪可改变合法活动排序；默认关闭、缺少情绪或缺少活动偏好时保留原分数。
- 修正同时受绝对与比例上限限制，心情与瞬时情绪可混合计算。
- 非正收益和不紧迫候选不能被情绪提升为可选；禁用与占用仍优先。
- 开启偏好时情绪提交触发活动选择，关闭时不增加触发入口。
- 评分分项、策略和情绪快照进入回执，旧候选数据继续兼容。
- 评分时临时投影情绪不改写状态，情绪反转后仍接续原 pending。

已有情绪引擎回归重点：

- 半衰期、耦合回归、分段不变性、冲击前回归及边界限制。
- 显式初始化、离线后按真实经过时长恢复、独立情绪游标。
- 冲击的原事件关联、重复提交、旧 tick 失效和重启后的回执重放。
- 普通需求结算保留情绪，错误提案及游标冲突不能提交。
- 初始化/冲击在下一条计划写入失败时一起回滚，重试可继续。
- 旧状态和旧提案缺少情绪字段时继续加载和重放。

已有仲裁回归重点：

- 禁用、模式及可用性等硬约束先于分数；禁用最高分后可选择较低分合法候选。
- running 继续，paused、人工 pending 和多项冲突保持空闲；全局关闭不强制终止当前活动。
- pending 接续匹配原实例、定义和模式，并检查当前开关和禁用策略。
- 仲裁结果与策略进入回执；旧回执不改写，启动前仍重新检查当前约束。
- 原选择评分、重复投递、回滚、重启和完成结算行为保持兼容。

原自主选择重点验证：

- 紧迫需求、活动副作用、边界收益、稳定同分与空闲决策。
- 需求事件触发启动，活动完成结算后再次选择，旧事件不重复创建活动。
- 启动失败与重启接续同一 pending；暂停及人工 pending 不被接管。
- 选择回执写入失败时，待开始实例一起回滚。

NeedEngine 的既有回归仍覆盖：

- 同样经过时长的需求变化不取决于 tick 次数，长时间变化仍保持边界。
- 旧状态第一次启用只建立时间基线，不补算未知历史。
- 普通需求效果不消耗自然时间游标；成功事件重放不重复计算。
- 重启后一次处理实际经过时间，确认失败后的重投仍保持幂等。
- 零速率或需求饱和时仍推进时间；后续计划保存失败时，需求、游标及回执共同回滚。
- 错误时间窗口或被更改的提案不能提交。

已有状态与持久化测试同步覆盖时间字段校验、旧 JSON 加载及旧提案回执重试，不另建重复用例。测试使用临时数据库和固定时钟，不修改实际角色数据。

自主选择复用现有状态、活动与调度流程，还应运行完整回归：

```powershell
python -m unittest discover -s tests -v
```

末尾应显示 `OK`。本次实现者只进行静态检查，未执行测试。

集成测试显式组合 `PersonaStore(SQLiteStorage(path))`。分层测试保留适配器独立加载、内存事务替换和回滚验证；活动测试保留生命周期、完成效果和 tick 耗尽回归。

数据库保持 v3 六张表，本地评价与阶段反馈保存为事件，现有状态和事件不改写。角色 JSON 含可选的 `needs_updated_at` 和 `emotion`，提案 JSON 含可选的 `needs_time` 和 `emotion_change`；缺失字段按默认值加载，旧回执按模型归一化比较，无需批量改写旧数据。没有新增第三方依赖。

自主活动选择和最小行为仲裁此前测试通过；本次本地规则及阶段反馈接线尚未运行测试，见 README 的完整示例。运行时内存队列恢复、社交、承诺与中断等完整高层行为仲裁、真实与混合活动、完整外部恢复仍未完成。需求时钟需要在应用组装时注册处理器、调用 `start_needs_clock()` 并保持原调度器运行，不会仅因导入模型或打开数据库而自动启动。
