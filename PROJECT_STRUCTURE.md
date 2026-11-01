# 项目结构说明（当前 v0.9.1，含历史增量）

当前保留的目录都有实际运行职责。v0.4.0 清理过 20 个只有说明文字的 Python 占位文件和 3 个空 YAML；v0.5.0 按实际需求建立了会话记忆模块。完整语义记忆、巩固、关系和主动社交继续保留在路线图中。

## 历史目录快照（v0.7 阶段）

下方树保留当时结构；新增会话、主动表达及期限模块见本文末尾版本增量。当前全量文件以 [v0.9.1 打包清单](PACKAGING_FILES_v0.9.1.txt) 为准。

```text
persona_agent/                         # 仓库根目录
├── main.py                           # AstrBot 生命周期、QQ 入口、聊天请求钩子、管理命令
├── metadata.yaml                     # 插件 v0.7.1，目标 AstrBot 4.29.3
├── _conf_schema.json                  # 唯一的宿主配置入口（与 HostSettings 对照）
├── requirements.txt
├── __init__.py                       # 插件包标记
├── persona_agent/__init__.py          # 本地测试与嵌套插件加载的业务命名空间
├── application/                      # 宿主组装与跨业务事务协调
│   ├── host.py                       # PersonaHost 生命周期、受控操作、只读上下文入口
│   ├── settings.py                   # 严格配置与默认模拟活动目录
│   ├── qq.py                         # QQ 消息、身份、时间审计适配
│   ├── persistence.py                # PersonaStore，组合多个仓储的原子事务
│   ├── autonomy.py                   # 自主选择、仲裁与启动恢复
│   ├── needs.py                      # 需求时钟与提案处理
│   ├── emotion.py                    # 情绪时钟与提案处理
│   ├── evaluation.py                 # 聊天/活动事实转评价输入
│   ├── evaluation_persistence.py     # 一次性评价、时效、冷却、效果提交
│   ├── appraisal_persistence.py      # 延后评价预约、截止时间与结果提交
│   ├── chat_context.py               # 会话反馈/记忆、只读投影、请求装配
│   ├── activity_memory.py            # 已提交活动事实到经历记忆的适配
│   ├── memory.py                     # 记忆管理结果显示
│   └── relationship.py               # 聊天规则转关系信号、诊断显示
├── core/                             # 通用角色运行内核
│   ├── events.py                     # 统一事件与来源标记
│   ├── state.py                      # 需求、情绪与版本快照
│   ├── settlement.py                 # 统一变更提案与纯结算
│   ├── runtime.py                    # 单角色事件队列和生命周期
│   ├── router.py                     # 事件路由与处理结果校验
│   ├── scheduler.py                  # 持久化单次计划、重试与投递
│   ├── arbiter.py                    # 开始、继续、空闲的行为仲裁
│   ├── ports.py                      # 核心业务存储协议
│   └── persistence.py                # 状态、事件、结算与计划的事务内规则
├── activity/                         # 活动业务
│   ├── models.py                     # 活动定义、实例、状态与执行模式
│   ├── selection.py                  # 需求驱动的活动效用选择（唯一实现）
│   ├── emotion_preferences.py        # 有限的情绪评分修正
│   ├── executor.py                   # 执行器协议与结果
│   ├── registry.py                   # 按定义和模式注册执行器
│   ├── manager.py                    # 生命周期调用和结果校验
│   ├── service.py                    # 活动控制、自动推进与恢复
│   ├── ticks.py                      # 推进事件及耗尽处理
│   ├── rewards.py                    # 固定完成需求效果
│   ├── feedback.py                   # 阶段/完成反馈协议
│   ├── ports.py                      # 活动业务存储协议
│   ├── persistence.py                # 活动修订、效果与索引一致性
│   └── executors/simulated.py        # 当前唯一执行器实现
├── persona/                          # 纯状态与动力计算
│   ├── needs.py                      # NeedState / NeedEngine
│   └── emotion.py                    # VAD / EmotionEngine
├── cognition/                        # 无模型调用的评价与上下文计算
│   ├── appraisal.py                  # 聊天短语、歧义与强度规则
│   ├── evaluation.py                 # 评价配置、结果与共享规则
│   ├── semantic.py                   # 结构化体验协议、先验和柔性修正
│   ├── need_appraisal.py             # 心理需求幅度映射
│   └── prompt_builder.py             # 有字符预算的状态上下文
├── memory/                           # 会话记忆与有来源的活动经历
│   ├── models.py                     # 草稿、当前记录、活动经历元数据、结果和配置
│   ├── extraction.py                 # 明确计划/偏好及日期的有限提取
│   ├── retrieval.py                  # 评分、角色自身事实门禁、辅助索引、近期关系及计划/活动展示分组
│   └── persistence.py                # 来源、修订、去重、有界补取召回、坏行隔离与索引修复
├── social/                           # 已实现的基础关系业务
│   ├── models.py                     # 配置、关系档案、信号和回执
│   ├── relationships.py              # 纯更新规则、冷却、预算、倾向回归及结算日
│   ├── clock.py                      # 稳定的每日 04/06 点计划和旧午夜协议兼容
│   └── persistence.py                # 身份隔离、幂等、关系与有界历史查询
├── persistence/                      # 与业务无关的存储契约
│   ├── ports.py                      # 行读写、事务/保存点 UnitOfWork
│   ├── errors.py                     # 公共持久化异常
│   └── serialization.py              # 规范序列化辅助
├── infrastructure/storage.py         # SQLite、schema v7 迁移、原始行操作
├── assets/images/                    # 原始目标架构图，非当前完成功能图
├── tests/                            # 测试及临时旧库夹具，不放进安装包
├── README.md                         # 当前接入说明 + 长期设计基线
├── PROJECT_STRUCTURE.md              # 本文
├── CHANGES_v0.7.1.md                  # 本轮按文件笔记与验证步骤
└── PACKAGING_FILES_v0.7.1.txt         # 自行全量打包清单
```

