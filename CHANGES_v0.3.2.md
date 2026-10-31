# v0.3.2：聊天评价开关与时间戳审计

针对普通互动路径的总开关回归，以及未来时间戳钳制后的来源追溯补充。本次只做静态检查，测试由用户运行；不生成安装包。依赖和数据库表结构不变，仍为 schema v3。

## 改动

| 文件 | 修改内容 |
| --- | --- |
| application/evaluation.py | 普通互动也检查 evaluation.enabled。关闭总开关后，明确情感与普通定向聊天均不改变情绪或需求 |
| application/qq.py | 新增稳定的时间调整记录 ID 与纯构造函数，保存平台原始时间、首个接收时刻和偏差秒数 |
| application/host.py | 重投先复用已保存的规范事件；仅对首次收到的未来时间进行钳制并申请审计，不因时钟追上而重新结算 |
| application/persistence.py | 原消息、时间审计和评价在同一事务保存；任一写入失败全部回滚 |
| tests/test_local_evaluation.py | 总开关关闭时，普通/正向/负向/空消息和定向群聊的状态保持不变；重开后未被关闭期间的消息占用冷却 |
| tests/test_astrbot_host.py | 保留原始时间、钳制前后重投、重载重投、内容冲突、旧无审计消息兼容、审计写失败回滚 |
| metadata.yaml、README.md、tests/README.md | 版本与验证说明 |

## 行为边界

保留普通定向聊天可缓解社交需求的行为（chat_engaged 默认 -0.3），这条路径不虚构情绪，仍受心理需求冷却和限幅控制。evaluation.enabled=false 会关闭这条路径，也关闭命中情感规则的状态效果；消息和中性回执仍可保存。

首次收到平台时间在未来的消息时，规范消息 timestamp 仍钳制到接收时刻。原始时间单独保存在 INTERNAL.message_time_adjusted 事件中，causation_id 指向 QQ 原消息，payload 含 agent_id、platform_timestamp、received_at 和 offset_seconds。该记录 ID 由角色与 QQ 事件 ID 稳定生成。它不会产生状态提案。

审计不修改 QQ 原消息 payload，不影响既有去重。重复投递始终复用首条规范消息和首条审计，包括平台时间后来已经落入过去的情形。同 ID 的内容变化仍报冲突。旧版已钳制但没有审计的消息继续兼容重投，不从后来的重投猜测或补写首次接收信息。正常时间的消息不额外生成审计。

## 手动验证

```powershell
python -m unittest discover -s tests -p "test_local_evaluation.py" -v
python -m unittest discover -s tests -p "test_astrbot_host.py" -v
python -m unittest discover -s tests -v
```

实现者未运行上述测试或真实 QQ 联调。按 PACKAGING_FILES_v0.3.2.txt 保留相对目录自行打包，main.py 位于压缩包根目录；本次没有生成 ZIP。
