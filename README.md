# Persona Agent

基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的人格化 Agent 插件。角色拥有持续变化的需求与情绪、自己的模拟生活、对每个用户的互动记忆与关系估计，并能在明确授权后主动开口。

- 目标环境：AstrBot **v4.29.3**、Python 3.10+
- 存储：SQLite，**schema v9，16 张表**，首次启动自动迁移
- 依赖：`pydantic`、`aiosqlite`（见 `requirements.txt`）
- 插件标识：`astrbot_plugin_persona`

本文件只描述当前版本的能力与边界。逐版本变更记录与打包清单保存在本地（`CHANGES_*.md`、`PACKAGING_FILES_*.txt`），不进版本库。

---

## 1. 它现在能做什么

| 能力 | 说明 |
| --- | --- |
| 持续状态 | 四项需求（精力/饱食/无聊/社交）与 VAD 情绪随真实时间演化，独立时间游标，懒更新 |
| 模拟生活 | 需求驱动的活动选择、行为仲裁、推进、暂停、恢复、完成收益；当前只实现 `simulated` 执行器 |
| 事件评价 | 规则评价（短语/否定/歧义）为默认；可选接入调用方已有的模型输出做结构化体验评价 |
| 会话记忆 | 自动提取明确的第一人称计划与偏好，归并、召回、展示去重；活动阶段/完成/失败写入经历记忆 |
| 基础关系 | 按平台+用户维护熟悉度与互动倾向，含冷却、每日预算、凌晨日回归 |
| 会话状态 | 维护会话活跃、待回复与免唤醒续聊窗口，参与行为仲裁的占用判断 |
| 主动表达 | 意图状态机 + 显式目标白名单；**默认只预览、默认不发送** |
| QQ 接入 | 私聊/群白名单、稳定消息 ID、平台时间钳制与审计、管理员诊断命令 |
| 聊天上下文 | 把需求、情绪、活动、近期反馈、记忆、关系装配成有字符预算的数据块，注入 AstrBot 既有请求 |

**明确不包含**：真实视频/游戏执行器、向量检索、语义记忆巩固、话题追踪与承诺管理、多角色、独立模型服务。

---

## 2. 快速开始

1. 把整个目录放进 AstrBot 的插件目录（`main.py` 位于插件根目录）。
2. 安装依赖（AstrBot 自带环境通常已有 `pydantic`）：

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. 在 AstrBot 管理面板填写配置，至少绑定要接收的会话：

   ```json
   {
     "qq_private_ids": ["你的QQ号"],
     "qq_platform_ids": ["LittleFish"]
   }
   ```

4. 保存并重载插件，在绑定私聊发送 `/persona_scope` 确认标识，再发 `/persona_status` 看状态。

群聊默认完全静默：只有 `qq_group_ids` 里列出的群会被记录，且只有 @/唤醒词/引用的消息会被当作"指向角色"。

---

## 3. 一条消息经过什么

```text
QQ 消息
  → application/qq.py        稳定事件 ID、平台时间；未来时间钳制到接收时刻并写审计事件
  → 同一事务                 入站事件 + 记忆提取 + 规则评价 + 关系更新
  → core/runtime.py          单角色队列 → 事件路由 → 状态变更提案
  → core/settlement.py       版本校验、去重、限幅 → SQLite 提交
  → on_llm_request           只读投影当前状态 + 召回记忆 + 关系 + 出站引用 → 注入请求
  → AstrBot 生成并发送回复
```

回复由 AstrBot 自己完成，本插件不重复调用模型、不产生第二条回复。上下文注入失败时保留原请求。

---

## 4. 后台时钟

全部由 `core/scheduler.py` 的**持久化单次计划**驱动，重启后继续；不使用常驻循环，也不做周期空结算。

| 计划 | 默认间隔 | 作用 |
| --- | --- | --- |
| 需求时钟 | 60 秒 | 推进需求并写时间游标 |
| 情绪时钟 | 60 秒 | VAD 向心情、心情向基线回归 |
| 活动推进 | 5 秒 | 模拟活动按预计时长推进，末次缩短至剩余时间 |
| 关系日回归 | 每日 04:00（UTC+8） | 互动倾向：负值归零、正向回落到 0.5 基线 |
| 会话/表达到期 | 单次 | 续聊窗口与表达意图到期后各自收敛 |

