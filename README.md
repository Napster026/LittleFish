# Persona Agent v1.0

基于 AstrBot 的事件驱动、持久化状态与可插拔活动执行器的人格化 Agent。

> 本文是已确定的架构开发基线，不是已完成功能清单。当前已实现基础模块、活动模拟与管理、活动持久化，以及完成活动与角色需求效果的原子结算和旧活动补结算。此前 141 项测试已由开发者本地运行通过；本步新增完成效果的 18 项测试待运行。活动选择与任务栈、自动时钟调度、待处理队列恢复及 AstrBot 插件入口尚未实现。文中“必须”“采用”表示实现约束；其余功能与接口细节仍需逐步落实。

## 1. 定位与设计原则

角色拥有稳定人格、连续生活状态、情绪变化、社交关系和长期记忆，能够参与群聊。第一版采用持续状态模拟与按需叙事相结合的方式；未来通过执行器接入真实视频、游戏和外部工具。

Python 负责状态、数值计算、权限、调度与最终行为控制；DeepSeek 负责理解、评价、反思及语言生成。模型生成活动描述不代表执行了真实操作。

| 原则 | 开发约束 |
| --- | --- |
| 事件驱动 | 消息、活动变化、观察和计时到期统一转为事件 |
| 状态单一所有权 | 持久状态只由统一状态结算器提交 |
| 认知与执行分离 | 模型提出评价与候选；执行器承担实际行为 |
| 活动可插拔 | 模拟、真实、混合执行器共享生命周期 |
| 记忆有来源 | 区分观察、模拟、推断和确认 |
| 表达与内部行为分离 | 有情绪或经历不意味着必须发言；沉默是合法行为 |

## 2. 总体架构

采用 **1 个 Agent Core + 4 个功能子系统 + 2 个基础设施层**。第一版统一运行在一个 Python 进程内，不拆微服务。

![Persona Agent 整体架构](assets/images/persona-agent-architecture.svg)

> 此图依据已定稿的“最终架构总览”内容与原卡片样式重建，包含两个基础设施层。图示表达目标架构；真实/混合执行器为后续扩展，不代表已实现。

图中连线表示逻辑协作，不授予模块跨边界直接写入状态的权限。

## 3. Agent Core

| 组件 | 职责 |
| --- | --- |
| `EventRouter` | 接收、分类、路由外部及内部事件 |
| `BehaviorArbiter` | 对全部候选高层行为进行统一仲裁 |
| `StateCoordinator` | 读取快照，协调校验与状态结算 |
| `TimeScheduler` | 管理定时事件与到期任务 |
| `AgentRuntime` | 启动、关闭、生命周期和异常恢复 |

行为仲裁顺序为：读取状态快照 → 构建候选 → 检查权限与硬约束 → 优先处理承诺等要求 → 对合法候选计算效用并选择 → 执行 → 反馈事件 → 统一结算。

候选包括继续活动、暂停并聊天、回复、记录待履行承诺、开始新活动和沉默。硬约束不能交给模型或概率采样决定。仲裁器管理高层行为，游戏逐帧控制属于具体执行器。

### 3.1 当前 EventRouter 接口

`core/router.py` 已实现精确事件路由：

| 接口 | 作用 |
| --- | --- |
| `register(event_type, handler)` | 注册某个具体事件类型的处理函数，例如 `ACTIVITY.completed` |
| `await dispatch(event, state)` | 将事件和当前状态交给对应处理函数，返回提案或 `None` |

处理函数的接口为异步 `(AgentEvent, AgentState) -> StateChangeProposal | None`。当前每个具体事件类型只能注册一个处理函数，由其汇总需求变化；重复注册报错，不覆盖原函数。事件名称复用事件模型的校验规则，没有通配或按类别自动广播。

路由器重新校验并复制输入数据，处理函数接收独立副本。返回提案时，路由器再次校验模型，并要求提案的 `event_id`、`agent_id`、`expected_version` 分别与本次事件和状态一致。返回非法类型或错误关联会报错；处理函数执行失败的异常原样向调用方传播，不视为正常无变化。

没有注册处理函数或处理函数明确返回 `None` 时，表示本次不产生需求变更提案。路由器不读写数据库、不发消息、不推进活动、不替代全局行为仲裁，也没有自己的事件队列。事件记录、状态读取、路由和提案提交现由 `AgentRuntime` 顺序编排。

处理函数应只计算提案，成功提案的 ID 和内容在重试时必须保持一致。路由器本身不去重，也不能靠重新生成一份提案保证重试安全；运行时会先识别已结算事件，再复用原提案通过存储层读取最新状态。

### 3.2 当前 AgentRuntime 接口

`core/runtime.py` 已实现绑定单个 `agent_id` 的运行时。构造时传入已打开的 `SQLiteStorage` 与已注册规则的 `EventRouter`，角色初始状态必须预先创建。

| 接口或参数 | 作用 |
| --- | --- |
| `await start()` | 检查角色存在，启动一个顺序处理事件的后台任务 |
| `await submit(event)` | 复制并入队事件，等待处理结果，返回最新角色状态 |
| `await stop()` | 停止接收新事件，等待已接收事件处理完 |
| `is_running` | 当前是否接受新事件 |
| `max_pending` | 等待队列上限，默认 100，不包含正在处理的事件 |
| `clock` | 可选的结算时钟，默认使用当前 UTC 时间；测试可传入固定时钟 |

支持 `async with AgentRuntime(...)` 自动启动与停止。数据库连接仍由外部管理，必须先停止运行时，再关闭数据库。启动不会重置角色状态；正常停止后允许再次启动。

```text
submit → 有界内存队列 → 单个工作任务
  → record_event：记录事件并检查内容冲突
  → get_committed_proposal：查询该角色该事件是否已经成功结算
      → 已结算：复用原提案返回当前最新状态，跳过处理函数
      → 未结算：读取当前状态 → dispatch
          → 无提案：返回当前状态
          → 有提案：commit_proposal → 返回已提交状态
```

