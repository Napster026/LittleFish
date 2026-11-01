# v0.6.2：活动分组展示与候选补足

有意义的每个 checkpoint 仍各自保存。普通召回按活动实例选一条代表，避免一场半小时活动的 15 个阶段占满上下文；缺少其他活动候选时，按有限页数继续检索。没有删除或合并记忆，没有新增模型调用。

## 展示选择

1. 分组依据是 agent、visibility、scope、kind、provenance、speaker、about、activity_id、definition_id。活动元数据或角色主体不明确时保留原条目，不拿 topic_key 或名称猜同一活动。不同活动实例即使 topic_key 相同也分别展示。
2. 普通查询每个活动最多一条。先比较查询词的归一化命中度，同等命中度优先有内容的 completed/failed 总结，再选有信息的阶段，再考虑仅“活动完成/失败”的最小事实。各层内部沿用原评分顺序。这样直接问某一内容时，相关阶段可以胜过无关整项总结。
3. 被选中的代表保留自己的原排序位置，不能借被隐藏高分阶段的位置越过其他活动。计划的粗细展示选择沿用原规则，普通无活动元数据的事实不做活动分组。
4. 明确查询活动过程、各阶段、每个阶段、阶段内容、检查点等细节时，有限规则进入经历检索，同一活动最多两条，总结果仍受 recall_limit 限制。用户自己的活动/未来计划不会因这些词转成角色经历查询。复杂任意问法或准确 checkpoint 编号查找不在本轮承诺范围。
5. 分组只作用于 recall/search 及其聊天注入。list/show/history 仍能查看全部 checkpoint，正文、修订、来源和原事件不变。对模型最终可见条数仍取决于字符预算，候选允许两条不代表必然全部注入。

## 候选补取

先沿用 candidate_limit 读取初始近期页或每词索引页，按已有权限、时效、相关度、重要度、时间衰减评分，再分组展示。如果不足 recall_limit，继续读取初始满页流的下一页。

- 近期流是 memories 当前会话或受限 self 查询；词项流是当前范围的 memory_terms。每次使用 before(sort_key) 独占边界、降序读取，游标严格推进。
- 所有流轮流补取，**合计最多八页额外预算**，不是每个词/每个范围各自八页；每页最多 candidate_limit（1..100）条。补满立即停止，预算耗尽允许少返回，避免为了补齐而扫描完整历史。
- 词项形成的候选 ID 按最多 128 条批量加载，已解析 ID 不重复读取。初始两路每词页数仍有 QUERY_TERM_LIMIT=16 的上界；应用层加载和解析条数有固定上界，不遍历全量历史。
- 所有新补页继续应用原会话/角色、自身模拟事实门禁、当前词项版本、时间有效性。无归属摘要不会因为补取而变成公开内容，不读取历史事件或终态活动列表。
- 只读，不补建反馈、不更新记忆时间、不修改角色需求/情绪，不生成结算。数据库仍为 schema v6、十二张表、词项版本 2，不新增迁移、配置字段或依赖。

## 按文件修改

| 文件 | 内容 |
| --- | --- |
| `memory/retrieval.py` | 活动分组键、有限细节意图、代表选择与每活动 1/2 条展示上限；保持计划去重；额外页数常量 |
| `memory/persistence.py` | 近期/词项候选流、游标补取、全局八页预算、补满即停、已加载 ID 去重；统一再评分和展示选择 |
| `tests/test_activity_recall.py`（新增） | 纯展示边界及代表排序；15 checkpoint 的近期/关键词/空查询补足；细节上限；只读、作用域/self 隔离、全局页数和游标推进；未运行 |
| `_conf_schema.json` | 补充展示与补取说明，配置默认值不变 |
| `metadata.yaml`、`README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md` | v0.6.2 版本、结构、行为和本地验收说明 |
| `CHANGES_v0.6.2.md`、`PACKAGING_FILES_v0.6.2.txt` | 按文件笔记和全量打包清单 |

## 本地验证

```powershell
python -m unittest discover -s tests -p "test_activity_recall.py" -v
python -m unittest discover -s tests -p "test_activity_memory.py" -v
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -v
```

测试辅助函数的活动标识参数使用 activity_id，保留 activity 作为记忆模型元数据的覆盖字段；缺少元数据用例会明确构造 activity=None，并先检查夹具字段。避免把 None 错传为 ActivityMemoryContext.activity_id。此修正仅涉及测试构造，版本仍为 v0.6.2，未复跑测试。

新增测试在临时库保存 15 个同活动 checkpoint 和两个更早的其他活动，使用 candidate_limit=2，验证补取后普通召回有三个活动；列表仍为 17 条。另测只存在一个活动时达到预算返回一条、多范围共享预算、满额不再查询下一页以及原始数据/结算完全不变。

实现者仅做静态检查，未运行测试、实际数据库或 QQ，不生成 ZIP。版本 **v0.6.2**，按 [PACKAGING_FILES_v0.6.2.txt](PACKAGING_FILES_v0.6.2.txt) 自行全量打包；保留相对路径，测试、缓存、数据库和旧打包清单不进入安装包。