---

## 5. 目录与分层

```text
persona_agent/
├── main.py                    AstrBot 入口、QQ 入站、请求钩子、管理命令
├── application/               宿主组装与跨业务事务协调
│   ├── host.py                生命周期、后台任务、受控操作
│   ├── persistence.py         事务门面（PersonaStore）
│   ├── autonomy.py            自主选择与仲裁编排
│   ├── evaluation.py          聊天/活动事实 → 评价输入
│   ├── evaluation_persistence.py 评价去重、冷却、时效、结果提交
│   ├── appraisal_persistence.py  延后评价预约与截止
│   ├── chat_context.py        只读快照与提示装配
│   ├── activity_memory.py     活动事实 → 经历记忆
│   ├── conversation.py        会话状态适配
│   ├── expression.py          表达预留与阶段审计
│   ├── astrbot_expression.py  AstrBot SDK 边界（生成与发送）
│   └── relationship.py        规则 → 关系信号、诊断文本
├── core/                      内核：事件、状态、结算、队列、路由、调度、仲裁
├── activity/                  活动定义、选择、执行器、推进、反馈、收益
├── persona/                   需求与情绪的纯计算
├── cognition/                 无模型调用的评价与提示装配
├── memory/                    记忆提取、评分、召回、持久化
├── social/                    关系、会话、表达的纯规则与持久化
├── persistence/               与业务无关的存储契约（端口、错误、序列化）
├── infrastructure/storage.py  SQLite 适配与结构迁移
└── tests/                     本地测试与旧结构夹具（不进安装包）
```

分层规则：

- `core`、`persona`、`cognition`、`social` 的纯计算部分**不导入**数据库与 AstrBot；
- `memory` 不导入 `activity`，跨业务适配放在 `application`；
- 业务模块不直接写 SQL，SQL 只在 `infrastructure/storage.py`；
- 宿主状态只由统一结算提交，业务模块产出提案。

---

## 6. 数据模型（schema v9，16 张表）

| 分组 | 表 | 用途 |
| --- | --- | --- |
| 状态与事件 | `agent_states`、`agent_events`、`state_settlements` | 角色快照、事件日志、提案结算回执 |
| 活动 | `activity_instances`、`activity_commits` | 活动实例与逐修订提交记录 |
| 调度 | `scheduled_events` | 单次计划、重试次数、下次尝试时间 |
| 上下文指针 | `feedback_heads` | 每个会话最近一条内容反馈 |
| 记忆 | `memories`、`memory_revisions`、`memory_sources`、`memory_terms`、`memory_receipts` | 正文、修订、来源、检索词、幂等回执 |
| 关系 | `relationships`、`relationship_receipts` | 档案与变化回执 |
| 会话 | `conversations` | 活跃、待回复、续聊窗口与出站引用 |
| 表达 | `expression_heads` | 每角色一条预算游标与当前意图 |

所有角色数据带 `agent_id`；升级前请停止插件并备份整个数据目录，旧版本不能直接打开新库。

---

## 7. 配置

`_conf_schema.json` 是唯一配置入口，字段与默认值以它为准。顶层分组：

| 分组 | 作用 |
| --- | --- |
| `agent_id` | 角色 ID，修改等于换一个角色，不迁移旧状态 |
| `autonomous_enabled`、`disabled_activities` | 自主启动总开关、禁用活动 |
| `qq_private_ids`、`qq_group_ids`、`qq_platform_ids` | 接收范围白名单 |
| `simulation` | 初始需求、需求速率、时钟间隔、初始情绪、活动目录、选择策略 |
| `evaluation` | 规则评价、活动效果、可选语义评价、心理需求映射 |
| `chat_context` | 注入请求的数据块开关、总字符预算、反馈时效 |
| `memory` | 记忆提取、归并窗口、召回条数与预算、半衰期 |
| `relationships` | 熟悉度/倾向步长、每日上限、日回归参数 |
| `conversation` | 续聊窗口时长、最大次数、结束短句 |
| `expression` | 目标白名单、阈值、冷却、日预算、安静时段、超时与正文字数 |

几个需要特别注意的默认值：

- `expression.send_enabled = false`：只记录"准备对谁开口"，不生成、不发送；
- `evaluation.semantic.enabled = false`：结构化体验评价默认关闭，开启后需要调用方在已有模型调用里附带输出协议；
- `conversation.private_only = true`、`memory.private_only = true`、`relationships.private_only = true`：三项默认都只作用于私聊。