普通处理异常回传给该次 `submit`，不会停止后续事件处理，也不会自动重试。队列满时抛出 `RuntimeQueueFullError`，该事件未接收、未由本次调用记录；尚未启动或正在停止分别报告明确的生命周期异常。已接收事件不会因调用方取消等待而撤回；停止等待被取消时，后台仍继续完成正常停止。处理函数不能等待同一运行时的 `submit` 或 `stop`，避免工作任务等待自身。

本阶段仅将成功结算的提案作为持久化去重依据。无对应处理函数或返回 `None` 的事件虽然会记录，但没有“已完成且无变更”的记录，再次提交仍经过路由。等待队列本身尚未持久化；进程异常退出后不会自动恢复未处理事件或扫描重放事件日志，当前不能宣称已实现完整重启恢复。

同一角色应只启动一个运行时负责正常事件顺序。不同连接造成的版本冲突仍由存储层拒绝并回传；当前不自动重新计算。串行工作任务目前适合短规则处理，未来长耗时模型调用需通过独立任务与结果事件解耦，避免长期占用事件处理入口。

## 4. 四个功能子系统

### 4.1 人格状态系统

| 组件 | 职责 |
| --- | --- |
| `PersonaManager` | 固定核心角色卡、软层人格与属性；支持低频演化 |
| `EmotionEngine` | VAD 三维情绪、心情和人格基线；事件冲击与时间回归 |
| `NeedEngine` | 精力、饱食度、无聊度、社交需求 |
| `RelationshipManager` | 熟悉度、信任、亲近感等长期关系档案 |

瞬时情绪向心情回归，心情向人格基线回归。关系档案不是情绪的第三层，也不自动构成回应义务。情绪可以有限影响活动偏好、社交意愿和表达风格，但不拥有最终发言决定权。属性保留基线、上下限、成长速率、可选衰减与行为映射。

本系统计算状态变更提案，由结算器持久化。衰减系数、变化速率和权重属于待调优配置，不视为已定稿参数。

### 4.2 活动执行系统

- `ActivityManager`：选择与管理活动实例、进度、中断和恢复。
- `ActivityRuntime`：管理生命周期、执行策略、能力检查与资源控制。
- `ActivityExecutor`：以模拟、真实或混合方式执行活动并反馈事件。

活动定义与活动实例必须分离。同一种活动可以反复执行，每次实例独立记录阶段、进度、执行上下文及来源。暂停活动使用可恢复队列或轻量任务栈，保留暂停原因、有效性、恢复成本和相关承诺。

### 4.3 认知与记忆系统

| 组件 | 职责 |
| --- | --- |
| `AppraisalEngine` | 评价事件的情绪、关系和记忆意义 |
| `ReflectionEngine` | 低频归纳经历，提出人格与偏好变化建议 |
| `NarrativeEngine` | 补充模拟活动或真实观察后的叙事 |
| `MemoryManager` | 写入、检索、遗忘与巩固 |
| `StoryFactManager` | 维护活动目标、阶段事实与叙事连续性 |

模型输出评价或候选事实，不输出可直接执行的数据库修改命令。游戏观察记录死亡一次，叙事不能改成三次；只观察了视频前五分钟，不能标记为完整观看。反思不得再次结算已经由原始事件产生的情绪或需求收益。

### 4.4 社交与表达系统

| 组件 | 职责 |
| --- | --- |
| `TopicTracker` | 追踪话题与角色参与程度 |
| `SocialPolicy` | 产生回应、主动发言等候选，管理社交预算 |
| `ExpressionGate` | 检查是否表达及表达时机 |
| `ResponseGenerator` | 组装允许使用的上下文，生成回复 |
| `CommitmentManager` | 管理待履行承诺 |

`SocialPolicy` 不拥有全局决策权；是否打断活动由 `BehaviorArbiter` 统一协调。直接提问、明确指令、延续话题、兴趣话题和无关消息采用不同优先级。“稍等”、活动完成通知等也必须经过表达阀门。

## 5. 统一事件协议与六个关键对象

### 5.1 事件协议

| 类别 | 示例 |
| --- | --- |
| `SOCIAL` | 群消息、私聊、话题结束 |
| `ACTIVITY` | 开始、阶段变化、完成、失败、中断 |
| `OBSERVATION` | 视频片段观察、游戏状态变化 |
| `INTERNAL` | 情绪回归、需求更新、反思到期 |
| `SYSTEM` | 启动、异常、重启恢复 |

事件模型已在 `core/events.py` 中实现。`event_type` 使用 `类别.小写动作名`，例如 `SOCIAL.message_received`、`ACTIVITY.started`；`category` 属性从前缀派生，不重复持久化。以下是当前调用示例：

```python
from datetime import datetime, timezone
from uuid import uuid4

from core.events import AgentEvent, EventProvenance

event = AgentEvent(
    event_id=str(uuid4()),
    event_type="SOCIAL.message_received",
    timestamp=datetime.now(timezone.utc),
    source="astrbot",
    scope_id="group-001",
    payload={"message_id": "message-001", "text": "你好"},
    provenance=EventProvenance.OBSERVED,
)

serialized = event.model_dump_json()
restored = AgentEvent.model_validate_json(serialized)
```

`event_id` 由生产方分配，重试同一事件必须沿用原 ID；模型不检查跨事件唯一性，实际去重由后续存储与结算实现。`correlation_id` 关联完整流程；`causation_id` 记录直接因果；`scope_id` 表示作用域；`activity_id` 指向活动实例。这四个关联字段均可省略。`schema_version` 默认且当前仅接受整数 `1`。`provenance` 必须显式按实际来源填写，没有默认值。

标识和来源不能是空白或包含首尾空白，时间必须带时区并统一转为 UTC；`payload` 只接受 JSON 对象及其合法嵌套值，拒绝非有限数值与循环引用；未知字段会被拒绝。事件字段禁止重新赋值，但 `payload` 的嵌套容器并非深度不可变，调用方应视其为只读。事件信封目前不校验消息或活动专属的 payload 字段，具体业务模型后续定义。

