# v0.5.0：会话记忆与近期归并第一版

当前绑定 QQ 私聊可以自动保存明确的第一人称计划、偏好，在后续聊天中按类型、日期和关键词召回；管理员可查看、新增、检索、查看修订及删除。复用 SQLite 和现有聊天请求，没有独立模型调用，没有向量数据库或新增依赖。附加记忆仍会占用现有模型请求的输入 token。

## 本轮按文件笔记

| 文件 | 修改内容 |
| --- | --- |
| `memory/__init__.py` | 建立有实际实现的记忆包；沿用 namespace 加载方式 |
| `memory/models.py` | `MemoryDraft`、`MemoryRecord`、写入结果和配置。区分陈述时间、有效区间、目标日期区间、计划状态；严格校验来源字段、时间、长度及重要度 |
| `memory/extraction.py` | 无模型调用的有限提取：明确第一人称计划/偏好、日期、否定与更正。人物身份绑定平台实例与发送者；第三人称/引用/假设/问题不自动记为本人事实。手动正文不能结构化时保存为普通 fact |
| `memory/retrieval.py` | 中文双字/英文词辅助检索、计划/偏好等查询意图、保守归并。候选先按来源时间倒序；时间近只是排序依据，关系不明确时新建 |
| `memory/persistence.py` | 事务内保存、来源关联、修订、去重回执、有限查询与物理删除；记忆修订独立于角色状态版本。索引过滤后 LIMIT，查询不解析全部事件历史 |
| `application/memory.py` | 管理结果和目标日期显示；列表分页、当前来源及修订历史 |
| `application/settings.py`、`_conf_schema.json` | 增加严格的 `memory` 配置及界面默认值 |
| `application/persistence.py` | QQ 原事件、记忆及原有规则评价原子保存；增加管理和结构化草稿事务入口；原消息重投复用记忆处理回执 |
| `application/host.py` | 注入记忆设置、管理入口、带查询文字/人物的上下文读取；抽出原有消息去重与未来时间钳制协作者。删除后清除该会话的最近请求预览 |
| `application/chat_context.py` | 同一只读事务召回当前会话记忆，附陈述时间、目标日期和状态，按配置时区显示相对日期依据 |
| `cognition/prompt_builder.py` | 独立的上下文记忆 DTO、记忆引用及来源 ID；记忆作为数据块，受独立与总字符预算约束；提示取消/完成不作为待办，保留来源区别 |
| `main.py` | 现有模型请求传入查询文字及调用者身份；增加管理员 `/persona_memory`，仅操作绑定范围，保留包含空格的整段正文 |
| `persistence/ports.py` | 通用端口增加倒序、上/下界、游标前界与带条件删除；没有引入业务 SQL 到存储接口 |
| `infrastructure/storage.py` | schema v4→v5 原子迁移，新增五张记忆表及索引；通用有限查询和删除实现，表列仍使用白名单 |
| `tests/test_memory.py` | 用户示例、时间邻近、日期跨日、独立安排、人物/范围、补充/更正/取消、偏好维度、重复/重载/删除、事务回滚、查询边界、提示预算和 v4 迁移；未运行 |
| `tests/test_astrbot_host.py` | QQ 管理门禁、正文空格、补充、已有请求召回、删除及预览清空；未运行 |
| `tests/schema_fixtures.py` | 增加临时测试库 v4 夹具，原 v3 夹具先剥离 v5 表；仅供测试，不是生产降级工具 |
| `tests/test_activity_storage.py`、`tests/test_scheduler.py`、`tests/test_local_evaluation.py`、`tests/test_chat_context.py`、`tests/test_storage.py` | 更新最终 schema v5 与未知版本 v6 的预期；未运行 |
| `tests/test_activity_rewards.py` | 修正旧用例直接写入完成快照时遗漏 status 索引的问题；独立验证“索引损坏”和“完成证据缺失”均拒绝发放收益；未运行 |
| `tests/test_persistence_layers.py` | 内存适配器跟进通用查询/删除协议；未运行 |
| `README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md`、`metadata.yaml`、`PACKAGING_FILES_v0.5.0.txt` | 更新实际结构、版本、说明与全量打包清单 |