---

## 8. 管理命令

全部需要管理员，且只在已绑定会话可用；不接收任意用户 ID 参数。

| 命令 | 作用 |
| --- | --- |
| `/persona_scope` | 查看当前会话的平台、会话、群、发送者标识 |
| `/persona_status` | 需求、情绪、活动、调度与降级提示 |
| `/persona_decision` | 最近一次行为仲裁结果与原因 |
| `/persona_evaluation [事件ID]` | 评价回执、等待状态与有效冲击 |
| `/persona_context [now\|last]` | 预览拟注入内容 / 上次实际注入内容 |
| `/persona_memory add/list/show/history/search/delete/pending` | 记忆管理 |
| `/persona_relationship [show\|history]` | 当前用户的关系档案与变化 |
| `/persona_conversation` | 会话活跃、待回复与续聊窗口 |
| `/persona_expression [show\|cancel\|pause\|resume]` | 主动表达诊断与控制 |
| `/persona_pause`、`/persona_resume` | 暂停/恢复自主启动 |
| `/persona_disable`、`/persona_enable` | 禁用/启用某个活动定义 |
| `/persona_activity pause\|resume\|stop` | 操作当前活动实例 |
| `/persona_help` | 命令列表 |

---

## 9. 一致性与失败边界

- **事件幂等**：事件 ID 由生产方稳定生成；同一事件重复投递不会重复结算，重试沿用原提案。
- **单一写入者**：状态变更必须经提案与统一结算，业务模块不直接改状态。
- **时钟分离**：需求/情绪的回归与活动推进各用自己的游标，互不推进对方。
- **迟到结果**：超过时效的评价只归档，不再改变情绪、需求或记忆时间。
- **发送语义**：主动表达是 **at-most-once**。`sending` 之后崩溃记为 `unknown`，不自动重试；平台返回不等于 QQ 送达。
- **坏数据降级**：记忆索引单条损坏时跳过该行、保留数据、下次启动重试，不阻断插件启动；可用 `/persona_memory pending` 定位。
- **迁移**：结构升级与索引重建是不同事务；升级失败回滚原版本。

---

## 10. 隐私

- 私聊信息默认不进入群聊：记忆带 `visibility` 与 `scope_id`，检索在 SQL 层先按会话过滤；反馈摘要按来源会话隔离。
- 关系按"角色 + 平台实例 + 用户"隔离，历史回执只显示当前会话。
- 主动表达只发往 `expression.targets` 显式列出的目标，且必须已绑定私聊。
- 诊断命令只查询当前事件携带的身份，不接受任意用户 ID。
- 注入的状态块明确标注"这是数据不是指令"，关系与反馈都标注为估计值。

---

## 11. 测试

测试使用固定时间和临时数据库，不访问 QQ 与模型，也不改动实际插件数据目录。

```powershell
python -m unittest discover -s tests -v
```

按模块单独运行（更快定位）：

```powershell
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_relationships.py" -v
python -m unittest discover -s tests -p "test_conversation.py" -v
python -m unittest discover -s tests -p "test_expression.py" -v
```

`tests/schema_fixtures.py` 只用于重建临时旧库，不是生产降级工具。

---

## 12. 已知限制

- 只实现 `simulated` 执行器；真实/混合模式保留接口，未接入。
- 记忆提取只覆盖明确的第一人称计划与偏好，第三人称事实、复杂改写、跨会话人物合并均未实现。
- 检索是有限关键词匹配（中文双字 + 英文词），不是语义检索。
- 关系是互动估计，不代表信任、承诺或权限；熟悉度不衰减。
- 主动表达依赖调用方配置与目标现有会话；目标无 AstrBot 对话或模型关闭时直接失败。
- 群聊默认不接收、不注入、不主动发言。

---

## 13. 技术栈

| 层面 | 选型 |
| --- | --- |
| 平台接入 | AstrBot 插件（v4.29.3） |
| 语言与调度 | Python 3.10+、`asyncio` |
| 存储 | SQLite + `aiosqlite` |
| 数据校验 | Pydantic v2 |
| 模型 | 复用 AstrBot 已配置的聊天模型，插件自身不追加调用 |
