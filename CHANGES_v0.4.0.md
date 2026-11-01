# v0.4.0：现有聊天接入状态与项目结构清理

本版把角色的需求、情绪、活动和当前会话近期反馈接入 AstrBot 已有聊天请求。不额外请求模型，也不自动外发消息；原有人设、提问、历史、工具和供应商配置继续由 AstrBot 管理。增加输入文本会增加本次请求的输入 token，字符预算不是精确 token 预算。

## 按文件笔记

| 文件 | 新增或修改内容 |
| --- | --- |
| `main.py` | 增加 `on_llm_request` 钩子；只处理绑定范围，默认私聊，群聊需显式启用且明确唤醒。跳过已匹配命令的请求；增加管理员 `/persona_context now`、`last`。上下文读取失败时继续原请求 |
| `cognition/prompt_builder.py` | 原占位改为严格的上下文配置/快照模型和纯装配函数。固定系统约束与动态 JSON 数据分离；限制活动数、摘要时效及总字符数，裁剪后仍保留有效 JSON |
| `application/chat_context.py`（新增） | 在单次事务读取状态；按当前时间纯投影需求和情绪，不写版本、事件或时钟；读取活跃活动和当前会话反馈指针；装配时只替换本插件的数据块 |
| `application/host.py` | 新增只读 `chat_context()`；最近实际注入内容按会话缓存，最多 64 个会话，关闭/重载后清空；阶段反馈入口增加可选 `scope_id` |
| `application/settings.py` | 在严格宿主配置中加入 `ChatContextSettings` |
| `_conf_schema.json` | 增加对应 `chat_context` 默认配置和使用提示 |
| `activity/feedback.py` | `build_feedback_event()` 接受可选会话范围，并保留在事件元数据中；默认 `None` 不改变旧事件格式。重投同 checkpoint 仍必须保持内容与范围一致 |
| `application/persistence.py` | 增加只读快照事务入口；有明确会话归属的内容反馈和反馈指针原子保存，即使评价关闭也能记录指针；启动时调用旧活动状态索引回填 |
| `activity/persistence.py` | 活动创建/修订同步维护状态列；加载时核对列与 JSON 一致；活跃查询使用状态索引，旧活动仅回填索引、不改实例 JSON/修订/角色版本 |
| `infrastructure/storage.py` | 数据库 v3 升至 v4：活动状态列及查询索引、新增 `feedback_heads` 会话指针表。结构升级在事务中完成，失败回滚 |
| `tests/test_chat_context.py`（新增） | 覆盖字符预算、来源与范围过滤、只读投影、指针原子回滚、重载、避免历史扫描、v3 升级及迁移失败恢复；未执行 |
| `tests/test_astrbot_host.py` | 为官方请求钩子补替身；覆盖已有请求保留、重复装配、命令/绑定/管理员门禁、群聊 opt-in、失败回退和开关；未执行 |
| `tests/schema_fixtures.py`（新增） | 仅供临时测试库重建真实旧结构，不能作为生产降级入口 |
| `tests/test_activity_storage.py`、`tests/test_scheduler.py` | 旧版迁移夹具改用真实旧结构，预期最终版本改为 v4；未执行 |
| `tests/test_storage.py`、`tests/test_local_evaluation.py` | 更新数据库版本相关预期；未执行 |
| `activity/__init__.py`、`activity/executors/__init__.py`、`core/__init__.py`、`persona/__init__.py`、`cognition/__init__.py`、`infrastructure/__init__.py` | 包说明改为现有职责，去除“全部尚未实现”的过期文字 |
| `README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md` | 更新现状、目录、清理结论及用户验证步骤 |
| `metadata.yaml`、`PACKAGING_FILES_v0.4.0.txt` | 更新版本和全量文件清单；没有生成安装包 |

本轮清理了 23 个没有运行逻辑/加载入口的占位文件，逐项理由见 [项目结构复核](PROJECT_STRUCTURE.md)。原来的 `memory/`、`social/` 等空目录不再预建；长期功能规划仍保留。既有业务接口、历史事件来源字符串及 namespace 加载方式保持兼容。

## 如何使用

在 AstrBot 中保留已有 AI 回复配置，并绑定 QQ 私聊（`qq_private_ids`，必要时设置 `qq_platform_ids`）。保存配置后重载。新配置默认如下，对应插件配置里的 `chat_context`：

