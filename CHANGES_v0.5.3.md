# v0.5.3：记忆索引修复的单行隔离与启动降级

待重建记忆的一条坏 JSON、索引字段不一致或修订回填冲突，不再阻断整个插件启动。坏行保留 index_version=0 和原数据，其他正常行可修复；本次启动跳过坏行，下次加载再重试。日志、启动提示和 QQ 状态可定位记忆索引问题。

## 修复策略

1. 显式每批最多 500 条，按 memory_id 升序和 after 游标翻页；整批失败也会推进游标，同一轮不反复撞回坏行。
2. JSON/索引校验失败直接跳过。写词项及回填 metadata 使用单行保存点，单行 StorageError/ValueError 会回滚该行全部改动，再继续后续记录；之前正常行的修复保留在外层事务中。
3. 失败行保持待修复标记，旧正文、修订、来源、回执、角色状态均不改。正常召回、归并候选和 list 仅查询当前索引版本，避免坏行又让消息处理或上下文组装失败。
4. 日志最多列出五个异常 ID/错误类型及一条总计，不打印记忆正文、坏 JSON 或完整校验异常；启动报告保存在内存。
5. 连接不可用、保存点建立/回滚失败、取消以及其他未预期系统异常仍向上传递，不把不可用数据库伪装成可继续运行。

原 fetch_all 默认 limit=100，因此旧代码不是无界读取。本次改成显式 500 的批次，主要补上失败后的游标推进及单行回滚；不会把 continue 加进原循环后无限读取同一坏行。

SQL 结构升级与业务索引重建仍是不同事务。结构升级失败按原规则回滚；结构已升级、索引存在坏行时，本版可降级启动。数据库文件本身不可用、角色或活动数据损坏等其他问题不在此降级范围内。

## QQ 诊断与清理

```text
/persona_status
/persona_memory pending
/persona_memory pending 上页最后ID
/persona_memory delete 记忆ID
```

status 在本次加载跳过异常行时显示提示；这个数是本次加载统计，不是删除后实时刷新的待修复总数。pending 只列当前角色、当前绑定会话的待修复 ID 和元数据，每页 10 条，不解析或展示坏 JSON；其他会话不可见。日志可查看启动跳过的 ID 样例。

管理员可删除指定坏行，删除入口按角色/会话及行元数据授权，不要求先反序列化正文。删除仍原子清理正文、修订、来源关联和索引，并保留无正文的去重回执，防止旧消息重投后复活。没有自动删除或重写坏数据；也不自动把坏 JSON 猜成正常记忆。

索引修复成功后正常 list/召回自动恢复。待修复行的 show/history 会提示索引未就绪；pending 和 delete 提供诊断、清理入口。原 QQ 事件日志与 AstrBot 历史仍由各自机制管理。

## 按文件修改

| 文件 | 修改内容 |
| --- | --- |
| `memory/persistence.py` | 显式 500 条游标批次、单行错误分类/保存点回滚、限量日志、修复报告；正常使用过滤待修复行；增加 pending；删除不依赖坏 JSON 解析 |
| `memory/models.py` | 新增不可变 MemoryIndexRepairReport，仅计数与 ID 样例，不含记忆正文 |
| `persistence/ports.py` | 通用事务会话增加 savepoint 契约，失败只回滚此块 |
| `infrastructure/storage.py` | 实现 SQLite 保存点，支持嵌套、异常/取消回滚，使用内部生成名称，不接收用户 SQL |
| `application/persistence.py` | 修复事务提交后保存启动报告，增加 pending 只读入口 |
| `application/host.py` | 状态提示及 pending 管理命令，继续原管理员/绑定/私聊限制 |
| `application/memory.py` | 待修复元数据列表及分页提示；删除结果允许修订号未知 |
| `main.py` | 加载成功但记忆降级时明确警告，附少量 ID 样例；帮助增加 pending |
| `_conf_schema.json` | 增加 pending 诊断提示，配置默认值不变 |
| `tests/test_memory.py` | 坏 JSON/好行并存、健康召回和新写入、按作用域诊断/删除、部分写入回滚、changed=0、下次重试、501 条全坏批次游标、日志不含正文、全局错误不误吞；未运行 |
| `tests/test_astrbot_host.py` | 坏记忆下 host.ready、状态诊断、上下文可用及管理员清理；未运行 |
| `tests/test_persistence_layers.py` | 内存适配器跟进保存点；SQLite 保存点的局部/嵌套/取消回滚及失效会话拒绝；未运行 |
| `README.md`、`PROJECT_STRUCTURE.md`、`tests/README.md`、`metadata.yaml`、`PACKAGING_FILES_v0.5.3.txt` | 更新本版说明和全量文件清单 |

没有修改评分、展示去重、归并关系或记忆内容语义。数据库仍为 **schema v6、十二张表**，词项版本仍为 2；相较 v0.5.2 不增加结构迁移或全量重建。没有额外模型请求或新增依赖。

## 用户验证

实现者仅做静态检查，未运行测试、实际数据库升级或生成 ZIP：

```powershell
python -m unittest discover -s tests -p "test_memory.py" -v
python -m unittest discover -s tests -p "test_persistence_layers.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

损坏数据场景由临时测试库模拟，不需要在实际 QQ 插件库中手工破坏记录。正常 QQ 的记忆添加、召回、归并和删除流程应继续工作。

版本为 **v0.5.3**，按 [PACKAGING_FILES_v0.5.3.txt](PACKAGING_FILES_v0.5.3.txt) 自行全量打包并保留目录。由更早版本升级时仍遵循原 v6 迁移备份说明。