### 5.2 六个关键对象

| 对象 | 已确定的职责与边界 |
| --- | --- |
| `AgentEvent` | 模块间统一事件载体，保存来源、关联、因果和版本 |
| `AgentState` | 角色持久状态的逻辑集合，不要求整块 JSON 存储 |
| `ActivityDefinition` | 活动类型的定义，包括规则、前置条件与可用执行方式；不保存某次执行进度 |
| `ActivityInstance` | 一次具体活动的阶段、进度、生命周期与执行上下文 |
| `ActivityExecutor` | 执行活动的统一接口，声明能力并反馈观察或执行结果 |
| `StateChangeProposal` | 规则或认知模块提出的候选状态变更，经校验才能提交 |

后续接口设计需明确各对象字段、返回类型和错误语义；不得把尚未讨论的完整字段表当作已实现协议。`AgentSnapshot` 是按事件需要构造的轻量读取视图，不替代持久状态。

### 5.3 统一结算

```text
事件产生 → 规则计算与事件评价 → StateChangeProposal
        → 校验 / 去重 / 限幅 → SQLite 事务提交 → 后续事件
```

单进程内采用异步事件队列和一个逻辑状态写入者保证更新顺序。明确哪些事件可触发后续处理，并限制因果链深度，避免“情绪更新触发情绪更新”的循环。

当前 `core/settlement.py` 已实现 `StateChangeProposal` 数据模型，`persona/needs.py` 已实现 `NeedDelta`。提案先支持四项需求变化：

| 提案字段 | 含义 |
| --- | --- |
| `proposal_id` | 生产方分配的提案 ID，重试同一提案必须沿用原 ID |
| `event_id` | 提案依据的事件 ID |
| `agent_id` | 要改变状态的角色 ID |
| `expected_version` | 提案基于的状态版本，必须明确提供非负整数 |
| `needs_delta` | 四项需求的变化量，未填写项为 0，至少一项非零 |

```python
from core.settlement import StateChangeProposal
from persona.needs import NeedDelta

proposal = StateChangeProposal(
    proposal_id="proposal-001",
    event_id="event-001",
    agent_id="persona-001",
    expected_version=0,
    needs_delta=NeedDelta(energy=10, social_need=-5),
)
restored = StateChangeProposal.model_validate_json(proposal.model_dump_json())
```

变化量只接受有限整数或浮点数，可正可负；它表示增减量，不是最终需求值，不受最终状态 0～100 的范围约束。比如 `energy=10` 表示精力增加 10，`social_need=-5` 表示社交需求降低 5。不同字段一增一减即使总和为 0，也属于有效变化。四项全为 0 则拒绝创建。

提案与嵌套变化量均禁止字段重新赋值和未知字段。提案模型只进行格式校验与 JSON 转换；通过格式校验不代表可以提交。

当前 `core/settlement.py` 中的 `settle_state(state, proposal, settled_at=...)` 实现内存结算，接收已校验的状态与提案，执行以下步骤：

1. 核对角色 ID，避免把一个角色的提案作用到另一个角色。
2. 核对 `expected_version` 与当前 `version`，旧版本或未来版本均拒绝。
3. 检查调用方传入的结算时间，必须为带时区的 `datetime`，且不能早于状态更新时间；相同时间允许连续结算。
4. 将四项变化量加到当前需求值上，再分别限制在 0～100。
5. 通过模型构造重新校验新状态，版本加 1，更新时间统一为 UTC。

函数返回新状态，原状态与原提案保持不变。即使需求值已到边界、限幅后所有数值不变，成功结算仍推进版本号并更新结算时间。

| 异常 | 处理含义 |
| --- | --- |
| `AgentMismatchError` | 目标角色不一致，应检查提案路由 |
| `StateVersionConflictError` | 状态版本不匹配，应重新读取状态并按策略重新计算 |
| `SettlementTimeError` | 结算时间无效或发生倒退 |

上述异常均继承 `SettlementError`。此函数只计算候选新状态，不保存内部状态，不检查事件存在性、权限或提案去重，不执行数据库事务。把同一份旧状态重复传入，仍能重复计算结果；版本检查不能替代持久化幂等机制。候选状态需通过 `SQLiteStorage.commit_proposal()` 完成去重、版本核对和原子提交后才能正式生效。单次变化幅度策略留待后续按活动规则配置。

### 5.4 SQLite 持久化与原子提交

`infrastructure/storage.py` 已实现异步 `SQLiteStorage`，使用 `aiosqlite`。数据库父目录由调用方创建；通过 `async with SQLiteStorage(path)` 或显式 `open()` / `close()` 管理连接。

| 接口 | 作用 |
| --- | --- |
| `create_state(state)` | 首次保存角色状态，已有角色报错，不覆盖重置 |
| `get_state(agent_id)` | 读取角色当前已提交状态，不存在返回 `None` |
| `record_event(event)` | 保存完整事件；同 ID 同内容重复记录无副作用，同 ID 不同内容报错 |
| `get_event(event_id)` | 读取原事件，不存在返回 `None` |
| `get_committed_proposal(agent_id, event_id)` | 读取已成功提交的原提案，不存在返回 `None`，供运行时跳过重复规则执行 |
| `commit_proposal(proposal, settled_at=...)` | 从数据库读取当前状态，在事务中结算并保存结果 |

当前数据库包含五张表：`agent_states` 保存角色 ID、版本及最小状态 JSON；`agent_events` 保存事件 ID 与完整事件 JSON；`state_settlements` 保存角色需求结算；`activity_instances` 保存固定活动定义与最新实例；`activity_commits` 保存活动原修订对应的请求和结果。当前角色 JSON 状态仅包含最小需求模型，活动另表存储，后续复杂子系统仍按职责与查询需要拆表。

一次新提案的提交顺序为：

```text
BEGIN IMMEDIATE
  → 检查提案 ID 与内容是否重复或冲突
  → 读取数据库中的角色状态
  → 检查该角色的该事件是否已结算、事件是否已记录
  → 调用 settle_state 核对版本、计算并校验新状态
  → 按角色 ID 和旧版本条件更新状态
  → 写入结算记录
COMMIT
```