本轮在需要时建立了实际工作的 memory 模块，没有恢复旧的空 manager、consolidation 或 story_facts 文件。

## 近期归并规则

记录中的 occurred_at 绑定当前正文的来源时间；created_at/updated_at 分别保存首条/最近来源时间，用于来源顺序，不代表另一个落库墙钟。去重确认不会改写旧正文的相对日期依据。

默认查同角色、同 scope、同主体、同类型/来源的最近 **1 小时、最多 20 条**候选；计划普通更新还需目标日期相同，显式改期可以跨日期寻找候选。查询先按索引和范围过滤，再限制数量，优先检查最近记录。

- “我明天会出门” → “我明天去逛街看电影”：近期目标日期内只有一个明确外出候选时，细化原外出计划，沿用记忆 ID，新增修订和来源。
- 丰富计划后再说“我明天出门”：保留已有细节，追加陈述来源；原文的陈述时间仍绑定原来源，避免跨午夜误解释“明天”。
- “改成后天去逛街看电影”：可对应同一具体安排，更新目标日期；只有一个明确近期候选时，也允许显式更改行动。
- “我后天不出门了”：只有明确匹配或唯一候选时标记取消；取消、完成均保留历史。
- 两条不同的具体安排（例如逛街和去医院）、不同人物或不同目标日期，不因新旧顺序直接覆盖。
- 偏好的“喜欢”和“是否吃”分属不同维度；明确“现在/其实/以后”的同维度变化可以更正。重复陈述不累加重要度。

不确定关系保留独立记录。默认窗口之外的相似内容可能并存，时间邻近不是可靠的通用语义理解。没有自动处理任意改写、复杂部分取消、多事件拆分、指代消解或跨会话人物合并。

## 提取与检索范围

自动提取当前支持明确第一人称、单段陈述，例如 `我明天会出门`、`我明天去逛街看电影`、`我不吃香菜`。支持今天/明天/后天/大后天、明确年月日、下周具体星期。第三人称、疑问、引用、假设、明显不确定表达保守跳过；较长或复杂陈述可以通过 add 明确保存。

日期以来源事件时间和 `utc_offset_minutes` 为准。默认固定 UTC+8；当前不处理夏令时地区的时区规则。“明天”保存为具体本地日的 UTC 起止区间，结束边界不包含；时间过去不会自动认定计划已完成。

“我的未来计划是什么”通过 plan 类型、人物和目标日期过滤，因此不要求正文包含“计划”。普通未来查询也会带上相关取消/完成状态供模型解释，不能把它们再说成待办。指定明确日期可查该日期的计划；无明确日期时只选择未过目标日及未定日期的计划。其他内容走有限关键词索引；同义词或隐含联系仍可能漏召回。

每个来源事件有处理回执，包括被跳过的普通消息，以保证重投、配置调整或删除后不会重新提取旧消息。不开启历史消息批量重提取。记忆回执不修改角色版本；普通聊天原有规则评价和后台时钟仍可改变状态版本。

## 配置

`memory` 默认值：

```json
{
  "enabled": true,
  "auto_capture": true,
  "private_only": true,
  "merge_window_seconds": 3600,
  "candidate_limit": 20,
  "recall_limit": 3,
  "max_context_chars": 600,
  "utc_offset_minutes": 480
}
```

召回上限为 3 条，并不保证每次都放入 3 条。默认记忆 JSON 独立预算 600 字符，仍受 `chat_context.max_chars=1000` 总预算约束；较长条目可能被略过。`now` 用近期记忆预览，`last` 记录实际问题的召回及最终预算结果。配置保存后重载；关闭 auto_capture 后仍可手动 add，关闭 enabled 后不召回也不开放管理操作。