树中省略了各业务包的 `__init__.py` 和保留的历史版本笔记；完整运行文件以本版打包清单为准。

## v0.4.0 占位清理与当前归属

| 删除文件 | 复核结果 / 后续归属 |
| --- | --- |
| `activity/selector.py` | 空壳，与已实现的 `selection.py` 重名近似，保留后者 |
| `activity/interrupt.py` | 空壳；现有暂停/恢复在 manager、service、arbiter，复杂抢占有具体需求后再拆 |
| `persona/profile.py`、`persona/relationship.py` | 当时为空壳；v0.7.0 基础关系已归 social，完整人格仍按需实现 |
| `cognition/llm_client.py` | 空壳；聊天模型请求由 AstrBot 负责，不另建一套客户端 |
| `cognition/narrative.py`、`cognition/reflection.py` | 空壳；叙事、低频反思尚未接入 |
| `memory/__init__.py`、`memory/manager.py`、`memory/retrieval.py`、`memory/consolidation.py`、`memory/story_facts.py` | v0.4.0 删除的是空占位；v0.5.0 已实际实现 models/extraction/retrieval/persistence，manager、consolidation、story_facts 仍未建立 |
| `social/__init__.py`、`social/topic.py`、`social/policy.py`、`social/expression.py`、`social/commitment.py` | 当时均为空占位；v0.7.0 已按需重建 social 基础关系，主动表达、话题和承诺仍属后续功能 |
| `infrastructure/outbox.py`、`infrastructure/permissions.py`、`infrastructure/observability.py` | 空壳；当前没有独立外发队列，范围/管理员检查位于入口，日志使用宿主 logger |
| `config/activities.yaml`、`config/persona.yaml`、`config/policies.yaml` | 只有注释和空映射，没有读取代码；当前统一使用 AstrBot 配置与严格模型 |

v0.4.0 删除前检查了内容及 Python 引用。历史版本笔记保留历史文件名，不据此恢复空壳。真实执行、长期记忆、外发消息等能力需要对应的权限和一致性机制；本轮只去掉空文件，没有取消这些约束。

## 为什么保留多个 persistence 文件

这些文件处理不同的事情，合并后反而会让存储和业务互相依赖：

| 位置 | 负责什么 |
| --- | --- |
| `persistence/` | 定义存储接口、公共错误和通用序列化，不理解角色或活动 |
| `infrastructure/storage.py` | 执行 SQLite 读写和结构迁移，不解释业务 JSON |
| `core/persistence.py` | 核对状态版本、事件身份、提案和定时计划 |
| `activity/persistence.py` | 核对活动修订、生命周期、完成收益和状态索引 |
| `memory/persistence.py` | 记忆归并、来源、修订、去重和范围内检索/删除 |
| `application/persistence.py` | 把上述规则放进同一事务，提供宿主使用的入口 |
| `application/*evaluation*_persistence.py` | 评价去重、冷却、延后处理与超时的业务规则 |