任一步失败，事务回滚，不留下“状态已经变化但提案没有记录”的部分结果。实例内用异步锁避免同一连接上的事务交错；数据库写事务与条件更新共同保护不同连接之间的版本一致性。数据库锁等待超时等底层错误向调用方报告，当前不自动重试。

当前提交策略：**同一角色、同一事件的需求变化先汇总为一份提案**。相同提案 ID、相同内容再次提交，返回该角色当前最新状态，不重复修改，不用历史结果覆盖较新的状态。同提案 ID 内容不同抛出 `ProposalConflictError`；同角色同事件换提案 ID 再次提交抛出 `EventAlreadySettledError`。同一事件可以分别作用于不同角色。

失败提案不会标记为已处理；修正后可按规则重试。成功提案的重试不再检查原预期版本或使用新的结算时间，因为没有执行新结算。已记录事件即使后续结算失败也会保留，事件记录与状态提交是两个独立事务。

除上述冲突外，存储层会报告未打开连接、角色不存在或已存在、依据事件不存在等明确异常。数据库通过 `PRAGMA user_version` 标记结构版本，当前支持版本 2；首次初始化和版本 1 升级均在写事务中执行，保留旧角色、事件与结算记录，迁移失败时回滚表结构和版本号。其他非零版本拒绝打开，不自动降级。存储接口面向内部调用，尚不承担平台身份、作用域和操作权限检查，也不保证外部消息发送或游戏操作的原子性。

## 6. 运行流程

采用三个异步循环与一个统一事件入口，循环之间不各自写数据库。

| 循环 | 流程 |
| --- | --- |
| 社交 | 收到消息 → 生成社交候选 → 全局仲裁 → 按需召回 → 生成回复 → 表达检查与发送 → 结果结算 |
| 活动 | 检查到期事件 → 更新需求及情绪 → 推进活动 → 处理反馈 → 结算 → 选择后续活动 |
| 反思 | 收集未整理的重要事件 → 总结经历 → 提取记忆及变化建议 → 校验与提交 |

保留三个阀门：**中断阀门**决定外部事件是否影响活动；**召回阀门**决定何时检索哪些记忆；**表达阀门**决定内部意图是否转为消息。召回也服务于活动选择和反思。

时间推进采用时间戳与懒更新；活动完成、承诺到期等需要主动触发的节点由调度器产生事件。重启时读取已提交状态、暂停活动、承诺和待处理事件，按能力与恢复策略重新协调，不凭模型叙事认定外部操作已经成功。

## 7. ActivityExecutor：模拟、真实与混合兼容

```python
from typing import Protocol
from activity.executor import ActivityContext, ActivityExecutionResult, ExecutorCapabilities
from activity.models import ActivityInstance

class ActivityExecutor(Protocol):
    @property
    def capabilities(self) -> ExecutorCapabilities: ...
    async def start(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def step(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def observe(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def pause(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def resume(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def stop(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def snapshot(self, context: ActivityContext) -> ActivityInstance: ...
```

该协议已在 `activity/executor.py` 定义，并补齐上下文、返回类型和能力声明。执行器必须声明能力，不能假定所有环境都支持暂停、恢复或快照；协议本身不自动实现这些操作。

| 模式 | 进度与事实依据 | 叙事边界 |
| --- | --- | --- |
| 模拟 `Simulated` | 规则、时间和阶段事件 | 按需补充细节，经历标记为模拟 |
| 真实 `Real` | 实际观察和外部操作结果 | 不能编造未观察到的进度或成功结果 |
| 混合 `Hybrid` | 真实反馈结合叙事补充 | 真实事实与补充内容分别保留来源 |

共享生命周期不意味着共享进度算法。时间型、过程型、交互型活动可以分别依据时间、阶段和外部事件推进。真实游戏的实时控制可在未来独立进程内运行，以事件接口与内核交换状态。

第一版不实现执行器之间自由迁移。模式切换必须有明确策略和事件记录：真实游戏断连后，不能自动用模拟器续写同一局并将结果当作真实成绩。

### 7.1 当前活动模型

`activity/models.py` 已实现两个只读模型与实例创建函数，不启动活动，不发事件，不结算需求，也不写数据库。

`ActivityDefinition` 保存一种活动的规则：

| 字段 | 含义 |
| --- | --- |
| `definition_id` | 活动类型 ID，例如 `rest` |
| `name` | 活动名称，例如“休息” |
| `supported_modes` | 明确声明支持的模式列表，不能为空或重复 |
| `estimated_duration_seconds` | 可选预计时长，提供时必须是有限正数；不代表真实完成时间 |
| `completion_needs_delta` | 可选完成效果模板，使用 `NeedDelta`；没有需求效果时为 `None` |

`ActivityInstance` 保存一次具体活动的数据：

| 字段 | 含义 |
| --- | --- |
| `activity_id` | 本次活动实例 ID，与事件中的 `activity_id` 对应 |
| `definition_id` | 关联的活动类型 ID |
| `agent_id` | 所属角色 |
| `mode` | 本次执行模式：`simulated`、`real` 或 `hybrid` |
| `created_at`、`updated_at` | 必填带时区时间，统一为 UTC，更新时间不得早于创建时间 |
| `status` | 默认 `pending`，另有 `running`、`paused`、`completed`、`failed`、`cancelled` |
| `progress` | 0～1 的量化进度，或 `None` 表示未知；不能用未知进度推断已完成 |
| `stage` | 可选的当前阶段文本 |
| `revision` | 活动实例修订号，默认 0，每次执行器产生状态变化时加 1；独立于角色状态 `version` |

待开始实例的进度只能为未知或 0，已完成实例的进度必须为 1。模型校验单份数据的一致性，不验证历史状态能否迁移到当前状态；实际开始、暂停、恢复、终止、进度推进和来源事实核验留给活动管理器与执行器。

