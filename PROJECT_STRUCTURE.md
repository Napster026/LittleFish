# 项目结构复核（v0.5.3）

当前保留的目录都有实际运行职责。v0.4.0 清理过 20 个只有说明文字的 Python 占位文件和 3 个空 YAML；v0.5.0 按实际需求建立了会话记忆模块。完整语义记忆、巩固、关系和主动社交继续保留在路线图中。

## 当前目录

```text
persona_agent/                         # 仓库根目录
├── main.py                           # AstrBot 生命周期、QQ 入口、聊天请求钩子、管理命令
├── metadata.yaml                     # 插件 v0.5.3，目标 AstrBot 4.29.3
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
│   └── memory.py                     # 记忆管理结果显示
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
├── memory/                           # 会话记忆第一版
│   ├── models.py                     # 草稿、当前记录、结果和配置
│   ├── extraction.py                 # 明确计划/偏好及日期的有限提取
│   ├── retrieval.py                  # 评分、辅助索引、近期关系判断和只读展示选择
│   └── persistence.py                # 来源、修订、去重、只读召回、坏行隔离与索引修复
├── persistence/                      # 与业务无关的存储契约
│   ├── ports.py                      # 行读写、事务/保存点 UnitOfWork
│   ├── errors.py                     # 公共持久化异常
│   └── serialization.py              # 规范序列化辅助
├── infrastructure/storage.py         # SQLite、schema v6 迁移、原始行操作
├── assets/images/                    # 原始目标架构图，非当前完成功能图
├── tests/                            # 测试及临时旧库夹具，不放进安装包
├── README.md                         # 当前接入说明 + 长期设计基线
├── PROJECT_STRUCTURE.md              # 本文
├── CHANGES_v0.5.3.md                  # 本轮按文件笔记与验证步骤
└── PACKAGING_FILES_v0.5.3.txt         # 自行全量打包清单
```

树中省略了各业务包的 `__init__.py` 和保留的历史版本笔记；完整运行文件以本版打包清单为准。

## v0.4.0 占位清理与当前归属

| 删除文件 | 复核结果 / 后续归属 |
| --- | --- |
| `activity/selector.py` | 空壳，与已实现的 `selection.py` 重名近似，保留后者 |
| `activity/interrupt.py` | 空壳；现有暂停/恢复在 manager、service、arbiter，复杂抢占有具体需求后再拆 |
| `persona/profile.py`、`persona/relationship.py` | 空壳；完整人格、关系档案未实现，先保留设计需求 |
| `cognition/llm_client.py` | 空壳；聊天模型请求由 AstrBot 负责，不另建一套客户端 |
| `cognition/narrative.py`、`cognition/reflection.py` | 空壳；叙事、低频反思尚未接入 |
| `memory/__init__.py`、`memory/manager.py`、`memory/retrieval.py`、`memory/consolidation.py`、`memory/story_facts.py` | v0.4.0 删除的是空占位；v0.5.0 已实际实现 models/extraction/retrieval/persistence，manager、consolidation、story_facts 仍未建立 |
| `social/__init__.py`、`social/topic.py`、`social/policy.py`、`social/expression.py`、`social/commitment.py` | 全部为空占位；当前是被动接入聊天，主动表达、话题和承诺仍属后续功能 |
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

先验证本版“明确陈述 → 近期归并 → 类型/日期/关键词召回 → 删除”的闭环，再考虑自由语义提取、语义检索和记忆巩固。关系、反思、主动表达和真实视频/游戏执行器仍按实际输入输出建立模块。当前较大的 `application/persistence.py` 保留事务门面，具体记忆规则已在 `memory/persistence.py`，本轮没有改变历史事件身份或将 SQL 引入业务层。