`persona_agent/__init__.py` 也需要保留：它把根目录业务包挂在统一命名空间下，支持本地测试以及 AstrBot 嵌套插件加载，没有复制另一份业务源码。

## 后续结构调整建议

先验证本版“明确陈述 → 近期归并 → 类型/日期/关键词召回 → 删除”的闭环，再考虑自由语义提取、语义检索和记忆巩固。基础关系已在 social 按实际职责建立；话题、承诺、反思、主动表达和真实视频/游戏执行器仍按实际输入输出建立模块。当前较大的 `application/persistence.py` 保留事务门面，具体记忆规则已在 `memory/persistence.py`，本轮没有改变历史事件身份或将 SQL 引入业务层。


## v0.8.0 会话状态增量

- `social/conversation.py`：独立的续聊配置、状态和纯判断。只保存身份、阶段、时间和计数，没有聊天正文。
- `social/conversation_persistence.py`：事务内按角色/会话精确读写；同会话单一当前对象，旧回调不能覆盖新入站。
- `application/host.py` / `application/persistence.py`：复用现有操作锁与事务，协调状态读写和诊断。
- `main.py`：绑定范围内接收、排除其他收件人/指令，受控放行当前消息的默认回复链；发送阶段钩子记录有限证据。
- `infrastructure/storage.py`：schema v7→v8 增加第十五张表 `conversations`，不解释业务 JSON。

会话状态不属于 needs、VAD 或关系倾向；也不复制 AstrBot 聊天历史。当前没有新模型服务、向量库或独立发消息执行器。仍由 AstrBot 执行回复，后续真正需要主动发起聊天时再接入行为仲裁。


## v0.8.1 会话仲裁增量

- `core/arbiter.py` 只接收 `reply_pending` / `waiting_followup` 原因，不依赖 QQ、social、PersonaHost 或会话正文；限制开始和接续 pending，不抢占 running。
- `social/conversation.py` 从会话时间/阶段/剩余续聊次数投影占用，`social/conversation_clock.py` 构造带会话修订号的单次到期计划。
- `application/conversation.py` 适配宿主当前 QQ 绑定范围；`application/persistence.py` 在预留活动的同一事务内汇总占用，分页只读每会话一条游标。
- `application/host.py` 在会话变更、截止投递和启动恢复时协调检查与仲裁，复用既有操作锁和调度器。没有新增发送执行器、模型判别器或状态结算器。


## v0.9.0 主动表达增量

- `social/expression.py`：目标、配置、意图与预算状态及安静时段/冷却规则；不依赖 AstrBot 或网络。
- `application/expression.py`：按角色精确读取一条表达游标，选择近期实际私聊目标、原子预留预算并写状态审计。
- `core/arbiter.py`：新增纯 `proactive_expression` 候选，比较效用并遵守活动和会话占用；核心不读取人格或平台。
- `application/host.py` / `application/persistence.py`：后台生成与发送协调，发送前复核，重启不确定状态收敛，正常入站取消旧意图。
- `application/astrbot_expression.py`：AstrBot v4.29.3 SDK 边界，读取目标现有对话与最终人设，调用一次聊天模型并主动发送纯文本。
- `social/conversation.py` 的可选出站引用及 `cognition/prompt_builder.py` 的有界只读展示：让角色知道自己刚才主动说过什么，不复制或覆盖 AstrBot 历史。

schema v9 新增第十六张表 `expression_heads`；每角色仅一条当前预算/意图，历史阶段写既有事件表，无事件扫描。尚未新增真实游戏/视频执行器或低频人格反思。


## v0.9.1 表达控制与期限增量

- `social/expression_clock.py`：以角色、意图 ID、会话和期限定义规范单次计划；不用会随阶段改变的预算游标修订号作为期限身份。
- `application/expression.py`：实际发送意图与期限任务同事务预留，预览不排队。
- `application/persistence.py`：取消、旧 pending 补计划、严格期限验证、幂等过期收敛；仲裁失败可沿原到期键恢复。
- `application/host.py`：取消生成任务不等待其在操作锁内退出；发送在途不宣称撤回，旧任务退出后接续新预留意图。暂停/恢复与配置持久化、跨日诊断、分级超时。
- `main.py`：绑定管理员私聊的 show/cancel/pause/resume 入口及配置保存回滚。

数据库保持 schema v9 / 十六张表，仍使用既有调度器、操作锁和状态机，无新模型调用或独立外发服务。