`create_activity_instance()` 根据定义创建待开始实例，并检查所选模式属于定义支持的模式。模拟实例初始进度为 0，真实与混合实例初始进度未知。该函数不查询数据库，不能证明执行器已安装或角色存在；活动实际持久化时，由 `create_activity()` 校验角色存在与实例 ID 唯一性。

```python
from datetime import datetime, timezone

from activity.models import ActivityDefinition, ExecutionMode, create_activity_instance
from persona.needs import NeedDelta

rest = ActivityDefinition(
    definition_id="rest",
    name="休息",
    supported_modes=[ExecutionMode.SIMULATED],
    estimated_duration_seconds=300,
    completion_needs_delta=NeedDelta(energy=10),
)
instance = create_activity_instance(
    rest,
    activity_id="activity-001",
    agent_id="persona-001",
    mode=ExecutionMode.SIMULATED,
    created_at=datetime.now(timezone.utc),
)
# instance.status 为 pending，进度为 0；尚未启动休息或增加精力。
```

示例时长与效果不是已调优的角色默认值。定义的前置条件、选择偏好等规则，以及实例的执行上下文和持久化，将随后续功能逐步补充。不能直接把模型中的完成状态当作执行成功或需求收益已提交的证据。

### 7.2 执行上下文与模拟生命周期

`ActivityContext` 包含 `definition`、`instance` 和 `now`。它核对定义 ID、模式支持与时间顺序，要求当前时间带时区且不早于实例更新时间。`ExecutorCapabilities` 显式声明暂停、恢复、快照能力，默认均为不支持。

`ActivityExecutionResult` 包含候选新 `instance` 和 `events` 元组，并检查事件的活动关联。`snapshot()` 单独返回可序列化的实例。调用方每次须传入最新实例，同一次活动应沿用相同的定义。

当前 `SimulatedActivityExecutor` 仅处理 `simulated` 模式且具有明确时长的活动，不调用模型，不操作真实环境，不维护隐藏的进度缓存。

| 操作 | 当前模拟实现 |
| --- | --- |
| `start` | 仅允许待开始实例，变为运行中、进度 0，反馈 `ACTIVITY.started` |
| `step` | 仅允许运行中，按经过的运行时间增加进度；反馈进展或完成事件 |
| `observe` | 返回已有状态，不推进进度，不生成事件 |
| `pause` | 先结算至当前时刻的进度，再暂停；若已到完成条件则直接完成 |
| `resume` | 仅允许暂停实例，保留进度，重置本次更新时间后继续运行 |
| `stop` | 取消未完成实例；运行时间已到完成条件则直接完成；终态重复停止无事件 |
| `snapshot` | 返回当前实例供调用方序列化，不推进进度，不自行保存 |

进度基于“上次状态进度 + 本次运行时长 / 活动时长”。暂停前结算运行时间，恢复时以恢复时刻更新 `updated_at`，所以暂停期间不会计入运行时间。无时间变化的推进不产生事件和新修订；每次实际状态变化使 `revision` 加 1。已完成活动不能再次推进或恢复。

反馈事件显式标记 `provenance=simulated`，包含角色、定义、模式、状态、进度和修订号。事件 ID 由角色、活动实例、修订号和事件类型确定；同一输入与时间重复计算得到相同 ID。重复暂停之间经过恢复后修订号不同，因而能区分不同反馈。修订号现由活动提交接口用于数据库版本核对；仅计算出候选结果仍不能保证已经接受，必须通过提交接口。

完成事件可携带定义中的 `needs_delta` 模板，取消或普通进度事件不携带完成效果。执行器只返回候选结果；当完成状态变化通过 `commit_activity_result()` 正式提交时，存储层会按固定定义自动结算完成需求效果。反馈尚未自动广播到其他认知模块；已结算的完成事件再次交给当前 `AgentRuntime` 时会复用原提案，不重复执行需求规则。

```python
from datetime import timedelta

from activity.executor import ActivityContext
from activity.executors.simulated import SimulatedActivityExecutor

executor = SimulatedActivityExecutor()
# 在异步函数中使用前文 rest 与 instance：
started = await executor.start(ActivityContext(
    definition=rest, instance=instance, now=instance.created_at,
))
progressed = await executor.step(ActivityContext(
    definition=rest,
    instance=started.instance,
    now=started.instance.updated_at + timedelta(seconds=150),
))
# rest 的预计时长为 300 秒，本例进度变为 0.5，结果尚未持久化。
```

### 7.3 执行器注册与活动管理入口

`ExecutorRegistry` 在 `activity/registry.py` 中按 `(definition_id, mode)` 注册执行器。同一执行模式下，不同活动定义可以选择不同执行器；同一键不能重复注册，找不到执行器时不会自动切换模式。

| 接口 | 作用 |
| --- | --- |
| `register(definition_id, mode, executor)` | 检查定义 ID、模式、协议方法与能力声明，再登记执行器 |
| `resolve(definition, mode)` | 核对定义支持模式，再精确查找注册项 |

注册表只检查接口结构与能力数据格式，不能证明执行器真的能控制某个环境或遵守接口语义。实际操作仍需执行器自身检查，资源与权限控制仍待接入。

`ActivityManager` 在 `activity/manager.py` 中提供与执行器对应的七个异步方法。当前实现的是生命周期调用编排与结果检查，不维护实例缓存，不自动选择活动，也不管理暂停栈或写入数据库。

```text
调用方传入 ActivityContext
  → 校验并复制上下文
  → 检查本操作要求的输入状态
  → 注册表选择执行器
  → 检查暂停、恢复、快照等可选能力
  → 调用并等待执行器
  → 重新校验模型与跨调用约束
  → 返回候选实例及事件
```

管理器要求活动 ID、定义 ID、角色 ID、模式和创建时间保持不变。实例发生变化时，修订号必须恰好加 1，更新时间不能倒退，并且必须带反馈事件；终态实例不允许再改变。快照必须与输入实例一致，不借快照推进状态。