首版只开放 scope 记忆。草稿结构保留 self/global 枚举以校验输入，但提交层拒绝扩大可见范围；模型不能自行决定公开、改来源或升级 observed 为 confirmed。事实以“用户陈述”身份使用，模拟来源保持模拟，不因为进入记忆就变成真实经历。

## QQ 测试步骤

先绑定当前 QQ 私聊，保留已有 AstrBot AI 回复设置；以下管理命令要求管理员。

1. 发送 `我明天会出门`，然后 `/persona_memory list`，应有一条 plan。
2. 发送 `我明天去逛街看电影`，再次 list，应沿用同一个 ID，修订变成 2。
3. `/persona_memory history 记忆ID`：看到 created/enriched 两次来源和内容。
4. 发送 `我的未来计划是什么`，等回复后 `/persona_context last`，应包含该记忆 ID、具体目标日期和丰富后的内容。
5. `/persona_memory search 我的未来计划是什么`：只做本地检索，不调用模型。
6. `/persona_memory add 我不吃香菜`，再问 `这道菜能放香菜吗`，观察召回及回复。
7. 重载后 list/history 仍可用；另一个 scope 看不到这些记忆。
8. `/persona_memory delete 记忆ID`，之后 list/search 不再返回；最近上下文预览清空。删除后旧消息重投不会重建该记忆。

可用命令：

```text
/persona_memory add 内容
/persona_memory list [上一页最后一条记忆ID]
/persona_memory show 记忆ID
/persona_memory history 记忆ID
/persona_memory search 问题
/persona_memory delete 记忆ID
```

add 无法结构化识别的正文仍会保存为普通 fact，不假造日期或人物。list 每页 10 条；history 默认显示最近 10 个修订。来源事件、当前正文和修订可追溯，但不会触发第二次情绪或需求结算。

命令匹配按 [AstrBot v4.29.3 命令过滤器源码](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.3/astrbot/core/star/filter/command.py)核对；正文在命令匹配后由入口完整读取。官方 API 替身不能替代真实 QQ 验证。

## 删除和升级

删除会在同一事务移除当前正文、修订正文、来源关联和检索索引，保留不含正文的请求摘要/结果回执，防止旧消息复活。原始 QQ 事件仍属于现有事件日志；AstrBot 聊天历史也仍由框架管理。删除不会清空这些来源记录或撤回已经发出的模型请求。

数据库升级为 **schema v5，共十二张表**。新增 `memories`、`memory_revisions`、`memory_sources`、`memory_terms`、`memory_receipts`。旧记忆占位没有历史数据，本次不扫描旧 QQ 日志补建记忆。旧状态、活动、反馈指针、评价与调度数据沿用。

更新前停用插件并备份完整数据目录；旧 v0.4.0 不支持打开 v5，回退代码时需恢复升级前数据。没有读取或改动用户当前实际插件数据库。

版本为 **v0.5.0**，按 [PACKAGING_FILES_v0.5.0.txt](PACKAGING_FILES_v0.5.0.txt) 全量覆盖源码并保留目录。未生成 ZIP，依赖不变。

## 用户回归反馈修订：旧完成证据夹具

用户全量运行报告 351 项、1 项错误：`test_completed_label_without_commit_evidence_is_rejected`。原因是该旧夹具直接改写 revision/instance_json，却没有维护 v4 起新增的 status 列，提前触发 `StorageError`。

夹具现同步写入完成状态索引，并先确认快照可正常读取，再验证缺少 activity_commits 仍抛出 `InvalidCompletionEventError`。另新增索引单独损坏的拒绝用例。未修改业务校验、异常类型、数据库或安装包运行文件；版本保持 v0.5.0。实现者未运行测试，等待用户复测。

## 用户运行测试

实现者只做静态检查，没有执行单元测试或真实 QQ 测试。

```powershell
python -m unittest discover -s tests -p "test_activity_rewards.py" -v
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```
