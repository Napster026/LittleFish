# v0.5.1：召回评分、辅助索引与终态保护

按用户反馈调整记忆召回：相关度归一化、importance 进入主评分、默认 30 天半衰；入库与查询共用虚词清洗；取消/完成不能通过普通终态反馈互相覆盖；归并时间范围改用独立列的显式条件。没有额外模型调用、新依赖或 ZIP。

## 评分

原建议的 `overlap / 查询词数` 是覆盖率；同一次查询的分母固定，仍会偏向能命中更多词的长文本。如果 score 还保留原始命中次数，也会绕开归一化。因此本版使用覆盖率与词项精度的调和平均（Dice）：

```text
coverage = overlap / 查询去重词数
precision = overlap / 记忆索引去重词数
similarity = 2 * coverage * precision / (coverage + precision)

age_days = max(0, 当前时间 - 最近关联来源时间) / 一天
rank = (base_score + 2 * similarity)
       * (0.5 + 0.5 * importance)
       * 0.5 ** (age_days / recall_half_life_days)
```

没有交集时 similarity=0。base_score 表示已知的结构化相关性：类型/日期路径为 2、空查询近期预览为 1、纯关键词路径为 0；它不再使用原始命中次数。排序平手时才使用最近来源时间、重要度和稳定 ID。

新增 `memory.recall_half_life_days`，默认 30，允许有限正数；旧配置缺失时使用默认值。衰减只影响排序，不删除记录、不改变有效期，也不意味着长期偏好过了 30 天就失效。确认同一事实可更新最近来源时间；索引升级不会让旧记忆变新。

关键词路径仍需至少一个索引命中，类型路径仍需对应类别；importance 不能让完全没有进入候选池的任意同义表达自动出现。本版没有语义向量检索。

## 候选池与查询成本

之前按命中数先截断候选、再调整权重，会使高重要度候选提前丢失。本版最多使用 16 个查询词，每词查询最近 candidate_limit 条索引结果，合并后统一评分，再取 recall_limit 条。

默认 candidate_limit=20，关键词候选池最多 320 条；每批最多 128 个 ID，通过通用 where_in 参数读取正文，避免为每条候选单独访问数据库。最大配置时仍有固定上界。归并候选限制仍为最近一小时最多 20 条，类型查询也保持原来的有界查询。数据读取始终限制角色和会话。

## 辅助分词

仅对搜索输入移除 `了 的 吗 呢 啊 吧 我 你 他 她 就 才 还 又`，然后生成中文双字及英文词。例如“逛了街”和“逛街”都能生成“逛街”。不删除“不、没、无、未”等否定词；原文、人物、偏好对象归并键与原始关键词记录均不改写。

已有关键词与正文在索引生成时应用同一规则。虚词清洗是低成本缓解，不能识别所有词界；“街看”等跨词 bigram 仍可能存在。权重归一化会降低多余词项的优势，但本版不宣称完整中文分词或语义理解。

## 按文件修改

| 文件 | 内容 |
| --- | --- |
| `memory/retrieval.py` | 去虚词、统一 indexed_terms、Dice 相关度、重要度/衰减评分；终态检查提前到普通 cancel/complete 分支之前。明确更正仍按原规则单独处理 |
| `memory/models.py` | 新增严格的 recall_half_life_days，拒绝布尔值、字符串、非有限数及非正值 |
| `memory/persistence.py` | 词项候选合并后批量加载和主评分，取消命中次数预截断；新写入维护索引版本及独立时间元数据；启动重建旧索引；归并使用 updated_at 的 >=/<= 条件 |
| `application/persistence.py` | 启动时在业务事务中修复旧记忆索引，随后再启动其他工作 |
| `persistence/ports.py` | 新增可选的有界 where_in 通用查询参数 |
| `infrastructure/storage.py` | schema v5→v6 迁移，新增 memories.updated_at/index_version 元数据和待重建/时间索引；批量条件仍使用表列白名单及参数绑定 |
| `_conf_schema.json` | 新增半衰期默认值及提示 |
| `tests/test_memory.py` | 长文填充、重要度、半衰/重新确认、词形/否定、预截断、取消后完成、原索引重建、失败回滚/重试、正文/版本/来源保留及时间边界；未运行 |
| `tests/test_persistence_layers.py` | 内存适配器跟进批量查询契约，验证 SQLite 批量查询空集/条件交集/白名单/数量上限；未运行 |
| `tests/schema_fixtures.py` | 增加临时库 v5 布局夹具，保留记忆正文和关联；不能用于生产降级 |
| `tests/test_activity_storage.py`、`tests/test_scheduler.py`、`tests/test_local_evaluation.py`、`tests/test_chat_context.py`、`tests/test_storage.py` | 当前 schema 预期改为 v6，未知版本夹具使用 v7；未运行 |
| `README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md`、`metadata.yaml`、`PACKAGING_FILES_v0.5.1.txt` | 更新本版说明、实际结构和全量清单 |

取消过的计划不会被普通 complete 反馈改成完成，完成过的计划也不会被普通 cancel 覆盖；不确定的新陈述可单独保存。历史原文完全相同的终态重投仍可去重关联；明确更正仍受单独的候选规则约束。

## 数据升级

数据库升级为 **schema v6，仍为十二张表**。新增 updated_at 索引列以直接表达来源时间区间，保留 sort_key 用于列表游标。新增 index_version，v5 的旧行先标记为待重建；业务启动按标记索引分批读取、重建搜索词并填入时间元数据。

一次性重建只读已有记忆，不遍历 QQ 事件日志；不改 record_json、修订号、来源关联、去重回执或角色版本，不新增情绪/需求结算。以后启动只查询待处理标记，不重复解析全部历史记忆。

结构迁移失败会回滚结构和版本；结构迁移成功但词项重建失败时，重建事务回滚，数据库已为 v6，旧行仍待处理，下次用本版启动会重试。请不要把这份库直接交给旧代码。

更新前停用插件，备份整个数据目录。旧 v0.5.0 不支持打开 v6；回退时恢复升级前备份。本次没有访问或修改用户实际插件数据库。

## 用户验证

实现者只做静态检查，未运行测试、真实 QQ 联调或生成安装包：

```powershell
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_persistence_layers.py" -v
python -m unittest discover -s tests -v
```

QQ 管理命令不变。可手动添加“我逛了街”，再用 `/persona_memory search 逛街` 验证词形；原有“明天出门→明天逛街看电影→未来计划”流程应继续可用。重要度和时间衰减的确定性对比由本地用例覆盖，普通 QQ 命令目前不能伪造来源日期或直接设重要度。

版本为 **v0.5.1**，按 [PACKAGING_FILES_v0.5.1.txt](PACKAGING_FILES_v0.5.1.txt) 自行全量打包，保留目录结构。