开始、推进、暂停、恢复的结果允许返回符合操作的目标状态，或完成、失败、取消等终态，以容纳外部环境反馈。模拟观察保持只读；统一管理入口允许其他执行器通过观察反馈状态变化，但同样受修订和事件约束，不统一规定进度算法或强制进度单调增加。

管理器还核对反馈中提供的定义、模式及修订号；模拟活动的活动与观察事件必须保留 `simulated` 来源。能力不支持、注册缺失或结果不合法时明确报错；执行器自身异常原样回传。结果检查发生在执行之后，不能撤销已经发生的外部操作，因此不能替代执行前的权限控制。

结果校验现公开为纯校验方法 `ActivityManager.validate_result()`，管理器与存储提交层共同使用；存储层不调用执行器，只重新检查候选结果，避免绕过调用管理层提交不合法的变化。

```python
from activity.manager import ActivityManager
from activity.registry import ExecutorRegistry

registry = ExecutorRegistry()
registry.register(rest.definition_id, ExecutionMode.SIMULATED, SimulatedActivityExecutor())
manager = ActivityManager(registry)
started = await manager.start(ActivityContext(
    definition=rest, instance=instance, now=instance.created_at,
))
# started 仍是候选结果，后续需通过活动持久化与事件提交入口接受。
```

### 7.4 活动持久化与修订提交

`ActivityRecord` 在 `activity/models.py` 中组合 `definition` 与 `instance`，保存本次活动的固定规则快照及当前实例，检查定义关联和模式。活动执行期间不能以同定义 ID 的新规则替换原快照。

`SQLiteStorage` 新增三个接口：

| 接口 | 作用 |
| --- | --- |
| `create_activity(definition, instance)` | 首次保存待开始、修订 0、创建和更新时间一致的活动；要求角色已存在，实例 ID 全库唯一 |
| `get_activity(agent_id, activity_id)` | 返回最新 `ActivityRecord`，不存在或角色不匹配返回 `None` |
| `commit_activity_result(operation, context, result, settled_at=...)` | 原子保存活动、反馈、提交记录和已配置完成效果，返回当前活动记录；结算时间可省略 |
| `settle_activity_completion(agent_id, activity_id, settled_at=...)` | 根据数据库完成证据补结算旧活动，安全重试返回当前角色状态 |

提交顺序为：

```text
校验上下文及候选结果
→ BEGIN IMMEDIATE
→ 按角色读取当前活动
→ 核对原定义快照
→ 检查该旧修订是否已经提交
→ 核对完整原实例与数据库一致
→ 按旧 revision 条件更新活动
→ 保存反馈事件
→ 保存请求与结果记录
→ 若本次完成，校验完成证据并生成需求提案
→ 结算角色状态并保存提案记录
→ COMMIT
```

同一操作、原上下文和候选结果完全一致的重试返回最新活动记录，不重新生成事件，不回退已经推进的实例。相同旧修订对应不同操作、时间或结果时抛出 `ActivityCommitConflictError`；未匹配数据库当前快照时抛出 `ActivityVersionConflictError`。调用方应保留原请求和结果重试，不能刷新时间后当作同一次提交。

无实例变化的合法操作只保存其反馈事件，不新增活动修订提交记录，避免占用该修订后续真实变化的提交位置。任何写入失败均回滚本次活动状态、事件和提交记录。原定义、角色与实例身份受检查，但这仍是内部数据一致性接口，不能替代外部操作权限控制。

```python
# 在数据库已打开、角色已创建且 manager 已注册执行器的异步调用中：
record = await storage.create_activity(rest, instance)
context = ActivityContext(
    definition=record.definition,
    instance=record.instance,
    now=record.instance.updated_at,
)
result = await manager.start(context)
saved = await storage.commit_activity_result("start", context, result)
# 本例只是开始活动，尚无完成效果；正式提交完成时才结算已配置的需求变化。
```

关闭重开后可按实例 ID 读回原规则与进度，并由调用方继续调用管理器；尚未实现自动查找并恢复全部活动、时钟调度或崩溃后待处理队列恢复。活动暂未加入 `AgentState` 的 JSON，读取角色状态不会自动包含活动实例。

### 7.5 完成需求效果与旧活动补结算

`activity/rewards.py` 的 `build_completion_proposal(record, event, state)` 是纯规则函数，不访问数据库。它使用活动创建时保存的 `completion_needs_delta`，校验完成事实后构造稳定 ID 的 `StateChangeProposal`。反馈提供的变化量若与原定义不一致则拒绝；反馈未提供变化量时仍以原定义为准。没有配置效果时不生成需求提案，也不因本次完成推进角色版本。

完成证据必须满足：实例确为 `completed`；完成提交记录与当前实例一致；记录中恰有一个 `ACTIVITY.completed` 事件且与事件日志一致；事件的活动、角色、定义、模式、状态、进度和修订均匹配；事件时间位于活动创建与更新时间之间。模拟模式要求 `simulated` 来源，真实与混合模式要求 `observed` 来源。来源标记校验属于内部一致性约束，不能替代真实环境适配器的授权与事实采集。

存储层在同一个写事务中提交活动变化、事件、活动提交记录、角色需求与需求提案记录。任一步失败均回滚；沿用 `settle_state()` 的角色版本递增和 0～100 限幅。角色结算部分复用不另开事务的 `_commit_proposal()`，避免嵌套事务与重复取锁。

首次结算时间默认采用完成实例的 `updated_at`，也可显式传入较晚的 `settled_at`；时间必须带时区，且不能早于活动完成记录或角色当前更新时间。若角色在迟到的活动结果之前已经更新，需要提供合适的提交时间，失败时不会留下部分活动或收益。

完成效果已提交后，重试复用原提案，返回当前角色状态，不改变已有版本和更新时间。已被不同规则或变化量结算的完成事件报 `ProposalConflictError`，不会再次发放或自动撤销先前独立事务。

上一阶段 v2 数据中，若活动已保存合法的完成记录与事件，但尚无需求结算，可显式补结算：

