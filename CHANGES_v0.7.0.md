# v0.7.0：基础用户关系档案

角色开始按用户保存熟悉度和互动倾向，回复时能参考与当前用户的相处历史。第一版使用实际入站和已有聊天分类，更新保守、可解释，不新增模型请求。v0.6.2 测试已由用户确认通过；本版测试已编写，尚未运行。

## 数据及更新规则

档案以 `(agent_id, platform_id, user_id)` 为身份，平台实例不同或角色不同就分开；同一用户跨会话使用同一关系数值、冷却和预算。保存熟悉度 0..100、互动倾向 -100..100、最近互动时间、冷却截止、预算日、已用预算和独立 revision。初始数值为 0；不从历史消息猜初始关系，不把关系写进 AgentState。

| 默认参数 | 数值 |
| --- | --- |
| enabled / private_only | true / true |
| 独立冷却 | 300 秒 |
| 来源有效期 | 300 秒 |
| 每次熟悉度增量 | +0.2 |
| 正向 / 负向互动倾向 | +0.1 / -0.2 |
| 每日熟悉度上限 | 累计 +2 |
| 每日倾向上限 | 正负变化的绝对值合计 1 |
| 预算时区 | UTC+8（utc_offset_minutes=480） |

只有实际 observed SOCIAL.message_received、新来源、明确平台/用户/会话且非机器人自身消息参与。私聊属于直接互动；群聊默认关闭，显式关闭 private_only 后仍要求消息指向角色。空消息、斜杠指令、身份不完整、过期或已在启用前记录的历史事件只保留忽略回执。晚于当前档案的较旧来源不回写关系；同一事件重投、重启及配置更改保留首条回执，不重复累计。

熟悉度来自有效互动事实。倾向复用本次已执行的聊天评价规则 ID：默认 thanks/praise/apology/invitation/friendly_emoji 为正，insult 为负；仅 matched_phrase 参与。含糊、引用、混合信号、无匹配及未知自定义规则保持中性，不从 VAD 数值推断关系，也不重新调用评价器。正负规则映射可配置且不可重叠。复用的是效果冷却和情绪初始化处理之前的规则分类，因此未初始化情绪不会让明确友好证据消失；关系另有自己的冷却。评价关闭时仍可积累真实互动的熟悉度，倾向保持不变。

日预算按实际处理时间和配置时区懒更新，跨天收到新互动才重置，不补发离线期间收益，不因静默自动衰减。预算日只向前推进；正负互相抵消也会消耗绝对变化预算。数值还受最终边界限制。冷却期间最近互动时间仍可更新，档案修订可增加，指标增量为零；字段完全不变时不重写档案。当前持久化冷却截止会在重载后继续生效。

## 原子性和诊断

消息、时间审计、既有聊天评价、关系档案和关系回执共用 PersonaStore 的单个事务；任一写入失败整体回滚，重试可以继续。关系记录不生成需求/情绪提案，不增加角色状态版本，不触发自主选择或主动发言。用户关系有自己的 revision，不能与 AgentState.version 混淆。

回执只存来源 ID、当前 scope、规则 ID/方向、原因、前后数值及档案修订，不复制聊天正文。show 默认显示当前用户档案及本会话最近一条原因；history 显示本会话最近十条。即使未生成档案，也能通过回执说明忽略原因。查询均按索引有界读取，不扫描历史事件；查询/预览本身不算互动。

```text
/persona_relationship
/persona_relationship show
/persona_relationship history
/persona_context now
/persona_context last
```

保留原管理员和 QQ 绑定门禁，只查询当前事件携带的用户身份，不接受任意用户 ID 参数。当前默认私聊限制适用于更新、诊断和聊天注入。关系数值可以跨同用户的会话使用；变化历史只限原 scope，原私聊记忆和消息内容没有扩大可见范围。

## 聊天上下文

main 显式传入当前平台和用户身份；只用 speaker_id 字符串而缺少独立身份参数时不读取关系。读取前核对身份，提示边界再核对角色、当前 speaker 和时间，避免旧请求或其他用户混入。

提示仅包含当前用户身份、熟悉度/倾向及其量程、最近互动时间，并标注 basis=interaction_estimate；固定约束说明关系不授予权限。没有把礼貌、亲近程度当成真实信任或承诺，关系也不会替代原有人设。