```json
{
  "enabled": true,
  "private_only": true,
  "include_feedback": true,
  "max_chars": 1000,
  "max_feedback_chars": 160,
  "feedback_max_age_seconds": 300
}
```

固定约束附加到原 `system_prompt`；每轮状态放入 `extra_user_content_parts`，避免动态状态改变系统前缀。按 AstrBot v4.29.3 官方请求实体及[请求钩子说明](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.3/docs/zh/dev/star/guides/listen-message-event.md)核对 API；真实 QQ 联调仍由用户验证。

默认最多附加 1000 字符（包括固定约束及分隔符），反馈最多 160 字符、保留 300 秒内的最近一条；活动优先展示运行中、再暂停和待开始，最多三项。必要时先缩短/移除反馈，再减少活动信息。原聊天历史不会被本插件裁剪；附加文本可能进入 AstrBot 的正常聊天历史，累计历史长度仍由框架自身管理。

- `/persona_context now`：重新读取当前状态、预览拟附加内容，不请求模型、不结算状态。
- 正常聊一条后执行 `/persona_context last`：查看本次加载中实际附加到最近请求的内容。它与 `now` 可能不同，因为时间和状态已变化。
- `/persona_status`：查看持久化状态。上下文会按读取时刻投影自然变化，可能与这份已结算快照有小差别；这不会单独增加版本号。

群聊默认不注入。若设置 `private_only=false`，仍需绑定群且本次明确 @/前缀/回复唤醒；普通群消息不会因此触发新的模型请求。`last` 仅保存最近 64 个会话的请求预览，重载后为空；持久化状态和新反馈指针不会因清空预览丢失。

## 反馈范围与接入约束

角色生活状态由所有绑定会话共享；反馈文字只使用与当前 `scope_id` 完全匹配的内容。旧反馈没有会话归属，不猜测、不批量回填、不自动公开。当前模拟休息/进食等不会自动生成内容总结，预览中没有反馈属于正常情况。未来执行器上报阶段内容时，需要显式传入来源会话，例如：

```python
from persona_agent.activity.feedback import ActivityFeedback

async def report_stage(host, activity_id, exact_scope_id):
    return await host.report_feedback(
        activity_id,
        ActivityFeedback(checkpoint_id="content-001", task="neutral", summary="本阶段处理了新的内容。"),
        scope_id=exact_scope_id,  # 使用真实来源会话的完整 scope_id
    )
```

完成总结沿用完成事件本身的 `scope_id`。相同 checkpoint 重试必须复用原范围；不能通过重投把旧反馈改成其他会话可见。

约束提示保留 `simulated`/`inferred` 等来源差异，反馈位于数据块中，不作为系统指令。它只能引导模型表达，不能保证模型逐句遵守；本版没有生成后硬过滤，也没有实现真实视频、游戏、长期记忆或主动社交。

## 数据库和安装文件

首次打开自动从旧版本升级到 **schema v4，七张表**。新增 `feedback_heads`，在 `activity_instances` 上增加状态索引；旧活动的索引首次启动回填，成本随旧活动数量增加，后续聊天不遍历历史终态活动或历史事件。管理查询 `latest_activity_feedback()` 仍有历史扫描，本轮没有改成全项目无扫描。

升级前若需保留回退能力，应停插件后备份整个插件数据目录（含数据库及可能存在的 WAL/SHM）。旧 v0.3.x 不支持打开 v4；需要回退时同时恢复升级前数据，不能直接把升级后的库交给旧代码。本轮没有访问或迁移用户正在使用的插件数据库。

版本：`v0.4.0`，依赖不变。请按 [PACKAGING_FILES_v0.4.0.txt](PACKAGING_FILES_v0.4.0.txt) 全量覆盖源码并保留相对路径，入口文件位于包根目录。旧空壳若因覆盖安装留在插件目录中不会再被引用；不要删除独立的数据目录。

## 验证

实现者只做静态检查，**没有运行单元测试，也没有生成 ZIP**。请在项目根目录执行：

```powershell
python -m unittest discover -s tests -p "test_chat_context.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

QQ 检查：绑定私聊发送普通问题后查看 `last`；确认原人设继续生效、没有额外一条模型回复；重载后 `now` 可用而 `last` 清空。关闭 `chat_context.enabled` 后，不再附加数据；默认群聊及未绑定会话不应出现本插件的状态块。模型是否自然地表现疲劳、情绪仍需实际对话观察，不能用提示预览替代该项验证。