```python
role = await storage.settle_activity_completion(
    agent_id="persona-001",
    activity_id="activity-001",
    settled_at=datetime.now(timezone.utc),
)
```

补结算从数据库读取原证据，不接受外部随意传入的奖励值；并发或重复调用不会重复增加效果。没有完成提交证据、未完成或已取消的活动会拒绝。此步骤复用已有 v2 表结构，不需要新迁移，也不会自动扫描和补发全部历史活动。

## 8. 数据与记忆

```text
AgentState
├── PersonaState：核心人格、软层属性、自我摘要
├── EmotionState：人格基线、心情、瞬时情绪
├── NeedState：精力、饱食度、无聊度、社交需求
├── ActivityState：当前活动、暂停活动、执行上下文
├── SocialState：话题会话、关系档案、承诺
└── MemoryState：情景记忆、语义记忆、叙事事实
```

以上为完整目标结构。当前 `core/state.py` 只实现最小 `AgentState`，`persona/needs.py` 实现嵌套的 `NeedState`，人格、情绪、活动、社交与记忆状态后续逐步加入。

| 当前状态字段 | 含义 |
| --- | --- |
| `agent_id` | 必填角色 ID，禁止空白或首尾空格 |
| `needs` | 必填的四项需求状态，所有初始值必须明确提供 |
| `updated_at` | 必填的带时区更新时间，保存为 UTC；加载时不重置 |
| `version` | 非负整数状态修订号，初始默认 0；后续由结算器成功提交时递增 |

需求值采用 0～100 的统一刻度，接受整数和浮点数；精力 `energy`、饱食度 `satiety` 越高越充足，无聊度 `boredom`、社交需求 `social_need` 越高越需要满足。模型拒绝越界值、非有限数值、布尔值和数值字符串，不静默截断。初始值、变化速率与行为阈值由后续配置及策略确定。

```python
from datetime import datetime, timezone

from core.state import AgentState
from persona.needs import NeedState

state = AgentState(
    agent_id="persona-001",
    needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
    updated_at=datetime.now(timezone.utc),
)
restored = AgentState.model_validate_json(state.model_dump_json())
```

示例初始值仅用于说明调用方式。两个模型均拒绝未知字段和字段重新赋值；模型本身不执行需求变化、版本递增、并发检查或数据库写入。新状态由结算函数构造并重新校验，不能依赖跳过校验的复制接口实现状态更新。

在已创建上述 `state` 和前文 `proposal` 后，可以计算候选新状态：

```python
from core.settlement import settle_state

new_state = settle_state(
    state,
    proposal,
    settled_at=state.updated_at,  # 示例使用相同时间；运行时由调用方提供结算时刻
)
# 本例精力从 40 变为 50，社交需求从 50 变为 45，版本从 0 变为 1。
# 原 state 保持不变；new_state 尚未持久化。运行时应通过存储层提交提案。
```

数据库分别存储这些逻辑实体，所有角色数据预留 `agent_id`。活动按实例隔离；关系按角色、平台、群与用户身份管理。跨群身份关联不自动扩大权限或记忆可见范围。

事件日志记录来源事实，情景记忆保存经历，语义记忆提炼事实与偏好，自我叙事维持角色连续性。派生内容必须能追溯来源；收到某人发言是观察事实，不等于发言内容已被证实。

| 来源 | 含义 |
| --- | --- |
| `observed` | 实际收到或观察到的信息 |
| `simulated` | 模拟活动产生的经历 |
| `inferred` | 模型推断，不能自动提升为确定事实 |
| `confirmed` | 用户明确确认的信息，仍需保留确认来源与作用域 |

第一版采用 SQLite FTS 与重要记忆召回，向量检索可选。召回可综合相关性、近期性、重要性和激活度，并限制重复召回的强化。高级巩固与分级遗忘留待后续实现。

权限过滤必须覆盖检索、上下文组装、总结和输出。私聊信息默认不得在群内披露，包括通过摘要间接披露。记忆管理需提供相关记录的查看、更正与删除能力；具体命令及授权流程在接口阶段落实。

## 9. 工程目录

以下目录已建立，各包包含 `__init__.py`。当前已实现基础模型、内存结算、SQLite 角色与活动持久化、事件路由、单角色运行时、执行器协议、模拟执行器与活动调用管理，其余业务模块仍为职责占位。`ActivityRuntime`、`StateCoordinator` 和 `ResponseGenerator` 是逻辑组件，具体文件归属可在不改变职责边界的前提下细化。

当前项目根目录为 `persona_agent/`；`metadata.yaml` 中的 `name: astrbot_plugin_persona` 是插件标识，两者含义不同。

```text
persona_agent/
├── README.md
├── main.py                     # AstrBot 入口
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt            # 当前模块依赖
├── .gitignore
├── assets/images/              # 架构图
├── core/
│   ├── runtime.py               # 单角色队列与生命周期编排
│   ├── events.py                # 统一事件
│   ├── state.py                 # 最小角色状态
│   ├── router.py                # 精确事件路由与处理结果校验
│   ├── arbiter.py
│   ├── scheduler.py
│   └── settlement.py            # 提案模型与内存结算
├── persona/
│   ├── profile.py
│   ├── emotion.py
│   ├── needs.py
│   └── relationship.py
├── activity/
│   ├── models.py               # 活动定义、实例与模式状态枚举
│   ├── rewards.py              # 固定定义的完成效果提案规则
│   ├── manager.py              # 生命周期编排、能力与结果检查
│   ├── selector.py
│   ├── interrupt.py
│   ├── executor.py
│   ├── registry.py             # 按活动定义和模式注册执行器
│   └── executors/
│       └── simulated.py
├── cognition/
│   ├── llm_client.py
│   ├── appraisal.py
│   ├── reflection.py
│   ├── narrative.py
│   └── prompt_builder.py
├── memory/
│   ├── manager.py
│   ├── retrieval.py
│   ├── consolidation.py
│   └── story_facts.py
├── social/
│   ├── topic.py
│   ├── policy.py
│   ├── expression.py
│   └── commitment.py
├── infrastructure/
│   ├── storage.py               # SQLite 状态、事件与事务结算
│   ├── outbox.py
│   ├── permissions.py
│   └── observability.py
├── config/
│   ├── persona.yaml
│   ├── activities.yaml
│   └── policies.yaml
└── tests/
    ├── README.md               # 手动测试说明
    ├── test_events.py          # 事件模型测试
    ├── test_state.py           # 角色与需求状态测试
    ├── test_proposals.py       # 变更提案与需求变化量测试
    ├── test_settlement.py      # 内存结算测试
    ├── test_storage.py         # SQLite 持久化与事务去重测试
    ├── test_router.py          # 事件路由与存储衔接测试
    ├── test_runtime.py         # 队列、生命周期与重试编排测试
    ├── test_activity_models.py # 活动定义与实例测试
    ├── test_simulated_executor.py # 模拟执行器与协议测试
    ├── test_activity_manager.py # 注册与活动编排测试
    ├── test_activity_storage.py # 活动事务保存与数据库迁移测试
    └── test_activity_rewards.py # 完成效果、补结算与原子性测试
```