保持默认总预算 1000 字符、记忆部分 600 字符、最小总预算 500 字符。关系块为可选项：预算紧张时先裁掉它，再按原规则处理记忆；实际 relationship_actor_id 引用同步清空，预览不会声称注入了已裁掉内容。原需求、情绪和活动状态仍保留；没有额外模型调用。

## 按文件笔记

| 文件 | 新增或修改 |
| --- | --- |
| `social/__init__.py`（新增） | 实际关系业务包，无未来空占位模块 |
| `social/models.py`（新增） | 严格配置、关系信号、档案、变化回执；量程、身份、时间和预算日期校验 |
| `social/relationships.py`（新增） | 纯更新函数：冷却、跨天预算、绝对变化限幅、边界和旧消息规则 |
| `social/persistence.py`（新增） | 每角色/平台/用户存取、按来源去重、乐观修订、当前 scope 的索引历史；不提交角色效果 |
| `application/relationship.py`（新增） | 复用原聊天分类映射信号，中文诊断与实际增量展示 |
| `application/persistence.py` | 在聊天事务内接入关系；旧事件不补算；只读关系视图及聊天身份参数 |
| `application/host.py` | 注入关系配置；当前用户诊断、功能/私聊门禁、聊天只读接线 |
| `application/chat_context.py` | 显式身份一致时读取关系档案，生成独立 ContextRelationship DTO；不写入或更新关系 |
| `cognition/prompt_builder.py` | 当前用户估计与量程、时间/身份过滤、预算裁剪、实际关系引用及固定表达约束 |
| `application/settings.py`、`_conf_schema.json` | relationships 严格配置及默认参数、规则映射和使用提示 |
| `main.py` | QQ 请求传递身份；新增管理员 persona_relationship，保留绑定范围检查及帮助 |
| `infrastructure/storage.py` | 原子 schema v6→v7：两张关系表、历史索引、原始列白名单；适配器不导入关系业务 |
| `tests/test_relationships.py`（新增） | 严格模型/规则、限幅/冷却/跨天/旧消息、隔离、去重/重启、无额外结算、原子回滚、有界无正文历史、v6 升级与失败重试；未运行 |
| `tests/test_relationship_context.py`（新增） | 身份/时间/开关/范围过滤、量程、提示预算、请求切换、宿主诊断与重启；未运行 |
| `tests/test_astrbot_host.py` | 官方 API 替身下关系管理员/绑定门禁及命令入口；未运行 |
| `tests/schema_fixtures.py` | 新增 v6 夹具；构造 v5/v4/v3 旧结构前清理 v7 关系表，防止假旧库迁移碰撞 |
| 既有存储/调度/活动/聊天/记忆测试 | 当前成功升级断言为 v7，未知版本从 v7 改为 v8；旧迁移失败后的原版本断言保持；未运行 |
| `metadata.yaml`、`README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md`、`PACKAGING_FILES_v0.7.0.txt` | 版本、结构、使用、回归和完整文件清单 |

social 纯模型/规则不导入应用、评价、活动或数据库实现。跨业务分类在 application；cognition 使用独立 DTO，不导入 social。没有增加第三方依赖。

## 升级和本地验证

数据库变为 **schema v7、十四张表**：新表为 relationships 与 relationship_receipts，旧表及 JSON 不做关系回填，记忆词项仍为 2。v1..v6 按原迁移链到 v7，全部结构变更在同一写事务中；失败回滚原版本和新增结构。关系 JSON 损坏不在启动时全表解析，异常限定在被读取档案；原聊天钩子沿用失败时保留 AstrBot 原请求的处理。

更新前停插件备份数据。v0.6.x 不能直接打开 v7，回退需要同时恢复升级前数据库。本版未实际运行数据库迁移。

```powershell
python -m unittest discover -s tests -p "test_relationships.py" -v
python -m unittest discover -s tests -p "test_relationship_context.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

新增用例仅使用固定时间和临时数据库，不访问真实 QQ 或模型。静态检查通过：97 个 Python 文件的 3.10 语法/编译、相对导入、分层、配置默认值、十四张表与列白名单、提示最小预算、文档链接和 85 项打包清单。实现者未运行业务导入、实际数据库、迁移、测试或打包。当前版本 **v0.7.0**，按 [PACKAGING_FILES_v0.7.0.txt](PACKAGING_FILES_v0.7.0.txt) 自行全量打包，保留目录结构；tests、缓存、数据库和旧打包清单不进入安装包。
