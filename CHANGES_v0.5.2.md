# v0.5.2：召回结果展示去重

召回评分完成后、recall_limit 截断之前选择展示代表，避免同一计划的粗细表述同时进入聊天上下文。只改变召回结果，不修改归并规则，不写数据库，不删除记忆或来源。

## 规则与边界

用户建议的 `(claim_type, topic_key, target_start_at, target_end_at)` 在当前实现中偏粗：`plan:outing` 同时用于“逛街”和“去医院”，按该键全压成一条会漏掉独立安排。本版在这个方向上增加主体、来源和具体安排边界：

- 仅处理有明确主体、目标时间区间、topic_key/detail_key 的 scope 计划。非计划、无日期、主体或键未知的记录保留原有排序。
- 同一角色/会话、主体/发言者、记忆类型、provenance、时间区间/精度和计划状态才可组成候选展示组，避免取消/完成或模拟/观察信息相互遮盖。
- 完全相同的具体安排保留排名最高的一条。
- `plan:outing` 中只有一种具体安排时，“出门”和该具体安排按同一展示组处理，保留排名最高者；不额外提高细节版本的排名。
- 同题同日若有多个不同的具体安排，具体安排分别保留，粗表述因关联不明也保留；同一粗表述自身的重复仍可去重。
- 先完成这层筛选，再取 recall_limit 条，重复项不会消耗最终展示名额。

例如数据库因超出归并窗口保留“我明天会出门”和“我明天去逛街看电影”两条：二者只有一个明确具体安排时，模型收到最高排名那一条；`/persona_memory list` 仍显示两条，ID、修订和来源均不改变。若最高排名恰好是粗表述，仍按排序保留粗表述，不在展示阶段合成新的记忆正文。

`/persona_memory search` 是召回预览，因此也应用展示去重；`list/show/history` 保持原有数据视图。已有提示预算仍可能进一步裁剪输出，来源引用仅包含最终实际注入的记忆。

## 按文件修改

| 文件 | 修改 |
| --- | --- |
| `memory/retrieval.py` | 新增纯函数 pick_for_display，按元数据和具体安排选择展示代表；不改变评分和 decide_update |
| `memory/persistence.py` | 排序后调用展示选择，再执行结果数量限制；不改写入、删除、索引和迁移路径 |
| `tests/test_memory.py` | 粗细版本选择最高排名、非相邻重复不占限额、独立具体安排、人物/来源/状态边界、未知元数据及非计划保留；集成验证库里两条、召回一条且正文/修订/来源/角色不变；未运行 |
| `README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md` | 更新本版说明 |
| `metadata.yaml`、`PACKAGING_FILES_v0.5.2.txt` | 版本及全量打包清单 |

相较 v0.5.1，数据库仍为 **schema v6、十二张表**，没有新增迁移、重新索引或依赖。由更早版本升级时，仍需遵循 [v0.5.1 的数据升级说明](CHANGES_v0.5.1.md)。不调用额外模型，不生成 ZIP。

## 用户验证

实现者只做静态检查，未运行单元测试或 QQ 测试：

```powershell
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -v
```

QQ 验证：已有粗细两条计划时，先用 list 确认两条都在，再用 search 预览召回，或正常询问“我的未来计划是什么”后看 `/persona_context last`。同一粗细组只应注入一个 ID；两项不同的具体安排应分别保留（仍受总数及字符预算限制）。

版本 **v0.5.2**，按 [PACKAGING_FILES_v0.5.2.txt](PACKAGING_FILES_v0.5.2.txt) 自行全量打包并保留目录结构。