`metadata.yaml` 是元信息草稿，作者与仓库地址待补齐；`_conf_schema.json` 与三个 YAML 配置暂为空映射，具体字段随接口设计确定。事件模块依赖已记录于 `requirements.txt`，手动测试步骤见 [tests/README.md](tests/README.md)；插件安装流程待接入 AstrBot 后补充。

## 10. 第一版范围与未来扩展

第一版交付目标：固定人格与软层属性结构；三维情绪和需求；活动选择、进度、中断与恢复；模拟执行器与叙事事实；群聊仲裁和表达阀门；事件日志、SQLite 与重启恢复；基础关系档案与重要记忆召回；DeepSeek 结构化事件评价与回复生成。

优先完成少量活动的完整生命周期，不以活动名称数量作为完成标准。第一版不宣称具备真实视频观看、游戏控制或完整真实环境 Agent 能力。

后续扩展：真实视频理解、游戏观察与控制、网页和外部工具、复杂长期人格成长、高级记忆巩固与遗忘、多角色、多进程执行器。扩展必须沿用事件协议、来源标记、统一结算和权限边界。

## 11. 技术栈

| 层面 | 确定选型 |
| --- | --- |
| 平台接入 | AstrBot 插件 |
| 语言与调度 | Python 3.10+、`asyncio` |
| 状态与数据访问 | SQLite、`aiosqlite` |
| 数据校验 | Pydantic |
| 模型 | DeepSeek API |
| 检索 | SQLite FTS，可选向量检索 |
| 配置 | AstrBot 配置与 YAML |
| 日志 | Python logging / JSON 日志 |
| 真实活动扩展 | 独立 `ActivityExecutor` 插件 |

第一版不引入 Redis、Celery、独立向量服务、多 Agent 框架或复杂工作流引擎。具体依赖版本、AstrBot 接口适配及模型参数需在实现时验证；本设计文档不提供未经验证的安装命令或兼容版本声明。

当前代码最低要求 Python 3.10，使用 `X | Y` 联合类型等语法。PyCharm 的代码兼容性检查目标应与此一致；该语言最低要求不代表已验证所有 Python 版本或 AstrBot 版本的完整兼容性。

## 12. 权限、资源与一致性约束

1. 业务模块不得直接改写其他模块状态；规则与模型均通过提案进入统一结算。
2. 事件必须可追溯且可去重，状态以已提交结果为准；同一次收益不得被活动、评价和反思重复计算。
3. LLM 调用不得阻塞整个 Agent；结构化输出需校验，并提供超时、重试和回退处理。迟到结果须结合当前状态重新校验。
4. SQLite 事务只保证本地一致性，不能与群消息发送或游戏控制形成跨系统原子事务。外部操作分别记录请求、结果和失败，通过 outbox 等机制管理；不得承诺外部操作恰好执行一次。
5. 群聊文本、模型输出和工具参数不能绕过权限检查。键鼠、文件、账号等外部操作必须受能力和权限限制。
6. 活动、暂停上下文、未完成承诺和待处理事件不能仅保存在内存。恢复必须尊重执行器能力及外部实际状态。
7. 社交预算、调用预算、限流和活动资源控制独立于人格偏好；活动结束不自动触发发言。
8. 管理员应能查看状态与决策原因，暂停主动发言、禁用活动、处理异常状态和管理指定记忆。日志记录事件关联、仲裁原因、调用消耗和失败，避免泄露敏感内容。

## 13. 开发路线

以下阶段是落实本基线的工作顺序，不是已完成里程碑。

| 阶段 | 工作内容 | 验收重点 |
| --- | --- | --- |
| 1. 接口与数据 | 定义六个核心对象、能力声明、错误语义、表结构与版本迁移 | 来源、身份、作用域、因果和状态所有权明确 |
| 2. 内核闭环 | 实现队列、路由、调度、结算、存储与启动恢复 | 重复事件不重复结算；异常不产生部分状态提交 |
| 3. 模拟活动 | 接入人格、需求、情绪与少量模拟活动 | 选择、推进、中断、恢复、完成形成闭环 |
| 4. 社交认知 | 接入 AstrBot、DeepSeek、话题、仲裁、表达与承诺 | 发言受预算控制；沉默可解释；模型异常可回退 |
| 5. 关系与记忆 | 重要记忆召回、基础关系、来源追溯与权限过滤 | 私聊不泄露到群聊；叙事不覆盖事实 |
| 6. v1 验证 | 重启、超时、重复消息、发送失败与活动恢复测试 | 核心状态连续，外部结果可追踪，运行开销可观测 |
| 7. 后续扩展 | 按需增加真实或混合执行器与高级记忆能力 | 不改变内核职责，不混淆执行来源 |

架构与模块职责以本文为基线。情绪衰减、需求速率、活动效用权重、发言频率等通过配置与实际群聊测试调整；具体数据字段和接口细节在第一阶段补齐，再进入实现。



